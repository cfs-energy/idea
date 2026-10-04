"""
Compute image inventory and builds behind SchedulerAdmin.ListComputeImages and
SchedulerAdmin.BuildComputeImage, shared with ideactl ami-builder. One row per base OS
and architecture: the image the cluster launches for it today (scheduler default first,
then queue profiles), whether that is a build or a stock image, and how the last build
ended. A combination with neither an image nor a build record has no row.
"""

import threading
import time
import socket
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from typing import Dict, List, Optional, Tuple
from ideadatamodel import (
    ImageKind,
    ImageVariant,
    ImageCheck,
    ImageRefreshResult,
    ImageRefreshSchedule,
    ImagePipelineSettings,
    RefreshImagesRequest,
    IMAGE_ROW_IN_FLIGHT,
)
from ideascheduler_meta import __version__
from ideascheduler.app.images.compute_image_canary import (
    ComputeImageCanary,
    VALIDATION_QUEUE_PREFIX,
)


from ideadatamodel import (
    BuildComputeImageRequest,
    ImageBuildRecord,
    ImageInventoryRow,
    exceptions,
)
from ideasdk.aws.image_builds import (
    BUILD_STATUS_BUILDING,
    COMPUTE_IMAGE_PREFIX,
    builder_log_link,
    check_builder_instance_type,
    custom_build_architecture,
    is_custom_record,
    IMAGE_BUILD_TAG,
    STALE_AFTER,
    ImageBuildRecordsDB,
    ImageBuildRunner,
    stop_builder,
    terminate_old_stopped_builders,
    promote_gate,
    resume_record,
    terminate_builder,
    build_stamp,
    describe_images_by_id,
    image_state,
    new_record,
    newest_owned_image,
    PIPELINE_IMAGE_TAG,
    deregister_unreferenced_images,
)
from ideasdk.aws.stock_amis import (
    find_latest_stock_ami,
    stock_unsupported_reason,
    trusted_owners,
)
from ideascheduler.app.images.compute_node_ami_builder import ComputeNodeAmiBuilder
from ideasdk.utils import Utils

# the base OS set ideactl ami-builder accepts
COMPUTE_BASE_OS = (
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rhel10',
    'rocky8',
    'rocky9',
    'rocky10',
    'ubuntu2204',
    'ubuntu2404',
)


def image_builds_table_name(context) -> str:
    return f'{context.cluster_name()}.{context.module_id()}.image-builds'


SCHEDULER_DEFAULT_REFERENCE = 'scheduler default'
# promotion tags the images it validated; only these and vendor stock images are ever moved off
VALIDATED_IMAGE_TAG = 'idea:ComputeImageValidated'


