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

from ideadatamodel import exceptions, errorcodes, constants
from ideasdk.utils import Utils
from ideadatamodel.common import SocaMemory, SocaMemoryUnit, SocaAmount
from ideadatamodel.scheduler import (
    SocaSpotAllocationStrategy,
    SocaFSxLustreConfig,
    SocaJobLicenseAsk,
    SocaJobParams,
    SocaInstanceTypeOptions,
    SocaJobProvisioningOptions,
    SocaCapacityType,
    SocaQueueManagementParams,
    JobValidationResultEntry,
    JobValidationResult,
    JobValidationDebugEntry,
    JobParameterInfo,
    HpcQueueProfile,
)

import ideascheduler

from ast import literal_eval
from typing import Optional, Dict, Any, List, Union, Tuple, TypeVar
from abc import ABC, abstractmethod
from collections import OrderedDict
import random
import re

JOB_PARAM_INFO = {
    constants.JOB_PARAM_NODES: JobParameterInfo(
        name=constants.JOB_PARAM_NODES,
        title='Nodes',
        description='No. of nodes required for the Job.',
    ),
    constants.JOB_PARAM_CPUS: JobParameterInfo(
        name=constants.JOB_PARAM_CPUS,
        title='CPUs',
        description='No. of CPUs required for the Job.',
    ),
    constants.JOB_PARAM_MEMORY: JobParameterInfo(
        name=constants.JOB_PARAM_MEMORY,
        title='Memory',
        description='Memory required for the Job.',
    ),
    constants.JOB_PARAM_GPUS: JobParameterInfo(
        name=constants.JOB_PARAM_GPUS,
        title='GPUs',
        description='No. of GPUs required for the Job.',
    ),
    constants.JOB_PARAM_MPIPROCS: JobParameterInfo(
        name=constants.JOB_PARAM_MPIPROCS,
        title='MPI Processes',
        description='No. of MPI processes required for the Job.',
    ),
    constants.JOB_PARAM_BASE_OS: JobParameterInfo(
        name=constants.JOB_PARAM_BASE_OS,
        title='Base OS',
        description='OS of the Compute Node.',
    ),
    constants.JOB_PARAM_INSTANCE_AMI: JobParameterInfo(
        name=constants.JOB_PARAM_INSTANCE_AMI,
        title='Instance AMI ID',
        description='Custom Amazon Machine Image (AMI) ID.',
    ),
    constants.JOB_PARAM_INSTANCE_TYPES: JobParameterInfo(
        name=constants.JOB_PARAM_INSTANCE_TYPES,
        title='EC2 Instance Types',
        description='Applicable EC2 Instance Types for the Job.',
        provider_names={constants.SCHEDULER_OPENPBS: 'instance_type'},
    ),
    constants.JOB_PARAM_FORCE_RESERVED_INSTANCES: JobParameterInfo(
        name=constants.JOB_PARAM_FORCE_RESERVED_INSTANCES,
        title='Force Reserved Instances',
        description='Ensures Job will be provisioned only if Reserved EC2 Instances are purchased and available',
        provider_names={constants.SCHEDULER_OPENPBS: 'force_ri'},
    ),
    constants.JOB_PARAM_SPOT_PRICE: JobParameterInfo(
        name=constants.JOB_PARAM_SPOT_PRICE,
        title='Spot Price',
        description='Indicates if EC2 Spot capacity is required for the Job. Spot price value can be (auto) or a '
        '[float] value.',
    ),
    constants.JOB_PARAM_SPOT_ALLOCATION_COUNT: JobParameterInfo(
        name=constants.JOB_PARAM_SPOT_ALLOCATION_COUNT,
        title='Spot Allocation Count',
        description='If provided, CapacityType is MIXED. If SpotAllocationCount value is provided, it indicates how '
        'many instances from of the desired job capacity should be Spot instances.',
    ),
    constants.JOB_PARAM_SPOT_ALLOCATION_STRATEGY: JobParameterInfo(
        name=constants.JOB_PARAM_SPOT_ALLOCATION_STRATEGY,
        title='Spot Allocation Strategy',
        description='Spot allocation strategies determine how the Spot Instances in your fleet are fulfilled from '
        'Spot Instance pools.',
    ),
    constants.JOB_PARAM_SUBNET_IDS: JobParameterInfo(
        name=constants.JOB_PARAM_SUBNET_IDS,
        title='AWS Subnet IDs',
        description='Specifies SubnetIds in which the Job resources should be provisioned.',
        provider_names={constants.SCHEDULER_OPENPBS: 'subnet_id'},
    ),
    constants.JOB_PARAM_SECURITY_GROUPS: JobParameterInfo(
        name=constants.JOB_PARAM_SECURITY_GROUPS,
        title='SecurityGroup IDs',
        description='Specifies Security Group IDs to be attached to Job compute nodes.',
    ),
    constants.JOB_PARAM_INSTANCE_PROFILE: JobParameterInfo(
        name=constants.JOB_PARAM_INSTANCE_PROFILE,
        title='IAM Instance Profile',
        description='Specifies a IAM Instance Profile Name to be attached to Job compute nodes.',
    ),
    constants.JOB_PARAM_KEEP_EBS_VOLUMES: JobParameterInfo(
        name=constants.JOB_PARAM_KEEP_EBS_VOLUMES,
        title='Keep EBS Volumes after Termination',
        description='Indicates if EBS volumes should be terminated after compute nodes are terminated.',
        provider_names={constants.SCHEDULER_OPENPBS: 'keep_ebs'},
    ),
    constants.JOB_PARAM_ROOT_STORAGE_SIZE: JobParameterInfo(
        name=constants.JOB_PARAM_ROOT_STORAGE_SIZE,
        title='Root Storage Size',
        description='Specifies the size (in GB) of the Root Storage volume.',
        provider_names={constants.SCHEDULER_OPENPBS: 'root_size'},
    ),
    constants.JOB_PARAM_SCRATCH_STORAGE_SIZE: JobParameterInfo(
        name=constants.JOB_PARAM_SCRATCH_STORAGE_SIZE,
        title='Scratch Storage Size',
        description='Specifies the size (in GB) of the Scratch Storage volume.',
        provider_names={constants.SCHEDULER_OPENPBS: 'scratch_size'},
    ),
    constants.JOB_PARAM_SCRATCH_IOPS: JobParameterInfo(
        name=constants.JOB_PARAM_SCRATCH_IOPS,
        title='Scratch Storage IOPS',
        description='Specifies provisioned IOPS for Scratch Storage.',
        provider_names={constants.SCHEDULER_OPENPBS: 'scratch_iops'},
    ),
    constants.JOB_PARAM_FSX_LUSTRE: JobParameterInfo(
        name=constants.JOB_PARAM_FSX_LUSTRE,
        title='FSx for Lustre - Enabled',
        description='Indicates if FSx Lustre should be provisioned as Scratch Storage instead of EBS.',
    ),
    constants.JOB_PARAM_FSX_LUSTRE_S3_BACKEND: JobParameterInfo(
        name=None,
        title='FSx for Lustre - S3 Backend',
        description='Specifies an S3 Bucket to create and mount a new FSx for Lustre.',
    ),
    constants.JOB_PARAM_FSX_LUSTRE_EXISTING_FSX: JobParameterInfo(
        name=None,
        title='FSx for Lustre - Existing FSx ID',
        description='Specifies an existing FSx for Lustre ID instead of creating a new one.',
    ),
    constants.JOB_PARAM_FSX_LUSTRE_IMPORT_PATH: JobParameterInfo(
        name=None,
        title='FSx for Lustre - Import Path',
        description='Specifies an import path for FSx for Lustre.',
    ),
    constants.JOB_PARAM_FSX_LUSTRE_EXPORT_PATH: JobParameterInfo(
        name=None,
        title='FSx for Lustre - Export Path',
        description='Specifies the export path for FSx for Lustre.',
    ),
    constants.JOB_PARAM_FSX_LUSTRE_DEPLOYMENT_TYPE: JobParameterInfo(
        name=constants.JOB_PARAM_FSX_LUSTRE_DEPLOYMENT_TYPE,
        title='FSx for Lustre - Deployment Type',
        description='Specifies the deployment type',
    ),
    constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT: JobParameterInfo(
        name=constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT,
        title='FSx for Lustre - Per Unit Throughput',
        description='Specifies the baseline desk throughput of FSx for Lustre.',
    ),
    constants.JOB_PARAM_FSX_LUSTRE_SIZE: JobParameterInfo(
        name=constants.JOB_PARAM_FSX_LUSTRE_SIZE,
        title='FSx for Lustre - Size',
        description='Specifies the size of of FSx for Lustre.',
    ),
    constants.JOB_PARAM_ENABLE_INSTANCE_STORE: JobParameterInfo(
        name=constants.JOB_PARAM_ENABLE_INSTANCE_STORE,
        title='Enable Instance Store',
        description='Indicates if Instance Store volume should be provisioned.',
    ),
    constants.JOB_PARAM_ENABLE_EFA_SUPPORT: JobParameterInfo(
        name=constants.JOB_PARAM_ENABLE_EFA_SUPPORT,
        title='Enable EFA Support',
        description='Provision AWS Elastic Fabric Adapter (EFA) on applicable instance types.',
        provider_names={constants.SCHEDULER_OPENPBS: 'efa_support'},
    ),
    constants.JOB_PARAM_ENABLE_HT_SUPPORT: JobParameterInfo(
        name=constants.JOB_PARAM_ENABLE_HT_SUPPORT,
        title='Enable Hyper-Threading',
        description='Indicates if hyper-threading should be enabled for the Job.',
        provider_names={constants.SCHEDULER_OPENPBS: 'ht_support'},
    ),
    constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP: JobParameterInfo(
        name=constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP,
        title='Enable EC2 Placement Group',
        description='Indicates if EC2 Placement Groups should be enabled for the Job.',
        provider_names={constants.SCHEDULER_OPENPBS: 'placement_group'},
    ),
    constants.JOB_PARAM_ENABLE_SYSTEM_METRICS: JobParameterInfo(
        name=constants.JOB_PARAM_ENABLE_SYSTEM_METRICS,
        title='Enable System Metrics',
        description='Indicates if System Metrics should be collected from Compute Notes and published for Analytics.',
        provider_names={constants.SCHEDULER_OPENPBS: 'system_metrics'},
    ),
    constants.JOB_PARAM_ENABLE_ANONYMOUS_METRICS: JobParameterInfo(
        name=constants.JOB_PARAM_ENABLE_ANONYMOUS_METRICS,
        title='Enable Anonymous Metrics',
        description='Indicates if anonymous metrics can be posted to AWS.',
        provider_names={constants.SCHEDULER_OPENPBS: 'anonymous_metrics'},
    ),
    constants.JOB_PARAM_LICENSES: JobParameterInfo(
        name=constants.JOB_PARAM_LICENSES,
        title='License Requirements',
        description='Specifies the license names and their counts applicable for the Job.',
    ),
    constants.JOB_PARAM_WALLTIME: JobParameterInfo(
        name=constants.JOB_PARAM_WALLTIME,
        title='Walltime',
        description='Specifies the total time the Job should run.',
    ),
    constants.JOB_PARAM_COMPUTE_STACK: JobParameterInfo(
        name=constants.JOB_PARAM_COMPUTE_STACK,
        title='Compute Stack',
        description='Specifies the AWS CloudFormation Stack Name under which Job resources are provisioned.',
    ),
    constants.JOB_PARAM_JOB_GROUP: JobParameterInfo(
        name=constants.JOB_PARAM_JOB_GROUP,
        title='Job Group',
        description='Specifies similar or related Jobs that can be provisioned on the same compute stack.',
    ),
    constants.JOB_PARAM_CUSTOM_PARAMS: JobParameterInfo(
        name=None, title=None, description=None
    ),
    constants.JOB_OPTION_TERMINATE_WHEN_IDLE: JobParameterInfo(
        name=constants.JOB_OPTION_TERMINATE_WHEN_IDLE,
        title='Compute Node: Terminate When Idle',
        description='Specifies the no. of minutes the Compute node can stay idle, after which it can be terminated.',
    ),
    constants.JOB_OPTION_KEEP_FOREVER: JobParameterInfo(
        name=constants.JOB_OPTION_KEEP_FOREVER,
        title='Compute Node: Keep Forever',
        description='Indicates if the compute node can be terminated or not.',
    ),
    constants.JOB_OPTION_TAGS: JobParameterInfo(
        name=None, title=None, description=None
    ),
}


