"""
Image build bookkeeping shared by the scheduler (compute images) and the virtual
desktop controller (desktop images).

Each image row is keyed by (kind, base_os, architecture, variant). kind is implied by
the table: the controller's image-builds table holds desktop rows, the scheduler's
holds compute rows. The table key stays (base_os, architecture): a CPU row stores its
plain architecture there, a GPU row stores 'architecture#variant' (see
ImageRowKey.range_key), so every existing record is already a valid CPU row.
"""

from ideadatamodel.base import SocaBaseModel
from ideadatamodel.api import SocaPayload

from typing import Optional, List
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pydantic import Field

__all__ = (
    'ImageKind',
    'ImageVariant',
    'ImageRowStatus',
    'ImageBuildTrigger',
    'IMAGE_ROW_IN_FLIGHT',
    'LEGACY_IMAGE_STATUS',
    'ImageCheck',
    'ImageRowKey',
    'ImageRowFilter',
    'ImageRefreshSchedule',
    'ImagePipelineSettings',
    'ImageBuildRecord',
    'ImageInventoryRow',
    'ImageRefreshResult',
    'ListImageRowsRequest',
    'ListImageRowsResponse',
    'RefreshImagesRequest',
    'RefreshImagesResponse',
    'RollbackImageRequest',
    'RollbackImageResponse',
    'SetImagePinnedRequest',
    'SetImagePinnedResponse',
    'GetImageScheduleRequest',
    'GetImageScheduleResponse',
    'UpdateImageScheduleRequest',
    'UpdateImageScheduleResponse',
)


class ImageKind(str, Enum):
    DESKTOP = 'desktop'
    COMPUTE = 'compute'


class ImageVariant(str, Enum):
    CPU = 'cpu'
    NVIDIA = 'nvidia'
    AMD = 'amd'


class ImageRowStatus(str, Enum):
    QUEUED = 'queued'
    RESOLVING = 'resolving'
    BUILDING = 'building'
    CHECKING = 'checking'
    TEST_LAUNCHING = 'test_launching'
    PROMOTING = 'promoting'
    CURRENT = 'current'
    FAILED = 'failed'
    WAITING_CAPACITY = 'waiting_capacity'
    PINNED = 'pinned'
    UNSUPPORTED = 'unsupported'


class ImageBuildTrigger(str, Enum):
    RELEASE = 'release'
    MONTHLY = 'monthly'
    BUTTON = 'button'


# a row in one of these states has a job working on it: RefreshImages reports it and
# does not queue it again. waiting_capacity counts, since its job resumes at retry_after
IMAGE_ROW_IN_FLIGHT = frozenset(
    {
        ImageRowStatus.QUEUED.value,
        ImageRowStatus.RESOLVING.value,
        ImageRowStatus.BUILDING.value,
        ImageRowStatus.CHECKING.value,
        ImageRowStatus.TEST_LAUNCHING.value,
        ImageRowStatus.PROMOTING.value,
        ImageRowStatus.WAITING_CAPACITY.value,
    }
)

# status values written before 26.10.1. 'complete' meant built (and, when update_target
# was set, applied); it was never validated, so a migrated row has no validated_on and
# the promote gate refuses its image_id until a pipeline run validates one
LEGACY_IMAGE_STATUS = {
    'building': ImageRowStatus.BUILDING.value,
    'complete': ImageRowStatus.CURRENT.value,
    'failed': ImageRowStatus.FAILED.value,
}

_ORDINALS = ('first', 'second', 'third', 'fourth', 'last')
_WEEKDAYS = (
    'monday',
    'tuesday',
    'wednesday',
    'thursday',
    'friday',
    'saturday',
    'sunday',
)


class ImageCheck(SocaBaseModel):
    """one validation check: in-bake (on the builder) or test-launch (from the new image)"""

    # stable identifier, e.g. bootstrap_clean, kernel_default, lustre_module, dcv_installed,
    # directory_packages, gpu_driver, ready_gate, connection_info, dcv_session,
    # directory_user, filesystem:<name>, bootstrap_log, gpu_runtime, reboot_ready,
    # compute_job
    name: Optional[str] = Field(default=None)
    ok: Optional[bool] = Field(default=None)
    # a plain sentence; for a failure, what was expected and what was seen
    detail: Optional[str] = Field(default=None)
    # whole seconds: the record is stored in DynamoDB, which rejects floats
    seconds: Optional[int] = Field(default=None)


