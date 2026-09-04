"""
Desktop image inventory and builds behind VirtualDesktopAdmin.ListDesktopImages and
BuildDesktopImage. One row per ss-base-<os>-<arch>-base software stack: the AMI desktops
launch from today, whether it is a built image or a stock one, and how the last build
ended. A successful build repoints the base stack and reindexes it, the same thing
ideactl build-desktop-image --update-stack does.
"""

import socket
from datetime import datetime, timezone
from typing import Dict, List, Optional

import ideavirtualdesktopcontroller
from ideadatamodel import (
    BuildAllDesktopImagesRequest,  # noqa: F401  (api symmetry)
    BuildDesktopImageRequest,
    DesktopImageBuildStartResult,
    UseBuiltDesktopImagesRequest,  # noqa: F401  (api symmetry)
    ImageBuildRecord,
    ImageInventoryRow,
    ListSoftwareStackRequest,
    VirtualDesktopSoftwareStack,
    exceptions,
)
from ideasdk.aws.image_builds import (
    BUILD_STATUS_BUILDING,
    MAX_CONCURRENT_BUILDS,
    check_builder_instance_type,
    ImageBuildRecordsDB,
    ImageBuildRunner,
    build_stamp,
    describe_images_by_id,
    image_state,
    new_record,
)
from ideasdk.aws.stock_amis import find_latest_stock_ami, stock_unsupported_reason
from ideavirtualdesktopcontroller.app.software_stacks.dcv_host_image_builder import (
    BUILD_SUPPORTED_BASE_OS,
    DcvHostImageBuilder,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
)
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_utils import (
    VirtualDesktopSoftwareStackUtils,
)

DESKTOP_IMAGE_PREFIX = 'idea-dcv-host-'


def image_builds_table_name(context) -> str:
    return f'{context.cluster_name()}.{context.module_id()}.controller.image-builds'


STACK_ARCH_TO_EC2 = {'x86-64': 'x86_64', 'arm64': 'arm64'}
EC2_ARCH_TO_STACK = {value: key for key, value in STACK_ARCH_TO_EC2.items()}


def base_stack_id(base_os: str, architecture: str) -> str:
    return f'ss-base-{base_os}-{EC2_ARCH_TO_STACK.get(architecture, architecture)}-base'


def parse_base_stack_id(stack_id: Optional[str]):
    """('rocky9', 'x86_64') for ss-base-rocky9-x86-64-base, else None"""
    if (
        Utils.is_empty(stack_id)
        or not stack_id.startswith('ss-base-')
        or not stack_id.endswith('-base')
    ):
        return None
    body = stack_id[len('ss-base-') : -len('-base')]
    base_os, _, arch_key = body.partition('-')
    if Utils.is_empty(base_os) or arch_key not in STACK_ARCH_TO_EC2:
        return None
    return base_os, STACK_ARCH_TO_EC2[arch_key]


