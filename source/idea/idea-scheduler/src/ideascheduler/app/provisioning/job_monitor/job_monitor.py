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

from ideadatamodel import exceptions, SocaJob, SocaJobState, JobUpdates, JobUpdate
from ideasdk.service import SocaService
from ideasdk.utils import Utils
from ideascheduler.app.app_protocols import JobMonitorProtocol
from ideascheduler.app.provisioning.job_monitor.finished_job_processor import (
    FinishedJobProcessor,
)
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect
from ideascheduler.app.scheduler.openpbs import OpenPBSEvent

from typing import Optional, Set, List
from threading import Thread, Condition, Event, RLock
import arrow


class JobMonitorState:
    """
    Thread safe implementation for job monitoring state

    Multiple reentrant locks are needed to synchronize invocations from multiple sources:
        > Hooks (RPC ThreadPoolExecutor Threads)
        > SQS Event Polling Thread
        > Job Monitoring Thread
    """

    def __init__(self, context: ideascheduler.AppContext):
        self._context = context

        self._queued_lock = RLock()
        self._modified_lock = RLock()
        self._running_lock = RLock()

        self.queued_jobs: Set[JobUpdate] = set()
        self.modified_jobs: Set[JobUpdate] = set()
        self.running_jobs: Set[JobUpdate] = set()

        self._last_periodic_run = None

        self._job_updates_available = Event()

    def job_queued(self, job: SocaJob):
        with self._queued_lock:
            self.queued_jobs.add(
                JobUpdate(
                    queue=job.queue, owner=job.owner, timestamp=arrow.utcnow().datetime
                )
            )
            self._job_updates_available.set()

    def job_modified(self, job: SocaJob):
        with self._modified_lock:
            self.modified_jobs.add(
                JobUpdate(
                    queue=job.queue,
                    job_id=job.job_id,
                    timestamp=arrow.utcnow().datetime,
                )
            )
            self._job_updates_available.set()

    def job_running(self, job: SocaJob):
        with self._running_lock:
            self.running_jobs.add(
                JobUpdate(
                    queue=job.queue,
                    job_id=job.job_id,
                    timestamp=arrow.utcnow().datetime,
                )
            )
            self._job_updates_available.set()

    def get_updates(self) -> JobUpdates:
        """
        create a copy of current state and clear state in one atomic operation
        :return: JobUpdates
        """
        now = arrow.utcnow()
        queued_jobs = set()
        with self._queued_lock:
            for job_update in self.queued_jobs:
                queued_jobs.add(job_update)
            self.queued_jobs = self.queued_jobs - queued_jobs
            pending_queued = len(self.queued_jobs)

        modified_jobs = set()
        with self._modified_lock:
            for job_update in self.modified_jobs:
                if job_update.is_applicable(now, delay_secs=1):
                    modified_jobs.add(job_update)
            self.modified_jobs = self.modified_jobs - modified_jobs
            pending_modified = len(self.modified_jobs)

        running_jobs = set()
        with self._running_lock:
            for job_update in self.running_jobs:
                if job_update.is_applicable(now, delay_secs=1):
                    running_jobs.add(job_update)
            self.running_jobs = self.running_jobs - running_jobs
            pending_running = len(self.running_jobs)

        total_pending = pending_queued + pending_modified + pending_running
        if total_pending == 0:
            self._job_updates_available.clear()

        return JobUpdates(
            queued=queued_jobs, modified=modified_jobs, running=running_jobs
        )

    def clear_all(self):
        with self._queued_lock:
            self.queued_jobs.clear()

        with self._modified_lock:
            self.modified_jobs.clear()

        with self._running_lock:
            self.running_jobs.clear()

        self._job_updates_available.clear()

    def is_job_update_available(self):
        return self._job_updates_available.is_set()


