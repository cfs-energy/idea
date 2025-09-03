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

from ideasdk.service import SocaService
from ideadatamodel import (
    exceptions,
    errorcodes,
    SocaBaseModel,
    SocaJob,
    SocaScalingMode,
    SocaQueueMode,
    ProvisioningStatus,
)
from ideasdk.utils import Utils

from ideascheduler.app.provisioning import (
    JobProvisioningQueue,
    JobProvisioningQueueEmpty,
    CloudFormationStackBuilder,
    JobProvisioningUtil,
)
from collections import OrderedDict
from typing import Optional, List, Dict
from threading import Thread, Event
import arrow
import logging
from pydantic import Field
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect
from ideascheduler.app.provisioning.job_provisioner.batch_capacity_helper import (
    BatchCapacityHelper,
)


class ProvisionJobsResult(SocaBaseModel):
    error_code: Optional[str] = Field(default=None)
    status: Optional[bool] = Field(default=None)
    unprovisioned_jobs: Optional[List[SocaJob]] = Field(default=None)
    exception: Optional[Exception] = Field(default=None)

    def get_error_code(self) -> str:
        if Utils.is_not_empty(self.error_code):
            return self.error_code
        if self.exception is not None and isinstance(
            self.exception, exceptions.SocaException
        ):
            return self.exception.error_code
        else:
            return errorcodes.GENERAL_ERROR

    def get_error_message(self) -> str:
        if self.exception is not None:
            return str(self.exception)
        if Utils.is_not_empty(self.error_code):
            return self.error_code
        return 'Unknown Error'