class ImageRowKey(SocaBaseModel):
    kind: Optional[ImageKind] = Field(default=None)
    base_os: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    # cpu when absent
    variant: Optional[ImageVariant] = Field(default=None)

    def range_key(self) -> str:
        """the image-builds table range key: x86_64 for CPU rows, x86_64#nvidia for GPU rows"""
        architecture = self.architecture or 'x86_64'
        variant = ImageVariant(self.variant or ImageVariant.CPU)
        if variant == ImageVariant.CPU:
            return architecture
        return f'{architecture}#{variant.value}'

    @staticmethod
    def split_range_key(value: str) -> tuple:
        """(architecture, variant) from a stored range key"""
        architecture, _, variant = value.partition('#')
        return architecture, variant or ImageVariant.CPU.value


class ImageRowFilter(SocaBaseModel):
    """every field present must match; an empty filter matches every row"""

    kind: Optional[ImageKind] = Field(default=None)
    # prefix of base_os: ubuntu, rhel, rocky, amazonlinux, windows
    base_os_family: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    variant: Optional[ImageVariant] = Field(default=None)
    statuses: Optional[List[ImageRowStatus]] = Field(default=None)

    def matches(self, record: 'ImageBuildRecord') -> bool:
        def value(v):
            return v.value if isinstance(v, Enum) else v

        if self.kind and value(record.kind) != value(self.kind):
            return False
        if self.base_os_family and not (record.base_os or '').startswith(
            self.base_os_family
        ):
            return False
        if self.architecture and record.architecture != self.architecture:
            return False
        if self.variant and value(record.variant or ImageVariant.CPU) != value(
            self.variant
        ):
            return False
        if self.statuses and record.status not in {value(s) for s in self.statuses}:
            return False
        return True


