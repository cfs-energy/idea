"""
Desktop image pipeline: one row per base OS + architecture + variant (CPU, or GPU where
a GPU software stack exists), each running

    queued -> resolving -> building -> checking -> test_launching -> promoting -> current

with failed, waiting_capacity, pinned and unsupported as the other outcomes. The
controller leader loop calls tick(): it resumes orphaned rows, fires the release and
monthly triggers, starts queued rows up to max_concurrent_bakes and cleans up. The
admin API queues rows (RefreshImages) and never runs a bake itself.

Every status change is persisted before the next step, and every write is fenced on the
row's host, so a previous leader's thread stops at its next write. Promotion repoints
only ss-base-* stacks that are not image_pinned, and only for a validated candidate
(promote_gate); custom stacks are never touched.
"""

import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ideadatamodel import (
    IMAGE_ROW_IN_FLIGHT,
    ImageBuildRecord,
    ImageBuildTrigger,
    ImageKind,
    ImagePipelineSettings,
    ImageRefreshResult,
    ImageRefreshSchedule,
    ImageRowFilter,
    ImageRowKey,
    ImageRowStatus,
    ImageVariant,
    ListSoftwareStackRequest,
    RefreshImagesRequest,
    VirtualDesktopGPU,
    VirtualDesktopSoftwareStack,
    constants,
    exceptions,
)
from ideasdk.aws.image_builds import (
    IMAGE_BUILD_TAG,
    LEGACY_MIN_AGE_DAYS,
    PIPELINE_IMAGE_TAG,
    deregister_legacy_images,
    deregister_unreferenced_images,
    describe_image_or_none,
    builder_log_link,
    is_custom_record,
    ImageBuildRecordsDB,
    ImageBuildRunner,
    ImageNotValidated,
    describe_images_by_id,
    promote_gate,
    resume_record,
    terminate_builder,
)
from ideasdk.aws.stock_amis import (
    resolve_stock_image,
    stock_unsupported_reason,
    trusted_owners,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.sessions.image_validation import (
    CapacityWait,
    ImageTestLauncher,
    is_capacity_problem,
    read_in_bake_checks,
)
from ideavirtualdesktopcontroller.app.software_stacks.dcv_host_image_builder import (
    AMI_BUILDER_STATUS_COMPLETE,
    DcvHostImageBuilder,
    BUILD_SUPPORTED_BASE_OS,
    is_windows,
)

PIPELINE_SETTINGS_KEY = 'virtual-desktop-controller.software_stacks.image_pipeline'
SCHEDULE_KEY = 'virtual-desktop-controller.software_stacks.image_refresh_schedule'
LAST_RUN_KEY = 'virtual-desktop-controller.software_stacks.image_refresh_last_run_on'
# the release every desktop row was last baked for; advanced once all rows are terminal
BAKED_RELEASE_KEY = 'virtual-desktop-controller.software_stacks.images_baked_release'

DESKTOP_IMAGE_PREFIX = 'idea-dcv-host-'
STACK_ARCH = {'x86_64': 'x86-64', 'arm64': 'arm64'}

S = ImageRowStatus
ACTIVE = (
    S.RESOLVING.value,
    S.BUILDING.value,
    S.CHECKING.value,
    S.TEST_LAUNCHING.value,
    S.PROMOTING.value,
)
TERMINAL = (
    S.CURRENT.value,
    S.FAILED.value,
    S.PINNED.value,
    S.UNSUPPORTED.value,
    None,
)

# GPU variants bake on the vendor's smallest GPU so the driver installs; CPU rows use
# the builder default for the architecture
BUILDER_INSTANCE_TYPES = {
    ('x86_64', ImageVariant.NVIDIA.value): 'g4dn.xlarge',
    ('arm64', ImageVariant.NVIDIA.value): 'g5g.xlarge',
    ('x86_64', ImageVariant.AMD.value): 'g4ad.xlarge',
}
GPU_VARIANT = {
    VirtualDesktopGPU.NVIDIA: ImageVariant.NVIDIA.value,
    VirtualDesktopGPU.AMD: ImageVariant.AMD.value,
}

# capacity: 15, 30, 60, 120 minutes, then the row fails with the reason
CAPACITY_BACKOFF = timedelta(minutes=15)
CAPACITY_MAX_ATTEMPTS = 5
# a builder or validation desktop older than this is left over from a dead run
REAP_AFTER = timedelta(hours=2)
CLEANUP_EVERY_SECONDS = 30 * 60

# threads running a row in this process, by row key; shared by every pipeline instance
_LIVE: Dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()


class RowFailed(Exception):
    pass


class LostOwnership(Exception):
    """another controller took the row over; this thread stops without writing"""


def row_id(record) -> str:
    key = record.row_key() if isinstance(record, ImageBuildRecord) else record
    return f'{key.base_os}/{key.range_key()}'


def variant_of(record) -> str:
    return getattr(record.variant, 'value', record.variant) or ImageVariant.CPU.value


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


class DesktopImagePipeline:
    def __init__(
        self,
        context,
        software_stack_db,
        software_stack_utils,
        tester: Optional[ImageTestLauncher] = None,
        records: Optional[ImageBuildRecordsDB] = None,
        version: Optional[str] = None,
    ):
        import ideavirtualdesktopcontroller
        from ideavirtualdesktopcontroller.app.software_stacks.desktop_images import (
            image_builds_table_name,
        )

        self.context = context
        self._stack_db = software_stack_db
        self._stack_utils = software_stack_utils
        self.tester = tester
        self.records = records or ImageBuildRecordsDB(
            context, image_builds_table_name(context), kind=ImageKind.DESKTOP
        )
        self.version = version or ideavirtualdesktopcontroller.__version__
        self.host = socket.gethostname()
        self._logger = context.logger('desktop-image-pipeline')
        self._last_cleanup = 0.0

    def managed(self) -> List[ImageBuildRecord]:
        """the pipeline's rows; custom (ad hoc) build records are never listed, run or promoted"""
        return [r for r in self.records.list_all() if not is_custom_record(r)]

    # settings

    def settings(self) -> ImagePipelineSettings:
        return ImagePipelineSettings(
            **dict(
                self.context.config().get_config(PIPELINE_SETTINGS_KEY, default={})
                or {}
            )
        )

    def _set_config(self, key: str, value):
        config = self.context.config()
        real_key = config.get_real_key(key)
        config.db.set_config_entry(real_key, value)
        config.put(real_key, value)

    def _claim_config(self, key: str, expected, value) -> bool:
        """write only if the stored value is still `expected`; the settings copy can lag"""
        config = self.context.config()
        real_key = config.get_real_key(key)
        if not config.db.set_config_entry_if(real_key, value, expected):
            return False
        config.put(real_key, value)
        return True

    # rows

    def _all_stacks(self) -> List[VirtualDesktopSoftwareStack]:
        request = ListSoftwareStackRequest(disabled_also=True)
        stacks: List[VirtualDesktopSoftwareStack] = []
        while True:
            response = self._stack_db.list_all_from_db(request)
            stacks.extend(response.listing or [])
            if Utils.is_empty(response.cursor):
                return stacks
            request.paginator = response.paginator

    @staticmethod
    def _stack_variant(stack: VirtualDesktopSoftwareStack) -> str:
        return GPU_VARIANT.get(stack.gpu, ImageVariant.CPU.value)

    @staticmethod
    def _stack_arch(stack: VirtualDesktopSoftwareStack) -> Optional[str]:
        """from ss-base-<os>-<arch>-<suffix> for base stacks, else the stack's own field"""
        base_os = getattr(stack.base_os, 'value', stack.base_os) or ''
        prefix = f'ss-base-{base_os}-'
        if (stack.stack_id or '').startswith(prefix):
            rest = stack.stack_id[len(prefix) :]
            for architecture, token in STACK_ARCH.items():
                if rest.startswith(f'{token}-'):
                    return architecture
        return getattr(stack.architecture, 'value', stack.architecture)

    def seed_rows(
        self, stacks: Optional[List[VirtualDesktopSoftwareStack]] = None
    ) -> List[ImageBuildRecord]:
        """
        every desktop row: a CPU row per configured base OS + architecture, a GPU row
        where any stack of that OS + architecture is a GPU stack. stored rows are
        returned as stored; a never-built row carries what its base stack launches today
        """
        stacks = self._all_stacks() if stacks is None else stacks
        config = self._stack_db.get_base_software_stack_config() or {}
        keys = {}
        for base_os, arches in config.items():
            for arch_key in arches or {}:
                for architecture, token in STACK_ARCH.items():
                    if arch_key == token:
                        keys[(base_os, architecture, ImageVariant.CPU.value)] = None
        for stack in stacks:
            variant = self._stack_variant(stack)
            base_os = getattr(stack.base_os, 'value', stack.base_os)
            architecture = self._stack_arch(stack)
            if variant != ImageVariant.CPU.value and architecture:
                keys[(base_os, architecture, variant)] = None
        stored = {row_id(r): r for r in self.managed()}
        rows = []
        for base_os, architecture, variant in sorted(keys):
            key = ImageRowKey(
                kind=ImageKind.DESKTOP,
                base_os=base_os,
                architecture=architecture,
                variant=variant,
            )
            record = stored.get(row_id(key))
            if record is None:
                targets = self.targets_for(key, stacks)
                record = ImageBuildRecord(
                    kind=ImageKind.DESKTOP,
                    base_os=base_os,
                    architecture=architecture,
                    variant=variant,
                    current_image_id=targets[0].ami_id if targets else None,
                )
            rows.append(record)
        return rows

    def targets_for(
        self, key, stacks: Optional[List[VirtualDesktopSoftwareStack]] = None
    ) -> List[VirtualDesktopSoftwareStack]:
        """the ss-base-* stacks a row's promotion repoints (pinned ones included; promote skips them)"""
        stacks = self._all_stacks() if stacks is None else stacks
        variant = getattr(key.variant, 'value', key.variant) or 'cpu'
        return [
            s
            for s in stacks
            if (s.stack_id or '').startswith('ss-base-')
            and getattr(s.base_os, 'value', s.base_os) == key.base_os
            and self._stack_arch(s) == key.architecture
            and self._stack_variant(s) == variant
        ]

    def unsupported_reason(self, record: ImageBuildRecord) -> Optional[str]:
        region = self.context.aws().ec2().meta.region_name
        reason = stock_unsupported_reason(record.base_os, region)
        if reason:
            return reason
        if record.base_os not in BUILD_SUPPORTED_BASE_OS:
            return f'{record.base_os} images cannot be baked by this release'
        if (record.architecture, variant_of(record)) not in BUILDER_INSTANCE_TYPES and (
            variant_of(record) != ImageVariant.CPU.value
        ):
            return (
                f'no {variant_of(record)} GPU builder exists for {record.architecture}'
            )
        return None

    def list_rows(self, row_filter: Optional[ImageRowFilter] = None):
        rows = self.seed_rows()
        if row_filter is not None:
            rows = [r for r in rows if row_filter.matches(r)]
        return rows

    def _find(self, key: ImageRowKey) -> Optional[ImageBuildRecord]:
        wanted = row_id(
            ImageRowKey(
                base_os=key.base_os,
                architecture=key.architecture or 'x86_64',
                variant=key.variant or ImageVariant.CPU,
            )
        )
        return next((r for r in self.seed_rows() if row_id(r) == wanted), None)

    @staticmethod
    def _check_kind(key):
        kind = getattr(key, 'kind', None) if key is not None else None
        if kind is not None and getattr(kind, 'value', kind) != ImageKind.DESKTOP.value:
            raise exceptions.invalid_params(
                'VirtualDesktopAdmin serves desktop rows; use SchedulerAdmin for compute rows'
            )

    # queueing (RefreshImages and the triggers)

    def queue(
        self,
        record: ImageBuildRecord,
        trigger: ImageBuildTrigger,
        requested_by: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[str, str]:
        """
        (outcome, message). idempotent: a row already in flight is reported, not queued.
        a row baked today (cluster time) is skipped by every trigger unless force is set
        """
        from zoneinfo import ZoneInfo

        if record.pinned:
            return 'pinned', 'the row is pinned; unpin it to bake'
        if record.is_in_flight():
            return 'in_flight', f'already {record.status}'
        now = now_utc()
        if not force and record.baked_today(
            now, ZoneInfo(self.context.cluster_timezone())
        ):
            return 'baked_today', 'already baked today; Force rebake bakes it again'
        reason = self.unsupported_reason(record)
        if reason:
            if record.status != S.UNSUPPORTED.value or record.error != reason:
                record.status = S.UNSUPPORTED.value
                record.error = reason
                self.records.put(record)
            return 'unsupported', reason
        previous_status = record.status
        record.status = S.QUEUED.value
        record.trigger = trigger
        record.release = self.version
        record.requested_by = requested_by or trigger.value
        record.attempts = 0
        record.retry_after = None
        record.error = None
        record.started_on = now
        record.finished_on = None
        record.host = self.host
        expected = {
            'status': [s for s in ACTIVE] + [S.QUEUED.value, S.WAITING_CAPACITY.value]
        }
        if not self.records.put_if(record, expected):
            record.status = previous_status
            return 'in_flight', 'another request queued this row first'
        return 'queued', f'queued ({trigger.value})'

    def refresh(
        self, request: RefreshImagesRequest, requested_by: Optional[str]
    ) -> List[ImageRefreshResult]:
        chosen = [
            bool(request.all),
            request.rows is not None,
            request.filter is not None,
        ]
        if sum(chosen) != 1:
            raise exceptions.invalid_params('give exactly one of all, rows or filter')
        rows = self.seed_rows()
        results: List[ImageRefreshResult] = []
        if request.rows is not None:
            by_id = {row_id(r): r for r in rows}
            targets = []
            for key in request.rows:
                self._check_kind(key)
                record = by_id.get(
                    row_id(
                        ImageRowKey(
                            base_os=key.base_os,
                            architecture=key.architecture or 'x86_64',
                            variant=key.variant or ImageVariant.CPU,
                        )
                    )
                )
                if record is None:
                    results.append(
                        ImageRefreshResult(
                            row=key,
                            outcome='not_found',
                            message='no such desktop image row',
                        )
                    )
                else:
                    targets.append(record)
        elif request.filter is not None:
            self._check_kind(request.filter)
            targets = [r for r in rows if request.filter.matches(r)]
        else:
            targets = rows
        for record in targets:
            try:
                outcome, message = self.queue(
                    record, ImageBuildTrigger.BUTTON, requested_by, bool(request.force)
                )
            except Exception as e:
                self._logger.error(f'{row_id(record)}: could not queue: {e}')
                outcome, message = (
                    'error',
                    f'{e.__class__.__name__}: see the controller log',
                )
            results.append(
                ImageRefreshResult(
                    row=record.row_key(),
                    outcome=outcome,
                    message=message,
                    record=record,
                )
            )
        return results

    def set_pinned(self, key: ImageRowKey, pinned: bool) -> ImageBuildRecord:
        self._check_kind(key)
        record = self._find(key)
        if record is None:
            raise exceptions.invalid_params('no such desktop image row')
        record.pinned = bool(pinned)
        if (
            self.records.get(record.base_os, record.architecture, record.variant)
            is None
        ):
            self.records.put(record)  # a never-built row gets its first record
        self.records.update_fields(record, {'pinned': record.pinned})
        if not record.is_in_flight():
            status = record.status
            if pinned:
                status = S.PINNED.value
            elif status == S.PINNED.value:
                status = S.CURRENT.value if record.current_image_id else None
            # a running job's status wins; it reads the pin at promotion
            if self.records.update_fields(
                record, {'status': status}, unless_status=IMAGE_ROW_IN_FLIGHT
            ):
                record.status = status
        return record

    def rollback(
        self, key: ImageRowKey, requested_by: Optional[str]
    ) -> ImageBuildRecord:
        """current <- previous on the row and its unpinned targets; holds automatic promotion"""
        self._check_kind(key)
        record = self._find(key)
        if record is None:
            raise exceptions.invalid_params('no such desktop image row')
        if record.is_in_flight():
            raise exceptions.invalid_params(
                f'the row is {record.status}; roll back once the bake has finished'
            )
        previous = record.previous_image_id
        if not previous:
            raise exceptions.invalid_params('the row has no previous validated image')
        try:
            promote_gate(record, previous)
        except ImageNotValidated as e:
            raise exceptions.invalid_params(str(e))
        image = describe_images_by_id(self.context.aws().ec2(), [previous]).get(
            previous
        )
        if image is None or image.get('State', 'available') != 'available':
            raise exceptions.invalid_params(
                f'previous image {previous} is no longer available'
            )
        self._repoint(record, previous, image_source=None)
        record.previous_image_id, record.current_image_id = (
            record.current_image_id,
            previous,
        )
        record.rollback_hold = True
        record.promoted_on = now_utc()
        record.requested_by = requested_by
        record.status = S.PINNED.value if record.pinned else S.CURRENT.value
        record.error = None
        self.records.put(record)
        self._logger.warning(
            f'{row_id(record)} rolled back to {previous} by {requested_by}; automatic promotion held'
        )
        return record

    # leader loop

    def tick(self, now: Optional[datetime] = None, blocking: bool = False):
        now = now or now_utc()
        # until this release has settled, nothing promotes before customized stacks are
        # pinned; a failure here stops the tick, and the next tick tries again
        if self.context.config().get_string(BAKED_RELEASE_KEY, default=None) != (
            self.version
        ):
            self.pin_customized_base_stacks()
        rows = self.managed()
        settings = self.settings()
        for record in rows:
            self._adopt(record, now, blocking)
        self._release_trigger()
        self._monthly_trigger(now)
        rows = self.managed()
        active = sum(1 for r in rows if r.status in ACTIVE)
        queued = sorted(
            (r for r in rows if r.status == S.QUEUED.value),
            key=lambda r: r.started_on or now,
        )
        for record in queued:
            if active >= (settings.max_concurrent_bakes or 1):
                break
            if self._start(record, blocking):
                active += 1
        if time.monotonic() - self._last_cleanup > CLEANUP_EVERY_SECONDS:
            self._last_cleanup = time.monotonic()
            self.cleanup()

    def pin_customized_base_stacks(self) -> List[str]:
        """
        the upgrade step for base stacks an administrator pointed at their own image before
        stack pins existed: pin them, so the release bake does not replace that image.
        managed, and left unpinned: an image the seeding config names, any image a
        managed row recorded (pipeline and pre-pipeline builder output alike), a stock
        image a base refresh set (ami_id == base_ami_id), an image this cluster's
        pipeline tagged, and the vendor's stock image. anything else, including a custom
        build and an image that no longer exists, is the administrator's. idempotent:
        pinned stacks are skipped. returns the stack ids it pinned
        """
        stacks = [
            s
            for s in self._all_stacks()
            if (s.stack_id or '').startswith('ss-base-')
            and not s.image_pinned
            and s.ami_id
        ]
        if not stacks:
            return []
        region = self.context.aws().ec2().meta.region_name
        managed = set()
        for arches in (self._stack_db.get_base_software_stack_config() or {}).values():
            for arch_config in (arches or {}).values():
                for entry in (arch_config or {}).get(region) or []:
                    managed.add((entry or {}).get('ami-id'))
        for record in self.managed():
            managed.update(
                (record.image_id, record.current_image_id, record.previous_image_id)
            )
        unknown = [
            s
            for s in stacks
            if s.ami_id not in managed
            and not (s.base_ami_id and s.ami_id == s.base_ami_id)
        ]
        if not unknown:
            return []
        images = self._describe_for_pinning({s.ami_id for s in unknown})
        pinned = []
        for stack in unknown:
            image = images.get(stack.ami_id)
            base_os = getattr(stack.base_os, 'value', stack.base_os)
            vendors = [o for o in trusted_owners(base_os, region) if o != 'self']
            if image is not None and (
                any(
                    t.get('Key') == PIPELINE_IMAGE_TAG and t.get('Value') == 'desktop'
                    for t in image.get('Tags', [])
                )
                or image.get('OwnerId') in vendors
                or image.get('ImageOwnerAlias') in vendors
            ):
                continue
            fresh = self._stack_db.get(stack_id=stack.stack_id, base_os=stack.base_os)
            if fresh is None or fresh.image_pinned or fresh.ami_id != stack.ami_id:
                continue
            fresh.image_pinned = True
            updated = self._stack_db.update(fresh)
            self._stack_utils.update_software_stack_entry_to_opensearch(updated)
            pinned.append(stack.stack_id)
            self._logger.warning(
                f'{stack.stack_id} pinned: it launches from {stack.ami_id}'
                f'{"" if image else " (not found)"}, which is neither a stock image nor '
                'one the image pipeline built; unpin it to let the pipeline replace it'
            )
        return pinned

    def _describe_for_pinning(self, image_ids) -> Dict[str, Dict]:
        """describe_images by id; an id EC2 does not know is absent, any other error raises"""
        ec2 = self.context.aws().ec2()
        found: Dict[str, Dict] = {}
        for image_id in sorted(image_ids):
            image = describe_image_or_none(ec2, image_id)
            if image is not None:
                found[image_id] = image
        return found

    def _alive(self, record) -> bool:
        with _LIVE_LOCK:
            thread = _LIVE.get(row_id(record))
        return thread is not None and thread.is_alive()

    def _adopt(self, record: ImageBuildRecord, now: datetime, blocking: bool = False):
        """rows whose job is not running here: waiting rows past retry_after, orphans of a dead process"""
        if record.status == S.WAITING_CAPACITY.value:
            if record.retry_after is not None and record.retry_after > now:
                return
            # an image already built resumes at the test launch, not a rebake
            if record.image_id and not record.validated_on:
                record.status = S.TEST_LAUNCHING.value
            else:
                record.status = S.QUEUED.value
            record.retry_after = None
            self.records.put(record)
            if record.status == S.QUEUED.value:
                return  # starts with the other queued rows
        if record.status not in ACTIVE or self._alive(record):
            return
        self._logger.warning(
            f'{row_id(record)} was {record.status} on {record.host} with no running job; resuming'
        )
        if record.status in (S.BUILDING.value, S.CHECKING.value):
            resume_record(self.context, self.records, record, self._logger)
        elif record.status == S.RESOLVING.value:
            record.status = S.QUEUED.value
            record.host = self.host
            self.records.put(record)
        else:
            # test_launching and promoting pick up where they were
            record.host = self.host
            self.records.put(record)
        if record.status in ACTIVE:
            self._start(record, blocking=blocking)

    def _release_trigger(self):
        config = self.context.config()
        baked = config.get_string(BAKED_RELEASE_KEY, default=None)
        if baked == self.version:
            return
        rows = self.seed_rows()
        # a row baked today waits for tomorrow's tick; the release stays unsettled till then
        waiting = False
        for record in rows:
            if (
                record.release != self.version
                and not record.pinned
                and not record.rollback_hold
                and not record.is_in_flight()
            ):
                if self.queue(record, ImageBuildTrigger.RELEASE)[0] == 'baked_today':
                    waiting = True
        if not waiting and all(not r.is_in_flight() for r in self.managed()):
            self._set_config(BAKED_RELEASE_KEY, self.version)
            self._logger.info(f'every desktop image row is settled for {self.version}')

    def _monthly_trigger(self, now: datetime):
        from zoneinfo import ZoneInfo

        config = self.context.config()
        schedule = ImageRefreshSchedule(
            **dict(config.get_config(SCHEDULE_KEY, default={}) or {})
        )
        tz = ZoneInfo(self.context.cluster_timezone())
        last_ms = config.get_int(LAST_RUN_KEY, default=None)
        if last_ms is None:
            # the release trigger covers a first bake; the schedule counts from here
            self._claim_config(LAST_RUN_KEY, None, int(now.timestamp() * 1000))
            return
        last = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).astimezone(tz)
        due = schedule.next_run_after(last)
        if due is None or due > now.astimezone(tz):
            return
        # claim the period first, so a controller reading a lagging copy of last-run
        # does not run the check twice
        if not self._claim_config(LAST_RUN_KEY, last_ms, int(now.timestamp() * 1000)):
            return
        ec2 = self.context.aws().ec2()
        queued = []
        for record in self.seed_rows():
            if record.pinned or record.rollback_hold or record.is_in_flight():
                continue
            # one row's lookup failing must not skip the rest until next month
            try:
                if self.unsupported_reason(record):
                    continue
                latest = resolve_stock_image(
                    ec2, record.base_os, record.architecture, self._logger
                )
                changed = latest is not None and latest['ImageId'] != record.source_ami
                if changed or record.release != self.version:
                    if self.queue(record, ImageBuildTrigger.MONTHLY)[0] == 'queued':
                        queued.append(row_id(record))
            except Exception as e:
                self._logger.error(f'{row_id(record)}: monthly image check failed: {e}')
        self._logger.info(f'monthly image check: queued {queued or "nothing"}')

    def _start(self, record: ImageBuildRecord, blocking: bool) -> bool:
        with _LIVE_LOCK:
            existing = _LIVE.get(row_id(record))
            if existing is not None and existing.is_alive():
                return False
            if blocking:
                _LIVE.pop(row_id(record), None)
            else:
                thread = threading.Thread(
                    target=self.run,
                    args=(record,),
                    name=f'image-bake-{row_id(record)}',
                    daemon=True,
                )
                _LIVE[row_id(record)] = thread
        if blocking:
            self.run(record)
        else:
            thread.start()
        return True

    # the job

    def run(self, record: ImageBuildRecord):
        phases = {
            S.QUEUED.value: self._resolve,
            S.RESOLVING.value: self._resolve,
            S.BUILDING.value: self._build,
            S.CHECKING.value: self._build,
            S.TEST_LAUNCHING.value: self._test_launch,
            S.PROMOTING.value: self._promote,
        }
        try:
            if record.host != self.host:
                self._claim(record)
            while record.status in phases:
                if not self._leader():
                    self._logger.warning(
                        f'{row_id(record)}: no longer the leader; leaving it'
                    )
                    return
                phases[record.status](record)
        except LostOwnership:
            self._logger.warning(f'{row_id(record)}: taken over by another controller')
        except CapacityWait as e:
            self._wait_for_capacity(record, str(e))
        except RowFailed as e:
            self._fail(record, str(e))
        except Exception as e:
            self._logger.exception(f'{row_id(record)}: bake failed')
            self._fail(record, ImageBuildRunner._short_error(e, 'bake failed'))

    def _leader(self) -> bool:
        try:
            return bool(self.context.is_leader())
        except Exception:
            return False

    def _claim(self, record: ImageBuildRecord):
        stored_host = record.host
        record.host = self.host
        if not self.records.put_if(record, {'host': stored_host}):
            raise LostOwnership()

    def _save(self, record: ImageBuildRecord, **expected):
        # the pin is the admin's: a job never writes back a stale one (SetImagePinned
        # updates only that attribute while a bake runs)
        stored = self.records.get(record.base_os, record.architecture, record.variant)
        if stored is not None:
            record.pinned = stored.pinned
        if not self.records.put_if(record, {'host': self.host, **expected}):
            raise LostOwnership()

    def _fail(self, record: ImageBuildRecord, reason: str):
        record.status = S.FAILED.value
        record.error = reason
        record.finished_on = now_utc()
        try:
            self._save(record)
        except LostOwnership:
            pass
        self._logger.error(f'{row_id(record)} failed: {reason}')

    def _wait_for_capacity(self, record: ImageBuildRecord, reason: str):
        record.attempts = (record.attempts or 0) + 1
        if record.attempts >= CAPACITY_MAX_ATTEMPTS:
            return self._fail(
                record, f'no EC2 capacity after {record.attempts} tries: {reason}'[:300]
            )
        delay = CAPACITY_BACKOFF * (2 ** min(record.attempts - 1, 3))
        record.status = S.WAITING_CAPACITY.value
        record.retry_after = now_utc() + delay
        record.error = f'waiting for EC2 capacity: {reason}'[:300]
        try:
            self._save(record)
        except LostOwnership:
            pass

    def _resolve(self, record: ImageBuildRecord):
        record.status = S.RESOLVING.value
        record.checks = []
        record.image_id = None
        record.instance_id = None
        record.validated_on = None
        record.error = None
        self._save(record)
        reason = self.unsupported_reason(record)
        if reason:
            record.status = S.UNSUPPORTED.value
            record.error = reason
            self._save(record)
            return
        image = resolve_stock_image(
            self.context.aws().ec2(), record.base_os, record.architecture, self._logger
        )
        if image is None:
            raise RowFailed(
                f'no available {record.base_os} {record.architecture} image from the vendor in this region'
            )
        record.source_ami = record.base_ami = image['ImageId']
        record.status = S.BUILDING.value
        self._save(record)

    def _build(self, record: ImageBuildRecord):
        """bake from source_ami; the builder snapshots only after a 'complete' tag and readable checks"""
        variant = variant_of(record)
        suffix = '' if variant == ImageVariant.CPU.value else f'-{variant}'
        windows = is_windows(record.base_os)

        def progress(update: Dict):
            for key, value in update.items():
                setattr(record, key, value)
            if update.get('instance_id'):
                record.log_link = builder_log_link(self.context, record.instance_id)
            self._save(record)

        def before_snapshot(instance_id: str, status: str):
            record.status = S.CHECKING.value
            self._save(record)
            try:
                release, checks = read_in_bake_checks(
                    self.context, instance_id, windows
                )
            except Exception as e:
                raise RowFailed(f'in-bake check results unreadable: {e}')
            record.checks = checks
            self._save(record)
            failing = [c for c in checks if not c.ok]
            if failing:
                raise RowFailed(
                    f'in-bake check {failing[0].name} failed on builder {instance_id}: '
                    f'{failing[0].detail}'
                )
            if status != AMI_BUILDER_STATUS_COMPLETE:
                return  # the builder raises with the status tag
            if release != self.version:
                raise RowFailed(
                    f'the builder ran the {release} bootstrap, not {self.version}'
                )

        try:
            builder = DcvHostImageBuilder(
                context=self.context,
                base_ami=record.source_ami,
                base_os=record.base_os,
                ami_name=f'{DESKTOP_IMAGE_PREFIX}{record.base_os}{suffix}',
                instance_type=BUILDER_INSTANCE_TYPES.get(
                    (record.architecture, variant)
                ),
                ebs_volume_size=self._builder_volume_gb(record),
                force=True,
                before_snapshot=before_snapshot,
                image_tags={PIPELINE_IMAGE_TAG: 'desktop'},
            )
            record.ami_name = builder.get_ami_full_name()
            self._save(record)
            image_id = builder.build(progress)
        except (RowFailed, LostOwnership, CapacityWait):
            raise
        except Exception as e:
            if is_capacity_problem(str(e)):
                raise CapacityWait(str(e))
            message = getattr(e, 'message', None) or str(e)
            raise RowFailed(message[:300])
        record.image_id = image_id
        record.status = S.TEST_LAUNCHING.value
        self._save(record)

    def _builder_volume_gb(self, record: ImageBuildRecord) -> Optional[int]:
        """
        the root size the row's base stacks launch desktops with: the bake needs the same
        room a desktop has (a GUI does not fit the vendor's default), and a desktop launched
        from the image needs a root at least as large as the image's
        """
        targets = self.targets_for(record.row_key()) or self.targets_for(
            ImageRowKey(
                base_os=record.base_os,
                architecture=record.architecture,
                variant=ImageVariant.CPU,
            )
        )
        sizes = [int(s.min_storage.int_val()) for s in targets if s.min_storage]
        return max(sizes) if sizes else None

    def _test_launch(self, record: ImageBuildRecord):
        targets = self.targets_for(record.row_key())
        base = next(iter(targets), None)
        if base is None:
            # a GPU row without an ss-base GPU stack launches from the CPU base stack
            base = next(
                iter(
                    self.targets_for(
                        ImageRowKey(
                            base_os=record.base_os,
                            architecture=record.architecture,
                            variant=ImageVariant.CPU,
                        )
                    )
                ),
                None,
            )
        if base is None:
            raise RowFailed(
                f'no ss-base stack for {record.base_os} {record.architecture} to test-launch from'
            )
        self._wait_image_available(record.image_id)
        terminate_builder(self.context, record.instance_id, self._logger)
        checks = self.tester.test_launch(record, base, self.settings())
        record.checks = list(record.checks or []) + checks
        failing = [c for c in checks if not c.ok]
        if failing:
            raise RowFailed(f'{failing[0].name}: {failing[0].detail}'[:300])
        record.validated_on = now_utc()
        record.status = S.PROMOTING.value
        self._save(record)

    def _wait_image_available(self, image_id: str, timeout: int = 3600):
        deadline = time.time() + timeout
        while True:
            image = describe_images_by_id(self.context.aws().ec2(), [image_id]).get(
                image_id
            )
            state = (image or {}).get('State')
            if state == 'available':
                return
            if state != 'pending' or time.time() > deadline:
                raise RowFailed(f'candidate image {image_id} is {state or "missing"}')
            time.sleep(15)

    def _promote(self, record: ImageBuildRecord):
        candidate = record.image_id
        if record.pinned:
            record.status = S.PINNED.value
            record.error = (
                f'{candidate} validated; not promoted because the row is pinned'
            )
            record.finished_on = now_utc()
            self._save(record)
            return
        if record.rollback_hold and record.trigger != ImageBuildTrigger.BUTTON:
            record.status = S.CURRENT.value
            record.error = (
                f'{candidate} validated; not promoted because of a rollback hold'
            )
            record.finished_on = now_utc()
            self._save(record)
            return
        promote_gate(record, candidate)
        # only a validated generation becomes the rollback target: before the first
        # promotion the row's current image is the stock or legacy one it was seeded with
        if (
            record.promoted_on is not None
            and record.current_image_id
            and record.current_image_id != candidate
        ):
            record.previous_image_id = record.current_image_id
        record.current_image_id = candidate
        record.promoted_on = record.finished_on = now_utc()
        record.rollback_hold = False
        record.status = S.CURRENT.value
        record.error = None
        # the one guarded write: still ours, still promoting this validated candidate.
        # it comes before any stack moves, so a refused write (LostOwnership) leaves every
        # stack where it was
        self._save(record, status=S.PROMOTING.value, image_id=candidate)
        try:
            self._repoint(record, candidate, image_source=record.source_ami)
        except Exception as e:
            # the row is promoted; stacks left behind catch up on Use built image or the
            # next promotion, which moves every unpinned target
            self._logger.error(
                f'{row_id(record)} promoted {candidate} but repointing its stacks failed: {e}'
            )
        self._logger.info(f'{row_id(record)} promoted {candidate}')
        self.cleanup()

    def _repoint(
        self, record: ImageBuildRecord, image_id: str, image_source: Optional[str]
    ):
        """point the row's unpinned ss-base targets at image_id, re-reading each stack first"""
        for stack in self.targets_for(record.row_key()):
            fresh = self._stack_db.get(stack_id=stack.stack_id, base_os=stack.base_os)
            if fresh is None or fresh.image_pinned or fresh.ami_id == image_id:
                continue
            # conditional: an admin who pins or repoints the stack after the read above wins
            updated = self._stack_db.repoint_image(
                fresh, fresh.ami_id, image_id, image_source
            )
            if updated is None:
                self._logger.info(
                    f'{row_id(record)} left {stack.stack_id}: it was pinned or changed meanwhile'
                )
                continue
            self._stack_utils.update_software_stack_entry_to_opensearch(updated)

    # cleanup

    def cleanup(self):
        """keep every row's current + previous; deregister the rest of this cluster's built images; reap leftovers"""
        try:
            self._deregister_unreferenced()
        except Exception as e:
            self._logger.error(f'image cleanup failed: {e}')
        try:
            deregister_legacy_images(
                self.context,
                self.context.config().get_int(
                    'virtual-desktop-controller.images.legacy_cleanup_min_age_days',
                    default=LEGACY_MIN_AGE_DAYS,
                ),
                {
                    r.ami_name
                    for r in self.records.list_all()
                    if r.status not in TERMINAL
                },
                self._logger,
            )
        except Exception as e:
            self._logger.error(f'legacy image cleanup failed: {e}')
        try:
            self._reap_builders()
        except Exception as e:
            self._logger.error(f'builder reaping failed: {e}')
        try:
            if self.tester is not None:
                self.tester.reap(self.settings(), REAP_AFTER)
        except Exception as e:
            self._logger.error(f'validation desktop reaping failed: {e}')

    def protected_images(self) -> set:
        protected = set()
        for record in self.records.list_all():
            protected.update(
                i for i in (record.current_image_id, record.previous_image_id) if i
            )
            # a custom build's image is the admin's; an in-flight candidate is the job's
            if is_custom_record(record) or record.status not in TERMINAL:
                protected.add(record.image_id)
        for stack in self._all_stacks():
            protected.update(i for i in (stack.ami_id, stack.base_ami_id) if i)
        protected.discard(None)
        return protected

    def _deregister_unreferenced(self) -> List[str]:
        # a bake between CreateImage and recording the id: its image carries the row's name
        baking = {
            r.ami_name for r in self.records.list_all() if r.status not in TERMINAL
        }
        return deregister_unreferenced_images(
            self.context,
            'desktop',
            DESKTOP_IMAGE_PREFIX,
            self.protected_images(),
            baking,
            self._logger,
        )

    def _reap_builders(self) -> List[str]:
        ec2 = self.context.aws().ec2()
        busy = {
            r.instance_id
            for r in self.records.list_all()
            if r.status in ACTIVE and r.instance_id
        }
        cutoff = now_utc() - REAP_AFTER
        stale = []
        result = ec2.describe_instances(
            Filters=[
                {'Name': 'tag-key', 'Values': [IMAGE_BUILD_TAG]},
                {
                    'Name': f'tag:{constants.IDEA_TAG_MODULE_ID}',
                    'Values': [self.context.module_id()],
                },
                {
                    'Name': 'instance-state-name',
                    'Values': ['pending', 'running', 'stopping', 'stopped'],
                },
            ]
        )
        for reservation in result.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                launched = instance.get('LaunchTime')
                if (
                    instance['InstanceId'] not in busy
                    and launched is not None
                    and launched < cutoff
                ):
                    stale.append(instance['InstanceId'])
        if stale:
            ec2.terminate_instances(InstanceIds=stale)
            self._logger.warning(
                f'terminated builder instances left over from dead bakes: {stale}'
            )
        return stale


def pipeline_for(api) -> DesktopImagePipeline:
    """the pipeline bound to a VirtualDesktopAdminAPI (its dbs, validation and session path), made once"""
    pipeline = getattr(api, '_image_pipeline', None)
    if pipeline is None:
        pipeline = DesktopImagePipeline(
            api.context,
            api.software_stack_db,
            api.software_stack_utils,
            tester=ImageTestLauncher(api.context, api),
        )
        api._image_pipeline = pipeline
    return pipeline