class JobMonitor(SocaService, JobMonitorProtocol):
    """
    Realtime monitoring of scheduler Queues for any job updates.
    Performs:
        > Periodic polling of queues
        > Receives realtime updates from hooks
        > Polls Job Status Events SQS queue for new events

    Updates JobCache with relevant updates.
    Submits Queued Jobs to JobProvisioningQueue
    """

    def __init__(self, context: ideascheduler.AppContext):
        super().__init__(context)
        self._context = context
        self._logger = context.logger('job_monitor')
        self._is_running = False
        self._job_status_queue_url = self._context.config().get_string(
            'scheduler.job_status_sqs_queue_url', required=True
        )
        # job submission monitor polls for jobs submitted via qsub
        self._job_submission_monitor: Optional[Thread] = None
        # job execution monitor chesk for job events after the job has started execution on the compute node
        self._job_execution_monitor: Optional[Thread] = None
        self._exit: Optional[Event] = None
        self._monitor: Optional[Condition] = None
        self._state: Optional[JobMonitorState] = None
        self._finished_job_processor: Optional[FinishedJobProcessor] = None
        self._queued_after: Optional[arrow.Arrow] = None

    def _initialize(self):
        self._job_submission_monitor = Thread(
            name='job-submission-monitor',
            target=self._monitor_job_submission,
        )
        self._job_execution_monitor = Thread(
            name='job-execution-monitor',
            target=self._monitor_job_execution,
        )
        self._exit = Event()
        self._monitor = Condition()
        self._state = JobMonitorState(context=self._context)
        self._finished_job_processor = FinishedJobProcessor(context=self._context)
        self._sync_all_jobs()
        self._context.job_cache.set_ready()

    def _submit_to_provisioning_queue(self, jobs: List[SocaJob]):
        for job in jobs:
            if self._exit.is_set():
                break

            provisioning_queue = self._context.queue_profiles.get_provisioning_queue(
                queue_profile_name=job.queue_type
            )
            if provisioning_queue is None:
                continue

            if job.state == SocaJobState.RUNNING:
                # if job execution has started running, ensure the job is deleted from provisioning queue.
                provisioning_queue.delete(job_id=job.job_id)
                continue

            if job.is_provisioned():
                if job.is_ephemeral_capacity():
                    provisioning_queue.put(job=job)
            else:
                provisioning_queue.put(job=job)

    def _sync_all_jobs(self):
        # we can call list all jobs and sync all jobs at once, but from metrics, error handling, memory and
        # performance standpoint, it's better to list jobs by queue.
        # this will help mitigate flooding the memory if there are more than 10k jobs per queue

        try:
            queue_profiles = self._context.queue_profiles.list_queue_profiles()
            for queue_profile in queue_profiles:
                if not Utils.is_true(queue_profile.enabled):
                    continue

                for queue in queue_profile.queues:
                    provisioning_queue = (
                        self._context.queue_profiles.get_provisioning_queue(
                            queue_profile_name=queue_profile.name
                        )
                    )
                    if provisioning_queue is None:
                        continue

                    jobs = self._context.scheduler.list_jobs(queue=queue)

                    self._context.job_cache.sync(jobs=jobs)

                    for job in jobs:
                        # if job is provisioned, add to provisioning queue only if scaling mode is single job.
                        # for batch scaling mode, once the job is provisioned, retry logic is not applicable
                        if job.is_provisioned():
                            if job.is_ephemeral_capacity():
                                provisioning_queue.put(job=job)
                        else:
                            provisioning_queue.put(job=job)

        except Exception as e:
            self._logger.exception(f'sync all jobs failed: {e}')

    def _sync_jobs(self, job_updates: Set[JobUpdate], job_type: str):
        try:
            if job_updates is None or len(job_updates) == 0:
                return

            # group queued jobs by queue_name: owners[] or job_ids[], to query the scheduler
            query = {}
            for update in job_updates:
                queue = update.queue
                if update.owner:
                    target = update.owner
                else:
                    target = update.job_id

                if queue in query:
                    targets = query[queue]
                else:
                    targets = set()
                    query[queue] = targets
                targets.add(target)

            for queue, targets in query.items():
                try:
                    if self._exit.is_set():
                        break

                    if job_type == 'queued':
                        processed_jobs = {}

                        queued_job_ids = OpenPBSQSelect(
                            context=self._context,
                            logger=self._logger,
                            log_tag='queued-jobs',
                            stack_id='tbd',
                            queue=queue,
                            job_state=[SocaJobState.QUEUED, SocaJobState.HELD],
                        ).list_jobs_ids()

                        has_more = len(queued_job_ids) > 0

                        self._logger.info(
                            f'jobs queued: {len(queued_job_ids)}, fetching ...'
                        )

                        while has_more:
                            job_ids_to_fetch = []
                            existing_jobs = []
                            for queued_job_id in queued_job_ids:
                                if queued_job_id in processed_jobs:
                                    continue
                                processed_jobs[queued_job_id] = True
                                job = self._context.job_cache.get_job(queued_job_id)
                                if job is not None:
                                    existing_jobs.append(job)
                                    continue
                                job_ids_to_fetch.append(queued_job_id)

                            if len(job_ids_to_fetch) == 0:
                                break

                            queued_jobs = self._context.scheduler.list_jobs(
                                job_ids=job_ids_to_fetch
                            )

                            self._context.job_cache.sync(jobs=queued_jobs)

                            self._submit_to_provisioning_queue(
                                jobs=queued_jobs + existing_jobs
                            )

                            queued_job_ids = OpenPBSQSelect(
                                context=self._context,
                                logger=self._logger,
                                stack_id='tbd',
                                queue=queue,
                                job_state=[SocaJobState.QUEUED, SocaJobState.HELD],
                            ).list_jobs_ids()

                            has_more = len(processed_jobs) < len(queued_job_ids)

                            if has_more:
                                self._logger.info(
                                    f'jobs queued: {len(queued_job_ids)}, '
                                    f'fetching additional {len(queued_job_ids) - len(processed_jobs)} job(s) ...'
                                )

                    else:
                        # otherwise, query for all job_ids
                        jobs = self._context.scheduler.list_jobs(
                            queue=queue, job_ids=list(targets)
                        )

                        if len(jobs) == 0:
                            continue

                        self._context.job_cache.sync(jobs=jobs)

                        if job_type == 'modified':
                            self._submit_to_provisioning_queue(jobs=jobs)

                        self._logger.info(
                            f'job update: {job_type}, num jobs: {len(jobs)}'
                        )

                except exceptions.SocaException as e:
                    self._logger.warning(str(e))

        except Exception as e:
            self._logger.exception(f'failed to process {job_type} jobs', exc_info=e)

    def _job_reconciler(self):
        """
        Job reconciliation fallback to catch jobs missed by hooks.
        This handles cases where PBS hooks don't fire immediately or fail.
        """
        try:
            queue_profiles = self._context.queue_profiles.list_queue_profiles()
            for queue_profile in queue_profiles:
                if not Utils.is_true(queue_profile.enabled):
                    continue

                for queue in queue_profile.queues:
                    if self._exit.is_set():
                        break

                    # Query PBS directly for queued/held jobs with stack_id=tbd
                    queued_job_ids = OpenPBSQSelect(
                        context=self._context,
                        logger=self._logger,
                        log_tag='job-reconciler',
                        stack_id='tbd',
                        queue=queue,
                        job_state=[SocaJobState.QUEUED, SocaJobState.HELD],
                    ).list_jobs_ids()

                    if len(queued_job_ids) > 0:
                        self._logger.info(
                            f'job reconciler found {len(queued_job_ids)} queued job(s) in queue {queue}: {queued_job_ids}'
                        )
                        queued_jobs = self._context.scheduler.list_jobs(
                            job_ids=queued_job_ids
                        )
                        self._context.job_cache.sync(jobs=queued_jobs)
                        self._submit_to_provisioning_queue(jobs=queued_jobs)

        except Exception as e:
            self._logger.exception(f'job reconciler failed: {e}')

    def _monitor_job_submission(self):
        while not self._exit.is_set():
            try:
                # there are delta updates: queued, modified or running jobs
                if self._state.is_job_update_available():
                    # get a copy of current state
                    job_updates = self._state.get_updates()

                    self._sync_jobs(job_updates=job_updates.queued, job_type='queued')
                    self._sync_jobs(
                        job_updates=job_updates.modified, job_type='modified'
                    )
                    self._sync_jobs(job_updates=job_updates.running, job_type='running')

                # Job reconciler fallback - check PBS directly every N seconds
                # This catches jobs when hooks fail to fire immediately
                now = arrow.utcnow()
                reconciler_interval = self._context.config().get_int(
                    'scheduler.job_provisioning.job_reconciler_interval_seconds',
                    default=60,
                )
                if (
                    self._state._last_periodic_run is None
                    or (now - self._state._last_periodic_run).total_seconds()
                    >= reconciler_interval
                ):
                    self._job_reconciler()
                    self._state._last_periodic_run = now

            except Exception as e:
                self._logger.exception(f'job monitor iteration failed: {e}')
            finally:
                try:
                    self._monitor.acquire()

                    # need to better handle scenario when wait_for timeout occurs exactly at the same time as
                    # as job is queued from the hook. the scheduler might not return the queued job as the job
                    # is not yet queued (race condition).
                    # worst case scenario is job will be caught in the job reconciler after a short delay (default 60 seconds).

                    # delta job updates if any, will be processed at an interval of 1 second
                    self._monitor.wait_for(
                        self._state.is_job_update_available,
                        self._context.config().get_int(
                            'scheduler.job_provisioning.job_submission_queue_interval_seconds',
                            default=1,
                        ),
                    )
                finally:
                    self._monitor.release()

    def _monitor_job_execution(self):
        while not self._exit.is_set():
            try:
                result = (
                    self._context.aws()
                    .sqs()
                    .receive_message(
                        QueueUrl=self._job_status_queue_url,
                        MaxNumberOfMessages=10,
                        WaitTimeSeconds=5,
                    )
                )
                messages = Utils.get_value_as_list('Messages', result)
                if messages is None:
                    continue
                if len(messages) == 0:
                    continue

                to_be_deleted = []
                for sqs_message in messages:
                    try:
                        body = Utils.get_value_as_string('Body', sqs_message)
                        message_json = Utils.base64_decode(body)
                        message = Utils.from_json(message_json)
                        payload = Utils.get_value_as_dict('payload', message)
                        event = Utils.get_value_as_dict('event', payload)
                        pbs_event = OpenPBSEvent(**event)

                        queue = pbs_event.job.queue
                        queue_profile = self._context.queue_profiles.get_queue_profile(
                            queue_name=queue
                        )
                        if queue_profile is None:
                            continue
                        job = pbs_event.as_soca_job(
                            context=self._context, queue_profile=queue_profile
                        )

                        self._logger.info(
                            f'{job.log_tag} {pbs_event.type} on host: {pbs_event.requestor_host}'
                        )
                        self.job_status_update(job)
                    except Exception as e:
                        self._logger.exception(
                            f'failed to process job execution event: {e}'
                        )
                    finally:
                        to_be_deleted.append(
                            {
                                'Id': Utils.get_value_as_string(
                                    'MessageId', sqs_message
                                ),
                                'ReceiptHandle': Utils.get_value_as_string(
                                    'ReceiptHandle', sqs_message
                                ),
                            }
                        )

                if len(to_be_deleted) > 0:
                    self._context.aws().sqs().delete_message_batch(
                        QueueUrl=self._job_status_queue_url, Entries=to_be_deleted
                    )

            except Exception as e:
                self._logger.exception(f'failed to process job execution event: {e}')

    def job_queued(self, job: SocaJob):
        """
        should be called when job is validated successfully and accepted (qsub)
        at this point in time, job_id is not available
        all operations should be low latency, non-blocking IO operations.

        :param job: SocaJob built using the event received from Scheduler
        """
        self._state.job_queued(job=job)

    def job_modified(self, job: SocaJob):
        """
        should be called when job modifications are validated successfully (qalter)
        all operations should be low latency, non-blocking IO operations.

        :param job: SocaJob built using the event received from Scheduler
        """
        self._state.job_modified(job=job)

    def job_running(self, job: SocaJob):
        """
        should be called when job is running
        """
        self._state.job_running(job=job)

        cached_job = self._context.job_cache.get_job(job.job_id)

        if cached_job is not None:
            job = cached_job

        job.state = SocaJobState.RUNNING
        self._context.metrics.jobs_running(queue_type=job.queue_type)
        self._logger.info(f'{job.log_tag} JobStarted')
        # send email notification if applicable
        self._context.job_notifications.job_started(job=job)

    def job_status_update(self, job: SocaJob):
        """
        should be called when job status events are received from compute nodes (from SQS)
        all operations should be low latency, non-blocking IO operations.

        :param job: SocaJob built using the event received from Scheduler
        """

        execution_hosts = job.execution_hosts
        if execution_hosts is None or len(execution_hosts) == 0:
            return

        job_id = job.job_id
        execution_host = execution_hosts[0]

        self._context.job_cache.log_job_execution(
            job_id=job_id, execution_host=execution_host
        )

    def start(self):
        if self._is_running:
            return
        self._initialize()
        self._is_running = True
        self._job_submission_monitor.start()
        self._job_execution_monitor.start()

    def stop(self):
        if not self._is_running:
            return
        self._exit.set()
        self._is_running = False
        self._job_submission_monitor.join()
        self._job_execution_monitor.join()
        self._finished_job_processor.stop()
