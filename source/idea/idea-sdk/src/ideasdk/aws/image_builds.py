"""
Image build bookkeeping shared by the scheduler (compute images) and the virtual desktop
controller (desktop images): one DynamoDB record per base OS and architecture, a runner
that executes a build in a thread and keeps that record current, and the helpers the
Custom AMIs page uses to classify what a cluster runs today.
"""

import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

from ideadatamodel import ImageBuildRecord, exceptions
from ideasdk.utils import Utils

BUILD_STATUS_BUILDING = 'building'
BUILD_STATUS_COMPLETE = 'complete'
BUILD_STATUS_FAILED = 'failed'

# a build takes about 20 minutes; a record still 'building' after this long belongs to a
# thread that died with its process, and the page should say so instead of spinning
STALE_AFTER = timedelta(hours=3)

# matches idea-compute-node-rocky9-v08312026-214021-3f9a; the 4 hex chars keep two builds
# started in the same second distinct (InvalidAMIName.Duplicate)
BUILD_STAMP = re.compile(
    r'-v(\d{2})(\d{2})(\d{4})-(\d{2})(\d{2})(\d{2})(?:-[0-9a-f]{4})?$'
)

# the name a compute image build carries. lives here because the scheduler builds these images and
# the administrator has to recognise one during an upgrade
COMPUTE_IMAGE_PREFIX = 'idea-compute-node-'

# the bootstrap tags the builder idea:AmiBuilderStatus when it is done; a builder that has
# not reported by then is stuck and gets stopped instead of billing forever
BUILDER_READY_TIMEOUT_SECONDS = 3600

# a builder that failed is stopped, not terminated, so its logs can be read; after this
# long nobody is reading them and the sweep terminates it
STOPPED_BUILDER_MAX_AGE = timedelta(hours=24)

# the records table is created by whichever module process gets there first
TABLE_READY_TIMEOUT_SECONDS = 300

THROTTLE_CODES = ('Throttling', 'ThrottlingException', 'RequestLimitExceeded')

# tags the sweep uses to find builder instances it may terminate
IMAGE_BUILD_TAG = 'idea:ImageBuild'
STOPPED_AT_TAG = 'idea:ImageBuilderStoppedAt'

# build all starts one builder per base stack. beyond this the rest are skipped with a
# visible reason rather than launched into an account limit
MAX_CONCURRENT_BUILDS = 16

# the builder only runs a bootstrap script, so the allowlist is general-purpose sizes plus
# one GPU size per architecture
BUILDER_INSTANCE_TYPES = {
    'x86_64': (
        'c5.large',
        'c5.xlarge',
        'c6i.large',
        'c6i.xlarge',
        'm6i.large',
        'm6i.xlarge',
        'g4dn.xlarge',
        'g5.xlarge',
    ),
    'arm64': ('c6g.large', 'c6g.xlarge', 'm6g.large', 'm6g.xlarge', 'g5g.xlarge'),
}
DEFAULT_ARM64_BUILDER_INSTANCE_TYPE = 'm6g.large'

_RECORD_TIMESTAMPS = ('started_on', 'finished_on')


def build_stamp(image_name: Optional[str]) -> Optional[datetime]:
    """the build time encoded in an IDEA image name (-vMMDDYYYY-HHmmss), or None"""
    if not image_name:
        return None
    match = BUILD_STAMP.search(image_name)
    if match is None:
        return None
    month, day, year, hour, minute, second = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def is_throttle(error: BaseException) -> bool:
    """a describe call refused for rate, which a poll loop waits out instead of failing a build"""
    response = getattr(error, 'response', None) or {}
    return response.get('Error', {}).get('Code') in THROTTLE_CODES


def stop_builder(context, instance_id: str, logger) -> None:
    """stop a builder instance for inspection and stamp it so the sweep can terminate it later"""
    try:
        ec2 = context.aws().ec2()
        ec2.stop_instances(InstanceIds=[instance_id])
        ec2.create_tags(
            Resources=[instance_id],
            Tags=[{'Key': STOPPED_AT_TAG, 'Value': str(int(time.time()))}],
        )
    except Exception as e:
        logger.error(f'could not stop builder instance {instance_id}: {e}')


