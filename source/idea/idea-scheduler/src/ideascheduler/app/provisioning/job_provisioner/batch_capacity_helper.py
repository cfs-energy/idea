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

from ideadatamodel import (
    SocaBaseModel,
    SocaJob, SocaComputeNodeState, ProvisioningCapacityInfo
)
from typing import Optional, List


class BatchCapacityResult(SocaBaseModel):
    provisioned_jobs: Optional[List[SocaJob]]
    unprovisioned_jobs: Optional[List[SocaJob]]
    capacity_info: Optional[ProvisioningCapacityInfo]


class BatchCapacityHelper:

    def __init__(self, context: ideascheduler.AppContext,
                 jobs: List[SocaJob],
                 provisioned_capacity: int):

        self._context = context
        self._logger = context.logger('batch-capacity')

        self._jobs = jobs
        self.provisioned_capacity = provisioned_capacity

        self.existing_capacity = 0
        self.idle_capacity = 0
        self.busy_capacity = 0
        self.pending_capacity = 0
        self.total_instances = 0
        self.idle_instances = 0
        self.busy_instances = 0
        self.pending_instances = 0
        self.group_capacity = 0
        self.available_capacity = 0

        self.provisioned_jobs = []
        self.unprovisioned_jobs = []

    @property
    def job(self) -> SocaJob:
        return self._jobs[0]

    @property
    def jobs(self) -> List[SocaJob]:
        return self._jobs

    @property
    def job_group(self) -> str:
        return self.job.get_job_group()

    def find_available_capacity(self):

        instances = self._context.instance_cache.list_compute_instances(
            cluster_name=self._context.cluster_name(),
            job_group=self.job_group,
            states=['pending', 'running']
        )

        for instance in instances:

            self.total_instances += 1
            instance_capacity = self.job.weighted_capacity(instance_type=instance.instance_type)

            node = self._context.scheduler.get_node(host=instance.private_dns_name)
            if node is None:
                self.pending_capacity += instance_capacity
                self.pending_instances += 1
                continue

            self.existing_capacity += instance_capacity

            if node.has_state(SocaComputeNodeState.FREE):
                self.idle_capacity += instance_capacity - len(node.jobs)
                self.idle_instances += 1
            elif node.has_state(SocaComputeNodeState.BUSY, SocaComputeNodeState.JOB_BUSY):
                self.busy_capacity += instance_capacity
                self.busy_instances += 1
            elif not node.has_state(SocaComputeNodeState.OFFLINE):
                self.pending_capacity += instance_capacity
                self.pending_instances += 1

        self.available_capacity = self.idle_capacity

    def find_group_capacity(self):
        self.group_capacity = self._context.job_cache.get_desired_capacity(job_group=self.job.job_group)

    def compute_batch_capacity(self):

        target_capacity = 0
        capacity = ProvisioningCapacityInfo()
        capacity.init_zero()

        self._logger.info(f'{self.job.log_tag} computing batch capacity: begin - '
                          f'desired_group_capacity: {self.group_capacity}, '
                          f'provisioned_capacity: {self.provisioned_capacity}, '
                          f'available_capacity: {self.available_capacity}, '
                          f'existing_instances: {self.total_instances}')

        index = None
        for index, current_job in enumerate(self.jobs):

            current_job_desired_capacity = current_job.desired_capacity()
            if self.available_capacity > 0 and self.available_capacity >= current_job_desired_capacity:
                self.available_capacity -= current_job_desired_capacity
            else:
                potential_target_capacity = self.group_capacity - self.available_capacity
                target_capacity = max(0, self.provisioned_capacity, potential_target_capacity)

            self.provisioned_jobs.append(current_job)

        if len(self.provisioned_jobs) < len(self.jobs):
            self.unprovisioned_jobs = self.jobs[index:]

        if target_capacity > 0:
            comment = 'Updating target_capacity'
        else:
            if self.provisioned_capacity < self.group_capacity:
                target_capacity = self.group_capacity
                comment = f'Set target_capacity to match desired_group_capacity: {self.group_capacity}'
            else:
                comment = 'Re-using available_capacity'

        self._logger.info(f'{self.job.log_tag} computed batch capacity: end - '
                          f'target_capacity - {target_capacity}, '
                          f'provisioned_capacity: {self.provisioned_capacity}, '
                          f'available_capacity: {self.available_capacity}, '
                          f'existing_instances: {self.total_instances}')

        return BatchCapacityResult(
            provisioned_jobs=self.provisioned_jobs,
            unprovisioned_jobs=self.unprovisioned_jobs,
            capacity_info=ProvisioningCapacityInfo(
                target_capacity=target_capacity,
                total_instances=self.total_instances,
                idle_instances=self.idle_instances,
                busy_instances=self.busy_instances,
                pending_instances=self.pending_instances,
                comment=comment
            )
        )

    def invoke(self) -> BatchCapacityResult:
        self.find_available_capacity()
        self.find_group_capacity()
        return self.compute_batch_capacity()
