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

__all__ = (
    'SocaJobState',
    'SocaJobPlacementArrangement',
    'SocaJobPlacementSharing',
    'SocaJobPlacement',
    'SocaSpotAllocationStrategy',
    'SocaFSxLustreConfig',
    'SocaJobLicenseAsk',
    'SocaJobParams',
    'SocaInstanceTypeOptions',
    'SocaJobProvisioningOptions',
    'SocaJobEstimatedBOMCostLineItem',
    'SocaJobEstimatedBOMCost',
    'SocaCapacityType',
    'SocaScalingMode',
    'SocaJobExecutionResourcesUsed',
    'SocaJobExecutionRun',
    'SocaJobExecution',
    'SocaJobExecutionHost',
    'SocaJobNotifications',
    'SocaJob',
    'SocaComputeNodeState',
    'SocaSchedulerInfo',
    'OpenPBSInfo',
    'SocaComputeNodeResources',
    'SocaComputeNodeSharing',
    'SocaComputeNode',
    'SocaQueueManagementParams',
    'SocaQueueMode',
    'SocaQueueStats',
    'SocaQueue',
    'JobValidationResultEntry',
    'JobValidationResult',
    'JobValidationDebugEntry',
    'JobParameterInfo',
    'JobMetrics',
    'JobGroupMetrics',
    'ProvisioningQueueMetrics',
    'ProvisioningStatus',
    'DryRunOption',
    'JobUpdate',
    'JobUpdates',
    'ProvisioningCapacityInfo',
    'SocaJobEstimatedBudgetUsage',
    'QueuedJob',
    'LimitCheckResult',
    'HpcApplication',
    'HpcQueueProfile',
    'HpcLicenseResource',
)

from ideadatamodel import (
    SocaBaseModel,
    SocaMemory,
    SocaAmount,
    exceptions,
    EC2Instance,
    SocaUserInputModuleMetadata,
    Project,
)

from ideadatamodel.model_utils import ModelUtils

from typing import Optional, List, Dict, Tuple, Any, Set, Union
from pydantic import Field
from enum import Enum
import hashlib
import os
from datetime import datetime
import arrow


class SocaJobState(str, Enum):
    TRANSITION = 'transition'
    QUEUED = 'queued'
    HELD = 'held'
    WAITING = 'waiting'
    RUNNING = 'running'
    EXIT = 'exit'
    SUBJOB_EXPIRED = 'subjob_expired'
    SUBJOB_BEGUN = 'subjob_begun'
    MOVED = 'moved'
    FINISHED = 'finished'
    SUSPENDED = 'suspended'


class SocaJobPlacementArrangement(str, Enum):
    FREE = 'free'
    PACK = 'pack'
    SCATTER = 'scatter'
    VSCATTER = 'vscatter'


class SocaJobPlacementSharing(str, Enum):
    EXCL = 'excl'
    SHARED = 'shared'
    EXCLHOST = 'exclhost'
    VSCATTER = 'vscatter'


class SocaJobPlacement(SocaBaseModel):
    arrangement: Optional[SocaJobPlacementArrangement] = Field(default=None)
    sharing: Optional[SocaJobPlacementSharing] = Field(default=None)
    grouping: Optional[str] = Field(default=None)


class SocaSpotAllocationStrategy(str, Enum):
    CAPACITY_OPTIMIZED = 'capacity-optimized'
    LOWEST_PRICE = 'lowest-price'
    DIVERSIFIED = 'diversified'

    def __str__(self):
        return self.value

    def asg_value(self) -> str:
        if self == self.CAPACITY_OPTIMIZED:
            return 'capacity-optimized'
        elif self == self.LOWEST_PRICE:
            return 'lowest-price'
        elif self == self.DIVERSIFIED:
            return 'capacity-optimized'

    def spot_value(self) -> str:
        if self == self.CAPACITY_OPTIMIZED:
            return 'capacityOptimized'
        elif self == self.LOWEST_PRICE:
            return 'lowestPrice'
        elif self == self.DIVERSIFIED:
            return 'diversified'

    @staticmethod
    def valid_values() -> List[str]:
        return ['capacity-optimized', 'lowest-price', 'diversified']

    @staticmethod
    def resolve(
        value: Optional[str], default=None
    ) -> Optional['SocaSpotAllocationStrategy']:
        if ModelUtils.is_empty(value):
            return default
        token = value.strip().lower()
        if token is None or len(token) == 0:
            return None
        if token in ['lowest-price', 'lowestprice']:
            return SocaSpotAllocationStrategy.LOWEST_PRICE
        if token in ['diversified']:
            return SocaSpotAllocationStrategy.DIVERSIFIED
        if token in ['capacityoptimized', 'capacity-optimized', 'optimized']:
            return SocaSpotAllocationStrategy.CAPACITY_OPTIMIZED
        return None


class SocaFSxLustreConfig(SocaBaseModel):
    enabled: Optional[bool] = Field(default=None)
    existing_fsx: Optional[str] = Field(default=None)
    s3_backend: Optional[str] = Field(default=None)
    import_path: Optional[str] = Field(default=None)
    export_path: Optional[str] = Field(default=None)
    deployment_type: Optional[str] = Field(default=None)
    per_unit_throughput: Optional[int] = Field(default=None)
    size: Optional[SocaMemory] = Field(default=None)

    def as_job_submit_params(self) -> Dict:
        result = {}

        if self.enabled is False:
            result['fsx_lustre'] = 'False'
            return result

        if self.existing_fsx is not None:
            result['fsx_lustre'] = self.existing_fsx
            return result

        if self.s3_backend is not None:
            fsx_lustre = self.s3_backend
            if self.export_path is not None:
                fsx_lustre += f'+{self.export_path}'
                if self.import_path is not None:
                    fsx_lustre += f'+{self.import_path}'
            result['fsx_lustre'] = fsx_lustre
        if self.deployment_type is not None:
            result['fsx_lustre_deployment_type'] = self.deployment_type
            if self.deployment_type == 'persistent_1':
                result['fsx_lustre_per_unit_throughput'] = self.per_unit_throughput
        if self.size is not None:
            result['fsx_lustre_size'] = int(self.size.value)

        if 'fsx_lustre' not in result:
            result['fsx_lustre'] = 'True'

        return result

    @staticmethod
    def allowed_deployment_types():
        return ['scratch_1', 'scratch_2', 'persistent_1', 'persistent_2']

    @staticmethod
    def allowed_per_unit_throughputs():
        return [50, 100, 200]

    @staticmethod
    def allowed_sizes_gb():
        # Begin from 2400 (the smallest multiple of 2400 after 1200)
        # Continue by incrementing by 2400 and stop before 100800
        sizes = list(range(2400, 100800, 2400))

        # Manually add 1200 to the list
        sizes.insert(0, 1200)

        # Return the completed list
        return sizes

    @staticmethod
    def is_allowed_deployment_type(value: str) -> bool:
        return value in SocaFSxLustreConfig.allowed_deployment_types()

    @staticmethod
    def is_allowed_per_unit_throughput(value: int) -> bool:
        return value in SocaFSxLustreConfig.allowed_per_unit_throughputs()

    @staticmethod
    def is_allowed_size(value: int) -> bool:
        return value in SocaFSxLustreConfig.allowed_sizes_gb()


