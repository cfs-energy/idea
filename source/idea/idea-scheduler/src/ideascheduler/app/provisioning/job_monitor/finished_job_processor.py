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
    constants, SocaJob, JobUpdate, CustomFileLoggerParams, SocaJobState
)
from ideascheduler.app.aws import PricingHelper, AwsBudgetsHelper
from ideasdk.utils import Utils

from typing import List
from threading import Thread, Event
import logging

from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect


class ProcessFinishedJob:
    """
    Finished Job Post-Processing
    """

    def __init__(self, context: ideascheduler.AppContext, logger: logging.Logger,
                 job: SocaJob, job_export_logger: logging.Logger):
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
        try:

            execution_hosts = self._context.job_cache.get_job_execution_hosts(job_id=self.job.job_id)

            self.job.execution_hosts = execution_hosts

            if self.job.end_time and self.job.start_time:
                delta = self.job.end_time - self.job.start_time
                self.job.total_time_secs = delta.seconds

            self._context.job_cache.delete_job_execution_hosts(job_id=self.job.job_id)

        except Exception as e:
            self._logger.exception(f'{self.job.log_tag} failed to apply job execution: {e}')

    def compute_and_apply_estimated_costs(self):
        try:

            # todo - compute pricing from actual usage rather than resource estimates
            #   see development list in PricingHelper

            total_time_secs = self.job.get_total_time_seconds()

            # for ephemeral capacity (single jobs), resources are launched and terminated as soon as job is complete.
            # for shared capacity, job pricing estimates, we set a minimum of 60 seconds since EC2 instances are
            # charged per second, with minimum of 60 seconds

            # if job ran for less than 60 seconds, update total time for price estimates

            if total_time_secs is None or total_time_secs < constants.SECONDS_IN_MINUTE:
                total_time_secs = constants.SECONDS_IN_MINUTE

            estimated_bom_cost = PricingHelper(
                context=self._context,
                job=self.job,
                total_time_secs=total_time_secs
            ).compute_estimated_bom_cost()

            self.job.estimated_bom_cost = estimated_bom_cost

        except Exception as e:
            self._logger.exception(f'{self.job.log_tag} failed to compute estimated costs: {e}')

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
            self._logger.exception(f'{self.job.log_tag} failed to compute budget usage: {e}')

    def publish_job_metrics(self):

        try:
            self._context.metrics.jobs_finished(queue_type=self.job.queue_type)

            if self.job.provisioning_time and self.job.start_time:
                provisioning_duration = self.job.provisioning_time - self.job.start_time
                self._context.metrics.jobs_provisioning_duration(
                    queue_type=self.job.queue_type,
                    duration_secs=provisioning_duration.seconds
                )

            if self.job.end_time and self.job.start_time:
                running_duration = self.job.end_time - self.job.start_time
                self._context.metrics.jobs_running_duration(
                    queue_type=self.job.queue_type,
                    duration_secs=running_duration.seconds
                )

            if self.job.queue_time and self.job.end_time:
                total_duration = self.job.end_time - self.job.queue_time
                self._context.metrics.jobs_total_duration(
                    queue_type=self.job.queue_type,
                    duration_secs=total_duration.seconds
                )
        except Exception as e:
            self._logger.exception(f'{self.job.log_tag} failed to publish job metrics: {e}')

    def publish_to_job_export_log(self):
        try:
            self._job_export_logger.critical(self.get_job_as_json())
        except Exception as e:
            self._logger.exception(f'{self.job.log_tag} failed to export job to job log: {e}')

    def send_email_notification(self):
        try:
            self._context.job_notifications.job_completed(job=self.job)
        except Exception as e:
            self._logger.exception(f'{self.job.log_tag} failed to send email notification: {e}')

    def publish_to_finished_jobs_db(self):
        try:
            self._context.job_cache.add_finished_job(job=self.job)
        except Exception as e:
            self._logger.exception(f'{self.job.log_tag} failed to add finished job to db: {e}')

    def log_job_complete(self):
        log_msg = f'{self.job.log_tag} JobCompleted'
        if self._logger.isEnabledFor(logging.DEBUG):
            log_msg += f' Job: {self.get_job_as_json()}'
        self._logger.info(log_msg)

    def invoke(self) -> SocaJob:
        try:

            self.apply_job_execution_context()

            self.compute_and_apply_estimated_costs()

            self.compute_and_apply_budget_usage()

            self.log_job_complete()

            self.publish_job_metrics()

            self.publish_to_job_export_log()

            self.send_email_notification()

            self.publish_to_finished_jobs_db()

            return self.job

        except Exception as e:
            self._logger.exception(f'{self.job.log_tag} finished job processing failed: {e}')


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
            name='finished-job-processor',
            target=self._poll_finished_jobs
        )
        self._finished_job_thread.start()

        self._retry_updates: List[JobUpdate] = []

    def _setup_job_export_logger(self) -> logging.Logger:
        return self._context.logging().get_custom_file_logger(CustomFileLoggerParams(
            logger_name='soca_job_export',
            log_dir_name='jobs',
            log_file_name='soca_job_export.log',
            when='midnight',
            interval=1,
            backupCount=365
        ))

    def _process_finished_jobs(self, jobs: List[SocaJob]):
        if len(jobs) == 0:
            return

        finished_job_ids = []
        for job in jobs:
            finished_job_ids.append(job.job_id)

        finished_jobs = self._context.scheduler.list_jobs(job_ids=finished_job_ids, job_state=SocaJobState.FINISHED)

        jobs_to_index = []
        for finished_job in finished_jobs:
            try:
                job_to_index = ProcessFinishedJob(
                    context=self._context,
                    logger=self._logger,
                    job=finished_job,
                    job_export_logger=self._jobs_export_logger
                ).invoke()
                jobs_to_index.append(job_to_index)
            except Exception as e:
                self._logger.exception(f'{finished_job.log_tag} failed to process finished job: {e}')

        try:
            self._context.document_store.add_jobs(jobs=jobs_to_index)
        except Exception as e:
            self._logger.exception(f'failed to publish jobs to opensearch: {e}')

    def _poll_finished_jobs(self):
        while not self._exit.is_set():
            try:

                jobs_table = self._context.job_cache.get_jobs_table()

                active_job_ids = OpenPBSQSelect(
                    self._context
                ).list_jobs_ids()
                active_job_ids = set(active_job_ids)

                jobs_deleted = 0
                jobs_finished = 0

                jobs_ids_to_delete = []
                finished_jobs = []

                result = jobs_table.all()
                for entry in result:

                    if self._exit.is_set():
                        break

                    try:
                        job_id = Utils.get_value_as_string('job_id', entry)

                        # if job is active, do nothing..
                        if job_id in active_job_ids:
                            continue

                        completed_job = self._context.job_cache.convert_db_entry_to_job(entry)
                        if completed_job is None:
                            continue

                        # if job was not provisioned, it was most certainly deleted using qdel
                        # before provisioning. no need to process as finished jobs. skip
                        if not completed_job.is_provisioned():
                            jobs_ids_to_delete.append(completed_job.job_id)
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
                    self._context.job_cache.delete_jobs(job_ids=jobs_ids_to_delete)

                if jobs_deleted + jobs_finished > 0:
                    self._logger.info(f'finished_jobs: {jobs_finished}, active jobs: {len(active_job_ids)}, deleted jobs: {jobs_deleted}')

                if len(finished_jobs) > 0:
                    self._process_finished_jobs(finished_jobs)

            finally:
                if not self._exit.is_set():
                    self._exit.wait(self._context.config().get_int('scheduler.job_provisioning.finished_job_processing_interval_seconds', default=30))

    def stop(self):
        self._exit.set()
