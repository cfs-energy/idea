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
    constants,
    SocaJob,
    JobUpdate,
    CustomFileLoggerParams,
    SocaJobState,
)
from ideascheduler.app.aws import PricingHelper, AwsBudgetsHelper
from ideasdk.utils import Utils

from typing import Dict, List, Optional
from threading import Thread, Event
import arrow
import logging

from ideascheduler.app.provisioning.lifecycle_events import (
    ProvisioningLifecycleEvents,
)
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect


class ProcessFinishedJob:
    """
    Finished Job Post-Processing
    """

    def __init__(
        self,
        context: ideascheduler.AppContext,
        logger: logging.Logger,
        job: SocaJob,
        job_export_logger: logging.Logger,
    ):
        self._context = context
        self._logger = logger
        self.job = job
        self._job_export_logger = job_export_logger

    def get_job_as_json(self) -> str:
        return Utils.to_json(self.job)

    def apply_job_execution_context(self):
        """
        1. copies the execution data collected for the job from job execution event updates
        2. applies the execution data to the finished job
        3. clears the execution data from JobCache
        """
        # duration depends only on the job's own timestamps; a job-cache error below must
        # never leave total_time_secs unset, or the walltime fallback will over-bill it.
        try:
            if self.job.start_time is None:
                # job was terminated before it started executing. no time was consumed,
                # and the requested walltime must not be used as a substitute.
                self._logger.info(
                    f'{self.job.log_tag} finished with no start time; '
                    f'total_time_secs set to 0, estimated cost left unset'
                )
                self.job.total_time_secs = 0
            elif self.job.end_time:
                delta = self.job.end_time - self.job.start_time
                self.job.total_time_secs = int(delta.total_seconds())
            else:
                # finished job with no derivable end time: measure to now rather than
                # substituting the requested walltime.
                self.job.total_time_secs = int(
                    (arrow.utcnow() - self.job.start_time).total_seconds()
                )
        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to compute job duration: {e}'
            )

        # execution hosts are cosmetic metadata; a fetch/clear failure here must not
        # affect the duration computed above.
        try:
            self.job.execution_hosts = self._context.job_cache.get_job_execution_hosts(
                job_id=self.job.job_id
            )
            self._context.job_cache.delete_job_execution_hosts(job_id=self.job.job_id)
        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to apply job execution hosts: {e}'
            )

    @staticmethod
    def never_started(job: SocaJob) -> bool:
        """
        the scheduler stamps a start time only when a node begins executing the job.
        a job that ran for less than a second still has one, so this does not catch it.
        """
        return job.start_time is None

    def compute_and_apply_estimated_costs(self):
        try:
            # todo - compute pricing from actual usage rather than resource estimates
            #   see development list in PricingHelper

            if self.never_started(self.job):
                # no measured instance time to price: the 60s floor and boot penalty adjust
                # a measurement rather than invent one, and whether an instance even launched isn't recorded.
                self._logger.info(
                    f'{self.job.log_tag} never started; estimated cost left unset '
                    f'(no measured instance time to price)'
                )
                self.job.estimated_bom_cost = None
                return

            # the measured run time only: falling back to the requested walltime would
            # price a terminated job for time it never used.
            total_time_secs = self.job.total_time_secs
            if total_time_secs is None:
                self._logger.warning(
                    f'{self.job.log_tag} run time was not measured; estimated cost left '
                    f'unset (the requested walltime is not a substitute)'
                )
                self.job.estimated_bom_cost = None
                return

            # for ephemeral capacity (single jobs), resources are launched and terminated as soon as job is complete.
            # for shared capacity, job pricing estimates, we set a minimum of 60 seconds since EC2 instances are
            # charged per second, with minimum of 60 seconds

            # if job ran for less than 60 seconds, update total time for price estimates

            if total_time_secs is None or total_time_secs < constants.SECONDS_IN_MINUTE:
                total_time_secs = constants.SECONDS_IN_MINUTE

            estimated_bom_cost = PricingHelper(
                context=self._context, job=self.job, total_time_secs=total_time_secs
            ).compute_estimated_bom_cost()

            self.job.estimated_bom_cost = estimated_bom_cost

        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to compute estimated costs: {e}'
            )

    def compute_and_apply_budget_usage(self):
        try:
            if self.job.estimated_bom_cost is None:
                return

            estimated_budget_usage = AwsBudgetsHelper(
                context=self._context,
                job=self.job,
            ).compute_budget_usage()

            self.job.estimated_budget_usage = estimated_budget_usage

        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to compute budget usage: {e}'
            )

    def publish_job_metrics(self):
        try:
            self._context.metrics.jobs_finished(queue_type=self.job.queue_type)

            if self.job.provisioning_time and self.job.start_time:
                provisioning_duration = self.job.start_time - self.job.provisioning_time
                self._context.metrics.jobs_provisioning_duration(
                    queue_type=self.job.queue_type,
                    duration_secs=int(provisioning_duration.total_seconds()),
                )

            if self.job.end_time and self.job.start_time:
                running_duration = self.job.end_time - self.job.start_time
                self._context.metrics.jobs_running_duration(
                    queue_type=self.job.queue_type,
                    duration_secs=int(running_duration.total_seconds()),
                )

            if self.job.queue_time and self.job.end_time:
                total_duration = self.job.end_time - self.job.queue_time
                self._context.metrics.jobs_total_duration(
                    queue_type=self.job.queue_type,
                    duration_secs=int(total_duration.total_seconds()),
                )
        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to publish job metrics: {e}'
            )

    def publish_lifecycle_event(self, disposition: Optional[str] = None):
        try:
            lifecycle_events = self._context.lifecycle_events
            if lifecycle_events is not None:
                lifecycle_events.job_disposition(
                    job=self.job,
                    disposition=disposition,
                    attempt_number=lifecycle_events.attempts_consumed(job=self.job),
                )
        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to publish job disposition event: {e}'
            )

    def publish_to_job_export_log(self):
        try:
            self._job_export_logger.critical(self.get_job_as_json())
        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to export job to job log: {e}'
            )

    def send_email_notification(self):
        try:
            self._context.job_notifications.job_completed(job=self.job)
        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to send email notification: {e}'
            )

    def publish_to_finished_jobs_db(self) -> bool:
        try:
            self._context.job_cache.add_finished_job(job=self.job)
            return True
        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} failed to add finished job to db: {e}'
            )
            return False

    def log_job_complete(self):
        log_msg = f'{self.job.log_tag} JobCompleted'
        if self._logger.isEnabledFor(logging.DEBUG):
            log_msg += f' Job: {self.get_job_as_json()}'
        self._logger.info(log_msg)

    def invoke_unprovisioned(self) -> Optional[SocaJob]:
        """
        record a job that never got capacity, so the failure survives the job cache.

        the pricing stages are skipped on purpose: nothing ran, and the total-time
        fallback is the requested walltime, which would invent a charge.
        """
        try:
            # read the disposition before the state is rewritten, so a job parked at the
            # provisioning retry cap still reports 'held' rather than 'deleted'.
            disposition = ProvisioningLifecycleEvents.get_disposition(self.job)

            self.job.state = SocaJobState.FINISHED
            self.job.total_time_secs = 0
            self.job.estimated_bom_cost = None
            self.job.estimated_budget_usage = None
            # the record needs an end. without one the portal measures the queue wait
            # against the browser clock and it grows for as long as the row exists.
            if self.job.end_time is None:
                self.job.end_time = arrow.utcnow().datetime

            self.log_job_complete()

            self.publish_lifecycle_event(disposition=disposition)

            self.publish_to_job_export_log()

            if not self.publish_to_finished_jobs_db():
                # indexing a job the finished jobs table does not have would leave the
                # two completed-jobs read paths disagreeing about it.
                return None

            return self.job

        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} unprovisioned job processing failed: {e}'
            )
            return None

    def invoke(self) -> SocaJob:
        try:
            self.apply_job_execution_context()

            self.compute_and_apply_estimated_costs()

            self.compute_and_apply_budget_usage()

            self.log_job_complete()

            self.publish_job_metrics()

            self.publish_lifecycle_event()

            self.publish_to_job_export_log()

            self.send_email_notification()

            self.publish_to_finished_jobs_db()

            return self.job

        except Exception as e:
            self._logger.exception(
                f'{self.job.log_tag} finished job processing failed: {e}'
            )