class SocaJobLicenseAsk(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    count: Optional[int] = Field(default=None)


class SocaJobParams(SocaBaseModel):
    nodes: Optional[int] = Field(default=None)
    cpus: Optional[int] = Field(default=None)
    memory: Optional[SocaMemory] = Field(default=None)
    gpus: Optional[int] = Field(default=None)
    mpiprocs: Optional[int] = Field(default=None)
    walltime: Optional[str] = Field(default=None)
    base_os: Optional[str] = Field(default=None)
    instance_ami: Optional[str] = Field(default=None)
    instance_types: Optional[List[str]] = Field(default=None)
    force_reserved_instances: Optional[bool] = Field(default=None)
    spot: Optional[bool] = Field(default=None)
    spot_price: Optional[SocaAmount] = Field(default=None)
    spot_allocation_count: Optional[int] = Field(default=None)
    spot_allocation_strategy: Optional[SocaSpotAllocationStrategy] = Field(default=None)
    subnet_ids: Optional[List[str]] = Field(default=None)
    security_groups: Optional[List[str]] = Field(default=None)
    instance_profile: Optional[str] = Field(default=None)
    keep_ebs_volumes: Optional[bool] = Field(default=None)
    root_storage_size: Optional[SocaMemory] = Field(default=None)
    enable_scratch: Optional[bool] = Field(default=None)
    scratch_provider: Optional[str] = Field(default=None)
    scratch_storage_size: Optional[SocaMemory] = Field(default=None)
    scratch_storage_iops: Optional[int] = Field(default=None)
    fsx_lustre: Optional[SocaFSxLustreConfig] = Field(default=None)
    enable_instance_store: Optional[bool] = Field(default=None)
    enable_efa_support: Optional[bool] = Field(default=None)
    enable_ht_support: Optional[bool] = Field(default=None)
    enable_placement_group: Optional[bool] = Field(default=None)
    enable_system_metrics: Optional[bool] = Field(default=None)
    enable_anonymous_metrics: Optional[bool] = Field(default=None)
    licenses: Optional[List[SocaJobLicenseAsk]] = Field(default=None)
    compute_stack: Optional[str] = Field(default=None)
    stack_id: Optional[str] = Field(default=None)
    job_group: Optional[str] = Field(default=None)
    job_started_email_template: Optional[str] = Field(default=None)
    job_completed_email_template: Optional[str] = Field(default=None)
    custom_params: Optional[Dict[str, Optional[str]]] = Field(default=None)


class SocaInstanceTypeOptions(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    weighted_capacity: Optional[int] = Field(default=None)
    cpu_options_supported: Optional[bool] = Field(default=None)
    default_core_count: Optional[int] = Field(default=None)
    default_vcpu_count: Optional[int] = Field(default=None)
    default_threads_per_core: Optional[int] = Field(default=None)
    threads_per_core: Optional[int] = Field(default=None)
    memory: Optional[SocaMemory] = Field(default=None)
    ebs_optimized: Optional[bool] = Field(default=None)


class SocaJobProvisioningOptions(SocaBaseModel):
    """
    These are job provisioning parameters, that satisfy any of these cases:

    > computed dynamically based on JobParams provided by the user
    > are not defined as resources in the scheduler
    > are primarily used while provisioning capacity for the job
    > values from soca-configuration in AWS Secrets

    If any of these values can be potentially be submitted by the user during job submission,
    these values must be pulled up to JobParams.
    """

    keep_forever: Optional[bool] = Field(default=None)
    terminate_when_idle: Optional[int] = Field(default=None)
    ebs_optimized: Optional[bool] = Field(default=None)
    spot_fleet_iam_role_arn: Optional[str] = Field(default=None)
    compute_fleet_instance_profile_arn: Optional[str] = Field(default=None)
    apps_fs_dns: Optional[str] = Field(default=None)
    apps_fs_provider: Optional[str] = Field(default=None)
    data_fs_dns: Optional[str] = Field(default=None)
    data_fs_provider: Optional[str] = Field(default=None)
    es_endpoint: Optional[str] = Field(default=None)
    stack_uuid: Optional[str] = Field(default=None)
    s3_bucket: Optional[str] = Field(default=None)
    s3_bucket_install_folder: Optional[str] = Field(default=None)
    scheduler_private_dns: Optional[str] = Field(default=None)
    scheduler_tcp_port: Optional[int] = Field(default=None)
    ssh_key_pair: Optional[str] = Field(default=None)
    auth_provider: Optional[str] = Field(default=None)
    tags: Optional[Dict[str, str]] = Field(default=None)
    anonymous_metrics_lambda_arn: Optional[str] = Field(default=None)
    instance_types: Optional[List[SocaInstanceTypeOptions]] = Field(default=None)


class SocaJobEstimatedBOMCostLineItem(SocaBaseModel):
    title: Optional[str] = Field(default=None)
    service: Optional[str] = Field(default=None)
    product: Optional[str] = Field(default=None)
    quantity: Optional[float] = Field(default=None)
    unit: Optional[str] = Field(default=None)
    unit_price: Optional[SocaAmount] = Field(default=None)
    total_price: Optional[SocaAmount] = Field(default=None)

    def replace(self, with_: 'SocaJobEstimatedBOMCostLineItem'):
        self.title = with_.title
        self.service = with_.service
        self.product = with_.product
        self.quantity = with_.quantity
        self.unit = with_.unit
        self.unit_price = with_.unit_price
        self.total_price = with_.total_price

    def update(self, with_: 'SocaJobEstimatedBOMCostLineItem'):
        self.quantity = with_.quantity
        total_amount = self.quantity * self.unit_price.amount
        self.total_price = SocaAmount(amount=total_amount)


class SocaJobEstimatedBOMCost(SocaBaseModel):
    line_items: Optional[List[SocaJobEstimatedBOMCostLineItem]] = Field(default=None)
    line_items_total: Optional[SocaAmount] = Field(default=None)
    savings: Optional[List[SocaJobEstimatedBOMCostLineItem]] = Field(default=None)
    savings_total: Optional[SocaAmount] = Field(default=None)
    total: Optional[SocaAmount] = Field(default=None)

    def _compute_total(self):
        # line items
        line_items_total = self.line_items_total
        if line_items_total is None:
            line_items_total = SocaAmount()
            self.line_items_total = line_items_total
        else:
            line_items_total.amount = 0

        for line_item in self.line_items:
            line_items_total.amount += line_item.total_price.amount

        # savings
        savings_total = None
        if self.savings is not None:
            savings_total = self.savings_total
            if savings_total is None:
                savings_total = SocaAmount()
                self.savings_total = savings_total
            else:
                savings_total.amount = 0

            for savings_line_item in self.savings:
                savings_total.amount += savings_line_item.total_price.amount

        # grand total
        total = self.total
        if total is None:
            total = SocaAmount()
            self.total = total
        else:
            total.amount = 0

        total.amount = line_items_total.amount
        if savings_total is not None:
            total.amount -= savings_total.amount

    @staticmethod
    def _get_line_item(
        line_items: Optional[List[SocaJobEstimatedBOMCostLineItem]],
        service: str,
        product: str,
    ) -> Optional[Tuple[int, Optional[SocaJobEstimatedBOMCostLineItem]]]:
        if line_items is None:
            return -1, None
        for index, line_item in enumerate(line_items):
            if service != line_item.service:
                continue
            if product != line_item.product:
                continue
            return index, line_item
        return -1, None

    @staticmethod
    def _build_line_item(**kwargs) -> SocaJobEstimatedBOMCostLineItem:
        title = kwargs.get('title', None)
        service = kwargs.get('service', None)
        product = kwargs.get('product', None)
        quantity = kwargs.get('quantity', None)
        unit = kwargs.get('unit', None)
        unit_price = kwargs.get('unit_price', None)
        total_price = kwargs.get('total_price', None)

        if title is None:
            raise exceptions.invalid_params('line_item.title is required')
        if service is None:
            raise exceptions.invalid_params('line_item.service is required')
        if product is None:
            raise exceptions.invalid_params('line_item.product is required')
        if quantity is None:
            raise exceptions.invalid_params('line_item.quantity is required')

        if unit_price is None:
            raise exceptions.invalid_params('line_item.unit_price is required')
        if isinstance(unit_price, float) or isinstance(unit_price, int):
            unit_price = SocaAmount(amount=unit_price)

        if total_price is None:
            total_price = unit_price.amount * quantity
            total_price = SocaAmount(amount=total_price)
        if isinstance(total_price, float) or isinstance(total_price, int):
            total_price = SocaAmount(amount=total_price)

        return SocaJobEstimatedBOMCostLineItem(
            title=title,
            service=service,
            product=product,
            quantity=quantity,
            unit=unit,
            unit_price=unit_price,
            total_price=total_price,
        )

    def get_line_item(
        self, service: str, product: str
    ) -> Optional[SocaJobEstimatedBOMCostLineItem]:
        index, line_item = self._get_line_item(
            line_items=self.line_items, service=service, product=product
        )
        return line_item

    def get_savings_line_item(
        self, service: str, product: str
    ) -> Optional[SocaJobEstimatedBOMCostLineItem]:
        index, line_item = self._get_line_item(
            line_items=self.savings, service=service, product=product
        )
        return line_item

    def add_line_item(self, update=False, **kwargs):
        line_item = self._build_line_item(**kwargs)

        line_items = self.line_items
        if line_items is None:
            line_items = []
            self.line_items = line_items

        existing = self.get_line_item(
            service=line_item.service, product=line_item.product
        )

        if existing:
            if update:
                existing.update(line_item)
            else:
                existing.replace(line_item)
        else:
            line_items.append(line_item)

        self._compute_total()

    def remove_line_item(self, service: str, product: str) -> bool:
        index, line_item = self._get_line_item(
            line_items=self.line_items, service=service, product=product
        )

        if index == -1:
            return False

        del self.line_items[index]

        self._compute_total()

        return True

    def add_savings(self, update=False, **kwargs):
        line_item = self._build_line_item(**kwargs)

        savings = self.savings
        if savings is None:
            savings = []
            self.savings = savings

        existing = self.get_savings_line_item(
            service=line_item.service, product=line_item.product
        )

        if existing:
            if update:
                existing.update(line_item)
            else:
                existing.replace(line_item)
        else:
            savings.append(line_item)

        self._compute_total()

    def remove_savings(self, service: str, product: str):
        index, line_item = self._get_line_item(
            line_items=self.savings, service=service, product=product
        )

        if index == -1:
            return False

        del self.savings[index]

        self._compute_total()

        return True

    def savings_percent(self) -> float:
        if self.line_items_total is None:
            return 0
        if self.savings_total is None:
            return 0
        percent = (self.savings_total.amount / self.line_items_total.amount) * 100
        return round(percent, 2)


class SocaCapacityType(str, Enum):
    ONDEMAND = 'on-demand'
    SPOT = 'spot'
    MIXED = 'mixed'

    def __str__(self):
        if self == self.ONDEMAND:
            return 'on-demand'
        elif self == self.SPOT:
            return 'spot'
        elif self == self.MIXED:
            return 'mixed'

    def __repr__(self):
        return str(self)

    @staticmethod
    def resolve(value: Optional[str], default=None) -> Optional['SocaCapacityType']:
        if value is None:
            return default
        _token = value.lower().strip()
        if _token in ('ondemand', 'on-demand', 'on_demand'):
            return SocaCapacityType.ONDEMAND
        elif _token == 'spot':
            return SocaCapacityType.SPOT
        elif _token == 'mixed':
            return SocaCapacityType.MIXED
        else:
            return default


class SocaScalingMode(str, Enum):
    SINGLE_JOB = 'single-job'
    BATCH = 'batch'

    def __str__(self):
        return self.value

    def __repr__(self):
        return str(self)

    @staticmethod
    def default():
        return SocaScalingMode.SINGLE_JOB

    @staticmethod
    def resolve(value: Optional[str], default=None) -> Optional['SocaScalingMode']:
        if value is None:
            return default
        token = value.lower().strip()
        if token in ('single-job', 'single_job'):
            return SocaScalingMode.SINGLE_JOB
        elif token in ('batch', 'multiple_jobs'):
            return SocaScalingMode.BATCH
        return default


class SocaJobExecutionResourcesUsed(SocaBaseModel):
    cpu_time_secs: Optional[int] = Field(default=None)
    memory: Optional[SocaMemory] = Field(default=None)
    virtual_memory: Optional[SocaMemory] = Field(default=None)
    cpus: Optional[int] = Field(default=None)
    gpus: Optional[int] = Field(default=None)
    cpu_percent: Optional[int] = Field(default=None)
    # todo - add additional resources after further testing.


class SocaJobExecutionRun(SocaBaseModel):
    run_id: Optional[str] = Field(default=None)
    start: Optional[datetime] = Field(default=None)
    end: Optional[datetime] = Field(default=None)
    exit_code: Optional[int] = Field(default=None)
    status: Optional[str] = Field(default=None)
    resources_used: Optional[SocaJobExecutionResourcesUsed] = Field(default=None)


class SocaJobExecution(SocaBaseModel):
    run_count: Optional[int] = Field(default=None)
    runs: Optional[List[SocaJobExecutionRun]] = Field(default=None)

    def get_or_create_run(self, run_id: str) -> Optional[SocaJobExecutionRun]:
        if self.runs is None:
            self.runs = []
        for run in self.runs:
            if run.run_id == run_id:
                return run
        if self.run_count is None:
            self.run_count = 1
        else:
            self.run_count += 1

        # do not support more than 10 job runs, to prevent out of memory
        # increment run count and return None
        if self.run_count > 10:
            return None

        run = SocaJobExecutionRun(run_id=run_id)
        self.runs.append(run)
        return run


class SocaJobExecutionHost(SocaBaseModel):
    host: Optional[str] = Field(default=None)
    instance_id: Optional[str] = Field(default=None)
    instance_type: Optional[str] = Field(default=None)
    capacity_type: Optional[SocaCapacityType] = Field(default=None)
    tenancy: Optional[str] = Field(default=None)
    reservation: Optional[str] = Field(default=None)
    execution: Optional[SocaJobExecution] = Field(default=None)


class SocaJobNotifications(SocaBaseModel):
    started: Optional[bool] = Field(default=None)
    completed: Optional[bool] = Field(default=None)
    subjobs: Optional[bool] = Field(default=None)
    job_started_email_template: Optional[str] = Field(default=None)
    job_completed_email_template: Optional[str] = Field(default=None)


class SocaJobEstimatedBudgetUsage(SocaBaseModel):
    budget_name: Optional[str] = Field(default=None)
    budget_limit: Optional[SocaAmount] = Field(default=None)
    actual_spend: Optional[SocaAmount] = Field(default=None)
    forecasted_spend: Optional[SocaAmount] = Field(default=None)
    job_usage_percent: Optional[float] = Field(default=None)
    job_usage_percent_with_savings: Optional[float] = Field(default=None)


class SocaJob(SocaBaseModel):
    cluster_name: Optional[str] = Field(default=None)
    cluster_version: Optional[str] = Field(default=None)
    job_id: Optional[str] = Field(default=None)
    job_uid: Optional[str] = Field(default=None)
    job_group: Optional[str] = Field(default=None)
    project: Optional[str] = Field(default=None)
    name: Optional[Union[int, str, float]] = Field(default=None)
    queue: Optional[str] = Field(default=None)
    queue_type: Optional[str] = Field(default=None)
    scaling_mode: Optional[SocaScalingMode] = Field(default=None)
    owner: Optional[str] = Field(default=None)
    owner_email: Optional[str] = Field(default=None)
    state: Optional[SocaJobState] = Field(default=None)
    exit_status: Optional[int] = Field(default=None, strict=False)
    provisioned: Optional[bool] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    queue_time: Optional[datetime] = Field(default=None)
    provisioning_time: Optional[datetime] = Field(default=None)
    start_time: Optional[datetime] = Field(default=None)
    end_time: Optional[datetime] = Field(default=None)
    total_time_secs: Optional[int] = Field(default=None)
    comment: Optional[str] = Field(default=None)
    debug: Optional[bool] = Field(default=None)
    capacity_added: Optional[bool] = Field(default=None)
    params: Optional[SocaJobParams] = Field(default=None)
    provisioning_options: Optional[SocaJobProvisioningOptions] = Field(default=None)
    estimated_budget_usage: Optional[SocaJobEstimatedBudgetUsage] = Field(default=None)
    estimated_bom_cost: Optional[SocaJobEstimatedBOMCost] = Field(default=None)
    execution_hosts: Optional[List[SocaJobExecutionHost]] = Field(default=None)
    notifications: Optional[SocaJobNotifications] = Field(default=None)

    def get_total_time_seconds(self) -> Optional[int]:
        if self.total_time_secs is not None:
            return self.total_time_secs

        if self.start_time is not None and self.end_time is not None:
            delta = self.end_time - self.start_time
            return delta.seconds

        if self.params is None:
            return None

        if self.params.walltime is None:
            return None

        return ModelUtils.walltime_to_seconds(self.params.walltime)

    def get_or_create_execution_host(self, host: str) -> SocaJobExecutionHost:
        if self.execution_hosts is None:
            self.execution_hosts = []
        for execution_host in self.execution_hosts:
            if execution_host.host == host:
                return execution_host
        execution_host = SocaJobExecutionHost(host=host)
        self.execution_hosts.append(execution_host)
        return execution_host

    def is_execution_complete(self) -> bool:
        if self.execution_hosts is None:
            return False
        total = len(self.execution_hosts)
        current = 0
        for execution_host in self.execution_hosts:
            execution = execution_host.execution
            if execution is None:
                continue
            if execution.runs is None or len(execution.runs) == 0:
                continue
            for run in execution.runs:
                if run.status is None:
                    continue
                current += 1
        return current >= total

    @property
    def log_tag(self) -> str:
        try:
            tag = f'(JobId: {self.job_id}, Owner: {self.owner}'
            if ModelUtils.is_not_empty(self.project):
                tag += f', Project: {self.project}'
            if self.is_shared_capacity():
                tag += f', JobGroup: {self.get_job_group()}'
            tag += ')'
            return tag
        except:  # noqa
            return f'(JobId: {self.job_id})'

    def desired_capacity(self) -> int:
        if self.params is None:
            raise exceptions.invalid_job('params not found.')
        return self.desired_nodes() * ModelUtils.get_as_int(self.params.cpus, 1)

    def desired_nodes(self) -> int:
        if self.params is None:
            raise exceptions.invalid_job('params not found.')
        return ModelUtils.get_as_int(self.params.nodes, 1)

    def try_extract_job_id_as_int(self) -> Optional[int]:
        if self.job_id is None:
            return None
        if ModelUtils.is_int(self.job_id):
            return int(self.job_id)
        int_val = ''
        for c in self.job_id:
            if not c.isdigit():
                break
            int_val += c
        if ModelUtils.is_int(int_val):
            return int(int_val)
        return None

    def is_provisioned(self) -> bool:
        """
        checks if the job has been provisioned.

        the word provisioned is quite overloaded here:
            1. is AWS capacity provisioned ?
            2. in case of OpenPBS, is computenode!=tbd ?
            3. if the user has provided a stack_id, but job has not been updated with computenode yet ?

        this method takes care of all these cases and returns the provisioning status of the job.

        SocaJob.is_provisioned is a computed property and is set True if:
            1. computenode != tbd at scheduler
            2. scheduler posts job related events only applicable after provisioning: eg. running and job status

        """
        if self.provisioned is not None:
            return ModelUtils.is_true(self.provisioned)
        if self.params is not None:
            if self.params.stack_id is None:
                return False
            if self.params.stack_id == 'tbd':
                return False
            return True
        return False

    def get_compute_stack(self) -> str:
        if self.params is None:
            raise exceptions.invalid_job('params not found')

        if (
            ModelUtils.is_not_empty(self.params.compute_stack)
            and self.params.compute_stack != 'tbd'
        ):
            return self.params.compute_stack

        def sanitize(token) -> str:
            return token.strip().lower().replace('-', '').replace('_', '')

        cluster_name = self.cluster_name

        capacity_type = sanitize(str(self.capacity_type()))

        queue_type = self.queue_type

        if self.is_persistent_capacity():
            identifier = f'keepforever-{sanitize(self.provisioning_options.stack_uuid)}'
        elif self.is_shared_capacity():
            identifier = self.get_job_group()
        else:
            identifier = self.job_id

        if self.is_persistent_capacity():
            return f'{cluster_name}-{identifier}'
        else:
            return f'{cluster_name}-{queue_type}-{capacity_type}-{identifier}'

    def get_job_group(self) -> str:
        if self.job_group is not None:
            return self.job_group

        if self.params is None:
            raise exceptions.invalid_job('params not found')

        if self.is_provisioned():
            if self.params.job_group is None:
                raise exceptions.invalid_job('params.job_group not found')
            return self.params.job_group

        if self.params.job_group is not None:
            return self.params.job_group

        cluster_name = self.cluster_name
        if cluster_name is None:
            raise exceptions.invalid_job('cluster_name not found')

        queue_type = self.queue_type
        if queue_type is None:
            raise exceptions.invalid_job('queue_type not found')

        queue = self.queue
        if queue is None:
            raise exceptions.invalid_job('queue not found')

        instance_types = self.params.instance_types
        if instance_types is None or len(instance_types) == 0:
            raise exceptions.invalid_job('params.instance_types not found')

        instance_ami = self.params.instance_ami
        if instance_ami is None or ModelUtils.is_empty(instance_ami):
            raise exceptions.invalid_job('params.instance_ami not found')

        enable_ht_support = self.params.enable_ht_support
        if enable_ht_support is None:
            raise exceptions.invalid_job('params.enable_ht_support not found')
        enable_ht_support = str(enable_ht_support).lower()

        capacity_type = str(self.capacity_type())

        tokens = [
            cluster_name,
            queue_type,
            queue,
            *instance_types,
            instance_ami,
            enable_ht_support,
            capacity_type,
        ]

        digest = hashlib.shake_256(':'.join(tokens).encode('utf-8')).hexdigest(4)
        return f'g{digest}'

    def capacity_type(self) -> SocaCapacityType:
        if self.params is None:
            raise exceptions.invalid_job('params not found')

        spot = ModelUtils.get_as_bool(self.params.spot, False)
        if spot:
            spot_allocation_count = ModelUtils.get_as_int(
                self.params.spot_allocation_count, 0
            )
            if spot_allocation_count == 0:
                return SocaCapacityType.SPOT
            else:
                return SocaCapacityType.MIXED
        else:
            return SocaCapacityType.ONDEMAND

    def is_spot_capacity(self) -> Optional[bool]:
        capacity_type = self.capacity_type()
        if capacity_type is None:
            return None
        return capacity_type == SocaCapacityType.SPOT

    def is_ondemand_capacity(self) -> Optional[bool]:
        capacity_type = self.capacity_type()
        if capacity_type is None:
            return None
        return capacity_type == SocaCapacityType.ONDEMAND

    def is_mixed_capacity(self) -> Optional[bool]:
        capacity_type = self.capacity_type()
        if capacity_type is None:
            return None
        return capacity_type == SocaCapacityType.MIXED

    def ondemand_nodes(self) -> int:
        if self.params is None:
            return 0
        if self.is_spot_capacity():
            return 0
        nodes = ModelUtils.get_as_int(self.params.nodes, 0)
        spot_allocation_count = ModelUtils.get_as_int(
            self.params.spot_allocation_count, 0
        )
        return nodes - spot_allocation_count

    def ondemand_capacity(self) -> int:
        return self.ondemand_nodes() * self.weighted_capacity()

    def spot_nodes(self) -> int:
        if self.params is None:
            return 0
        if self.params.nodes is None:
            return 0
        capacity_type = self.capacity_type()
        if capacity_type == SocaCapacityType.MIXED:
            return self.params.spot_allocation_count
        elif capacity_type == SocaCapacityType.SPOT:
            return self.params.nodes
        else:
            return 0

    def spot_capacity(self) -> int:
        return self.spot_nodes() * self.weighted_capacity()

    def default_vcpus(self) -> int:
        instance_type_option = self.default_instance_type_option
        return instance_type_option.default_vcpu_count

    def weighted_capacity(self, instance_type: Optional[str] = None) -> int:
        if instance_type is None:
            instance_type_option = self.default_instance_type_option
        else:
            instance_type_option = self.get_instance_type_option(
                instance_type=instance_type
            )
        return instance_type_option.weighted_capacity

    def _memory_values(self) -> Optional[List[SocaMemory]]:
        options = self.provisioning_options
        if options is None:
            return None
        if options.instance_types is None:
            return None

        result = []
        for instance_type in options.instance_types:
            memory = instance_type.memory
            if memory is None:
                continue
            result.append(memory)

        if len(result) == 0:
            return None

        return result

    def min_memory(self) -> Optional[SocaMemory]:
        values = self._memory_values()
        if values is None:
            return None
        return min(values)

    def max_memory(self) -> Optional[SocaMemory]:
        values = self._memory_values()
        if values is None:
            return None
        return max(values)

    def has_licenses(self) -> bool:
        if self.params is None:
            return False
        licenses = self.params.licenses
        if licenses is None:
            return False
        return len(licenses) > 0

    def is_ephemeral_capacity(self) -> bool:
        """
        ephemeral capacity is short-lived and is terminated as soon as job execution is complete.
        > if keep_forever is True, capacity will never be deleted and has to be manually deleted
        > if terminate_when_idle > 0, the capacity will be deleted after {terminate_when_idle} minutes of inactivity.
            - inactivity is defined as the time the node is in FREE status
        """
        if self.provisioning_options is None:
            return False
        if ModelUtils.is_true(self.provisioning_options.keep_forever):
            return False
        terminate_when_idle = ModelUtils.get_as_int(
            self.provisioning_options.terminate_when_idle, 0
        )
        if terminate_when_idle > 0:
            return False
        return True

    def is_shared_capacity(self) -> bool:
        return not self.is_ephemeral_capacity()

    def is_persistent_capacity(self) -> bool:
        if self.provisioning_options is None:
            return False
        return ModelUtils.is_true(self.provisioning_options.keep_forever)

    def total_licenses(self) -> int:
        license_total = 0
        if not self.has_licenses():
            return license_total
        for license_ in self.params.licenses:
            license_total += license_.count
        return license_total

    @property
    def is_multiple_instance_types(self) -> bool:
        if self.params is None:
            raise exceptions.invalid_job('params not found')
        instance_types = self.params.instance_types
        if ModelUtils.is_empty(instance_types):
            raise exceptions.invalid_job('params.instance_types not found')
        return len(instance_types) > 1

    @property
    def is_hyper_threading_enabled(self) -> bool:
        if self.params is None:
            raise exceptions.invalid_job('params not found')
        enable_ht_support = self.params.enable_ht_support
        if enable_ht_support is None:
            raise exceptions.invalid_job('params.enable_ht_support not found')
        return ModelUtils.is_true(enable_ht_support)

    @property
    def is_hyper_threading_disabled(self) -> bool:
        return not self.is_hyper_threading_enabled

    def get_params(self) -> SocaJobParams:
        if self.params is None:
            raise exceptions.invalid_job('params not found')
        return self.params

    def get_provisioning_options(self) -> SocaJobProvisioningOptions:
        if self.provisioning_options is None:
            raise exceptions.invalid_job('provisioning_options not found')
        return self.provisioning_options

    @property
    def default_instance_type(self) -> str:
        params = self.get_params()
        if ModelUtils.is_empty(params.instance_types):
            raise exceptions.invalid_job('params.instance_types not found')
        return params.instance_types[0]

    @property
    def default_instance_type_option(self) -> SocaInstanceTypeOptions:
        options = self.get_provisioning_options()
        if ModelUtils.is_empty(options.instance_types):
            raise exceptions.invalid_job(
                'provisioning_options.instance_types not found'
            )
        return options.instance_types[0]

    def get_instance_type_option(self, instance_type: str):
        options = self.get_provisioning_options()
        if ModelUtils.is_empty(options.instance_types):
            raise exceptions.invalid_job(
                'provisioning_options.instance_types not found'
            )
        for option in options.instance_types:
            if option.name == instance_type:
                return option
        raise exceptions.invalid_job(
            f'{instance_type} not found in provisioning_options.instance_types[]'
        )

    def get_spot_price(self) -> Optional[SocaAmount]:
        if self.params is None:
            return None
        spot = ModelUtils.get_as_bool(self.params.spot, False)
        if not spot:
            return None
        return self.params.spot_price

    def get_fsx_lustre_export_path(self) -> Optional[str]:
        fsx_lustre_config = self.params.fsx_lustre
        if fsx_lustre_config is None:
            return None
        if ModelUtils.is_false(fsx_lustre_config.enabled):
            return None
        if ModelUtils.is_empty(fsx_lustre_config.s3_backend):
            return None
        if ModelUtils.is_not_empty(fsx_lustre_config.export_path):
            return fsx_lustre_config.export_path
        if self.is_ephemeral_capacity():
            export_path = f'{fsx_lustre_config.s3_backend}/{self.cluster_name}-fsxoutput/job-{self.job_id}'
        else:
            export_path = f'{fsx_lustre_config.s3_backend}/{self.cluster_name}-fsxoutput/job-{self.job_group}'
        return export_path

    def get_fsx_lustre_import_path(self) -> Optional[str]:
        fsx_lustre_config = self.params.fsx_lustre
        if fsx_lustre_config is None:
            return None
        if ModelUtils.is_false(fsx_lustre_config.enabled):
            return None
        if ModelUtils.is_empty(fsx_lustre_config.s3_backend):
            return None
        if ModelUtils.is_not_empty(fsx_lustre_config.import_path):
            return fsx_lustre_config.import_path
        return fsx_lustre_config.s3_backend


class SocaComputeNodeState(str, Enum):
    BUSY = 'busy'
    DOWN = 'down'
    FREE = 'free'
    OFFLINE = 'offline'
    JOB_BUSY = 'job-busy'
    JOB_EXCLUSIVE = 'job-exclusive'
    PROVISIONING = 'provisioning'
    RESV_EXCLUSIVE = 'resv-exclusive'
    STALE = 'stale'
    STALE_UNKNOWN = 'stale-unknown'
    UNRESOLVABLE = 'unresolvable'
    WAIT_PROVISIONING = 'wait-provisioning'
    INITIALIZING = 'initializing'


class SocaSchedulerInfo(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    version: Optional[str] = Field(default=None)


class OpenPBSInfo(SocaSchedulerInfo):
    mom_private_dns: Optional[str] = Field(default=None)
    mom_port: Optional[int] = Field(default=None)


class SocaComputeNodeResources(SocaBaseModel):
    cpus: Optional[int] = Field(default=None)
    gpus: Optional[int] = Field(default=None)
    memory: Optional[SocaMemory] = Field(default=None)


class SocaComputeNodeSharing(str, Enum):
    DEFAULT_EXCL = 'default-excl'
    DEFAULT_EXCLHOST = 'default-exlchost'
    DEFAULT_SHARED = 'default-shared'
    FORCE_EXCL = 'force-excl'
    FORCE_EXCLHOST = 'force-exclhost'
    IGNORE_EXCL = 'ignore-excl'


class SocaComputeNode(SocaBaseModel):
    host: Optional[str] = Field(default=None)
    cluster_name: Optional[str] = Field(default=None)
    cluster_version: Optional[str] = Field(default=None)
    states: Optional[List[SocaComputeNodeState]] = Field(default=None)
    queue_type: Optional[str] = Field(default=None)
    queue: Optional[str] = Field(default=None)
    provisioning_time: Optional[datetime] = Field(default=None)
    last_used_time: Optional[datetime] = Field(default=None)
    last_state_changed_time: Optional[datetime] = Field(default=None)
    availability_zone: Optional[str] = Field(default=None)
    subnet_id: Optional[str] = Field(default=None)
    instance_id: Optional[str] = Field(default=None)
    instance_type: Optional[str] = Field(default=None)
    instance_ami: Optional[str] = Field(default=None)
    instance_profile: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    scheduler_info: Optional[SocaSchedulerInfo] = Field(default=None)
    sharing: Optional[SocaComputeNodeSharing] = Field(default=None)
    job_id: Optional[str] = Field(default=None)
    job_group: Optional[str] = Field(default=None)
    scaling_mode: Optional[SocaScalingMode] = Field(default=None)
    keep_forever: Optional[bool] = Field(default=None)
    terminate_when_idle: Optional[int] = Field(default=None)
    compute_stack: Optional[str] = Field(default=None)
    stack_id: Optional[str] = Field(default=None)
    lifecycle: Optional[str] = Field(default=None)
    tenancy: Optional[str] = Field(default=None)
    spot_fleet_request: Optional[str] = Field(default=None)
    auto_scaling_group: Optional[str] = Field(default=None)
    spot: Optional[bool] = Field(default=None)
    spot_price: Optional[SocaAmount] = Field(default=None)
    base_os: Optional[str] = Field(default=None)
    enable_placement_group: Optional[bool] = Field(default=None)
    enable_ht_support: Optional[bool] = Field(default=None)
    keep_ebs_volumes: Optional[bool] = Field(default=None)
    root_storage_size: Optional[SocaMemory] = Field(default=None)
    scratch_storage_size: Optional[SocaMemory] = Field(default=None)
    scratch_storage_iops: Optional[int] = Field(default=None)
    enable_efa_support: Optional[bool] = Field(default=None)
    force_reserved_instances: Optional[bool] = Field(default=None)
    enable_system_metrics: Optional[bool] = Field(default=None)
    enable_anonymous_metrics: Optional[bool] = Field(default=None)
    fsx_lustre: Optional[SocaFSxLustreConfig] = Field(default=None)
    resources_available: Optional[SocaComputeNodeResources] = Field(default=None)
    resources_assigned: Optional[SocaComputeNodeResources] = Field(default=None)
    launch_time: Optional[datetime] = Field(default=None)
    termination_time: Optional[datetime] = Field(default=None)
    terminated: Optional[bool] = Field(default=None)
    jobs: Optional[List[str]] = Field(default=None)

    def __eq__(self, other: 'SocaComputeNode'):
        return self.host == other.host

    def __str__(self):
        if self.is_shared_capacity():
            s = f'JobGroup: {self.job_group}, '
        else:
            s = f'JobId: {self.job_id}, '
        s += f'Host: {self.host}, '
        s += f'InstanceId: {self.instance_id}'
        if self.is_provisioned():
            s += f', ComputeStack: {self.compute_stack}'
        return s

    def __repr__(self):
        return str(self)

    def __hash__(self):
        return hash(self.host)

    def seconds_since_launch(self) -> int:
        delta = arrow.utcnow() - self.launch_time
        return delta.seconds

    def has_state(self, *state: SocaComputeNodeState) -> bool:
        if self.states is None:
            return False
        for node_state in state:
            if node_state in self.states:
                return True
        return False

    def is_shared_capacity(self) -> bool:
        if ModelUtils.is_true(self.keep_forever):
            return True
        if ModelUtils.get_as_int(self.terminate_when_idle, 0) > 0:
            return True
        return False

    @property
    def log_tag(self):
        return str(self)

    def is_current_cluster(self, cluster_name: str) -> bool:
        node_cluster_name = self.cluster_name
        if ModelUtils.is_empty(node_cluster_name):
            return False
        if ModelUtils.is_empty(cluster_name):
            return False
        return cluster_name == node_cluster_name

    def is_valid_idea_compute_node(self) -> bool:
        if ModelUtils.is_empty(self.cluster_name):
            return False
        if ModelUtils.is_empty(self.queue_type):
            return False
        if ModelUtils.is_empty(self.instance_id):
            return False
        return True

    def is_provisioned(self) -> bool:
        if self.compute_stack is None:
            return False
        if self.compute_stack == 'tbd':
            return False
        return True

    def is_ready(self) -> bool:
        return self.has_state(
            SocaComputeNodeState.FREE,
            SocaComputeNodeState.BUSY,
            SocaComputeNodeState.JOB_BUSY,
        )

    def get_spot_price(self) -> Optional[SocaAmount]:
        spot = ModelUtils.get_as_bool(self.spot, False)
        if not spot:
            return None
        return self.spot_price

    @staticmethod
    def from_ec2_instance(instance: EC2Instance) -> Optional['SocaComputeNode']:
        if not instance.is_valid_idea_compute_node():
            return None

        auto_scaling_group = None
        spot_fleet_request = None
        if instance.soca_capacity_type == SocaCapacityType.SPOT:
            spot_fleet_request = instance.aws_ec2spot_fleet_request_id
        else:
            auto_scaling_group = instance.aws_autoscaling_group_name

        return SocaComputeNode(
            host=instance.private_dns_name,
            cluster_name=instance.soca_cluster_name,
            queue=instance.soca_job_queue,
            queue_type=instance.soca_queue_type,
            instance_id=instance.instance_id,
            instance_type=instance.instance_type,
            availability_zone=instance.placement_availability_zone,
            subnet_id=instance.subnet_id,
            stack_id=instance.aws_cloudformation_stack_id,
            compute_stack=instance.soca_compute_stack,
            scaling_mode=SocaScalingMode.resolve(
                instance.soca_scaling_mode, default=SocaScalingMode.SINGLE_JOB
            ),
            job_id=instance.soca_job_id,
            job_group=instance.soca_job_group,
            instance_ami=instance.image_id,
            instance_profile=instance.iam_instance_profile_id,
            lifecycle=instance.instance_lifecycle,
            tenancy=instance.placement_tenancy,
            spot_fleet_request=spot_fleet_request,
            auto_scaling_group=auto_scaling_group,
            keep_forever=instance.soca_keep_forever,
            terminate_when_idle=instance.soca_terminate_when_idle,
            launch_time=instance.launch_time.datetime,
        )


class SocaQueueManagementParams(SocaBaseModel):
    max_running_jobs: Optional[int] = Field(default=None)
    max_provisioned_instances: Optional[int] = Field(default=None)
    max_provisioned_capacity: Optional[int] = Field(default=None)
    wait_on_any_job_with_license: Optional[bool] = Field(default=None)
    allowed_instance_types: Optional[List[str]] = Field(default=None)
    excluded_instance_types: Optional[List[str]] = Field(default=None)
    restricted_parameters: Optional[List[str]] = Field(default=None)
    allowed_security_groups: Optional[List[str]] = Field(default=None)
    allowed_instance_profiles: Optional[List[str]] = Field(default=None)

    def is_allowed_security_group(self, security_group: str) -> bool:
        if (
            self.allowed_security_groups is None
            or len(self.allowed_security_groups) == 0
        ):
            return True
        return security_group in self.allowed_security_groups

    def is_restricted_parameter(self, param_name: str):
        if self.restricted_parameters is None or len(self.restricted_parameters) == 0:
            return False
        return param_name in self.restricted_parameters

    def is_allowed_instance_profile(self, instance_profile: str):
        if (
            self.allowed_instance_profiles is None
            or len(self.allowed_instance_profiles) == 0
        ):
            return True
        return instance_profile in self.allowed_instance_profiles

    @staticmethod
    def _get_instance_families(
        instance_types: Optional[List[str]] = None,
    ) -> Optional[List[str]]:
        if instance_types is None:
            return None
        if len(instance_types) == 0:
            return None
        instance_families = []
        for instance_type in instance_types:
            if '.' in instance_type:
                continue
            instance_families.append(instance_type)
        if len(instance_families) == 0:
            return None
        return instance_families

    def _is_allowed_or_excluded_instance_type(
        self, instance_type: str, instance_types: Optional[List[str]]
    ) -> bool:
        check_type = instance_type in instance_types

        check_family = False
        instance_families = self._get_instance_families(instance_types)
        if instance_families is not None:
            instance_family = instance_type.split('.')[0]
            check_family = instance_family in instance_families

        return check_type or check_family

    def is_allowed_instance_type(self, instance_type) -> bool:
        if ModelUtils.is_empty(self.allowed_instance_types):
            return True
        return self._is_allowed_or_excluded_instance_type(
            instance_type=instance_type, instance_types=self.allowed_instance_types
        )

    def is_excluded_instance_type(self, instance_type) -> bool:
        if ModelUtils.is_empty(self.excluded_instance_types):
            return False
        return self._is_allowed_or_excluded_instance_type(
            instance_type=instance_type, instance_types=self.excluded_instance_types
        )


class SocaQueueMode(str, Enum):
    FIFO = ('fifo',)
    FAIRSHARE = 'fairshare'
    LICENSE_OPTIMIZED = 'license-optimized'

    def __str__(self):
        return self.value

    def __repr__(self):
        return str(self)

    @staticmethod
    def resolve(value: Optional[str], default=None) -> Optional['SocaQueueMode']:
        if ModelUtils.is_empty(value):
            return default
        _token = value.strip().lower()
        if _token == 'fairshare':
            return SocaQueueMode.FAIRSHARE
        elif _token == 'fifo':
            return SocaQueueMode.FIFO
        elif _token == 'license-optimized':
            return SocaQueueMode.LICENSE_OPTIMIZED
        return default


class SocaQueueStats(SocaBaseModel):
    transit: Optional[int] = Field(default=None)
    queued: Optional[int] = Field(default=None)
    held: Optional[int] = Field(default=None)
    waiting: Optional[int] = Field(default=None)
    running: Optional[int] = Field(default=None)
    exiting: Optional[int] = Field(default=None)
    begun: Optional[int] = Field(default=None)


class SocaQueue(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    started: Optional[bool] = Field(default=None)
    total_jobs: Optional[int] = Field(default=None)
    stats: Optional[SocaQueueStats] = Field(default=None)


class LimitCheckResult(SocaBaseModel):
    success: Optional[bool] = Field(default=None)
    limit_type: Optional[str] = Field(default=None)
    queue_threshold: Optional[int] = Field(default=None)
    queue_current: Optional[int] = Field(default=None)
    group_threshold: Optional[int] = Field(default=None)
    group_current: Optional[int] = Field(default=None)

    def __bool__(self):
        return ModelUtils.get_as_bool(self.success, False)

    def ok(self) -> 'LimitCheckResult':
        self.success = True
        return self

    def fail(self) -> 'LimitCheckResult':
        self.success = False
        return self

    def __str__(self):
        if self.success:
            s = f'{self.limit_type} limit ok. '
        else:
            s = f'{self.limit_type} limit exceeded. '
        if (
            self.queue_threshold is not None
            and 0 < self.queue_threshold < self.queue_current
        ):
            s += f'QueueLimit: {self.queue_threshold}'
        if (
            self.group_threshold is not None
            and 0 < self.group_threshold < self.group_current
        ):
            s += f'GroupLimit: {self.group_threshold}'
        return s

    def is_group_limit(self) -> bool:
        if self.group_threshold is None:
            return False
        return self.group_current > self.group_threshold

    def is_queue_limit(self) -> bool:
        if self.queue_threshold is None:
            return False
        return self.queue_current > self.queue_threshold


class JobValidationResultEntry(SocaBaseModel):
    error_code: Optional[str] = Field(default=None)
    message: Optional[str] = Field(default=None)


class JobValidationResult(SocaBaseModel):
    results: Optional[List[JobValidationResultEntry]] = Field(default=None)

    def is_valid(self):
        return len(self.results) == 0

    def __bool__(self):
        return self.is_valid()

    def __str__(self):
        s = ''
        for entry in self.results:
            if entry.error_code is None and entry.message is None:
                s += f'{os.linesep}'
                continue
            s += f'[{entry.error_code}] {entry.message}{os.linesep}'
        return s

    def as_string(self, pad_left: int = 0):
        s = str(self)
        if pad_left == 0:
            return s
        ret = ''
        for line in s.splitlines(keepends=True):
            ret += line.rjust(pad_left, ' ')
        return ret


class JobValidationDebugEntry(SocaBaseModel):
    title: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    valid: Optional[bool] = Field(default=None)
    user_value: Optional[Any] = Field(default=None)
    job_value: Optional[Any] = Field(default=None)
    default_value: Optional[Any] = Field(default=None)


class JobParameterInfo(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    provider_names: Optional[Dict[str, str]] = Field(default=None)


LicenseName = str
LicenseCount = int
LicenseEntries = Dict[LicenseName, LicenseCount]
JobId = str
JobGroup = str
ProvisioningQueue = str
LicenseName = str
LicenseCount = int
InstanceType = str
ReservedInstanceCount = int
ReservedInstanceUsage = Dict[InstanceType, ReservedInstanceCount]


class JobMetrics(SocaBaseModel):
    active_jobs: int = 0
    desired_capacity: int = 0

    def get_value(self, metric_name) -> Optional[Union[int, float, LicenseEntries]]:
        return getattr(self, metric_name)

    def negate(self):
        self.active_jobs = -self.active_jobs
        self.desired_capacity = -self.desired_capacity

    def get_delta(self, other: 'JobMetrics') -> 'JobMetrics':
        """
        computes the deltas for JobMetrics.
        """

        return JobMetrics(
            active_jobs=other.active_jobs - self.active_jobs,
            desired_capacity=other.desired_capacity - self.desired_capacity,
        )

    def apply_delta(self, delta: 'JobMetrics'):
        self.active_jobs += delta.active_jobs
        self.desired_capacity += delta.desired_capacity


JobMetricEntries = Dict[JobId, JobMetrics]


class JobGroupMetrics(SocaBaseModel):
    total: JobMetrics = JobMetrics()
    jobs: JobMetricEntries = dict()


JobGroupEntries = Dict[JobGroup, JobGroupMetrics]


class ProvisioningQueueMetrics(SocaBaseModel):
    total: JobMetrics = JobMetrics()
    groups: JobGroupEntries = dict()


QueueEntries = Dict[ProvisioningQueue, ProvisioningQueueMetrics]


class ProvisioningStatus(str, Enum):
    NOT_PROVISIONED = 'NotProvisioned'
    IN_PROGRESS = 'InProgress'
    COMPLETED = 'Completed'
    TIMEOUT = 'Timeout'
    FAILED = 'Failed'
    DELETE_IN_PROGRESS = 'DeleteInProgress'


class DryRunOption(str, Enum):
    DEFAULT = 'true'
    JSON_JOB = 'json:job'
    JSON_BOM = 'json:bom'
    JSON_BUDGET = 'json:budget'
    JSON_QUOTA = 'json:quota'
    JSON_QUEUE = 'json:queue'
    NOTIFICATION_EMAIL = 'notification:email'
    DEBUG = 'debug'

    @staticmethod
    def resolve(value: str = None) -> Optional['DryRunOption']:
        if value is None:
            return None
        if isinstance(value, bool):
            if value:
                return DryRunOption.DEFAULT
            return None

        _token = value.lower().strip()

        if _token == 'false':
            return None
        elif _token in ('json', 'json:job'):
            return DryRunOption.JSON_JOB
        elif _token == 'json:bom':
            return DryRunOption.JSON_BOM
        elif _token == 'json:budget':
            return DryRunOption.JSON_BUDGET
        elif _token == 'json:quota':
            return DryRunOption.JSON_QUOTA
        elif _token == 'json:queue':
            return DryRunOption.JSON_QUEUE
        elif _token == 'notifications:email':
            return DryRunOption.NOTIFICATION_EMAIL
        return DryRunOption.DEFAULT


class JobUpdate(SocaBaseModel):
    queue: Optional[str] = Field(default=None)
    owner: Optional[str] = Field(default=None)
    job_id: Optional[str] = Field(default=None)
    timestamp: Optional[datetime] = Field(default=None)

    def __hash__(self):
        return hash(f'{self.owner}.{self.job_id}')

    def is_applicable(self, now: arrow.Arrow, delay_secs: float = 4) -> bool:
        delta = now - self.timestamp
        return delta.seconds >= delay_secs


class JobUpdates(SocaBaseModel):
    queued: Set[JobUpdate]
    modified: Set[JobUpdate]
    running: Set[JobUpdate]


class ProvisioningCapacityInfo(SocaBaseModel):
    desired_capacity: Optional[int] = Field(default=None)
    group_capacity: Optional[int] = Field(default=None)
    target_capacity: Optional[int] = Field(default=None)
    existing_capacity: Optional[int] = Field(default=None)
    provisioned_capacity: Optional[int] = Field(default=None)
    idle_capacity: Optional[int] = Field(default=None)
    busy_capacity: Optional[int] = Field(default=None)
    pending_capacity: Optional[int] = Field(default=None)
    total_instances: Optional[int] = Field(default=None)
    idle_instances: Optional[int] = Field(default=None)
    busy_instances: Optional[int] = Field(default=None)
    pending_instances: Optional[int] = Field(default=None)
    max_provisioned_instances: Optional[int] = Field(default=None)
    max_provisioned_capacity: Optional[int] = Field(default=None)
    comment: Optional[str] = Field(default=None)
    error_code: Optional[str] = Field(default=None)

    def __str__(self):
        s = ''
        if self.target_capacity > 0:
            s += f'target_capacity: {self.target_capacity}, '
        else:
            s += 'target_capacity: no-change, '
        s += f'existing_instances: {self.total_instances}'

        if self.max_provisioned_instances:
            s += f', max_provisioned_instances: {self.max_provisioned_instances}'
        if self.max_provisioned_capacity:
            s += f', max_provisioned_capacity: {self.max_provisioned_instances}'

        return s

    def is_capacity_increased(self) -> bool:
        return self.capacity_increased > 0

    @property
    def capacity_increased(self) -> int:
        return self.target_capacity - self.provisioned_capacity

    def init_zero(self):
        self.desired_capacity = 0
        self.group_capacity = 0
        self.target_capacity = 0
        self.existing_capacity = 0
        self.provisioned_capacity = 0
        self.idle_capacity = 0
        self.busy_capacity = 0
        self.pending_capacity = 0
        self.total_instances = 0
        self.idle_instances = 0
        self.busy_instances = 0
        self.pending_instances = 0
        self.max_provisioned_instances = 0
        self.max_provisioned_capacity = 0


class QueuedJob(SocaBaseModel):
    priority: Optional[int] = Field(default=None)
    job_id: str
    job_group: str
    deleted: Optional[bool] = Field(default=None)
    processed: Optional[bool] = Field(default=None)
    capacity_added: Optional[bool] = Field(default=None)

    def __eq__(self, other: 'QueuedJob'):
        return self.job_id == other.job_id

    def __lt__(self, other: 'QueuedJob'):
        return self.priority < other.priority

    def __hash__(self):
        return hash(self.job_id)

    def __str__(self):
        return f'JobId: {self.job_id}, Priority: {self.priority}'

    def __repr__(self):
        return str(self)


class HpcApplication(SocaBaseModel):
    application_id: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    thumbnail_url: Optional[str] = Field(default=None)
    thumbnail_data: Optional[str] = Field(default=None)
    form_template: Optional[SocaUserInputModuleMetadata] = Field(default=None)
    job_script_interpreter: Optional[str] = Field(default=None)
    job_script_type: Optional[str] = Field(default=None)
    job_script_template: Optional[str] = Field(default=None)
    projects: Optional[List[Project]] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)


class HpcQueueProfile(SocaBaseModel):
    queue_profile_id: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    projects: Optional[List[Project]] = Field(default=None)
    queues: Optional[List[str]] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    queue_mode: Optional[SocaQueueMode] = Field(default=None)
    scaling_mode: Optional[SocaScalingMode] = Field(default=None)
    terminate_when_idle: Optional[int] = Field(default=None)
    keep_forever: Optional[bool] = Field(default=None)
    stack_uuid: Optional[str] = Field(default=None)
    queue_management_params: Optional[SocaQueueManagementParams] = Field(default=None)
    default_job_params: Optional[SocaJobParams] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)
    # real time params sourced from JobProvisioningQueue
    status: Optional[str] = Field(default=None)
    limit_info: Optional[LimitCheckResult] = Field(default=None)
    queue_size: Optional[int] = Field(default=None)

    def is_enabled(self) -> bool:
        return ModelUtils.get_as_bool(self.enabled, False)


class HpcLicenseResource(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None)
    availability_check_cmd: Optional[str] = Field(default=None)
    availability_check_status: Optional[str] = Field(default=None)
    reserved_count: Optional[int] = Field(default=None)
    available_count: Optional[int] = Field(default=None)
    created_on: Optional[datetime] = Field(default=None)
    updated_on: Optional[datetime] = Field(default=None)
