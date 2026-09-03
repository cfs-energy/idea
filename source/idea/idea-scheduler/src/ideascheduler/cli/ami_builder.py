#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
ideactl ami-builder. The builder itself lives in app/images/compute_node_ami_builder.py so
the scheduler can run it; this module adds the confirmation table and the prompt.
"""

from ideasdk.utils import Utils
from ideadatamodel import constants
from ideascheduler.cli import build_cli_context
from ideascheduler.app.images.compute_node_ami_builder import (  # noqa: F401  re-exported
    DEFAULT_EBS_VOLUME_SIZE_GB,
    DEFAULT_INSTANCE_TYPE,
    ComputeNodeAmiBuilder,
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


@click.group()
def ami_builder():
    """
    compute node ami builder
    """


@ami_builder.command(context_settings=constants.CLICK_SETTINGS)
@click.option('--ami-name', help='AMI Name. Default: idea-compute-node-{baseos}')
@click.option('--ami-version', help='AMI Version. Default: MMDDYYYY-HHmmss')
@click.option(
    '--base-ami',
    help='AMI ID of the base image using which the AMI Builder EC2 instance will be launched',
)
@click.option(
    '--base-os',
    help='BaseOS of the AMI. Must be one of: [amazonlinux2023, ubuntu2204, ubuntu2404, rhel8, rhel9, rhel10, rocky8, rocky9, rocky10]',
)
@click.option(
    '--instance-type',
    help='Instance Type. Specify a GPU instance type to install GPU Drivers. Default: c5.large',
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
    '--enable-driver',
    multiple=True,
    help='Specify applicable drivers to be installed in the custom AMI. Supported values: [efa, fsx_lustre]. '
    'Multiple drivers can be enabled using --enable-driver efa --enable-driver fsx_lustre',
)
@click.option(
    '--instance-id',
    help='Instance Id. Can be used for creating AMI from an existing EC2 instance.',
)
@click.option(
    '--no-terminate',
    is_flag=True,
    help='Specify if the AMI Builder EC2 Instance must be terminated or not. Instance will be stopped instead of terminating.',
)
@click.option(
    '--no-stop',
    is_flag=True,
    help='Specify if the AMI Builder EC2 Instance must be stopped or not. Applicable only if --no-terminate is provided.',
)
@click.option(
    '--no-reboot',
    is_flag=True,
    help='Specify that the EC2 Instance should not be rebooted prior to taking snapshot of attached volumes when creating the AMI.',
)
@click.option(
    '--overwrite', is_flag=True, help='Overwrite existing bootstrap package if exists.'
)
@click.option('--force', is_flag=True, help='Skip all confirmation prompts.')
def build(no_stop: bool, no_terminate: bool, security_group_ids: str, **kwargs):
    """
    build compute node AMI

    \b
    performs below operations:
        * build bootstrap package for ami builder EC2 instance
        * upload bootstrap package to Cluster S3 bucket
        * launch AMI builder EC2 instance
        * wait for all applicable packages to be installed on the ec2 instance
        * create AMI after all packages are installed
        * wait for AMI to be `ready`
        * print AMI details

    \b
    Note:
        User Data and Package customizations can be implemented in IDEA_APP_DEPLOY_DIR/scheduler/ami_builder
    """
    context = build_cli_context()

    context.check_root_access()

    try:
        security_group_ids_list = []
        if Utils.is_not_empty(security_group_ids):
            tokens = security_group_ids.split(',')
            for token in tokens:
                token = token.strip()
                if token in security_group_ids_list:
                    continue
                security_group_ids_list.append(token)

        builder = ComputeNodeAmiBuilder(
            context=context,
            stop=not no_stop,
            terminate=not no_terminate,
            security_group_ids=security_group_ids_list,
            **kwargs,
        )
        if builder.get_image_by_name() is None and not builder.force:
            print(plan_table(builder.describe()))
            if not context.prompt(
                'Are you sure you want to proceed with Compute Node AMI creation with above parameters?'
            ):
                context.info('AMI Builder aborted.')
                return
        # the same record the Custom AMIs page reads, so a cli build shows up there too
        from ideascheduler.app.images.compute_images import ComputeImageService

        ComputeImageService(context).run_build(
            builder,
            requested_by=f'ideactl ({os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"})',
            blocking=True,
        )
    except KeyboardInterrupt:
        context.error(
            'AMI builder aborted. You will need to manually terminate the '
            'EC2 instance launched by AMI builder.'
        )


@ami_builder.command(context_settings=constants.CLICK_SETTINGS)
@click.option('--ami-name', help='AMI Name. Default: idea-compute-node-{baseos}')
@click.option('--ami-version', help='AMI Version. Default: MMDDYYYY-HHmmss')
@click.option(
    '--base-ami',
    help='AMI ID of the base image using which the AMI Builder EC2 instance will be launched',
)
@click.option(
    '--base-os',
    help='BaseOS of the AMI. Must be one of: [amazonlinux2023, ubuntu2204, ubuntu2404, rhel8, rhel9, rhel10, rocky8, rocky9, rocky10]',
)
@click.option(
    '--instance-type',
    help='Instance Type. Specify a GPU instance type to install GPU Drivers. Default: c5.large',
)
@click.option('--block-device-name', help='EBS block device name.')
@click.option('--ebs-volume-size', type=int, help='EBS volume size in GB')
@click.option(
    '--enable-driver',
    multiple=True,
    help='Specify applicable drivers to be installed in the custom AMI. Supported values: [efa, fsx_lustre]. '
    'Multiple drivers can be enabled using --enable-driver efa --enable-driver fsx_lustre',
)
@click.option(
    '--overwrite', is_flag=True, help='Overwrite existing bootstrap package if exists.'
)
@click.option(
    '--upload-to-s3', is_flag=True, help='Upload bootstrap package to cluster S3 bucket'
)
def generate_bootstrap_package(upload_to_s3: bool, **kwargs):
    """
    generate bootstrap package

    this is an intermediate step, when `ami-builder build` command is invoked.
    the purpose of this command is to generate the bootstrap package, so that additional customizations can be implemented prior to creating custom ami

    the `ami-builder build` command can then be invoked with the same ami name and ami version as that of bootstrap package, to re-use the customized bootstrap package.
    """
    context = build_cli_context()
    context.check_root_access()
    builder = ComputeNodeAmiBuilder(context=context, **kwargs)
    builder.build_userdata(upload_to_s3=upload_to_s3)

    ami_dir = builder.get_ami_dir()
    bootstrap_tmp_dir = os.path.join(
        ami_dir, 'bootstrap', f'ami-builder-{builder.get_ami_full_name()}'
    )
    context.info('bootstrap package location:')
    print(bootstrap_tmp_dir)
