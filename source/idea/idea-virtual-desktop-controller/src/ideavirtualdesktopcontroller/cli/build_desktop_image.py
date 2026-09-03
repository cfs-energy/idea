"""
ideactl build-desktop-image: the cli face of the dcv host image builder. The builder lives
in app/software_stacks/dcv_host_image_builder.py so the controller can run it; this module
adds what only an operator at a terminal needs (the confirmation table, the prompt, the
raw-table stack repoint) and is the only place prettytable is imported.
"""

from ideasdk.utils import Utils
from ideadatamodel import (
    constants,
    ReIndexSoftwareStacksRequest,
    ReIndexSoftwareStacksResponse,
)
from ideasdk.aws.image_builds import ImageBuildRecordsDB, ImageBuildRunner, new_record
from ideavirtualdesktopcontroller.cli import build_cli_context
from ideavirtualdesktopcontroller.cli.software_stacks import update_software_stack_ami
from ideavirtualdesktopcontroller.app.software_stacks.dcv_host_image_builder import (  # noqa: F401  re-exported
    ARCHITECTURE_TO_STACK_KEY,
    BUILD_SUPPORTED_BASE_OS,
    DEFAULT_EBS_VOLUME_SIZE_GB,
    DEFAULT_INSTANCE_TYPE,
    DcvHostImageBuilder,
)
import click
from prettytable import PrettyTable
import os


def plan_table(rows) -> PrettyTable:
    table = PrettyTable(['Name', 'Value'])
    table.align = 'l'
    for name, value in rows:
        table.add_row([name, value])
    return table


def update_base_stack_row(context, builder, image_id: str) -> bool:
    """
    point the matching ss-base-<os>-<arch>-base stack row at the new image
    and rebuild the search index. returns True when the row was updated.
    """
    logger = context.logger('build-desktop-image')
    image = builder.get_image_by_id(image_id=image_id)
    architecture = Utils.get_value_as_string('Architecture', image)
    arch_key = ARCHITECTURE_TO_STACK_KEY.get(architecture)
    if arch_key is None:
        builder.report.warning(
            f'unknown image architecture: {architecture}. stack row not updated.'
        )
        return False
    stack_id = f'ss-base-{builder.base_os}-{arch_key}-base'
    cluster_name = context.config().get_string('cluster.cluster_name', required=True)
    table_name = f'{cluster_name}.{context.module_id()}.controller.software-stacks'
    table = context.aws().dynamodb_table().Table(table_name)
    existing = table.get_item(
        Key={'base_os': builder.base_os, 'stack_id': stack_id}
    ).get('Item')
    if existing is None:
        builder.report.warning(
            f'software stack {stack_id} not found in {table_name}. stack row not updated.'
        )
        return False
    if not update_software_stack_ami(
        table,
        stack_id,
        builder.base_os,
        image_id,
        logger,
        base_ami_id=getattr(builder, 'base_ami', None),
    ):
        return False
    builder.report.success(f'{stack_id} now points at {image_id}')
    try:
        _ = context.unix_socket_client.invoke_alt(
            namespace='VirtualDesktopAdmin.ReIndexSoftwareStacks',
            payload=ReIndexSoftwareStacksRequest(),
            result_as=ReIndexSoftwareStacksResponse,
        )
        builder.report.info('software stack index rebuilt')
    except Exception as e:
        builder.report.warning(
            f'stack row updated but reindex failed ({e}). '
            f'run: ideactl reindex-software-stacks --reset'
        )
    return True


def build_records_table(context) -> str:
    cluster_name = context.config().get_string('cluster.cluster_name', required=True)
    return f'{cluster_name}.{context.module_id()}.controller.image-builds'


