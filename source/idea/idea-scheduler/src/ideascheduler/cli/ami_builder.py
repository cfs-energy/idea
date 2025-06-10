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

from ideasdk.utils import EnvironmentUtils, Utils
from ideadatamodel import exceptions, constants, EC2Instance
from ideasdk.context import BootstrapContext, SocaCliContext
from ideasdk.bootstrap import (
    BootstrapUserDataBuilder,
    BootstrapPackageBuilder,
    BootstrapUtils,
)
from ideasdk.metrics import CloudWatchAgentLogFileOptions

from ideascheduler.cli import build_cli_context

from typing import List, Optional, Dict, Tuple
import time
import os.path
import click
import arrow
from pathlib import Path
from prettytable import PrettyTable
import os

DEFAULT_INSTANCE_TYPE = 'c5.large'
DEFAULT_EBS_VOLUME_SIZE_GB = 10


class ComputeNodeAmiBuilder:
    """
    Compute Node AMI Builder
    Automate the process of building custom images for IDEA Compute Nodes.

    User Data and Package customizations can be implemented in IDEA_APP_DEPLOY_DIR/scheduler/resources/bootstrap/compute-node-ami-builder/ jinja2 files.
    """

    def __init__(
        self,
        context: SocaCliContext,
        ami_name: str = None,
        ami_version: str = None,
        base_ami: str = None,
        base_os: str = None,
        instance_type: str = None,
        instance_profile_arn: str = None,
        security_group_ids: List[str] = None,
        subnet_id: str = None,
        ssh_key_pair: str = None,
        block_device_name: str = None,
        ebs_volume_size: int = None,
        enable_driver: Tuple[str] = (),
        instance_id: str = None,
        stop: bool = True,
        terminate: bool = True,
        force: bool = False,
        no_reboot: bool = False,
        overwrite: bool = False,
    ):
        self.context = context

        # base ami and base os
        if Utils.are_empty(base_ami, base_os):
            # default to compute node ami and os from cluster config
            base_ami = context.config().get_string(
                'scheduler.compute_node_ami', required=True
            )
            base_os = context.config().get_string(
                'scheduler.compute_node_os', required=True
            )
        else:
            if Utils.is_empty(base_ami):
                raise exceptions.invalid_params(
                    'base_ami is required when base_os is provided'
                )
            if Utils.is_empty(base_os):
                raise exceptions.invalid_params(
                    'base_os is required when base_ami is provided'
                )

        # AMI name and version
        # the final AMI name will be of the format: {ami_name}-v{ami_version}
        if Utils.is_empty(ami_name):
            # do not associate name with any specific cluster as generated ami is cluster agnostic
            ami_name = f'idea-compute-node-{base_os}'
        if Utils.is_empty(ami_version):
            # noinspection StrFormat
            ami_version = arrow.get().format('MMDDYYYY-HHmmss')

        # instance type
        if Utils.is_empty(instance_type):
            instance_type = DEFAULT_INSTANCE_TYPE

        # instance profile arn
        if Utils.is_empty(instance_profile_arn):
            instance_profile_arn = context.config().get_string(
                'scheduler.compute_node_instance_profile_arn', required=True
            )
            if not instance_profile_arn.startswith('arn:'):
                instance_profile_info = context.aws_util().get_instance_profile_arn(
                    instance_profile_name=instance_profile_arn
                )
                instance_profile_arn = instance_profile_info['arn']

        # security group ids
        compute_node_security_group_ids = context.config().get_list(
            'scheduler.compute_node_security_group_ids', required=True
        )
        if Utils.is_empty(security_group_ids):
            security_group_ids = compute_node_security_group_ids
        else:
            for security_group_id in compute_node_security_group_ids:
                if security_group_id in security_group_ids:
                    continue
                security_group_ids.append(security_group_id)

        # subnet id
        if Utils.is_empty(subnet_id):
            cluster_subnet_ids = context.config().get_list(
                'cluster.network.private_subnets', required=True
            )
            subnet_id = cluster_subnet_ids[0]

        # ssh key pair
        if Utils.is_empty(ssh_key_pair):
            ssh_key_pair = context.config().get_string(
                'cluster.network.ssh_key_pair', required=True
            )

        # ebs volume: block device name and size in GB
        image = self.get_image_by_id(image_id=base_ami)
        ami_block_device_mappings = image['BlockDeviceMappings']
        ami_block_device = ami_block_device_mappings[0]
        ami_block_device_name = ami_block_device['DeviceName']
        ami_block_ebs = ami_block_device['Ebs']
        ami_ebs_volume_size_gb = ami_block_ebs['VolumeSize']

        if Utils.is_empty(block_device_name):
            block_device_name = ami_block_device_name
        if Utils.is_empty(ebs_volume_size):
            ebs_volume_size = max(DEFAULT_EBS_VOLUME_SIZE_GB, ami_ebs_volume_size_gb)
        else:
            if ebs_volume_size < ami_ebs_volume_size_gb:
                raise exceptions.invalid_params(
                    f'ebs volume size must be greater or equal to base ami ebs volume size: {ami_ebs_volume_size_gb}gb'
                )

        # stop/terminate
        if terminate:
            stop = False

        self.base_ami = base_ami
        self.ami_name = ami_name
        self.ami_version = ami_version
        self.base_os = base_os
        self.instance_type = instance_type
        self.instance_profile_arn = instance_profile_arn
        self.security_group_ids = security_group_ids
        self.subnet_id = subnet_id
        self.ssh_key_pair = ssh_key_pair
        self.block_device_name = block_device_name
        self.ebs_volume_size = ebs_volume_size
        self.enable_driver = enable_driver
        self.instance_id = instance_id
        self.force = force
        self.stop = stop
        self.terminate = terminate
        self.no_reboot = no_reboot
        self.overwrite = overwrite

    def get_ami_full_name(self) -> str:
        return f'{self.ami_name}-v{self.ami_version}'

    def get_scheduler_dir(self) -> str:
        # this will be /apps/<cluster-name>/<module-id>
        cluster_home_dir = self.context.config().get_string(
            'cluster.home_dir', required=True
        )
        return os.path.join(cluster_home_dir, self.context.module_id())

    def get_ami_builder_dir(self):
        # this will be /apps/<cluster-name>/<module-id>/ami_builder
        return os.path.join(self.get_scheduler_dir(), 'ami_builder')

    def get_ami_dir(self):
        # this will be /apps/<cluster-name>/<module-id>/ami_builder/<ami_name>/<ami_version>
        return os.path.join(self.get_ami_builder_dir(), self.ami_name, self.ami_version)

    @staticmethod
    def get_resources_dir() -> str:
        if Utils.is_true(EnvironmentUtils.idea_dev_mode(), False):
            script_dir = Path(os.path.abspath(__file__))
            scheduler_project_dir = script_dir.parent.parent.parent.parent
            return os.path.join(scheduler_project_dir, 'resources')
        else:
            return os.path.join(
                EnvironmentUtils.idea_app_deploy_dir(required=True),
                'scheduler',
                'resources',
            )

    def get_bootstrap_dir(self) -> str:
        if Utils.is_true(EnvironmentUtils.idea_dev_mode(), False):
            script_dir = Path(os.path.abspath(__file__))
            idea_source_dir = script_dir.parent.parent.parent.parent.parent
            return os.path.join(idea_source_dir, 'idea-bootstrap')
        else:
            return os.path.join(self.get_resources_dir(), 'bootstrap')

    def build_userdata(self, upload_to_s3: bool = True) -> str:
        bootstrap_context = BootstrapContext(
            config=self.context.config(),
            module_name=self.context.module_name(),
            module_id=self.context.module_id(),
            module_set=self.context.module_set(),
            base_os=self.base_os,
            instance_type=self.instance_type,
        )

        ami_dir = self.get_ami_dir()
        bootstrap_tmp_dir = os.path.join(ami_dir, 'bootstrap')
        cluster_s3_bucket = self.context.config().get_string(
            'cluster.cluster_s3_bucket', required=True
        )

        bootstrap_context.vars.ami_dir = self.get_ami_dir()
        bootstrap_context.vars.ami_name = self.get_ami_full_name()

        # optional packages / driver flags
        bootstrap_context.vars.enabled_drivers = self.enable_driver

        cloudwatch_log_group_name = (
            f'/{self.context.cluster_name()}/{self.context.module_id()}/ami-builder'
        )

        BootstrapUtils.check_and_attach_cloudwatch_logging_and_metrics(
            bootstrap_context=bootstrap_context,
            metrics_namespace=f'{self.context.cluster_name()}/{self.context.module_id()}/ami-builder',  # not used
            node_type=constants.NODE_TYPE_AMI_BUILDER,
            enable_logging=True,
            log_files=[
                CloudWatchAgentLogFileOptions(
                    file_path='/root/bootstrap/logs/**.log',
                    log_group_name=cloudwatch_log_group_name,
                    log_stream_name='bootstrap_{ip_address}',
                ),
                CloudWatchAgentLogFileOptions(
                    file_path=f'{ami_dir}/logs/**.log',
                    log_group_name=cloudwatch_log_group_name,
                    log_stream_name='bootstrap_{ip_address}',
                ),
            ],
            enable_metrics=False,
        )

        bootstrap_source_dir = self.get_bootstrap_dir()
        bootstrap_package_archive_file = BootstrapPackageBuilder(
            bootstrap_context=bootstrap_context,
            source_directory=bootstrap_source_dir,
            target_package_basename=f'ami-builder-{self.get_ami_full_name()}',
            components=['compute-node-ami-builder'],
            tmp_dir=bootstrap_tmp_dir,
            force_build=self.overwrite,
        ).build()
        self.context.info('built bootstrap package archive:')
        print(bootstrap_package_archive_file)

        bootstrap_package_key = f'idea/{self.context.module_id()}/bootstrap/{os.path.basename(bootstrap_package_archive_file)}'
        bootstrap_package_uri = f's3://{cluster_s3_bucket}/{bootstrap_package_key}'
        if upload_to_s3:
            self.context.info(f'uploading bootstrap package: {bootstrap_package_uri}')
            self.context.aws().s3().upload_file(
                Bucket=cluster_s3_bucket,
                Filename=bootstrap_package_archive_file,
                Key=bootstrap_package_key,
            )

        https_proxy = self.context.config().get_string(
            'cluster.network.https_proxy', required=False, default=''
        )
        no_proxy = self.context.config().get_string(
            'cluster.network.no_proxy', required=False, default=''
        )
        proxy_config = {}
        if Utils.is_not_empty(https_proxy):
            proxy_config = {
                'http_proxy': https_proxy,
                'https_proxy': https_proxy,
                'no_proxy': no_proxy,
            }

        return BootstrapUserDataBuilder(
            aws_region=self.context.aws().aws_region(),
            bootstrap_package_uri=bootstrap_package_uri,
            install_commands=['/bin/bash compute-node-ami-builder/setup.sh'],
            proxy_config=proxy_config,
            base_os=self.base_os,
            substitution_support=False,
        ).build()

    def launch_ec2_instance(self) -> EC2Instance:
        """
        launch a temporary ec2 instance to install applicable packages and build the ami
        :return: EC2Instance
        """

        custom_tags = self.context.config().get_list('global-settings.custom_tags', [])
        custom_tags.append(
            f'Key=Name,Value={self.context.cluster_name()}-{self.get_ami_full_name()}'
        )
        custom_tags.append(
            f'Key={constants.IDEA_TAG_CLUSTER_NAME},Value={self.context.cluster_name()}'
        )
        custom_tags.append(
            f'Key={constants.IDEA_TAG_MODULE_NAME},Value={self.context.module_name()}'
        )
        custom_tags.append(
            f'Key={constants.IDEA_TAG_MODULE_ID},Value={self.context.module_id()}'
        )
        custom_tags.append(
            f'Key={constants.IDEA_TAG_NODE_TYPE},Value={constants.NODE_TYPE_AMI_BUILDER}'
        )
        custom_tags_dict = Utils.convert_custom_tags_to_key_value_pairs(custom_tags)
        tags = []
        for key, value in custom_tags_dict.items():
            tags.append({'Key': key, 'Value': value})

        run_instance_request = {
            'ImageId': self.base_ami,
            'InstanceType': self.instance_type,
            'BlockDeviceMappings': [
                {
                    'DeviceName': self.block_device_name,
                    'Ebs': {'VolumeSize': self.ebs_volume_size},
                }
            ],
            'IamInstanceProfile': {'Arn': self.instance_profile_arn},
            'KeyName': self.ssh_key_pair,
            'NetworkInterfaces': [
                {
                    'DeviceIndex': 0,
                    'Groups': self.security_group_ids,
                    'SubnetId': self.subnet_id,
                }
            ],
            'MaxCount': 1,
            'MinCount': 1,
            'TagSpecifications': [{'ResourceType': 'instance', 'Tags': tags}],
            'UserData': self.build_userdata(),
        }

        run_instances_result = (
            self.context.aws().ec2().run_instances(**run_instance_request)
        )
        created_instances = Utils.get_value_as_list('Instances', run_instances_result)
        return EC2Instance(data=Utils.get_first(created_instances))

    def wait_for_software_packages(self, instance_id: str):
        with self.context.spinner('installing software packages ...'):
            while True:
                describe_instances_result = (
                    self.context.aws()
                    .ec2()
                    .describe_instances(InstanceIds=[instance_id])
                )
                reservations = Utils.get_value_as_list(
                    'Reservations', describe_instances_result
                )
                instances = Utils.get_value_as_list('Instances', reservations[0])
                instance = EC2Instance(data=instances[0])
                ami_builder_status = instance.get_tag('idea:AmiBuilderStatus')
                if Utils.is_not_empty(ami_builder_status):
                    break

                time.sleep(10)

    def create_image(self, instance_id: str) -> str:
        block_device_name = Utils.get_ec2_block_device_name(self.base_os)

        custom_tags = self.context.config().get_list('global-settings.custom_tags', [])
        custom_tags.append(f'Key=Name,Value={self.get_ami_full_name()}')
        custom_tags.append(
            f'Key={constants.IDEA_TAG_MODULE_NAME},Value={self.context.module_name()}'
        )
        custom_tags.append(f'Key={constants.IDEA_TAG_AMI_BUILDER},Value=true')
        custom_tags_dict = Utils.convert_custom_tags_to_key_value_pairs(custom_tags)
        tags = []
        for key, value in custom_tags_dict.items():
            tags.append({'Key': key, 'Value': value})

        create_image_result = (
            self.context.aws()
            .ec2()
            .create_image(
                BlockDeviceMappings=[
                    {
                        'DeviceName': block_device_name,
                        'Ebs': {'VolumeSize': self.ebs_volume_size},
                    }
                ],
                Description=f'IDEA Compute Node AMI: {self.ami_name}, Version: {self.ami_version}',
                Name=self.get_ami_full_name(),
                InstanceId=instance_id,
                TagSpecifications=[
                    {'ResourceType': 'image', 'Tags': tags},
                    {'ResourceType': 'snapshot', 'Tags': tags},
                ],
                NoReboot=self.no_reboot,
            )
        )

        image_id = Utils.get_value_as_string('ImageId', create_image_result)
        return image_id

    def get_image_by_id(self, image_id: str) -> Optional[Dict]:
        result = self.context.aws().ec2().describe_images(ImageIds=[image_id])
        images = Utils.get_value_as_list('Images', result, [])
        if Utils.is_empty(images):
            return None
        return images[0]

    def get_image_by_name(self) -> Optional[Dict]:
        result = (
            self.context.aws()
            .ec2()
            .describe_images(
                Filters=[{'Name': 'name', 'Values': [self.get_ami_full_name()]}]
            )
        )
        images = Utils.get_value_as_list('Images', result, [])
        if Utils.is_empty(images):
            return None
        return images[0]

    def wait_for_image(self, image_id: str):
        while True:
            describe_image_result = (
                self.context.aws().ec2().describe_images(ImageIds=[image_id])
            )
            images = Utils.get_value_as_list('Images', describe_image_result)
            if len(images) == 0:
                self.context.error(
                    f'Something went wrong. Could not find any images for ImageId: {image_id}'
                )
                break

            image = Utils.get_first(images)
            state = Utils.get_value_as_string('State', image)
            if state != 'pending':
                break

            time.sleep(10)

    def terminate_ec2_instance(self, instance_id: str):
        self.context.aws().ec2().terminate_instances(InstanceIds=[instance_id])

    def stop_ec2_instance(self, instance_id: str):
        self.context.aws().ec2().stop_instances(InstanceIds=[instance_id])

    def build(self):
        # check if image already exists for the given name
        existing_image = self.get_image_by_name()
        instance_id = None

        self.context.info(f'building compute node AMI: {self.get_ami_full_name()} ...')

        if existing_image is None:
            table = PrettyTable(['Name', 'Value'])
            table.align = 'l'
            table.add_row(
                [
                    'AMI Name',
                    f'{self.get_ami_full_name()}{os.linesep}- Name: {self.ami_name}{os.linesep}- Version: {self.ami_version}',
                ]
            )
            if Utils.is_empty(self.instance_id):
                table.add_row(['Base AMI', self.base_ami])
                table.add_row(['Base OS', self.base_os])
                table.add_row(['Instance Type', self.instance_type])
                table.add_row(['Instance Profile ARN', self.instance_profile_arn])
                table.add_row(
                    [
                        'Security Group Ids',
                        Utils.to_yaml(self.security_group_ids).strip(),
                    ]
                )
                table.add_row(['Subnet ID', self.subnet_id])
                table.add_row(['SSH Key Pair', self.ssh_key_pair])
                if len(self.enable_driver) > 0:
                    enabled_drivers = Utils.to_yaml(list(self.enable_driver)).strip()
                else:
                    enabled_drivers = '-'
                table.add_row(['Enabled Drivers', enabled_drivers])
            else:
                table.add_row(['Existing Instance ID', self.instance_id])
            table.add_row(['Reboot during ec2:CreateImage?', not self.no_reboot])
            table.add_row(['Terminate AMI Builder Instance?', self.terminate])
            if not self.terminate:
                table.add_row(['Stop AMI Builder Instance?', self.stop])
            table.add_row(['Block Device Name', self.block_device_name])
            table.add_row(['EBS Volume Size (GB)', self.ebs_volume_size])

            print(table)
            if not self.force:
                confirm = self.context.prompt(
                    'Are you sure you want to proceed with Compute Node AMI creation with above parameters?'
                )
                if not confirm:
                    self.context.info('AMI Builder aborted.')
                    raise SystemExit(0)

            if Utils.is_empty(self.instance_id):
                ec2_instance = self.launch_ec2_instance()
                instance_id = ec2_instance.instance_id
                self.context.success(
                    f'EC2 instance launched for building AMI. InstanceId: {instance_id}'
                )
                bootstrap_logs = os.path.join(
                    self.get_ami_dir(),
                    'logs',
                    Utils.ipv4_to_ec2_hostname(ec2_instance.private_ip_address),
                    'compute_node_ami_builder_bootstrap.log',
                )
                self.context.info(
                    'Bootstrap logs will be available at the below location shortly:'
                )
                self.context.print(bootstrap_logs)
                time.sleep(3)
                self.wait_for_software_packages(instance_id=ec2_instance.instance_id)
            else:
                instance_id = self.instance_id

            image_id = self.create_image(instance_id=instance_id)

            time.sleep(3)

        else:
            self.context.info(
                f'found an existing image for name: {self.get_ami_full_name()}'
            )
            image_id = existing_image['ImageId']

        with self.context.spinner(
            f'AMI creation initiated, ImageId: {image_id}. waiting for AMI to be ready ...'
        ):
            self.wait_for_image(image_id=image_id)

        self.context.success('AMI created successfully:')
        self.context.success(f'- Name: {self.ami_name}')
        self.context.success(f'- ImageId: {image_id}')

        if Utils.is_not_empty(instance_id):
            if self.stop:
                self.stop_ec2_instance(instance_id=instance_id)
                self.context.info(f'ec2 instance: {instance_id} stopped.')
            elif self.terminate:
                self.terminate_ec2_instance(instance_id=instance_id)
                self.context.info(f'ec2 instance: {instance_id} terminated.')
            else:
                self.context.warning(
                    f'AMI builder ec2 instance: {instance_id} needs to be manually terminated.'
                )


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
    help='BaseOS of the AMI. Must be one of: [amazonlinux2, amazonlinux2023, ubuntu2204, ubuntu2404, rhel8, rhel9, rocky8, rocky9]',
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
        builder.build()
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
    help='BaseOS of the AMI. Must be one of: [amazonlinux2, amazonlinux2023, ubuntu2204, ubuntu2404, rhel8, rhel9, rocky8, rocky9]',
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