class ProvisionJobs:
    """
    Provisions a Job or a Batch of Jobs.
    """

    def __init__(
        self,
        context: ideascheduler.AppContext,
        jobs: List[SocaJob],
        logger: logging.Logger,
    ):
        self._context = context
        self._logger = logger

        self.jobs = jobs

        self.provisioning_util = JobProvisioningUtil(
            context=self._context, jobs=self.jobs, logger=self._logger
        )

        self._provisioning_status: Optional[ProvisioningStatus] = None

    @property
    def job(self) -> SocaJob:
        return self.jobs[0]

    @property
    def is_batch(self) -> bool:
        return self.job.scaling_mode == SocaScalingMode.BATCH

    def log_tag(self, job: Optional[SocaJob] = None):
        if job is not None:
            return job.log_tag
        return self.job.log_tag

    @property
    def provisioning_status(self) -> ProvisioningStatus:
        if self._provisioning_status is not None:
            return self._provisioning_status

        self._provisioning_status = self.provisioning_util.check_status()
        return self._provisioning_status

    @property
    def stack_creation_time(self) -> Optional[arrow.Arrow]:
        return self.provisioning_util.stack.creation_time

    def provision_job_in_scheduler(self, job: SocaJob, stack_id: str):
        provisioning_time = self._context.scheduler.provision_job(
            job=job, stack_id=stack_id
        )

        job.provisioned = True
        job.params.stack_id = stack_id
        job.params.compute_stack = job.get_compute_stack()
        job.provisioning_time = arrow.get(provisioning_time).datetime
        self._context.job_cache.sync([job])
        self._context.job_cache.add_active_licenses([job])
        self._context.job_cache.clear_job_provisioning_error(job.job_id)

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                f'{self.log_tag(job)} Provisioned Job: {Utils.to_json(job)}'
            )
        else:
            self._logger.info(
                f'{self.log_tag(job)} Job Provisioned. ComputeStack: {job.get_compute_stack()}'
            )

        self._context.metrics.jobs_provisioned(queue_type=job.queue_type)

        pending_duration = arrow.utcnow() - job.queue_time

        self._context.metrics.jobs_pending_duration(
            queue_type=job.queue_type, duration_secs=pending_duration.seconds
        )

    def update_capacity(self):
        """
        Update capacity of an existing ASG or SpotFleet
        :return: List of SocaJobs that could not be provisioned due to limits
        """

        self.provisioning_util.check_budgets()

        self.provisioning_util.check_service_quota()

        self.provisioning_util.check_reserved_instance_usage()

        self.provisioning_util.check_licenses()

        result = self.provisioning_util.update_capacity()

        provisioned_jobs = result.provisioned_jobs
        unprovisioned_jobs = result.unprovisioned_jobs
        capacity_info = result.capacity_info

        if self.job.is_spot_capacity():
            spot_or_asg = 'SpotFleet'
        else:
            spot_or_asg = 'ASG'

        self._logger.info(
            f'{self.job.log_tag} {spot_or_asg}: {capacity_info.comment} ({capacity_info})'
        )

        for provisioned_job in provisioned_jobs:
            self.provision_job_in_scheduler(
                job=provisioned_job, stack_id=self.provisioning_util.stack.stack_id
            )

        if len(unprovisioned_jobs) > 0:
            raise exceptions.SocaException(
                error_code=errorcodes.RETRY_JOB_PROVISIONING,
                message=f'{len(unprovisioned_jobs)} of {len(self.jobs)} could not be provisioned at this time. '
                f'Provisioning will be retried for these jobs',
                ref=unprovisioned_jobs,
            )

    def create_new_stack(self):
        """
        Create a new CloudFormation Stack to provision Jobs
        :return: List of SocaJobs that could not be provisioned due to limits
        """

        self.provisioning_util.check_budgets()

        self.provisioning_util.ec2_dry_run()

        self.provisioning_util.check_service_quota()

        self.provisioning_util.check_reserved_instance_usage()

        self.provisioning_util.check_licenses()

        if self.is_batch:
            result = BatchCapacityHelper(
                context=self._context,
                jobs=self.jobs,
                provisioned_capacity=self.provisioning_util.provisioned_capacity,
            ).invoke()

            stack_builder = CloudFormationStackBuilder(
                context=self._context,
                job=self.job,
                target_capacity_override=result.capacity_info.target_capacity,
            )
            stack_id = stack_builder.build()

            spot_or_asg = 'ASG'
            if self.job.is_spot_capacity():
                spot_or_asg = 'SpotFleet'
            self._logger.info(
                f'{self.job.log_tag} {spot_or_asg}: {result.capacity_info.comment} ({result.capacity_info})'
            )

            for job in result.provisioned_jobs:
                self.provision_job_in_scheduler(job=job, stack_id=stack_id)
            unprovisioned_jobs = result.unprovisioned_jobs

        else:
            stack_builder = CloudFormationStackBuilder(
                context=self._context, job=self.job
            )
            stack_id = stack_builder.build()
            self.provision_job_in_scheduler(job=self.job, stack_id=stack_id)
            unprovisioned_jobs = []

        # publish metrics
        self._context.metrics.stacks_created(queue_type=self.job.queue_type)

        if self.job.is_spot_capacity():
            self._context.metrics.spotfleet_created(queue_type=self.job.queue_type)
        else:
            self._context.metrics.asg_created(queue_type=self.job.queue_type)

        if len(unprovisioned_jobs) > 0:
            raise exceptions.SocaException(
                error_code=errorcodes.RETRY_JOB_PROVISIONING,
                message=f'{len(unprovisioned_jobs)} of {len(self.jobs)} could not be provisioned at this time. '
                f'Provisioning will be retried for these jobs',
                ref=unprovisioned_jobs,
            )

    def provision_job_on_shared_capacity(self):
        job_group = self.job.get_job_group()
        provisioned_job_group = self.provisioning_util.stack.soca_job_group

        if job_group != provisioned_job_group:
            raise exceptions.SocaException(
                error_code=errorcodes.SHARED_CAPACITY_MISMATCH,
                message=f'Provisioned capacity does not match the desired capacity requirements.'
                f'Provisioned: {provisioned_job_group}, Required: {job_group}',
            )

        job_queue = self.job.queue
        provisioned_job_queue = self.provisioning_util.stack.soca_job_queue
        if job_queue != provisioned_job_queue:
            raise exceptions.SocaException(
                error_code=errorcodes.SHARED_CAPACITY_INVALID_QUEUE,
                message=f'Provisioned capacity queue does not match the desired capacity queue.'
                f'Provisioned: {provisioned_job_queue}, Required: {job_queue}',
            )

        desired_capacity = self.job.desired_capacity()
        if self.provisioning_util.is_spot_fleet:
            provisioned_capacity = self.provisioning_util.spot_fleet.target_capacity
        else:
            provisioned_capacity = (
                self.provisioning_util.auto_scaling_group.desired_capacity
            )

        if desired_capacity > provisioned_capacity:
            raise exceptions.SocaException(
                error_code=errorcodes.SHARED_CAPACITY_UNAVAILABLE,
                message=f'Not enough capacity available to run job on shared stack.'
                f'desired: {desired_capacity}, provisioned: {provisioned_capacity}',
            )

        for job in self.jobs:
            self.provision_job_in_scheduler(
                job=job, stack_id=self.provisioning_util.stack.stack_id
            )

    def handle_completed(self):
        # nothing to do for ephemeral capacity.
        # job has already been provisioned in scheduler when stack was created.
        # node housekeeper will deal with additional provisioning statuses and retries.
        if self.job.is_ephemeral_capacity():
            return

        if self.job.scaling_mode == SocaScalingMode.BATCH:
            # batch or dynamic
            self.update_capacity()

        else:
            # always on or terminate when idle
            self.provision_job_on_shared_capacity()

    def print_status(self):
        provisioning_status = self.provisioning_status
        self._logger.info(
            f'{self.log_tag()} '
            f'Stack: {self.job.get_compute_stack()}, '
            f'ProvisioningStatus: {provisioning_status}'
        )

    def invoke(self) -> ProvisionJobsResult:
        try:
            provisioning_status = self.provisioning_status
            if provisioning_status == ProvisioningStatus.NOT_PROVISIONED:
                self.print_status()
                self.create_new_stack()
                return ProvisionJobsResult(status=True)

            elif provisioning_status == ProvisioningStatus.COMPLETED:
                self.handle_completed()
                return ProvisionJobsResult(status=True)

            elif provisioning_status in (
                ProvisioningStatus.IN_PROGRESS,
                ProvisioningStatus.DELETE_IN_PROGRESS,
            ):
                self.print_status()
                return ProvisionJobsResult(status=False)

            elif provisioning_status in (
                ProvisioningStatus.FAILED,
                ProvisioningStatus.TIMEOUT,
            ):
                self.print_status()
                # For failed/timeout jobs, return a special error code to indicate
                # they should be left for the node housekeeper to clean up
                # rather than being continuously retried
                return ProvisionJobsResult(
                    status=False,
                    error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED,
                )

            else:
                return ProvisionJobsResult(status=True)

        except exceptions.SocaException as e:
            if e.error_code in (
                errorcodes.SPOT_FLEET_CAPACITY_UPDATE_IN_PROGRESS,
                errorcodes.MAX_PROVISIONED_INSTANCES_LIMIT,
            ):
                self._logger.info(f'{self.log_tag()} {e.message}')
                return ProvisionJobsResult(
                    status=False, error_code=e.error_code, exception=e
                )
            elif e.error_code == errorcodes.RETRY_JOB_PROVISIONING:
                self._logger.info(f'{self.log_tag()} {e.message}')
                return ProvisionJobsResult(
                    status=False,
                    unprovisioned_jobs=e.ref,
                    error_code=e.error_code,
                    exception=e,
                )
            else:
                # need to know the job context, to understand why provisioning is failing.
                if e.error_code == errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED:
                    self._logger.error(f'{self.log_tag()} {Utils.to_json(self.job)}')

                if e.exception is not None:
                    self._logger.exception(f'{self.log_tag()} {e}')
                else:
                    self._logger.error(f'{self.log_tag()} {e}')

                self._context.metrics.job_provisioning_failed(
                    queue_type=self.job.queue_type, error_code=e.error_code
                )
                return ProvisionJobsResult(
                    status=False, error_code=e.error_code, exception=e
                )

        except Exception as e:
            self._logger.exception(
                f'{self.log_tag()} provisioning failed: {e}', exc_info=e
            )
            self._context.metrics.job_provisioning_failed(
                queue_type=self.job.queue_type, error_code=errorcodes.GENERAL_ERROR
            )
            return ProvisionJobsResult(
                status=False, error_code=errorcodes.GENERAL_ERROR, exception=e
            )


