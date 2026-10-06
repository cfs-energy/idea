"""
Image build bookkeeping shared by the scheduler (compute images) and the virtual desktop
controller (desktop images): one DynamoDB record per base OS and architecture, a runner
that executes a build in a thread and keeps that record current, and the helpers the
Custom AMIs page uses to classify what a cluster runs today.
"""

import json
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

from ideadatamodel import (
    IMAGE_ROW_IN_FLIGHT,
    ImageBuildRecord,
    ImageKind,
    ImageRowKey,
    ImageRowStatus,
    ImageVariant,
    constants,
    exceptions,
)
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


def builder_log_link(context, instance_id: str) -> str:
    """
    CloudWatch console link to the builder's bootstrap_<instance id> stream, which carries
    the bootstrap log and the in-bake check results. the console escapes path characters
    as $25xx inside its fragment
    """
    region = context.aws().ec2().meta.region_name

    def escape(name: str) -> str:
        return quote(quote(name, safe=''), safe='').replace('%', '$')

    group = escape(f'/{context.cluster_name()}/{context.module_id()}/ami-builder')
    stream = escape(f'bootstrap_{instance_id}')
    return (
        f'https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}'
        f'#logsV2:log-groups/log-group/{group}/log-events/{stream}'
    )


# build all starts one builder per base stack. beyond this the rest are skipped with a
# visible reason rather than launched into an account limit
MAX_CONCURRENT_BUILDS = 16

# the builder only runs a bootstrap script, so the allowlist is general-purpose sizes plus
# one GPU size per architecture
BUILDER_INSTANCE_TYPES = {
    'x86_64': (
        'c6i.large',
        'c6i.xlarge',
        'c7i.large',
        'c7i.xlarge',
        'm6i.large',
        'm6i.xlarge',
        'm7i.large',
        'm7i.xlarge',
        'g4dn.xlarge',
        'g5.xlarge',
        'g4ad.xlarge',
    ),
    'arm64': (
        'c6g.large',
        'c6g.xlarge',
        'c8g.large',
        'c8g.xlarge',
        'm6g.large',
        'm6g.xlarge',
        'm8g.large',
        'm8g.xlarge',
        'g5g.xlarge',
    ),
}
DEFAULT_ARM64_BUILDER_INSTANCE_TYPE = 'm8g.large'

_RECORD_TIMESTAMPS = (
    'started_on',
    'finished_on',
    'validated_on',
    'promoted_on',
    'retry_after',
)

# custom (ad hoc) builds share the table with the pipeline rows under their own range
# key, base_os + 'x86_64#custom', so a custom build can never overwrite a managed row.
# a custom record keeps that raw key as its architecture; the pipeline skips it
CUSTOM_BUILD_SUFFIX = '#custom'

# the pipeline tags the images it bakes with this; cleanup only ever deregisters those
PIPELINE_IMAGE_TAG = 'idea:ImagePipeline'


def custom_build_architecture(architecture: Optional[str]) -> str:
    return f'{architecture or "x86_64"}{CUSTOM_BUILD_SUFFIX}'


def is_custom_record(record: ImageBuildRecord) -> bool:
    return (record.architecture or '').endswith(CUSTOM_BUILD_SUFFIX)


# a resumed row whose build cannot be picked up again is retried this many times in all
MAX_RESUME_ATTEMPTS = 2


class ImageNotValidated(Exception):
    """the promote gate refused an image: it never passed every validation check"""


def _candidate_validated(record: ImageBuildRecord) -> bool:
    if record.validated_on is None or not record.image_id:
        return False
    return record.started_on is None or record.validated_on >= record.started_on


def promote_gate(record: Optional[ImageBuildRecord], image_id: Optional[str]) -> None:
    """
    the one rule every path that repoints a target at an image goes through: the image
    is the row's candidate and that candidate validated in this run (validated_on), or
    it is the row's current/previous image put there by a promotion (promoted_on). a
    legacy or custom record has neither. raises ImageNotValidated with a sentence an
    admin can act on.
    """
    if not image_id:
        raise ImageNotValidated('no image to promote')
    if record is None or is_custom_record(record):
        raise ImageNotValidated(
            f'{image_id} is not a validated image; only images validated by Refresh and validate can be used'
        )
    if image_id == record.image_id and _candidate_validated(record):
        return
    if record.promoted_on is not None and image_id in (
        record.current_image_id,
        record.previous_image_id,
    ):
        return
    raise ImageNotValidated(
        f'{image_id} has not passed validation for {record.base_os} '
        f'{record.architecture}; run Refresh and validate on the Images page'
    )