@click.command(
    'build-desktop-image',
    context_settings=constants.CLICK_SETTINGS,
    short_help='Build an eVDI desktop image so desktops skip the long first-boot install',
)
@click.option('--ami-name', help='AMI Name. Default: idea-dcv-host-{baseos}')
@click.option('--ami-version', help='AMI Version. Default: MMDDYYYY-HHmmss')
@click.option(
    '--base-ami',
    required=True,
    help='AMI ID of the stock base image to build from',
)
@click.option(
    '--base-os',
    required=True,
    help=f'BaseOS of the AMI. Must be one of: [{", ".join(BUILD_SUPPORTED_BASE_OS)}]',
)
@click.option(
    '--instance-type',
    help='Instance Type. Specify a GPU instance type to install GPU drivers. Default: m7i.large',
)
@click.option('--instance-profile-arn', help='IAM Instance Profile ARN')
@click.option(
    '--security-group-ids',
    help='Security Group Ids. Provide multiple security group ids separated by comma (,)',
)
@click.option('--subnet-id', help='Subnet Id')
@click.option('--ssh-key-pair', help='SSH Key Pair name')
@click.option('--block-device-name', help='EBS block device name.')
@click.option('--ebs-volume-size', type=int, help='EBS volume size in GB')
@click.option(
    '--update-stack',
    is_flag=True,
    help='Point the matching ss-base-<os>-<arch>-base software stack at the new image and reindex',
)
@click.option(
    '--no-terminate',
    is_flag=True,
    help='Do not terminate the AMI builder instance. It will be stopped instead.',
)
@click.option(
    '--no-stop',
    is_flag=True,
    help='Do not stop the AMI builder instance. Applicable only with --no-terminate.',
)
@click.option(
    '--no-reboot',
    is_flag=True,
    help='Do not reboot the instance before snapshotting the volumes.',
)
@click.option(
    '--overwrite', is_flag=True, help='Overwrite existing bootstrap package if exists.'
)
@click.option('--force', is_flag=True, help='Skip all confirmation prompts.')
def build_desktop_image(
    no_stop: bool, no_terminate: bool, security_group_ids: str, **kwargs
):
    """
    build an eVDI desktop image

    \b
    performs below operations:
        * build the dcv host bootstrap package and upload it to the cluster S3 bucket
        * launch a temporary EC2 instance from the stock base AMI
        * install packages, system updates, DCV server, session manager agent and GPU drivers
        * snapshot the instance into an AMI named idea-dcv-host-<baseos>-v<version>
        * optionally point the matching base software stack at the new image (--update-stack)

    \b
    Desktops launched from the built image only run per-session configuration on
    first boot and typically reach READY in a few minutes instead of 15 or more.
    """
    context = build_cli_context()
    context.check_root_access()
    try:
        security_group_ids_list = []
        if Utils.is_not_empty(security_group_ids):
            for token in security_group_ids.split(','):
                token = token.strip()
                if token and token not in security_group_ids_list:
                    security_group_ids_list.append(token)
        builder = DcvHostImageBuilder(
            context=context,
            stop=not no_stop,
            terminate=not no_terminate,
            security_group_ids=security_group_ids_list,
            **kwargs,
        )
        if builder.get_image_by_name() is None and not builder.force:
            print(plan_table(builder.describe()))
            if not context.prompt(
                'Are you sure you want to proceed with DCV Host AMI creation with above parameters?'
            ):
                context.info('AMI Builder aborted.')
                return
        # the same record the Custom AMIs page reads, so a cli build shows up there too
        base_image = builder.get_image_by_id(builder.base_ami) or {}
        record = new_record(
            base_os=builder.base_os,
            architecture=base_image.get('Architecture', 'x86_64'),
            ami_name=builder.get_ami_full_name(),
            base_ami=builder.base_ami,
            requested_by=f'ideactl ({os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"})',
            update_target=builder.update_stack,
        )
        records = ImageBuildRecordsDB(
            context, build_records_table(context)
        ).initialize()
        record = ImageBuildRunner(
            context, records, context.logger('build-desktop-image')
        ).start(record, build=builder.build, blocking=True)
        if builder.update_stack and Utils.is_not_empty(record.image_id):
            update_base_stack_row(context, builder, record.image_id)
    except KeyboardInterrupt:
        context.error(
            'AMI builder aborted. You will need to manually terminate the '
            'EC2 instance launched by AMI builder.'
        )