def terminate_old_stopped_builders(context, logger) -> List[str]:
    """terminate this module's builder instances that have sat stopped for over a day; returns their ids"""
    terminated: List[str] = []
    try:
        ec2 = context.aws().ec2()
        result = ec2.describe_instances(
            Filters=[
                {'Name': 'instance-state-name', 'Values': ['stopped']},
                {'Name': 'tag-key', 'Values': [STOPPED_AT_TAG]},
                {'Name': 'tag:idea:ModuleId', 'Values': [context.module_id()]},
            ]
        )
        cutoff = time.time() - STOPPED_BUILDER_MAX_AGE.total_seconds()
        for reservation in result.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                try:
                    stopped_at = float(tags.get(STOPPED_AT_TAG, ''))
                except ValueError:
                    continue
                if stopped_at < cutoff:
                    terminated.append(instance['InstanceId'])
        if terminated:
            ec2.terminate_instances(InstanceIds=terminated)
            logger.info(
                f'terminated builder instances stopped for over a day: {terminated}'
            )
    except Exception as e:
        logger.error(f'could not sweep stopped builder instances: {e}')
    return terminated


def unique_build_version() -> str:
    """MMDDYYYY-HHmmss plus 4 hex chars, the version half of an IDEA image name"""
    stamp = datetime.now(tz=timezone.utc).strftime('%m%d%Y-%H%M%S')
    return f'{stamp}-{uuid.uuid4().hex[:4]}'


def builder_type_architecture(instance_type: Optional[str]) -> Optional[str]:
    """the architecture an allowlisted builder type runs, or None when it is not on the list"""
    for architecture, types in BUILDER_INSTANCE_TYPES.items():
        if instance_type in types:
            return architecture
    return None


def default_builder_instance_type(architecture: Optional[str], x86_default: str) -> str:
    """the builder size for an image's architecture: the module's own x86_64 default, m6g.large for arm64"""
    if (architecture or 'x86_64') == 'arm64':
        return DEFAULT_ARM64_BUILDER_INSTANCE_TYPE
    return x86_default


def check_builder_type_architecture(
    instance_type: Optional[str], architecture: Optional[str]
):
    """an explicit builder type must run the image's architecture; EC2 refuses the mix at launch, this says so first"""
    belongs_to = builder_type_architecture(instance_type)
    wanted = architecture or 'x86_64'
    if belongs_to and belongs_to != wanted:
        raise exceptions.invalid_params(
            f'{instance_type} is {belongs_to}; a {wanted} image needs one of: '
            f'{", ".join(BUILDER_INSTANCE_TYPES.get(wanted, ()))}'
        )


def check_builder_instance_type(
    instance_type: Optional[str], architecture: Optional[str] = 'x86_64'
):
    """an empty type means the builder default; anything else must be on the allowlist for the architecture"""
    if Utils.is_empty(instance_type):
        return
    wanted = architecture or 'x86_64'
    allowed = BUILDER_INSTANCE_TYPES.get(wanted, ())
    if instance_type in allowed:
        return
    check_builder_type_architecture(instance_type, wanted)
    raise exceptions.invalid_params(
        f'instance_type must be one of: {", ".join(allowed)}'
    )


def sanitize_aws_message(text: Optional[str]) -> str:
    """an AWS error message without the identifiers it tends to carry (ARNs, account ids)"""
    text = re.sub(r'arn:[^\s"\']+', '<arn>', text or '')
    return re.sub(r'\b\d{12}\b', '<account>', text)


def image_state(image_name: Optional[str], built_prefix: str) -> str:
    """'built' when the name carries the IDEA builder prefix, 'stock' otherwise"""
    if image_name and image_name.startswith(built_prefix):
        return 'built'
    return 'stock'


def is_built_image(ec2_client, image_id: Optional[str]) -> bool:
    """
    True when this account owns the image, which for IDEA means a build rather than a
    vendor's stock image. False for anything else, including an id that no longer exists.
    """
    if not image_id:
        return False
    try:
        result = ec2_client.describe_images(ImageIds=[image_id], Owners=['self'])
    except Exception:
        return False
    return len(result.get('Images', [])) > 0