def validated_image_ids(records: List[ImageBuildRecord]) -> set:
    """every image promote_gate would accept, across rows"""
    found = set()
    for record in records:
        for image_id in (
            record.image_id,
            record.current_image_id,
            record.previous_image_id,
        ):
            if not image_id:
                continue
            try:
                promote_gate(record, image_id)
                found.add(image_id)
            except ImageNotValidated:
                pass
    return found


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


# CreateImage reboots a running instance. After cloud-init clean, that reboot runs user-data again.
SNAPSHOT_STOP_TIMEOUT_SECONDS = 600


def wait_until_stopped(context, instance_id: str, timeout_seconds: int) -> None:
    """block until the instance is stopped, or raise when the deadline passes"""
    deadline = time.time() + timeout_seconds
    while True:
        state = None
        try:
            result = context.aws().ec2().describe_instances(InstanceIds=[instance_id])
            reservations = result.get('Reservations') or []
            instances = (reservations[0].get('Instances') or []) if reservations else []
            if instances:
                state = (instances[0].get('State') or {}).get('Name')
        except Exception as e:
            if not is_throttle(e):
                raise
        if state == 'stopped':
            return
        if time.time() > deadline:
            raise exceptions.general_exception(
                f'builder {instance_id} did not stop within {timeout_seconds // 60} minutes'
            )
        time.sleep(10)


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


def terminate_builder(context, instance_id: Optional[str], logger) -> None:
    """
    terminate a bake's builder once its image is available. the bake terminates its own
    builder, but a row resumed after a restart reaches the test launch without that step;
    terminating an instance that is already terminated changes nothing.
    """
    if not instance_id:
        return
    try:
        context.aws().ec2().terminate_instances(InstanceIds=[instance_id])
    except Exception as e:
        logger.warning(f'could not terminate builder {instance_id}: {e}')


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


def deregister_unreferenced_images(
    context, pipeline: str, name_prefix: str, protected: set, baking: set, logger
) -> List[str]:
    """
    deregister this cluster's pipeline images (tagged PIPELINE_IMAGE_TAG=pipeline) that are
    not protected, not a bake still recording its id (by name), and not named anywhere else
    (referenced_images without the image rows), then delete each one's snapshots that no
    other image shares
    """
    ec2 = context.aws().ec2()
    images = ec2.describe_images(
        Owners=['self'],
        Filters=[
            {
                'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
                'Values': [context.cluster_name()],
            },
            {'Name': f'tag:{PIPELINE_IMAGE_TAG}', 'Values': [pipeline]},
            {'Name': 'name', 'Values': [f'{name_prefix}*']},
        ],
    ).get('Images', [])
    candidates = [
        i
        for i in images
        if i['ImageId'] not in protected
        and i.get('Name') not in baking
        and i.get('State', 'available') == 'available'
    ]
    if not candidates:
        return []
    # anything else that names an image keeps it: a running host launched before a promotion,
    # a launch template version, a setting, queue or stack. the pipeline's own rows are
    # judged by `protected` (a failed candidate's row still names it). a source that cannot
    # be read raises, so nothing is deleted
    in_use = referenced_images(
        context, [i['ImageId'] for i in candidates], image_rows=False
    )
    removed = []
    for image in candidates:
        if image['ImageId'] in in_use:
            continue
        ec2.deregister_image(ImageId=image['ImageId'])
        removed.append(image['ImageId'])
        delete_unshared_snapshots(ec2, image)
    if removed:
        logger.info(f'deregistered unreferenced {pipeline} images: {removed}')
    return removed


def delete_unshared_snapshots(ec2, image: Dict) -> None:
    """delete each snapshot of a deregistered image that no remaining image uses"""
    for mapping in image.get('BlockDeviceMappings', []):
        snapshot = (mapping.get('Ebs') or {}).get('SnapshotId')
        if not snapshot:
            continue
        others = ec2.describe_images(
            Owners=['self'],
            Filters=[
                {'Name': 'block-device-mapping.snapshot-id', 'Values': [snapshot]}
            ],
        ).get('Images', [])
        if all(o['ImageId'] == image['ImageId'] for o in others):
            ec2.delete_snapshot(SnapshotId=snapshot)