class ComputeImageService:
    def __init__(self, context):
        self.context = context
        self._logger = context.logger('compute-images')
        self.records = ImageBuildRecordsDB(
            context, image_builds_table_name(context), kind=ImageKind.COMPUTE
        ).initialize()
        self.runner = ImageBuildRunner(context, self.records, self._logger)
        self._live = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._last_sweep = 0.0

    # listing

    def list_images(self) -> List[ImageInventoryRow]:
        config = self.context.config()
        default_os = config.get_string('scheduler.compute_node_os', default=None)
        default_ami = config.get_string('scheduler.compute_node_ami', default=None)

        references: Dict[str, List[str]] = {}
        images_by_os: Dict[str, List[str]] = {}

        def add(base_os: Optional[str], image_id: Optional[str], reference: str):
            if Utils.is_empty(base_os) or Utils.is_empty(image_id):
                return
            references.setdefault(image_id, []).append(reference)
            ordered = images_by_os.setdefault(base_os, [])
            if image_id not in ordered:
                ordered.append(image_id)

        add(default_os, default_ami, SCHEDULER_DEFAULT_REFERENCE)
        for queue_profile in self._queue_profiles():
            params = queue_profile.default_job_params
            if params is None or Utils.is_empty(params.instance_ami):
                continue
            add(
                params.base_os or default_os,
                params.instance_ami,
                f'queue profile: {queue_profile.name}',
            )

        ec2_client = self.context.aws().ec2()
        described = describe_images_by_id(ec2_client, list(references.keys()))

        combinations = self._combinations(ec2_client, images_by_os, described)
        # the Custom images tab: its rows show custom builds, keyed by their real architecture
        records = {
            (
                record.base_os,
                (record.architecture or 'x86_64').split('#', 1)[0],
            ): record
            for record in self.records.list_all()
            if record.base_os in COMPUTE_BASE_OS and is_custom_record(record)
        }
        # a build record earns a row of its own, so a build in flight is visible before it
        # has produced an image
        for key in records:
            combinations.setdefault(key, [])

        rows: List[ImageInventoryRow] = []
        for base_os, architecture in sorted(
            combinations, key=lambda key: (COMPUTE_BASE_OS.index(key[0]), key[1])
        ):
            candidates = combinations[(base_os, architecture)]
            row = ImageInventoryRow(
                base_os=base_os,
                architecture=architecture,
                state='none',
                referenced_by=[],
            )
            image_id = candidates[0] if candidates else None
            if image_id is not None:
                image = described.get(image_id)
                row.image_id = image_id
                row.referenced_by = list(references.get(image_id, []))
                if image is None:
                    row.state = 'missing'
                    row.notes = 'the referenced image no longer exists in this account'
                else:
                    row.image_name = image.get('Name')
                    row.state = image_state(row.image_name, COMPUTE_IMAGE_PREFIX)
                    if row.state == 'built':
                        row.build_date = build_stamp(row.image_name)
                others = [
                    f'{other} ({", ".join(references.get(other, []))})'
                    for other in candidates[1:]
                ]
                if others:
                    row.notes = 'also in use for this OS: ' + '; '.join(others)
            record = records.get((base_os, architecture))
            if record is not None:
                row.last_build = record
                if record.status == BUILD_STATUS_BUILDING:
                    row.state = 'building'
            rows.append(row)
        return rows

    def _combinations(
        self, ec2_client, images_by_os: Dict[str, List[str]], described: Dict[str, Dict]
    ) -> Dict[Tuple[str, str], List[str]]:
        """
        the (base OS, architecture) pairs an image is referenced for, each with its images
        most authoritative first. an OS nothing points at falls back to the newest build
        left in the account, which is added to `described`. a referenced image that no
        longer exists keeps the default architecture, the only one that can still be
        assumed for it.
        """
        combinations: Dict[Tuple[str, str], List[str]] = {}
        for base_os in COMPUTE_BASE_OS:
            candidates = images_by_os.get(base_os, [])
            if not candidates:
                image = newest_owned_image(
                    ec2_client, f'{COMPUTE_IMAGE_PREFIX}{base_os}-v*'
                )
                if image is not None:
                    described[image['ImageId']] = image
                    candidates = [image['ImageId']]
            for image_id in candidates:
                architecture = (described.get(image_id) or {}).get(
                    'Architecture', 'x86_64'
                )
                combinations.setdefault((base_os, architecture), []).append(image_id)
        return combinations

    def _queue_profiles(self):
        service = getattr(self.context, 'queue_profiles', None)
        if service is None:
            return []
        return service.list_queue_profiles() or []

    # building

    def build(
        self, request: BuildComputeImageRequest, requested_by: Optional[str]
    ) -> ImageBuildRecord:
        base_os = request.base_os
        if base_os not in COMPUTE_BASE_OS:
            raise exceptions.invalid_params(
                f'base_os must be one of: {", ".join(COMPUTE_BASE_OS)}'
            )
        unsupported = stock_unsupported_reason(
            base_os, self.context.aws().ec2().meta.region_name
        )
        if unsupported:
            raise exceptions.invalid_params(unsupported)
        architecture = self.resolve_architecture(
            request.instance_type, request.architecture
        )
        base_ami = self.default_base_ami(base_os, architecture)
        if Utils.is_empty(base_ami):
            raise exceptions.invalid_params(
                f'no stock {base_os} {architecture} image could be resolved in this region'
            )
        check_builder_instance_type(request.instance_type, architecture)
        # the builder refuses a base_ami owned by neither this account nor the OS vendor
        builder = ComputeNodeAmiBuilder(
            context=self.context,
            base_ami=base_ami,
            base_os=base_os,
            instance_type=request.instance_type,
            enable_driver=tuple(request.enable_drivers or ()),
            force=True,
        )
        return self.run_build(builder, requested_by=requested_by, blocking=False)

    def run_build(
        self, builder, requested_by: Optional[str], blocking: bool
    ) -> ImageBuildRecord:
        """
        a custom build, shared by the API (threaded) and ideactl ami-builder (blocking):
        recorded under <arch>#custom so it never touches the managed row, and never
        promoted. a validated image comes from Refresh and validate
        """
        record = new_record(
            base_os=builder.base_os,
            architecture=custom_build_architecture(builder.architecture),
            ami_name=builder.get_ami_full_name(),
            base_ami=builder.base_ami,
            requested_by=requested_by,
            update_target=False,
        )
        return self.runner.start(record, build=builder.build, blocking=blocking)

    def architecture_of(self, instance_type: Optional[str]) -> Optional[str]:
        """the architecture an instance type runs, from the sdk's instance type cache"""
        if Utils.is_empty(instance_type):
            return None
        ec2_instance_type = self.context.aws_util().get_ec2_instance_type(instance_type)
        if ec2_instance_type is None:
            return None
        supported = ec2_instance_type.processor_info_supported_architectures
        if not isinstance(supported, (list, tuple)):
            return None
        if 'arm64' in supported:
            return 'arm64'
        if 'x86_64' in supported:
            return 'x86_64'
        return None

    def resolve_architecture(
        self, instance_type: Optional[str], requested: Optional[str]
    ) -> str:
        """derived from the instance type, defaulting to x86_64; an explicit value must agree"""
        derived = self.architecture_of(instance_type)
        if requested and derived and requested != derived:
            raise exceptions.invalid_params(
                f'{instance_type} is {derived}; architecture {requested} does not match'
            )
        return requested or derived or 'x86_64'

    def default_base_ami(
        self, base_os: str, architecture: str = 'x86_64'
    ) -> Optional[str]:
        """Resolve vendor-latest explicitly, never the configured (possibly stale) image."""
        return find_latest_stock_ami(
            self.context.aws().ec2(), base_os, architecture, self._logger
        )

    @property
    def table(self):
        return (
            self.context.aws()
            .dynamodb_table()
            .Table(image_builds_table_name(self.context))
        )

    @staticmethod
    def _validate_kind(value):
        if value is not None and value.kind not in (None, ImageKind.COMPUTE):
            raise exceptions.invalid_params(
                'Scheduler image requests must use kind compute.'
            )

    def list_rows(self, row_filter=None):
        self._validate_kind(row_filter)
        rows = {
            (os, arch): ImageBuildRecord(
                kind=ImageKind.COMPUTE,
                base_os=os,
                architecture=arch,
                variant=ImageVariant.CPU,
                status='failed',
                error='This compute image has not been validated yet.',
            )
            for os in COMPUTE_BASE_OS
            for arch in ('x86_64', 'arm64')
        }
        # Seed from live references, not the newest unused AMI in the account.
        config = self.context.config()
        default_os = config.get_string('scheduler.compute_node_os', default=None)
        references = [
            (default_os, config.get_string('scheduler.compute_node_ami', default=None))
        ]
        references += [
            (
                p.default_job_params.base_os or default_os,
                p.default_job_params.instance_ami,
            )
            for p in self._queue_profiles()
            if p.default_job_params
            and not (p.name or '').startswith(VALIDATION_QUEUE_PREFIX)
        ]
        images = describe_images_by_id(
            self.context.aws().ec2(), [ami for _, ami in references if ami]
        )
        for os, ami in references:
            image = images.get(ami, {})
            row = rows.get((os, image.get('Architecture', 'x86_64')))
            owners = (
                [
                    owner
                    for owner in trusted_owners(
                        os, self.context.aws().ec2().meta.region_name
                    )
                    if owner != 'self'
                ]
                if row
                else []
            )
            if (
                row
                and not row.current_image_id
                and (
                    image.get('Name', '').startswith(COMPUTE_IMAGE_PREFIX)
                    or image.get('OwnerId') in owners
                    or image.get('ImageOwnerAlias') in owners
                )
            ):
                row.current_image_id = ami
        for stored in self.records.list_all():
            if is_custom_record(stored):
                continue
            record = stored.migrated(ImageKind.COMPUTE)
            key = (record.base_os, record.row_key().range_key())
            if not record.current_image_id and key in rows:
                record.current_image_id = rows[key].current_image_id
            rows[key] = record
        region = self.context.aws().ec2().meta.region_name
        result = []
        for record in rows.values():
            reason = stock_unsupported_reason(record.base_os, region)
            if record.variant != ImageVariant.CPU:
                reason = 'Compute validation currently supports CPU images only.'
            if record.pinned:
                record.status = 'pinned'
            elif reason and not record.is_in_flight():
                record.status, record.error = 'unsupported', reason
            if row_filter is None or row_filter.matches(record):
                result.append(record)
        return result

    def _get_row(self, key):
        if key is None or not key.base_os:
            raise exceptions.invalid_params('An image row with base_os is required.')
        self._validate_kind(key)
        return next(
            (
                r
                for r in self.list_rows()
                if r.base_os == key.base_os
                and r.row_key().range_key() == key.range_key()
            ),
            None,
        )

    def refresh_images(self, request: RefreshImagesRequest, requested_by=None):
        if (
            sum(
                (
                    request.all is True,
                    request.rows is not None,
                    request.filter is not None,
                )
            )
            != 1
        ):
            raise exceptions.invalid_params(
                'Select exactly one of all, rows or filter.'
            )
        self._validate_kind(request.filter)
        for key in request.rows or []:
            self._validate_kind(key)
        if request.rows is not None:
            selected = [(key, self._get_row(key)) for key in request.rows]
        else:
            selected = [(r.row_key(), r) for r in self.list_rows(request.filter)]
        results = []
        for key, record in selected:
            if record is None:
                results.append(
                    ImageRefreshResult(
                        row=key,
                        outcome='not_found',
                        message='The compute image row was not found.',
                    )
                )
                continue
            try:
                results.append(
                    self._enqueue(
                        record, requested_by, 'button', force=bool(request.force)
                    )
                )
            except Exception as error:
                results.append(
                    ImageRefreshResult(
                        row=key,
                        outcome='error',
                        message=getattr(error, 'message', None) or str(error),
                    )
                )
        return results

    def _baked_today(self, row, now=None) -> bool:
        return row.baked_today(
            now or datetime.now(timezone.utc),
            ZoneInfo(self.context.cluster_timezone()),
        )

    def _enqueue(self, row, requested_by, trigger, force=False):
        key = row.row_key()
        existing = self.records.get(row.base_os, key.range_key())
        current = row.current_image_id
        row = (
            existing.migrated(ImageKind.COMPUTE)
            if existing
            else row.model_copy(deep=True)
        )
        row.current_image_id = row.current_image_id or current
        outcome = None
        if row.pinned or (row.rollback_hold and trigger != 'button'):
            outcome = 'pinned'
        elif row.is_in_flight():
            outcome = 'in_flight'
        elif not force and self._baked_today(row):
            return ImageRefreshResult(
                row=key,
                outcome='baked_today',
                record=row,
                message='Already baked today; Force rebake bakes it again.',
            )
        elif (
            row.status == 'unsupported'
            or row.base_os not in COMPUTE_BASE_OS
            or row.architecture not in ('x86_64', 'arm64')
            or row.variant not in (None, ImageVariant.CPU)
            or stock_unsupported_reason(
                row.base_os, self.context.aws().ec2().meta.region_name
            )
        ):
            outcome = 'unsupported'
        if outcome:
            return ImageRefreshResult(
                row=key,
                outcome=outcome,
                record=row,
                message=f'The compute row is {outcome}.',
            )
        row.status = 'queued'
        row.kind, row.variant = ImageKind.COMPUTE, ImageVariant.CPU
        row.trigger, row.requested_by = trigger, requested_by
        row.release = __version__
        row.host = socket.gethostname()
        row.started_on, row.finished_on = datetime.now(timezone.utc), None
        row.image_id = row.instance_id = row.validated_on = row.promoted_on = None
        row.error = row.retry_after = None
        row.checks = []
        row.attempts = (
            0  # Shared resume/capacity helpers count retries, not the initial attempt.
        )
        row.rollback_hold = False
        if not self.records.put_if(
            row,
            {
                'status': IMAGE_ROW_IN_FLIGHT,
                'pinned': existing.pinned if existing else None,
                'rollback_hold': existing.rollback_hold if existing else None,
            },
        ):
            return ImageRefreshResult(
                row=key,
                outcome='in_flight',
                record=self.records.get(row.base_os, key.range_key()),
                message='The compute row is already in flight.',
            )
        return ImageRefreshResult(row=key, outcome='queued', record=row)

    def _save(self, record):
        # Partial updates preserve pins written while the builder/canary is running.
        item = self.records.to_item(record)
        protected = {
            'base_os',
            'architecture',
            'pinned',
            'rollback_hold',
            'current_image_id',
            'previous_image_id',
        }
        updates = {key: value for key, value in item.items() if key not in protected}
        names = {f'#k{i}': key for i, key in enumerate(updates)}
        values = {f':v{i}': value for i, value in enumerate(updates.values())}
        values[':started'] = item['started_on']
        self.table.update_item(
            Key={
                'base_os': record.base_os,
                'architecture': record.row_key().range_key(),
            },
            UpdateExpression='SET '
            + ', '.join(f'#k{i} = :v{i}' for i in range(len(updates))),
            ConditionExpression='started_on = :started',
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def _start_row(self, record, builder=None):
        key = (record.base_os, record.row_key().range_key())
        with self._lock:
            if key in self._live and self._live[key].is_alive():
                return
            thread = threading.Thread(
                target=self._run_pipeline,
                args=(record, builder),
                daemon=True,
                name='compute-image-' + record.base_os,
            )
            self._live[key] = thread
            thread.start()

    def _run_pipeline(self, record, builder=None):
        def progress(update):
            if not self.context.is_leader():
                raise exceptions.general_exception(
                    'The scheduler lost leadership during compute image validation.'
                )
            for key, value in update.items():
                setattr(record, key, value)
            if update.get('instance_id'):
                record.log_link = builder_log_link(self.context, record.instance_id)
            self._save(record)

        try:
            if record.status in ('building', 'checking'):
                record = resume_record(self.context, self.records, record, self._logger)
                if record.status == 'failed':
                    return
            if not record.image_id:
                progress({'status': 'resolving'})
                source = self.default_base_ami(record.base_os, record.architecture)
                if not source:
                    raise exceptions.general_exception(
                        'No vendor image is available for this compute row.'
                    )
                record.release = __version__
                record.source_ami = record.base_ami = source
                if builder is None:
                    storage = (
                        self.context.config().get_config('shared-storage', default={})
                        or {}
                    )
                    drivers = (
                        ('fsx_lustre',)
                        if any(
                            fs.get('provider') == 'fsx_lustre'
                            for fs in storage.values()
                            if isinstance(fs, dict)
                        )
                        else ()
                    )
                    builder = ComputeNodeAmiBuilder(
                        enable_driver=drivers,
                        context=self.context,
                        base_os=record.base_os,
                        base_ami=source,
                        force=True,
                        image_tags={PIPELINE_IMAGE_TAG: 'compute'},
                    )
                else:
                    builder.base_ami = source
                record.ami_name = builder.get_ami_full_name()
                region = self.context.aws().ec2().meta.region_name
                group = quote(
                    f'/{self.context.cluster_name()}/{self.context.module_id()}/ami-builder',
                    safe='',
                )
                record.log_link = f'https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{group}'
                progress({'status': 'building'})
                image_id = builder.build(progress)
                progress({'image_id': image_id})
            # A restarted worker resumes its candidate without baking another image.
            if builder is None:
                builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
                builder.context = self.context
            builder.wait_for_image(record.image_id)
            terminate_builder(self.context, record.instance_id, self._logger)
            record.checks = [
                c
                for c in record.checks or []
                if not (c.name or '').startswith(('compute_', 'filesystem:', 'pbs_'))
            ]
            if not record.checks or any(c.ok is not True for c in record.checks):
                raise exceptions.general_exception(
                    'The compute candidate has no successful in-bake checks.'
                )
            progress({'status': 'test_launching'})
            ComputeImageCanary(self.context).validate(record, progress)
            progress(
                {'validated_on': datetime.now(timezone.utc), 'status': 'promoting'}
            )
            self.promote(record)
        except Exception as error:
            if not self.context.is_leader():
                return  # The new leader resumes the persisted row.
            self._logger.exception('Compute image validation failed')
            record.status = 'failed'
            record.error = (
                getattr(error, 'message', None)
                or str(error)
                or 'Compute image validation failed.'
            )
            record.finished_on = datetime.now(timezone.utc)
            if not any(c.ok is False for c in record.checks or []):
                record.checks = list(record.checks or []) + [
                    ImageCheck(
                        name='pipeline', ok=False, detail=record.error, seconds=0
                    )
                ]
            self._save(record)

    def promote(self, record):
        promote_gate(record, record.image_id)
        if not self.context.is_leader():
            raise exceptions.invalid_params(
                'Only the scheduler leader can promote a compute image.'
            )
        if (
            not record.image_id
            or not record.validated_on
            or not record.checks
            or any(c.ok is not True for c in record.checks)
        ):
            raise exceptions.invalid_params(
                'Only a validated compute image can be promoted.'
            )
        if record.pinned or record.rollback_hold:
            raise exceptions.invalid_params(
                'The compute image is pinned or held after rollback.'
            )
        # This also proves a previous generation is eligible for rollback; legacy builds have no tag.
        self.context.aws().ec2().create_tags(
            Resources=[record.image_id],
            Tags=[{'Key': VALIDATED_IMAGE_TAG, 'Value': record.release}],
        )
        self._move_targets(record, record.image_id, rollback=False)

    def _managed_image(self, base_os, image_id) -> bool:
        """
        an image the pipeline may move a target off: one it validated, or the vendor's
        stock image. anything else (a custom build an admin set as default, their own
        AMI, an image that no longer exists) is treated as pinned
        """
        if not image_id:
            return False
        image = describe_images_by_id(self.context.aws().ec2(), [image_id]).get(
            image_id
        )
        if image is None:
            return False
        if any(t.get('Key') == VALIDATED_IMAGE_TAG for t in image.get('Tags', [])):
            return True
        vendors = [
            owner
            for owner in trusted_owners(
                base_os, self.context.aws().ec2().meta.region_name
            )
            if owner != 'self'
        ]
        return (
            image.get('OwnerId') in vendors or image.get('ImageOwnerAlias') in vendors
        )

    def _move_targets(self, record, image_id, rollback):
        old = record.current_image_id
        # the row's current image moves on; its targets follow only off a managed image
        movable = self._managed_image(record.base_os, old)
        replacement = record.model_copy(deep=True)
        replacement.previous_image_id, replacement.current_image_id = old, image_id
        replacement.status = 'current'
        replacement.rollback_hold = rollback
        replacement.promoted_on = replacement.finished_on = datetime.now(timezone.utc)
        item = self.records.to_item(replacement)
        serialize = TypeSerializer().serialize

        def encode(values):
            return {k: serialize(v) for k, v in values.items()}

        condition = '(attribute_not_exists(pinned) OR pinned = :false) AND current_image_id = :old'
        values = {':false': False, ':old': old}
        if old is None:
            condition = condition.replace(
                'current_image_id = :old', 'attribute_not_exists(current_image_id)'
            )
            del values[':old']
        if rollback:
            condition += ' AND previous_image_id = :image AND #s = :current'
            values.update({':image': image_id, ':current': 'current'})
        else:
            condition += ' AND (attribute_not_exists(rollback_hold) OR rollback_hold = :false) AND image_id = :image AND validated_on = :validated AND #s = :promoting'
            values.update(
                {
                    ':image': image_id,
                    ':validated': item['validated_on'],
                    ':promoting': 'promoting',
                }
            )
        writes = [
            {
                'Put': {
                    'TableName': image_builds_table_name(self.context),
                    'Item': encode(item),
                    'ConditionExpression': condition,
                    'ExpressionAttributeNames': {'#s': 'status'},
                    'ExpressionAttributeValues': encode(values),
                }
            }
        ]
        profiles = []
        for profile in self._queue_profiles():
            params = profile.default_job_params
            if (
                not movable
                or profile.image_pinned
                or not params
                or params.instance_ami != old
                or (profile.name or '').startswith(VALIDATION_QUEUE_PREFIX)
            ):
                continue
            writes.append(
                {
                    'Update': {
                        'TableName': f'{self.context.cluster_name()}.{self.context.module_id()}.queue-profiles',
                        'Key': encode({'queue_profile_id': profile.queue_profile_id}),
                        'UpdateExpression': 'SET param_instance_ami = :new',
                        'ConditionExpression': 'param_instance_ami = :old AND (attribute_not_exists(image_pinned) OR image_pinned = :false)',
                        'ExpressionAttributeValues': encode(
                            {':new': image_id, ':old': old, ':false': False}
                        ),
                    }
                }
            )
            profiles.append(profile)
        config = self.context.config()
        default_key = config.get_real_key('scheduler.compute_node_ami')
        settings_table = f'{self.context.cluster_name()}.cluster-settings'
        if (
            movable
            and config.get_string('scheduler.compute_node_ami', default=None) == old
            and not config.get_bool(
                'scheduler.images.default_image_pinned', default=False
            )
        ):
            writes.append(
                {
                    'ConditionCheck': {
                        'TableName': settings_table,
                        'Key': encode(
                            {
                                'key': config.get_real_key(
                                    'scheduler.images.default_image_pinned'
                                )
                            }
                        ),
                        'ConditionExpression': 'attribute_not_exists(#v) OR #v = :false',
                        'ExpressionAttributeNames': {'#v': 'value'},
                        'ExpressionAttributeValues': encode({':false': False}),
                    }
                }
            )
            writes.append(
                {
                    'Update': {
                        'TableName': settings_table,
                        'Key': encode({'key': default_key}),
                        'UpdateExpression': 'SET #v = :new, #src = :src ADD #ver :one',
                        'ConditionExpression': '#v = :old',
                        'ExpressionAttributeNames': {
                            '#v': 'value',
                            '#src': 'source',
                            '#ver': 'version',
                        },
                        'ExpressionAttributeValues': encode(
                            {':new': image_id, ':old': old, ':src': 'sdk', ':one': 1}
                        ),
                    }
                }
            )
        # ponytail: DynamoDB's 100-item transaction ceiling; reject rather than split an atomic promotion.
        if len(writes) > 100:
            raise exceptions.invalid_params(
                'Too many compute targets for one atomic image promotion.'
            )
        self.context.aws().dynamodb().transact_write_items(TransactItems=writes)
        for profile in profiles:
            try:
                self.context.queue_profiles.cache_clear(profile)
                updated = self.context.queue_profiles.get_queue_profile(
                    queue_profile_id=profile.queue_profile_id
                )
                self.context.queue_profiles.initialize_job_provisioner(updated)
            except Exception:
                self._logger.exception(
                    'Compute image promoted; queue cache refresh failed'
                )
        record.current_image_id = replacement.current_image_id
        record.previous_image_id = replacement.previous_image_id
        record.status, record.rollback_hold = (
            replacement.status,
            replacement.rollback_hold,
        )
        record.promoted_on, record.finished_on = (
            replacement.promoted_on,
            replacement.finished_on,
        )
        return record

    def rollback_image(self, key):
        record = self._get_row(key)
        if (
            not record
            or not record.previous_image_id
            or record.is_in_flight()
            or record.pinned
        ):
            raise exceptions.invalid_params(
                'Rollback requires an idle, unpinned row with a previous validated image.'
            )
        image = describe_images_by_id(
            self.context.aws().ec2(), [record.previous_image_id]
        ).get(record.previous_image_id, {})
        if image.get('State') != 'available' or not any(
            t.get('Key') == VALIDATED_IMAGE_TAG for t in image.get('Tags', [])
        ):
            raise exceptions.invalid_params(
                'The previous compute image is unavailable or was never validated.'
            )
        return self._move_targets(record, record.previous_image_id, rollback=True)

    def set_image_pinned(self, key, pinned):
        record = self._get_row(key)
        if record is None or pinned is None:
            raise exceptions.invalid_params(
                'An existing compute row and pinned boolean are required.'
            )
        if self.records.get(record.base_os, record.row_key().range_key()) is None:
            # Seed an unbuilt row without overwriting a concurrent claim.
            try:
                self.table.put_item(
                    Item=self.records.to_item(record),
                    ConditionExpression='attribute_not_exists(base_os)',
                )
            except ClientError as error:
                if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
        self.table.update_item(
            Key={
                'base_os': record.base_os,
                'architecture': record.row_key().range_key(),
            },
            UpdateExpression='SET pinned = :pin',
            ExpressionAttributeValues={':pin': pinned},
        )
        record.pinned = pinned
        return record

    def _setting(self, key, value):
        config = self.context.config()
        config.db.set_config_entry(config.get_real_key(key), value)

    def sweep_builders(self, now):
        """
        a builder no live record owns and older than any real bake (its process died
        before it recorded the instance) is stopped; one stopped for a day is terminated
        """
        records = [r for r in self.records.list_all() if r.is_in_flight()]
        busy = {r.instance_id for r in records}
        # a hidden validation queue whose candidate no row is validating was left by a
        # failed cleanup or a restart; reap it so the queues do not accumulate
        candidates = {r.image_id for r in records if r.image_id}
        canary = ComputeImageCanary(self.context)
        for profile in self.context.queue_profiles.list_queue_profiles():
            params = profile.default_job_params
            if (profile.name or '').startswith(VALIDATION_QUEUE_PREFIX) and (
                params is None or params.instance_ami not in candidates
            ):
                errors = canary.reap(profile)
                if errors:
                    self._logger.warning(
                        f'validation queue {profile.name} not reaped: {"; ".join(errors)}'
                    )
        result = (
            self.context.aws()
            .ec2()
            .describe_instances(
                Filters=[
                    {'Name': 'tag-key', 'Values': [IMAGE_BUILD_TAG]},
                    {'Name': 'tag:idea:ModuleId', 'Values': [self.context.module_id()]},
                    {'Name': 'instance-state-name', 'Values': ['pending', 'running']},
                ]
            )
        )
        for reservation in result.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                launched = instance.get('LaunchTime')
                if (
                    instance['InstanceId'] not in busy
                    and launched is not None
                    and launched < now - STALE_AFTER
                ):
                    stop_builder(self.context, instance['InstanceId'], self._logger)
        terminate_old_stopped_builders(self.context, self._logger)
        try:
            self.deregister_unreferenced_images()
        except Exception:
            self._logger.exception('Compute image cleanup failed')

    def protected_images(self) -> set:
        """every image a row, a queue or the scheduler default still names"""
        protected = set()
        for record in self.records.list_all():
            protected.update((record.current_image_id, record.previous_image_id))
            # a custom build's image is the admin's; an in-flight candidate is the job's
            if is_custom_record(record) or record.is_in_flight():
                protected.add(record.image_id)
        for profile in self.context.queue_profiles.list_queue_profiles():
            if profile.default_job_params is not None:
                protected.add(profile.default_job_params.instance_ami)
        protected.add(
            self.context.config().get_string('scheduler.compute_node_ami', default=None)
        )
        protected.discard(None)
        return protected

    def deregister_unreferenced_images(self) -> List[str]:
        """failed candidates and generations older than previous: image and snapshots"""
        # a bake between CreateImage and recording the id: its image carries the row's name
        baking = {r.ami_name for r in self.records.list_all() if r.is_in_flight()}
        return deregister_unreferenced_images(
            self.context,
            'compute',
            COMPUTE_IMAGE_PREFIX,
            self.protected_images(),
            baking,
            self._logger,
        )

    def tick(self, now=None):
        if not self.context.is_leader():
            return
        now = now or datetime.now(timezone.utc)
        if time.monotonic() - self._last_sweep > 1800:
            self._last_sweep = time.monotonic()
            try:
                self.sweep_builders(now)
            except Exception:
                self._logger.exception('Compute builder sweep failed')
        config = self.context.config()
        schedule = ImageRefreshSchedule(
            **dict(
                config.get_config(
                    'virtual-desktop-controller.software_stacks.image_refresh_schedule',
                    default={},
                )
                or {}
            )
        )
        local = now.astimezone(ZoneInfo(self.context.cluster_timezone()))
        last = config.get_int(
            'scheduler.images.image_refresh_last_run_on', default=None
        )
        baseline = (
            datetime.fromtimestamp(last / 1000, tz=local.tzinfo)
            if last
            else local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            - timedelta(seconds=1)
        )
        due = schedule.next_run_after(baseline)
        monthly = due is not None and due <= local
        requested_release = config.get_string(
            'scheduler.images.refresh_requested_release', default=__version__
        )
        # A stale intent from an earlier upgrade must not suppress this binary's release.
        requested_release = (
            __version__ if requested_release != __version__ else requested_release
        )
        release = (
            config.get_string('scheduler.images.refreshed_release', default=None)
            != requested_release
        )
        if monthly or release:
            pending_release = False
            for row in self.list_rows():
                if row.is_in_flight() and row.release != __version__:
                    pending_release = True
                if (
                    row.is_in_flight()
                    or row.pinned
                    or row.rollback_hold
                    or row.status == 'unsupported'
                ):
                    continue
                if self._baked_today(row, now):
                    # tomorrow's tick bakes it; the release stays unsettled until then
                    pending_release = pending_release or release
                    continue
                try:
                    source = self.default_base_ami(row.base_os, row.architecture)
                    if source and (
                        source != row.source_ami or row.release != __version__
                    ):
                        self._enqueue(row, None, 'release' if release else 'monthly')
                except Exception:
                    # one row's lookup failing must not hold back the others; the
                    # release is retried next tick instead of being marked done
                    pending_release = True
                    self._logger.exception(f'{row.base_os}: image refresh check failed')
            if monthly:
                self._setting(
                    'scheduler.images.image_refresh_last_run_on',
                    int(now.timestamp() * 1000),
                )
            if release and not pending_release:
                self._setting('scheduler.images.refreshed_release', requested_release)
        settings = ImagePipelineSettings(
            **dict(config.get_config('scheduler.images', default={}) or {})
        )
        with self._lock:
            self._live = {
                key: thread for key, thread in self._live.items() if thread.is_alive()
            }
            for row in self.records.list_all():
                if is_custom_record(row):
                    continue  # the runner owns custom builds
                if len(self._live) >= max(1, settings.max_concurrent_bakes):
                    break
                if (
                    row.is_in_flight()
                    and not row.pinned
                    and (row.retry_after is None or row.retry_after <= now)
                    and (row.base_os, row.row_key().range_key()) not in self._live
                ):
                    self._start_row(row)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    self._logger.exception('Compute image leader loop failed')
                self._stop.wait(15)

        self._thread = threading.Thread(
            target=loop, daemon=True, name='compute-image-refresh'
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
