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
from ideasdk.utils import Utils

from ideadatamodel import exceptions, errorcodes
from ideadatamodel.scheduler import HpcQueueProfile, SocaScalingMode
from ideascheduler.app.provisioning.job_provisioning_queue.hpc_queue_profiles_dao import (
    HpcQueueProfilesDAO,
)
from ideascheduler.app.scheduler import SocaJobBuilder
from ideascheduler.app.app_protocols import HpcQueueProfilesServiceProtocol
from ideascheduler.app.provisioning import JobProvisioningQueue, JobProvisioner

from typing import List, Dict, Optional
from threading import RLock


class HpcQueueProfilesService(SocaService, HpcQueueProfilesServiceProtocol):
    """
    Manage Queue Profiles for IDEA Scheduler

    Queue Profiles are used in critical path during job submissions, validations, events etc.

    Queue profile updates via API will trigger cache and DB updates.
    Any manual changes to DB, will need a server restart.

    Enabling or Disabling a queue profile starts or stops the JobProvisioner thread for the queue profile.

    For each queue provided in the queue profile, a scheduler queue is automatically created if the queue
    does not already exist in the scheduler.

    If a queue profile is deleted, scheduler queues are also deleted. A queue that cannot be deleted (eg. jobs are
    still queued or running) is left enabled and accepting jobs, and the failure is raised to the caller.
    """

    def __init__(self, context: ideascheduler.AppContext):
        super().__init__(context)
        self.context = context
        self.logger = context.logger('queue-profiles')

        self.queue_profile_dao = HpcQueueProfilesDAO(context)
        self.queue_profile_dao.initialize()

        # self.cache = QueueProfilesCache()
        self._provisioning_queues: Dict[str, JobProvisioningQueue] = {}
        self._provisioners: Dict[str, JobProvisioner] = {}
        self._is_running = False

        self._provisioning_queue_lock = RLock()

    def initialize_job_provisioner(self, queue_profile: HpcQueueProfile):
        if Utils.is_false(queue_profile.enabled):
            return

        with self._provisioning_queue_lock:
            # stop if applicable (re-entrant lock is good for lock within lock)
            self.stop_job_provisioner(queue_profile)

            provisioning_queue = JobProvisioningQueue(
                context=self.context, queue_profile=queue_profile
            )
            provisioner = JobProvisioner(context=self.context, queue=provisioning_queue)
            self._provisioning_queues[queue_profile.queue_profile_id] = (
                provisioning_queue
            )
            self._provisioners[queue_profile.queue_profile_id] = provisioner

            provisioner.start()

    def stop_job_provisioner(self, queue_profile: HpcQueueProfile):
        with self._provisioning_queue_lock:
            provisioner = self._provisioners.get(queue_profile.queue_profile_id, None)
            if provisioner is not None:
                provisioner.stop()
                del self._provisioners[queue_profile.queue_profile_id]

            provisioning_queue = self._provisioning_queues.get(
                queue_profile.queue_profile_id, None
            )
            if provisioning_queue is not None:
                del self._provisioning_queues[queue_profile.queue_profile_id]

    def validate_and_sanitize_queue_profile(self, queue_profile: HpcQueueProfile):
        if queue_profile is None:
            raise exceptions.invalid_params('queue_profile is required')
        if Utils.is_empty(queue_profile.name):
            raise exceptions.invalid_params('queue_profile.name is required')
        if Utils.is_empty(queue_profile.queues):
            raise exceptions.invalid_params('queue_profile.queues is required')
        if Utils.is_empty(queue_profile.scaling_mode) and not Utils.get_as_bool(
            queue_profile.keep_forever, False
        ):
            raise exceptions.invalid_params('queue_profile.scaling_mode is required')
        if Utils.is_empty(queue_profile.queue_mode):
            raise exceptions.invalid_params('queue_profile.queue_mode is required')
        if Utils.is_empty(queue_profile.projects):
            raise exceptions.invalid_params('queue_profile.projects[] is required')
        for project in queue_profile.projects:
            if Utils.is_empty(project.project_id):
                raise exceptions.invalid_params('queue_profile.projects[] is required')

        terminate_when_idle = queue_profile.terminate_when_idle
        if (
            queue_profile.scaling_mode is not None
            and queue_profile.scaling_mode == SocaScalingMode.BATCH
        ):
            if terminate_when_idle is None or terminate_when_idle <= 0:
                raise exceptions.invalid_params(
                    'queue_profile.terminate_when_idle must be required and > 0 when scaling_mode == BATCH'
                )
        elif (
            queue_profile.scaling_mode is not None
            and queue_profile.scaling_mode == SocaScalingMode.SINGLE_JOB
        ):
            if terminate_when_idle is not None and terminate_when_idle > 0:
                raise exceptions.invalid_params(
                    'queue_profile.terminate_when_idle must be 0 when scaling_mode == SINGLE_JOB'
                )

        keep_forever = queue_profile.keep_forever
        if keep_forever is not None and keep_forever:
            if queue_profile.stack_uuid is None:
                queue_profile.stack_uuid = Utils.uuid()

        if queue_profile.default_job_params is not None:
            default_job_params = Utils.to_dict(queue_profile.default_job_params)
        else:
            default_job_params = {}

        builder = SocaJobBuilder(context=self.context, params=default_job_params)

        validation_result = builder.validate()
        if not validation_result.is_valid():
            raise exceptions.soca_exception(
                error_code=errorcodes.VALIDATION_FAILED,
                message='Job Parameter validation failed',
                ref=validation_result,
            )

    def cache_get(
        self,
        queue_profile_id: str = None,
        queue_profile_name: str = None,
        queue_name: str = None,
    ) -> Optional[HpcQueueProfile]:
        if Utils.is_not_empty(queue_profile_id):
            return (
                self.context.cache()
                .short_term()
                .get(f'queue-profile.id.{queue_profile_id}')
            )
        elif Utils.is_not_empty(queue_profile_name):
            return (
                self.context.cache()
                .short_term()
                .get(f'queue-profile.name.{queue_profile_name}')
            )
        elif Utils.is_not_empty(queue_name):
            return (
                self.context.cache()
                .short_term()
                .get(f'queue-profile.queue.{queue_name}')
            )

    def cache_set(self, queue_profile: HpcQueueProfile):
        self.context.cache().short_term().set(
            f'queue-profile.id.{queue_profile.queue_profile_id}', queue_profile
        )
        self.context.cache().short_term().set(
            f'queue-profile.name.{queue_profile.name}', queue_profile
        )
        for queue in queue_profile.queues:
            self.context.cache().short_term().set(
                f'queue-profile.queue.{queue}', queue_profile
            )

    def cache_clear(self, queue_profile: HpcQueueProfile):
        self.context.cache().short_term().delete(
            f'queue-profile.id.{queue_profile.queue_profile_id}'
        )
        self.context.cache().short_term().delete(
            f'queue-profile.name.{queue_profile.name}'
        )
        for queue in queue_profile.queues:
            self.context.cache().short_term().delete(f'queue-profile.queue.{queue}')

    def create_queue_profile(self, queue_profile: HpcQueueProfile) -> HpcQueueProfile:
        self.validate_and_sanitize_queue_profile(queue_profile)

        existing = self.queue_profile_dao.get_queue_profile_by_name(queue_profile.name)
        if existing is not None:
            raise exceptions.invalid_params(
                f'queue profile already exists for queue profile name: {queue_profile.name}'
            )

        # set enabled to explicit false during creation
        queue_profile.enabled = False

        db_queue_profile = self.queue_profile_dao.convert_to_db(queue_profile)
        db_created = self.queue_profile_dao.create_queue_profile(db_queue_profile)

        created = self.queue_profile_dao.convert_from_db(db_created)

        if Utils.is_not_empty(queue_profile.queues):
            self._create_queues(queue_names=created.queues)

        self.cache_set(created)

        return created

    def get_queue_profile(
        self,
        queue_profile_id: str = None,
        queue_profile_name: str = None,
        queue_name: str = None,
    ) -> HpcQueueProfile:
        queue_profile = self.cache_get(
            queue_profile_id=queue_profile_id,
            queue_profile_name=queue_profile_name,
            queue_name=queue_name,
        )
        if queue_profile is not None:
            return queue_profile

        db_queue_profile = None
        if Utils.is_not_empty(queue_profile_id):
            db_queue_profile = self.queue_profile_dao.get_queue_profile_by_id(
                queue_profile_id
            )
        elif Utils.is_not_empty(queue_profile_name):
            db_queue_profile = self.queue_profile_dao.get_queue_profile_by_name(
                queue_profile_name
            )
        elif Utils.is_not_empty(queue_name):
            db_queue_profile = self.queue_profile_dao.get_queue_profile_by_queue(
                queue_name
            )

        if db_queue_profile is None:
            if Utils.is_not_empty(queue_profile_id):
                raise exceptions.soca_exception(
                    error_code=errorcodes.SCHEDULER_QUEUE_PROFILE_NOT_FOUND,
                    message=f'queue profile not found for id: {queue_profile_id}',
                )
            if Utils.is_not_empty(queue_profile_name):
                raise exceptions.soca_exception(
                    error_code=errorcodes.SCHEDULER_QUEUE_PROFILE_NOT_FOUND,
                    message=f'queue profile not found for name: {queue_profile_name}',
                )
            if Utils.is_not_empty(queue_name):
                raise exceptions.soca_exception(
                    error_code=errorcodes.SCHEDULER_QUEUE_PROFILE_NOT_FOUND,
                    message=f'queue profile not found for queue: {queue_name}',
                )

        queue_profile = self.queue_profile_dao.convert_from_db(db_queue_profile)
        self.cache_set(queue_profile)

        return queue_profile

    def list_queue_profiles(self) -> List[HpcQueueProfile]:
        queue_profiles = []
        db_queue_profiles = self.queue_profile_dao.list_queue_profiles()
        for db_queue_profile in db_queue_profiles:
            queue_profiles.append(
                self.queue_profile_dao.convert_from_db(db_queue_profile)
            )
        queue_profiles.sort(key=lambda queue_profile: queue_profile.name)
        return queue_profiles

    def get_provisioning_queue(
        self, queue_profile_name: str
    ) -> Optional[JobProvisioningQueue]:
        if Utils.is_empty(queue_profile_name):
            return None
        queue_profile = self.get_queue_profile(queue_profile_name=queue_profile_name)
        if queue_profile is None:
            return None

        with self._provisioning_queue_lock:
            return self._provisioning_queues.get(queue_profile.queue_profile_id, None)

    def enable_queue_profile(
        self, queue_profile_id: str = None, queue_profile_name: str = None
    ):
        queue_profile = self.get_queue_profile(queue_profile_id, queue_profile_name)
        if Utils.is_true(queue_profile.enabled):
            return

        db_queue_profile = {
            'queue_profile_id': queue_profile.queue_profile_id,
            'enabled': True,
        }
        db_updated = self.queue_profile_dao.update(db_queue_profile)
        updated_queue_profile = self.queue_profile_dao.convert_from_db(db_updated)

        self.cache_set(updated_queue_profile)

        self.initialize_job_provisioner(updated_queue_profile)

    def disable_queue_profile(
        self, queue_profile_id: str = None, queue_profile_name: str = None
    ):
        queue_profile = self.get_queue_profile(queue_profile_id, queue_profile_name)
        if Utils.is_false(queue_profile.enabled):
            return

        db_queue_profile = {
            'queue_profile_id': queue_profile.queue_profile_id,
            'enabled': False,
        }
        db_updated = self.queue_profile_dao.update(db_queue_profile)
        updated_queue_profile = self.queue_profile_dao.convert_from_db(db_updated)

        self.cache_set(updated_queue_profile)

        self.stop_job_provisioner(updated_queue_profile)

    def update_queue_profile(self, queue_profile: HpcQueueProfile) -> HpcQueueProfile:
        self.validate_and_sanitize_queue_profile(queue_profile)

        if Utils.are_empty(queue_profile.queue_profile_id, queue_profile.name):
            raise exceptions.invalid_params(
                'Either queue_profile.queue_profile_id or queue_profile.name is required'
            )

        existing_queue_profile = self.get_queue_profile(
            queue_profile_name=queue_profile.name,
            queue_profile_id=queue_profile.queue_profile_id,
        )
        if existing_queue_profile is None:
            raise exceptions.invalid_params('queue_profile not found')

        db_updates = self.queue_profile_dao.convert_to_db(queue_profile)

        existing_queues = Utils.get_as_list(existing_queue_profile.queues, [])
        # an update that carries no queues means no queue changes, never delete them all
        new_queues = Utils.get_as_list(queue_profile.queues, existing_queues)
        queues_to_delete = list(set(existing_queues) - set(new_queues))
        queues_to_create = list(set(new_queues) - set(existing_queues))

        # scheduler queues are reconciled before the profile is persisted, so it never lists
        # a queue the scheduler lacks. create runs first so a failed create can't have deleted one.
        if Utils.is_not_empty(queues_to_create):
            self._create_queues(queue_names=queues_to_create)

        queue_deletion_failures = {}
        if Utils.is_not_empty(queues_to_delete):
            queue_deletion_failures = self._delete_queues(queue_names=queues_to_delete)

        # a queue that could not be deleted is still live in the scheduler: keep it on the
        # profile so its queued jobs keep a servicing provisioner.
        retained_queues = [
            queue_name
            for queue_name in existing_queues
            if queue_name in queue_deletion_failures
        ]

        # perform db update
        db_updated_queue_profile = self.queue_profile_dao.update(
            {
                **db_updates,
                'queue_profile_id': existing_queue_profile.queue_profile_id,
                'queues': new_queues + retained_queues,
            }
        )

        updated_queue_profile = self.queue_profile_dao.convert_from_db(
            db_updated_queue_profile
        )

        self.cache_set(updated_queue_profile)

        # reinitialize job provisioner (if applicable)
        self.initialize_job_provisioner(updated_queue_profile)

        if queue_deletion_failures:
            raise self._queue_deletion_failed_exception(
                failures=queue_deletion_failures,
                message=f'queue profile: {updated_queue_profile.name} updated',
            )

        return updated_queue_profile

    def delete_queue_profile(
        self,
        queue_profile_id: str = None,
        queue_profile_name: str = None,
        delete_queues: bool = True,
    ):
        queue_profile = self.get_queue_profile(queue_profile_id, queue_profile_name)

        # scheduler queues go first: a queue that cannot be deleted (busy) keeps the
        # profile, its cache entry and its provisioner as they are.
        delete_queues = Utils.get_as_bool(delete_queues, True)
        if delete_queues and Utils.is_not_empty(queue_profile.queues):
            queue_deletion_failures = self._delete_queues(
                queue_names=queue_profile.queues
            )
            if queue_deletion_failures:
                raise self._queue_deletion_failed_exception(
                    failures=queue_deletion_failures,
                    message=f'queue profile: {queue_profile.name} not deleted',
                )

        self.stop_job_provisioner(queue_profile)

        self.cache_clear(queue_profile)

        self.queue_profile_dao.delete_queue_profile(
            queue_profile_id=queue_profile.queue_profile_id
        )

    def _create_queues(self, queue_names: List[str]) -> int:
        """
        creates scheduler queues for all queues provided in queue_names
        :param queue_names:
        :return: count of queues created successfully.
        """
        created_count = 0
        for queue_name in queue_names:
            try:
                self.context.scheduler.create_queue(queue_name=queue_name)
                created_count += 1
            except exceptions.SocaException as e:
                if e.error_code == errorcodes.SCHEDULER_QUEUE_ALREADY_EXISTS:
                    created_count += 1
                else:
                    raise e
        return created_count

    def create_queues(
        self,
        queue_names: List[str],
        queue_profile_id: str = None,
        queue_profile_name: str = None,
        update_db=True,
        check_existing_profile=True,
    ):
        if Utils.is_empty(queue_names):
            raise exceptions.invalid_params('queue_names[] is required')

        queue_profile = self.get_queue_profile(queue_profile_id, queue_profile_name)

        if check_existing_profile:
            for queue_name in queue_names:
                existing = self.queue_profile_dao.get_queue_profile_by_queue(queue_name)
                if existing is None:
                    continue
                if existing['queue_profile_id'] != queue_profile.queue_profile_id:
                    raise exceptions.invalid_params(
                        f'queue: {queue_name} is already associated with queue profile: {existing["name"]}'
                    )

        # re-init provisioner if new queue is created in scheduler, or an existing queue is added to the queue profile
        created_count = self._create_queues(queue_names)
        reinitialize_provisioner = created_count > 0

        # dao.update() replaces the queues attribute, so the profile's existing queues
        # must be carried over. order is preserved and duplicates are dropped.
        existing_queues = Utils.get_as_list(queue_profile.queues, [])
        updated_queues = list(existing_queues)
        for queue_name in queue_names:
            if queue_name not in updated_queues:
                updated_queues.append(queue_name)

        if not reinitialize_provisioner:
            # find delta between existing queue names and new queue names
            reinitialize_provisioner = len(updated_queues) > len(existing_queues)

        # no change, nothing to do
        if not reinitialize_provisioner:
            return

        if update_db:
            db_queue_profile = {
                'queue_profile_id': queue_profile_id,
                'queues': updated_queues,
            }
            db_updated_queue_profile = self.queue_profile_dao.update(db_queue_profile)
            queue_profile = self.queue_profile_dao.convert_from_db(
                db_updated_queue_profile
            )
            self.cache_set(queue_profile)

        self.initialize_job_provisioner(queue_profile)

    def _delete_queues(
        self, queue_names: List[str]
    ) -> Dict[str, exceptions.SocaException]:
        """
        Calls scheduler delete queues for all queues provided in queue_names
        :param queue_names:
        :return: queue names that could not be deleted, mapped to the scheduler failure
        """
        failures: Dict[str, exceptions.SocaException] = {}
        for queue_name in queue_names:
            try:
                self.context.scheduler.delete_queue(queue_name=queue_name)
            except exceptions.SocaException as e:
                self.logger.error(
                    f'failed to delete scheduler queue: {queue_name}: {e}'
                )
                failures[queue_name] = e
        return failures

    @staticmethod
    def _queue_deletion_failed_exception(
        failures: Dict[str, exceptions.SocaException], message: str
    ) -> exceptions.SocaException:
        """
        builds the exception to be raised when scheduler queues could not be deleted.
        the queues are left enabled and accepting jobs - an operator must drain and delete them.
        """
        error_codes = set(failure.error_code for failure in failures.values())
        if len(error_codes) == 1:
            error_code = error_codes.pop()
        else:
            error_code = errorcodes.SCHEDULER_ERROR
        entries = ', '.join(
            f'{queue_name}: {failure.message}'
            for queue_name, failure in sorted(failures.items())
        )
        return exceptions.soca_exception(
            error_code=error_code,
            message=f'{message}, but below scheduler queues could not be deleted and are still active: {entries}',
        )

    def delete_queues(
        self,
        queue_names: List[str],
        queue_profile_id: str = None,
        queue_profile_name: str = None,
        update_db=True,
        initialize_job_provisioner=True,
    ):
        if Utils.is_empty(queue_names):
            raise exceptions.invalid_params('queue_names[] is required')

        queue_profile = self.get_queue_profile(queue_profile_id, queue_profile_name)

        # copy: a validation failure below must not leave the cached profile mutated
        updated_queues = list(Utils.get_as_list(queue_profile.queues, []))
        for queue_name in queue_names:
            if queue_name not in updated_queues:
                raise exceptions.invalid_params(
                    f'queue name: {queue_name} is not associated with queue profile: {queue_profile.name}'
                )
            updated_queues.remove(queue_name)

        queue_deletion_failures = self._delete_queues(queue_names)

        if update_db:
            # dao.update() replaces the queues attribute: write the remaining queues,
            # not the deleted ones.
            db_queue_profile = {
                'queue_profile_id': queue_profile_id,
                'queues': updated_queues,
            }
            db_updated_queue_profile = self.queue_profile_dao.update(db_queue_profile)
            queue_profile = self.queue_profile_dao.convert_from_db(
                db_updated_queue_profile
            )
            self.cache_set(queue_profile)

        if initialize_job_provisioner:
            self.initialize_job_provisioner(queue_profile)

        if queue_deletion_failures:
            raise self._queue_deletion_failed_exception(
                failures=queue_deletion_failures,
                message=f'queues removed from queue profile: {queue_profile.name}',
            )

    def start(self):
        if self._is_running:
            return
        self._is_running = True

        self.queue_profile_dao.initialize()

        db_queue_profiles = self.queue_profile_dao.list_queue_profiles()
        queue_profiles = []
        for db_queue_profile in db_queue_profiles:
            queue_profile = self.queue_profile_dao.convert_from_db(db_queue_profile)
            # create queues if they do not exist on start up.
            # this also addresses scenarios where a new scheduler is launched (after upgrade)
            self._create_queues(queue_profile.queues)
            queue_profiles.append(queue_profile)

        for queue_profile in queue_profiles:
            self.initialize_job_provisioner(queue_profile)

    def stop(self):
        if not self._is_running:
            return
        queue_profiles = self.list_queue_profiles()
        for queue_profile in queue_profiles:
            self.stop_job_provisioner(queue_profile)

        self._is_running = False
