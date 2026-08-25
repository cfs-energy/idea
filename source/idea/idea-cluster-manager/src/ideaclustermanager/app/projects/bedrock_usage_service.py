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

__all__ = (
    'BedrockUsageService',
    'INVOCATION_QUERY',
    'CALLER_ARN_RE',
    'INSTANCE_ID_RE',
    'parse_caller_arn',
    'row_to_dict',
    'get_project_usage_by_model',
)

from ideadatamodel import (
    constants,
    exceptions,
    BedrockModelUsage,
    ListProjectsRequest,
    Project,
)
from ideasdk.context import SocaContext
from ideasdk.service import SocaService
from ideasdk.utils import Utils

from ideaclustermanager.app.projects.bedrock_invocation_logging import (
    BedrockInvocationLogging,
)
from ideaclustermanager.app.projects.db.bedrock_usage_dao import (
    BedrockInstanceOwnerDAO,
    UNATTRIBUTED_USER,
    build_day_key,
    build_project_key,
    build_user_key,
    build_day_job_key,
    build_job_key,
)

import arrow
import botocore.exceptions
import re
import threading
from typing import Dict, List, Optional, Tuple

# sts assumed-role arn. for instance profile credentials the role session name is
# the ec2 instance id, which is what ties an invocation to a session or job owner.
CALLER_ARN_RE = re.compile(
    r'^arn:[^:]*:sts::\d+:assumed-role/(?P<role>[^/]+)/(?P<session>.+)$'
)
INSTANCE_ID_RE = re.compile(r'^i-[0-9a-f]{8,}$')

# requestMetadata is caller supplied and deliberately not read: identity.arn is
# stamped by bedrock and is the only attribution input trusted here.
INVOCATION_QUERY = """fields identity.arn as caller_arn, modelId as model_id, input.inputTokenCount as in_tokens, output.outputTokenCount as out_tokens
| filter ispresent(caller_arn) and ispresent(model_id)
| stats sum(in_tokens) as input_tokens, sum(out_tokens) as output_tokens, count(*) as invocations by caller_arn, model_id, bin(1d) as usage_day
| sort invocations desc
| limit {limit}"""

QUERY_POLL_INTERVAL_SECONDS = 2
DESCRIBE_INSTANCES_CHUNK_SIZE = 200
INSTANCE_CACHE_MAX_ENTRIES = 20000
MAX_PROJECT_PAGES = 1000


def parse_caller_arn(caller_arn: str) -> Optional[Tuple[str, str]]:
    match = CALLER_ARN_RE.match(Utils.get_as_string(caller_arn, ''))
    if match is None:
        return None
    return match.group('role'), match.group('session')


def row_to_dict(row: List[Dict]) -> Dict[str, str]:
    return {
        Utils.get_value_as_string('field', entry, ''): Utils.get_value_as_string(
            'value', entry, ''
        )
        for entry in row
    }


def _new_counters() -> Dict[str, int]:
    return {'invocations': 0, 'input_tokens': 0, 'output_tokens': 0}


def _add_counters(target: Dict[str, int], source: Dict[str, int]):
    for key in ('invocations', 'input_tokens', 'output_tokens'):
        target[key] += Utils.get_as_int(source.get(key), 0)


def _row_counters(row: Dict) -> Dict[str, int]:
    return {
        key: Utils.get_value_as_int(key, row, 0)
        for key in ('invocations', 'input_tokens', 'output_tokens')
    }


