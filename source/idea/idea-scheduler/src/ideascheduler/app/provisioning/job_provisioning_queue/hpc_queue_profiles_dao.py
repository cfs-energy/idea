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

import ideascheduler

from ideasdk.utils import Utils
from ideadatamodel import (
    exceptions,
    HpcQueueProfile,
    SocaScalingMode,
    SocaQueueMode,
    SocaQueueManagementParams,
    SocaJobParams,
    SocaMemory,
    SocaMemoryUnit,
    SocaAmount,
    SocaSpotAllocationStrategy,
    SocaFSxLustreConfig,
    Project
)

from typing import Dict, Optional, List
from boto3.dynamodb.conditions import Attr


class HpcQueueProfilesDAO:

    def __init__(self, context: ideascheduler.AppContext, logger=None):
        self.context = context
        if logger is not None:
            self.logger = logger
        else:
            self.logger = context.logger('queue-profiles-dao')
        self.table = None

    def get_table_name(self) -> str:
        return f'{self.context.cluster_name()}.{self.context.module_id()}.queue-profiles'

    def initialize(self):
        self.context.aws_util().dynamodb_create_table(
            create_table_request={
                'TableName': self.get_table_name(),
                'AttributeDefinitions': [
                    {
                        'AttributeName': 'queue_profile_id',
                        'AttributeType': 'S'
                    }
                ],
                'KeySchema': [
                    {
                        'AttributeName': 'queue_profile_id',
                        'KeyType': 'HASH'
                    }
                ],
                'BillingMode': 'PAY_PER_REQUEST'
            },
            wait=True
        )
        self.table = self.context.aws().dynamodb_table().Table(self.get_table_name())

    @staticmethod
    def convert_from_db(db_queue_profile: Dict) -> HpcQueueProfile:

        queue_profile = HpcQueueProfile()
        queue_profile.queue_profile_id = Utils.get_value_as_string('queue_profile_id', db_queue_profile)
        queue_profile.title = Utils.get_value_as_string('title', db_queue_profile)

        queue_profile.description = Utils.get_value_as_string('description', db_queue_profile)
        queue_profile.name = Utils.get_value_as_string('name', db_queue_profile)
        project_ids = Utils.get_value_as_list('project_ids', db_queue_profile)
        if project_ids is not None:
            projects = []
            for project_id in project_ids:
                projects.append(Project(project_id=project_id))
            queue_profile.projects = projects

        scaling_mode = Utils.get_value_as_string('scaling_mode', db_queue_profile)
        if Utils.is_not_empty(scaling_mode):
            queue_profile.scaling_mode = SocaScalingMode.resolve(scaling_mode)
        queue_mode = Utils.get_value_as_string('queue_mode', db_queue_profile)
        if Utils.is_not_empty(queue_mode):
            queue_profile.queue_mode = SocaQueueMode.resolve(queue_mode)
        queue_profile.enabled = Utils.get_value_as_bool('enabled', db_queue_profile)
        queue_profile.queues = Utils.get_value_as_list('queues', db_queue_profile)
        queue_profile.keep_forever = Utils.get_value_as_bool('keep_forever', db_queue_profile)

        queue_profile.terminate_when_idle = Utils.get_value_as_int('terminate_when_idle', db_queue_profile)
        queue_profile.stack_uuid = Utils.get_value_as_string('stack_uuid', db_queue_profile)

        queue_management_params = SocaQueueManagementParams()
        queue_profile.queue_management_params = queue_management_params
        queue_management_params.max_running_jobs = Utils.get_value_as_int('max_running_jobs', db_queue_profile)
        queue_management_params.max_provisioned_instances = Utils.get_value_as_int('max_provisioned_instances', db_queue_profile)
        queue_management_params.max_provisioned_capacity = Utils.get_value_as_int('max_provisioned_capacity', db_queue_profile)
        queue_management_params.wait_on_any_job_with_license = Utils.get_value_as_bool('wait_on_any_job_with_license', db_queue_profile)
        queue_management_params.allowed_instance_types = Utils.get_value_as_list('allowed_instance_types', db_queue_profile)
        queue_management_params.excluded_instance_types = Utils.get_value_as_list('excluded_instance_types', db_queue_profile)
        queue_management_params.restricted_parameters = Utils.get_value_as_list('restricted_parameters', db_queue_profile)
        queue_management_params.allowed_security_groups = Utils.get_value_as_list('allowed_security_groups', db_queue_profile)
        queue_management_params.allowed_instance_profiles = Utils.get_value_as_list('allowed_instance_profiles', db_queue_profile)

        default_job_params = SocaJobParams()
        queue_profile.default_job_params = default_job_params
        default_job_params.nodes = Utils.get_value_as_int('param_nodes', db_queue_profile)
        default_job_params.cpus = Utils.get_value_as_int('param_cpus', db_queue_profile)
        param_memory = Utils.get_value_as_float('param_memory', db_queue_profile)
        param_memory_unit = Utils.get_value_as_string('param_memory_unit', db_queue_profile)
        if param_memory is not None and param_memory_unit is not None:
            default_job_params.memory = SocaMemory(value=param_memory, unit=SocaMemoryUnit(param_memory_unit))
        default_job_params.gpus = Utils.get_value_as_int('param_gpus', db_queue_profile)
        default_job_params.mpiprocs = Utils.get_value_as_int('param_mpiprocs', db_queue_profile)
        default_job_params.base_os = Utils.get_value_as_string('param_base_os', db_queue_profile)
        default_job_params.instance_ami = Utils.get_value_as_string('param_instance_ami', db_queue_profile)
        default_job_params.instance_types = Utils.get_value_as_list('param_instance_types', db_queue_profile)
        default_job_params.force_reserved_instances = Utils.get_value_as_bool('param_force_reserved_instances', db_queue_profile)
        default_job_params.spot = Utils.get_value_as_bool('param_spot', db_queue_profile)
        spot_price = Utils.get_value_as_float('param_spot_price', db_queue_profile)
        if spot_price is not None:
            default_job_params.spot_price = SocaAmount(amount=spot_price)
        default_job_params.spot_allocation_count = Utils.get_value_as_int('param_spot_allocation_count', db_queue_profile)
        spot_allocation_strategy = Utils.get_value_as_string('param_spot_allocation_strategy', db_queue_profile)
        if spot_allocation_strategy is not None:
            default_job_params.spot_allocation_strategy = SocaSpotAllocationStrategy(spot_allocation_strategy)
        default_job_params.subnet_ids = Utils.get_value_as_list('param_subnet_ids', db_queue_profile)
        default_job_params.security_groups = Utils.get_value_as_list('param_security_groups', db_queue_profile)
        default_job_params.instance_profile = Utils.get_value_as_string('param_instance_profile', db_queue_profile)
        root_storage_size = Utils.get_value_as_float('param_root_storage_size', db_queue_profile)
        root_storage_size_unit = Utils.get_value_as_string('param_root_storage_size_unit', db_queue_profile)
        if root_storage_size is not None and root_storage_size_unit is not None:
            default_job_params.root_storage_size = SocaMemory(value=root_storage_size, unit=SocaMemoryUnit(root_storage_size_unit))
        default_job_params.enable_scratch = Utils.get_value_as_bool('param_enable_scratch', db_queue_profile)
        default_job_params.scratch_provider = Utils.get_value_as_string('param_scratch_provider', db_queue_profile)
        scratch_storage_size = Utils.get_value_as_float('param_scratch_storage_size', db_queue_profile)
        scratch_storage_size_unit = Utils.get_value_as_string('param_scratch_storage_size_unit', db_queue_profile)
        if scratch_storage_size is not None and scratch_storage_size_unit is not None:
            default_job_params.scratch_storage_size = SocaMemory(value=scratch_storage_size, unit=SocaMemoryUnit(scratch_storage_size_unit))
        default_job_params.scratch_storage_iops = Utils.get_value_as_int('param_scratch_storage_iops', db_queue_profile)
        default_job_params.enable_instance_store = Utils.get_value_as_bool('param_enable_instance_store', db_queue_profile)
        default_job_params.enable_efa_support = Utils.get_value_as_bool('param_enable_efa_support', db_queue_profile)
        default_job_params.enable_ht_support = Utils.get_value_as_bool('param_enable_ht_support', db_queue_profile)
        default_job_params.enable_placement_group = Utils.get_value_as_bool('param_enable_placement_group', db_queue_profile)
        default_job_params.enable_system_metrics = Utils.get_value_as_bool('param_enable_system_metrics', db_queue_profile)
        default_job_params.enable_anonymous_metrics = Utils.get_value_as_bool('param_enable_anonymous_metrics', db_queue_profile)
        default_job_params.compute_stack = Utils.get_value_as_string('param_compute_stack', db_queue_profile)
        default_job_params.stack_id = Utils.get_value_as_string('param_stack_id', db_queue_profile)
        default_job_params.job_group = Utils.get_value_as_string('param_job_group', db_queue_profile)

        if 'param_fsx_lustre_enabled' in db_queue_profile:
            fsx_lustre = SocaFSxLustreConfig()
            default_job_params.fsx_lustre = fsx_lustre
            fsx_lustre.enabled = Utils.get_value_as_bool('param_fsx_lustre_enabled', db_queue_profile)
            fsx_lustre.existing_fsx = Utils.get_value_as_string('param_fsx_lustre_existing_fsx', db_queue_profile)
            fsx_lustre.s3_backend = Utils.get_value_as_string('param_fsx_lustre_s3_backend', db_queue_profile)
            fsx_lustre.import_path = Utils.get_value_as_string('param_fsx_lustre_import_path', db_queue_profile)
            fsx_lustre.export_path = Utils.get_value_as_string('param_fsx_lustre_export_path', db_queue_profile)
            fsx_lustre.per_unit_throughput = Utils.get_value_as_int('param_fsx_lustre_per_unit_throughput', db_queue_profile)
            fsx_lustre_size = Utils.get_value_as_float('param_fsx_lustre_size', db_queue_profile)
            fsx_lustre_size_unit = Utils.get_value_as_string('param_fsx_lustre_size_unit', db_queue_profile)
            if fsx_lustre_size is not None and fsx_lustre_size_unit is not None:
                fsx_lustre.size = SocaMemory(value=fsx_lustre_size, unit=SocaMemoryUnit(fsx_lustre_size_unit))

        return queue_profile

    @staticmethod
    def convert_to_db(queue_profile: HpcQueueProfile) -> Dict:
        db_queue_profile = {}

        if queue_profile.queue_profile_id is not None:
            db_queue_profile['queue_profile_id'] = queue_profile.queue_profile_id
        if queue_profile.title is not None:
            db_queue_profile['title'] = queue_profile.title
        if queue_profile.description is not None:
            db_queue_profile['description'] = queue_profile.description
        if queue_profile.name is not None:
            db_queue_profile['name'] = queue_profile.name
        if queue_profile.projects is not None:
            project_ids = []
            for project in queue_profile.projects:
                if Utils.is_empty(project.project_id):
                    continue
                project_ids.append(project.project_id)
            if Utils.is_not_empty(project_ids):
                db_queue_profile['project_ids'] = project_ids
        if queue_profile.scaling_mode is not None:
            db_queue_profile['scaling_mode'] = str(queue_profile.scaling_mode)
        if queue_profile.queue_mode is not None:
            db_queue_profile['queue_mode'] = str(queue_profile.queue_mode)
        if queue_profile.enabled is not None:
            db_queue_profile['enabled'] = queue_profile.enabled
        if queue_profile.queues is not None:
            db_queue_profile['queues'] = queue_profile.queues
        if queue_profile.keep_forever is not None:
            db_queue_profile['keep_forever'] = queue_profile.keep_forever
        if queue_profile.terminate_when_idle is not None:
            db_queue_profile['terminate_when_idle'] = queue_profile.terminate_when_idle
        if queue_profile.stack_uuid is not None:
            db_queue_profile['stack_uuid'] = queue_profile.stack_uuid

        queue_management_params = queue_profile.queue_management_params
        if queue_management_params is not None:
            if queue_management_params.max_running_jobs is not None:
                db_queue_profile['max_running_jobs'] = queue_management_params.max_running_jobs
            if queue_management_params.max_provisioned_instances is not None:
                db_queue_profile['max_provisioned_instances'] = queue_management_params.max_provisioned_instances
            if queue_management_params.max_provisioned_capacity is not None:
                db_queue_profile['max_provisioned_capacity'] = queue_management_params.max_provisioned_capacity
            if queue_management_params.wait_on_any_job_with_license is not None:
                db_queue_profile['wait_on_any_job_with_license'] = queue_management_params.wait_on_any_job_with_license
            if queue_management_params.allowed_instance_types is not None:
                db_queue_profile['allowed_instance_types'] = queue_management_params.allowed_instance_types
            if queue_management_params.excluded_instance_types is not None:
                db_queue_profile['excluded_instance_types'] = queue_management_params.excluded_instance_types
            if queue_management_params.restricted_parameters is not None:
                db_queue_profile['restricted_parameters'] = queue_management_params.restricted_parameters
            if queue_management_params.allowed_security_groups is not None:
                db_queue_profile['allowed_security_groups'] = queue_management_params.allowed_security_groups
            if queue_management_params.allowed_instance_profiles is not None:
                db_queue_profile['allowed_instance_profiles'] = queue_management_params.allowed_instance_profiles

        job_params = queue_profile.default_job_params
        if job_params is not None:
            if job_params.nodes is not None:
                db_queue_profile['param_nodes'] = job_params.nodes
            if job_params.cpus is not None:
                db_queue_profile['param_cpus'] = job_params.cpus
            if job_params.memory is not None:
                db_queue_profile['param_memory'] = Utils.get_as_decimal(job_params.memory.value)
                db_queue_profile['param_memory_unit'] = str(job_params.memory.unit)
            if job_params.gpus is not None:
                db_queue_profile['param_gpus'] = job_params.gpus
            if job_params.mpiprocs is not None:
                db_queue_profile['param_mpiprocs'] = job_params.mpiprocs
            if job_params.walltime is not None:
                db_queue_profile['param_walltime'] = job_params.walltime
            if job_params.base_os is not None:
                db_queue_profile['param_base_os'] = job_params.base_os
            if job_params.instance_ami is not None:
                db_queue_profile['param_instance_ami'] = job_params.instance_ami
            if job_params.instance_types is not None:
                db_queue_profile['param_instance_types'] = job_params.instance_types
            if job_params.force_reserved_instances is not None:
                db_queue_profile['param_force_reserved_instances'] = job_params.force_reserved_instances
            if job_params.spot is not None:
                db_queue_profile['param_spot'] = job_params.spot
            if job_params.spot_price is not None:
                db_queue_profile['param_spot_price'] = Utils.get_as_decimal(job_params.spot_price.amount)
            if job_params.spot_allocation_count is not None:
                db_queue_profile['param_spot_allocation_count'] = job_params.spot_allocation_count
            if job_params.spot_allocation_strategy is not None:
                db_queue_profile['param_spot_allocation_strategy'] = str(job_params.spot_allocation_strategy)
            if job_params.subnet_ids is not None:
                db_queue_profile['param_subnet_ids'] = job_params.subnet_ids
            if job_params.security_groups is not None:
                db_queue_profile['param_security_groups'] = job_params.security_groups
            if job_params.instance_profile is not None:
                db_queue_profile['param_instance_profile'] = job_params.instance_profile
            if job_params.keep_ebs_volumes is not None:
                db_queue_profile['param_keep_ebs_volumes'] = job_params.keep_ebs_volumes
            if job_params.root_storage_size is not None:
                db_queue_profile['param_root_storage_size'] = Utils.get_as_decimal(job_params.root_storage_size.value)
                db_queue_profile['param_root_storage_size_unit'] = str(job_params.root_storage_size.unit)
            if job_params.enable_scratch is not None:
                db_queue_profile['param_enable_scratch'] = job_params.enable_scratch
            if job_params.scratch_provider is not None:
                db_queue_profile['param_scratch_provider'] = job_params.scratch_provider
            if job_params.scratch_storage_size is not None:
                db_queue_profile['param_scratch_storage_size'] = Utils.get_as_decimal(job_params.scratch_storage_size.value)
                db_queue_profile['param_scratch_storage_size_unit'] = str(job_params.scratch_storage_size.unit)
            if job_params.scratch_storage_iops is not None:
                db_queue_profile['param_scratch_storage_iops'] = job_params.scratch_storage_iops
            if job_params.enable_instance_store is not None:
                db_queue_profile['param_enable_instance_store'] = job_params.enable_instance_store
            if job_params.enable_efa_support is not None:
                db_queue_profile['param_enable_efa_support'] = job_params.enable_efa_support
            if job_params.enable_ht_support is not None:
                db_queue_profile['param_enable_ht_support'] = job_params.enable_ht_support
            if job_params.enable_placement_group is not None:
                db_queue_profile['param_enable_placement_group'] = job_params.enable_placement_group
            if job_params.enable_system_metrics is not None:
                db_queue_profile['param_enable_system_metrics'] = job_params.enable_system_metrics
            if job_params.enable_anonymous_metrics is not None:
                db_queue_profile['param_enable_anonymous_metrics'] = job_params.enable_anonymous_metrics
            if job_params.compute_stack is not None:
                db_queue_profile['param_compute_stack'] = job_params.compute_stack
            if job_params.stack_id is not None:
                db_queue_profile['param_stack_id'] = job_params.stack_id
            if job_params.job_group is not None:
                db_queue_profile['param_job_group'] = job_params.job_group
            if job_params.fsx_lustre is not None:
                fsx_lustre = job_params.fsx_lustre
                if fsx_lustre.enabled is not None:
                    db_queue_profile['param_fsx_lustre_enabled'] = fsx_lustre.enabled
                if fsx_lustre.existing_fsx is not None:
                    db_queue_profile['param_fsx_lustre_existing_fsx'] = fsx_lustre.existing_fsx
                if fsx_lustre.s3_backend is not None:
                    db_queue_profile['param_fsx_lustre_s3_backend'] = fsx_lustre.s3_backend
                if fsx_lustre.import_path is not None:
                    db_queue_profile['param_fsx_lustre_import_path'] = fsx_lustre.import_path
                if fsx_lustre.export_path is not None:
                    db_queue_profile['param_fsx_lustre_export_path'] = fsx_lustre.export_path
                if fsx_lustre.deployment_type is not None:
                    db_queue_profile['param_fsx_lustre_deployment_type'] = fsx_lustre.deployment_type
                if fsx_lustre.per_unit_throughput is not None:
                    db_queue_profile['param_fsx_lustre_per_unit_throughput'] = fsx_lustre.per_unit_throughput
                if fsx_lustre.size is not None:
                    db_queue_profile['param_fsx_lustre_size'] = Utils.get_as_decimal(fsx_lustre.size.value)
                    db_queue_profile['param_fsx_lustre_size_unit'] = str(fsx_lustre.size.unit)

        return db_queue_profile

    def create_queue_profile(self, queue_profile: Dict) -> Dict:

        created_queue_profile = {
            **queue_profile,
            'queue_profile_id': Utils.uuid(),
            'created_on': Utils.current_time_ms(),
            'updated_on': Utils.current_time_ms()
        }
        self.table.put_item(
            Item=created_queue_profile
        )
        return created_queue_profile

    def get_queue_profile_by_id(self, queue_profile_id: str) -> Optional[Dict]:
        if Utils.is_empty(queue_profile_id):
            raise exceptions.invalid_params('queue_profile_id is required')

        result = self.table.get_item(
            Key={
                'queue_profile_id': queue_profile_id
            }
        )
        return Utils.get_value_as_dict('Item', result)

    def get_queue_profile_by_name(self, queue_profile_name: str) -> Optional[Dict]:
        if Utils.is_empty(queue_profile_name):
            raise exceptions.invalid_params('queue_profile_name is required')

        scan_result = self.table.scan(
            FilterExpression=Attr('name').eq(queue_profile_name)
        )
        db_queue_profiles = Utils.get_value_as_list('Items', scan_result, [])
        if Utils.is_empty(db_queue_profiles):
            return None
        return db_queue_profiles[0]

    def get_queue_profile_by_queue(self, queue_name: str) -> Optional[Dict]:
        if Utils.is_empty(queue_name):
            raise exceptions.invalid_params('queue_profile_name is required')

        scan_result = self.table.scan()
        db_queue_profiles = Utils.get_value_as_list('Items', scan_result, [])
        if Utils.is_empty(db_queue_profiles):
            return None

        for db_queue_profile in db_queue_profiles:
            queues = Utils.get_value_as_list('queues', db_queue_profile, [])
            for queue in queues:
                if queue == queue_name:
                    return db_queue_profile
        return None

    def update(self, queue_profile: Dict) -> Dict:

        queue_profile_id = queue_profile['queue_profile_id']
        if Utils.is_empty(queue_profile_id):
            raise exceptions.invalid_params('queue_profile_id is required')
        queue_profile['updated_on'] = Utils.current_time_ms()

        update_expression_tokens = []
        expression_attr_names = {}
        expression_attr_values = {}

        for key, value in queue_profile.items():
            if key in ('queue_profile_id', 'created_on'):
                continue
            update_expression_tokens.append(f'#{key} = :{key}')
            expression_attr_names[f'#{key}'] = key
            expression_attr_values[f':{key}'] = value

        result = self.table.update_item(
            Key={
                'queue_profile_id': queue_profile_id
            },
            ConditionExpression=Attr('queue_profile_id').eq(queue_profile_id),
            UpdateExpression='SET ' + ', '.join(update_expression_tokens),
            ExpressionAttributeNames=expression_attr_names,
            ExpressionAttributeValues=expression_attr_values,
            ReturnValues='ALL_NEW'
        )

        updated_queue_profile = result['Attributes']
        updated_queue_profile['queue_profile_id'] = queue_profile_id
        return updated_queue_profile

    def delete_queue_profile(self, queue_profile_id: str):

        if Utils.is_empty(queue_profile_id):
            raise exceptions.invalid_params('queue_profile_id is required')

        self.table.delete_item(
            Key={
                'queue_profile_id': queue_profile_id
            }
        )

    def list_queue_profiles(self) -> List[Dict]:
        scan_result = self.table.scan()
        return Utils.get_value_as_list('Items', scan_result, [])