class ParamBuilderProtocol(ABC):
    @property
    @abstractmethod
    def builder_id(self) -> str: ...

    @abstractmethod
    def validate(self) -> bool:
        """
        validate should identify all validation issues and log it in the list of errors
        > this method should not raise any exceptions
        :return: True if validation succeeds, False otherwise
        """
        ...

    @abstractmethod
    def get(self) -> Any:
        """
        get should return the value passed in the raw params
        > if any type conversions are applicable, they should be performed here.
        > for eg. yes/no should be converted to booleans, + separated lists should be returned as a list
        > method should not return any defaults
        :return: the param supplied by user
        """
        ...

    @abstractmethod
    def apply(self):
        """
        should get the value using get() and apply to to job_params
        > if any defaults are applicable, should fetch the default and apply it if get() returns None
        """
        ...

    @abstractmethod
    def default(self, **kwargs) -> Any:
        """
        should return any applicable defaults for the parameter
        :return: default value for the parameter
        """
        ...


ParamBuilderType = TypeVar('ParamBuilderType', bound=ParamBuilderProtocol)


class JobParamsBuilderContextProtocol(ABC):
    @property
    @abstractmethod
    def params(self) -> Dict[str, Any]: ...

    @property
    @abstractmethod
    def job_params(self) -> SocaJobParams: ...

    @property
    @abstractmethod
    def provisioning_options(self) -> SocaJobProvisioningOptions: ...

    @abstractmethod
    def get_builder(self, name: str) -> Optional[ParamBuilderType]: ...

    @property
    @abstractmethod
    def soca_context(self) -> ideascheduler.AppContext: ...

    @abstractmethod
    def is_failed(self, *params: str) -> bool: ...

    @abstractmethod
    def add_validation_entry(self, param: str, message: str): ...

    @abstractmethod
    def has_queue(self) -> bool: ...

    @property
    @abstractmethod
    def queue(self) -> Optional[HpcQueueProfile]: ...


class BaseParamBuilder(ParamBuilderProtocol, ABC):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        self.context = context
        self.job_param = job_param
        self.validation_results: List[JobValidationResultEntry] = []

    @property
    def builder_id(self) -> str:
        return type(self).__name__

    @property
    def params(self):
        return self.context.params

    @property
    def result(self):
        return self.context.job_params

    @property
    def provisioning_options(self):
        return self.context.provisioning_options

    @property
    def soca_context(self) -> ideascheduler.AppContext:
        return self.context.soca_context

    @property
    def queue_management(self) -> Optional[SocaQueueManagementParams]:
        if self.context.queue is None:
            return None
        return self.context.queue.queue_management_params

    @property
    def default_job_params(self) -> Optional[SocaJobParams]:
        if self.context.queue is None:
            return None
        return self.context.queue.default_job_params

    def add_validation_entry(self, param: str, message: str):
        self.context.add_validation_entry(param, message)

    def eval_required(self, param: str, value: Any):
        if not Utils.is_empty(value=value):
            return True

        self.add_validation_entry(
            param=param, message=f'Job parameter: [{param}] is required.'
        )
        return False

    def eval_positive_nonzero_int(
        self, param: str, value: Union[int, float, SocaMemory] = None
    ):
        if value is None:
            return True

        if value > 0:
            return True

        self.add_validation_entry(
            param=param, message=f'{param} must be greater than 0: ({value})'
        )
        return False

    def eval_positive_int(self, param: str, value: Union[int, float, SocaMemory]):
        if value >= 0:
            return True

        self.add_validation_entry(
            param=param, message=f'{param} must be a positive number: ({value})'
        )
        return False

    def default(self, **kwargs) -> None:
        return None

    def get_capacity_type(self) -> Optional[SocaCapacityType]:
        if self.context.is_failed(
            constants.JOB_PARAM_NODES,
            constants.JOB_PARAM_SPOT,
            constants.JOB_PARAM_SPOT_ALLOCATION_COUNT,
        ):
            return None

        nodes_builder = self.context.get_builder(constants.JOB_PARAM_NODES)
        nodes = nodes_builder.get()
        if nodes is None:
            return None

        spot_param_builder: SpotParamBuilder = self.context.get_builder(
            constants.JOB_PARAM_SPOT
        )
        spot = spot_param_builder.get()
        if not Utils.is_true(spot):
            return SocaCapacityType.ONDEMAND

        spot_allocation_count_builder = self.context.get_builder(
            constants.JOB_PARAM_SPOT_ALLOCATION_COUNT
        )
        spot_allocation_count = spot_allocation_count_builder.get()

        if spot_allocation_count is None or spot_allocation_count <= 0:
            return SocaCapacityType.SPOT

        return SocaCapacityType.MIXED

    def is_spot_capacity(self) -> bool:
        capacity_type = self.get_capacity_type()
        if capacity_type is None:
            return False
        return capacity_type == SocaCapacityType.SPOT

    def is_ondemand_capacity(self) -> bool:
        capacity_type = self.get_capacity_type()
        if capacity_type is None:
            return False
        return capacity_type == SocaCapacityType.ONDEMAND

    def is_mixed_capacity(self) -> bool:
        capacity_type = self.get_capacity_type()
        if capacity_type is None:
            return False
        return capacity_type == SocaCapacityType.MIXED

    def is_restricted_parameter(self, param_name: Optional[str] = None) -> bool:
        """
        Checks if current param is a restricted parameter.
        QueueManagementParam: restricted_parameters
        :return: True if restricted parameter, False otherwise.
        """

        if self.context.queue is None:
            return False

        job_queue = self.context.queue.name

        if self.queue_management is None:
            return False

        if param_name is None:
            param_name = self.job_param

        provider_param_name = None
        if param_name in JOB_PARAM_INFO:
            param_info = JOB_PARAM_INFO[param_name]
            if param_info.provider_names is not None:
                provider_name = self.context.soca_context.config().get_string(
                    'scheduler.provider', required=True
                )
                provider_param_name = Utils.get_value_as_string(
                    provider_name, param_info.provider_names
                )

        def check_restricted_param(param_name_to_check: str) -> bool:
            if self.queue_management.is_restricted_parameter(
                param_name=param_name_to_check
            ):
                self.add_validation_entry(
                    param=param_name_to_check,
                    message=f'{param_name_to_check} is restricted for queue: ({job_queue}) '
                    f'and cannot be submitted as a job parameter by the user.',
                )
                return True
            return False

        if check_restricted_param(param_name):
            return True
        if Utils.is_not_empty(provider_param_name) and check_restricted_param(
            provider_param_name
        ):
            return True

        return False


class NodesParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        nodes = self.get()

        if nodes is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not self.eval_positive_nonzero_int(
            param=constants.JOB_PARAM_NODES, value=nodes
        ):
            return False

        return True

    def get(self) -> Optional[int]:
        return Utils.get_value_as_int(key='nodes', obj=self.params, default=None)

    def apply(self):
        nodes = self.get()
        if nodes is None:
            nodes = self.default()
        self.result.nodes = nodes

    def default(self) -> int:
        if self.default_job_params and self.default_job_params.nodes is not None:
            return self.default_job_params.nodes
        return constants.DEFAULT_NODES


class CpusParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        cpus = self.get()

        if cpus is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not self.eval_positive_nonzero_int(
            param=constants.JOB_PARAM_CPUS, value=cpus
        ):
            return False

        if self.context.is_failed(
            constants.JOB_PARAM_INSTANCE_TYPES, constants.JOB_PARAM_ENABLE_HT_SUPPORT
        ):
            return False

        instance_types_builder = self.context.get_builder(
            constants.JOB_PARAM_INSTANCE_TYPES
        )
        instance_types = instance_types_builder.get()
        if Utils.is_empty(instance_types):
            instance_types = instance_types_builder.default()

        enable_ht_support_builder = self.context.get_builder(
            constants.JOB_PARAM_ENABLE_HT_SUPPORT
        )
        enable_ht_support = enable_ht_support_builder.get()
        if enable_ht_support is None:
            enable_ht_support = enable_ht_support_builder.default()

        min_cpus = 9999999  # choose some arbitrary max value
        for instance_type in instance_types:
            ec2_instance_type = self.soca_context.aws_util().get_ec2_instance_type(
                instance_type=instance_type
            )
            if enable_ht_support:
                instance_type_cpus = ec2_instance_type.vcpu_info_default_vcpus
            else:
                instance_type_cpus = ec2_instance_type.vcpu_info_default_cores
            min_cpus = min(min_cpus, instance_type_cpus)

        if cpus > min_cpus:
            self.add_validation_entry(
                param=constants.JOB_PARAM_CPUS,
                message=f'Invalid {constants.JOB_PARAM_CPUS}: ({cpus}). One of the instance types: [{",".join(instance_types)}]'
                f' do not have enough CPUs: ({min_cpus}). ht_support={enable_ht_support}',
            )
            return False

        return True

    def get(self) -> Optional[int]:
        return Utils.get_value_as_int(key='cpus', obj=self.params, default=None)

    def apply(self):
        cpus = self.get()
        if cpus is None:
            cpus = self.default()
        self.result.cpus = cpus

    def default(self) -> int:
        if self.default_job_params and self.default_job_params.cpus is not None:
            return self.default_job_params.cpus
        return constants.DEFAULT_CPUS


class MemoryParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        try:
            memory = self.get(raise_exc=True)
        except exceptions.SocaException as e:
            self.add_validation_entry(
                param=constants.JOB_PARAM_MEMORY,
                message=f'failed to parse memory value. {e.message}',
            )
            return False

        if memory is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not self.eval_positive_nonzero_int(
            param=constants.JOB_PARAM_CPUS, value=memory
        ):
            return False

        if self.context.is_failed(constants.JOB_PARAM_INSTANCE_TYPES):
            return False

        instance_types_builder = self.context.get_builder(
            constants.JOB_PARAM_INSTANCE_TYPES
        )
        instance_types = instance_types_builder.get()
        if Utils.is_empty(instance_types):
            instance_types = instance_types_builder.default()

        memory_values = []
        for instance_type in instance_types:
            ec2_instance_type = self.soca_context.aws_util().get_ec2_instance_type(
                instance_type=instance_type
            )
            instance_type_memory = SocaMemory(
                value=ec2_instance_type.memory_info_size_in_mib, unit=SocaMemoryUnit.MiB
            )
            memory_values.append(instance_type_memory)

        min_memory = min(memory_values)
        if memory > min_memory:
            self.add_validation_entry(
                param=constants.JOB_PARAM_MEMORY,
                message=f'applicable instance types do not have sufficient memory requested for this job. '
                f'requested memory: {memory}, instance type memory (min): {min_memory}',
            )
            return False

        return True

    def get(self, raise_exc=False) -> Optional[SocaMemory]:
        memory = Utils.get_value_as_string(key='memory', obj=self.params, default=None)

        if memory is None:
            return None

        try:
            return SocaMemory.resolve(memory, SocaMemoryUnit.MiB)
        except exceptions.SocaException as e:
            if raise_exc:
                raise e
            else:
                return None

    def apply(self):
        memory = self.get()
        if memory is None:
            memory = self.default()
        self.result.memory = memory

    def default(self) -> Optional[SocaMemory]:
        if self.default_job_params and self.default_job_params.memory is not None:
            return self.default_job_params.memory
        return None


class GpusParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        gpus = self.get()

        if gpus is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not self.eval_positive_nonzero_int(
            param=constants.JOB_PARAM_GPUS, value=gpus
        ):
            return False

        return True

    def get(self) -> Optional[int]:
        return Utils.get_value_as_int(key='gpus', obj=self.params, default=None)

    def apply(self):
        gpus = self.get()
        if gpus is None:
            gpus = self.default()
        self.result.gpus = gpus

    def default(self) -> Optional[int]:
        if self.default_job_params and self.default_job_params.gpus is not None:
            return self.default_job_params.gpus
        return None


class MpiprocsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        mpiprocs = self.get()

        if mpiprocs is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not self.eval_positive_nonzero_int(
            param=constants.JOB_PARAM_MPIPROCS, value=mpiprocs
        ):
            return False

        return True

    def get(self) -> Optional[int]:
        return Utils.get_value_as_int(key='mpiprocs', obj=self.params, default=None)

    def apply(self):
        mpiprocs = self.get()
        if mpiprocs is None:
            mpiprocs = self.default()
        self.result.mpiprocs = mpiprocs

    def default(self) -> Optional[int]:
        if self.default_job_params and self.default_job_params.mpiprocs is not None:
            return self.default_job_params.mpiprocs
        return None


class BaseOsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        base_os = self.get()

        if base_os is None:
            return True

        if self.is_restricted_parameter():
            return False

        if base_os not in constants.ALLOWED_BASEOS:
            self.add_validation_entry(
                param=constants.JOB_PARAM_BASE_OS,
                message=f'base_os: ({base_os}) must be one of '
                f'the following values: {", ".join(constants.ALLOWED_BASEOS)}',
            )

        return True

    def get(self) -> Optional[str]:
        return Utils.get_value_as_string(key='base_os', obj=self.params, default=None)

    def apply(self):
        base_os = self.get()

        if base_os is None:
            base_os = self.default()

        self.result.base_os = base_os

    def default(self) -> str:
        if self.default_job_params and self.default_job_params.base_os is not None:
            return self.default_job_params.base_os
        return self.context.soca_context.config().get_string(
            'scheduler.compute_node_os'
        )


class InstanceAmiParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        instance_ami = self.get()

        if instance_ami is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not instance_ami.startswith('ami-'):
            self.add_validation_entry(
                param=constants.JOB_PARAM_INSTANCE_AMI,
                message=f'invalid instance_ami: ({instance_ami})',
            )
            return False

        return True

    def get(self) -> Optional[str]:
        return Utils.get_value_as_string(
            key='instance_ami', obj=self.params, default=None
        )

    def apply(self):
        instance_ami = self.get()

        if instance_ami is None:
            instance_ami = self.default()

        self.result.instance_ami = instance_ami

    def default(self) -> str:
        if self.default_job_params and self.default_job_params.instance_ami is not None:
            return self.default_job_params.instance_ami
        return self.context.soca_context.config().get_string(
            'scheduler.compute_node_ami'
        )


class InstanceTypesParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        instance_types = self.get()

        if instance_types is None:
            return True

        if self.is_restricted_parameter():
            return False

        all_instance_types = set(self.soca_context.aws_util().get_all_instance_types())

        invalid = []
        not_allowed = []
        excluded = []
        for instance_type in instance_types:
            # local checks before making network call

            if instance_type not in all_instance_types:
                invalid.append(instance_type)
                continue

            if self.queue_management:
                check_failed = False

                if not self.queue_management.is_allowed_instance_type(
                    instance_type=instance_type
                ):
                    not_allowed.append(instance_type)
                    check_failed = True

                if self.queue_management.is_excluded_instance_type(
                    instance_type=instance_type
                ):
                    excluded.append(instance_type)
                    check_failed = True

                if check_failed:
                    continue

            # make network call

            is_valid = self.soca_context.aws_util().is_instance_type_valid(
                instance_type=instance_type
            )
            if not is_valid:
                invalid.append(instance_type)

        success = True
        if len(invalid) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_INSTANCE_TYPES,
                message=f'Instance types invalid or not found: [{", ".join(invalid)}]',
            )
            success = False

        if len(not_allowed) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_INSTANCE_TYPES,
                message=f'Instance types not allowed for this queue: [{", ".join(not_allowed)}]. '
                f'Allowed instance types are: [{", ".join(self.queue_management.allowed_instance_types)}]',
            )
            success = False

        if len(excluded) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_INSTANCE_TYPES,
                message=f'One of more of given instance types are excluded for this queue: [{", ".join(excluded)}]. '
                f'Excluded instance types are: [{", ".join(self.queue_management.excluded_instance_types)}]',
            )
            success = False

        return success

    def get(self) -> Optional[List[str]]:
        # backward compatibility
        instance_types = Utils.get_any_value(key='instance_type', obj=self.params)

        if instance_types is None:
            instance_types = Utils.get_any_value(key='instance_types', obj=self.params)

        elif isinstance(instance_types, str):
            instance_types = instance_types.split('+')

        if Utils.is_empty(instance_types):
            return None

        result = []
        for instance_type in instance_types:
            if instance_type in result:
                continue
            result.append(instance_type)

        return result

    def apply(self):
        instance_types = self.get()

        if instance_types is None:
            instance_types = self.default()

        if instance_types is None or len(instance_types) == 0:
            raise exceptions.SocaException(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'{constants.JOB_PARAM_INSTANCE_TYPES} is a required job parameter.',
            )

        self.result.instance_types = instance_types
        instance_type_options = self.get_instance_type_options(
            instance_types=instance_types
        )
        self.provisioning_options.instance_types = instance_type_options

    def get_instance_type_options(
        self, instance_types: List[str], enable_ht_support=False
    ) -> List[SocaInstanceTypeOptions]:
        result = []
        for instance_type in instance_types:
            ec2_instance_type = self.soca_context.aws_util().get_ec2_instance_type(
                instance_type=instance_type
            )

            if ec2_instance_type.is_cpu_options_supported is True:
                cpu_options_supported = True
            else:
                cpu_options_supported = False

            # core_count
            default_core_count = ec2_instance_type.vcpu_info_default_cores
            default_vcpu_count = ec2_instance_type.vcpu_info_default_vcpus
            default_threads_per_core = (
                ec2_instance_type.vcpu_info_default_threads_per_core
            )

            # threads_per_core will be overridden in EnableHtSupportParamBuilder if enable_ht_support = True.
            if enable_ht_support:
                threads_per_core = default_threads_per_core
            else:
                threads_per_core = 1

            if enable_ht_support:
                weighted_capacity = default_vcpu_count
            else:
                weighted_capacity = default_core_count

            # ebs_optimized
            ebs_optimized = True
            if ec2_instance_type.ebs_info_ebs_optimized_support is None:
                ebs_optimized = False
            elif ec2_instance_type.ebs_info_ebs_optimized_support == 'unsupported':
                ebs_optimized = False

            # memory
            memory = SocaMemory(
                value=ec2_instance_type.memory_info_size_in_mib, unit=SocaMemoryUnit.MiB
            )

            options = SocaInstanceTypeOptions(
                name=instance_type,
                cpu_options_supported=cpu_options_supported,
                default_core_count=default_core_count,
                default_vcpu_count=default_vcpu_count,
                default_threads_per_core=default_threads_per_core,
                threads_per_core=threads_per_core,
                ebs_optimized=ebs_optimized,
                weighted_capacity=weighted_capacity,
                memory=memory,
            )
            result.append(options)

        return result

    def default(self) -> Optional[List[str]]:
        if (
            self.default_job_params
            and self.default_job_params.instance_types is not None
        ):
            return self.default_job_params.instance_types
        return None


class ForceReservedInstancesParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        force_ri = self.get()

        if force_ri is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Any:
        # backward compatibility
        value = Utils.get_value_as_bool(key='force_ri', obj=self.params, default=None)
        if value is None:
            value = Utils.get_value_as_bool(
                key='force_reserved_instances', obj=self.params, default=None
            )

        return value

    def apply(self):
        force_reserved_instances = self.get()
        if force_reserved_instances is None:
            force_reserved_instances = self.default()
        self.result.force_reserved_instances = force_reserved_instances

    def default(self) -> bool:
        if (
            self.default_job_params
            and self.default_job_params.force_reserved_instances is not None
        ):
            return self.default_job_params.force_reserved_instances
        return constants.DEFAULT_FORCE_RESERVED_INSTANCES


class SpotParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        enable_spot = self.get()

        if enable_spot is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[bool]:
        spot_price_builder = self.context.get_builder(constants.JOB_PARAM_SPOT_PRICE)
        spot_price = spot_price_builder.get()
        if spot_price is not None:
            return True
        return Utils.get_value_as_bool('spot', self.params)

    def apply(self):
        enable_spot = self.get()
        if enable_spot is None:
            enable_spot = self.default()
        self.result.spot = enable_spot

    def default(self) -> bool:
        if self.default_job_params and self.default_job_params.spot is not None:
            return self.default_job_params.spot
        return constants.DEFAULT_ENABLE_SPOT


class SpotPriceParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        spot_price = self.get()

        if spot_price is None:
            return True

        if self.is_restricted_parameter():
            return False

        if isinstance(spot_price, float):
            if not self.eval_positive_nonzero_int(
                param=constants.JOB_PARAM_SPOT_PRICE, value=spot_price
            ):
                return False
            return True

        if isinstance(spot_price, str):
            if spot_price == constants.SPOT_PRICE_AUTO:
                return True

        self.add_validation_entry(
            param=constants.JOB_PARAM_SPOT_PRICE,
            message=f'invalid spot price: {spot_price}. spot_price must be (auto) or a [float] value.',
        )
        return False

    def get(self) -> Optional[Union[float, str]]:
        value = Utils.get_any_value(key='spot_price', obj=self.params, default=None)
        if value is not None and isinstance(value, dict):
            amount = Utils.get_value_as_float('amount', value)
            if amount == 0.0:
                return constants.SPOT_PRICE_AUTO
            else:
                return amount

        value = Utils.get_value_as_float(
            key='spot_price', obj=self.params, default=None
        )
        if value is not None:
            return value
        value = Utils.get_value_as_string(
            key='spot_price', obj=self.params, default=None
        )
        if value is None:
            return None
        return value

    def apply(self):
        spot_price = self.get()
        if isinstance(spot_price, float):
            spot_price = SocaAmount(amount=spot_price)
        if spot_price is None:
            spot_price = self.default()
        if spot_price is not None:
            if isinstance(spot_price, SocaAmount):
                self.result.spot_price = spot_price

    def default(self) -> Optional[SocaAmount]:
        if self.default_job_params and self.default_job_params.spot_price is not None:
            return self.default_job_params.spot_price
        return None


class SpotAllocationCountParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        value = self.get()

        if value is None:
            return True

        if self.is_restricted_parameter():
            return False

        if self.context.is_failed(
            constants.JOB_PARAM_NODES, constants.JOB_PARAM_SPOT_PRICE
        ):
            return False

        if not self.eval_positive_int(
            param=constants.JOB_PARAM_SPOT_ALLOCATION_COUNT, value=value
        ):
            return False

        spot_price_builder = self.context.get_builder(constants.JOB_PARAM_SPOT_PRICE)
        spot_price = spot_price_builder.get()

        if spot_price is None:
            spot_price = spot_price_builder.default()

        if spot_price is None:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SPOT_ALLOCATION_COUNT,
                message=f'{constants.JOB_PARAM_SPOT_PRICE} is required if {constants.JOB_PARAM_SPOT_ALLOCATION_COUNT} is provided.',
            )
            return False

        nodes_builder = self.context.get_builder(constants.JOB_PARAM_NODES)
        nodes = nodes_builder.get()

        if value >= nodes:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SPOT_ALLOCATION_COUNT,
                message=f'{constants.JOB_PARAM_SPOT_ALLOCATION_COUNT}: ({value}) must be '
                f'lower than the number of nodes to be provisioned for '
                f'this job: ({nodes})',
            )
            return False

        return True

    def get(self) -> Optional[int]:
        value = Utils.get_value_as_int(
            key='spot_allocation_count', obj=self.params, default=None
        )
        if value is None:
            return None
        if value == 0:
            return None
        return value

    def apply(self):
        spot_allocation_count = self.get()
        if spot_allocation_count is None:
            spot_allocation_count = self.default()
        self.result.spot_allocation_count = spot_allocation_count

    def default(self) -> Optional[Union[float, str]]:
        if (
            self.default_job_params
            and self.default_job_params.spot_allocation_count is not None
        ):
            return self.default_job_params.spot_allocation_count
        return None


class SpotAllocationStrategyParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        try:
            spot_allocation_strategy = self.get(raise_exc=True)
        except exceptions.SocaException as e:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SPOT_ALLOCATION_STRATEGY, message=e.message
            )
            return False

        if spot_allocation_strategy is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self, raise_exc=False) -> Optional[SocaSpotAllocationStrategy]:
        if self.context.is_failed(constants.JOB_PARAM_SPOT):
            return None

        spot_builder = self.context.get_builder(constants.JOB_PARAM_SPOT)
        spot = spot_builder.get()

        if Utils.is_false(spot):
            return None

        value = Utils.get_value_as_string(
            'spot_allocation_strategy', self.params, default=None
        )
        if value is None:
            return None

        value = SocaSpotAllocationStrategy.resolve(value=value)
        if value is None:
            if raise_exc:
                raise exceptions.SocaException(
                    error_code=errorcodes.INVALID_PARAMS,
                    message=f'Invalid {constants.JOB_PARAM_SPOT_ALLOCATION_STRATEGY}: ({value}). '
                    f'Must be one of: [{", ".join(SocaSpotAllocationStrategy.valid_values())}]',
                )
            else:
                return None
        return value

    def apply(self):
        spot_allocation_strategy = self.get()
        if spot_allocation_strategy is None:
            spot_allocation_strategy = self.default()
        self.result.spot_allocation_strategy = spot_allocation_strategy

    def default(self) -> Optional[SocaSpotAllocationStrategy]:
        if self.is_mixed_capacity() or self.is_spot_capacity():
            if (
                self.default_job_params
                and self.default_job_params.spot_allocation_strategy is not None
            ):
                return self.default_job_params.spot_allocation_strategy
            return SocaSpotAllocationStrategy.resolve(
                constants.DEFAULT_SPOT_ALLOCATION_STRATEGY
            )
        return None


class SubnetIdsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        try:
            subnet_ids = self.get(raise_exc=True)
        except exceptions.SocaException as e:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SUBNET_IDS, message=e.message
            )
            return False

        if subnet_ids is None:
            return True

        if self.is_restricted_parameter():
            return False

        if self.context.is_failed(
            constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP,
            constants.JOB_PARAM_ENABLE_EFA_SUPPORT,
        ):
            return False

        success = True

        # note that we are calling enable_placement_group_builder.get()
        # this returns the raw value supplied by the user and not default value
        # if user explicitly enabled placement
        # prior to 3.0, if placement group was enabled, and user provided more than 1 subnet,
        #   subnets were automatically corrected to 1 subnet. user input should not be corrected automatically
        #   and should return an error
        enable_placement_group_builder = self.context.get_builder(
            constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP
        )
        enable_placement_group = enable_placement_group_builder.get()

        if Utils.is_true(enable_placement_group) and len(subnet_ids) > 1:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SUBNET_IDS,
                message=f'When placement group is enabled, no. of {constants.JOB_PARAM_SUBNET_IDS} cannot be more than (1).',
            )
            success = False

        enable_efa_support_builder = self.context.get_builder(
            constants.JOB_PARAM_ENABLE_EFA_SUPPORT
        )
        enable_efa_support = enable_efa_support_builder.get()
        if Utils.is_true(enable_efa_support) and len(subnet_ids) > 1:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SUBNET_IDS,
                message=f'When EFA support is enabled, no. of {constants.JOB_PARAM_SUBNET_IDS} cannot be more than (1).',
            )
            success = False

        return success

    def get(self, raise_exc=False) -> Optional[List[str]]:
        # backward compatibility
        value = Utils.get_any_value('subnet_id', self.params, default=None)
        if value is None:
            value = Utils.get_any_value('subnet_ids', self.params, default=None)

        if value is None:
            return None

        if Utils.is_int(value):
            value = Utils.get_as_int(value)
            private_subnets = self.context.soca_context.config().get_list(
                'cluster.network.private_subnets', required=True
            )
            max_subnets = len(private_subnets)
            if not (0 < value <= max_subnets):
                if raise_exc:
                    raise exceptions.SocaException(
                        error_code=errorcodes.INVALID_PARAMS,
                        message=f'{constants.JOB_PARAM_SUBNET_IDS} value must be between 1 and {max_subnets}',
                    )
                else:
                    return None
            else:
                return private_subnets[:value]
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return value.split('+')
        return None

    def apply(self):
        subnet_ids = self.get()
        if subnet_ids is None or len(subnet_ids) == 0:
            subnet_ids = self.default()
        self.result.subnet_ids = subnet_ids

    def default(self) -> List[str]:
        default_subnet_ids = None
        if self.default_job_params:
            if (
                self.default_job_params.subnet_ids is not None
                and len(self.default_job_params.subnet_ids) > 0
            ):
                default_subnet_ids = self.default_job_params.subnet_ids

        nodes_builder = self.context.get_builder(constants.JOB_PARAM_NODES)
        nodes = nodes_builder.get()
        if nodes is None:
            nodes = nodes_builder.default()

        if default_subnet_ids is None or len(default_subnet_ids) == 0:
            if self.is_spot_capacity() or self.is_mixed_capacity() or nodes == 1:
                default_subnet_ids = self.context.soca_context.config().get_list(
                    'cluster.network.private_subnets', required=True
                )
            else:
                private_subnets = self.context.soca_context.config().get_list(
                    'cluster.network.private_subnets', required=True
                )
                default_subnet_ids = [random.choice(private_subnets)]

        if len(default_subnet_ids) == 1:
            return default_subnet_ids

        # if FSx Lustre or single zone FileSystem is enabled, return the first subnet
        fsx_lustre_param_builder: FsxLustreParamBuilder = self.context.get_builder(
            constants.JOB_PARAM_FSX_LUSTRE
        )
        fsx_lustre = fsx_lustre_param_builder.get()
        if fsx_lustre is not None and fsx_lustre.enabled:
            return [default_subnet_ids[0]]

        # if EFA is enabled, multiple subnets cannot be supported.
        enable_efa_support_builder = self.context.get_builder(
            constants.JOB_PARAM_ENABLE_EFA_SUPPORT
        )
        enable_efa_support = enable_efa_support_builder.get()
        if enable_efa_support and len(default_subnet_ids) > 1 and nodes > 1:
            return [random.choice(default_subnet_ids)]

        # if PlacementGroup is enabled, multiple subnets cannot be supported.
        enable_placement_group_builder = self.context.get_builder(
            constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP
        )
        enable_placement_group = enable_placement_group_builder.get()
        if enable_placement_group and len(default_subnet_ids) > 1:
            return [random.choice(default_subnet_ids)]

        return default_subnet_ids


class SecurityGroupsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        security_groups = self.get()

        if security_groups is None:
            return True

        if self.is_restricted_parameter():
            return False

        if len(security_groups) > constants.MAX_SECURITY_GROUPS:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SECURITY_GROUPS,
                message=f'{constants.JOB_PARAM_SECURITY_GROUPS} cannot be more than {constants.MAX_SECURITY_GROUPS}',
            )
            return False

        blocked_security_groups = []
        invalid_security_groups = []
        for security_group in security_groups:
            # local checks

            if not security_group.startswith('sg-'):
                invalid_security_groups.append(security_group)
                continue

            if self.queue_management:
                if not self.queue_management.is_allowed_security_group(security_group):
                    blocked_security_groups.append(security_group)
                    continue

            # make network call
            is_valid = self.soca_context.aws_util().is_security_group_valid(
                security_group_id=security_group
            )

            if not is_valid:
                invalid_security_groups.append(security_group)

        success = True
        if len(blocked_security_groups) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SECURITY_GROUPS,
                message=f'Security groups [{",".join(blocked_security_groups)}] are not authorized '
                f'for queue: ({self.context.queue.name}). '
                f'List of valid security groups for this queue are: '
                f'[{", ".join(self.queue_management.allowed_security_groups)}].',
            )
            success = False

        if len(invalid_security_groups) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_SECURITY_GROUPS,
                message=f'Security groups not found or invalid: [{", ".join(invalid_security_groups)}]',
            )
            success = False

        return success

    def get(self) -> Optional[List[str]]:
        value = Utils.get_any_value('security_groups', self.params)
        if value is None:
            return None
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return value.split('+')
        return None

    def apply(self):
        additional_security_groups = self.get()
        default_security_groups = self.default()
        if additional_security_groups is None:
            security_groups = default_security_groups
        else:
            security_groups = list(
                set(default_security_groups + additional_security_groups)
            )
        self.result.security_groups = security_groups

    def default(self) -> List[str]:
        if self.default_job_params:
            if (
                self.default_job_params.security_groups is not None
                and len(self.default_job_params.security_groups) > 0
            ):
                return self.default_job_params.security_groups
        return self.soca_context.config().get_list(
            'scheduler.compute_node_security_group_ids', []
        )


class InstanceProfileParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        instance_profile = self.get()
        if instance_profile is None:
            return True

        if self.is_restricted_parameter():
            return False

        if (
            self.queue_management
            and not self.queue_management.is_allowed_instance_profile(instance_profile)
        ):
            self.add_validation_entry(
                param=constants.JOB_PARAM_INSTANCE_PROFILE,
                message=f'IAM instance profile: ({instance_profile}) is not authorized '
                f'for queue: ({self.context.queue.name}). '
                f'List of valid instance profiles for this queue are: '
                f'[{", ".join(self.queue_management.allowed_instance_profiles)}].',
            )
            return False

        if instance_profile.startswith('arn:'):
            instance_profile = instance_profile.split(':instance-profile/')[1]

        result = self.soca_context.aws_util().get_instance_profile_arn(
            instance_profile_name=instance_profile
        )
        if not result['success']:
            self.add_validation_entry(
                param=constants.JOB_PARAM_INSTANCE_PROFILE,
                message=f'{constants.JOB_PARAM_INSTANCE_PROFILE} not found: ({instance_profile})',
            )
            return False

        return True

    def get(self, raise_exc=False) -> Optional[str]:
        return Utils.get_value_as_string('instance_profile', self.params)

    def get_arn(self, instance_profile: str):
        result = self.soca_context.aws_util().get_instance_profile_arn(
            instance_profile_name=instance_profile
        )
        if not result['success']:
            # this will only happen if the instance profile is deleted after job is queued
            # in normal scenarios, validation will cover this scenario
            raise exceptions.SocaException(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Instance profile not found: ({instance_profile})',
            )
        return result['arn']

    def apply(self):
        instance_profile = self.get()
        if instance_profile is None:
            instance_profile = self.default()

        if instance_profile.startswith('arn:'):
            instance_profile_arn = instance_profile
        else:
            instance_profile_arn = self.get_arn(instance_profile=instance_profile)

        self.result.instance_profile = instance_profile_arn

    def default(self) -> str:
        if (
            self.default_job_params
            and self.default_job_params.instance_profile is not None
        ):
            return self.default_job_params.instance_profile
        return self.soca_context.config().get_string(
            'scheduler.compute_node_instance_profile_arn'
        )


class KeepEbsVolumesParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        keep_ebs_volumes = self.get()

        if keep_ebs_volumes is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[bool]:
        # backward compatibility
        value = Utils.get_value_as_bool('keep_ebs', self.params, default=None)
        if value is None:
            value = Utils.get_value_as_bool(
                'keep_ebs_volumes', self.params, default=None
            )
        return value

    def apply(self):
        value = self.get()
        if value is None:
            value = self.default()
        self.result.keep_ebs_volumes = value

    def default(self) -> bool:
        if (
            self.default_job_params
            and self.default_job_params.keep_ebs_volumes is not None
        ):
            return self.default_job_params.keep_ebs_volumes
        return constants.DEFAULT_KEEP_EBS_VOLUMES


class RootStorageSizeParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        value = self.get()

        if value is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not self.eval_positive_nonzero_int(
            param=constants.JOB_PARAM_ROOT_STORAGE_SIZE, value=value
        ):
            return False

        return True

    def get(self) -> Optional[SocaMemory]:
        # backward compatibility
        value = Utils.get_value_as_int('root_size', self.params)
        if value is None:
            value = Utils.get_value_as_int('root_storage_size', self.params)
        if value is None:
            value = Utils.get_any_value('root_storage_size', self.params)
        if value is None:
            return None
        if isinstance(value, dict):
            return SocaMemory(**value)
        else:
            return SocaMemory(value=value, unit=SocaMemoryUnit.GB)

    def apply(self):
        value = self.get()
        if value is None:
            value = self.default()
        self.result.root_storage_size = value

    def default(self) -> SocaMemory:
        if (
            self.default_job_params
            and self.default_job_params.root_storage_size is not None
        ):
            return self.default_job_params.root_storage_size
        return SocaMemory(
            value=constants.DEFAULT_ROOT_STORAGE_SIZE, unit=SocaMemoryUnit.GB
        )


class ScratchStorageSizeParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        value = self.get()

        if value is None:
            return True

        if self.is_restricted_parameter():
            return False

        if not self.eval_positive_int(
            param=constants.JOB_PARAM_SCRATCH_STORAGE_SIZE, value=value
        ):
            return False

        return True

    def get(self) -> Optional[SocaMemory]:
        # backward compatibility
        value = Utils.get_value_as_int('scratch_size', self.params)
        if value is None:
            value = Utils.get_value_as_int('scratch_storage_size', self.params)
        if value is None:
            value = Utils.get_any_value('scratch_storage_size', self.params)
        if value is None:
            return None
        if isinstance(value, dict):
            return SocaMemory(**value)
        else:
            return SocaMemory(value=value, unit=SocaMemoryUnit.GB)

    def apply(self):
        value = self.get()
        if value is None:
            value = self.default()
        self.result.scratch_storage_size = value
        if value.value > 0:
            self.result.scratch_provider = 'ebs'

    def default(self) -> SocaMemory:
        if (
            self.default_job_params
            and self.default_job_params.scratch_storage_size is not None
        ):
            return self.default_job_params.scratch_storage_size
        return SocaMemory(
            value=constants.DEFAULT_SCRATCH_STORAGE_SIZE, unit=SocaMemoryUnit.GB
        )


class ScratchIopsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        value = self.get()

        # if user has specified a value, check for restricted params
        if value is not None:
            if self.is_restricted_parameter():
                return False

            if not self.eval_positive_int(
                param=constants.JOB_PARAM_SCRATCH_IOPS, value=value
            ):
                return False

        if value is None:
            value = self.default()

        scratch_storage_size: ScratchStorageSizeParamBuilder = self.context.get_builder(
            constants.JOB_PARAM_SCRATCH_STORAGE_SIZE
        )
        storage_value = scratch_storage_size.get()
        if storage_value is None:
            storage_value = scratch_storage_size.default()

        if value is not None and scratch_storage_size is not None and storage_value > 0:
            iops_to_volume_size_ratio = value / storage_value.value
            if iops_to_volume_size_ratio > 50:
                self.add_validation_entry(
                    param=constants.JOB_PARAM_SCRATCH_IOPS,
                    message=f'Iops to volume size ratio of {iops_to_volume_size_ratio} is too high; maximum is 50. '
                    f'Either reduce the Iops or increase the scratch storage volume size.',
                )
                return False

        return True

    def get(self) -> Optional[int]:
        # backward compatibility
        scratch_storage_iops = Utils.get_value_as_int('scratch_iops', self.params)
        if scratch_storage_iops is None:
            scratch_storage_iops = Utils.get_value_as_int(
                'scratch_storage_iops', self.params
            )
        return scratch_storage_iops

    def apply(self):
        value = self.get()
        if value is None:
            value = self.default()
        self.result.scratch_storage_iops = value

    def default(self) -> int:
        if (
            self.default_job_params
            and self.default_job_params.scratch_storage_iops is not None
        ):
            return self.default_job_params.scratch_storage_iops
        return constants.DEFAULT_SCRATCH_IOPS


class FsxLustreParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        success = True

        fsx_lustre = self.get()

        if fsx_lustre is None:
            return True

        if fsx_lustre.enabled is False:
            return True

        if self.is_restricted_parameter(param_name=constants.JOB_PARAM_FSX_LUSTRE):
            return False
        if fsx_lustre.size is not None:
            if self.is_restricted_parameter(
                param_name=constants.JOB_PARAM_FSX_LUSTRE_SIZE
            ):
                return False
        if fsx_lustre.deployment_type is not None:
            if self.is_restricted_parameter(
                param_name=constants.JOB_PARAM_FSX_LUSTRE_DEPLOYMENT_TYPE
            ):
                return False
        if fsx_lustre.per_unit_throughput is not None:
            if self.is_restricted_parameter(
                param_name=constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT
            ):
                return False

        # get_as_int will eat the value provided by user if it's not a valid integer
        # check if user provided a size value and handle appropriately
        size_gb_str = Utils.get_value_as_string(
            constants.JOB_PARAM_FSX_LUSTRE_SIZE, self.params
        )
        if fsx_lustre.size is None and size_gb_str is not None:
            self.add_validation_entry(
                param=constants.JOB_PARAM_FSX_LUSTRE_SIZE,
                message=f'{constants.JOB_PARAM_FSX_LUSTRE_SIZE} must be a valid integer: ({size_gb_str})',
            )
            success = False

        if fsx_lustre.size is not None and not SocaFSxLustreConfig.is_allowed_size(
            int(fsx_lustre.size.value)
        ):
            self.add_validation_entry(
                param=constants.JOB_PARAM_FSX_LUSTRE_SIZE,
                message=f'Invalid {constants.JOB_PARAM_FSX_LUSTRE_SIZE}: ({size_gb_str}). '
                f'Allowed values: '
                f'[{", ".join([str(x) for x in SocaFSxLustreConfig.allowed_sizes_gb()])}]',
            )
            success = False

        if (
            fsx_lustre.deployment_type is not None
            and not SocaFSxLustreConfig.is_allowed_deployment_type(
                fsx_lustre.deployment_type
            )
        ):
            self.add_validation_entry(
                param=constants.JOB_PARAM_FSX_LUSTRE_DEPLOYMENT_TYPE,
                message=f'Invalid {constants.JOB_PARAM_FSX_LUSTRE_DEPLOYMENT_TYPE}: ({fsx_lustre.deployment_type}). '
                f'Allowed values: '
                f'[{", ".join(SocaFSxLustreConfig.allowed_deployment_types())}]',
            )
            success = False

        if (
            fsx_lustre.deployment_type
            == constants.FSX_LUSTRE_DEPLOYMENT_TYPE_PERSISTENT_1
        ):
            # get_as_int will eat the value provided by user if it's not a valid integer
            # check if user provided a throughput value and handle appropriately
            per_unit_throughput_str = Utils.get_value_as_string(
                constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT, self.params
            )
            if (
                fsx_lustre.per_unit_throughput is None
                and per_unit_throughput_str is not None
            ):
                self.add_validation_entry(
                    param=constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT,
                    message=f'{constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT} must be a valid integer: '
                    f'({per_unit_throughput_str}).',
                )
                success = False

            if (
                fsx_lustre.per_unit_throughput is not None
                and not SocaFSxLustreConfig.is_allowed_per_unit_throughput(
                    fsx_lustre.per_unit_throughput
                )
            ):
                self.add_validation_entry(
                    param=constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT,
                    message=f'Invalid {constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT}: ({fsx_lustre.per_unit_throughput}). '
                    f'Allowed values: '
                    f'[{", ".join([str(x) for x in SocaFSxLustreConfig.allowed_per_unit_throughputs()])}]',
                )
                success = False

        if fsx_lustre.s3_backend:
            bucket_name = fsx_lustre.s3_backend.split('s3://')[-1]
            result = self.soca_context.aws_util().s3_bucket_has_access(
                bucket_name=bucket_name
            )
            if not result['success']:
                error_code = result['error_code']
                if error_code == 'AccessDenied':
                    self.add_validation_entry(
                        param=constants.JOB_PARAM_FSX_LUSTRE_S3_BACKEND,
                        message=f'IDEA does not have access to this bucket: {bucket_name}.',
                    )
                elif error_code == 'NoSuchBucket':
                    self.add_validation_entry(
                        param=constants.JOB_PARAM_FSX_LUSTRE_S3_BACKEND,
                        message=f'Invalid {constants.JOB_PARAM_FSX_LUSTRE_S3_BACKEND}: ({bucket_name}). Bucket does not exist.',
                    )
            success = False

        return success

    def get(self) -> Optional[SocaFSxLustreConfig]:
        fsx_lustre_config = Utils.get_any_value('fsx_lustre', self.params)
        fsx_lustre_bool_value = Utils.get_value_as_bool('fsx_lustre', self.params)
        fsx_lustre_string_value = Utils.get_value_as_string('fsx_lustre', self.params)

        if fsx_lustre_config is not None and isinstance(fsx_lustre_config, dict):
            return SocaFSxLustreConfig(**fsx_lustre_config)

        size_gb = Utils.get_any_value('fsx_lustre_size', self.params)
        size = None
        if size_gb is not None:
            if isinstance(size_gb, dict):
                size = SocaMemory(**size_gb)
            else:
                size = SocaMemory(
                    value=Utils.get_as_int(size_gb), unit=SocaMemoryUnit.GB
                )

        per_unit_throughput = Utils.get_value_as_int(
            'fsx_lustre_per_unit_throughput', self.params
        )

        deployment_type = Utils.get_value_as_string(
            'fsx_lustre_deployment_type', self.params
        )
        if deployment_type is not None:
            deployment_type = deployment_type.lower()

        # both string and bool value are none, no FSxLustre config
        if fsx_lustre_bool_value is None and fsx_lustre_string_value is None:
            return None

        # if bool value is False, user has explicitly called out to disable FSxLustre
        if fsx_lustre_bool_value is False:
            return SocaFSxLustreConfig(enabled=False)

        # if bool value is True, return FSxLustreConfig with any other parameters supplied by user
        # defaults should take care of other values
        if fsx_lustre_bool_value is True:
            return SocaFSxLustreConfig(
                enabled=True,
                size=size,
                per_unit_throughput=per_unit_throughput,
                deployment_type=deployment_type,
            )

        # bool value is taken care of at this point, let's take a look at string value

        # if string value is none, other values do not matter, disable fsx lustre
        if fsx_lustre_string_value is None:
            return SocaFSxLustreConfig(enabled=False)

        # if existing file system is provided, return as so..
        if fsx_lustre_string_value.startswith(
            'fs-'
        ) or fsx_lustre_string_value.startswith('fc-'):
            existing_fsx = fsx_lustre_string_value
            return SocaFSxLustreConfig(enabled=True, existing_fsx=existing_fsx)

        # parse the tokens from string value and identify <bucket_name>+<export_path>+<import_path>
        tokens = fsx_lustre_string_value.split('+')
        s3_backend = tokens[0]
        if not s3_backend.startswith('s3://'):
            s3_backend = f's3://{s3_backend}'

        export_path = None
        if len(tokens) >= 2:
            export_path = tokens[1]

        import_path = None
        if len(tokens) >= 3:
            import_path = tokens[2]

        return SocaFSxLustreConfig(
            enabled=True,
            s3_backend=s3_backend,
            export_path=export_path,
            import_path=import_path,
            per_unit_throughput=per_unit_throughput,
            deployment_type=deployment_type,
            size=size,
        )

    def apply(self):
        fsx_config = self.get()

        if fsx_config is None:
            # disable fsx config
            self.result.fsx_lustre = SocaFSxLustreConfig(enabled=False)
            return

        if fsx_config.enabled is False:
            # if user as explicitly called out to disable fsx_lustre
            self.result.fsx_lustre = SocaFSxLustreConfig(enabled=False)
            return

        if fsx_config.existing_fsx is not None:
            # if user has provided existing fsx config
            self.result.fsx_lustre = fsx_config
            self.result.scratch_provider = 'fsx-lustre-existing'
            return

        if fsx_config.enabled is True:
            # defaults will be applied only if user had the intent to enable fsx,
            # i.e. fsx_config.enable is True

            defaults = self.default(s3_backend=fsx_config.s3_backend)
            self.result.scratch_provider = 'fsx-lustre-new'

            if fsx_config.s3_backend is None:
                fsx_config.s3_backend = defaults.s3_backend
            if fsx_config.import_path is None:
                fsx_config.import_path = defaults.import_path
            if fsx_config.export_path is None:
                fsx_config.export_path = defaults.export_path
            if fsx_config.deployment_type is None:
                fsx_config.deployment_type = defaults.deployment_type
            if fsx_config.per_unit_throughput is None:
                fsx_config.per_unit_throughput = defaults.per_unit_throughput
            if fsx_config.size is None:
                fsx_config.size = defaults.size

        self.result.fsx_lustre = fsx_config

    def default(self, s3_backend: Optional[str] = None) -> SocaFSxLustreConfig:
        if self.default_job_params and self.default_job_params.fsx_lustre is not None:
            default_fsx_config = self.default_job_params.fsx_lustre
        else:
            # create dummy defaults to make it easier to write code, else there will be lot of None checks
            default_fsx_config = SocaFSxLustreConfig()

        # admin may configure existing fsx at queue level
        # if so, no other parameters are applicable for defaults
        if default_fsx_config.existing_fsx is not None:
            return SocaFSxLustreConfig(existing_fsx=default_fsx_config.existing_fsx)

        deployment_type = default_fsx_config.deployment_type
        if deployment_type is None:
            deployment_type = constants.DEFAULT_FSX_LUSTRE_DEPLOYMENT_TYPE

        per_unit_throughput = None
        if deployment_type in constants.FSX_LUSTRE_PER_UNIT_THROUGHPUT_TYPES:
            per_unit_throughput = constants.DEFAULT_FSX_LUSTRE_PER_UNIT_THROUGHPUT

        size = default_fsx_config.size
        if size is None:
            size = SocaMemory(
                value=constants.DEFAULT_FSX_LUSTRE_SIZE_GB, unit=SocaMemoryUnit.GB
            )

        if s3_backend is None:
            s3_backend = default_fsx_config.s3_backend

        return SocaFSxLustreConfig(
            s3_backend=s3_backend,
            deployment_type=deployment_type,
            per_unit_throughput=per_unit_throughput,
            size=size,
        )


class EnableScratchParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        enable_scratch = self.get()

        if enable_scratch is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[bool]:
        enable_scratch = Utils.get_value_as_bool('enable_scratch', self.params)
        if enable_scratch is not None:
            return enable_scratch

        scratch_storage_size_builder: ScratchStorageSizeParamBuilder = (
            self.context.get_builder(constants.JOB_PARAM_SCRATCH_STORAGE_SIZE)
        )
        scratch_storage_size = scratch_storage_size_builder.get()

        fsx_lustre_builder: FsxLustreParamBuilder = self.context.get_builder(
            constants.JOB_PARAM_FSX_LUSTRE
        )
        fsx_lustre = fsx_lustre_builder.get()

        if scratch_storage_size is not None and scratch_storage_size > 0:
            return True
        if fsx_lustre is not None and Utils.is_true(fsx_lustre.enabled):
            return True

        return self.default()

    def apply(self):
        enable_scratch = self.get()
        if enable_scratch is None:
            enable_scratch = self.default()
        self.result.enable_scratch = enable_scratch

    def default(self) -> bool:
        if (
            self.default_job_params
            and self.default_job_params.enable_scratch is not None
        ):
            return self.default_job_params.enable_scratch
        return constants.DEFAULT_ENABLE_SCRATCH


class EnableEfaParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        enable_efa_support = self.get()

        if enable_efa_support is None:
            # Check if EFA is enabled by default in the queue profile
            enable_efa_support = self.default()

        if self.is_restricted_parameter():
            return False

        if enable_efa_support is False:
            # if it's false, we have no further validations to be performed. return True
            return True

        if self.context.is_failed(
            constants.JOB_PARAM_INSTANCE_TYPES, constants.JOB_PARAM_SPOT_PRICE
        ):
            return False

        instance_type_builder = self.context.get_builder(
            constants.JOB_PARAM_INSTANCE_TYPES
        )
        instance_types = instance_type_builder.get()
        if instance_types is None:
            instance_types = instance_type_builder.default()

        invalid_instances = []
        for instance_type in instance_types:
            has_efa_support = (
                self.soca_context.aws_util().is_instance_type_efa_supported(
                    instance_type=instance_type
                )
            )
            if not has_efa_support:
                invalid_instances.append(instance_type)

        if len(invalid_instances) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_ENABLE_EFA_SUPPORT,
                message=f'You have requested EFA support but given '
                f'instance types [{", ".join(invalid_instances)}] do not support EFA.',
            )
            return False

        return True

    def get(self) -> Optional[bool]:
        # backward compatibility
        enable_efa_support = Utils.get_value_as_bool(key='efa_support', obj=self.params)
        if enable_efa_support is None:
            enable_efa_support = Utils.get_value_as_bool(
                key='enable_efa_support', obj=self.params
            )
        return enable_efa_support

    def apply(self):
        value = self.get()
        if value is None:
            value = self.default()
        self.result.enable_efa_support = value

    def default(self) -> bool:
        if (
            self.default_job_params
            and self.default_job_params.enable_efa_support is not None
        ):
            return self.default_job_params.enable_efa_support
        return constants.DEFAULT_ENABLE_EFA_SUPPORT


class EnableHtSupportParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        enable_ht_support = self.get()

        if enable_ht_support is None:
            return True

        if self.is_restricted_parameter():
            return False

        if enable_ht_support and self.context.is_failed(
            constants.JOB_PARAM_INSTANCE_TYPES
        ):
            return False

        if enable_ht_support is True:
            return True

        return True

    def get(self) -> Optional[bool]:
        # backward compatibility
        enable_ht_support = Utils.get_value_as_bool('ht_support', self.params)
        if enable_ht_support is None:
            enable_ht_support = Utils.get_value_as_bool(
                'enable_ht_support', self.params
            )
        return enable_ht_support

    def apply(self):
        enable_ht_support = self.get()
        if enable_ht_support is None:
            enable_ht_support = self.default()
        self.result.enable_ht_support = enable_ht_support
        self.override_provisioning_options(enable_ht_support=enable_ht_support)

    def override_provisioning_options(self, enable_ht_support: bool):
        # if hyper-threading is disabled, leave the existing options as is
        #   as they are already setup in InstanceTypesParamBuilder
        if not enable_ht_support:
            return

        # override provisioning_options for hyper-threading enabled
        for option in self.provisioning_options.instance_types:
            option.threads_per_core = option.default_threads_per_core
            option.weighted_capacity = option.default_vcpu_count

    def default(self) -> bool:
        if (
            self.default_job_params
            and self.default_job_params.enable_ht_support is not None
        ):
            return self.default_job_params.enable_ht_support

        return constants.DEFAULT_ENABLE_HT_SUPPORT


class EnablePlacementGroupParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        enable_placement_group = self.get()
        if enable_placement_group is not None:
            if self.is_restricted_parameter():
                return False

        else:
            # to find placement group default value, all of these need to be validated first..
            dependencies_failed = self.context.is_failed(
                constants.JOB_PARAM_NODES,
                constants.JOB_PARAM_SUBNET_IDS,
                constants.JOB_PARAM_INSTANCE_TYPES,
            )
            if dependencies_failed:
                self.soca_context.logger().debug(
                    'Placement group validation deferred - dependencies failed'
                )
                return False

            enable_placement_group = self.default()

        if enable_placement_group is False:
            self.soca_context.logger().debug(
                'Placement group validation passed - disabled by configuration'
            )
            return True  # valid, as nothing needs to be checked.

        instance_types_builder = self.context.get_builder(
            constants.JOB_PARAM_INSTANCE_TYPES
        )
        instance_types = instance_types_builder.get()
        if Utils.is_empty(instance_types):
            instance_types = instance_types_builder.default()

        self.soca_context.logger().debug(
            f'Validating placement group support for instance types: {instance_types}'
        )

        invalid_instance_types = []
        for instance_type in instance_types:
            ec2_instance_type = self.soca_context.aws_util().get_ec2_instance_type(
                instance_type=instance_type
            )
            placement_group_strategies = (
                ec2_instance_type.placement_group_info_supported_strategies
            )
            if (
                placement_group_strategies is None
                or len(placement_group_strategies) == 0
            ):
                self.soca_context.logger().debug(
                    f'Instance type {instance_type} does not support any placement group strategies'
                )
                invalid_instance_types.append(instance_type)
                continue
            if (
                constants.EC2_PLACEMENT_GROUP_STRATEGY_CLUSTER
                not in placement_group_strategies
            ):
                self.soca_context.logger().debug(
                    f'Instance type {instance_type} does not support cluster placement group strategy'
                )
                invalid_instance_types.append(instance_type)
                continue

        if len(invalid_instance_types) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP,
                message=f'Placement group is enabled, but given instance types: [{",".join(invalid_instance_types)}] '
                f'do not support placement group strategy: {constants.EC2_PLACEMENT_GROUP_STRATEGY_CLUSTER}',
            )
            return False

        self.soca_context.logger().debug(
            'Placement group validation passed - all instance types support cluster placement group'
        )
        return True

    def get(self) -> Optional[bool]:
        # backward compatibility
        enable_placement_group = Utils.get_value_as_bool('placement_group', self.params)
        if enable_placement_group is None:
            enable_placement_group = Utils.get_value_as_bool(
                'enable_placement_group', self.params
            )

        if enable_placement_group is not None:
            self.soca_context.logger().info(
                f'User explicitly set placement group: {enable_placement_group}'
            )
        return enable_placement_group

    def apply(self):
        enable_placement_group = self.get()
        if enable_placement_group is None:
            enable_placement_group = self.default()
        self.result.enable_placement_group = enable_placement_group
        self.soca_context.logger().info(
            f'Final placement group setting: {enable_placement_group}'
        )

    def default(self) -> bool:
        # no. of nodes == 1, and placement group is specified, provisioning will fail.
        #   default enable_placement_group = False
        nodes_builder = self.context.get_builder(constants.JOB_PARAM_NODES)
        nodes = nodes_builder.get()
        if nodes == 1:
            self.soca_context.logger().info(
                'Placement group disabled - single node job (nodes=1)'
            )
            return False

        # if user has explicitly provided multiple subnets, disable placement group
        subnet_ids_builder = self.context.get_builder(constants.JOB_PARAM_SUBNET_IDS)
        subnet_ids = subnet_ids_builder.get()
        if subnet_ids is not None and len(subnet_ids) > 1:
            self.soca_context.logger().info(
                f'Placement group disabled - multiple subnets specified: {subnet_ids}'
            )
            return False

        # if a spot fleet is requested
        #   disable placement groups
        if self.is_spot_capacity():
            self.soca_context.logger().info(
                'Placement group disabled - capacity type is SPOT'
            )
            return False
        elif self.is_mixed_capacity():
            self.soca_context.logger().info(
                'Placement group disabled - capacity type is MIXED'
            )
            return False

        if (
            self.default_job_params
            and self.default_job_params.enable_placement_group is not None
        ):
            self.soca_context.logger().info(
                f'Using queue default placement group setting: {self.default_job_params.enable_placement_group}'
            )
            return self.default_job_params.enable_placement_group

        self.soca_context.logger().info(
            f'Using global default placement group setting: {constants.DEFAULT_ENABLE_PLACEMENT_GROUP}'
        )
        return constants.DEFAULT_ENABLE_PLACEMENT_GROUP


class EnableSystemMetricsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        enable_system_metrics = self.get()

        if enable_system_metrics is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[bool]:
        # backward compatibility
        enable_system_metrics = Utils.get_value_as_bool('system_metrics', self.params)
        if enable_system_metrics is None:
            enable_system_metrics = Utils.get_value_as_bool(
                'enable_system_metrics', self.params
            )
        return enable_system_metrics

    def apply(self):
        enable_system_metrics = self.get()
        if enable_system_metrics is None:
            enable_system_metrics = self.default()
        self.result.enable_system_metrics = enable_system_metrics

    def default(self) -> bool:
        if (
            self.default_job_params
            and self.default_job_params.enable_system_metrics is not None
        ):
            return self.default_job_params.enable_system_metrics
        return constants.DEFAULT_ENABLE_SYSTEM_METRICS


class EnableAnonymousMetricsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        enable_anonymous_metrics = self.get()

        if enable_anonymous_metrics is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[bool]:
        # backward compatibility
        enable_anonymous_metrics = Utils.get_value_as_bool(
            'anonymous_metrics', self.params
        )
        if enable_anonymous_metrics is None:
            enable_anonymous_metrics = Utils.get_value_as_bool(
                'enable_anonymous_metrics', self.params
            )
        return enable_anonymous_metrics

    def apply(self):
        enable_anonymous_metrics = self.get()
        if enable_anonymous_metrics is None:
            enable_anonymous_metrics = self.default()
        self.result.enable_anonymous_metrics = enable_anonymous_metrics

    def default(self) -> bool:
        if (
            self.default_job_params
            and self.default_job_params.enable_anonymous_metrics is not None
        ):
            return self.default_job_params.enable_anonymous_metrics
        return self.soca_context.config().get_bool(
            'cluster.solution.enable_solution_metrics', True
        )


class LicensesParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        licenses = self.get()
        if licenses is None:
            return True

        restricted_licences = []
        invalid_licenses = {}
        for license_ask in licenses:
            if self.is_restricted_parameter(param_name=license_ask.name):
                restricted_licences.append(license_ask.name)
                continue

            if license_ask.count <= 0:
                invalid_licenses[license_ask.name] = license_ask.count
                continue

        success = True

        if len(restricted_licences) > 0:
            success = False

        if len(invalid_licenses) > 0:
            self.add_validation_entry(
                param=constants.JOB_PARAM_LICENSES,
                message=f'Invalid license values: {invalid_licenses}. License values must be a valid non-zero int.',
            )
            success = False

        return success

    def get(self) -> Optional[List[SocaJobLicenseAsk]]:
        licenses = []
        for key in self.params:
            if '_lic_' in key:
                int_val = Utils.get_value_as_int(key, self.params)
                if int_val is not None:
                    licenses.append(SocaJobLicenseAsk(name=key, count=int_val))
        if len(licenses) == 0:
            return None
        return licenses

    def apply(self):
        licences = self.get()
        self.result.licenses = licences

    def default(self) -> Optional[Dict[str, int]]:
        return None


class TerminateWhenIdleOptionBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        terminate_when_idle = self.get()

        if terminate_when_idle is None:
            return True

        if not self.eval_positive_int(
            constants.JOB_OPTION_TERMINATE_WHEN_IDLE, terminate_when_idle
        ):
            return False

        return True

    def get(self) -> Optional[int]:
        return Utils.get_value_as_int('terminate_when_idle', self.params)

    def apply(self):
        terminate_when_idle = self.get()

        if terminate_when_idle is None:
            terminate_when_idle = self.default()

        self.provisioning_options.terminate_when_idle = terminate_when_idle

    def default(self) -> int:
        if self.context.queue:
            return Utils.get_as_int(
                self.context.queue.terminate_when_idle,
                constants.DEFAULT_TERMINATE_WHEN_IDLE,
            )
        return constants.DEFAULT_TERMINATE_WHEN_IDLE


class KeepForeverOptionBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        return True

    def get(self) -> Optional[bool]:
        return Utils.get_value_as_bool('keep_forever', self.params)

    def apply(self):
        keep_forever = self.get()
        if keep_forever is None:
            keep_forever = self.default()
        self.provisioning_options.keep_forever = keep_forever

    def default(self) -> bool:
        if self.context.queue:
            return Utils.get_as_bool(
                self.context.queue.keep_forever, constants.DEFAULT_KEEP_FOREVER
            )
        return constants.DEFAULT_KEEP_FOREVER


class TagsOptionBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        try:
            self.get(raise_exc=True)
            return True
        except exceptions.SocaException as e:
            self.add_validation_entry(
                param=constants.JOB_OPTION_TAGS, message=e.message
            )
            return False

    def get(self, raise_exc=False) -> Optional[Dict]:
        tags = Utils.get_value_as_string('tags', self.params)
        if Utils.is_empty(tags):
            return None
        try:
            return literal_eval(tags)
        except Exception as e:
            if raise_exc:
                raise exceptions.SocaException(
                    error_code=errorcodes.INVALID_PARAMS,
                    message=f'{constants.JOB_OPTION_TAGS} should be a valid dictionary. Error: {e}',
                )

    def apply(self):
        self.provisioning_options.tags = self.get()


class ComputeStackParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        compute_stack = self.get()
        if compute_stack is None:
            return True

        if self.is_restricted_parameter():
            return False

        if compute_stack.strip().lower() == 'tbd':
            return True

        def sanitize(token) -> str:
            return token.strip().lower().replace('-', '').replace('_', '')

        cluster_name = self.soca_context.cluster_name()
        compute_stack_prefix = sanitize(cluster_name)

        pattern = compute_stack_prefix + '-([a-z0-9-]){5, 50}'
        match = re.match(pattern, compute_stack)
        if not match:
            self.add_validation_entry(
                param=constants.JOB_PARAM_COMPUTE_STACK,
                message=f'stack_id must match the pattern: /{pattern}/g',
            )
            return False

        return True

    def get(self) -> Optional[str]:
        compute_stack = Utils.get_value_as_string('compute_node', self.params)
        if compute_stack is None:
            compute_stack = Utils.get_value_as_string('compute_stack', self.params)
        return compute_stack

    def apply(self):
        compute_stack = self.get()
        if compute_stack is None:
            compute_stack = self.default()
        self.result.compute_stack = compute_stack

    def default(self) -> Optional[str]:
        if (
            self.default_job_params is not None
            and self.default_job_params.compute_stack is not None
        ):
            return self.default_job_params.compute_stack
        return None


class StackIdParamBuilder(BaseParamBuilder):
    # todo - make this parameter restricted by default. should be set only by JobProvisioner after
    #   cloud formation stack is created.

    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        stack_id = self.get()
        if stack_id is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[str]:
        return Utils.get_value_as_string('stack_id', self.params)

    def apply(self):
        stack_id = self.get()
        if stack_id is None:
            stack_id = self.default()
        self.result.stack_id = stack_id

    def default(self) -> Optional[str]:
        if (
            self.default_job_params is not None
            and self.default_job_params.stack_id is not None
        ):
            return self.default_job_params.stack_id
        return None


class JobGroupParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        job_group = self.get()
        if job_group is None:
            return True

        if self.is_restricted_parameter():
            return False

        # remember: validate is only called when user submits the job.
        # so this pattern check for job group is only applicable if user submits a job with a custom job group
        pattern = 'custom-([a-z0-9]){1, 10}'
        match = re.match(pattern, job_group)
        if not match:
            self.add_validation_entry(
                param=constants.JOB_PARAM_COMPUTE_STACK,
                message=f'stack_id must match the pattern: /{pattern}/g',
            )
            return False

        return True

    def get(self) -> Optional[str]:
        return Utils.get_value_as_string('job_group', self.params)

    def apply(self):
        job_group = self.get()
        if job_group is None:
            job_group = self.default()
        self.result.job_group = job_group

    def default(self) -> Optional[str]:
        if (
            self.default_job_params is not None
            and self.default_job_params.job_group is not None
        ):
            return self.default_job_params.job_group
        return None


class JobStartedEmailTemplateParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        job_started_email_template = self.get()
        if job_started_email_template is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[str]:
        return Utils.get_value_as_string('job_started_email_template', self.params)

    def apply(self):
        job_started_email_template = self.get()
        if job_started_email_template is None:
            job_started_email_template = self.default()
        self.result.job_started_email_template = job_started_email_template

    def default(self) -> Optional[str]:
        return None


class JobCompletedEmailTemplateParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        job_completed_email_template = self.get()
        if job_completed_email_template is None:
            return True

        if self.is_restricted_parameter():
            return False

        return True

    def get(self) -> Optional[str]:
        return Utils.get_value_as_string('job_completed_email_template', self.params)

    def apply(self):
        job_completed_email_template = self.get()
        if job_completed_email_template is None:
            job_completed_email_template = self.default()
        self.result.job_completed_email_template = job_completed_email_template

    def default(self) -> Optional[str]:
        return None


class WalltimeParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        walltime = self.get()
        if walltime is None:
            return True

        # restricted parameter check not applicable for walltime.

        try:
            walltime_seconds = Utils.walltime_to_seconds(walltime=walltime)
        except ValueError:
            self.add_validation_entry(
                param=constants.JOB_PARAM_WALLTIME,
                message=f'Invalid walltime: {walltime}. Walltime must be of format - hours:minutes:seconds. '
                f'eg. 02:30:00',
            )
            return False

        if not self.eval_positive_int(constants.JOB_PARAM_WALLTIME, walltime_seconds):
            return False

        return True

    def get(self) -> Optional[str]:
        return Utils.get_value_as_string('walltime', self.params)

    def apply(self):
        walltime = self.get()
        if walltime is None:
            walltime = self.default()
        self.result.walltime = walltime

    def default(self) -> Optional[str]:
        if (
            self.default_job_params is not None
            and self.default_job_params.walltime is not None
        ):
            return self.default_job_params.walltime
        return None


class CustomParamsParamBuilder(BaseParamBuilder):
    def __init__(self, context: JobParamsBuilderContextProtocol, job_param: str):
        super().__init__(context, job_param)

    def validate(self) -> bool:
        pass

    def get(self) -> Any:
        pass

    def apply(self):
        pass


