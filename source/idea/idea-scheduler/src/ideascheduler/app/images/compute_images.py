"""
Compute image inventory and builds behind SchedulerAdmin.ListComputeImages and
SchedulerAdmin.BuildComputeImage, shared with ideactl ami-builder. One row per base OS
and architecture: the image the cluster launches for it today (scheduler default first,
then queue profiles), whether that is a build or a stock image, and how the last build
ended. A combination with neither an image nor a build record has no row.
"""

from typing import Dict, List, Optional, Tuple

from ideadatamodel import (
    BuildComputeImageRequest,
    ImageBuildRecord,
    ImageInventoryRow,
    exceptions,
)
from ideasdk.aws.image_builds import (
    BUILD_STATUS_BUILDING,
    COMPUTE_IMAGE_PREFIX,
    check_builder_instance_type,
    ImageBuildRecordsDB,
    ImageBuildRunner,
    build_stamp,
    describe_images_by_id,
    image_state,
    new_record,
    newest_owned_image,
)
from ideasdk.aws.stock_amis import find_latest_stock_ami, stock_unsupported_reason
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


class ComputeImageService:
    def __init__(self, context):
        self.context = context
        self._logger = context.logger('compute-images')
        self.records = ImageBuildRecordsDB(
            context, image_builds_table_name(context)
        ).initialize()
        self.runner = ImageBuildRunner(context, self.records, self._logger)

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
        records = {
            (record.base_os, record.architecture or 'x86_64'): record
            for record in self.records.list_all()
            if record.base_os in COMPUTE_BASE_OS
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
                record = self.runner.refresh(record)
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
        base_ami = request.base_ami or self.default_base_ami(base_os, architecture)
        if Utils.is_empty(base_ami):
            raise exceptions.invalid_params(
                f'no stock {base_os} {architecture} image could be resolved in this region; provide base_ami'
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
        """shared by the API (threaded) and ideactl ami-builder (blocking), so both leave a record"""
        base_image = builder.get_image_by_id(builder.base_ami) or {}
        record = new_record(
            base_os=builder.base_os,
            architecture=base_image.get('Architecture', 'x86_64'),
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
        supported = ec2_instance_type.processor_info_supported_architectures()
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
        """
        the scheduler default when it is a stock image of this OS and architecture, else
        the newest stock image the vendor publishes. a build never stacks on a build.
        """
        config = self.context.config()
        ec2_client = self.context.aws().ec2()
        default_os = config.get_string('scheduler.compute_node_os', default=None)
        default_ami = config.get_string('scheduler.compute_node_ami', default=None)
        if default_os == base_os and Utils.is_not_empty(default_ami):
            image = describe_images_by_id(ec2_client, [default_ami]).get(default_ami)
            if (
                image
                and image_state(image.get('Name'), COMPUTE_IMAGE_PREFIX) == 'stock'
                and image.get('Architecture', 'x86_64') == architecture
            ):
                return default_ami
        return find_latest_stock_ami(ec2_client, base_os, architecture, self._logger)