class DesktopImageService:
    def __init__(
        self,
        context: ideavirtualdesktopcontroller.AppContext,
        software_stack_db: VirtualDesktopSoftwareStackDB,
        software_stack_utils: VirtualDesktopSoftwareStackUtils,
    ):
        self.context = context
        self._software_stack_db = software_stack_db
        self._software_stack_utils = software_stack_utils
        self._logger = context.logger('desktop-images')
        self.records = ImageBuildRecordsDB(
            context, image_builds_table_name(context)
        ).initialize()
        self.runner = ImageBuildRunner(context, self.records, self._logger)

    def _all_stacks(self) -> List[VirtualDesktopSoftwareStack]:
        request = ListSoftwareStackRequest(disabled_also=True)
        stacks: List[VirtualDesktopSoftwareStack] = []
        while True:
            response = self._software_stack_db.list_all_from_db(request)
            stacks.extend(response.listing or [])
            if Utils.is_empty(response.cursor):
                return stacks
            request.paginator = response.paginator

    def _supported_base_stacks(self):
        """([(stack, (base_os, architecture))...], {ami_id: custom stack count})"""
        base_stacks = []
        sharing: Dict[str, int] = {}
        for stack in self._all_stacks():
            parsed = parse_base_stack_id(stack.stack_id)
            if parsed is None:
                if Utils.is_not_empty(stack.ami_id):
                    sharing[stack.ami_id] = sharing.get(stack.ami_id, 0) + 1
                continue
            if parsed[0] in BUILD_SUPPORTED_BASE_OS:
                base_stacks.append((stack, parsed))
        return base_stacks, sharing

    def list_images(self) -> List[ImageInventoryRow]:
        base_stacks, sharing = self._supported_base_stacks()

        ec2_client = self.context.aws().ec2()
        described = describe_images_by_id(
            ec2_client, [stack.ami_id for stack, _ in base_stacks]
        )

        rows: List[ImageInventoryRow] = []
        for stack, (base_os, architecture) in sorted(
            base_stacks, key=lambda entry: (entry[1][0], entry[1][1])
        ):
            row = ImageInventoryRow(
                base_os=base_os,
                architecture=architecture,
                stack_id=stack.stack_id,
                image_id=stack.ami_id,
                base_ami_id=stack.base_ami_id,
                referenced_by=[stack.stack_id],
                state='none',
            )
            image = described.get(stack.ami_id) if stack.ami_id else None
            if Utils.is_not_empty(stack.ami_id):
                if image is None:
                    row.state = 'missing'
                    row.notes = 'the referenced image no longer exists in this account'
                else:
                    row.image_name = image.get('Name')
                    row.state = image_state(row.image_name, DESKTOP_IMAGE_PREFIX)
                    if row.state == 'built':
                        row.build_date = build_stamp(row.image_name)
                custom = sharing.get(stack.ami_id, 0)
                if custom:
                    row.referenced_by.append(
                        f'{custom} custom stack{"s" if custom > 1 else ""} on the same image'
                    )
            record = self.records.get(base_os, architecture)
            if record is not None:
                record = self.runner.refresh(record)
                row.last_build = record
                if record.status == BUILD_STATUS_BUILDING:
                    row.state = 'building'
                elif (
                    row.state == 'built'
                    and record.status == 'complete'
                    and record.image_id == stack.ami_id
                    and record.base_ami
                    and stack.base_ami_id
                    and record.base_ami != stack.base_ami_id
                ):
                    # the refresh moved the base past what this image was built from
                    row.state = 'built_outdated'
                    row.notes = f'built from {record.base_ami}; the base is now {stack.base_ami_id}'
            rows.append(row)
        return rows

    def use_built_images(
        self, stack_ids: Optional[List[str]], requested_by: Optional[str]
    ) -> List[DesktopImageBuildStartResult]:
        """
        point base stacks at their last completed build without rebuilding: the undo for
        a refresh or any other repoint. only the base stack row changes.
        """
        wanted = set(stack_ids) if stack_ids else None
        ec2_client = self.context.aws().ec2()
        results: List[DesktopImageBuildStartResult] = []
        for stack, (base_os, architecture) in sorted(
            self._supported_base_stacks()[0],
            key=lambda entry: (entry[1][0], entry[1][1]),
        ):
            if wanted is not None and stack.stack_id not in wanted:
                continue
            result = DesktopImageBuildStartResult(
                stack_id=stack.stack_id, base_os=base_os, architecture=architecture
            )
            results.append(result)
            record = self.records.get(base_os, architecture)
            if record is None or record.status != 'complete' or not record.image_id:
                result.status = 'skipped'
                result.message = 'no completed build for this stack'
                continue
            if stack.ami_id == record.image_id:
                result.status = 'skipped'
                result.message = f'already launches from {record.image_id}'
                continue
            image = describe_images_by_id(ec2_client, [record.image_id]).get(
                record.image_id
            )
            if image is None or image.get('State') not in (None, 'available'):
                result.status = 'error'
                result.message = f'built image {record.image_id} is not available'
                continue
            found = image.get('Architecture')
            if found and found != architecture:
                result.status = 'error'
                result.message = f'built image {record.image_id} is {found}, the stack is {architecture}'
                continue
            try:
                stack.ami_id = record.image_id
                stack.base_ami_id = record.base_ami or stack.base_ami_id
                updated = self._software_stack_db.update(stack)
                self._software_stack_utils.update_software_stack_entry_to_opensearch(
                    updated
                )
                result.status = 'updated'
                result.message = f'launches from {record.image_id}'
                self._logger.info(
                    f'{stack.stack_id} repointed at built image {record.image_id} by {requested_by}'
                )
            except Exception as e:
                self._logger.error(f'{stack.stack_id}: repoint failed: {e}')
                result.status = 'error'
                result.message = (
                    f'{e.__class__.__name__}: repoint failed, see the controller log'
                )
        if wanted is not None:
            for missing in sorted(wanted - {r.stack_id for r in results}):
                results.append(
                    DesktopImageBuildStartResult(
                        stack_id=missing,
                        status='error',
                        message='not a base software stack',
                    )
                )
        return results

    def build(
        self, request: BuildDesktopImageRequest, requested_by: Optional[str]
    ) -> ImageBuildRecord:
        base_os = request.base_os
        architecture = request.architecture or 'x86_64'
        if base_os not in BUILD_SUPPORTED_BASE_OS:
            raise exceptions.invalid_params(
                f'base_os must be one of: {", ".join(BUILD_SUPPORTED_BASE_OS)}'
            )
        unsupported = stock_unsupported_reason(
            base_os, self.context.aws().ec2().meta.region_name
        )
        if unsupported:
            raise exceptions.invalid_params(unsupported)
        stack_id = base_stack_id(base_os, architecture)
        stack = self._software_stack_db.get(stack_id=stack_id, base_os=base_os)
        if stack is None:
            raise exceptions.invalid_params(f'software stack {stack_id} does not exist')

        # the base the refresh keeps current wins over a fresh resolver lookup
        base_ami = (
            request.base_ami
            or stack.base_ami_id
            or self.default_base_ami(stack, base_os, architecture)
        )
        if Utils.is_empty(base_ami):
            raise exceptions.invalid_params(
                f'no stock {base_os} {architecture} image could be resolved; provide base_ami'
            )
        update_stack = Utils.get_as_bool(request.update_stack, True)
        check_builder_instance_type(request.instance_type, architecture)
        # the builder refuses a base_ami that is not ours or the vendor's
        builder = DcvHostImageBuilder(
            context=self.context,
            base_ami=base_ami,
            base_os=base_os,
            instance_type=request.instance_type,
            force=True,
        )
        record = new_record(
            base_os=base_os,
            architecture=architecture,
            ami_name=builder.get_ami_full_name(),
            base_ami=base_ami,
            requested_by=requested_by,
            update_target=update_stack,
        )
        on_success = (
            self._repoint(stack_id, base_os, architecture) if update_stack else None
        )
        return self.runner.start(record, build=builder.build, on_success=on_success)

    def build_all(
        self, requested_by: Optional[str]
    ) -> List[DesktopImageBuildStartResult]:
        """
        one build per base stack, each isolated exactly like a single build. rows with a
        build already running are skipped, so a repeated request starts nothing.
        """
        results: List[DesktopImageBuildStartResult] = []
        base_stacks, _ = self._supported_base_stacks()
        in_progress = sum(
            1
            for existing in self.records.list_all()
            if self.runner.refresh(existing).status == BUILD_STATUS_BUILDING
        )
        for stack, (base_os, architecture) in sorted(
            base_stacks, key=lambda entry: (entry[1][0], entry[1][1])
        ):
            result = DesktopImageBuildStartResult(
                stack_id=stack.stack_id, base_os=base_os, architecture=architecture
            )
            results.append(result)
            if in_progress >= MAX_CONCURRENT_BUILDS:
                result.status = 'skipped'
                result.message = (
                    f'{MAX_CONCURRENT_BUILDS} builds already in progress; '
                    f'start this one when some have finished'
                )
                self._note(
                    base_os, architecture, requested_by, 'skipped', result.message
                )
                continue
            try:
                record = self.build(
                    BuildDesktopImageRequest(
                        base_os=base_os, architecture=architecture, update_stack=True
                    ),
                    requested_by=requested_by,
                )
                result.status = 'started'
                result.message = record.ami_name
                in_progress += 1
            except exceptions.SocaException as e:
                if 'already running' in (e.message or ''):
                    result.status = 'skipped'
                    result.message = e.message
                    continue
                result.status = 'error'
                result.message = e.message
                self._note(base_os, architecture, requested_by, 'failed', e.message)
            except Exception as e:
                self._logger.error(f'{stack.stack_id}: build could not start: {e}')
                result.status = 'error'
                result.message = (
                    f'{e.__class__.__name__}: could not start, see the controller log'
                )
                self._note(
                    base_os, architecture, requested_by, 'failed', result.message
                )
        return results

    def _note(
        self,
        base_os: str,
        architecture: str,
        requested_by: Optional[str],
        status: str,
        reason: Optional[str],
    ):
        """a row that build all did not start still shows why in its Last build column"""
        existing = self.records.get(base_os, architecture)
        if existing is not None and existing.status == BUILD_STATUS_BUILDING:
            return
        self.records.put(
            ImageBuildRecord(
                base_os=base_os,
                architecture=architecture,
                status=status,
                error=reason,
                requested_by=requested_by,
                host=socket.gethostname(),
                started_on=datetime.now(tz=timezone.utc),
                finished_on=datetime.now(tz=timezone.utc),
            )
        )

    def default_base_ami(
        self, stack: VirtualDesktopSoftwareStack, base_os: str, architecture: str
    ) -> Optional[str]:
        """
        the newest stock image for the OS; failing that the stack's own AMI when it is
        still a stock image. a build never stacks on top of a previous build.
        """
        ec2_client = self.context.aws().ec2()
        stock = find_latest_stock_ami(ec2_client, base_os, architecture, self._logger)
        if Utils.is_not_empty(stock):
            return stock
        if Utils.is_empty(stack.ami_id):
            return None
        image = describe_images_by_id(ec2_client, [stack.ami_id]).get(stack.ami_id)
        if image and image_state(image.get('Name'), DESKTOP_IMAGE_PREFIX) == 'stock':
            return stack.ami_id
        return None

    def _repoint(self, stack_id: str, base_os: str, architecture: str):
        def on_success(image_id: str, record: ImageBuildRecord):
            stack = self._software_stack_db.get(stack_id=stack_id, base_os=base_os)
            if stack is None:
                raise exceptions.general_exception(f'{stack_id} no longer exists')
            image = describe_images_by_id(self.context.aws().ec2(), [image_id]).get(
                image_id
            )
            found = (image or {}).get('Architecture')
            if found and found != architecture:
                raise exceptions.general_exception(
                    f'image {image_id} is {found}, stack {stack_id} is {architecture}; stack not repointed'
                )
            stack.ami_id = image_id
            stack.base_ami_id = record.base_ami or stack.base_ami_id
            updated = self._software_stack_db.update(stack)
            self._software_stack_utils.update_software_stack_entry_to_opensearch(
                updated
            )
            self._logger.info(
                f'{stack_id} now points at {image_id} (built from {stack.base_ami_id}); '
                f'index update posted'
            )

        return on_success