class ImageRefreshSchedule(SocaBaseModel):
    """
    the monthly vendor check, stored at vdc.software_stacks.image_refresh_schedule.
    the scheduler reads the same key for compute rows. hour is in cluster.timezone
    """

    enabled: Optional[bool] = Field(default=True)
    # '<first|second|third|fourth|last> <weekday>'
    day: Optional[str] = Field(default='first sunday')
    hour: Optional[int] = Field(default=2)

    def validate_rule(self):
        tokens = (self.day or '').lower().split()
        if len(tokens) != 2 or tokens[0] not in _ORDINALS or tokens[1] not in _WEEKDAYS:
            raise ValueError(
                f"day must look like 'first sunday' "
                f'({"|".join(_ORDINALS)} + a weekday), got: {self.day!r}'
            )
        if self.hour is None or not 0 <= self.hour <= 23:
            raise ValueError(f'hour must be 0-23, got: {self.hour!r}')

    def _run_in_month(self, year: int, month: int, tzinfo) -> datetime:
        ordinal, weekday = (self.day or '').lower().split()
        target = _WEEKDAYS.index(weekday)
        if ordinal == 'last':
            first_next = datetime(year + month // 12, month % 12 + 1, 1)
            day = first_next - timedelta(days=1)
            day -= timedelta(days=(day.weekday() - target) % 7)
        else:
            day = datetime(year, month, 1)
            day += timedelta(days=(target - day.weekday()) % 7)
            day += timedelta(weeks=_ORDINALS.index(ordinal))
        return day.replace(hour=self.hour, tzinfo=tzinfo)

    def next_run_after(self, after: datetime) -> Optional[datetime]:
        """the first scheduled run strictly after `after` (pass it in cluster time); None when disabled"""
        if not self.enabled:
            return None
        self.validate_rule()
        year, month = after.year, after.month
        for _ in range(2):
            run = self._run_in_month(year, month, after.tzinfo)
            if run > after:
                return run
            year, month = year + month // 12, month % 12 + 1
        return None


class ImagePipelineSettings(SocaBaseModel):
    """
    vdc.software_stacks.image_pipeline (desktop) and scheduler.images (compute; the
    gate and validation keys are read from vdc). defaults here match the settings
    templates and cover clusters deployed before the keys existed
    """

    max_concurrent_bakes: Optional[int] = Field(default=4)
    keep_generations: Optional[int] = Field(default=2)
    ready_gate_seconds_linux: Optional[int] = Field(default=600)
    ready_gate_seconds_windows: Optional[int] = Field(default=900)
    validation_user: Optional[str] = Field(default='idea-validate')
    validation_project: Optional[str] = Field(default='idea-validate')


class ImageBuildRecord(SocaBaseModel):
    """
    one image row, as kept in the module's image-builds table.

    rows written before 26.10.1 carry only base_os .. finished_on with status
    building | complete | failed; migrated() maps them onto the row model.
    """

    base_os: Optional[str] = Field(default=None)
    # the plain architecture; the stored range key may carry '#variant' (ImageRowKey)
    architecture: Optional[str] = Field(default=None)
    # ImageRowStatus value. a str so legacy rows still load; see LEGACY_IMAGE_STATUS
    status: Optional[str] = Field(default=None)
    ami_name: Optional[str] = Field(default=None)
    # legacy name of source_ami; still written by the builders until they move over
    base_ami: Optional[str] = Field(default=None)
    # the candidate: the image this run built (or is building)
    image_id: Optional[str] = Field(default=None)
    instance_id: Optional[str] = Field(default=None)
    requested_by: Optional[str] = Field(default=None)
    # the module host that runs the build thread; a restart there orphans the build
    host: Optional[str] = Field(default=None)
    # whether the caller asked for the default / base stack to be repointed on success
    update_target: Optional[bool] = Field(default=None)
    # a plain sentence an admin can act on
    error: Optional[str] = Field(default=None)
    started_on: Optional[datetime] = Field(default=None)
    finished_on: Optional[datetime] = Field(default=None)

    # 26.10.1 image row fields
    kind: Optional[ImageKind] = Field(default=None)
    # cpu when absent
    variant: Optional[ImageVariant] = Field(default=None)
    # the IDEA release whose bootstrap the candidate carries
    release: Optional[str] = Field(default=None)
    # the vendor base image the candidate was baked from
    source_ami: Optional[str] = Field(default=None)
    # what the row's targets launch from today, and the validated one before it
    current_image_id: Optional[str] = Field(default=None)
    previous_image_id: Optional[str] = Field(default=None)
    # set only when every check passed; promotion requires it for image_id
    validated_on: Optional[datetime] = Field(default=None)
    promoted_on: Optional[datetime] = Field(default=None)
    checks: Optional[List[ImageCheck]] = Field(default=None)
    # CloudWatch console link for the run's log stream
    log_link: Optional[str] = Field(default=None)
    attempts: Optional[int] = Field(default=None)
    # waiting_capacity: when the job tries again
    retry_after: Optional[datetime] = Field(default=None)
    trigger: Optional[ImageBuildTrigger] = Field(default=None)
    # the row is never rebaked or promoted automatically (row Pin/Unpin)
    pinned: Optional[bool] = Field(default=None)
    # set by RollbackImage: automatic promotion is held until the admin refreshes the row
    rollback_hold: Optional[bool] = Field(default=None)

    def row_key(self) -> ImageRowKey:
        return ImageRowKey(
            kind=self.kind,
            base_os=self.base_os,
            architecture=self.architecture,
            variant=self.variant or ImageVariant.CPU,
        )

    def is_in_flight(self) -> bool:
        return self.status in IMAGE_ROW_IN_FLIGHT

    def baked_today(self, now: datetime, tz: tzinfo) -> bool:
        """
        a bake started on now's calendar day in tz (the cluster timezone), whatever its
        outcome. every trigger skips such a row until the next day; only RefreshImages
        with force (administrators) bakes it again
        """
        started = self.started_on
        if started is None:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return started.astimezone(tz).date() == now.astimezone(tz).date()

    def migrated(self, kind: ImageKind) -> 'ImageBuildRecord':
        """
        a copy on the 26.10.1 row model. legacy status maps through
        LEGACY_IMAGE_STATUS, base_ami fills source_ami, a CPU variant and the table's
        kind are filled in. a legacy 'complete' row's image_id becomes current_image_id
        only when update_target was set (that build was applied); validated_on stays
        empty either way. rows already on the new model come back unchanged.
        """
        record = self.model_copy(deep=True)
        legacy_complete = record.status == 'complete'
        record.status = LEGACY_IMAGE_STATUS.get(record.status, record.status)
        record.kind = record.kind or kind
        record.variant = record.variant or ImageVariant.CPU
        record.source_ami = record.source_ami or record.base_ami
        if legacy_complete and record.update_target and not record.current_image_id:
            record.current_image_id = record.image_id
        return record


class ImageInventoryRow(SocaBaseModel):
    """one line of the Custom AMIs page: what an OS runs on today and what the last build did"""

    base_os: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    # desktop rows: the ss-base-* software stack the image belongs to
    stack_id: Optional[str] = Field(default=None)
    image_id: Optional[str] = Field(default=None)
    image_name: Optional[str] = Field(default=None)
    # desktop rows: the stock image the next build starts from (the stack's base_ami_id)
    base_ami_id: Optional[str] = Field(default=None)
    # built | built_outdated (a newer stock base exists than the built image came from)
    # | stock | missing | none | building
    state: Optional[str] = Field(default=None)
    build_date: Optional[datetime] = Field(default=None)
    referenced_by: Optional[List[str]] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    last_build: Optional[ImageBuildRecord] = Field(default=None)


# image pipeline API. served as VirtualDesktopAdmin.<Name> (desktop rows, plus the
# schedule) and SchedulerAdmin.<Name> (compute rows); same payloads in both namespaces.
# the kind on a request key or filter must match the namespace or be absent.


# ListImageRows - Request
class ListImageRowsRequest(SocaPayload):
    filter: Optional[ImageRowFilter] = Field(default=None)


# ListImageRows - Response: every known row (seeded from the base stacks / compute OS
# list, so a never-built row still shows), migrated() to the row model
class ListImageRowsResponse(SocaPayload):
    listing: Optional[List[ImageBuildRecord]] = Field(default=None)


# RefreshImages - per-row outcome
class ImageRefreshResult(SocaPayload):
    row: Optional[ImageRowKey] = Field(default=None)
    # queued | in_flight (already running; not queued again) | baked_today (already
    # baked today; force bakes it again) | pinned | unsupported | not_found | error
    outcome: Optional[str] = Field(default=None)
    message: Optional[str] = Field(default=None)
    record: Optional[ImageBuildRecord] = Field(default=None)


# RefreshImages - Request: exactly one of all, rows, filter. always trigger=button and
# rebakes unchanged rows too (the admin asked); the monthly and release triggers skip
# them. a row baked today is skipped (outcome baked_today) unless force is set, and
# only an administrator may set force
class RefreshImagesRequest(SocaPayload):
    all: Optional[bool] = Field(default=None)
    rows: Optional[List[ImageRowKey]] = Field(default=None)
    filter: Optional[ImageRowFilter] = Field(default=None)
    force: Optional[bool] = Field(default=None)


# RefreshImages - Response
class RefreshImagesResponse(SocaPayload):
    results: Optional[List[ImageRefreshResult]] = Field(default=None)


# RollbackImage - Request: current <- previous, sets rollback_hold
class RollbackImageRequest(SocaPayload):
    row: Optional[ImageRowKey] = Field(default=None)


# RollbackImage - Response
class RollbackImageResponse(SocaPayload):
    record: Optional[ImageBuildRecord] = Field(default=None)


# SetImagePinned - Request
class SetImagePinnedRequest(SocaPayload):
    row: Optional[ImageRowKey] = Field(default=None)
    pinned: Optional[bool] = Field(default=None)


# SetImagePinned - Response
class SetImagePinnedResponse(SocaPayload):
    record: Optional[ImageBuildRecord] = Field(default=None)


# VirtualDesktopAdmin.GetImageSchedule - Request
class GetImageScheduleRequest(SocaPayload):
    pass


# VirtualDesktopAdmin.GetImageSchedule - Response
class GetImageScheduleResponse(SocaPayload):
    schedule: Optional[ImageRefreshSchedule] = Field(default=None)
    # vdc.software_stacks.image_refresh_last_run_on: the desktop monthly check's last run
    last_run_on: Optional[datetime] = Field(default=None)
    next_run_on: Optional[datetime] = Field(default=None)


# VirtualDesktopAdmin.UpdateImageSchedule - Request
class UpdateImageScheduleRequest(SocaPayload):
    schedule: Optional[ImageRefreshSchedule] = Field(default=None)


# VirtualDesktopAdmin.UpdateImageSchedule - Response
class UpdateImageScheduleResponse(SocaPayload):
    schedule: Optional[ImageRefreshSchedule] = Field(default=None)
    next_run_on: Optional[datetime] = Field(default=None)