def describe_images_by_id(ec2_client, image_ids: List[str]) -> Dict[str, Dict]:
    """
    describe_images for a set of ids, keyed by id. one unknown id fails the whole batch, so
    a failed batch is retried one id at a time and unknown ids are absent from the result.
    """
    ids = sorted({image_id for image_id in image_ids if image_id})
    found: Dict[str, Dict] = {}
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        try:
            result = ec2_client.describe_images(ImageIds=chunk)
            for image in result.get('Images', []):
                found[image['ImageId']] = image
        except Exception:
            for image_id in chunk:
                try:
                    result = ec2_client.describe_images(ImageIds=[image_id])
                    for image in result.get('Images', []):
                        found[image['ImageId']] = image
                except Exception:
                    continue
    return found


def newest_owned_image(ec2_client, name_pattern: str) -> Optional[Dict]:
    """the newest available image this account owns whose name matches the pattern"""
    result = ec2_client.describe_images(
        Owners=['self'],
        Filters=[
            {'Name': 'name', 'Values': [name_pattern]},
            {'Name': 'state', 'Values': ['available']},
        ],
    )
    images = result.get('Images', [])
    if not images:
        return None
    return max(images, key=lambda image: image.get('CreationDate', ''))


class BuildReporter:
    """
    print through a cli context when there is one, otherwise log, so the AMI builders run
    unchanged under ideactl and inside the module apps.
    """

    def __init__(self, context, logger_name: str):
        self._cli = context if callable(getattr(context, 'spinner', None)) else None
        self._logger = context.logger(logger_name)

    def info(self, message):
        (self._cli.info if self._cli else self._logger.info)(message)

    def success(self, message):
        (self._cli.success if self._cli else self._logger.info)(message)

    def warning(self, message):
        (self._cli.warning if self._cli else self._logger.warning)(message)

    def error(self, message):
        (self._cli.error if self._cli else self._logger.error)(message)

    def print(self, message):
        (self._cli.print if self._cli else self._logger.info)(message)

    def spinner(self, message):
        if self._cli:
            return self._cli.spinner(message)
        return self._log_instead(message)

    @contextmanager
    def _log_instead(self, message):
        self._logger.info(message)
        yield


def new_record(
    base_os: str,
    architecture: str,
    ami_name: str,
    base_ami: str,
    requested_by: Optional[str],
    update_target: bool,
) -> ImageBuildRecord:
    return ImageBuildRecord(
        base_os=base_os,
        architecture=architecture or 'x86_64',
        ami_name=ami_name,
        base_ami=base_ami,
        requested_by=requested_by,
        host=socket.gethostname(),
        update_target=update_target,
    )


