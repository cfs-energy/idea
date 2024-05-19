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

from ideasdk.protocols import SocaContextProtocol
from ideadatamodel import SocaJob, SocaJobEstimatedBOMCost, SocaMemory, SocaMemoryUnit

TOTAL_SECONDS_IN_MONTH = 60 * 60 * 24 * 30


class PricingHelper:
    """
    Helps compute the cost "estimates" for running a Soca Job.

    Estimates are not accurate and may vary based on: (essentially a to do list for development)
        1. Reserved Instance Usage
        2. Savings Plans
        3. Updated configurations for Provisioned IOPS or Volume Types
        4. DataTransfer
        5. Existing FSX for Lustre and customizations.
        6. EFA Usage
        7. Shared jobs using the same instance.
        8. Jobs running on persistent capacity (KeepForever instances)
        9. TerminateWhenIdle is not yet accounted for in pricing.
        10. and more ...

    Developer Notes:
    This code is work in progress and needs additional development to get close to accurate pricing estimates.
    """

    def __init__(self, context: SocaContextProtocol, job: SocaJob, total_time_secs: int = None):
        self._context = context
        self.job = job
        self.total_time_secs = total_time_secs

    def config(self):
        return self._context.config()

    @property
    def root_storage_size(self) -> SocaMemory:
        return self.job.params.root_storage_size

    @property
    def scratch_storage_size(self) -> SocaMemory:
        return self.job.params.scratch_storage_size

    @property
    def scratch_storage_iops(self) -> int:
        return self.job.params.scratch_storage_iops

    @property
    def ec2_boot_penalty_seconds(self) -> int:
        return self.config().get_int('scheduler.cost_estimation.ec2_boot_penalty_seconds')

    @property
    def total_time_seconds(self) -> int:
        return self.total_time_secs + self.ec2_boot_penalty_seconds

    @property
    def total_time_hours(self) -> float:
        return self.total_time_seconds / 3600

    @property
    def root_storage_unit_price(self) -> float:
        return self.config().get_float('scheduler.cost_estimation.ebs_gp3_storage')

    @property
    def root_storage_quantity(self) -> float:
        gb_seconds = self.root_storage_size.int_val() * self.total_time_seconds
        gb_per_month = gb_seconds / TOTAL_SECONDS_IN_MONTH
        return round(gb_per_month * self.job.desired_nodes(), 2)

    @property
    def scratch_storage_unit_price(self) -> float:
        if self.scratch_storage_iops:
            return self.config().get_float('scheduler.cost_estimation.ebs_io1_storage')
        else:
            return self.config().get_float('scheduler.cost_estimation.ebs_gp3_storage')

    @property
    def scratch_storage_quantity(self) -> float:
        gb_seconds = self.scratch_storage_size.int_val() * self.total_time_seconds
        gb_per_month = gb_seconds / TOTAL_SECONDS_IN_MONTH
        return round(gb_per_month * self.job.desired_nodes(), 2)

    @property
    def scratch_storage_iops_unit_price(self) -> float:
        return self.config().get_int('scheduler.cost_estimation.provisioned_iops')

    @property
    def scratch_storage_iops_quantity(self) -> float:
        iops = self.scratch_storage_iops * self.job.desired_nodes() * self.total_time_seconds
        return round(iops / TOTAL_SECONDS_IN_MONTH)

    @property
    def is_fsx_lustre_enabled(self):
        return self.job.params.fsx_lustre.enabled

    @property
    def fsx_lustre_unit_price(self) -> float:
        return self.config().get_float('scheduler.cost_estimation.fsx_lustre')

    @property
    def fsx_lustre_size(self) -> SocaMemory:
        fsx_lustre = self.job.params.fsx_lustre
        if not fsx_lustre.enabled:
            return SocaMemory.zero(SocaMemoryUnit.GB)
        if fsx_lustre.size is not None:
            return fsx_lustre.size
        else:
            default_fsx_lustre_size = self.config().get_int('scheduler.cost_estimation.default_fsx_lustre_size')
            return SocaMemory(value=default_fsx_lustre_size, unit=SocaMemoryUnit.GB)

    @property
    def fsx_lustre_quantity(self) -> float:
        fsx_lustre_size = self.fsx_lustre_size
        if fsx_lustre_size == 0:
            return 0
        return fsx_lustre_size.int_val() * self.total_time_hours

    def get_instance_type_unit_price(self):
        return self._context.aws_util().get_ec2_instance_type_unit_price(
            instance_type=self.job.default_instance_type
        )

    @property
    def ec2_ondemand_price(self):
        unit_price = self.get_instance_type_unit_price()
        if unit_price is None:
            return 0
        price = self.total_time_hours * unit_price.ondemand
        return round(price * self.job.ondemand_nodes(), 2)

    @property
    def ec2_reserved_price(self):
        unit_price = self.get_instance_type_unit_price()
        reserved_rate = unit_price.reserved
        price = self.total_time_hours * reserved_rate
        return round(price * self.job.ondemand_nodes(), 2)

    @property
    def ec2_spot_unit_price(self) -> float:
        if self.job.is_mixed_capacity() or self.job.is_spot_capacity():
            if self.job.params.spot_price is None:
                unit_price = self.get_instance_type_unit_price()
                spot_price = unit_price.ondemand
            else:
                spot_price = self.job.params.spot_price.amount
            return spot_price
        return 0.0

    def compute_estimated_bom_cost(self) -> SocaJobEstimatedBOMCost:
        """
        HARDCODING ALERT! There's a lot of hardcoding here and needs additional comprehensive
        development and configuration for each service that is priced.

        These are estimated prices and actual prices may vary.
        """

        estimated_bom_cost = SocaJobEstimatedBOMCost()

        instance_type_unit_price = self.get_instance_type_unit_price()

        if self.job.ondemand_nodes() > 0:
            ondemand_usage_hours = self.total_time_hours * self.job.ondemand_nodes()
            estimated_bom_cost.add_line_item(
                title=f'Compute On-Demand: '
                      f'({self.job.ondemand_nodes()} x {self.job.default_instance_type})',
                service='aws.ec2',
                product=f'instance_type={self.job.default_instance_type},lifecycle=default',
                unit='per hour',
                quantity=ondemand_usage_hours,
                unit_price=instance_type_unit_price.ondemand
            )

            reserved_savings = instance_type_unit_price.ondemand - instance_type_unit_price.reserved
            estimated_bom_cost.add_savings(
                title=f'Compute Reserved: [1yr No Upfront] '
                      f'({self.job.ondemand_nodes()} x {self.job.default_instance_type})',
                service='aws.ec2',
                product=f'instance_type={self.job.default_instance_type},lifecycle=default',
                unit='per hour',
                quantity=ondemand_usage_hours,
                unit_price=reserved_savings
            )

        if self.job.spot_nodes() > 0:
            spot_usage_hours = self.total_time_hours * self.job.spot_nodes()
            estimated_bom_cost.add_line_item(
                title=f'Compute Spot: '
                      f'({self.job.spot_nodes()} x {self.job.default_instance_type})',
                service='aws.ec2',
                product=f'instance_type={self.job.default_instance_type},lifecycle=spot',
                unit='per hour',
                quantity=spot_usage_hours,
                unit_price=self.ec2_spot_unit_price
            )

        root_storage_unit_price = self.root_storage_unit_price
        root_storage_quantity = self.root_storage_quantity
        root_storage_size = self.root_storage_size
        estimated_bom_cost.add_line_item(
            title=f'Root: EBS gp3 ({self.job.desired_nodes()} x {root_storage_size})',
            service='aws.ebs',
            product='root_storage=gp3',
            unit='GB-month',
            quantity=root_storage_quantity,
            unit_price=root_storage_unit_price
        )

        if self.is_fsx_lustre_enabled:
            fsx_lustre_unit_price = self.fsx_lustre_unit_price
            fsx_lustre_quantity = self.fsx_lustre_quantity
            estimated_bom_cost.add_line_item(
                title=f'Scratch: FSx for Lustre ({self.fsx_lustre_size})',
                service='aws.fsx',
                product='scratch_storage=lustre',
                unit='GB-month',
                quantity=fsx_lustre_quantity,
                unit_price=fsx_lustre_unit_price
            )
        elif self.scratch_storage_size > 0:
            scratch_storage_unit_price = self.scratch_storage_unit_price
            scratch_storage_quantity = self.scratch_storage_quantity
            scratch_storage_size = self.scratch_storage_size
            if self.scratch_storage_iops > 0:
                estimated_bom_cost.add_line_item(
                    title=f'Scratch: EBS io1 ({self.job.desired_nodes()} x {scratch_storage_size})',
                    service='aws.ebs',
                    product=f'scratch_storage=io1',
                    unit='GB-month',
                    quantity=scratch_storage_quantity,
                    unit_price=scratch_storage_unit_price
                )

                scratch_storage_iops_unit_price = self.scratch_storage_iops_unit_price
                scratch_storage_iops_quantity = self.scratch_storage_iops_quantity
                scratch_storage_iops = self.scratch_storage_iops
                estimated_bom_cost.add_line_item(
                    title=f'Scratch: Provisioned IOPS ({self.job.desired_nodes()} x {scratch_storage_iops})',
                    service='aws.ebs',
                    product=f'scratch_storage_iops={scratch_storage_iops_quantity}',
                    unit='IOPS-month',
                    quantity=scratch_storage_iops_quantity,
                    unit_price=scratch_storage_iops_unit_price
                )
            else:
                title = f'Scratch: EBS gp3 ({self.job.desired_nodes()} x {scratch_storage_size})'
                product = 'gp3'
                estimated_bom_cost.add_line_item(
                    title=title,
                    service='aws.ebs',
                    product=f'scratch_storage={product}',
                    unit='GB-month',
                    quantity=scratch_storage_quantity,
                    unit_price=scratch_storage_unit_price
                )

        return estimated_bom_cost