class JobProvisioner(SocaService):
    """
    JobProvisioner subscribes to the JobProvisioningQueue and provisions the jobs.
     - JobProvisioner is initialized by JobProvisioningContext at soca-daemon startup.
     - JobProvisioningQueue and JobProvisioner has a 1-1 mapping relationship.
    """

    def __init__(self, context: ideascheduler.AppContext, queue: JobProvisioningQueue):
        self._queue = queue
        super().__init__(context)
        logger_name = f'job_provisioner_{queue.queue_type}'
        self._logger = context.logger(logger_name)
        self._context = context
        self._queue_poller_thread: Optional[Thread] = None
        self._exit: Optional[Event] = None
        self._is_running = False

        self._previous_job: Optional[SocaJob] = None
        self._current_job: Optional[SocaJob] = None

    def service_id(self) -> str:
        return f'{self.__class__.__name__}.QueueType.{self._queue.queue_type}'

    def _provision_with_retry_backoff(
        self, jobs: List[SocaJob], max_retries: int = 5
    ) -> ProvisionJobsResult:
        retry_count = 1
        backoff_in_seconds = 2
        sleep = 2

        while not self._exit.is_set():
            active_jobs = []

            start = Utils.current_time_ms()

            # check for first job in the batch if it's deleted. if deleted, skip entire batch
            job = jobs[0]
            if self._context.scheduler.is_job_queued_or_running(job_id=job.job_id):
                active_jobs = jobs
            else:
                self._logger.info(f'{job.log_tag} Job Deleted, skip provisioning.')

            total_ms = Utils.current_time_ms() - start
            self._logger.info(
                f'total jobs: {len(jobs)}, pre-processing time: {total_ms}ms'
            )

            if len(active_jobs) == 0:
                return ProvisionJobsResult(status=True)

            result = ProvisionJobs(
                context=self._context, jobs=active_jobs, logger=self._logger
            ).invoke()

            retry_count += 1

            if self._queue.queue_mode == SocaQueueMode.LICENSE_OPTIMIZED:
                # if job has failed due to unavailability of licenses, do not block the entire queue and try provisioning
                # next job if available.
                # the job will be picked up in the next minute cycle when job cache is refreshed and job monitor detects
                # that job is not yet provisioned.
                if (
                    Utils.is_not_empty(result.error_code)
                    and result.error_code == errorcodes.NOT_ENOUGH_LICENSES
                ):
                    return ProvisionJobsResult(status=True)

            # if job has failed due to CloudFormation stack failures (FAILED/TIMEOUT status),
            # do not block the entire queue and let the node housekeeper handle cleanup.
            # the job will be reset and retried when the housekeeper runs.
            if (
                Utils.is_not_empty(result.error_code)
                and result.error_code == errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED
            ):
                self._logger.info(
                    f'{jobs[0].log_tag} CloudFormation stack failed or timed out. '
                    f'Leaving for node housekeeper to clean up and retry.'
                )
                return ProvisionJobsResult(status=True)

            if retry_count > max_retries or result.status:
                return result

            self._exit.wait(timeout=sleep)

            sleep = Utils.get_retry_backoff_interval(
                current_retry=retry_count, backoff_in_seconds=backoff_in_seconds
            )

    def _drain_batch_queue(self) -> Dict[str, List[SocaJob]]:
        """
        Drain the batch queue to fetch all Jobs.

        Implementation waits for 3 seconds before returning the result. This means, jobs submitted within 3 seconds intervals
        will be processed in a single batch.

        :return: a mapping of jobs group -> list of jobs in the group
        """
        # job_group -> (job_id, job)
        batches: Dict[str, Dict[str, SocaJob]] = {}

        more_jobs_available = True

        while more_jobs_available:
            if self._exit.is_set():
                break

            iterations = 0
            while True:
                try:
                    job = self._queue.get(timeout=1)

                    if job.is_provisioned():
                        continue

                    job_group = job.get_job_group()
                    if job_group in batches:
                        jobs = batches[job_group]
                    else:
                        jobs: Dict[str, SocaJob] = OrderedDict()
                        batches[job_group] = jobs

                    jobs[job.job_id] = job

                    iterations += 1

                except JobProvisioningQueueEmpty:
                    break

            jobs_to_be_provisioned = 0
            for job_group, jobs in batches.items():
                expected_batch_size = OpenPBSQSelect(
                    context=self._context,
                    logger=self._logger,
                    log_tag=f'JobGroup: {job_group}',
                    job_group=job_group,
                    stack_id='tbd',
                ).get_count()
                current_batch_size = len(jobs)
                self._logger.info(
                    f'JobGroup: {job_group}, ExpectedBatchSize: {expected_batch_size}, CurrentBatchSize: {len(jobs)}'
                )
                if expected_batch_size < current_batch_size:
                    # if expected_batch_size less than current batch size, there is a mismatch in expectation.
                    # reset the batch to start clean in the next refresh cycle
                    batches[job_group] = OrderedDict()
                    self._logger.warning(
                        f'ExpectedBatchSize ({expected_batch_size}) < CurrentBatchSize ({len(jobs)}). '
                        f'Possible job update or deletion. Resetting batch.'
                    )
                jobs_to_be_provisioned += expected_batch_size

            total_jobs = 0
            for jobs in batches.values():
                total_jobs += len(jobs)

            more_jobs_available = total_jobs < jobs_to_be_provisioned
            if more_jobs_available:
                self._exit.wait(
                    timeout=self._context.config().get_int(
                        'scheduler.job_provisioning.batch_provisioning_wait_interval_seconds',
                        default=3,
                    )
                )

        result = {}
        for job_group in batches:
            result[job_group] = list(batches[job_group].values())

        return result

    def _poll_queue(self):
        while not self._exit.is_set():
            try:
                # batch scaling mode - provision all queued jobs in a group at once.
                if self._queue.queue_profile.scaling_mode == SocaScalingMode.BATCH:
                    batches = self._drain_batch_queue()

                    for batch in batches.values():
                        if batch is None or len(batch) == 0:
                            continue

                        self._logger.info(
                            f'{batch[0].log_tag} Provisioning {len(batch)} jobs in batch ...'
                        )

                        result = self._provision_with_retry_backoff(jobs=batch)

                        if result.status:
                            self._logger.info(
                                f'{batch[0].log_tag} {len(batch)} jobs in batch provisioned.'
                            )
                        else:
                            # unprovisioned jobs are returned when max provisioned instances or max job limits are set
                            # todo - limits is currently not implemented for batch jobs
                            if result.unprovisioned_jobs:
                                num_failed_jobs = len(result.unprovisioned_jobs)
                                for job in result.unprovisioned_jobs:
                                    self._context.job_cache.set_job_provisioning_error(
                                        job_id=job.job_id,
                                        error_code=result.get_error_code(),
                                        message=result.get_error_message(),
                                    )
                                    self._queue.put(job=job)
                            else:
                                num_failed_jobs = len(batch)
                                for job in batch:
                                    self._context.job_cache.set_job_provisioning_error(
                                        job_id=job.job_id,
                                        error_code=result.get_error_code(),
                                        message=result.get_error_message(),
                                    )
                                    self._queue.put(job=job)

                            self._logger.info(
                                f'{batch[0].log_tag} failed to provision {num_failed_jobs} jobs in batch. '
                                f'provisioning will be retried ...'
                            )

                else:
                    # ephemeral jobs - one stack per job
                    job = self._queue.get(timeout=1)

                    if job.is_provisioned():
                        continue

                    result = self._provision_with_retry_backoff(jobs=[job])

                    if result and not result.status:
                        if result.exception:
                            self._context.job_cache.set_job_provisioning_error(
                                job_id=job.job_id,
                                error_code=result.get_error_code(),
                                message=result.get_error_message(),
                            )
                        self._queue.put(job=job)

            except JobProvisioningQueueEmpty:
                pass
            except Exception as e:
                self._logger.exception(
                    f'exception while processing JobProvisioningQueue: {self._queue.queue_type}, '
                    f'Error: {e}'
                )
            finally:
                # add an artificial delay, after each job provisioning run, to ensure
                # we don't flood AWS with provisioning requests.
                self._exit.wait(
                    timeout=self._context.config().get_int(
                        'scheduler.job_provisioning.job_provisioning_interval_seconds',
                        default=1,
                    )
                )

    def _initialize(self):
        self._queue_poller_thread = Thread(
            name=f'job-queue-{self._queue.queue_type}', target=self._poll_queue
        )
        self._exit = Event()

    def start(self):
        if self._is_running:
            return
        self._initialize()
        self._is_running = True
        self._queue_poller_thread.start()

    def stop(self):
        if not self._is_running:
            return
        self._is_running = False
        self._exit.set()
        self._queue_poller_thread.join()