class ImageBuildRecordsDB:
    """one row per (base_os, architecture): the last build and how it ended"""

    def __init__(self, context, table_name: str):
        self.context = context
        self.table_name = table_name
        self._table_obj = None

    def initialize(self) -> 'ImageBuildRecordsDB':
        """
        create the table if needed, then sweep what a previous process on this host left
        behind. two module processes may race here; the loser waits for the winner's table.
        """
        logger = self.context.logger('image-builds')
        if not self._table_active():
            try:
                self.context.aws_util().dynamodb_create_table(
                    create_table_request={
                        'TableName': self.table_name,
                        'AttributeDefinitions': [
                            {'AttributeName': 'base_os', 'AttributeType': 'S'},
                            {'AttributeName': 'architecture', 'AttributeType': 'S'},
                        ],
                        'KeySchema': [
                            {'AttributeName': 'base_os', 'KeyType': 'HASH'},
                            {'AttributeName': 'architecture', 'KeyType': 'RANGE'},
                        ],
                        'BillingMode': 'PAY_PER_REQUEST',
                    },
                    wait=False,
                )
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') != 'ResourceInUseException':
                    raise
            deadline = time.time() + TABLE_READY_TIMEOUT_SECONDS
            while not self._table_active():
                if time.time() > deadline:
                    raise exceptions.general_exception(
                        f'{self.table_name} did not become active within {TABLE_READY_TIMEOUT_SECONDS // 60} minutes'
                    )
                time.sleep(5)
        try:
            self.sweep_orphans(logger)
        except Exception as e:
            logger.error(f'image build sweep failed: {e}')
        return self

    def _table_active(self) -> bool:
        try:
            result = (
                self.context.aws().dynamodb().describe_table(TableName=self.table_name)
            )
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'ResourceNotFoundException':
                return False
            raise
        return result.get('Table', {}).get('TableStatus') == 'ACTIVE'

    def sweep_orphans(self, logger) -> List[str]:
        """
        a process restart kills the daemon build threads, so every 'building' record this
        host owns is failed and its builder stopped. other hosts' records are left alone.
        builders stopped for over a day are terminated.
        """
        host = socket.gethostname()
        orphaned: List[str] = []
        for record in self.list_all():
            if record.status != BUILD_STATUS_BUILDING or record.host != host:
                continue
            record.status = BUILD_STATUS_FAILED
            record.error = 'the module restarted during the build'
            record.finished_on = datetime.now(tz=timezone.utc)
            self.put(record)
            orphaned.append(f'{record.base_os}/{record.architecture}')
            if record.instance_id:
                stop_builder(self.context, record.instance_id, logger)
        if orphaned:
            logger.warning(f'builds orphaned by a restart on {host}: {orphaned}')
        terminate_old_stopped_builders(self.context, logger)
        return orphaned

    @property
    def _table(self):
        if self._table_obj is None:
            self._table_obj = self.context.aws().dynamodb_table().Table(self.table_name)
        return self._table_obj

    @staticmethod
    def to_item(record: ImageBuildRecord) -> Dict[str, Any]:
        item: Dict[str, Any] = {}
        for key, value in Utils.to_dict(record).items():
            if value is None:
                continue
            if key in _RECORD_TIMESTAMPS:
                value = getattr(record, key)
                value = int(value.timestamp() * 1000)
            item[key] = value
        return item

    @staticmethod
    def from_item(item: Optional[Dict[str, Any]]) -> Optional[ImageBuildRecord]:
        if not item:
            return None
        data = dict(item)
        for key in _RECORD_TIMESTAMPS:
            value = data.get(key)
            if isinstance(value, (int, float, Decimal)):
                data[key] = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        return ImageBuildRecord(**data)

    def put(self, record: ImageBuildRecord) -> ImageBuildRecord:
        self._table.put_item(Item=self.to_item(record))
        return record

    def claim(self, record: ImageBuildRecord) -> bool:
        """
        write the 'building' row only when no build holds the key. False means another
        request already holds it.
        """
        try:
            self._table.put_item(
                Item=self.to_item(record),
                ConditionExpression='attribute_not_exists(base_os) OR #s <> :building',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':building': BUILD_STATUS_BUILDING},
            )
            return True
        except ClientError as e:
            if (
                e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'
            ):
                return False
            raise

    def get(self, base_os: str, architecture: str) -> Optional[ImageBuildRecord]:
        result = self._table.get_item(
            Key={'base_os': base_os, 'architecture': architecture or 'x86_64'}
        )
        return self.from_item(result.get('Item'))

    def delete(self, base_os: str, architecture: str):
        self._table.delete_item(
            Key={'base_os': base_os, 'architecture': architecture or 'x86_64'}
        )

    def list_all(self) -> List[ImageBuildRecord]:
        records: List[ImageBuildRecord] = []
        kwargs: Dict[str, Any] = {}
        while True:
            result = self._table.scan(**kwargs)
            records.extend(self.from_item(item) for item in result.get('Items', []))
            last_key = result.get('LastEvaluatedKey')
            if not last_key:
                return records
            kwargs['ExclusiveStartKey'] = last_key


