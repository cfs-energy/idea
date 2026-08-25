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
    errorcodes,
    SocaJob,
    SocaJobExecutionHost,
    EC2Instance,
    SocaCapacityType,
)
from ideascheduler.app.app_protocols import JobCacheProtocol
from ideasdk.utils import Utils
from ideasdk.utils.error_redaction import AWS_IDENTIFIERS

from typing import List, Optional, Dict
import dataset
import os
import logging
import re
from threading import Event, RLock


JOBS_TABLE = 'jobs'
FINISHED_JOBS_TABLE = 'finished_jobs'
EXECUTION_HOSTS_TABLE = 'execution_hosts'
ACTIVE_JOB_LICENSES_TABLE = 'active_job_licenses'
JOB_PROVISIONING_ERRORS = 'job_provisioning_errors'
JOB_PROVISIONING_RETRIES = 'job_provisioning_retries'

ERROR_MESSAGE_MAX_LENGTH = 256
ERROR_MESSAGE_REDACTED = 'redacted'
# job owners read error_message using qstat -f. account and resource identifiers are redacted and
# remain available to administrators via the scheduler logs and the cluster manager web interface.
ERROR_MESSAGE_IDENTIFIERS = AWS_IDENTIFIERS
# qalter parses a resource value up to the next comma. limit the value to characters that cannot
# be mistaken for additional resources.
ERROR_MESSAGE_UNSUPPORTED_CHARS = re.compile(r'[^A-Za-z0-9._:/-]+')


def build_job_error_message(error_code: str, message: str) -> str:
    """
    build the short, user facing provisioning error, written to the job's error_message resource.
    """
    error_message = ERROR_MESSAGE_UNSUPPORTED_CHARS.sub(
        '_', Utils.get_as_string(error_code, errorcodes.GENERAL_ERROR)
    ).strip('_')

    reason = Utils.get_as_string(message, '')
    if reason.startswith(f'[{error_code}]'):
        # SocaException messages are already prefixed with the error code
        reason = reason[len(error_code) + 2 :]
    reason = ERROR_MESSAGE_UNSUPPORTED_CHARS.sub(
        '_', ERROR_MESSAGE_IDENTIFIERS.sub(ERROR_MESSAGE_REDACTED, reason)
    ).strip('_')

    if Utils.is_not_empty(reason) and reason != error_message:
        error_message = f'{error_message}: {reason}'

    return error_message[:ERROR_MESSAGE_MAX_LENGTH].strip(' _')


