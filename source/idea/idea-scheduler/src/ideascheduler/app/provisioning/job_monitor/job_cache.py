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
from ideadatamodel import SocaJob, SocaJobExecutionHost, EC2Instance, SocaCapacityType
from ideascheduler.app.app_protocols import JobCacheProtocol
from ideasdk.utils import Utils

from typing import List, Optional, Dict
import dataset
import os
import logging
from threading import Event
import arrow


JOBS_TABLE = 'jobs'
FINISHED_JOBS_TABLE = 'finished_jobs'
EXECUTION_HOSTS_TABLE = 'execution_hosts'
ACTIVE_JOB_LICENSES_TABLE = 'active_job_licenses'
JOB_PROVISIONING_ERRORS = 'job_provisioning_errors'


class JobsDB:
    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger()

        self.db: Optional[dataset.Database] = None

        db_dir = f'{self._context.get_scheduler_app_deploy_dir()}/db'
        db_file = f'{db_dir}/job-cache-v3.db'
        os.makedirs(db_dir, exist_ok=True)
        self.connection_string = f'sqlite:///{db_file}'

        self.init_db()

    def init_db(self):
        self.db = dataset.connect(self.connection_string)
        self.init_tables()
        self.init_indices()

    def init_tables(self):

        self.db.create_table(table_name=JOBS_TABLE)
        self.db.create_table(table_name=FINISHED_JOBS_TABLE)
        self.db.create_table(table_name=EXECUTION_HOSTS_TABLE)
        self.db.create_table(table_name=ACTIVE_JOB_LICENSES_TABLE)
        self.db.create_table(table_name=JOB_PROVISIONING_ERRORS)
        self.db[JOBS_TABLE].insert_ignore({
            'id': 0,
            'job_id': 'NULL',
            'job_uid': 'NULL',
            'job_group': 'NULL',
            'owner': 'NULL',
            'project': 'NULL',
            'state': 'NULL',
            'queue': 'NULL',
            'name': 'NULL',
            'provisioned': False,
            'queue_profile': 'NULL',
            'queue_time': arrow.get().datetime,
            'job_data': 'NULL'
        }, ['id'])
        self.db[FINISHED_JOBS_TABLE].insert_ignore({
            'id': 0,
            'job_id': 'NULL',
            'job_uid': 'NULL',
            'job_group': 'NULL',
            'owner': 'NULL',
            'project': 'NULL',
            'state': 'NULL',
            'queue': 'NULL',
            'name': 'NULL',
            'provisioned': False,
            'queue_profile': 'NULL',
            'queue_time': arrow.get().datetime,
            'job_data': 'NULL'
        }, ['id'])
        self.db[EXECUTION_HOSTS_TABLE].insert_ignore({
            'id': 0,
            'job_id': 'NULL',
            'host': 'NULL'
        }, ['id'])
        self.db[ACTIVE_JOB_LICENSES_TABLE].insert_ignore({
            'id': 0,
            'job_id': 'NULL',
            'license_name': 'NULL',
            'count': 0
        }, ['id'])
        self.db[JOB_PROVISIONING_ERRORS].insert_ignore({
            'id': 0,
            'job_id': 'NULL',
            'error_code': 'NULL',
            'message': 'NULL'
        }, ['id'])

    def init_indices(self):
        # jobs indices
        if not self.db[JOBS_TABLE].has_index('jobs_job_id_index'):
            self.db[JOBS_TABLE].create_index(columns=['job_id'], name='jobs_job_id_index')
        if not self.db[JOBS_TABLE].has_index('jobs_owner_index'):
            self.db[JOBS_TABLE].create_index(columns=['owner'], name='jobs_owner_index')
        if not self.db[JOBS_TABLE].has_index('jobs_project_owner_index'):
            self.db[JOBS_TABLE].create_index(columns=['project', 'owner'], name='jobs_project_owner_index')
        if not self.db[JOBS_TABLE].has_index('jobs_job_group_state_index'):
            self.db[JOBS_TABLE].create_index(columns=['job_group', 'state'], name='jobs_job_group_state_index')
        if not self.db[JOBS_TABLE].has_index('jobs_queue_profile_job_state_provisioned_index'):
            self.db[JOBS_TABLE].create_index(columns=['queue_profile', 'state', 'provisioned'],
                                             name='jobs_queue_profile_job_state_provisioned_index')

        # execution host indices
        if not self.db[EXECUTION_HOSTS_TABLE].has_index('exec_hosts_job_id_host_index'):
            self.db[EXECUTION_HOSTS_TABLE].create_index(columns=['job_id', 'host'], name='exec_hosts_job_id_host_index')

        # finished jobs indices
        if not self.db[FINISHED_JOBS_TABLE].has_index('finished_jobs_job_id_index'):
            self.db[FINISHED_JOBS_TABLE].create_index(columns=['job_id'], name='finished_jobs_job_id_index')
        if not self.db[FINISHED_JOBS_TABLE].has_index('finished_jobs_owner_index'):
            self.db[FINISHED_JOBS_TABLE].create_index(columns=['owner'], name='finished_jobs_owner_index')
        if not self.db[FINISHED_JOBS_TABLE].has_index('finished_jobs_project_index'):
            self.db[FINISHED_JOBS_TABLE].create_index(columns=['project'], name='finished_jobs_project_index')
        if not self.db[FINISHED_JOBS_TABLE].has_index('finished_jobs_job_group_index'):
            self.db[FINISHED_JOBS_TABLE].create_index(columns=['job_group'], name='finished_jobs_job_group_index')

        # job licenses table
        if not self.db[ACTIVE_JOB_LICENSES_TABLE].has_index('job_licenses_job_id_index'):
            self.db[ACTIVE_JOB_LICENSES_TABLE].create_index(columns=['job_id'], name='job_licenses_job_id_index')
        if not self.db[ACTIVE_JOB_LICENSES_TABLE].has_index('job_licenses_license_name_index'):
            self.db[ACTIVE_JOB_LICENSES_TABLE].create_index(columns=['license_name'], name='job_licenses_license_name_index')

        # job provisioning errors
        if not self.db[JOB_PROVISIONING_ERRORS].has_index('job_provisioning_errors_job_id_index'):
            self.db[JOB_PROVISIONING_ERRORS].create_index(columns=['job_id'], name='job_provisioning_errors_job_id_index')

    def get(self, job_id: str) -> Optional[SocaJob]:
        entry = self.db[JOBS_TABLE].find_one(job_id=job_id)
        return self.convert_db_entry_to_job(entry)

    def add(self, job: SocaJob):
        with self.db as tx:
            tx[JOBS_TABLE].upsert(
                row={
                    'job_id': job.job_id,
                    'job_group': job.job_group,
                    'job_uid': job.job_uid,
                    'desired_capacity': job.desired_capacity(),
                    'state': job.state.value,
                    'owner': job.owner,
                    'queue': job.queue,
                    'queue_profile': job.queue_type,
                    'project': job.project,
                    'provisioned': job.is_provisioned(),
                    'job_data': Utils.to_json(job)
                },
                keys=['job_id']
            )

    def add_finished_job(self, job: SocaJob):
        with self.db as tx:
            tx[FINISHED_JOBS_TABLE].upsert(
                row={
                    'job_id': job.job_id,
                    'job_group': job.job_group,
                    'job_uid': job.job_uid,
                    'desired_capacity': job.desired_capacity(),
                    'state': job.state.value,
                    'owner': job.owner,
                    'queue': job.queue,
                    'queue_profile': job.queue_type,
                    'project': job.project,
                    'job_data': Utils.to_json(job)
                },
                keys=['job_id']
            )

    def get_finished_job(self, job_id: str) -> Optional[SocaJob]:
        entry = self.db[FINISHED_JOBS_TABLE].find_one(job_id=job_id)
        if entry is None:
            return None
        return self.convert_db_entry_to_job(entry)

    def add_license_ask(self, jobs: List[SocaJob]):
        if Utils.is_empty(jobs):
            return
        with self.db as tx:
            for job in jobs:
                if Utils.is_empty(job.job_id):
                    continue
                if Utils.is_empty(job.params):
                    continue
                if Utils.is_empty(job.params.licenses):
                    continue
                for license_ask in job.params.licenses:
                    if Utils.is_empty(license_ask.name):
                        continue
                    if Utils.get_as_int(license_ask.count, 0) <= 0:
                        continue
                    tx[ACTIVE_JOB_LICENSES_TABLE].upsert(
                        row={
                            'job_id': job.job_id,
                            'license_name': license_ask.name,
                            'count': license_ask.count
                        },
                        keys=['job_id', 'license_name']
                    )

    def add_many(self, jobs: List[SocaJob]):
        with self.db:
            for job in jobs:
                self.add(job)

    def delete(self, job_id: str):
        with self.db as tx:
            tx[JOBS_TABLE].delete(job_id=job_id)
            tx[ACTIVE_JOB_LICENSES_TABLE].delete(job_id=job_id)
            tx[JOB_PROVISIONING_ERRORS].delete(job_id=job_id)

    def delete_many(self, job_ids: List[str]):
        with self.db:
            for job_id in job_ids:
                self.delete(job_id)

    def query(self, **kwargs) -> List[SocaJob]:
        jobs = []
        result = self.db[JOBS_TABLE].find(**kwargs)
        for entry in result:
            job = self.convert_db_entry_to_job(entry, fetch_errors=True)
            if job is None:
                continue
            jobs.append(job)
        return jobs

    def query_finished_jobs(self, **kwargs) -> List[SocaJob]:
        jobs = []
        result = self.db[FINISHED_JOBS_TABLE].find(**kwargs)
        for entry in result:
            job = self.convert_db_entry_to_job(entry)
            if job is None:
                continue
            jobs.append(job)
        return jobs

    def exists(self, job_id: str) -> bool:
        entry = self.db[JOBS_TABLE].find_one(job_id=job_id)
        return entry is not None

    def add_execution_host(self, job_id: str, execution_host: SocaJobExecutionHost):
        with self.db as tx:
            tx[EXECUTION_HOSTS_TABLE].upsert(
                row={
                    'job_id': job_id,
                    'host': execution_host.host,
                    'execution_host_data': Utils.to_json(execution_host)
                },
                keys=['job_id', 'host']
            )

    def get_execution_hosts(self, job_id: str) -> Optional[List[SocaJobExecutionHost]]:
        result = self.db[EXECUTION_HOSTS_TABLE].find(job_id=job_id)
        hosts = []
        for entry in result:
            execution_host_data = Utils.get_value_as_string('execution_host_data', entry)
            hosts.append(SocaJobExecutionHost(**Utils.from_json(execution_host_data)))
        return hosts

    def delete_execution(self, job_id: str):
        with self.db as tx:
            tx[EXECUTION_HOSTS_TABLE].delete(job_id=job_id)

    def convert_db_entry_to_job(self, entry: Dict, fetch_errors: bool = False) -> Optional[SocaJob]:
        if entry is None:
            return None
        job_data = Utils.get_value_as_string('job_data', entry)
        if job_data is None:
            return None
        if job_data == 'NULL':
            return None
        job = SocaJob(**Utils.from_json(job_data))
        if fetch_errors:
            error = self.db[JOB_PROVISIONING_ERRORS].find_one(job_id=job.job_id)
            error_message = Utils.get_value_as_string('message', error)
            job.error_message = error_message
        return job

    def set_job_provisioning_error(self, job_id: str, error_code: str, message: str):
        with self.db as tx:
            tx[JOB_PROVISIONING_ERRORS].upsert(
                row={
                    'job_id': job_id,
                    'error_code': error_code,
                    'message': message
                },
                keys=['job_id']
            )

    def clear_job_provisioning_error(self, job_id: str):
        if Utils.is_empty(job_id):
            return
        with self.db as tx:
            tx[JOB_PROVISIONING_ERRORS].delete(job_id=job_id)