def get_project_usage_by_model(
    usage_dao, project_id: str, period: str
) -> List[BedrockModelUsage]:
    """
    per model totals for one period, summed across users. the stored rollups are per
    user and per job, so the model dimension is aggregated on read from the day rows.
    takes the dao because the read path holds one but not the usage service itself.
    """
    by_model: Dict[str, Dict[str, int]] = {}
    for row in usage_dao.query_day_rows(project_id, f'{period}-01', f'{period}-31'):
        model_id = Utils.get_value_as_string('model_id', row)
        if Utils.is_empty(model_id):
            continue
        _add_counters(
            by_model.setdefault(model_id, _new_counters()), _row_counters(row)
        )

    entries = [
        BedrockModelUsage(
            model_id=model_id,
            invocations=counters['invocations'],
            input_tokens=counters['input_tokens'],
            output_tokens=counters['output_tokens'],
            total_tokens=counters['input_tokens'] + counters['output_tokens'],
        )
        for model_id, counters in by_model.items()
    ]
    entries.sort(key=lambda entry: entry.total_tokens, reverse=True)
    return entries


class BedrockUsageService(SocaService):
    """
    turns bedrock model invocation log records into per project and per user
    usage rows. the window is recomputed and overwritten on every run, so a
    repeated or retried run produces the same rows.
    """

    def __init__(self, context: SocaContext, projects_service):
        super().__init__(context)
        self.context = context
        self.logger = context.logger('bedrock-usage')

        self.projects_service = projects_service
        self.usage_dao = projects_service.bedrock_usage_dao
        self.instance_owner_dao = BedrockInstanceOwnerDAO(context)
        self.invocation_logging = BedrockInvocationLogging(context)

        self._exit = threading.Event()
        self._thread = threading.Thread(target=self._loop, name='bedrock-usage')
        self._instance_cache: Dict[str, Optional[str]] = {}

    # configuration

    def _config_key(self, suffix: str) -> str:
        return f'{self.context.module_id()}.bedrock.{suffix}'

    def is_enabled(self) -> bool:
        if not self.context.config().get_bool(self._config_key('enabled'), False):
            return False
        return self.context.config().get_bool(self._config_key('usage.enabled'), True)

    def get_interval_seconds(self) -> int:
        minutes = self.context.config().get_int(
            self._config_key('usage.interval_minutes'), 60
        )
        return max(5, minutes) * 60

    def get_lookback_days(self) -> int:
        return max(
            1, self.context.config().get_int(self._config_key('usage.lookback_days'), 2)
        )

    def get_max_query_results(self) -> int:
        return self.context.config().get_int(
            self._config_key('usage.max_query_results'), 10000
        )

    def get_query_timeout_seconds(self) -> int:
        return self.context.config().get_int(
            self._config_key('usage.query_timeout_seconds'), 300
        )

    def get_retention_days(self) -> int:
        return self.context.config().get_int(
            self._config_key('usage.retention_days'), 400
        )

    def get_row_ttl(self) -> int:
        return Utils.current_time() + (self.get_retention_days() * 86400)

    # lifecycle

    def start(self):
        if not self.is_enabled():
            self.logger.debug('bedrock usage tracking is disabled. skip.')
            return
        # the table grants arrive with a cluster-manager redeploy, which can trail
        # the setting. attribution needs this table, so tracking stays off until a
        # redeploy rather than stopping the module from starting.
        try:
            self.instance_owner_dao.initialize()
        except Exception as e:
            self.logger.warning(
                f'bedrock instance owner table is unavailable: {e}. usage tracking '
                f'is off until the cluster-manager module is redeployed.'
            )
            return
        self._thread.start()

    def stop(self):
        self._exit.set()
        if self._thread.is_alive():
            self._thread.join()

    def _loop(self):
        while not self._exit.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.logger.exception(f'bedrock usage aggregation failed: {e}')
            finally:
                self._exit.wait(self.get_interval_seconds())

    def run_once(self):
        lock_key = f'{self.context.module_id()}-bedrock-usage'
        try:
            self.context.distributed_lock().acquire(key=lock_key)
        except Exception as e:
            self.logger.info(f'bedrock usage aggregation is running elsewhere: {e}')
            return
        try:
            self.aggregate()
        finally:
            self.context.distributed_lock().release(key=lock_key)

    # aggregation

    def aggregate(self):
        self.invocation_logging.reconcile()

        log_group_name = self.invocation_logging.get_log_group_name()
        if Utils.is_empty(log_group_name):
            return

        projects = self.list_bedrock_projects()
        if len(projects) == 0:
            return

        role_index, profile_index = self.build_indexes(projects)
        days = self.build_window_days()
        start_time = arrow.get(days[0]).int_timestamp
        end_time = Utils.current_time()

        records = self.query_invocation_records(log_group_name, start_time, end_time)
        aggregates, job_aggregates, skipped = self.attribute(
            records, role_index, profile_index
        )
        if skipped > 0:
            self.logger.info(
                f'{skipped} invocation record groups were not attributed to an idea '
                f'project. account and region invocation logging captures every '
                f'bedrock caller, not only idea hosts.'
            )
        self.store(projects, aggregates, days, job_aggregates=job_aggregates)

    def list_bedrock_projects(self) -> List[Project]:
        projects = []
        cursor = None
        for _ in range(MAX_PROJECT_PAGES):
            result = self.projects_service.projects_dao.list_projects(
                ListProjectsRequest(cursor=cursor)
            )
            for project in Utils.get_as_list(result.listing, []):
                if project.bedrock is not None:
                    projects.append(project)
            cursor = result.paginator.cursor if result.paginator is not None else None
            if Utils.is_empty(cursor):
                break
        return projects

    @staticmethod
    def build_indexes(projects: List[Project]):
        role_index: Dict[str, Project] = {}
        profile_index: Dict[str, Tuple[Project, str]] = {}
        for project in projects:
            role_arn = project.bedrock.role_arn
            if Utils.is_not_empty(role_arn):
                role_index[role_arn.split('/')[-1].lower()] = project
            profile_arns = Utils.get_as_dict(project.bedrock.inference_profile_arns, {})
            for model_id, profile_arn in profile_arns.items():
                if Utils.is_empty(profile_arn):
                    continue
                profile_index[profile_arn.lower()] = (project, model_id)
                profile_index[profile_arn.split('/')[-1].lower()] = (project, model_id)
        return role_index, profile_index

    def build_window_days(self) -> List[str]:
        today = arrow.utcnow()
        lookback = self.get_lookback_days()
        return [
            today.shift(days=-offset).format('YYYY-MM-DD')
            for offset in range(lookback - 1, -1, -1)
        ]

    def query_invocation_records(
        self, log_group_name: str, start_time: int, end_time: int
    ) -> List[List[Dict]]:
        logs = self.context.aws().logs()
        query_string = INVOCATION_QUERY.format(limit=self.get_max_query_results())
        try:
            response = logs.start_query(
                logGroupName=log_group_name,
                startTime=start_time,
                endTime=end_time,
                queryString=query_string,
            )
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                self.logger.info(
                    f'model invocation log group {log_group_name} does not exist yet.'
                )
                return []
            raise e

        query_id = Utils.get_value_as_string('queryId', response)
        deadline = Utils.current_time() + self.get_query_timeout_seconds()
        while True:
            result = logs.get_query_results(queryId=query_id)
            status = Utils.get_value_as_string('status', result)
            if status == 'Complete':
                results = Utils.get_value_as_list('results', result, [])
                if len(results) >= self.get_max_query_results():
                    self.logger.warning(
                        f'model invocation log query returned the maximum of '
                        f'{self.get_max_query_results()} rows. usage for this window is '
                        f'incomplete. raise {self._config_key("usage.max_query_results")} '
                        f'or lower {self._config_key("usage.lookback_days")}.'
                    )
                return results
            if status in ('Failed', 'Cancelled', 'Timeout'):
                raise exceptions.general_exception(
                    f'model invocation log query {status}: {query_id}'
                )
            if Utils.current_time() > deadline or self._exit.is_set():
                self._stop_query(query_id)
                raise exceptions.general_exception(
                    f'model invocation log query did not complete within '
                    f'{self.get_query_timeout_seconds()}s: {query_id}'
                )
            self._exit.wait(QUERY_POLL_INTERVAL_SECONDS)

    def _stop_query(self, query_id: str):
        try:
            self.context.aws().logs().stop_query(queryId=query_id)
        except Exception as e:
            self.logger.debug(f'failed to stop query {query_id}: {e}')

    def attribute(
        self,
        records: List[List[Dict]],
        role_index: Dict[str, Project],
        profile_index: Dict[str, Tuple[Project, str]],
    ):
        parsed = []
        instance_ids = set()
        skipped = 0

        for record in records:
            row = row_to_dict(record)
            caller = parse_caller_arn(row.get('caller_arn'))
            if caller is None:
                skipped += 1
                continue
            role_name, session_name = caller

            raw_model_id = Utils.get_as_string(row.get('model_id'), '')
            project = role_index.get(role_name.lower())
            model_id = None
            profile_entry = profile_index.get(raw_model_id.lower())
            if profile_entry is not None:
                if project is None:
                    project = profile_entry[0]
                model_id = profile_entry[1]
            if project is None:
                skipped += 1
                continue
            if Utils.is_empty(model_id):
                model_id = raw_model_id

            usage_date = Utils.get_as_string(row.get('usage_day'), '')[:10]
            if len(usage_date) != 10:
                skipped += 1
                continue

            instance_id = None
            if INSTANCE_ID_RE.match(session_name) is not None:
                instance_id = session_name
                instance_ids.add(instance_id)

            parsed.append(
                (
                    project.project_id,
                    usage_date,
                    model_id,
                    instance_id,
                    {
                        'invocations': Utils.get_as_int(row.get('invocations'), 0),
                        'input_tokens': Utils.get_as_int(row.get('input_tokens'), 0),
                        'output_tokens': Utils.get_as_int(row.get('output_tokens'), 0),
                    },
                )
            )

        owners = self.resolve_instance_owners(sorted(instance_ids))

        aggregates: Dict[str, Dict[Tuple[str, str, str], Dict[str, int]]] = {}
        job_aggregates: Dict[str, Dict[Tuple[str, str, str], Dict[str, int]]] = {}
        for project_id, usage_date, model_id, instance_id, counters in parsed:
            attribution = owners.get(instance_id) or ('', '')
            username = attribution[0] or UNATTRIBUTED_USER
            project_rows = aggregates.setdefault(project_id, {})
            key = (usage_date, username, model_id)
            _add_counters(project_rows.setdefault(key, _new_counters()), counters)

            job_id = attribution[1]
            if Utils.is_not_empty(job_id):
                job_rows = job_aggregates.setdefault(project_id, {})
                job_key = (usage_date, job_id, model_id)
                _add_counters(job_rows.setdefault(job_key, _new_counters()), counters)

        return aggregates, job_aggregates, skipped

    def resolve_instance_owners(self, instance_ids: List[str]) -> Dict[str, str]:
        if len(instance_ids) == 0:
            return {}
        if len(self._instance_cache) > INSTANCE_CACHE_MAX_ENTRIES:
            self._instance_cache.clear()

        resolved: Dict[str, str] = {}
        pending = []
        for instance_id in instance_ids:
            if instance_id in self._instance_cache:
                username = self._instance_cache[instance_id]
                if username is not None:
                    resolved[instance_id] = username
            else:
                pending.append(instance_id)

        if len(pending) > 0:
            cached = self.instance_owner_dao.get_attribution(pending)
            for instance_id, username in cached.items():
                resolved[instance_id] = username
                self._instance_cache[instance_id] = username
            pending = [
                instance_id for instance_id in pending if instance_id not in cached
            ]

        if len(pending) > 0:
            ttl_seconds = self.get_retention_days() * 86400
            described = self.describe_instance_owners(pending)
            for instance_id in pending:
                attribution = described.get(instance_id)
                self._instance_cache[instance_id] = attribution
                if attribution is None:
                    continue
                resolved[instance_id] = attribution
                self.instance_owner_dao.put_owner(
                    instance_id, attribution[0], ttl_seconds, job_id=attribution[1]
                )

        return resolved

    def describe_instance_owners(self, instance_ids: List[str]) -> Dict[str, tuple]:
        """(username, job_id) per instance. A desktop has no job, so its job_id is empty."""
        ec2 = self.context.aws().ec2()
        owners: Dict[str, tuple] = {}
        for index in range(0, len(instance_ids), DESCRIBE_INSTANCES_CHUNK_SIZE):
            chunk = instance_ids[index : index + DESCRIBE_INSTANCES_CHUNK_SIZE]
            next_token = None
            while True:
                # filters, not InstanceIds: a terminated and expired instance id
                # must not fail the whole call.
                kwargs = {
                    'Filters': [{'Name': 'instance-id', 'Values': chunk}],
                }
                if Utils.is_not_empty(next_token):
                    kwargs['NextToken'] = next_token
                result = ec2.describe_instances(**kwargs)
                for reservation in Utils.get_value_as_list('Reservations', result, []):
                    for instance in Utils.get_value_as_list(
                        'Instances', reservation, []
                    ):
                        instance_id = Utils.get_value_as_string('InstanceId', instance)
                        tags = {
                            Utils.get_value_as_string(
                                'Key', tag
                            ): Utils.get_value_as_string('Value', tag)
                            for tag in Utils.get_value_as_list('Tags', instance, [])
                        }
                        username = Utils.get_as_string(
                            tags.get(constants.IDEA_TAG_JOB_OWNER), ''
                        )
                        job_id = Utils.get_as_string(
                            tags.get(constants.IDEA_TAG_JOB_ID), ''
                        )
                        if Utils.is_not_empty(username) or Utils.is_not_empty(job_id):
                            owners[instance_id] = (username, job_id)
                next_token = Utils.get_value_as_string('NextToken', result)
                if Utils.is_empty(next_token):
                    break
        return owners

    # persistence

    def store(
        self,
        projects: List[Project],
        aggregates: Dict[str, Dict[Tuple[str, str, str], Dict[str, int]]],
        days: List[str],
        job_aggregates: Dict[str, Dict[Tuple[str, str, str], Dict[str, int]]] = None,
    ):
        periods = sorted({day[:7] for day in days})
        ttl = self.get_row_ttl()
        updated_on = Utils.current_time_ms()

        for project in projects:
            project_id = project.project_id
            desired = aggregates.get(project_id, {})

            rows = []
            desired_keys = set()

            # the job dimension is its own set of rows, so nothing about the user rows changes
            job_desired = (job_aggregates or {}).get(project_id, {})
            for (usage_date, job_id, model_id), counters in job_desired.items():
                usage_id = build_day_job_key(usage_date, job_id, model_id)
                desired_keys.add(usage_id)
                rows.append(
                    {
                        'project_id': project_id,
                        'usage_id': usage_id,
                        'usage_date': usage_date,
                        'period': usage_date[:7],
                        'job_id': job_id,
                        'model_id': model_id,
                        'invocations': counters['invocations'],
                        'input_tokens': counters['input_tokens'],
                        'output_tokens': counters['output_tokens'],
                        'total_tokens': counters['input_tokens']
                        + counters['output_tokens'],
                        'updated_on': updated_on,
                        'ttl': ttl,
                    }
                )

            for (usage_date, username, model_id), counters in desired.items():
                usage_id = build_day_key(usage_date, username, model_id)
                desired_keys.add(usage_id)
                rows.append(
                    {
                        'project_id': project_id,
                        'usage_id': usage_id,
                        'usage_date': usage_date,
                        'period': usage_date[:7],
                        'username': username,
                        'model_id': model_id,
                        'invocations': counters['invocations'],
                        'input_tokens': counters['input_tokens'],
                        'output_tokens': counters['output_tokens'],
                        'total_tokens': counters['input_tokens']
                        + counters['output_tokens'],
                        'updated_on': updated_on,
                        'ttl': ttl,
                    }
                )

            existing = self.usage_dao.query_day_rows(
                project_id, days[0], days[-1]
            ) + self.usage_dao.query_day_job_rows(project_id, days[0], days[-1])
            existing_keys = {
                Utils.get_value_as_string('usage_id', row) for row in existing
            }
            self.usage_dao.put_rows(rows)
            self.usage_dao.delete_rows(project_id, existing_keys - desired_keys)

            for period in periods:
                self.rebuild_rollups(project_id, period, ttl, updated_on)

    def rebuild_rollups(self, project_id: str, period: str, ttl: int, updated_on: int):
        day_rows = self.usage_dao.query_day_rows(
            project_id, f'{period}-01', f'{period}-31'
        )

        by_user: Dict[str, Dict[str, int]] = {}
        project_counters = _new_counters()
        for row in day_rows:
            username = Utils.get_value_as_string('username', row, UNATTRIBUTED_USER)
            counters = _row_counters(row)
            _add_counters(by_user.setdefault(username, _new_counters()), counters)
            _add_counters(project_counters, counters)

        # the job rollup covers the whole period, not only the days in the query
        # window, so a job that ran across days keeps every day in its month.
        by_job: Dict[str, Dict[str, int]] = {}
        for row in self.usage_dao.query_day_job_rows(
            project_id, f'{period}-01', f'{period}-31'
        ):
            job_id = Utils.get_value_as_string('job_id', row)
            if Utils.is_empty(job_id):
                continue
            _add_counters(
                by_job.setdefault(job_id, _new_counters()), _row_counters(row)
            )

        rows = []
        desired_keys = set()
        for job_id, counters in by_job.items():
            usage_id = build_job_key(period, job_id)
            desired_keys.add(usage_id)
            rows.append(
                {
                    'project_id': project_id,
                    'usage_id': usage_id,
                    'period': period,
                    'job_id': job_id,
                    'invocations': counters['invocations'],
                    'input_tokens': counters['input_tokens'],
                    'output_tokens': counters['output_tokens'],
                    'total_tokens': counters['input_tokens']
                    + counters['output_tokens'],
                    'updated_on': updated_on,
                    'ttl': ttl,
                }
            )
        for username, counters in by_user.items():
            usage_id = build_user_key(period, username)
            desired_keys.add(usage_id)
            rows.append(
                {
                    'project_id': project_id,
                    'usage_id': usage_id,
                    'period': period,
                    'username': username,
                    'invocations': counters['invocations'],
                    'input_tokens': counters['input_tokens'],
                    'output_tokens': counters['output_tokens'],
                    'total_tokens': counters['input_tokens']
                    + counters['output_tokens'],
                    'updated_on': updated_on,
                    'ttl': ttl,
                }
            )

        project_key = build_project_key(period)
        if len(by_user) > 0:
            rows.append(
                {
                    'project_id': project_id,
                    'usage_id': project_key,
                    'period': period,
                    'invocations': project_counters['invocations'],
                    'input_tokens': project_counters['input_tokens'],
                    'output_tokens': project_counters['output_tokens'],
                    'total_tokens': project_counters['input_tokens']
                    + project_counters['output_tokens'],
                    'updated_on': updated_on,
                    'ttl': ttl,
                }
            )

        self.usage_dao.put_rows(rows)

        stale = {
            Utils.get_value_as_string('usage_id', row)
            for row in self.usage_dao.query_user_rollups(project_id, period)
            + self.usage_dao.query_job_rollups(project_id, period)
        } - desired_keys
        if len(by_user) == 0:
            stale.add(project_key)
        self.usage_dao.delete_rows(project_id, stale)