# a builder image the pipeline did not tag as its own (an older release's bake, a manual build)
# is deregistered only once it is this many days old and nothing names it.
# <module>.images.legacy_cleanup_min_age_days; 0 turns legacy cleanup off
LEGACY_MIN_AGE_DAYS = 30
# a wrong reference check or setting can take at most this many legacy images per sweep
LEGACY_MAX_PER_SWEEP = 20
LIVE_INSTANCE_STATES = ['pending', 'running', 'shutting-down', 'stopping', 'stopped']


def _reference_tables(context, image_rows: bool = True) -> List[str]:
    """the cluster tables an image id can be written into: settings, queues, stacks, image rows"""
    cluster = context.cluster_name()
    config = context.config()
    tables = [f'{cluster}.cluster-settings']
    if config.is_module_enabled(constants.MODULE_SCHEDULER):
        module_id = config.get_module_id(constants.MODULE_SCHEDULER)
        tables.append(f'{cluster}.{module_id}.queue-profiles')
        if image_rows:
            tables.append(f'{cluster}.{module_id}.image-builds')
    if config.is_module_enabled(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER):
        module_id = config.get_module_id(constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER)
        tables.append(f'{cluster}.{module_id}.controller.software-stacks')
        if image_rows:
            tables.append(f'{cluster}.{module_id}.controller.image-builds')
    return tables


def referenced_images(
    context, image_ids: List[str], image_rows: bool = True
) -> Dict[str, str]:
    """
    image id -> where it is still named: any attribute of any item in the reference tables (raw
    items, so a field no model knows still counts), any instance that is not terminated, any
    launch template version. a source that cannot be read raises, so nothing is deleted.
    image_rows=False leaves out the image-builds tables, for the pipeline sweep whose rows
    decide protection themselves
    """
    found: Dict[str, str] = {}
    remaining = lambda: [i for i in image_ids if i not in found]  # noqa: E731
    dynamodb = context.aws().dynamodb()
    for table in _reference_tables(context, image_rows):
        try:
            for page in dynamodb.get_paginator('scan').paginate(TableName=table):
                for item in page.get('Items', []):
                    text = json.dumps(item, default=str)
                    for image_id in remaining():
                        if image_id in text:
                            found[image_id] = f'table {table}'
        except ClientError as e:
            # a module's table that was never created names nothing
            if e.response.get('Error', {}).get('Code') != 'ResourceNotFoundException':
                raise
    ec2 = context.aws().ec2()
    ids = remaining()
    for start in range(0, len(ids), 100):
        for page in ec2.get_paginator('describe_instances').paginate(
            Filters=[
                {'Name': 'image-id', 'Values': ids[start : start + 100]},
                {'Name': 'instance-state-name', 'Values': LIVE_INSTANCE_STATES},
            ]
        ):
            for reservation in page.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    found.setdefault(
                        instance.get('ImageId'),
                        f'instance {instance.get("InstanceId")}',
                    )
    if not remaining():
        return found
    for page in ec2.get_paginator('describe_launch_templates').paginate():
        for template in page.get('LaunchTemplates', []):
            for versions in ec2.get_paginator(
                'describe_launch_template_versions'
            ).paginate(LaunchTemplateId=template['LaunchTemplateId']):
                for version in versions.get('LaunchTemplateVersions', []):
                    image_id = (version.get('LaunchTemplateData') or {}).get('ImageId')
                    if image_id in image_ids:
                        found.setdefault(
                            image_id,
                            f'launch template {template.get("LaunchTemplateName")} '
                            f'version {version.get("VersionNumber")}',
                        )
    return found


