"""
Compute node AMI builder.

Lives under app/ because the scheduler runs it for SchedulerAdmin.BuildComputeImage;
ideactl ami-builder wraps it with the confirmation table and the prompt. Nothing here may
import cli modules or cli-only packages (prettytable, click).
"""

from ideasdk.utils import EnvironmentUtils, Utils
from ideadatamodel import exceptions, constants, EC2Instance
from ideasdk.context import BootstrapContext
from ideasdk.bootstrap import (
    BootstrapUserDataBuilder,
    BootstrapPackageBuilder,
    BootstrapUtils,
)
from ideasdk.metrics import CloudWatchAgentLogFileOptions
from ideasdk.aws.image_builds import (
    BUILDER_READY_TIMEOUT_SECONDS,
    IMAGE_BUILD_TAG,
    BuildReporter,
    check_builder_type_architecture,
    default_builder_instance_type,
    is_throttle,
    stop_builder,
    unique_build_version,
)
from ideasdk.aws.stock_amis import trusted_owners

from typing import List, Optional, Dict, Tuple
import time
import os.path
from pathlib import Path
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
        context,
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
        progress=None,
    ):
        self.context = context
        # called with {"instance_id": ...} once the builder instance exists
        self.progress = progress

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
            ami_version = unique_build_version()

        # instance type

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

        # ebs volume: block device name and size in GB. the base image must be owned by
        # this account or the OS vendor; anything else is refused here
        image = self.get_image_by_id(
            image_id=base_ami,
            owners=trusted_owners(base_os, context.aws().ec2().meta.region_name),
        )
        if image is None:
            raise exceptions.invalid_params(
                f'base_ami {base_ami} is not an image owned by this account or by the {base_os} vendor'
            )
        self.architecture = image.get('Architecture', 'x86_64')
        # RunInstances refuses a builder type of the wrong architecture, so the default
        # follows the image and an explicit type is checked first
        if Utils.is_empty(instance_type):
            instance_type = default_builder_instance_type(
                self.architecture, DEFAULT_INSTANCE_TYPE
            )
        else:
            check_builder_type_architecture(instance_type, self.architecture)
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
        # honored by the cli command (confirmation prompt); the app always builds unattended
        self.force = force
        self.stop = stop
        self.terminate = terminate
        self.no_reboot = no_reboot
        self.overwrite = overwrite

    @property
    def report(self) -> BuildReporter:
        # prints under ideactl, logs inside the module app
        report = self.__dict__.get('_report')
        if report is None:
            report = self.__dict__['_report'] = BuildReporter(
                self.context, 'ami-builder'
            )
        return report

    def get_ami_full_name(self) -> str:
        return f'{self.ami_name}-v{self.ami_version}'

    def describe(self):
        """(name, value) rows describing the build, for the cli confirmation table"""
        rows = [
            (
                'AMI Name',
                f'{self.get_ami_full_name()}{os.linesep}- Name: {self.ami_name}{os.linesep}- Version: {self.ami_version}',
            )
        ]
        if Utils.is_empty(self.instance_id):
            rows.extend(
                [
                    ('Base AMI', self.base_ami),
                    ('Base OS', self.base_os),
                    ('Instance Type', self.instance_type),
                    ('Instance Profile ARN', self.instance_profile_arn),
                    (
                        'Security Group Ids',
                        Utils.to_yaml(self.security_group_ids).strip(),
                    ),
                    ('Subnet ID', self.subnet_id),
                    ('SSH Key Pair', self.ssh_key_pair),
                    (
                        'Enabled Drivers',
                        Utils.to_yaml(list(self.enable_driver)).strip()
                        if len(self.enable_driver) > 0
                        else '-',
                    ),
                ]
            )
        else:
            rows.append(('Existing Instance ID', self.instance_id))
        rows.extend(
            [
                ('Reboot during ec2:CreateImage?', not self.no_reboot),
                ('Terminate AMI Builder Instance?', self.terminate),
            ]
        )
        if not self.terminate:
            rows.append(('Stop AMI Builder Instance?', self.stop))
        rows.extend(
            [
                ('Block Device Name', self.block_device_name),
                ('EBS Volume Size (GB)', self.ebs_volume_size),
            ]
        )
        return rows

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
            scheduler_project_dir = script_dir.parent.parent.parent.parent.parent
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
            idea_source_dir = script_dir.parent.parent.parent.parent.parent.parent
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
        self.report.info('built bootstrap package archive:')
        print(bootstrap_package_archive_file)

        bootstrap_package_key = f'idea/{self.context.module_id()}/bootstrap/{os.path.basename(bootstrap_package_archive_file)}'
        bootstrap_package_uri = f's3://{cluster_s3_bucket}/{bootstrap_package_key}'
        if upload_to_s3:
            self.report.info(f'uploading bootstrap package: {bootstrap_package_uri}')
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
        custom_tags.append(
            f'Key={IMAGE_BUILD_TAG},Value={self.base_os}/{self.architecture}'
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
        """
        poll until the bootstrap tags the builder idea:AmiBuilderStatus, or give up after
        BUILDER_READY_TIMEOUT_SECONDS. a failed bootstrap never sets the tag, and the raise
        routes through keep_for_inspection, so the instance is stopped rather than billed on.
        """
        deadline = time.time() + BUILDER_READY_TIMEOUT_SECONDS
        with self.report.spinner('installing software packages ...'):
            while True:
                try:
                    describe_instances_result = (
                        self.context.aws()
                        .ec2()
                        .describe_instances(InstanceIds=[instance_id])
                    )
                except Exception as e:
                    # a throttled poll is not a failed build; the deadline still applies
                    if not is_throttle(e):
                        raise
                    describe_instances_result = {}
                reservations = Utils.get_value_as_list(
                    'Reservations', describe_instances_result, []
                )
                instances = (
                    Utils.get_value_as_list('Instances', reservations[0], [])
                    if reservations
                    else []
                )
                ami_builder_status = (
                    EC2Instance(data=instances[0]).get_tag('idea:AmiBuilderStatus')
                    if instances
                    else None
                )
                if Utils.is_not_empty(ami_builder_status):
                    break
                if time.time() > deadline:
                    raise exceptions.general_exception(
                        f'builder {instance_id} did not report ready within '
                        f'{BUILDER_READY_TIMEOUT_SECONDS // 60} minutes'
                    )
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

    def get_image_by_id(
        self, image_id: str, owners: Optional[List[str]] = None
    ) -> Optional[Dict]:
        """the image, or None when it does not exist or is not owned by one of `owners`"""
        request: Dict = {'ImageIds': [image_id]}
        if owners:
            request['Owners'] = owners
        try:
            result = self.context.aws().ec2().describe_images(**request)
        except Exception:
            # an unknown id raises InvalidAMIID.NotFound; that is 'None' to every caller
            return None
        images = Utils.get_value_as_list('Images', result, [])
        if Utils.is_empty(images):
            return None
        return images[0]

    def get_image_by_name(self) -> Optional[Dict]:
        result = (
            self.context.aws()
            .ec2()
            .describe_images(
                Owners=['self'],
                Filters=[
                    {'Name': 'name', 'Values': [self.get_ami_full_name()]},
                    {'Name': 'state', 'Values': ['available']},
                ],
            )
        )
        images = Utils.get_value_as_list('Images', result, [])
        if Utils.is_empty(images):
            return None
        return images[0]

    def wait_for_image(self, image_id: str):
        """
        block until the image is available. every other terminal state raises, so a dead
        AMI is never promoted, and an image that never appears hits the builder deadline.
        """
        deadline = time.time() + BUILDER_READY_TIMEOUT_SECONDS
        while True:
            try:
                describe_image_result = (
                    self.context.aws().ec2().describe_images(ImageIds=[image_id])
                )
            except Exception as e:
                if not is_throttle(e):
                    raise
                describe_image_result = {}
            images = Utils.get_value_as_list('Images', describe_image_result, [])
            if images:
                state = Utils.get_value_as_string('State', Utils.get_first(images))
                if state == 'available':
                    return
                if state != 'pending':
                    raise exceptions.general_exception(
                        f'image {image_id} ended in state {state}'
                    )
            if time.time() > deadline:
                raise exceptions.general_exception(
                    f'image {image_id} was not available within {BUILDER_READY_TIMEOUT_SECONDS // 60} minutes'
                )
            time.sleep(10)

    def terminate_ec2_instance(self, instance_id: str):
        self.context.aws().ec2().terminate_instances(InstanceIds=[instance_id])

    def stop_ec2_instance(self, instance_id: str):
        self.context.aws().ec2().stop_instances(InstanceIds=[instance_id])

    def build(self, progress=None) -> str:
        """build the image and return its id; the builder instance is stopped, not terminated, when a step fails"""
        progress = progress or self.progress
        # check if image already exists for the given name
        existing_image = self.get_image_by_name()
        instance_id = None

        self.report.info(f'building compute node AMI: {self.get_ami_full_name()} ...')

        if existing_image is None:
            if Utils.is_empty(self.instance_id):
                ec2_instance = self.launch_ec2_instance()
                instance_id = ec2_instance.instance_id
                self.report.success(
                    f'EC2 instance launched for building AMI. InstanceId: {instance_id}'
                )
                bootstrap_logs = os.path.join(
                    self.get_ami_dir(),
                    'logs',
                    Utils.ipv4_to_ec2_hostname(ec2_instance.private_ip_address),
                    'compute_node_ami_builder_bootstrap.log',
                )
                self.report.info(
                    'Bootstrap logs will be available at the below location shortly:'
                )
                self.report.print(bootstrap_logs)
                time.sleep(3)
                if progress is not None:
                    progress({'instance_id': instance_id})
            else:
                instance_id = self.instance_id

            try:
                if Utils.is_empty(self.instance_id):
                    self.wait_for_software_packages(instance_id=instance_id)
                image_id = self.create_image(instance_id=instance_id)
            except BaseException as e:
                self.keep_for_inspection(instance_id, e)
                raise

            time.sleep(3)

        else:
            self.report.info(
                f'found an existing image for name: {self.get_ami_full_name()}'
            )
            image_id = existing_image['ImageId']

        try:
            with self.report.spinner(
                f'AMI creation initiated, ImageId: {image_id}. waiting for AMI to be ready ...'
            ):
                self.wait_for_image(image_id=image_id)
        except BaseException as e:
            if Utils.is_not_empty(instance_id):
                self.keep_for_inspection(instance_id, e)
            raise

        self.report.success('AMI created successfully:')
        self.report.success(f'- Name: {self.ami_name}')
        self.report.success(f'- ImageId: {image_id}')

        if Utils.is_not_empty(instance_id):
            if self.stop:
                self.stop_ec2_instance(instance_id=instance_id)
                self.report.info(f'ec2 instance: {instance_id} stopped.')
            elif self.terminate:
                self.terminate_ec2_instance(instance_id=instance_id)
                self.report.info(f'ec2 instance: {instance_id} terminated.')
            else:
                self.report.warning(
                    f'AMI builder ec2 instance: {instance_id} needs to be manually terminated.'
                )
        return image_id

    def keep_for_inspection(self, instance_id: str, error: BaseException):
        """a failed build leaves its builder stopped so the bootstrap logs survive"""
        if Utils.is_empty(instance_id) or Utils.is_not_empty(self.instance_id):
            return
        stop_builder(self.context, instance_id, self.context.logger('ami-builder'))
        self.report.warning(
            f'build failed ({error}); builder instance {instance_id} was stopped for inspection '
            f'and is terminated by the sweep after a day'
        )
