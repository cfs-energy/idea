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
from typing import List, Optional, Dict

import yaml

import ideavirtualdesktopcontroller
from ideadatamodel import (
    exceptions,
    VirtualDesktopBaseOS,
    VirtualDesktopArchitecture,
    VirtualDesktopSoftwareStack,
    SocaMemory,
    SocaMemoryUnit,
    Project,
    GetProjectRequest,
    SocaSortBy,
    SocaSortOrder,
    ListSoftwareStackRequest,
    ListSoftwareStackResponse,
    SocaFilter,
    SocaListingPayload,
    SocaPaginator,
)
from ideadatamodel.virtual_desktop.virtual_desktop_model import (
    VirtualDesktopGPU,
    VirtualDesktopTenancy,
)
from ideasdk.aws.opensearch.opensearchable_db import OpenSearchableDB
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.virtual_desktop_notifiable_db import (
    VirtualDesktopNotifiableDB,
)
from ideavirtualdesktopcontroller.app.software_stacks import (
    constants as software_stacks_constants,
)


class VirtualDesktopSoftwareStackDB(VirtualDesktopNotifiableDB, OpenSearchableDB):
    DEFAULT_PAGE_SIZE = 10

    def __init__(self, context: ideavirtualdesktopcontroller.AppContext):
        self.context = context
        self._logger = self.context.logger('virtual-desktop-software-stack-db')
        self.BASE_STACKS_CONFIG_FILE = (
            f'{self.context.get_resources_dir()}/base-software-stack-config.yaml'
        )
        self._table_obj = None
        self._ddb_client = self.context.aws().dynamodb_table()

        VirtualDesktopNotifiableDB.__init__(
            self, context=self.context, table_name=self.table_name, logger=self._logger
        )
        OpenSearchableDB.__init__(
            self,
            context=self.context,
            logger=self._logger,
            term_filter_map={
                software_stacks_constants.SOFTWARE_STACK_DB_STACK_ID_KEY: 'stack_id.raw',
                software_stacks_constants.SOFTWARE_STACK_DB_FILTER_BASE_OS_KEY: 'base_os.raw',
                software_stacks_constants.SOFTWARE_STACK_DB_FILTER_PROJECT_ID_KEY: 'projects.project_id.raw',
                '$all': '$all',
            },
            date_range_filter_map={
                software_stacks_constants.SOFTWARE_STACK_DB_FILTER_CREATED_ON_KEY: 'created_on',
                software_stacks_constants.SOFTWARE_STACK_DB_FILTER_UPDATED_ON_KEY: 'updated_on',
            },
            default_page_size=self.DEFAULT_PAGE_SIZE,
        )

    def initialize(self):
        exists = self.context.aws_util().dynamodb_check_table_exists(
            self.table_name, True
        )
        if not exists:
            self.context.aws_util().dynamodb_create_table(
                create_table_request={
                    'TableName': self.table_name,
                    'AttributeDefinitions': [
                        {
                            'AttributeName': software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY,
                            'AttributeType': 'S',
                        },
                        {
                            'AttributeName': software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY,
                            'AttributeType': 'S',
                        },
                    ],
                    'KeySchema': [
                        {
                            'AttributeName': software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY,
                            'KeyType': 'HASH',
                        },
                        {
                            'AttributeName': software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY,
                            'KeyType': 'RANGE',
                        },
                    ],
                    'BillingMode': 'PAY_PER_REQUEST',
                },
                wait=True,
            )
            self._create_base_software_stacks()

    @property
    def _table(self):
        if Utils.is_empty(self._table_obj):
            self._table_obj = self._ddb_client.Table(self.table_name)
        return self._table_obj

    def _create_base_software_stacks(self):
        with open(self.BASE_STACKS_CONFIG_FILE, 'r') as f:
            base_stacks_config = yaml.safe_load(f)

        if Utils.is_empty(base_stacks_config):
            self._logger.error(
                f'{self.BASE_STACKS_CONFIG_FILE} file is empty. Returning'
            )
            return

        default_project = self.context.projects_client.get_default_project().project
        for base_os in VirtualDesktopBaseOS:
            software_stacks_seen = set()
            # Handle base_os as enum member - use the VALUE (not name)
            base_os_value = base_os.value
            self._logger.info(
                f'Processing base_os enum: {base_os}, value: {base_os_value}'
            )
            os_config = base_stacks_config.get(base_os_value)
            if Utils.is_empty(os_config):
                self._logger.error(
                    f'No configuration found for base_os: {base_os_value}. Skipping.'
                )
                continue
            for arch in VirtualDesktopArchitecture:
                arch_key = arch.replace('_', '-').lower()
                self._logger.info(
                    f'Processing architecture: {arch_key} within base_os: {base_os}'
                )
                arch_config = os_config.get(arch_key)
                if Utils.is_empty(arch_config):
                    self._logger.error(
                        f'Entry for architecture: {arch_key} within base_os: {base_os}. '
                        f'NOT FOUND. Returning'
                    )
                    continue

                default_name = arch_config.get('default-name')
                default_description = arch_config.get('default-description')
                default_min_storage_value = arch_config.get('default-min-storage-value')
                default_min_storage_unit = arch_config.get('default-min-storage-unit')
                default_min_ram_value = arch_config.get('default-min-ram-value')
                default_min_ram_unit = arch_config.get('default-min-ram-unit')
                if (
                    Utils.is_empty(default_name)
                    or Utils.is_empty(default_description)
                    or Utils.is_empty(default_min_storage_value)
                    or Utils.is_empty(default_min_storage_unit)
                    or Utils.is_empty(default_min_ram_value)
                    or Utils.is_empty(default_min_ram_unit)
                ):
                    error_message = (
                        f'Invalid base-software-stack-config.yaml configuration for OS: {base_os} Arch Config: '
                        f'{arch}. Missing default-name and/or default-description and/or default-min-storage-value '
                        f'and/or default-min-storage-unit and/or default-min-ram-value and/or default-min-ram-unit'
                    )
                    self._logger.error(error_message)
                    raise exceptions.invalid_params(error_message)

                aws_region = self.context.config().get_string(
                    'cluster.aws.region', required=True
                )
                self._logger.info(
                    f'Processing arch: {arch_key} within base_os: {base_os} '
                    f'for aws_region: {aws_region}'
                )

                region_configs = arch_config.get(aws_region)
                if Utils.is_empty(region_configs):
                    self._logger.error(
                        f'Entry for arch: {arch_key} within base_os: {base_os}. '
                        f'for aws_region: {aws_region} '
                        f'NOT FOUND. Returning'
                    )
                    continue

                for region_config in region_configs:
                    ami_id = region_config.get('ami-id')
                    if Utils.is_empty(ami_id):
                        error_message = (
                            f'Invalid base-software-stack-config.yaml configuration for OS: {base_os} Arch'
                            f' Config: {arch} AWS-Region: {aws_region}.'
                            f' Missing ami-id'
                        )
                        self._logger.error(error_message)
                        raise exceptions.general_exception(error_message)

                    ss_id_suffix = region_config.get('ss-id-suffix')
                    if Utils.is_empty(ss_id_suffix):
                        error_message = (
                            f'Invalid base-software-stack-config.yaml configuration for OS: {base_os} Arch'
                            f' Config: {arch} AWS-Region: {aws_region}.'
                            f' Missing ss-id-suffix'
                        )
                        self._logger.error(error_message)
                        raise exceptions.general_exception(error_message)

                    gpu_manufacturer = region_config.get('gpu-manufacturer')
                    if Utils.is_not_empty(
                        gpu_manufacturer
                    ) and gpu_manufacturer not in {'AMD', 'NVIDIA', 'NO_GPU'}:
                        error_message = (
                            f'Invalid base-software-stack-config.yaml configuration for OS: {base_os} Arch'
                            f' Config: {arch} AWS-Region: {aws_region}.'
                            f' Invalid gpu-manufacturer {gpu_manufacturer}. Can be one of AMD, NVIDIA, NO_GPU only'
                        )

                        self._logger.error(error_message)
                        raise exceptions.general_exception(error_message)
                    elif Utils.is_empty(gpu_manufacturer):
                        gpu_manufacturer = 'NO_GPU'

                    custom_stack_name = (
                        region_config.get('name')
                        if Utils.is_not_empty(region_config.get('name'))
                        else default_name
                    )
                    custom_stack_description = (
                        region_config.get('description')
                        if Utils.is_not_empty(region_config.get('description'))
                        else default_description
                    )
                    custom_stack_min_storage_value = (
                        region_config.get('min-storage-value')
                        if Utils.is_not_empty(region_config.get('min-storage-value'))
                        else default_min_storage_value
                    )
                    custom_stack_min_storage_unit = (
                        region_config.get('min-storage-unit')
                        if Utils.is_not_empty(region_config.get('min-storage-unit'))
                        else default_min_storage_unit
                    )
                    custom_stack_min_ram_value = (
                        region_config.get('min-ram-value')
                        if Utils.is_not_empty(region_config.get('min-ram-value'))
                        else default_min_ram_value
                    )
                    custom_stack_min_ram_unit = (
                        region_config.get('min-ram-unit')
                        if Utils.is_not_empty(region_config.get('min-ram-unit'))
                        else default_min_ram_unit
                    )
                    custom_stack_gpu_manufacturer = VirtualDesktopGPU(gpu_manufacturer)

                    self._logger.info(
                        f'Creating software stack with params: BASE_STACK_PREFIX={software_stacks_constants.BASE_STACK_PREFIX}, base_os_value={base_os_value}, arch_key={arch_key}, ss_id_suffix={ss_id_suffix}'
                    )
                    software_stack_id = f'{software_stacks_constants.BASE_STACK_PREFIX}-{base_os_value}-{arch_key}-{ss_id_suffix}'
                    self._logger.info(
                        f'Constructed software_stack_id: {software_stack_id}'
                    )
                    software_stacks_seen.add(software_stack_id)
                    software_stack_db_entry = self.get(
                        stack_id=software_stack_id, base_os=base_os_value
                    )
                    if Utils.is_empty(software_stack_db_entry):
                        # base SS doesn't exist. creating
                        self._logger.info(
                            f"software_stack_id: {software_stack_id}, doesn't exist. CREATING with base_os enum: {base_os}, value: {base_os_value}"
                        )
                        self.create(
                            VirtualDesktopSoftwareStack(
                                base_os=base_os,
                                stack_id=software_stack_id,
                                name=custom_stack_name,
                                description=custom_stack_description,
                                ami_id=ami_id,
                                enabled=True,
                                min_storage=SocaMemory(
                                    value=custom_stack_min_storage_value,
                                    unit=SocaMemoryUnit(custom_stack_min_storage_unit),
                                ),
                                min_ram=SocaMemory(
                                    value=custom_stack_min_ram_value,
                                    unit=SocaMemoryUnit(custom_stack_min_ram_unit),
                                ),
                                architecture=VirtualDesktopArchitecture(arch),
                                gpu=custom_stack_gpu_manufacturer,
                                projects=[default_project],
                                pool_enabled=False,
                                pool_asg_name=None,
                                launch_tenancy=VirtualDesktopTenancy.DEFAULT,
                            )
                        )

    @property
    def table_name(self) -> str:
        return f'{self.context.cluster_name()}.{self.context.module_id()}.controller.software-stacks'

    @staticmethod
    def convert_db_dict_to_software_stack_object(
        db_entry: dict,
    ) -> Optional[VirtualDesktopSoftwareStack]:
        if Utils.is_empty(db_entry):
            return None

        software_stack = VirtualDesktopSoftwareStack(
            base_os=VirtualDesktopBaseOS(
                Utils.get_value_as_string(
                    software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY, db_entry
                )
            ),
            stack_id=Utils.get_value_as_string(
                software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY, db_entry
            ),
            name=Utils.get_value_as_string(
                software_stacks_constants.SOFTWARE_STACK_DB_NAME_KEY, db_entry
            ),
            description=Utils.get_value_as_string(
                software_stacks_constants.SOFTWARE_STACK_DB_DESCRIPTION_KEY, db_entry
            ),
            created_on=Utils.to_datetime(
                Utils.get_value_as_int(
                    software_stacks_constants.SOFTWARE_STACK_DB_CREATED_ON_KEY, db_entry
                )
            ),
            updated_on=Utils.to_datetime(
                Utils.get_value_as_int(
                    software_stacks_constants.SOFTWARE_STACK_DB_UPDATED_ON_KEY, db_entry
                )
            ),
            ami_id=Utils.get_value_as_string(
                software_stacks_constants.SOFTWARE_STACK_DB_AMI_ID_KEY, db_entry
            ),
            enabled=Utils.get_value_as_bool(
                software_stacks_constants.SOFTWARE_STACK_DB_ENABLED_KEY, db_entry
            ),
            min_storage=SocaMemory(
                value=Utils.get_value_as_float(
                    software_stacks_constants.SOFTWARE_STACK_DB_MIN_STORAGE_VALUE_KEY,
                    db_entry,
                ),
                unit=SocaMemoryUnit(
                    Utils.get_value_as_string(
                        software_stacks_constants.SOFTWARE_STACK_DB_MIN_STORAGE_UNIT_KEY,
                        db_entry,
                    )
                ),
            ),
            min_ram=SocaMemory(
                value=Utils.get_value_as_float(
                    software_stacks_constants.SOFTWARE_STACK_DB_MIN_RAM_VALUE_KEY,
                    db_entry,
                ),
                unit=SocaMemoryUnit(
                    Utils.get_value_as_string(
                        software_stacks_constants.SOFTWARE_STACK_DB_MIN_RAM_UNIT_KEY,
                        db_entry,
                    )
                ),
            ),
            architecture=VirtualDesktopArchitecture(
                Utils.get_value_as_string(
                    software_stacks_constants.SOFTWARE_STACK_DB_ARCHITECTURE_KEY,
                    db_entry,
                )
            ),
            gpu=VirtualDesktopGPU(
                Utils.get_value_as_string(
                    software_stacks_constants.SOFTWARE_STACK_DB_GPU_KEY, db_entry
                )
            ),
            projects=[],
            pool_enabled=Utils.get_value_as_bool(
                software_stacks_constants.SOFTWARE_STACK_DB_POOL_ENABLED_KEY, db_entry
            ),
            pool_asg_name=Utils.get_value_as_string(
                software_stacks_constants.SOFTWARE_STACK_DB_POOL_ASG_KEY, db_entry
            ),
            launch_tenancy=VirtualDesktopTenancy(
                Utils.get_value_as_string(
                    software_stacks_constants.SOFTWARE_STACK_DB_LAUNCH_TENANCY_KEY,
                    db_entry,
                    default='default',
                )
            ),
            allowed_instance_types=Utils.get_value_as_list(
                software_stacks_constants.SOFTWARE_STACK_DB_ALLOWED_INSTANCE_TYPES_KEY,
                db_entry,
                [],
            ),
        )

        for project_id in Utils.get_value_as_list(
            software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY, db_entry, []
        ):
            software_stack.projects.append(Project(project_id=project_id))

        return software_stack

    def convert_software_stack_object_to_index_dict(
        self, software_stack: VirtualDesktopSoftwareStack
    ) -> Dict:
        index_dict = self.convert_software_stack_object_to_db_dict(software_stack)

        project_ids = Utils.get_value_as_list(
            software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY, index_dict, []
        )
        index_dict[software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY] = []

        for project_id in project_ids:
            try:
                project = self.context.projects_client.get_project(
                    GetProjectRequest(project_id=project_id)
                ).project
                index_dict[
                    software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY
                ].append(
                    {
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_ID_KEY: project_id,
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_NAME_KEY: project.name,
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_TITLE_KEY: project.title,
                    }
                )
            except Exception as e:
                # Log the error but continue processing other projects
                # This prevents OpenSearch indexing from failing completely due to cluster manager connectivity issues
                self._logger.warning(
                    f'Failed to get project details for project_id {project_id} from cluster manager: {e}. '
                    f'Using project_id only for OpenSearch indexing.'
                )
                # Add project with just the ID - this allows indexing to continue
                index_dict[
                    software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY
                ].append(
                    {
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_ID_KEY: project_id,
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_NAME_KEY: f'unknown-{project_id}',
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_TITLE_KEY: f'Unknown Project ({project_id})',
                    }
                )
        return index_dict

    def convert_index_dict_to_software_stack_object(self, index_dict: Dict):
        ss_projects = Utils.get_value_as_list(
            software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY, index_dict, []
        )
        index_dict[software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY] = []
        software_stack = self.convert_db_dict_to_software_stack_object(index_dict)
        for project in ss_projects:
            software_stack.projects.append(
                Project(
                    project_id=Utils.get_value_as_string(
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_ID_KEY,
                        project,
                        None,
                    ),
                    name=Utils.get_value_as_string(
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_NAME_KEY,
                        project,
                        None,
                    ),
                    title=Utils.get_value_as_string(
                        software_stacks_constants.SOFTWARE_STACK_DB_PROJECT_TITLE_KEY,
                        project,
                        None,
                    ),
                )
            )
        return software_stack

    @staticmethod
    def convert_software_stack_object_to_db_dict(
        software_stack: VirtualDesktopSoftwareStack,
    ) -> Dict:
        if Utils.is_empty(software_stack):
            return {}

        # Ensure we're using the enum value for base_os, not the string representation of the enum
        base_os = (
            software_stack.base_os.value
            if hasattr(software_stack.base_os, 'value')
            else software_stack.base_os
        )

        db_dict = {
            software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY: base_os,
            software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY: software_stack.stack_id,
            software_stacks_constants.SOFTWARE_STACK_DB_NAME_KEY: software_stack.name,
            software_stacks_constants.SOFTWARE_STACK_DB_DESCRIPTION_KEY: software_stack.description,
            software_stacks_constants.SOFTWARE_STACK_DB_CREATED_ON_KEY: Utils.to_milliseconds(
                software_stack.created_on
            ),
            software_stacks_constants.SOFTWARE_STACK_DB_UPDATED_ON_KEY: Utils.to_milliseconds(
                software_stack.updated_on
            ),
            software_stacks_constants.SOFTWARE_STACK_DB_AMI_ID_KEY: software_stack.ami_id,
            software_stacks_constants.SOFTWARE_STACK_DB_ENABLED_KEY: software_stack.enabled,
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_STORAGE_VALUE_KEY: str(
                software_stack.min_storage.value
            ),
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_STORAGE_UNIT_KEY: software_stack.min_storage.unit,
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_RAM_VALUE_KEY: str(
                software_stack.min_ram.value
            ),
            software_stacks_constants.SOFTWARE_STACK_DB_MIN_RAM_UNIT_KEY: software_stack.min_ram.unit,
            software_stacks_constants.SOFTWARE_STACK_DB_ARCHITECTURE_KEY: software_stack.architecture,
            software_stacks_constants.SOFTWARE_STACK_DB_GPU_KEY: software_stack.gpu,
            software_stacks_constants.SOFTWARE_STACK_DB_POOL_ENABLED_KEY: software_stack.pool_enabled,
            software_stacks_constants.SOFTWARE_STACK_DB_POOL_ASG_KEY: software_stack.pool_asg_name,
            software_stacks_constants.SOFTWARE_STACK_DB_LAUNCH_TENANCY_KEY: software_stack.launch_tenancy,
        }

        # Always include allowed_instance_types in the database entry, even if empty
        # This ensures that when a user clears all instance types, it's saved to the database
        if hasattr(software_stack, 'allowed_instance_types'):
            db_dict[
                software_stacks_constants.SOFTWARE_STACK_DB_ALLOWED_INSTANCE_TYPES_KEY
            ] = software_stack.allowed_instance_types

        project_ids = []
        for project in software_stack.projects:
            project_ids.append(project.project_id)

        db_dict[software_stacks_constants.SOFTWARE_STACK_DB_PROJECTS_KEY] = project_ids
        return db_dict

    def get_index_name(self) -> str:
        return f'{self.context.config().get_string("virtual-desktop-controller.opensearch.software_stack.alias", required=True)}-{self.context.software_stack_template_version}'

    def get_default_sort(self) -> SocaSortBy:
        return SocaSortBy(key='created_on', order=SocaSortOrder.ASC)

    def create(
        self, software_stack: VirtualDesktopSoftwareStack
    ) -> VirtualDesktopSoftwareStack:
        self._logger.info(
            f'Creating software stack: {software_stack.stack_id}, base_os type: {type(software_stack.base_os)}, base_os: {software_stack.base_os}, base_os value: {software_stack.base_os.value if hasattr(software_stack.base_os, "value") else "no value"}'
        )
        db_entry = self.convert_software_stack_object_to_db_dict(software_stack)
        self._logger.info(
            f'DB entry for stack_id: {db_entry.get(software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY)}, base_os: {db_entry.get(software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY)}'
        )
        db_entry[software_stacks_constants.SOFTWARE_STACK_DB_CREATED_ON_KEY] = (
            Utils.current_time_ms()
        )
        db_entry[software_stacks_constants.SOFTWARE_STACK_DB_UPDATED_ON_KEY] = (
            Utils.current_time_ms()
        )
        self._table.put_item(Item=db_entry)
        self.trigger_create_event(
            db_entry[software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY],
            db_entry[software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY],
            new_entry=db_entry,
        )
        return self.convert_db_dict_to_software_stack_object(db_entry)

    def get_from_index(self, stack_id: str) -> Optional[VirtualDesktopSoftwareStack]:
        request = ListSoftwareStackRequest()
        request.add_filter(
            SocaFilter(
                key=software_stacks_constants.SOFTWARE_STACK_DB_STACK_ID_KEY,
                value=stack_id,
            )
        )
        response = self.list_from_index(request)
        if Utils.is_empty(response.listing):
            return None
        return response.listing[0]

    def get(self, stack_id: str, base_os: str) -> Optional[VirtualDesktopSoftwareStack]:
        software_stack_db_entry = None
        if Utils.is_empty(stack_id) or Utils.is_empty(base_os):
            self._logger.error(
                f'invalid values for stack_id: {stack_id} and/or base_os: {base_os}'
            )
        else:
            try:
                result = self._table.get_item(
                    Key={
                        software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY: base_os,
                        software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY: stack_id,
                    }
                )
                software_stack_db_entry = Utils.get_value_as_dict('Item', result)
            except self._ddb_client.exceptions.ResourceNotFoundException as _:
                # in this case we simply need to return None since the resource was not found
                return None
            except Exception as e:
                self._logger.exception(e)
                raise e

        return self.convert_db_dict_to_software_stack_object(software_stack_db_entry)

    def update(
        self, software_stack: VirtualDesktopSoftwareStack
    ) -> VirtualDesktopSoftwareStack:
        db_entry = self.convert_software_stack_object_to_db_dict(software_stack)
        db_entry[software_stacks_constants.SOFTWARE_STACK_DB_UPDATED_ON_KEY] = (
            Utils.current_time_ms()
        )
        update_expression_tokens = []
        expression_attr_names = {}
        expression_attr_values = {}

        for key, value in db_entry.items():
            if key in {
                software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY,
                software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY,
                software_stacks_constants.SOFTWARE_STACK_DB_CREATED_ON_KEY,
            }:
                continue
            update_expression_tokens.append(f'#{key} = :{key}')
            expression_attr_names[f'#{key}'] = key
            expression_attr_values[f':{key}'] = value

        result = self._table.update_item(
            Key={
                software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY: db_entry[
                    software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY
                ],
                software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY: db_entry[
                    software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY
                ],
            },
            UpdateExpression='SET ' + ', '.join(update_expression_tokens),
            ExpressionAttributeNames=expression_attr_names,
            ExpressionAttributeValues=expression_attr_values,
            ReturnValues='ALL_OLD',
        )

        old_db_entry = result['Attributes']
        self.trigger_update_event(
            db_entry[software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY],
            db_entry[software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY],
            old_entry=old_db_entry,
            new_entry=db_entry,
        )
        return self.convert_db_dict_to_software_stack_object(db_entry)

    def delete(self, software_stack: VirtualDesktopSoftwareStack):
        if Utils.is_empty(software_stack.stack_id) or Utils.is_empty(
            software_stack.base_os
        ):
            raise exceptions.invalid_params('stack_id and base_os are required')

        result = self._table.delete_item(
            Key={
                software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY: software_stack.base_os,
                software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY: software_stack.stack_id,
            },
            ReturnValues='ALL_OLD',
        )
        old_db_entry = result['Attributes']
        self.trigger_delete_event(
            old_db_entry[software_stacks_constants.SOFTWARE_STACK_DB_HASH_KEY],
            old_db_entry[software_stacks_constants.SOFTWARE_STACK_DB_RANGE_KEY],
            deleted_entry=old_db_entry,
        )

    def list_from_index(
        self, options: ListSoftwareStackRequest
    ) -> ListSoftwareStackResponse:
        if Utils.is_not_empty(options.filters):
            new_filters: List[SocaFilter] = []
            for listing_filter in options.filters:
                if (
                    listing_filter.key
                    == software_stacks_constants.SOFTWARE_STACK_DB_FILTER_BASE_OS_KEY
                    and listing_filter.value == '$all'
                ):
                    # needs to see all OS's no point adding a filter for OS at this point
                    continue
                new_filters.append(listing_filter)

            options.filters = new_filters

        response = self.list_from_opensearch(options)
        software_stacks: List[VirtualDesktopSoftwareStack] = []
        software_stacks_responses = Utils.get_value_as_list(
            'hits', Utils.get_value_as_dict('hits', response, default={}), default=[]
        )
        for software_stacks_response in software_stacks_responses:
            index_object = Utils.get_value_as_dict(
                '_source', software_stacks_response, default={}
            )
            software_stacks.append(
                self.convert_index_dict_to_software_stack_object(index_object)
            )

        return ListSoftwareStackResponse(
            listing=software_stacks,
            paginator=SocaPaginator(
                page_size=options.paginator.page_size
                if Utils.is_not_empty(options.paginator)
                and Utils.is_not_empty(options.paginator.page_size)
                else self.DEFAULT_PAGE_SIZE,
                start=options.paginator.start
                if Utils.is_not_empty(options.paginator)
                and Utils.is_not_empty(options.paginator.start)
                else 0,
                total=Utils.get_value_as_int(
                    'value',
                    Utils.get_value_as_dict(
                        'total', Utils.get_value_as_dict('hits', response, {}), {}
                    ),
                    0,
                ),
            ),
            filters=options.filters,
            date_range=options.date_range,
            sort_by=options.sort_by,
        )

    def list_all_from_db(self, request: ListSoftwareStackRequest) -> SocaListingPayload:
        list_request = {}

        exclusive_start_key = None
        if Utils.is_not_empty(request.cursor):
            exclusive_start_key = Utils.from_json(Utils.base64_decode(request.cursor))

        if exclusive_start_key is not None:
            list_request['ExclusiveStartKey'] = exclusive_start_key

        list_result = self._table.scan(**list_request)

        session_entries = Utils.get_value_as_list('Items', list_result, [])
        result = []
        for session in session_entries:
            result.append(self.convert_db_dict_to_software_stack_object(session))

        response_cursor = None
        exclusive_start_key = Utils.get_any_value('LastEvaluatedKey', list_result)
        if exclusive_start_key is not None:
            response_cursor = Utils.base64_encode(Utils.to_json(exclusive_start_key))

        return SocaListingPayload(
            listing=result,
            paginator=SocaPaginator(
                page_size=request.page_size, cursor=response_cursor
            ),
        )