def deregister_legacy_images(
    context, min_age_days: Optional[int], baking: set, logger, now=None
) -> List[str]:
    """
    deregister this cluster's builder images of this module that the pipeline did not tag
    (older releases, manual builds) once they are min_age_days old and nothing names them,
    oldest first and at most LEGACY_MAX_PER_SWEEP per sweep, with the snapshots no remaining
    image uses. images without this cluster's tag are never candidates
    """
    if not min_age_days or min_age_days <= 0:
        logger.info('legacy image sweep: disabled (legacy_cleanup_min_age_days is 0)')
        return []
    ec2 = context.aws().ec2()
    images = ec2.describe_images(
        Owners=['self'],
        Filters=[
            {
                'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
                'Values': [context.cluster_name()],
            },
            {'Name': f'tag:{constants.IDEA_TAG_AMI_BUILDER}', 'Values': ['true']},
            {
                'Name': f'tag:{constants.IDEA_TAG_MODULE_NAME}',
                'Values': [context.module_name()],
            },
        ],
    ).get('Images', [])
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=min_age_days)

    def created(image: Dict) -> datetime:
        return datetime.fromisoformat(image['CreationDate'].replace('Z', '+00:00'))

    # the pipeline's own images keep current + previous in their own sweep
    legacy = [
        i
        for i in images
        if not any(t.get('Key') == PIPELINE_IMAGE_TAG for t in i.get('Tags', []))
    ]
    in_flight = [
        i for i in legacy if i.get('State') != 'available' or i.get('Name') in baking
    ]
    too_new = [
        i
        for i in legacy
        if i not in in_flight and not (i.get('CreationDate') and created(i) < cutoff)
    ]
    candidates = sorted(
        (i for i in legacy if i not in in_flight and i not in too_new), key=created
    )
    removed: List[str] = []
    capped = False

    def summary(referenced: int) -> None:
        logger.info(
            f'legacy image sweep: {len(legacy)} candidates, '
            f'{referenced + len(too_new) + len(in_flight)} kept ({referenced} referenced, '
            f'{len(too_new)} too new, {len(in_flight)} in flight), {len(removed)} deleted, '
            f'capped={"yes" if capped else "no"}'
        )

    if not candidates:
        summary(0)
        return []
    references = referenced_images(context, [i['ImageId'] for i in candidates])
    for image in candidates:
        if image['ImageId'] in references:
            continue
        if len(removed) >= LEGACY_MAX_PER_SWEEP:
            logger.warning(
                f'legacy image cleanup stopped at {LEGACY_MAX_PER_SWEEP} images this '
                'sweep; the rest are checked again next sweep'
            )
            capped = True
            break
        ec2.deregister_image(ImageId=image['ImageId'])
        removed.append(image['ImageId'])
        logger.info(
            f'deregistered legacy builder image {image["ImageId"]} ({image.get("Name")}): '
            f'created {image["CreationDate"]}, older than {min_age_days} days, and no '
            'setting, queue, stack, image row, instance or launch template names it'
        )
        delete_unshared_snapshots(ec2, image)
    summary(sum(1 for i in candidates if i['ImageId'] in references))
    return removed


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
    """the builder size for an image's architecture: the module's own x86_64 default, m8g.large for arm64"""
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


def root_ebs_mapping(image: Dict) -> Dict:
    """the root device mapping from a DescribeImages image, else the first mapping"""
    mappings = image['BlockDeviceMappings']
    root = image.get('RootDeviceName')
    if root:
        for mapping in mappings:
            ebs = mapping.get('Ebs')
            if mapping.get('DeviceName') == root and isinstance(ebs, dict):
                return mapping
    return mappings[0]


def root_device_volume_gb(image: Optional[Dict]) -> int:
    """VolumeSize of the root device mapping. 0 when the image has no EBS root."""
    if not isinstance(image, dict) or not image.get('BlockDeviceMappings'):
        return 0
    try:
        return int(root_ebs_mapping(image)['Ebs']['VolumeSize'])
    except (KeyError, TypeError, ValueError):
        return 0


def builder_root_gb(requested: Optional[int], ami_root_gb: int, floor_gb: int) -> int:
    """builder disk only: max(stack or queue minimum, AMI root snapshot, floor)"""
    return max(int(requested or 0), int(ami_root_gb or 0), int(floor_gb))


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


def describe_image_or_none(ec2_client, image_id: str) -> Optional[Dict]:
    """
    one image by id; None only when EC2 says the id does not exist (InvalidAMIID.*).
    any other error (throttling, access) raises: a caller deciding whether an image is
    managed must not read "could not ask" as "custom or deleted"
    """
    try:
        result = ec2_client.describe_images(ImageIds=[image_id])
    except Exception as e:
        code = ''
        if isinstance(e, ClientError):
            code = e.response.get('Error', {}).get('Code', '')
        if code.startswith('InvalidAMIID') or (not code and 'InvalidAMIID' in str(e)):
            return None
        raise
    for image in result.get('Images', []):
        if image.get('ImageId') == image_id:
            return image
    return None


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