class JobCache(JobCacheProtocol):
    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger()
        self._jobs_db = JobsDB(context=self._context)
        self._is_ready = Event()

    def sync(self, jobs: List[SocaJob]):
        self._jobs_db.add_many(jobs=jobs)

    def list_jobs(self, _limit: int = -1, _offset: int = 0, **kwargs) -> List[SocaJob]:
        if _limit > 0:
            return self._jobs_db.query(**kwargs, _limit=_limit, _offset=_offset, order_by='-id')
        else:
            return self._jobs_db.query(**kwargs)

    def list_completed_jobs(self, _limit: int = -1, _offset: int = 0, **kwargs) -> List[SocaJob]:
        if _limit > 0:
            return self._jobs_db.query_finished_jobs(**kwargs, _limit=_limit, _offset=_offset, order_by='-id')
        else:
            return self._jobs_db.query_finished_jobs(**kwargs)

    def get_job(self, job_id: str) -> Optional[SocaJob]:
        return self._jobs_db.get(job_id=job_id)

    def get_completed_job(self, job_id: str) -> Optional[SocaJob]:
        return self._jobs_db.get_finished_job(job_id)

    def delete_jobs(self, job_ids: List[str]):
        self._jobs_db.delete_many(job_ids=job_ids)

    def log_job_execution(self, job_id: str, execution_host: SocaJobExecutionHost,
                          instance: Optional[EC2Instance] = None):

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(f'Updating Execution Host: {execution_host}')

        if instance is None and execution_host.instance_id is not None:
            instance = self._context.instance_cache.get_instance(
                instance_id=execution_host.instance_id
            )

        if instance is not None:
            if execution_host.capacity_type is None:
                execution_host.capacity_type = SocaCapacityType.resolve(instance.soca_capacity_type)
            if execution_host.instance_id is None:
                execution_host.instance_id = instance.instance_id
            if execution_host.instance_type is None:
                execution_host.instance_type = instance.instance_type
            if execution_host.tenancy is None:
                execution_host.tenancy = instance.placement_tenancy
            if execution_host.reservation is None:
                execution_host.reservation = instance.capacity_reservation_id

        self._jobs_db.add_execution_host(job_id=job_id, execution_host=execution_host)

    def get_job_execution_hosts(self, job_id: str) -> Optional[List[SocaJobExecutionHost]]:
        return self._jobs_db.get_execution_hosts(job_id=job_id)

    def delete_job_execution_hosts(self, job_id: str):
        self._jobs_db.delete_execution(job_id=job_id)

    def add_finished_job(self, job: SocaJob):
        self._jobs_db.add_finished_job(job)

    def get_jobs_table(self) -> dataset.Table:
        return self._jobs_db.db[JOBS_TABLE]

    def get_connection(self) -> dataset.Database:
        return self._jobs_db.db

    def is_ready(self) -> bool:
        return self._is_ready.is_set()

    def get_desired_capacity(self, job_group: str) -> int:
        result = self._jobs_db.db.query(f"select sum(desired_capacity) as desired_capacity from jobs "
                                        f"where job_group='{job_group}'")
        for entry in result:
            return Utils.get_value_as_int('desired_capacity', entry, 0)
        return 0

    def get_active_jobs(self, queue_profile: str) -> int:
        result = self._jobs_db.db.query(f"select count(*) as active_jobs from jobs where "
                                        f"queue_profile='{queue_profile}' "
                                        f"and state in ('queued', 'running') "
                                        f"and provisioned = 1")
        for entry in result:
            return Utils.get_value_as_int('active_jobs', entry)
        return 0

    def get_count(self, **kwargs) -> int:
        return self._jobs_db.db[JOBS_TABLE].count(**kwargs)

    def get_completed_jobs_count(self, **kwargs) -> int:
        return self._jobs_db.db[FINISHED_JOBS_TABLE].count(**kwargs)

    def get_active_license_count(self, license_name: str) -> int:
        result = self._jobs_db.db.query(f"select sum(count) as active_count from active_job_licenses "
                                        f"where license_name='{license_name}'")
        for entry in result:
            return Utils.get_value_as_int('active_count', entry, 0)
        return 0

    def set_ready(self):
        self._is_ready.set()

    def add_active_licenses(self, jobs: List[SocaJob]):
        self._jobs_db.add_license_ask(jobs)

    def convert_db_entry_to_job(self, entry: Dict) -> Optional[SocaJob]:
        return self._jobs_db.convert_db_entry_to_job(entry)

    def set_job_provisioning_error(self, job_id: str, error_code: str, message: str):
        self._jobs_db.set_job_provisioning_error(job_id, error_code, message)

    def clear_job_provisioning_error(self, job_id: str):
        self._jobs_db.clear_job_provisioning_error(job_id)