class ImageBuildRunner:
    """
    runs one build and keeps its record current. `build` takes a progress callback (called
    with {'instance_id': ...} once the builder instance exists) and returns the image id;
    `on_success` runs after the image is ready, to repoint whatever should now use it.
    """

    def __init__(self, context, records: ImageBuildRecordsDB, logger):
        self.context = context
        self.records = records
        self._logger = logger
        # builds this process is running; a record whose thread is alive is never stale
        self._live: Dict[Tuple[str, str], threading.Thread] = {}

    def start(
        self,
        record: ImageBuildRecord,
        build: Callable[[Callable[[Dict], None]], str],
        on_success: Optional[Callable[[str, ImageBuildRecord], None]] = None,
        blocking: bool = False,
    ) -> ImageBuildRecord:
        existing = self.records.get(record.base_os, record.architecture)
        if existing is not None:
            existing = self.refresh(existing)
            if existing.status == BUILD_STATUS_BUILDING:
                raise self._already_running(record, existing)
        record.status = BUILD_STATUS_BUILDING
        record.started_on = datetime.now(tz=timezone.utc)
        record.finished_on = None
        record.image_id = None
        record.instance_id = None
        record.error = None
        # the conditional write is the lock: two requests that both read 'no build' cannot
        # both get past here
        if not self.records.claim(record):
            raise self._already_running(
                record, self.records.get(record.base_os, record.architecture)
            )
        if blocking:
            self._run(record, build, on_success, previous=existing)
        else:
            thread = threading.Thread(
                target=self._run,
                args=(record, build, on_success, existing),
                name=f'image-build-{record.base_os}-{record.architecture}',
                daemon=True,
            )
            self._live[(record.base_os, record.architecture)] = thread
            thread.start()
        return record

    @staticmethod
    def _already_running(
        record: ImageBuildRecord, existing: Optional[ImageBuildRecord]
    ):
        where = ''
        since = ''
        if existing is not None:
            if existing.instance_id:
                where = f' on {existing.instance_id}'
            if existing.started_on:
                since = f', started {existing.started_on:%Y-%m-%d %H:%M} UTC'
        return exceptions.invalid_params(
            f'a {record.base_os} ({record.architecture}) build is already running{where}{since}'
        )

    def _run(self, record: ImageBuildRecord, build, on_success, previous=None):
        def progress(update: Dict):
            for key, value in update.items():
                setattr(record, key, value)
            self.records.put(record)

        try:
            image_id = build(progress)
            record.image_id = image_id
            record.status = BUILD_STATUS_COMPLETE
            record.finished_on = datetime.now(tz=timezone.utc)
            self.records.put(record)
            if on_success is not None:
                try:
                    on_success(image_id, record)
                except Exception as e:
                    self._logger.error(
                        f'{record.ami_name}: image {image_id} is ready but the post-build step failed: {e}'
                    )
                    record.error = self._short_error(
                        e, 'image built, post-build step failed'
                    )
                    self.records.put(record)
        except SystemExit:
            # the cli prompt was declined before anything launched: leave no trace
            if previous is not None:
                self.records.put(previous)
            else:
                self.records.delete(record.base_os, record.architecture)
            raise
        except BaseException as e:
            self._logger.error(f'{record.ami_name}: build failed: {e}')
            record.status = BUILD_STATUS_FAILED
            record.error = self._short_error(e, 'build failed')
            record.finished_on = datetime.now(tz=timezone.utc)
            self.records.put(record)
            if not isinstance(e, Exception):
                raise

    @staticmethod
    def _short_error(e: BaseException, what: str) -> str:
        """
        what the record and the page carry. IDEA exceptions keep their message, an AWS
        error keeps its code and message with ARNs and account ids scrubbed, and anything
        else points at the module log, where the full text is kept at ERROR.
        """
        if isinstance(e, exceptions.SocaException) and e.message:
            return f'{e.__class__.__name__}: {e.message}'[:256]
        if isinstance(e, ClientError):
            error = e.response.get('Error', {}) if isinstance(e.response, dict) else {}
            code = error.get('Code') or 'ClientError'
            return f'{code}: {sanitize_aws_message(error.get("Message"))}'[:256]
        return f'{e.__class__.__name__}: {what}, see the module log'

    def refresh(self, record: ImageBuildRecord) -> ImageBuildRecord:
        """
        a 'building' record older than STALE_AFTER whose thread is not alive in this
        process is marked failed, once, durably, and its builder instance is stopped
        """
        if record.status != BUILD_STATUS_BUILDING or record.started_on is None:
            return record
        thread = self._live.get((record.base_os, record.architecture))
        if thread is not None and thread.is_alive():
            return record
        started = record.started_on
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if datetime.now(tz=timezone.utc) - started < STALE_AFTER:
            return record
        record.status = BUILD_STATUS_FAILED
        hours = int(STALE_AFTER.total_seconds() // 3600)
        where = f' on {record.instance_id}' if record.instance_id else ''
        record.error = (
            f'no result after {hours} hours{where}; the module probably restarted '
            f'mid-build. check the builder instance and build again'
        )
        record.finished_on = datetime.now(tz=timezone.utc)
        self.records.put(record)
        if record.instance_id:
            stop_builder(self.context, record.instance_id, self._logger)
        return record
