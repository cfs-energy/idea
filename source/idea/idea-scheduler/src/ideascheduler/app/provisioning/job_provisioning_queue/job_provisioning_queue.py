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
    exceptions, errorcodes, SocaQueueMode, SocaJob, SocaJobState,
    QueuedJob, LimitCheckResult, HpcQueueProfile
)

from ideasdk.utils import Utils
from ideascheduler.app.app_protocols import JobProvisioningQueueProtocol

from typing import Optional, Dict, List
from queue import Queue, PriorityQueue
from threading import RLock, Event
import queue
import time
import arrow

LIMIT_TYPE_MAX_PROVISIONED_INSTANCES = 'max_provisioned_instances'
LIMIT_TYPE_MAX_RUNNING_JOBS = 'max_running_jobs'
LIMIT_TYPE_MAX_PROVISIONED_CAPACITY = 'max_provisioned_capacity'

CLEAR_PROCESSED_JOBS_INTERVAL_SECS = 10
PROVISIONING_RETRY_BACKOFF_MAX = 10

LIMIT_CHECK_OK = 0
LIMIT_CHECK_NOT_OK_QUEUE = 1
LIMIT_CHECK_NOT_OK_JOB_GROUP = 2


class JobProvisioningQueueEmpty(Exception):
    pass


class RoundRobinPriorityQueueEntry:
    def __init__(self, priority_queue: Queue, group: str):
        self.group = group
        self.priority_queue = priority_queue


class RoundRobinPriorityQueue:
    """
    A round robin PriorityQueue data structure.

    For ephemeral capacity queues, one 1 (one) PriorityQueue will ever be active.

    For shared capacity queues:
    * no. of PriorityQueues == No. of active JobGroups.
    * this simplifies implementation to enforce job order for jobs within a job group.

    """

    def __init__(self, maxsize_per_queue=10000):
        self._queue_map: Dict[str, RoundRobinPriorityQueueEntry] = {}
        self._queues: List[RoundRobinPriorityQueueEntry] = []
        self._lock = RLock()
        self._index = 0
        self._maxsize_per_queue = maxsize_per_queue

    def get(self, timeout: float = 1) -> QueuedJob:

        if len(self._queues) == 0:
            time.sleep(timeout)
            raise queue.Empty()

        with self._lock:
            try:
                entry = self._queues[self._index]

                queued_job = entry.priority_queue.get(timeout=timeout)

                self._index += 1
                self._index = self._index % len(self._queues)

                return queued_job

            except queue.Empty as e:
                entry = self._queues[self._index]
                del self._queue_map[entry.group]
                del self._queues[self._index]
                self._index -= 1
                self._index = max(0, self._index)

                raise e

    def put(self, item: QueuedJob):
        with self._lock:
            if item.job_group in self._queue_map:
                entry = self._queue_map[item.job_group]
            else:
                entry = RoundRobinPriorityQueueEntry(
                    priority_queue=PriorityQueue(maxsize=self._maxsize_per_queue),
                    group=item.job_group
                )
                self._queue_map[item.job_group] = entry
                self._queues.append(entry)

            entry.priority_queue.put(item=item)

    def qsize(self, key: Optional[str] = None) -> int:
        """
        returns approximate pending items in queue using qsize. (not reliable!)
        :return: pending items
        """
        if Utils.is_not_empty(key):
            with self._lock:
                if key in self._queues:
                    return self._queue_map[key].priority_queue.qsize()
                return 0
        else:
            with self._lock:
                pending = 0
                for entry in self._queues:
                    pending += entry.priority_queue.qsize()
                return pending


