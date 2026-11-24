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
    SocaJob,
    SocaComputeNodeState,
    ProvisioningCapacityInfo,
)
from typing import Optional, List
from pydantic import Field


class BatchCapacityResult(SocaBaseModel):
    provisioned_jobs: Optional[List[SocaJob]] = Field(default=None)
    unprovisioned_jobs: Optional[List[SocaJob]] = Field(default=None)
    capacity_info: Optional[ProvisioningCapacityInfo] = Field(default=None)


class BatchCapacityHelper:
    def __init__(
        self,
        context: ideascheduler.AppContext,
        jobs: List[SocaJob],
        provisioned_capacity: int,
    ):
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
            states=['pending', 'running'],
        )

        for instance in instances:
            self.total_instances += 1
            instance_capacity = self.job.weighted_capacity(
                instance_type=instance.instance_type
            )

            node = self._context.scheduler.find_node(
                hosts=instance.node_host_candidates
            )
            if node is None:
                self.pending_capacity += instance_capacity
                self.pending_instances += 1
                continue

            self.existing_capacity += instance_capacity

            if node.has_state(SocaComputeNodeState.FREE):
                self.idle_capacity += instance_capacity - len(node.jobs)
                self.idle_instances += 1
            elif node.has_state(
                SocaComputeNodeState.BUSY, SocaComputeNodeState.JOB_BUSY
            ):
                self.busy_capacity += instance_capacity
                self.busy_instances += 1
            elif not node.has_state(SocaComputeNodeState.OFFLINE):
                self.pending_capacity += instance_capacity
                self.pending_instances += 1

        self.available_capacity = self.idle_capacity

    def find_group_capacity(self):
        # the jobs already on this capacity plus the ones admitted now; jobs a check held
        # back, and the group's jobs in another profile, are not bought nodes (SCH-23)
        self.group_capacity = self._context.job_cache.get_desired_capacity(
            job_group=self.job.job_group,
            queue_profile=self.job.queue_type,
            provisioned_only=True,
        ) + sum(job.desired_capacity() for job in self.jobs)

    def admit_within_queue_limits(self):
        """
        the queue's max_running_jobs and max_provisioned_instances, applied to the batch as
        a whole: each job was checked alone against the count before the batch (SCH-24).
        jobs past a limit are returned unprovisioned, and the caller re-queues them.
        """
        params = None
        try:
            profile = self._context.queue_profiles.get_queue_profile(
                queue_profile_name=self.job.queue_type
            )
            params = profile.queue_management_params if profile else None
        except Exception as e:
            self._logger.warning(f'{self.job.log_tag} queue limits not read: {e}')
        if params is None:
            return
        max_jobs = int(params.max_running_jobs or 0)
        max_instances = int(params.max_provisioned_instances or 0)
        jobs_left = (
            max_jobs - self._context.job_cache.get_active_jobs(self.job.queue_type)
            if max_jobs > 0
            else None
        )
        instances_left = (
            max_instances
            - self._context.instance_cache.get_queue_profile_instance_count(
                self.job.queue_type
            )
            + self.idle_instances
            if max_instances > 0
            else None
        )
        admitted = []
        for job in self._jobs:
            nodes = job.desired_nodes()
            if (jobs_left is not None and jobs_left < 1) or (
                instances_left is not None and instances_left < nodes
            ):
                self.unprovisioned_jobs.append(job)
                continue
            admitted.append(job)
            if jobs_left is not None:
                jobs_left -= 1
            if instances_left is not None:
                instances_left -= nodes
        self._jobs = admitted

    def compute_batch_capacity(self):
        target_capacity = 0
        capacity = ProvisioningCapacityInfo()
        capacity.init_zero()

        self._logger.info(
            f'{self.job.log_tag} computing batch capacity: begin - '
            f'desired_group_capacity: {self.group_capacity}, '
            f'provisioned_capacity: {self.provisioned_capacity}, '
            f'available_capacity: {self.available_capacity}, '
            f'existing_instances: {self.total_instances}'
        )

        for current_job in self.jobs:
            current_job_desired_capacity = current_job.desired_capacity()
            if (
                self.available_capacity > 0
                and self.available_capacity >= current_job_desired_capacity
            ):
                self.available_capacity -= current_job_desired_capacity
            else:
                potential_target_capacity = (
                    self.group_capacity - self.available_capacity
                )
                target_capacity = max(
                    0, self.provisioned_capacity, potential_target_capacity
                )

            self.provisioned_jobs.append(current_job)

        if target_capacity > 0:
            comment = 'Updating target_capacity'
        else:
            if self.provisioned_capacity < self.group_capacity:
                target_capacity = self.group_capacity
                comment = f'Set target_capacity to match desired_group_capacity: {self.group_capacity}'
            else:
                comment = 'Re-using available_capacity'

        self._logger.info(
            f'{self.job.log_tag} computed batch capacity: end - '
            f'target_capacity - {target_capacity}, '
            f'provisioned_capacity: {self.provisioned_capacity}, '
            f'available_capacity: {self.available_capacity}, '
            f'existing_instances: {self.total_instances}'
        )

        return BatchCapacityResult(
            provisioned_jobs=self.provisioned_jobs,
            unprovisioned_jobs=self.unprovisioned_jobs,
            capacity_info=ProvisioningCapacityInfo(
                target_capacity=target_capacity,
                total_instances=self.total_instances,
                idle_instances=self.idle_instances,
                busy_instances=self.busy_instances,
                pending_instances=self.pending_instances,
                comment=comment,
            ),
        )

    def invoke(self) -> BatchCapacityResult:
        self.find_available_capacity()
        self.admit_within_queue_limits()
        if not self._jobs:
            return BatchCapacityResult(
                provisioned_jobs=[],
                unprovisioned_jobs=self.unprovisioned_jobs,
                capacity_info=ProvisioningCapacityInfo(
                    target_capacity=0, comment='queue limit reached'
                ),
            )
        self.find_group_capacity()
        return self.compute_batch_capacity()