class JobsDB:
    def __init__(self, context: ideascheduler.AppContext):
        self._context = context
        self._logger = context.logger()

        db_dir = f'{self._context.get_scheduler_app_deploy_dir()}/db'
        db_file = f'{db_dir}/job-cache-v3.db'

        # Make sure directory exists with proper permissions
        try:
            os.makedirs(db_dir, exist_ok=True)
            # Check if we can write to this directory
            if not os.access(db_dir, os.W_OK):
                self._logger.error(
                    f'No write permission for database directory: {db_dir}'
                )
                raise Exception(f'No write permission for database directory: {db_dir}')
        except Exception as e:
            self._logger.error(f'Failed to create database directory: {str(e)}')
            raise

        self.connection_string = f'sqlite:///{db_file}'
        self._is_ready = Event()
        self.db = None
        self._db_lock = RLock()  # Keep the RLock for database operations
        self.init_db()

    def init_db(self):
        self._logger.info(f'initializing job cache db file: {self.connection_string}')
        try:
            # Create the database connection
            self.db = dataset.connect(self.connection_string)

            # First check if tables already exist
            existing_tables = self.db.tables
            self._logger.info(f'Found existing tables: {existing_tables}')

            # Create all tables in a single transaction if needed
            self.create_all_tables()

            # a database written by an earlier release made job_id unique on the
            # finished jobs table, which cannot hold two jobs given the same id.
            self.drop_unique_index(FINISHED_JOBS_TABLE, 'ix_finished_jobs_job_id')

            # Create indices after tables are confirmed to exist
            self.init_indices()

        except Exception as e:
            self._logger.error(f'Failed to initialize database: {str(e)}')
            raise

    def create_all_tables(self):
        """Create all required tables in a single transaction"""
        required_tables = [
            JOBS_TABLE,
            FINISHED_JOBS_TABLE,
            EXECUTION_HOSTS_TABLE,
            ACTIVE_JOB_LICENSES_TABLE,
            JOB_PROVISIONING_ERRORS,
            JOB_PROVISIONING_RETRIES,
        ]

        with self._db_lock:
            # Use a more explicit transaction approach
            conn = self.db.engine.raw_connection()
            try:
                cursor = conn.cursor()

                for table in required_tables:
                    if table not in self.db:
                        self._logger.info(f'Creating table: {table}')
                        # table names are module-level constants; identifiers cannot be bound as parameters
                        cursor.execute(
                            f'CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY AUTOINCREMENT)'
                        )

                conn.commit()
                self._logger.info('All tables created successfully')

                # Verify tables were created
                for table in required_tables:
                    cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    )
                    if not cursor.fetchone():
                        raise Exception(
                            f'Failed to create table {table} despite successful execution'
                        )

            except Exception as e:
                conn.rollback()
                self._logger.error(f'Error creating tables: {str(e)}')
                raise
            finally:
                conn.close()

        # Refresh database object after raw connection usage
        self.db = dataset.connect(self.connection_string)

    def init_tables(self):
        # This method is kept for backward compatibility
        # But actual table creation is now handled by create_all_tables()
        pass

    def drop_unique_index(self, table: str, index_name: str):
        """
        drop an index carried over from an earlier schema, if it is still unique.

        best effort: if the drop fails the old index stays and a job given a reused id
        is not recorded locally, which is preferable to refusing to start.
        """
        dropped = False
        with self._db_lock:
            conn = self.db.engine.raw_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (index_name,),
                )
                entry = cursor.fetchone()
                definition = entry[0] if entry is not None else None
                if definition is not None and 'UNIQUE' in definition.upper():
                    self._logger.info(f'dropping unique index {index_name} on {table}')
                    # index names are module-level constants; identifiers cannot be bound
                    cursor.execute(f'DROP INDEX IF EXISTS {index_name}')
                    conn.commit()
                    dropped = True
            except Exception as e:
                conn.rollback()
                self._logger.error(f'failed to drop index {index_name}: {e}')
            finally:
                conn.close()

        if dropped:
            # Refresh database object after raw connection usage
            self.db = dataset.connect(self.connection_string)

    def init_indices(self):
        """Initialize all indices outside of any transaction"""
        try:
            with self._db_lock:
                # First verify all tables exist
                if not all(
                    table in self.db.tables
                    for table in [
                        JOBS_TABLE,
                        FINISHED_JOBS_TABLE,
                        EXECUTION_HOSTS_TABLE,
                        ACTIVE_JOB_LICENSES_TABLE,
                        JOB_PROVISIONING_ERRORS,
                        JOB_PROVISIONING_RETRIES,
                    ]
                ):
                    raise Exception(
                        'Cannot create indices - one or more required tables are missing'
                    )

                # jobs indices
                if not self.db[JOBS_TABLE].has_index('ix_jobs_job_id'):
                    self.db[JOBS_TABLE].create_index(
                        ['job_id'], unique=True, name='ix_jobs_job_id'
                    )
                if not self.db[JOBS_TABLE].has_index('ix_jobs_job_group'):
                    self.db[JOBS_TABLE].create_index(
                        ['job_group'], name='ix_jobs_job_group'
                    )
                if not self.db[JOBS_TABLE].has_index('ix_jobs_queue_profile'):
                    self.db[JOBS_TABLE].create_index(
                        ['queue_profile'], name='ix_jobs_queue_profile'
                    )
                if not self.db[JOBS_TABLE].has_index('ix_jobs_owner'):
                    self.db[JOBS_TABLE].create_index(['owner'], name='ix_jobs_owner')
                if not self.db[JOBS_TABLE].has_index('ix_jobs_state'):
                    self.db[JOBS_TABLE].create_index(['state'], name='ix_jobs_state')
                if not self.db[JOBS_TABLE].has_index('ix_jobs_queue'):
                    self.db[JOBS_TABLE].create_index(['queue'], name='ix_jobs_queue')

                # finished jobs indices
                # job_id is not unique here: it's reused once the scheduler host is replaced,
                # so several finished jobs can share one. job_uid is the identity stored under.
                if not self.db[FINISHED_JOBS_TABLE].has_index(
                    'ix_finished_jobs_job_uid'
                ):
                    self.db[FINISHED_JOBS_TABLE].create_index(
                        ['job_uid'], name='ix_finished_jobs_job_uid'
                    )
                if not self.db[FINISHED_JOBS_TABLE].has_index(
                    'ix_finished_jobs_job_id'
                ):
                    self.db[FINISHED_JOBS_TABLE].create_index(
                        ['job_id'], name='ix_finished_jobs_job_id'
                    )
                if not self.db[FINISHED_JOBS_TABLE].has_index(
                    'ix_finished_jobs_job_group'
                ):
                    self.db[FINISHED_JOBS_TABLE].create_index(
                        ['job_group'], name='ix_finished_jobs_job_group'
                    )
                if not self.db[FINISHED_JOBS_TABLE].has_index(
                    'ix_finished_jobs_queue_profile'
                ):
                    self.db[FINISHED_JOBS_TABLE].create_index(
                        ['queue_profile'], name='ix_finished_jobs_queue_profile'
                    )
                if not self.db[FINISHED_JOBS_TABLE].has_index('ix_finished_jobs_owner'):
                    self.db[FINISHED_JOBS_TABLE].create_index(
                        ['owner'], name='ix_finished_jobs_owner'
                    )
                if not self.db[FINISHED_JOBS_TABLE].has_index('ix_finished_jobs_state'):
                    self.db[FINISHED_JOBS_TABLE].create_index(
                        ['state'], name='ix_finished_jobs_state'
                    )
                if not self.db[FINISHED_JOBS_TABLE].has_index('ix_finished_jobs_queue'):
                    self.db[FINISHED_JOBS_TABLE].create_index(
                        ['queue'], name='ix_finished_jobs_queue'
                    )

                # execution hosts indices
                if not self.db[EXECUTION_HOSTS_TABLE].has_index(
                    'ix_execution_hosts_job_id'
                ):
                    self.db[EXECUTION_HOSTS_TABLE].create_index(
                        ['job_id'], name='ix_execution_hosts_job_id'
                    )
                if not self.db[EXECUTION_HOSTS_TABLE].has_index(
                    'ix_execution_hosts_job_id_host'
                ):
                    self.db[EXECUTION_HOSTS_TABLE].create_index(
                        ['job_id', 'host'],
                        unique=True,
                        name='ix_execution_hosts_job_id_host',
                    )

                # active job licenses indices
                if not self.db[ACTIVE_JOB_LICENSES_TABLE].has_index(
                    'ix_active_job_licenses_job_id'
                ):
                    self.db[ACTIVE_JOB_LICENSES_TABLE].create_index(
                        ['job_id'], name='ix_active_job_licenses_job_id'
                    )
                if not self.db[ACTIVE_JOB_LICENSES_TABLE].has_index(
                    'ix_active_job_licenses_license_name'
                ):
                    self.db[ACTIVE_JOB_LICENSES_TABLE].create_index(
                        ['license_name'], name='ix_active_job_licenses_license_name'
                    )
                if not self.db[ACTIVE_JOB_LICENSES_TABLE].has_index(
                    'ix_active_job_licenses_job_id_license_name'
                ):
                    self.db[ACTIVE_JOB_LICENSES_TABLE].create_index(
                        ['job_id', 'license_name'],
                        unique=True,
                        name='ix_active_job_licenses_job_id_license_name',
                    )

                # job provisioning errors indices
                if not self.db[JOB_PROVISIONING_ERRORS].has_index(
                    'ix_job_provisioning_errors_job_id'
                ):
                    self.db[JOB_PROVISIONING_ERRORS].create_index(
                        ['job_id'],
                        unique=True,
                        name='ix_job_provisioning_errors_job_id',
                    )

                # job provisioning retries indices
                if not self.db[JOB_PROVISIONING_RETRIES].has_index(
                    'ix_job_provisioning_retries_job_id'
                ):
                    self.db[JOB_PROVISIONING_RETRIES].create_index(
                        ['job_id'],
                        unique=True,
                        name='ix_job_provisioning_retries_job_id',
                    )
        except Exception as e:
            self._logger.error(f'Error creating indices: {str(e)}')
            raise

    def get(self, job_id: str) -> Optional[SocaJob]:
        with self._db_lock:
            entry = self.db[JOBS_TABLE].find_one(job_id=job_id)
            return self.convert_db_entry_to_job(entry, fetch_errors=True)

    def add(self, job: SocaJob):
        with self._db_lock:
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
                        'job_data': Utils.to_json(job),
                    },
                    keys=['job_id'],
                )

    def add_finished_job(self, job: SocaJob):
        # keyed on job_uid, not job_id: ids are reused once the scheduler host is replaced,
        # which would let a later job overwrite an earlier one's record; jobs with no uid fall back to job_id.
        keys = ['job_uid'] if Utils.is_not_empty(job.job_uid) else ['job_id']
        with self._db_lock:
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
                        'job_data': Utils.to_json(job),
                    },
                    keys=keys,
                )

    def get_finished_job(self, job_id: str) -> Optional[SocaJob]:
        with self._db_lock:
            # a job id can name more than one finished job. the caller asked for an id
            # and nothing else, so the most recently recorded one is the answer.
            entries = list(
                self.db[FINISHED_JOBS_TABLE].find(
                    job_id=job_id, order_by='-id', _limit=1
                )
            )
            if len(entries) == 0:
                return None
            return self.convert_db_entry_to_job(entries[0])

    def get_finished_job_by_uid(self, job_uid: str) -> Optional[SocaJob]:
        if Utils.is_empty(job_uid):
            return None
        with self._db_lock:
            entry = self.db[FINISHED_JOBS_TABLE].find_one(job_uid=job_uid)
            return self.convert_db_entry_to_job(entry)

    def add_license_ask(self, jobs: List[SocaJob]):
        if Utils.is_empty(jobs):
            return
        with self._db_lock:
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
                                'count': license_ask.count,
                            },
                            keys=['job_id', 'license_name'],
                        )

    def add_many(self, jobs: List[SocaJob]):
        with self._db_lock:
            with self.db:
                for job in jobs:
                    self.add(job)

    def delete(self, job_id: str):
        with self._db_lock:
            with self.db as tx:
                tx[JOBS_TABLE].delete(job_id=job_id)
                tx[ACTIVE_JOB_LICENSES_TABLE].delete(job_id=job_id)
                tx[JOB_PROVISIONING_ERRORS].delete(job_id=job_id)
                tx[JOB_PROVISIONING_RETRIES].delete(job_id=job_id)

    def delete_many(self, job_ids: List[str]):
        with self._db_lock:
            with self.db:
                for job_id in job_ids:
                    self.delete(job_id)

    def query(self, **kwargs) -> List[SocaJob]:
        with self._db_lock:
            jobs = []
            result = self.db[JOBS_TABLE].find(**kwargs)
            for entry in result:
                job = self.convert_db_entry_to_job(entry, fetch_errors=True)
                if job is None:
                    continue
                jobs.append(job)
            return jobs

    def query_finished_jobs(self, **kwargs) -> List[SocaJob]:
        with self._db_lock:
            jobs = []
            result = self.db[FINISHED_JOBS_TABLE].find(**kwargs)
            for entry in result:
                job = self.convert_db_entry_to_job(entry)
                if job is None:
                    continue
                jobs.append(job)
            return jobs

    def exists(self, job_id: str) -> bool:
        with self._db_lock:
            entry = self.db[JOBS_TABLE].find_one(job_id=job_id)
            return entry is not None

    def add_execution_host(self, job_id: str, execution_host: SocaJobExecutionHost):
        with self._db_lock:
            with self.db as tx:
                tx[EXECUTION_HOSTS_TABLE].upsert(
                    row={
                        'job_id': job_id,
                        'host': execution_host.host,
                        'execution_host_data': Utils.to_json(execution_host),
                    },
                    keys=['job_id', 'host'],
                )

    def get_execution_hosts(self, job_id: str) -> Optional[List[SocaJobExecutionHost]]:
        with self._db_lock:
            result = self.db[EXECUTION_HOSTS_TABLE].find(job_id=job_id)
            hosts = []
            for entry in result:
                execution_host_data = Utils.get_value_as_string(
                    'execution_host_data', entry
                )
                hosts.append(
                    SocaJobExecutionHost(**Utils.from_json(execution_host_data))
                )
            return hosts

    def delete_execution(self, job_id: str):
        with self._db_lock:
            with self.db as tx:
                tx[EXECUTION_HOSTS_TABLE].delete(job_id=job_id)

    def convert_db_entry_to_job(
        self, entry: Dict, fetch_errors: bool = False
    ) -> Optional[SocaJob]:
        if entry is None:
            return None
        job_data = Utils.get_value_as_string('job_data', entry)
        if job_data is None:
            return None
        if job_data == 'NULL':
            return None
        job = SocaJob(**Utils.from_json(job_data))
        if fetch_errors:
            with self._db_lock:
                error = self.db[JOB_PROVISIONING_ERRORS].find_one(job_id=job.job_id)
                error_message = Utils.get_value_as_string('message', error)
                job.error_message = error_message
        return job

    def set_job_provisioning_error(
        self, job_id: str, error_code: str, message: str
    ) -> bool:
        """
        :return: True if the error was not already recorded for the job
        """
        with self._db_lock:
            with self.db as tx:
                table = tx[JOB_PROVISIONING_ERRORS]
                current = table.find_one(job_id=job_id)
                changed = (
                    Utils.get_value_as_string('error_code', current) != error_code
                    or Utils.get_value_as_string('message', current) != message
                )
                table.upsert(
                    row={
                        'job_id': job_id,
                        'error_code': error_code,
                        'message': message,
                    },
                    keys=['job_id'],
                )
                return changed

    def clear_job_provisioning_error(self, job_id: str) -> bool:
        """
        :return: True if an error was recorded for the job
        """
        if Utils.is_empty(job_id):
            return False
        with self._db_lock:
            with self.db as tx:
                return tx[JOB_PROVISIONING_ERRORS].delete(job_id=job_id)

    def increment_job_provisioning_retry(self, job_id: str) -> int:
        with self._db_lock:
            with self.db as tx:
                entry = tx[JOB_PROVISIONING_RETRIES].find_one(job_id=job_id)
                retry_count = Utils.get_value_as_int('retry_count', entry, 0) + 1
                tx[JOB_PROVISIONING_RETRIES].upsert(
                    row={
                        'job_id': job_id,
                        'retry_count': retry_count,
                    },
                    keys=['job_id'],
                )
                return retry_count

    def get_job_provisioning_retry_count(self, job_id: str) -> int:
        with self._db_lock:
            entry = self.db[JOB_PROVISIONING_RETRIES].find_one(job_id=job_id)
            return Utils.get_value_as_int('retry_count', entry, 0)

    def clear_job_provisioning_retries(self, job_id: str):
        if Utils.is_empty(job_id):
            return
        with self._db_lock:
            with self.db as tx:
                tx[JOB_PROVISIONING_RETRIES].delete(job_id=job_id)


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
            return self._jobs_db.query(
                **kwargs, _limit=_limit, _offset=_offset, order_by='-id'
            )
        else:
            return self._jobs_db.query(**kwargs)

    def list_completed_jobs(
        self, _limit: int = -1, _offset: int = 0, **kwargs
    ) -> List[SocaJob]:
        if _limit > 0:
            return self._jobs_db.query_finished_jobs(
                **kwargs, _limit=_limit, _offset=_offset, order_by='-id'
            )
        else:
            return self._jobs_db.query_finished_jobs(**kwargs)

    def get_job(self, job_id: str) -> Optional[SocaJob]:
        return self._jobs_db.get(job_id=job_id)

    def get_completed_job(self, job_id: str) -> Optional[SocaJob]:
        return self._jobs_db.get_finished_job(job_id)

    def get_completed_job_by_uid(self, job_uid: str) -> Optional[SocaJob]:
        return self._jobs_db.get_finished_job_by_uid(job_uid)

    def delete_jobs(self, job_ids: List[str]):
        self._jobs_db.delete_many(job_ids=job_ids)

    def log_job_execution(
        self,
        job_id: str,
        execution_host: SocaJobExecutionHost,
        instance: Optional[EC2Instance] = None,
    ):
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(f'Updating Execution Host: {execution_host}')

        if instance is None and execution_host.instance_id is not None:
            instance = self._context.instance_cache.get_instance(
                instance_id=execution_host.instance_id
            )

        if instance is not None:
            if execution_host.capacity_type is None:
                execution_host.capacity_type = SocaCapacityType.resolve(
                    instance.soca_capacity_type
                )
            if execution_host.instance_id is None:
                execution_host.instance_id = instance.instance_id
            if execution_host.instance_type is None:
                execution_host.instance_type = instance.instance_type
            if execution_host.tenancy is None:
                execution_host.tenancy = instance.placement_tenancy
            if execution_host.reservation is None:
                execution_host.reservation = instance.capacity_reservation_id

        self._jobs_db.add_execution_host(job_id=job_id, execution_host=execution_host)

    def get_job_execution_hosts(
        self, job_id: str
    ) -> Optional[List[SocaJobExecutionHost]]:
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
        result = self._jobs_db.db.query(
            'select sum(desired_capacity) as desired_capacity from jobs '
            'where job_group = :job_group',
            job_group=job_group,
        )
        for entry in result:
            return Utils.get_value_as_int('desired_capacity', entry, 0)
        return 0

    def get_active_jobs(self, queue_profile: str) -> int:
        result = self._jobs_db.db.query(
            'select count(*) as active_jobs from jobs where '
            'queue_profile = :queue_profile '
            "and state in ('queued', 'running') "
            'and provisioned = 1',
            queue_profile=queue_profile,
        )
        for entry in result:
            return Utils.get_value_as_int('active_jobs', entry)
        return 0

    def get_count(self, **kwargs) -> int:
        return self._jobs_db.db[JOBS_TABLE].count(**kwargs)

    def get_completed_jobs_count(self, **kwargs) -> int:
        return self._jobs_db.db[FINISHED_JOBS_TABLE].count(**kwargs)

    def get_active_license_count(self, license_name: str) -> int:
        result = self._jobs_db.db.query(
            'select sum(count) as active_count from active_job_licenses '
            'where license_name = :license_name',
            license_name=license_name,
        )
        for entry in result:
            return Utils.get_value_as_int('active_count', entry, 0)
        return 0

    def set_ready(self):
        self._is_ready.set()

    def add_active_licenses(self, jobs: List[SocaJob]):
        self._jobs_db.add_license_ask(jobs)

    def convert_db_entry_to_job(
        self, entry: Dict, fetch_errors: bool = False
    ) -> Optional[SocaJob]:
        return self._jobs_db.convert_db_entry_to_job(entry, fetch_errors=fetch_errors)

    def _publish_error_message(self, job_id: str, error_message: Optional[str]):
        """
        publish the provisioning error on the job, so that job owners can find out why their job is
        not starting, using qstat -f. best effort: the job may no longer exist in the scheduler.
        """
        try:
            self._context.scheduler.set_job_attributes(
                job_id=job_id, attributes={'error_message': error_message}
            )
        except Exception as e:
            self._logger.warning(
                f'(JobId: {job_id}) failed to update job error_message: {e}'
            )

    def set_job_provisioning_error(self, job_id: str, error_code: str, message: str):
        # the DB write and the qalter publish aren't atomic: interleaved writers can leave
        # qstat stale until the next change. accepted - serializing would hold the db lock across a fork.
        if self._jobs_db.set_job_provisioning_error(job_id, error_code, message):
            self._publish_error_message(
                job_id=job_id,
                error_message=build_job_error_message(
                    error_code=error_code, message=message
                ),
            )

    def clear_job_provisioning_error(self, job_id: str):
        if self._jobs_db.clear_job_provisioning_error(job_id):
            self._publish_error_message(job_id=job_id, error_message=None)

    def increment_job_provisioning_retry(self, job_id: str) -> int:
        return self._jobs_db.increment_job_provisioning_retry(job_id)

    def get_job_provisioning_retry_count(self, job_id: str) -> int:
        return self._jobs_db.get_job_provisioning_retry_count(job_id)

    def clear_job_provisioning_retries(self, job_id: str):
        self._jobs_db.clear_job_provisioning_retries(job_id)