def resume_record(
    context, records, record: ImageBuildRecord, logger
) -> ImageBuildRecord:
    """
    pick a pipeline row back up after the thread that ran its build died with its
    process. the candidate image exists (the builder snapshots only after its checks
    passed): go on to the test launch, which waits for a pending image. otherwise stop
    the builder and queue the row once more; a second loss fails it.
    """
    image = None
    try:
        ec2 = context.aws().ec2()
        if record.image_id:
            image = describe_images_by_id(ec2, [record.image_id]).get(record.image_id)
        elif record.ami_name:
            result = ec2.describe_images(
                Owners=['self'],
                Filters=[{'Name': 'name', 'Values': [record.ami_name]}],
            )
            image = next(iter(result.get('Images', [])), None)
    except Exception as e:
        logger.warning(
            f'{record.base_os}/{record.architecture}: image lookup on resume failed: {e}'
        )
    state = (image or {}).get('State')
    if image is not None and state in ('available', 'pending'):
        record.image_id = image['ImageId']
        record.status = ImageRowStatus.TEST_LAUNCHING.value
        record.error = None
    else:
        if record.instance_id:
            stop_builder(context, record.instance_id, logger)
        record.attempts = (record.attempts or 0) + 1
        if record.attempts < MAX_RESUME_ATTEMPTS:
            record.status = ImageRowStatus.QUEUED.value
            record.error = (
                'the controller restarted during the bake; it is being retried'
            )
        else:
            record.status = ImageRowStatus.FAILED.value
            record.error = 'the controller restarted during the bake twice; refresh the row to try again'
            record.finished_on = datetime.now(tz=timezone.utc)
        record.instance_id = None
    record.host = socket.gethostname()
    records.put(record)
    return record


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
    """
    one row per (base_os, architecture[#variant]): the last build and how it ended.
    kind set means the module runs the 26.10.1 pipeline: rows load migrated() onto the
    row model and a restart resumes in-flight rows instead of failing them.
    """

    def __init__(self, context, table_name: str, kind: Optional[ImageKind] = None):
        self.context = context
        self.table_name = table_name
        self.kind = kind
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
        a process restart kills the daemon build threads. legacy tables fail every
        'building' record this host owns and stop its builder; a pipeline table (kind
        set) keeps its in-flight rows for the leader loop to resume. other hosts' records
        are left alone. builders stopped for over a day are terminated.
        """
        host = socket.gethostname()
        orphaned: List[str] = []
        for record in self.list_all():
            if record.host != host:
                continue
            if self.kind is not None:
                # the module's leader loop resumes these (resume_record); a restart
                # alone never fails a pipeline row
                if record.status in IMAGE_ROW_IN_FLIGHT:
                    orphaned.append(f'{record.base_os}/{record.architecture}')
                continue
            if record.status != BUILD_STATUS_BUILDING:
                continue
            record.status = BUILD_STATUS_FAILED
            record.error = 'the module restarted during the build'
            record.finished_on = datetime.now(tz=timezone.utc)
            self.put(record)
            orphaned.append(f'{record.base_os}/{record.architecture}')
            if record.instance_id:
                stop_builder(self.context, record.instance_id, logger)
        if orphaned:
            logger.warning(
                f'builds {"left for the leader to resume" if self.kind else "orphaned"} '
                f'by a restart on {host}: {orphaned}'
            )
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
        # the table range key carries the variant for GPU rows (ImageRowKey.range_key)
        item['architecture'] = record.row_key().range_key()
        return item

    @staticmethod
    def from_item(
        item: Optional[Dict[str, Any]], kind: Optional[ImageKind] = None
    ) -> Optional[ImageBuildRecord]:
        if not item:
            return None
        data = dict(item)
        for key in _RECORD_TIMESTAMPS:
            value = data.get(key)
            if isinstance(value, (int, float, Decimal)):
                data[key] = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        for check in data.get('checks') or []:
            if isinstance(check.get('seconds'), Decimal):
                check['seconds'] = int(check['seconds'])
        for key in ('attempts',):
            if isinstance(data.get(key), Decimal):
                data[key] = int(data[key])
        if data.get('architecture') and not data['architecture'].endswith(
            CUSTOM_BUILD_SUFFIX
        ):
            architecture, variant = ImageRowKey.split_range_key(data['architecture'])
            data['architecture'] = architecture
            if variant != ImageVariant.CPU.value:
                data['variant'] = variant
        record = ImageBuildRecord(**data)
        return record.migrated(kind) if kind is not None else record

    def _load(self, item) -> Optional[ImageBuildRecord]:
        return self.from_item(item, self.kind)

    @staticmethod
    def _key(base_os: str, architecture: str, variant=None) -> Dict[str, str]:
        return {
            'base_os': base_os,
            'architecture': ImageRowKey(
                base_os=base_os, architecture=architecture, variant=variant
            ).range_key(),
        }

    def put(self, record: ImageBuildRecord) -> ImageBuildRecord:
        self._table.put_item(Item=self.to_item(record))
        return record

    def put_if(self, record: ImageBuildRecord, expected: Dict[str, Any]) -> bool:
        """
        write the row only while the stored row still has every expected value (None:
        attribute absent). the pipeline's guarded writes: queue claims, the promote write
        and the host fence that stops a previous leader's thread. False when refused.
        """
        conditions = []
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        for index, (name, value) in enumerate(expected.items()):
            names[f'#e{index}'] = name
            if value is None:
                conditions.append(f'attribute_not_exists(#e{index})')
            elif isinstance(value, (list, tuple, set, frozenset)):
                placeholders = []
                for position, option in enumerate(sorted(value)):
                    values[f':e{index}_{position}'] = option
                    placeholders.append(f':e{index}_{position}')
                conditions.append(
                    f'(attribute_not_exists(#e{index}) OR NOT #e{index} IN ({", ".join(placeholders)}))'
                )
            else:
                values[f':e{index}'] = value
                conditions.append(f'#e{index} = :e{index}')
        kwargs: Dict[str, Any] = {'Item': self.to_item(record)}
        if conditions:
            kwargs['ConditionExpression'] = ' AND '.join(conditions)
            kwargs['ExpressionAttributeNames'] = names
            if values:
                kwargs['ExpressionAttributeValues'] = values
        try:
            self._table.put_item(**kwargs)
            return True
        except ClientError as e:
            if (
                e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'
            ):
                return False
            raise

    def claim(self, record: ImageBuildRecord) -> bool:
        """
        write the record (its status as given) only when no job holds the key: the
        stored row is absent, or neither in flight (any IMAGE_ROW_IN_FLIGHT status) nor
        pinned. False means another request holds it or the row is pinned.
        """
        in_flight = sorted(IMAGE_ROW_IN_FLIGHT)
        values = {f':s{i}': status for i, status in enumerate(in_flight)}
        values[':true'] = True
        try:
            self._table.put_item(
                Item=self.to_item(record),
                ConditionExpression=(
                    'attribute_not_exists(base_os) OR ('
                    f'NOT #s IN ({", ".join(values_key for values_key in values if values_key != ":true")}) '
                    'AND (attribute_not_exists(#p) OR #p <> :true))'
                ),
                ExpressionAttributeNames={'#s': 'status', '#p': 'pinned'},
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as e:
            if (
                e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'
            ):
                return False
            raise

    def update_fields(
        self,
        record: ImageBuildRecord,
        fields: Dict[str, Any],
        unless_status=None,
    ) -> bool:
        """
        set only these attributes of the stored row (None removes one), optionally only
        while its status is not in unless_status. admin edits (pin) go through here so
        they never clobber a running job's fields. False when the condition refused it.
        """
        key = {
            'base_os': record.base_os,
            'architecture': record.row_key().range_key(),
        }
        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        sets, removes = [], []
        for index, (name, value) in enumerate(fields.items()):
            names[f'#f{index}'] = name
            if value is None:
                removes.append(f'#f{index}')
            else:
                values[f':f{index}'] = value
                sets.append(f'#f{index} = :f{index}')
        expression = ''
        if sets:
            expression += 'SET ' + ', '.join(sets)
        if removes:
            expression += ' REMOVE ' + ', '.join(removes)
        kwargs: Dict[str, Any] = {
            'Key': key,
            'UpdateExpression': expression.strip(),
            'ExpressionAttributeNames': names,
        }
        if unless_status:
            names['#s'] = 'status'
            placeholders = []
            for index, status in enumerate(sorted(unless_status)):
                values[f':u{index}'] = status
                placeholders.append(f':u{index}')
            kwargs['ConditionExpression'] = (
                f'attribute_not_exists(#s) OR NOT #s IN ({", ".join(placeholders)})'
            )
        if values:
            kwargs['ExpressionAttributeValues'] = values
        try:
            self._table.update_item(**kwargs)
            return True
        except ClientError as e:
            if (
                e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'
            ):
                return False
            raise

    def get(
        self, base_os: str, architecture: str, variant=None
    ) -> Optional[ImageBuildRecord]:
        result = self._table.get_item(Key=self._key(base_os, architecture, variant))
        return self._load(result.get('Item'))

    def delete(self, base_os: str, architecture: str, variant=None):
        self._table.delete_item(Key=self._key(base_os, architecture, variant))

    def list_all(self) -> List[ImageBuildRecord]:
        records: List[ImageBuildRecord] = []
        kwargs: Dict[str, Any] = {}
        while True:
            result = self._table.scan(**kwargs)
            records.extend(self._load(item) for item in result.get('Items', []))
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
