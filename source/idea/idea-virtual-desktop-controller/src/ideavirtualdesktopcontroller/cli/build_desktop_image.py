"""
ideactl build-desktop-image: the cli face of the dcv host image builder. The builder lives
in app/software_stacks/dcv_host_image_builder.py so the controller can run it; this module
adds what only an operator at a terminal needs (the confirmation table, the prompt, the
raw-table stack repoint) and is the only place prettytable is imported.
"""

from ideasdk.utils import Utils
from ideadatamodel import constants
from ideadatamodel import ImageKind
from ideasdk.aws.image_builds import (
    ImageBuildRecordsDB,
    ImageBuildRunner,
    custom_build_architecture,
    new_record,
)
from ideavirtualdesktopcontroller.cli import build_cli_context
from ideavirtualdesktopcontroller.app.software_stacks.dcv_host_image_builder import (  # noqa: F401  re-exported
    ARCHITECTURE_TO_STACK_KEY,
    BUILD_SUPPORTED_BASE_OS,
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
    help='Refused since 26.10.1: base stacks only move to validated images (Refresh and validate on the Images page)',
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
        * record it as a custom build; base stacks move only to images the image
          pipeline validated, so --update-stack is refused

    \b
    Desktops launched from the built image only run per-session configuration on
    first boot and typically reach READY in a few minutes instead of 15 or more.
    """
    context = build_cli_context()
    context.check_root_access()
    if kwargs.get('update_stack'):
        context.error(
            '--update-stack is refused: a built image repoints base stacks only after it '
            'passes validation. use Refresh and validate on the Images page'
        )
        raise SystemExit(1)
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
            architecture=custom_build_architecture(
                base_image.get('Architecture', 'x86_64')
            ),
            ami_name=builder.get_ami_full_name(),
            base_ami=builder.base_ami,
            requested_by=f'ideactl ({os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"})',
            update_target=builder.update_stack,
        )
        records = ImageBuildRecordsDB(
            context, build_records_table(context), kind=ImageKind.DESKTOP
        ).initialize()
        record = ImageBuildRunner(
            context, records, context.logger('build-desktop-image')
        ).start(record, build=builder.build, blocking=True)
    except KeyboardInterrupt:
        context.error(
            'AMI builder aborted. You will need to manually terminate the '
            'EC2 instance launched by AMI builder.'
        )