class JobProvisioningQueue(JobProvisioningQueueProtocol):
    """
    SOCA Job Provisioning Queue. Responsible for:
    * maintaining order or jobs being provisioned (fifo or fair-share)
    * checking limits: max instances, max jobs, max capacity at queue and group level.
    """

    def __init__(self, context: ideascheduler.AppContext, queue_profile: HpcQueueProfile):
        self._context = context
        self._logger = context.logger(name=f'job_provisioning_queue_{queue_profile.name}')
        self.queue_profile = queue_profile
        self.queue_type = queue_profile.name

        self._applicable_queues = set()
        for queue_name in self.queue_profile.queues:
            self._applicable_queues.add(queue_name)

        self._queue: RoundRobinPriorityQueue = RoundRobinPriorityQueue(maxsize_per_queue=10000)
        self._queued_jobs: Dict[str, QueuedJob] = {}
        self._clear_processed_jobs_timestamp = None

        self._lock = RLock()
        self._is_running = Event()

        self._provisioning_not_ready_retry_attempt = 0

        self._limit_flag_set: Optional[arrow.Arrow] = None
        self._limit_info: Optional[LimitCheckResult] = None

    def get_queue_order(self, queue_name: str) -> int:
        """
        if there are multiple queues defined in queue settings, such as [high, normal, low] -
        priority for provisioning of jobs these queues must be treated separately.

        :param queue_name: name of the queue
        :return: position of the queue in queue_types.queues,
        eg:
            1 - if the job being queued belongs to the 'high' priority queue
            3 - if the job being queued belongs to the 'low' priority queue
        """
        queue_order = 0
        for queue_name in self.queue_profile.queues:
            queue_order += 1
            if queue_name != queue_name:
                continue
            return queue_order

        raise exceptions.SocaException(
            error_code=errorcodes.GENERAL_ERROR,
            message=f'queue_name not found: {queue_name}'
        )

    @property
    def queue_mode(self) -> SocaQueueMode:
        queue_mode = self.queue_profile.queue_mode
        if queue_mode is None:
            return SocaQueueMode.FIFO
        return queue_mode

    def _compute_fairshare_score(self, job: SocaJob) -> int:
        running_jobs = self._context.job_cache.list_jobs(queue_type=self.queue_type, job_state=SocaJobState.RUNNING)
        queued_jobs = self._context.job_cache.list_jobs(queue_type=self.queue_type, job_state=SocaJobState.QUEUED)

        config_prefix = 'scheduler.job_provisioning.queue_mode.fair_share'
        start_score = self._context.config().get_int(f'{config_prefix}.start_score')
        penalty = self._context.config().get_int(f'{config_prefix}.running_job_penalty')

        score = start_score
        bonus_score = 0

        for running_job in running_jobs:
            if running_job.owner != job.owner:
                continue
            score += penalty

        for queued_job in queued_jobs:
            if queued_job.owner != job.owner:
                continue
            if queued_job.params.compute_stack is not None:
                bonus_score += penalty
            else:
                resource_count = job.params.nodes + job.total_licenses()
                score_type = self._context.config().get_string(f'{config_prefix}.score_type')

                if score_type in ('dynamic', 'linear'):
                    c1 = self._context.config().get_float(f'{config_prefix}.c1')
                    c2 = self._context.config().get_float(f'{config_prefix}.c2')
                    elapsed_time = Utils.current_time_ms() - (job.queue_time.timestamp() * 1000)
                    bonus_score += resource_count * ((c1 * elapsed_time / (1000 * 60 * 60 * 24)) ** c2)
                else:
                    bonus_score += 1

        return score + bonus_score

    def _get_job_priority(self, job: SocaJob) -> int:
        if self.queue_mode == SocaQueueMode.FAIRSHARE:
            score = self._compute_fairshare_score(job=job)
            priority = score * self.get_queue_order(queue_name=job.queue)
        else:
            timestamp = int(job.queue_time.timestamp())
            priority = timestamp * self.get_queue_order(queue_name=job.queue)

        # find a constant to break tie for jobs queued at the exact time.
        # job_id is a good candidate.
        job_id_int_val = job.try_extract_job_id_as_int()
        if job_id_int_val is not None:
            priority += job_id_int_val

        return priority

    def put(self, job: SocaJob, modified=False):
        """
        queues the job in the provisioning queue

        if an already queued job is queued again with modified=True, priority for the job will be re-evaluated,
        and any other deletion flags (eg. deletion) will be reset.

        :param job: the SocaJob to be queued
        :param modified: if the job was modified
        """

        # check if job to provisioned belongs in this queue
        if not self._is_queue_applicable(queue_name=job.queue):
            return

        # only allow jobs with QUEUED state to be provisioned
        state = job.state
        if state is None or state != SocaJobState.QUEUED:
            return

        # do not allow already provisioned jobs in queue
        if job.is_provisioned():
            return

        with self._lock:

            # if job is already queued, update priority and return
            if job.job_id in self._queued_jobs:
                # job is already queued, update priority and reset any flags
                queued_job = self._queued_jobs[job.job_id]
                queued_job.priority = self._get_job_priority(job=job)
                queued_job.deleted = None
                return

                # queue the job

            if job.is_ephemeral_capacity():
                job_group = self.queue_type
            else:
                job_group = job.get_job_group()

            queued_job = QueuedJob(
                priority=self._get_job_priority(job=job),
                job_id=job.job_id,
                job_group=job_group,
                deleted=None,
                capacity_added=job.capacity_added
            )
            self._queue.put(item=queued_job)
            self._queued_jobs[queued_job.job_id] = queued_job

    def delete(self, job_id: str):
        """
        mark a queued job for deletion. this should be called when the job has begun running.
        :param job_id:
        """
        with self._lock:
            if job_id in self._queued_jobs:
                self._queued_jobs[job_id].deleted = True

    def _check_and_clear_processed_jobs(self):
        with self._lock:
            now = Utils.current_time_ms()

            is_expired = Utils.is_interval_expired(
                last_run_ms=self._clear_processed_jobs_timestamp,
                now_ms=now,
                interval_secs=CLEAR_PROCESSED_JOBS_INTERVAL_SECS)

            if not is_expired:
                return

            self._clear_processed_jobs_timestamp = now

            for job_id in list(self._queued_jobs.keys()):
                queued_job = self._queued_jobs[job_id]
                if queued_job.processed:
                    del self._queued_jobs[job_id]

    def _remove_queued_job(self, job: QueuedJob):
        with self._lock:
            del self._queued_jobs[job.job_id]

    def _is_queue_applicable(self, queue_name: Optional[str] = None) -> bool:
        if queue_name is None:
            return False
        return queue_name in self._applicable_queues

    def _is_max_provisioned_instances_limit(self, job: SocaJob) -> Optional[LimitCheckResult]:

        queue_params = self.queue_profile.queue_management_params
        count = self._context.instance_cache.get_queue_profile_instance_count(self.queue_type)
        max_provisioned_instances = Utils.get_as_int(queue_params.max_provisioned_instances, 0)
        if max_provisioned_instances == 0:
            return None

        result = LimitCheckResult(
            limit_type=LIMIT_TYPE_MAX_PROVISIONED_INSTANCES,
            queue_threshold=queue_params.max_provisioned_instances,
            queue_current=count + job.desired_nodes()
        )
        if count + job.desired_nodes() > max_provisioned_instances:
            return result.fail()

        return result.ok()

    def _is_max_running_jobs_limit(self) -> Optional[LimitCheckResult]:

        queue_params = self.queue_profile.queue_management_params
        max_running_jobs = Utils.get_as_int(queue_params.max_running_jobs, 0)
        if max_running_jobs == 0:
            return None

        queue_total = self._context.job_cache.get_active_jobs(self.queue_type)

        result = LimitCheckResult(
            limit_type=LIMIT_TYPE_MAX_RUNNING_JOBS,
            queue_threshold=queue_params.max_provisioned_instances,
            queue_current=queue_total
        )

        if queue_total + 1 > max_running_jobs:
            return result.fail()

        return result.ok()

    @staticmethod
    def _is_wait_on_licenses_applicable(job: SocaJob) -> int:
        # todo - integrate with license server
        return LIMIT_CHECK_OK

    @staticmethod
    def _block(timeout: float = 1):
        if timeout > 0:
            time.sleep(timeout)

    def is_queue_blocked_by_limits(self) -> bool:
        if self._limit_flag_set is None:
            return False
        return True

    def get_limit_info(self) -> Optional[LimitCheckResult]:
        return self._limit_info

    def get_queue_size(self, key: Optional[str] = None) -> int:
        if Utils.is_not_empty(key):
            return self._queue.qsize(key)
        else:
            return self._queue.qsize()

    def _check_limits(self, job: SocaJob, timeout: float) -> bool:

        def not_ok(limit):
            self.put(job=job)

            if limit.is_queue_limit():
                # if limit check failed at queue level, block the queue
                self._limit_info = limit

                delta = None
                if self._limit_flag_set:
                    delta = arrow.utcnow() - self._limit_flag_set

                if delta is None or delta.seconds > 60:
                    self._limit_flag_set = arrow.utcnow()
                    pending = self._queue.qsize()
                    self._logger.info(f'{limit}, PendingJobs: {pending}')
                    self._context.metrics.jobs_pending(self.queue_type, pending)

                self._context.job_cache.set_job_provisioning_error(
                    job_id=job.job_id,
                    error_code=limit.limit_type,
                    message=str(limit)
                )
                self._block(timeout=timeout)
                raise queue.Empty()
            else:
                # if limit check failed at group level, don't block the queue.
                self._limit_flag_set = None
                return False

        result = self._is_max_provisioned_instances_limit(job=job)
        if result is not None and not result:
            return not_ok(result)

        result = self._is_max_running_jobs_limit()
        if result is not None and not result:
            return not_ok(result)

        self._limit_flag_set = None
        self._limit_info = None
        return True

    def get(self, timeout: Optional[float] = 1) -> SocaJob:
        while not self._is_running.is_set():
            try:

                if not self._context.is_ready():
                    while not self._context.is_ready():
                        interval = Utils.get_retry_backoff_interval(
                            current_retry=self._provisioning_not_ready_retry_attempt,
                            max_retries=PROVISIONING_RETRY_BACKOFF_MAX,
                            backoff_in_seconds=timeout
                        )
                        self._provisioning_not_ready_retry_attempt += 1
                        self._block(interval)
                        if not self._context.is_ready():
                            self._logger.warning('job provisioning not ready. skip.')
                    self._logger.info(f'accepting jobs for queue profile: {self.queue_profile.name}')

                queued_job = self._queue.get(timeout=timeout)
                try:
                    with self._lock:

                        job = self._context.job_cache.get_job(queued_job.job_id)

                        # job is deleted from scheduler, but was queued previously
                        # could happen with retry jobs or when job is stuck in queue due to limits
                        # if job is deleted, remove from job tracker
                        if job is None:
                            continue

                        # job state has changed - could happen with retry jobs.
                        if job.state != SocaJobState.QUEUED:
                            continue

                        # decide if queue needs to be blocked
                        # if blocked, current job will be added to retry queue
                        if not self._check_limits(job=job, timeout=timeout):
                            continue

                        return job
                finally:
                    self._remove_queued_job(queued_job)

            except queue.Empty:

                raise JobProvisioningQueueEmpty()

            except exceptions.SocaException as e:
                if e.error_code == errorcodes.SCHEDULER_JOB_FINISHED:
                    pass
                else:
                    raise e

    def destroy(self):
        self._is_running.set()
