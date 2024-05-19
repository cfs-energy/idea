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
import os
from threading import RLock
from typing import List, Dict, Optional

import ideavirtualdesktopcontroller
from botocore.exceptions import ClientError

from ideadatamodel import constants, VirtualDesktopArchitecture
from ideadatamodel import (
    VirtualDesktopSession,
    VirtualDesktopBaseOS,
    BaseOS,
    VirtualDesktopGPU,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopSoftwareStack
)
from ideasdk.bootstrap import BootstrapPackageBuilder, BootstrapUserDataBuilder
from ideasdk.context import BootstrapContext
from ideasdk.utils import Utils, GroupNameHelper
from ideavirtualdesktopcontroller.app.clients.events_client.events_client import VirtualDesktopEventType
from ideavirtualdesktopcontroller.app.events.events_utils import EventsUtils


class VirtualDesktopControllerUtils:

    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        self.context = context
        self._logger = self.context.logger('virtual-desktop-controller-utils')
        self.s3_client = self.context.aws().s3()
        self.ec2_client = self.context.aws().ec2()
        self.eventbridge_client = self.context.aws().eventbridge()
        self.ssm_client = self.context.aws().ssm()
        self.sqs_client = self.context.aws().sqs()
        self.events_utils = EventsUtils(context=self.context)
        self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY = 'aws.ec2.all-instance-types-names-list'
        self.INSTANCE_INFO_CACHE_KEY = 'aws.ec2.all-instance-types-data'
        self.instance_types_lock = RLock()
        self.group_name_helper = GroupNameHelper(self.context)

    def create_tag(self, instance_id: str, tag_key: str, tag_value: str):
        self.ec2_client.create_tags(
            Resources=[
                instance_id
            ],
            Tags=[{
                'Key': tag_key,
                'Value': tag_value
            }]
        )

    def _build_and_upload_bootstrap_package(self, session: VirtualDesktopSession) -> str:
        bootstrap_context = BootstrapContext(
            config=self.context.config(),
            module_name=constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
            module_id=self.context.module_id(),
            module_set=self.context.module_set(),
            base_os=session.software_stack.base_os.value,
            instance_type=session.server.instance_type
        )
        bootstrap_context.vars.session_owner = session.owner
        bootstrap_context.vars.idea_session_id = session.idea_session_id
        bootstrap_context.vars.project = session.project.name
        if session.software_stack.base_os != VirtualDesktopBaseOS.WINDOWS:
            escape_chars = '\\'
        else:
            escape_chars = '`'

        # TODO: Deprecate
        bootstrap_context.vars.dcv_host_ready_message = f'{{{escape_chars}"event_group_id{escape_chars}":{escape_chars}"{session.idea_session_id}{escape_chars}",{escape_chars}"event_type{escape_chars}":{escape_chars}"{VirtualDesktopEventType.DCV_HOST_READY_EVENT}{escape_chars}",{escape_chars}"detail{escape_chars}":{{{escape_chars}"idea_session_id{escape_chars}":{escape_chars}"{session.idea_session_id}{escape_chars}",{escape_chars}"idea_session_owner{escape_chars}":{escape_chars}"{session.owner}{escape_chars}"}}}}'

        component = 'virtual-desktop-host-linux'
        if session.software_stack.base_os == VirtualDesktopBaseOS.WINDOWS:
            component = 'virtual-desktop-host-windows'

        bootstrap_package_archive_file = BootstrapPackageBuilder(
            bootstrap_context=bootstrap_context,
            source_directory=self.context.get_bootstrap_dir(),
            target_package_basename=f'dcv-host-{session.idea_session_id}',
            components=[component],
            tmp_dir=os.path.join(f'{self.context.config().get_string("shared-storage.apps.mount_dir", required=True)}', self.context.cluster_name(), self.context.module_id(), 'dcv-host-bootstrap', session.owner, f'{Utils.to_secure_filename(session.name)}-{session.idea_session_id}'),
            force_build=True,
            base_os=session.software_stack.base_os.value,
            logger=self._logger
        ).build()

        self._logger.debug(f'{session.idea_session_id} built bootstrap package: {bootstrap_package_archive_file}')
        cluster_s3_bucket = self.context.config().get_string('cluster.cluster_s3_bucket', required=True)
        upload_key = f'idea/{self.context.module_id()}/dcv-host-bootstrap/{Utils.to_secure_filename(session.name)}-{session.idea_session_id}/{os.path.basename(bootstrap_package_archive_file)}'
        self._logger.debug(f'{session.idea_session_id} uploading bootstrap package: {upload_key}')
        self.s3_client.upload_file(
            Bucket=cluster_s3_bucket,
            Filename=bootstrap_package_archive_file,
            Key=upload_key
        )
        return f's3://{cluster_s3_bucket}/{upload_key}'

    def _build_userdata(self, session: VirtualDesktopSession):
        install_commands = [
            '/bin/bash virtual-desktop-host-linux/setup.sh'
        ]

        if session.software_stack.base_os == BaseOS.WINDOWS:
            install_commands = [
                'cd \"virtual-desktop-host-windows\"',
                'Import-Module .\\SetUp.ps1',
                'Setup-WindowsEC2Instance'
            ]

        use_vpc_endpoints = self.context.config().get_bool('cluster.network.use_vpc_endpoints', default=False)
        https_proxy = self.context.config().get_string('cluster.network.https_proxy', required=False, default='')
        no_proxy = self.context.config().get_string('cluster.network.no_proxy', required=False, default='')
        proxy_config = {}
        if use_vpc_endpoints and Utils.is_not_empty(https_proxy):
            proxy_config = {
                    'http_proxy': https_proxy,
                    'https_proxy': https_proxy,
                    'no_proxy': no_proxy
                    }

        user_data_builder = BootstrapUserDataBuilder(
            base_os=session.software_stack.base_os.value,
            aws_region=self.context.config().get_string('cluster.aws.region', required=True),
            bootstrap_package_uri=self._build_and_upload_bootstrap_package(session),
            install_commands=install_commands,
            proxy_config=proxy_config,
            substitution_support=False
        )

        return user_data_builder.build()

    def provision_dcv_host_for_session(self, session: VirtualDesktopSession) -> dict:

        tags = {
            constants.IDEA_TAG_NAME: f'{self.context.cluster_name()}-{session.name}-{session.owner}',
            constants.IDEA_TAG_NODE_TYPE: constants.NODE_TYPE_DCV_HOST,
            constants.IDEA_TAG_CLUSTER_NAME: self.context.cluster_name(),
            constants.IDEA_TAG_MODULE_ID: self.context.module_id(),
            constants.IDEA_TAG_MODULE_NAME: self.context.module_name(),
            constants.IDEA_TAG_MODULE_VERSION: self.context.module_version(),
            constants.IDEA_TAG_BACKUP_PLAN: f'{self.context.cluster_name()}-{self.context.module_id()}',
            constants.IDEA_TAG_PROJECT: session.project.name,
            constants.IDEA_TAG_DCV_SESSION_ID: 'TBD'
        }

        if Utils.is_not_empty(session.project.tags):
            for tag in session.project.tags:
                tags[tag.key] = tag.value

        custom_tags = self.context.config().get_list('global-settings.custom_tags', [])
        custom_tags_dict = Utils.convert_custom_tags_to_key_value_pairs(custom_tags)
        tags = {
            **custom_tags_dict,
            **tags
        }

        aws_tags = []
        for key, value in tags.items():
            aws_tags.append({
                'Key': key,
                'Value': value
            })

        metadata_http_tokens = self.context.config().get_string('virtual-desktop-controller.dcv_session.metadata_http_tokens', required=True)
        response = self.ec2_client.run_instances(
            UserData=self._build_userdata(session),
            ImageId=session.software_stack.ami_id,
            InstanceType=session.server.instance_type,
            TagSpecifications=[
                {
                    'ResourceType': 'instance',
                    'Tags': aws_tags
                }
            ],
            MaxCount=1,
            MinCount=1,
            NetworkInterfaces=[
                {
                    'DeviceIndex': 0,
                    'AssociatePublicIpAddress': False,
                    'SubnetId': session.server.subnet_id,
                    'Groups': session.server.security_groups
                }
            ],
            IamInstanceProfile={
                'Arn': session.server.instance_profile_arn
            },
            BlockDeviceMappings=[
                {
                    'DeviceName': Utils.get_ec2_block_device_name(session.software_stack.base_os.value),
                    'Ebs': {
                        'DeleteOnTermination': True,
                        'VolumeSize': session.server.root_volume_size.int_val(),
                        'Encrypted': False if Utils.is_empty(session.hibernation_enabled) else session.hibernation_enabled,
                        'VolumeType': 'gp3'
                    }
                }
            ],
            KeyName=session.server.key_pair_name,
            HibernationOptions={
                'Configured': False if Utils.is_empty(session.hibernation_enabled) else session.hibernation_enabled
            },
            MetadataOptions={
                'HttpTokens': metadata_http_tokens,
                'HttpEndpoint': 'enabled'
            }
        )
        return Utils.to_dict(response)

    def _add_instance_data_to_cache(self):
        with self.instance_types_lock:
            instance_type_names = self.context.cache().long_term().get(self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY)
            instance_info_data = self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
            if instance_type_names is None or instance_info_data is None:
                instance_type_names = []
                instance_info_data = {}

                has_more = True
                next_token = None
                while has_more:
                    if next_token is None:
                        result = self.context.aws().ec2().describe_instance_types(MaxResults=100)
                    else:
                        result = self.context.aws().ec2().describe_instance_types(MaxResults=100, NextToken=next_token)

                    next_token = Utils.get_value_as_string('NextToken', result)
                    has_more = Utils.is_not_empty(next_token)
                    current_instance_types = Utils.get_value_as_list('InstanceTypes', result)
                    for current_instance_type in current_instance_types:
                        instance_type_name = Utils.get_value_as_string('InstanceType', current_instance_type, '')
                        instance_type_names.append(instance_type_name)
                        instance_info_data[instance_type_name] = current_instance_type

                self.context.cache().long_term().set(self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY, instance_type_names)
                self.context.cache().long_term().set(self.INSTANCE_INFO_CACHE_KEY, instance_info_data)

    def is_gpu_instance(self, instance_type: str) -> bool:
        return self.get_gpu_manufacturer(instance_type) != VirtualDesktopGPU.NO_GPU

    def get_instance_type_info(self, instance_type: str) -> Dict:
        instance_types_data = self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
        if instance_types_data is None:
            # not found in cache, need to update it again.
            self._add_instance_data_to_cache()
            instance_types_data = self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
        return instance_types_data[instance_type]

    def get_instance_ram(self, instance_type: str) -> SocaMemory:
        instance_info = self.get_instance_type_info(instance_type)
        return SocaMemory(
            value=Utils.get_value_as_float('SizeInMiB', Utils.get_value_as_dict('MemoryInfo', instance_info, {}), 0),
            unit=SocaMemoryUnit.MiB
        )

    def get_architecture(self, instance_type: str) -> Optional[VirtualDesktopArchitecture]:
        instance_info = self.get_instance_type_info(instance_type)
        supported_archs = Utils.get_value_as_list('SupportedArchitectures', Utils.get_value_as_dict('ProcessorInfo', instance_info, {}), [])
        for supported_arch in supported_archs:
            if supported_arch == VirtualDesktopArchitecture.ARM64.value:
                return VirtualDesktopArchitecture.ARM64
            if supported_arch == VirtualDesktopArchitecture.X86_64.value:
                return VirtualDesktopArchitecture.X86_64

    def get_gpu_manufacturer(self, instance_type: str) -> VirtualDesktopGPU:
        instance_info = self.get_instance_type_info(instance_type)
        supported_gpus = Utils.get_value_as_list('Gpus', Utils.get_value_as_dict('GpuInfo', instance_info, {}), [])
        if len(supported_gpus) == 0:
            return VirtualDesktopGPU.NO_GPU

        for supported_gpu in supported_gpus:
            if Utils.get_value_as_string('Manufacturer', supported_gpu, '').lower() == VirtualDesktopGPU.NVIDIA.lower():
                return VirtualDesktopGPU.NVIDIA
            elif Utils.get_value_as_string('Manufacturer', supported_gpu, '').lower() == VirtualDesktopGPU.AMD.lower():
                return VirtualDesktopGPU.AMD

        return VirtualDesktopGPU.NO_GPU

    def get_valid_instance_types(self, hibernation_support: bool, software_stack: VirtualDesktopSoftwareStack = None, gpu: VirtualDesktopGPU = None) -> List[Dict]:
        instance_types_names = self.context.cache().long_term().get(self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY)
        instance_info_data = self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)
        if instance_types_names is None or instance_info_data is None:
            # not found in cache, need to update it again.
            self._add_instance_data_to_cache()
            instance_types_names = self.context.cache().long_term().get(self.INSTANCE_TYPES_NAMES_LIST_CACHE_KEY)
            instance_info_data = self.context.cache().long_term().get(self.INSTANCE_INFO_CACHE_KEY)

        # We now have a list of all instance types (Cache has been updated IF it was empty).
        valid_instance_types = []

        allowed_instance_types = self.context.config().get_list('virtual-desktop-controller.dcv_session.instance_types.allow', default=[])
        allowed_instance_type_names = set()
        allowed_instance_type_families = set()
        for instance_type in allowed_instance_types:
            if '.' in instance_type:
                allowed_instance_type_names.add(instance_type)
            else:
                allowed_instance_type_families.add(instance_type)

        denied_instance_types = set(self.context.config().get_list('virtual-desktop-controller.dcv_session.instance_types.deny', default=[]))
        denied_instance_type_names = set()
        denied_instance_type_families = set()
        for instance_type in denied_instance_types:
            if '.' in instance_type:
                denied_instance_type_names.add(instance_type)
            else:
                denied_instance_type_families.add(instance_type)

        for instance_type_name in instance_types_names:
            instance_type_family = instance_type_name.split('.')[0]

            if instance_type_name not in allowed_instance_type_names and instance_type_family not in allowed_instance_type_families:
                # instance type or instance family is not present in allow list
                continue

            if instance_type_name in denied_instance_type_names or instance_type_family in denied_instance_type_families:
                # instance type or instance family is present in deny list
                continue

            instance_info = instance_info_data[instance_type_name]
            if Utils.is_not_empty(software_stack) and software_stack.min_ram > self.get_instance_ram(instance_type_name):
                # this instance doesn't have the minimum ram required to support the software stack.
                continue

            hibernation_supported = Utils.get_value_as_bool('HibernationSupported', instance_info, False)
            if hibernation_support and not hibernation_supported:
                continue

            supported_archs = Utils.get_value_as_list('SupportedArchitectures', Utils.get_value_as_dict('ProcessorInfo', instance_info, {}), [])
            if Utils.is_not_empty(software_stack) and software_stack.architecture.value not in supported_archs:
                # not desired architecture
                continue

            supported_gpus = Utils.get_value_as_list('Gpus', Utils.get_value_as_dict('GpuInfo', instance_info, {}), [])
            perform_gpu_check = False
            gpu_to_check_against = None
            if Utils.is_not_empty(gpu):
                # We have gotten a GPU as a parameter. We need to perform a strict check
                perform_gpu_check = True
                gpu_to_check_against = gpu
            elif Utils.is_not_empty(software_stack) and software_stack.base_os == VirtualDesktopBaseOS.WINDOWS:
                # For Windows the GPU check is strict, Stacks with NO_GPU should return Instances without GPU.
                perform_gpu_check = True
                gpu_to_check_against = software_stack.gpu
            elif Utils.is_not_empty(software_stack) and software_stack.base_os != VirtualDesktopBaseOS.WINDOWS:
                # For Linux the GPU is not as strict, Stacks with NO_GPU can return Instances with GPU
                perform_gpu_check = software_stack.gpu != VirtualDesktopGPU.NO_GPU
                gpu_to_check_against = software_stack.gpu

            if perform_gpu_check:
                if gpu_to_check_against == VirtualDesktopGPU.NO_GPU:
                    # we don't need GPU
                    if len(supported_gpus) > 0:
                        # this instance SHOULD NOT have GPU support, but it does.
                        continue
                else:
                    # we need GPU
                    gpu_found = False
                    for supported_gpu in supported_gpus:
                        gpu_found = gpu_to_check_against.value.lower() == Utils.get_value_as_string('Manufacturer', supported_gpu, '').lower()
                        if gpu_found:
                            break

                    if not gpu_found:
                        # we needed a GPU, but we didn't find any
                        continue

            valid_instance_types.append(instance_info)
        return valid_instance_types

    def describe_image_id(self, ami_id: str) -> dict:
        try:
            response = Utils.to_dict(self.ec2_client.describe_images(
                ImageIds=[
                    ami_id
                ]
            ))
        except ClientError as e:
            self._logger.error(e)
            return {}

        images = Utils.get_value_as_list('Images', response, [])
        for image in images:
            return image
        return {}

    def create_image_for_instance_id(self, instance_id, image_name, image_description):
        if Utils.is_empty(image_name):
            image_name = f'IDEA-IMAGE-NAME-{instance_id}'

        if Utils.is_empty(image_description):
            image_description = f'IDEA-IMAGE-DESCRIPTION-{instance_id}'

        response = Utils.to_dict(self.ec2_client.create_image(
            Name=image_name,
            Description=image_description,
            NoReboot=True,
            InstanceId=instance_id
        ))
        return response

    def is_active_directory(self) -> bool:
        provider = self.context.config().get_string('directoryservice.provider', required=True)
        return provider in {constants.DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY, constants.DIRECTORYSERVICE_ACTIVE_DIRECTORY}

    def change_instance_type(self, instance_id: str, instance_type_name: str) -> (str, bool):
        if Utils.is_empty(instance_id) or Utils.is_empty(instance_type_name):
            return f'Invalid {instance_id} or {instance_type_name}', False

        self._logger.info(f'Changing instance type for {instance_id} to {instance_type_name}')
        # TODO: check if server is already stopped.
        try:
            _ = self.ec2_client.modify_instance_attribute(
                InstanceId=instance_id,
                Attribute='instanceType',
                Value=instance_type_name
            )
        except Exception as e:
            self._logger.error(e)
            return repr(e), False
        return '', True

    def get_virtual_desktop_users_group(self) -> str:
        return self.group_name_helper.get_module_users_group(self.context.module_id())

    def get_virtual_desktop_admin_group(self) -> str:
        return self.group_name_helper.get_module_administrators_group(module_id=self.context.module_id())