class FinishedJobProcessor:
    """
    prepares finished jobs for post processing
    """

    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger(name='finished_job_processor')
        self._exit = Event()

        self._jobs_export_logger: logging.Logger = self._setup_job_export_logger()
        self._finished_job_thread = Thread(
            name='finished-job-processor', target=self._poll_finished_jobs
        )
        self._finished_job_thread.start()

        self._retry_updates: List[JobUpdate] = []

    def _setup_job_export_logger(self) -> logging.Logger:
        return self._context.logging().get_custom_file_logger(
            CustomFileLoggerParams(
                logger_name='soca_job_export',
                log_dir_name='jobs',
                log_file_name='soca_job_export.log',
                when='midnight',
                interval=1,
                backupCount=365,
            )
        )

    def _process_finished_jobs(self, jobs: List[SocaJob]):
        if len(jobs) == 0:
            return

        finished_job_ids = []
        provisioning_errors: Dict[str, str] = {}
        for job in jobs:
            finished_job_ids.append(job.job_id)
            if Utils.is_not_empty(job.error_message):
                provisioning_errors[job.job_id] = job.error_message

        finished_jobs = self._context.scheduler.list_jobs(
            job_ids=finished_job_ids, job_state=SocaJobState.FINISHED
        )

        jobs_to_index = []
        for finished_job in finished_jobs:
            try:
                # the provisioning error row is deleted with the job cache entry before this
                # runs; re-attach it so the reason lands on the finished record too.
                if Utils.is_empty(finished_job.error_message):
                    finished_job.error_message = provisioning_errors.get(
                        finished_job.job_id
                    )
                job_to_index = ProcessFinishedJob(
                    context=self._context,
                    logger=self._logger,
                    job=finished_job,
                    job_export_logger=self._jobs_export_logger,
                ).invoke()
                if job_to_index is not None:
                    jobs_to_index.append(job_to_index)
            except Exception as e:
                self._logger.exception(
                    f'{finished_job.log_tag} failed to process finished job: {e}'
                )

        try:
            self._context.document_store.add_jobs(jobs=jobs_to_index)
        except Exception as e:
            self._logger.exception(f'failed to publish jobs to opensearch: {e}')

    @staticmethod
    def should_record_unprovisioned(job: SocaJob) -> bool:
        """
        a job that never got capacity is recorded when the platform stopped trying,
        which is the retry cap holding it. gating on error_message instead would record
        every deletion of a job still being worked on: that row is written on transient
        waits too and is cleared only by a successful provision.
        """
        return job.state == SocaJobState.HELD

    def _process_unprovisioned_jobs(self, jobs: List[SocaJob]):
        """
        write jobs that never got capacity straight to the finished jobs table. they
        cannot be re-read from the scheduler, and they are never priced.
        """
        jobs_to_index = []
        for job in jobs:
            try:
                job_to_index = ProcessFinishedJob(
                    context=self._context,
                    logger=self._logger,
                    job=job,
                    job_export_logger=self._jobs_export_logger,
                ).invoke_unprovisioned()
                if job_to_index is not None:
                    jobs_to_index.append(job_to_index)
            except Exception as e:
                self._logger.exception(
                    f'{job.log_tag} failed to record unprovisioned job: {e}'
                )

        try:
            self._context.document_store.add_jobs(jobs=jobs_to_index)
        except Exception as e:
            self._logger.exception(
                f'failed to publish unprovisioned jobs to opensearch: {e}'
            )

    def _poll_finished_jobs(self):
        while not self._exit.is_set():
            try:
                self._logger.debug('FinishedJobProcessor: Starting processing cycle')

                jobs_table = self._context.job_cache.get_jobs_table()

                try:
                    # raise_on_error: a failed qselect must not read as "no active jobs" -
                    # that would mass-finish every cached job and price it at full walltime.
                    active_job_ids = OpenPBSQSelect(self._context).list_jobs_ids(
                        raise_on_error=True
                    )
                    active_job_ids = set(active_job_ids)
                    self._logger.debug(
                        f'FinishedJobProcessor: Retrieved {len(active_job_ids)} active jobs from PBS'
                    )
                except Exception as e:
                    self._logger.error(f'Failed to get active job IDs from PBS: {e}')
                    continue

                jobs_deleted = 0
                jobs_finished = 0
                jobs_recorded = 0

                jobs_ids_to_delete = []
                finished_jobs = []
                unprovisioned_jobs = []

                result = jobs_table.all()
                for entry in result:
                    if self._exit.is_set():
                        break

                    try:
                        job_id = Utils.get_value_as_string('job_id', entry)

                        # if job is active, do nothing..
                        if job_id in active_job_ids:
                            continue

                        # fetch_errors: the delete below drops the provisioning error row,
                        # so read it onto the job here or the reason is lost.
                        completed_job = self._context.job_cache.convert_db_entry_to_job(
                            entry, fetch_errors=True
                        )
                        if completed_job is None:
                            self._logger.warning(
                                f'Failed to convert job {job_id} from cache entry, skipping'
                            )
                            continue

                        # can't re-read this from the scheduler once it's gone: record only
                        # jobs held at the retry cap (already reported 'held'); others were owner-deleted mid-wait.
                        if not completed_job.is_provisioned():
                            jobs_ids_to_delete.append(completed_job.job_id)
                            if self.should_record_unprovisioned(completed_job):
                                unprovisioned_jobs.append(completed_job)
                                jobs_recorded += 1
                                continue
                            lifecycle_events = self._context.lifecycle_events
                            if lifecycle_events is not None:
                                lifecycle_events.job_disposition(
                                    job=completed_job,
                                    attempt_number=lifecycle_events.attempts_consumed(
                                        job=completed_job
                                    ),
                                )
                            jobs_deleted += 1
                            continue

                        if completed_job.state != SocaJobState.FINISHED:
                            completed_job.state = SocaJobState.FINISHED
                        finished_jobs.append(completed_job)
                        jobs_ids_to_delete.append(completed_job.job_id)
                        jobs_finished += 1

                    except Exception as e:
                        self._logger.exception(f'failed to process finished job: {e}')

                if len(jobs_ids_to_delete) > 0:
                    try:
                        self._logger.debug(
                            f'FinishedJobProcessor: Deleting {len(jobs_ids_to_delete)} jobs from cache'
                        )
                        self._context.job_cache.delete_jobs(job_ids=jobs_ids_to_delete)
                    except Exception as e:
                        self._logger.error(f'Failed to delete jobs from cache: {e}')

                if jobs_deleted + jobs_finished + jobs_recorded > 0:
                    self._logger.info(
                        f'finished_jobs: {jobs_finished}, active jobs: {len(active_job_ids)}, '
                        f'deleted jobs: {jobs_deleted}, unprovisioned jobs recorded: {jobs_recorded}'
                    )

                if len(unprovisioned_jobs) > 0:
                    self._process_unprovisioned_jobs(unprovisioned_jobs)

                if len(finished_jobs) > 0:
                    self._process_finished_jobs(finished_jobs)

                self._logger.debug(
                    'FinishedJobProcessor: Processing cycle completed successfully'
                )

            except Exception as e:
                # CRITICAL: Top-level exception handler to prevent thread death
                # This catches any unhandled exception that would otherwise kill the thread
                self._logger.exception(
                    f'CRITICAL: Unhandled exception in FinishedJobProcessor cycle: {e}'
                )
                # Thread continues running despite the exception

            finally:
                if not self._exit.is_set():
                    self._exit.wait(
                        self._context.config().get_int(
                            'scheduler.job_provisioning.finished_job_processing_interval_seconds',
                            default=30,
                        )
                    )

    def stop(self):
        self._exit.set()