class JobParamsBuilderContext(JobParamsBuilderContextProtocol):
    def __init__(
        self,
        context: ideascheduler.AppContext,
        params: Dict[str, Any],
        queue_profile: Optional[HpcQueueProfile] = None,
        stack_uuid: Optional[str] = None,
    ):
        self._params = params

        if Utils.is_empty(stack_uuid) and queue_profile is not None:
            stack_uuid = queue_profile.stack_uuid

        if Utils.is_empty(stack_uuid):
            stack_uuid = Utils.uuid()

        self._provisioning_options = SocaJobProvisioningOptions(stack_uuid=stack_uuid)

        self._job_params = SocaJobParams()
        self.builders = OrderedDict()
        self._soca_context = context
        self.validation_results: List[JobValidationResultEntry] = []
        self._failed_params = set()
        self._initialize()
        self._queue_profile = queue_profile

    def add_builder(self, builder: BaseParamBuilder):
        self.builders[builder.job_param] = builder

    def _initialize(self):
        """
        Initialize job param builders

        NOTE: ORDER IS IMPORTANT
        for eg.
        > instance_types must be validated before enable_ht_support
        > enable_ht_support must be validated before cpus
        > placement_group must be validated before subnet_ids
        > and so on ...
        """
        self.add_builder(
            NodesParamBuilder(job_param=constants.JOB_PARAM_NODES, context=self)
        )
        self.add_builder(
            InstanceTypesParamBuilder(
                job_param=constants.JOB_PARAM_INSTANCE_TYPES, context=self
            )
        )
        self.add_builder(
            EnableHtSupportParamBuilder(
                job_param=constants.JOB_PARAM_ENABLE_HT_SUPPORT, context=self
            )
        )
        self.add_builder(
            CpusParamBuilder(job_param=constants.JOB_PARAM_CPUS, context=self)
        )
        self.add_builder(
            MemoryParamBuilder(job_param=constants.JOB_PARAM_MEMORY, context=self)
        )
        self.add_builder(
            GpusParamBuilder(job_param=constants.JOB_PARAM_GPUS, context=self)
        )
        self.add_builder(
            MpiprocsParamBuilder(job_param=constants.JOB_PARAM_MPIPROCS, context=self)
        )
        self.add_builder(
            BaseOsParamBuilder(job_param=constants.JOB_PARAM_BASE_OS, context=self)
        )
        self.add_builder(
            InstanceAmiParamBuilder(
                job_param=constants.JOB_PARAM_INSTANCE_AMI, context=self
            )
        )
        self.add_builder(
            EnablePlacementGroupParamBuilder(
                job_param=constants.JOB_PARAM_ENABLE_PLACEMENT_GROUP, context=self
            )
        )
        self.add_builder(
            SpotParamBuilder(job_param=constants.JOB_PARAM_SPOT, context=self)
        )
        self.add_builder(
            SpotPriceParamBuilder(
                job_param=constants.JOB_PARAM_SPOT_PRICE, context=self
            )
        )
        self.add_builder(
            SpotAllocationCountParamBuilder(
                job_param=constants.JOB_PARAM_SPOT_ALLOCATION_COUNT, context=self
            )
        )
        self.add_builder(
            SpotAllocationStrategyParamBuilder(
                job_param=constants.JOB_PARAM_SPOT_ALLOCATION_STRATEGY, context=self
            )
        )
        self.add_builder(
            ForceReservedInstancesParamBuilder(
                job_param=constants.JOB_PARAM_FORCE_RESERVED_INSTANCES, context=self
            )
        )
        self.add_builder(
            EnableEfaParamBuilder(
                job_param=constants.JOB_PARAM_ENABLE_EFA_SUPPORT, context=self
            )
        )
        self.add_builder(
            FsxLustreParamBuilder(
                job_param=constants.JOB_PARAM_FSX_LUSTRE, context=self
            )
        )
        self.add_builder(
            SubnetIdsParamBuilder(
                job_param=constants.JOB_PARAM_SUBNET_IDS, context=self
            )
        )
        self.add_builder(
            SecurityGroupsParamBuilder(
                job_param=constants.JOB_PARAM_SECURITY_GROUPS, context=self
            )
        )
        self.add_builder(
            InstanceProfileParamBuilder(
                job_param=constants.JOB_PARAM_INSTANCE_PROFILE, context=self
            )
        )
        self.add_builder(
            KeepEbsVolumesParamBuilder(
                job_param=constants.JOB_PARAM_KEEP_EBS_VOLUMES, context=self
            )
        )
        self.add_builder(
            RootStorageSizeParamBuilder(
                job_param=constants.JOB_PARAM_ROOT_STORAGE_SIZE, context=self
            )
        )
        self.add_builder(
            ScratchStorageSizeParamBuilder(
                job_param=constants.JOB_PARAM_SCRATCH_STORAGE_SIZE, context=self
            )
        )
        self.add_builder(
            ScratchIopsParamBuilder(
                job_param=constants.JOB_PARAM_SCRATCH_IOPS, context=self
            )
        )
        self.add_builder(
            EnableScratchParamBuilder(
                job_param=constants.JOB_PARAM_ENABLE_SCRATCH, context=self
            )
        )
        self.add_builder(
            EnableSystemMetricsParamBuilder(
                job_param=constants.JOB_PARAM_ENABLE_SYSTEM_METRICS, context=self
            )
        )
        self.add_builder(
            EnableAnonymousMetricsParamBuilder(
                job_param=constants.JOB_PARAM_ENABLE_ANONYMOUS_METRICS, context=self
            )
        )
        self.add_builder(
            KeepForeverOptionBuilder(
                job_param=constants.JOB_OPTION_KEEP_FOREVER, context=self
            )
        )
        self.add_builder(
            TerminateWhenIdleOptionBuilder(
                job_param=constants.JOB_OPTION_TERMINATE_WHEN_IDLE, context=self
            )
        )
        self.add_builder(
            TagsOptionBuilder(job_param=constants.JOB_OPTION_TAGS, context=self)
        )
        self.add_builder(
            LicensesParamBuilder(job_param=constants.JOB_PARAM_LICENSES, context=self)
        )
        self.add_builder(
            ComputeStackParamBuilder(
                job_param=constants.JOB_PARAM_COMPUTE_STACK, context=self
            )
        )
        self.add_builder(
            StackIdParamBuilder(job_param=constants.JOB_PARAM_STACK_ID, context=self)
        )
        self.add_builder(
            JobGroupParamBuilder(job_param=constants.JOB_PARAM_JOB_GROUP, context=self)
        )
        self.add_builder(
            JobStartedEmailTemplateParamBuilder(
                job_param=constants.JOB_PARAM_JOB_STARTED_EMAIL_TEMPLATE, context=self
            )
        )
        self.add_builder(
            JobCompletedEmailTemplateParamBuilder(
                job_param=constants.JOB_PARAM_JOB_COMPLETED_EMAIL_TEMPLATE, context=self
            )
        )
        self.add_builder(
            WalltimeParamBuilder(job_param=constants.JOB_PARAM_WALLTIME, context=self)
        )
        self.add_builder(
            CustomParamsParamBuilder(
                job_param=constants.JOB_PARAM_CUSTOM_PARAMS, context=self
            )
        )

    def get_builder(self, param: str) -> Optional[BaseParamBuilder]:
        if param in self.builders:
            return self.builders[param]
        raise exceptions.SocaException(
            error_code=errorcodes.GENERAL_ERROR,
            message=f'job param builder not found for param: {param}',
        )

    @property
    def job_params(self) -> SocaJobParams:
        return self._job_params

    @property
    def provisioning_options(self) -> SocaJobProvisioningOptions:
        return self._provisioning_options

    @property
    def params(self) -> Dict[str, Any]:
        return self._params

    @property
    def soca_context(self) -> ideascheduler.AppContext:
        return self._soca_context

    def has_queue(self) -> bool:
        return self._queue_profile is not None

    @property
    def queue(self) -> Optional[HpcQueueProfile]:
        return self._queue_profile

    def is_failed(self, *params: str) -> bool:
        for param in params:
            if param in self._failed_params:
                return True
        return False

    def add_validation_entry(self, param: str, message: str):
        if self.is_failed(param):
            return
        self.validation_results.append(
            JobValidationResultEntry(error_code=param, message=message)
        )
        self._failed_params.add(param)


class SocaJobBuilder:
    def __init__(
        self,
        context: ideascheduler.AppContext,
        params: Dict[str, Any],
        queue_profile: HpcQueueProfile = None,
        stack_uuid: Optional[str] = None,
    ):
        self._context = JobParamsBuilderContext(
            context=context,
            params=params,
            queue_profile=queue_profile,
            stack_uuid=stack_uuid,
        )

    def validate(self) -> JobValidationResult:
        for builder in self._context.builders.values():
            builder.validate()
        return JobValidationResult(results=self._context.validation_results)

    def build(self) -> Tuple[SocaJobParams, SocaJobProvisioningOptions]:
        for builder in self._context.builders.values():
            builder.apply()
        return self._context.job_params, self._context.provisioning_options

    @staticmethod
    def _get_param_info(param_name: str) -> Optional[JobParameterInfo]:
        if param_name not in JOB_PARAM_INFO:
            return None

        info = JOB_PARAM_INFO[param_name]

        name = info.name
        title = info.title
        description = info.description

        if title is None and description is None:
            return None

        if name is None:
            return JobParameterInfo(
                name='[computed]', title=title, description=description
            )

        if name in (
            constants.JOB_OPTION_TAGS,
            constants.JOB_OPTION_KEEP_FOREVER,
            constants.JOB_OPTION_TERMINATE_WHEN_IDLE,
        ):
            return None

        return info

    def _fsx_lustre_debug_entries(
        self, builder: FsxLustreParamBuilder
    ) -> List[JobValidationDebugEntry]:
        result = []

        fsx_lustre_params = [
            constants.JOB_PARAM_FSX_LUSTRE,
            constants.JOB_PARAM_FSX_LUSTRE_EXISTING_FSX,
            constants.JOB_PARAM_FSX_LUSTRE_S3_BACKEND,
            constants.JOB_PARAM_FSX_LUSTRE_DEPLOYMENT_TYPE,
            constants.JOB_PARAM_FSX_LUSTRE_SIZE,
            constants.JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT,
            constants.JOB_PARAM_FSX_LUSTRE_IMPORT_PATH,
            constants.JOB_PARAM_FSX_LUSTRE_EXPORT_PATH,
        ]

        fsx_lustre_user = builder.get()
        if fsx_lustre_user is None:
            fsx_lustre_user = SocaFSxLustreConfig(enabled=False)
        fsx_lustre_job = self._context.job_params.fsx_lustre
        fsx_lustre_default = builder.default(s3_backend=fsx_lustre_job.s3_backend)

        for job_param in fsx_lustre_params:
            info = self._get_param_info(param_name=job_param)
            if info is None:
                continue

            if (
                fsx_lustre_user.enabled is False
                and job_param != constants.JOB_PARAM_FSX_LUSTRE
            ):
                continue

            title = info.title
            name = info.name
            description = info.description
            valid = builder.validate()

            if job_param != constants.JOB_PARAM_FSX_LUSTRE:
                fsx_lustre_key = job_param.replace('fsx_lustre_', '')
            else:
                fsx_lustre_key = 'enabled'

            user_value = getattr(fsx_lustre_user, fsx_lustre_key, None)
            job_value = getattr(fsx_lustre_job, fsx_lustre_key, None)
            if job_value and job_param == constants.JOB_PARAM_FSX_LUSTRE_EXISTING_FSX:
                job_value = str(job_value).split('.')[0]
            default_value = getattr(fsx_lustre_default, fsx_lustre_key, None)

            result.append(
                JobValidationDebugEntry(
                    title=title,
                    name=name,
                    description=description,
                    valid=valid,
                    user_value=user_value,
                    job_value=job_value,
                    default_value=default_value,
                )
            )
        return result

    def debug(self) -> List[JobValidationDebugEntry]:
        result = []
        for builder in self._context.builders.values():
            job_param = builder.job_param
            info = self._get_param_info(param_name=job_param)
            if info is None:
                continue

            if job_param is constants.JOB_PARAM_FSX_LUSTRE:
                fsx_lustre_entries = self._fsx_lustre_debug_entries(builder=builder)
                result += fsx_lustre_entries
                continue

            name = info.name
            title = info.title
            description = info.description
            valid = builder.validate()
            user_value = builder.get()
            job_value = getattr(self._context.job_params, builder.job_param)
            default_value = builder.default()

            result.append(
                JobValidationDebugEntry(
                    title=title,
                    name=name,
                    description=description,
                    valid=valid,
                    user_value=user_value,
                    job_value=job_value,
                    default_value=default_value,
                )
            )

        return result
