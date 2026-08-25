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

"""
Test Cases for bedrock usage tracking

The invocation logging manager and the usage aggregator run against recording
bedrock, logs, ec2 and dynamodb fakes. The real DAOs are used so key encoding and
range queries are exercised; nothing touches DynamoDB or AWS.
"""

from ideaclustermanager.app.projects.bedrock_invocation_logging import (
    BedrockInvocationLogging,
    STATE_ADOPTED,
    STATE_ADOPTED_ALREADY,
    STATE_DISABLED,
    STATE_NOT_CONFIGURED,
    STATE_STOOD_DOWN,
    STATE_UNAVAILABLE,
)
from ideaclustermanager.app.projects.bedrock_usage_service import (
    BedrockUsageService,
    INVOCATION_QUERY,
    get_project_usage_by_model,
    parse_caller_arn,
)
from ideaclustermanager.app.projects.db.bedrock_usage_dao import (
    BedrockInstanceOwnerDAO,
    BedrockUsageDAO,
    UNATTRIBUTED_USER,
)
from ideaclustermanager.app.api.projects_api import ProjectsAPI
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO
from ideaclustermanager.app.projects.projects_service import ProjectsService

from ideadatamodel import (
    constants,
    exceptions,
    locale,
    ListProjectsResult,
    Project,
    ProjectBedrockConfig,
    ProjectBedrockUsage,
    SocaPaginator,
)

import arrow
import pytest


@pytest.fixture(autouse=True)
def initialized_locale():
    # SocaAmount reads the currency code the app context normally initializes.
    try:
        locale.get_currency_code()
    except Exception:
        locale.init('C')


CLUSTER_NAME = 'idea-test'
MODULE_ID = 'cluster-manager'
REGION = 'us-east-2'
ACCOUNT_ID = '123456789012'

LOG_GROUP = f'/{CLUSTER_NAME}/{MODULE_ID}/bedrock-invocations'
LOG_ROLE_ARN = (
    f'arn:aws:iam::{ACCOUNT_ID}:role/{CLUSTER_NAME}-bedrock-invocation-logging-{REGION}'
)

PROJECT_ID = 'a1b2c3d4-0000-4000-8000-000000000001'
PROJECT_ID_2 = 'a1b2c3d4-0000-4000-8000-000000000002'
ROLE_NAME = f'{CLUSTER_NAME}-{PROJECT_ID}-project'
ROLE_ARN = f'arn:aws:iam::{ACCOUNT_ID}:role/idea/{CLUSTER_NAME}/projects/{ROLE_NAME}'

MODEL_A = 'us.vendor-a.model-1'
MODEL_B = 'vendor-b.model-9'
PROFILE_A = (
    f'arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:application-inference-profile/abc123'
)

INSTANCE_ONE = 'i-0aaaaaaaaaaaaaaa1'
INSTANCE_TWO = 'i-0aaaaaaaaaaaaaaa2'

TODAY = arrow.utcnow().format('YYYY-MM-DD')
PERIOD = TODAY[:7]

CONFIG_VALUES = {
    'cluster.cluster_name': CLUSTER_NAME,
    'cluster.aws.region': REGION,
    'cluster.aws.account_id': ACCOUNT_ID,
    'cluster.aws.partition': 'aws',
    f'{MODULE_ID}.bedrock.enabled': True,
    f'{MODULE_ID}.bedrock.usage.enabled': True,
    f'{MODULE_ID}.bedrock.usage.lookback_days': 2,
    f'{MODULE_ID}.bedrock.invocation_log_group_name': LOG_GROUP,
    f'{MODULE_ID}.bedrock.invocation_log_role_arn': LOG_ROLE_ARN,
}


class FakeConfig:
    def __init__(self, values):
        self.values = dict(values)

    def _get(self, key, default, required):
        value = self.values.get(key, default)
        if required and value is None:
            raise KeyError(key)
        return value

    def get_string(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_bool(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_int(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_list(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_module_id(self, module_name):
        return module_name


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(('info', str(message)))

    def debug(self, message, *args, **kwargs):
        self.messages.append(('debug', str(message)))

    def warning(self, message, *args, **kwargs):
        self.messages.append(('warning', str(message)))

    def error(self, message, *args, **kwargs):
        self.messages.append(('error', str(message)))

    def exception(self, message, *args, **kwargs):
        self.messages.append(('exception', str(message)))


class FakeBedrock:
    def __init__(self, logging_config=None):
        self.calls = []
        self.logging_config = logging_config

    def get_model_invocation_logging_configuration(self):
        self.calls.append(('get', {}))
        if self.logging_config is None:
            return {}
        return {'loggingConfig': self.logging_config}

    def put_model_invocation_logging_configuration(self, loggingConfig):
        self.calls.append(('put', loggingConfig))
        self.logging_config = loggingConfig
        return {}


class FakeLogs:
    def __init__(self, results=None, status='Complete'):
        self.calls = []
        self.results = results if results is not None else []
        self.status = status

    def start_query(self, logGroupName, startTime, endTime, queryString):
        self.calls.append(
            (
                'start_query',
                {
                    'logGroupName': logGroupName,
                    'startTime': startTime,
                    'endTime': endTime,
                    'queryString': queryString,
                },
            )
        )
        return {'queryId': 'q-1'}

    def get_query_results(self, queryId):
        self.calls.append(('get_query_results', {'queryId': queryId}))
        return {'status': self.status, 'results': self.results}

    def stop_query(self, queryId):
        self.calls.append(('stop_query', {'queryId': queryId}))
        return {}


class FakeEc2:
    def __init__(self, owners=None, jobs=None):
        self.jobs = jobs if jobs is not None else {}
        self.calls = []
        self.owners = owners if owners is not None else {}

    def describe_instances(self, **kwargs):
        self.calls.append(('describe_instances', kwargs))
        requested = kwargs['Filters'][0]['Values']
        instances = []
        for instance_id in requested:
            username = self.owners.get(instance_id)
            if username is None:
                continue
            instances.append(
                {
                    'InstanceId': instance_id,
                    'Tags': [
                        {'Key': 'idea:ClusterName', 'Value': CLUSTER_NAME},
                        {'Key': 'idea:JobOwner', 'Value': username},
                    ]
                    + (
                        [{'Key': 'idea:JobId', 'Value': self.jobs[instance_id]}]
                        if instance_id in self.jobs
                        else []
                    ),
                }
            )
        return {'Reservations': [{'Instances': instances}]}


def _evaluate(condition, item):
    expression = condition.get_expression()
    operator = expression['operator']
    values = expression['values']
    if operator == 'AND':
        return _evaluate(values[0], item) and _evaluate(values[1], item)
    actual = item.get(values[0].name)
    if operator == '=':
        return actual == values[1]
    if operator == 'BETWEEN':
        return values[1] <= actual <= values[2]
    if operator == 'begins_with':
        return isinstance(actual, str) and actual.startswith(values[1])
    raise AssertionError(f'unsupported operator in test: {operator}')


class _BatchWriter:
    def __init__(self, table):
        self.table = table

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def put_item(self, Item):
        self.table.put_item(Item=Item)

    def delete_item(self, Key):
        self.table.delete_item(Key=Key)


class FakeTable:
    def __init__(self, key_attributes):
        self.key_attributes = key_attributes
        self.items = {}
        self.calls = []

    def _key(self, item):
        return tuple(item[attribute] for attribute in self.key_attributes)

    def put_item(self, Item):
        self.calls.append(('put_item', Item))
        self.items[self._key(Item)] = dict(Item)

    def delete_item(self, Key):
        self.calls.append(('delete_item', Key))
        self.items.pop(self._key(Key), None)

    def get_item(self, Key):
        self.calls.append(('get_item', Key))
        item = self.items.get(self._key(Key))
        if item is None:
            return {}
        return {'Item': dict(item)}

    def query(self, KeyConditionExpression, **kwargs):
        self.calls.append(('query', KeyConditionExpression))
        return {
            'Items': [
                dict(item)
                for item in self.items.values()
                if _evaluate(KeyConditionExpression, item)
            ]
        }

    def batch_writer(self):
        return _BatchWriter(self)


class FakeAws:
    def __init__(self, bedrock=None, logs=None, ec2=None):
        self._bedrock = bedrock
        self._logs = logs
        self._ec2 = ec2

    def bedrock(self):
        return self._bedrock

    def logs(self):
        return self._logs

    def ec2(self):
        return self._ec2


class FakeServiceRegistry:
    def __init__(self):
        self.services = []

    def register(self, service):
        self.services.append(service)


class FakeDistributedLock:
    def __init__(self):
        self.calls = []

    def acquire(self, key):
        self.calls.append(('acquire', key))

    def release(self, key):
        self.calls.append(('release', key))


class FakeAwsUtil:
    """stands in for the Cost Explorer read. spend is set per test."""

    def __init__(self):
        self.spend = None
        self.raises = False
        self.calls = []

    def cost_explorer_get_tagged_service_spend(self, tag_key, tag_value):
        self.calls.append((tag_key, tag_value))
        if self.raises:
            raise RuntimeError('cost explorer is not reachable')
        return self.spend


class FakeContext:
    def __init__(self, config, aws):
        self._config = config
        self._aws = aws
        self._aws_util = FakeAwsUtil()
        self._logger = FakeLogger()
        self._registry = FakeServiceRegistry()
        self._lock = FakeDistributedLock()

    def config(self):
        return self._config

    def aws(self):
        return self._aws

    def aws_util(self):
        return self._aws_util

    def logger(self, name=None):
        return self._logger

    def cluster_name(self):
        return CLUSTER_NAME

    def module_id(self):
        return MODULE_ID

    def service_registry(self):
        return self._registry

    def distributed_lock(self):
        return self._lock


class FakeProjectsDAO:
    def __init__(self, projects):
        self.projects = projects
        self.calls = []

    def list_projects(self, request):
        self.calls.append(('list_projects', request.cursor))
        return ListProjectsResult(
            listing=list(self.projects), paginator=SocaPaginator(cursor=None)
        )


class FakeProjectsService:
    def __init__(self, context, projects, usage_dao):
        self.context = context
        self.projects_dao = FakeProjectsDAO(projects)
        self.bedrock_usage_dao = usage_dao


def build_project(
    project_id=PROJECT_ID, role_arn=ROLE_ARN, profiles=None, name='research'
):
    if profiles is None:
        profiles = {MODEL_A: PROFILE_A}
    return Project(
        project_id=project_id,
        name=name,
        enabled=True,
        bedrock=ProjectBedrockConfig(
            enabled=True,
            model_ids=[MODEL_A, MODEL_B],
            role_arn=role_arn,
            inference_profile_arns=profiles,
        ),
    )


def insights_row(
    caller_arn,
    model_id,
    usage_day,
    input_tokens,
    output_tokens,
    invocations,
    extra=None,
):
    row = [
        {'field': 'caller_arn', 'value': caller_arn},
        {'field': 'model_id', 'value': model_id},
        {'field': 'usage_day', 'value': f'{usage_day} 00:00:00.000'},
        {'field': 'input_tokens', 'value': str(input_tokens)},
        {'field': 'output_tokens', 'value': str(output_tokens)},
        {'field': 'invocations', 'value': str(invocations)},
    ]
    if extra is not None:
        for field, value in extra.items():
            row.append({'field': field, 'value': value})
    return row


def caller_arn(role_name=ROLE_NAME, session=INSTANCE_ONE):
    return f'arn:aws:sts::{ACCOUNT_ID}:assumed-role/{role_name}/{session}'


def build_service(
    config_values=None,
    results=None,
    owners=None,
    jobs=None,
    projects=None,
    bedrock=None,
    status='Complete',
):
    config = FakeConfig({**CONFIG_VALUES, **(config_values or {})})
    logs = FakeLogs(results=results, status=status)
    ec2 = FakeEc2(
        owners=owners if owners is not None else {INSTANCE_ONE: 'alice'}, jobs=jobs
    )
    context = FakeContext(
        config, FakeAws(bedrock=bedrock or FakeBedrock(), logs=logs, ec2=ec2)
    )

    usage_dao = BedrockUsageDAO(context)
    usage_dao.table = FakeTable(['project_id', 'usage_id'])

    projects_service = FakeProjectsService(
        context, projects if projects is not None else [build_project()], usage_dao
    )
    service = BedrockUsageService(context=context, projects_service=projects_service)
    instance_dao = BedrockInstanceOwnerDAO(context)
    instance_dao.table = FakeTable(['instance_id'])
    service.instance_owner_dao = instance_dao
    return service, context, logs, ec2, usage_dao


def rows_by_usage_id(usage_dao):
    return {usage_id: item for (_, usage_id), item in usage_dao.table.items.items()}


# ------------------------------------------------------------ invocation logging


def build_invocation_logging(config_values=None, logging_config=None):
    config = FakeConfig({**CONFIG_VALUES, **(config_values or {})})
    bedrock = FakeBedrock(logging_config=logging_config)
    context = FakeContext(config, FakeAws(bedrock=bedrock))
    return BedrockInvocationLogging(context), bedrock, context


def test_invocation_logging_feature_off_makes_no_calls():
    manager, bedrock, _ = build_invocation_logging(
        config_values={f'{MODULE_ID}.bedrock.enabled': False}
    )
    assert manager.reconcile() == STATE_DISABLED
    assert bedrock.calls == []


def test_invocation_logging_adopts_when_absent():
    manager, bedrock, _ = build_invocation_logging()
    assert manager.reconcile() == STATE_ADOPTED
    assert [name for name, _ in bedrock.calls] == ['get', 'put']
    logging_config = bedrock.calls[1][1]
    assert logging_config['cloudWatchConfig'] == {
        'logGroupName': LOG_GROUP,
        'roleArn': LOG_ROLE_ARN,
    }


def test_adopted_configuration_excludes_prompt_and_completion_data():
    manager, bedrock, _ = build_invocation_logging()
    manager.reconcile()
    logging_config = bedrock.calls[1][1]
    for key in (
        'textDataDeliveryEnabled',
        'imageDataDeliveryEnabled',
        'embeddingDataDeliveryEnabled',
        'videoDataDeliveryEnabled',
        'audioDataDeliveryEnabled',
    ):
        assert logging_config[key] is False


def test_request_response_data_can_be_opted_in():
    manager, bedrock, _ = build_invocation_logging(
        config_values={
            f'{MODULE_ID}.bedrock.invocation_logging.include_request_response_data': True
        }
    )
    manager.reconcile()
    assert bedrock.calls[1][1]['textDataDeliveryEnabled'] is True


def test_invocation_logging_is_idempotent_when_already_ours():
    manager, bedrock, _ = build_invocation_logging(
        logging_config={
            'cloudWatchConfig': {'logGroupName': LOG_GROUP, 'roleArn': LOG_ROLE_ARN}
        }
    )
    assert manager.reconcile() == STATE_ADOPTED_ALREADY
    assert [name for name, _ in bedrock.calls] == ['get']


def test_invocation_logging_stands_down_for_another_owner():
    manager, bedrock, context = build_invocation_logging(
        logging_config={
            'cloudWatchConfig': {
                'logGroupName': '/somebody-else/bedrock',
                'roleArn': 'arn:aws:iam::123456789012:role/other',
            }
        }
    )
    assert manager.reconcile() == STATE_STOOD_DOWN
    assert [name for name, _ in bedrock.calls] == ['get']
    assert any(
        '/somebody-else/bedrock' in message
        for level, message in context.logger().messages
        if level == 'warning'
    )


def test_invocation_logging_stands_down_for_an_s3_only_configuration():
    manager, bedrock, _ = build_invocation_logging(
        logging_config={'s3Config': {'bucketName': 'someone-elses-bucket'}}
    )
    assert manager.reconcile() == STATE_STOOD_DOWN
    assert [name for name, _ in bedrock.calls] == ['get']


def test_manage_configuration_false_never_writes():
    manager, bedrock, _ = build_invocation_logging(
        config_values={
            f'{MODULE_ID}.bedrock.invocation_logging.manage_configuration': False
        }
    )
    assert manager.reconcile() == STATE_NOT_CONFIGURED
    assert [name for name, _ in bedrock.calls] == ['get']


def test_invocation_logging_without_a_destination_is_unavailable():
    manager, bedrock, _ = build_invocation_logging(
        config_values={f'{MODULE_ID}.bedrock.invocation_log_group_name': None}
    )
    assert manager.reconcile() == STATE_UNAVAILABLE
    assert bedrock.calls == []


# ----------------------------------------------------------------- attribution


def test_query_never_reads_caller_supplied_metadata():
    assert 'requestMetadata' not in INVOCATION_QUERY
    assert 'identity.arn' in INVOCATION_QUERY


def test_parse_caller_arn():
    assert parse_caller_arn(caller_arn()) == (ROLE_NAME, INSTANCE_ONE)
    assert parse_caller_arn(f'arn:aws:iam::{ACCOUNT_ID}:user/someone') is None
    assert parse_caller_arn('') is None
    assert parse_caller_arn(None) is None


def test_attribution_uses_the_role_and_the_instance_owner():
    service, _, _, ec2, usage_dao = build_service(
        results=[insights_row(caller_arn(), MODEL_B, TODAY, 100, 20, 4)]
    )
    service.aggregate()

    rows = rows_by_usage_id(usage_dao)
    day_key = f'day#{TODAY}#alice#{MODEL_B}'
    assert day_key in rows
    assert rows[day_key]['input_tokens'] == 100
    assert rows[day_key]['output_tokens'] == 20
    assert rows[day_key]['total_tokens'] == 120
    assert rows[day_key]['invocations'] == 4
    assert rows[f'user#{PERIOD}#alice']['total_tokens'] == 120
    assert rows[f'project#{PERIOD}']['total_tokens'] == 120
    assert ec2.calls[0][1]['Filters'] == [
        {'Name': 'instance-id', 'Values': [INSTANCE_ONE]}
    ]


def test_caller_supplied_username_is_ignored():
    service, _, _, _, usage_dao = build_service(
        results=[
            insights_row(
                caller_arn(),
                MODEL_B,
                TODAY,
                10,
                1,
                1,
                extra={'requestMetadata.username': 'mallory'},
            )
        ]
    )
    service.aggregate()
    rows = rows_by_usage_id(usage_dao)
    assert f'day#{TODAY}#alice#{MODEL_B}' in rows
    assert not any('mallory' in usage_id for usage_id in rows)


def test_inference_profile_arn_maps_back_to_the_catalog_model_id():
    service, _, _, _, usage_dao = build_service(
        results=[insights_row(caller_arn(), PROFILE_A, TODAY, 5, 5, 1)]
    )
    service.aggregate()
    assert f'day#{TODAY}#alice#{MODEL_A}' in rows_by_usage_id(usage_dao)


def test_unknown_role_and_unknown_profile_are_not_attributed():
    service, context, _, _, usage_dao = build_service(
        results=[
            insights_row(
                caller_arn(role_name='some-other-role'), MODEL_B, TODAY, 999, 999, 9
            )
        ]
    )
    service.aggregate()
    assert rows_by_usage_id(usage_dao) == {}
    assert any(
        'were not attributed' in message
        for level, message in context.logger().messages
        if level == 'info'
    )


def test_non_instance_session_lands_in_the_unattributed_bucket():
    service, _, _, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(session='someone-cli'), MODEL_B, TODAY, 7, 3, 1)
        ]
    )
    service.aggregate()
    rows = rows_by_usage_id(usage_dao)
    assert f'day#{TODAY}#{UNATTRIBUTED_USER}#{MODEL_B}' in rows
    assert rows[f'project#{PERIOD}']['total_tokens'] == 10


def test_untagged_instance_lands_in_the_unattributed_bucket():
    service, _, _, _, usage_dao = build_service(
        results=[insights_row(caller_arn(), MODEL_B, TODAY, 7, 3, 1)], owners={}
    )
    service.aggregate()
    assert f'day#{TODAY}#{UNATTRIBUTED_USER}#{MODEL_B}' in rows_by_usage_id(usage_dao)


def test_usage_of_two_users_rolls_up_per_user_and_per_project():
    service, _, _, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 10, 5, 1),
            insights_row(caller_arn(session=INSTANCE_TWO), MODEL_A, TODAY, 20, 5, 2),
            insights_row(caller_arn(session=INSTANCE_TWO), MODEL_B, TODAY, 1, 1, 1),
        ],
        owners={INSTANCE_ONE: 'alice', INSTANCE_TWO: 'bob'},
    )
    service.aggregate()
    rows = rows_by_usage_id(usage_dao)
    assert rows[f'user#{PERIOD}#alice']['total_tokens'] == 15
    assert rows[f'user#{PERIOD}#bob']['total_tokens'] == 27
    assert rows[f'project#{PERIOD}']['total_tokens'] == 42
    assert rows[f'project#{PERIOD}']['invocations'] == 4


def test_a_project_only_receives_its_own_usage():
    project_two = build_project(
        project_id=PROJECT_ID_2,
        role_arn=f'arn:aws:iam::{ACCOUNT_ID}:role/idea/{CLUSTER_NAME}/projects/{CLUSTER_NAME}-{PROJECT_ID_2}-project',
        profiles={},
        name='other',
    )
    service, _, _, _, usage_dao = build_service(
        results=[insights_row(caller_arn(), MODEL_B, TODAY, 10, 10, 1)],
        projects=[build_project(), project_two],
    )
    service.aggregate()
    assert all(
        project_id == PROJECT_ID for project_id, _ in usage_dao.table.items.keys()
    )


# ------------------------------------------------------------------ idempotency


def test_repeating_a_run_produces_identical_rows():
    results = [insights_row(caller_arn(), MODEL_B, TODAY, 100, 20, 4)]
    service, _, _, _, usage_dao = build_service(results=results)
    service.aggregate()
    first = {
        key: {k: v for k, v in item.items() if k != 'updated_on'}
        for key, item in usage_dao.table.items.items()
    }
    service.aggregate()
    second = {
        key: {k: v for k, v in item.items() if k != 'updated_on'}
        for key, item in usage_dao.table.items.items()
    }
    assert first == second


def test_recompute_removes_rows_that_left_the_window():
    logs_results = [
        insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 10, 5, 1),
        insights_row(caller_arn(session=INSTANCE_TWO), MODEL_A, TODAY, 20, 5, 2),
    ]
    service, _, logs, _, usage_dao = build_service(
        results=logs_results, owners={INSTANCE_ONE: 'alice', INSTANCE_TWO: 'bob'}
    )
    service.aggregate()
    assert f'user#{PERIOD}#bob' in rows_by_usage_id(usage_dao)

    logs.results = [logs_results[0]]
    service.aggregate()
    rows = rows_by_usage_id(usage_dao)
    assert f'day#{TODAY}#bob#{MODEL_A}' not in rows
    assert f'user#{PERIOD}#bob' not in rows
    assert rows[f'project#{PERIOD}']['total_tokens'] == 15


def test_usage_dropping_to_zero_removes_the_project_rollup():
    service, _, logs, _, usage_dao = build_service(
        results=[insights_row(caller_arn(), MODEL_B, TODAY, 10, 10, 1)]
    )
    service.aggregate()
    assert f'project#{PERIOD}' in rows_by_usage_id(usage_dao)

    logs.results = []
    service.aggregate()
    assert rows_by_usage_id(usage_dao) == {}


def test_instance_owner_is_resolved_once_and_cached_in_dynamodb():
    service, _, _, ec2, _ = build_service(
        results=[insights_row(caller_arn(), MODEL_B, TODAY, 10, 10, 1)]
    )
    service.aggregate()
    assert len(ec2.calls) == 1
    assert (INSTANCE_ONE,) in service.instance_owner_dao.table.items

    # a fresh process keeps the mapping through the dynamodb cache
    service._instance_cache.clear()
    service.aggregate()
    assert len(ec2.calls) == 1


def test_terminated_instance_still_resolves_from_the_cache():
    service, _, _, ec2, usage_dao = build_service(
        results=[insights_row(caller_arn(), MODEL_B, TODAY, 10, 10, 1)]
    )
    service.aggregate()
    service._instance_cache.clear()
    ec2.owners = {}
    service.aggregate()
    assert f'day#{TODAY}#alice#{MODEL_B}' in rows_by_usage_id(usage_dao)


# ------------------------------------------------------------------- query path


def test_query_window_covers_the_lookback_days():
    service, _, logs, _, _ = build_service(results=[])
    service.aggregate()
    start_query = [call for call in logs.calls if call[0] == 'start_query'][0][1]
    assert start_query['logGroupName'] == LOG_GROUP
    expected_start = arrow.get(
        arrow.utcnow().shift(days=-1).format('YYYY-MM-DD')
    ).int_timestamp
    assert start_query['startTime'] == expected_start


def test_query_failure_is_raised():
    service, _, _, _, _ = build_service(results=[], status='Failed')
    with pytest.raises(exceptions.SocaException):
        service.aggregate()


def test_result_truncation_is_reported():
    service, context, _, _, _ = build_service(
        results=[insights_row(caller_arn(), MODEL_B, TODAY, 1, 1, 1)],
        config_values={f'{MODULE_ID}.bedrock.usage.max_query_results': 1},
    )
    service.aggregate()
    assert any(
        'maximum of 1 rows' in message
        for level, message in context.logger().messages
        if level == 'warning'
    )


def test_disabled_usage_service_does_not_start():
    service, _, logs, _, _ = build_service(
        config_values={f'{MODULE_ID}.bedrock.usage.enabled': False}
    )
    assert service.is_enabled() is False
    service.start()
    assert service._thread.is_alive() is False
    assert logs.calls == []


def test_feature_off_disables_the_usage_service():
    service, _, _, _, _ = build_service(
        config_values={f'{MODULE_ID}.bedrock.enabled': False}
    )
    assert service.is_enabled() is False


def test_run_once_takes_the_distributed_lock():
    service, context, _, _, _ = build_service(results=[])
    service.run_once()
    assert context.distributed_lock().calls == [
        ('acquire', f'{MODULE_ID}-bedrock-usage'),
        ('release', f'{MODULE_ID}-bedrock-usage'),
    ]


# --------------------------------------------------------------------- read api


class StubProjectsService:
    def __init__(self, context, usage_dao, enabled=True):
        self.context = context
        self.logger = context.logger()
        self.bedrock_usage_dao = usage_dao
        self.bedrock_provisioner = type('P', (), {'is_enabled': lambda self: enabled})()

    build_bedrock_usage = staticmethod(ProjectsService.build_bedrock_usage)
    get_project_bedrock_usage = ProjectsService.get_project_bedrock_usage
    apply_bedrock_spend = ProjectsService.apply_bedrock_spend


def test_get_project_bedrock_usage_returns_totals_and_users():
    service, context, _, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 10, 5, 1),
            insights_row(caller_arn(session=INSTANCE_TWO), MODEL_A, TODAY, 20, 5, 2),
        ],
        owners={INSTANCE_ONE: 'alice', INSTANCE_TWO: 'bob'},
    )
    service.aggregate()

    stub = StubProjectsService(context, usage_dao)
    usage = ProjectsService.get_project_bedrock_usage(stub, project_id=PROJECT_ID)
    assert usage.period == PERIOD
    assert usage.username is None
    assert usage.total_tokens == 40
    assert [entry.username for entry in usage.by_user] == ['bob', 'alice']
    assert usage.by_user[0].total_tokens == 25
    assert [entry.model_id for entry in usage.by_model] == [MODEL_A]


def test_get_project_bedrock_usage_scoped_to_one_user():
    service, context, _, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 10, 5, 1),
            insights_row(caller_arn(session=INSTANCE_TWO), MODEL_A, TODAY, 20, 5, 2),
        ],
        owners={INSTANCE_ONE: 'alice', INSTANCE_TWO: 'bob'},
    )
    service.aggregate()

    stub = StubProjectsService(context, usage_dao)
    usage = ProjectsService.get_project_bedrock_usage(
        stub, project_id=PROJECT_ID, username='alice'
    )
    assert usage.username == 'alice'
    assert usage.total_tokens == 15
    assert usage.by_user is None


def test_get_project_bedrock_usage_is_none_when_the_feature_is_off():
    service, context, _, _, usage_dao = build_service(results=[])
    stub = StubProjectsService(context, usage_dao, enabled=False)
    assert (
        ProjectsService.get_project_bedrock_usage(stub, project_id=PROJECT_ID) is None
    )


def test_get_project_bedrock_usage_is_none_without_rows():
    service, context, _, _, usage_dao = build_service(results=[])
    stub = StubProjectsService(context, usage_dao)
    assert (
        ProjectsService.get_project_bedrock_usage(stub, project_id=PROJECT_ID) is None
    )


def test_project_usage_by_model_sums_across_users():
    service, _, _, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 10, 5, 1),
            insights_row(caller_arn(session=INSTANCE_TWO), MODEL_A, TODAY, 20, 5, 2),
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_B, TODAY, 1, 1, 1),
        ],
        owners={INSTANCE_ONE: 'alice', INSTANCE_TWO: 'bob'},
    )
    service.aggregate()

    by_model = get_project_usage_by_model(usage_dao, PROJECT_ID, PERIOD)

    assert [entry.model_id for entry in by_model] == [MODEL_A, MODEL_B]
    assert by_model[0].total_tokens == 40
    assert by_model[0].invocations == 3
    assert by_model[1].total_tokens == 2


# ------------------------------------------------------------------ spend


def usage_with_spend(spend=None, raises=False, project_name='research'):
    service, context, _, _, usage_dao = build_service(
        results=[insights_row(caller_arn(), MODEL_A, TODAY, 10, 5, 1)]
    )
    service.aggregate()
    context.aws_util().spend = spend
    context.aws_util().raises = raises
    stub = StubProjectsService(context, usage_dao)
    usage = ProjectsService.get_project_bedrock_usage(
        stub, project_id=PROJECT_ID, project_name=project_name
    )
    return usage, context


def test_bedrock_spend_is_unavailable_when_cost_explorer_has_no_answer():
    usage, context = usage_with_spend(spend=None)

    assert usage.spend_is_unavailable is True
    assert usage.spend is None
    assert context.aws_util().calls == [(constants.IDEA_TAG_PROJECT, 'research')]


def test_bedrock_spend_is_zero_when_nothing_is_priced_yet():
    usage, _ = usage_with_spend(spend={})

    assert usage.spend.amount == 0.0
    assert usage.spend_is_unavailable is None


def test_bedrock_spend_counts_only_bedrock_services():
    usage, _ = usage_with_spend(
        spend={
            'Claude Opus 5 (Amazon Bedrock Edition)': 1.2,
            'Amazon Bedrock': 0.1,
            'Amazon EC2': 9.0,
        }
    )

    assert usage.spend.amount == 1.30


def test_bedrock_spend_failure_reads_as_unavailable_not_zero():
    usage, _ = usage_with_spend(raises=True)

    assert usage.spend_is_unavailable is True
    assert usage.spend is None


# ------------------------------------------------------------ api hydration


class StubApiContext:
    def __init__(self, projects_service, logger):
        self.projects = projects_service
        self._logger = logger

    def logger(self, name=None):
        return self._logger


class StubApi:
    def __init__(self, context):
        self.context = context

    apply_bedrock_usage = ProjectsAPI.apply_bedrock_usage


def test_api_does_not_read_usage_for_a_project_without_bedrock():
    service, context, _, _, usage_dao = build_service(results=[])
    stub_projects = StubProjectsService(context, usage_dao)
    api = StubApi(StubApiContext(stub_projects, context.logger()))
    project = Project(project_id=PROJECT_ID, name='plain', enabled=True)
    api.apply_bedrock_usage(project)
    assert project.bedrock_usage is None
    assert usage_dao.table.calls == []


def test_api_hydration_survives_a_usage_read_failure():
    service, context, _, _, usage_dao = build_service(results=[])

    class Exploding:
        table = usage_dao.table

        def get_project_rollup(self, **kwargs):
            raise RuntimeError('table unavailable')

    stub_projects = StubProjectsService(context, Exploding())
    api = StubApi(StubApiContext(stub_projects, context.logger()))
    project = build_project()
    api.apply_bedrock_usage(project)
    # a read failure must be distinguishable from a project nobody used.
    assert project.bedrock_usage is not None
    assert project.bedrock_usage.is_unavailable is True
    assert project.bedrock_usage.total_tokens is None
    assert any(
        'failed to read bedrock usage' in message
        for level, message in context.logger().messages
        if level == 'warning'
    )


def test_usage_is_never_written_back_to_the_projects_table():
    project = build_project()
    project.bedrock_usage = ProjectBedrockUsage(period=PERIOD, total_tokens=42)
    db_project = ProjectsDAO.convert_to_db(project)
    assert 'bedrock_usage' not in db_project
    assert 'bedrock' in db_project


def test_a_compute_node_attributes_tokens_to_its_job_as_well_as_its_owner():
    """a job host carries idea:JobId next to idea:JobOwner, so both dimensions come from one describe"""
    service, _, _, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 10, 5, 1),
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 20, 5, 2),
        ],
        owners={INSTANCE_ONE: 'alice'},
        jobs={INSTANCE_ONE: '4242'},
    )

    service.aggregate()
    rows = rows_by_usage_id(usage_dao)

    assert rows[f'dayjob#{TODAY}#4242#{MODEL_A}']['total_tokens'] == 40
    assert rows[f'job#{PERIOD}#4242']['total_tokens'] == 40
    assert rows[f'job#{PERIOD}#4242']['invocations'] == 3
    # the user dimension is unchanged by the job rows existing
    assert rows[f'user#{PERIOD}#alice']['total_tokens'] == 40


def test_a_desktop_produces_no_job_rows():
    """a desktop is not a job, and an empty job id must not create a job#<period># row"""
    service, _, _, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(session=INSTANCE_ONE), MODEL_A, TODAY, 10, 5, 1)
        ],
        owners={INSTANCE_ONE: 'alice'},
        jobs={},
    )

    service.aggregate()
    rows = rows_by_usage_id(usage_dao)

    assert not any(key.startswith('job#') for key in rows)
    assert not any(key.startswith('dayjob#') for key in rows)
    assert rows[f'user#{PERIOD}#alice']['total_tokens'] == 15


# a second day in the same month as today, so both land in one period
OTHER_DAY = (
    arrow.get(TODAY).shift(days=-1 if TODAY[8:] != '01' else 1).format('YYYY-MM-DD')
)


def test_a_job_spanning_days_keeps_every_day_in_its_month_rollup():
    """with a one day window only today is recomputed; the rollup still covers the month"""
    service, _, logs, _, usage_dao = build_service(
        results=[
            insights_row(caller_arn(), MODEL_A, OTHER_DAY, 10, 5, 1),
            insights_row(caller_arn(), MODEL_A, TODAY, 20, 5, 2),
        ],
        jobs={INSTANCE_ONE: '4242'},
        config_values={f'{MODULE_ID}.bedrock.usage.lookback_days': 1},
    )
    service.aggregate()
    assert rows_by_usage_id(usage_dao)[f'job#{PERIOD}#4242']['total_tokens'] == 40

    logs.results = [insights_row(caller_arn(), MODEL_A, TODAY, 20, 5, 2)]
    service.aggregate()
    rows = rows_by_usage_id(usage_dao)

    assert f'dayjob#{OTHER_DAY}#4242#{MODEL_A}' in rows
    assert rows[f'job#{PERIOD}#4242']['total_tokens'] == 40
    assert rows[f'job#{PERIOD}#4242']['invocations'] == 3


def test_a_reattributed_job_loses_its_stale_day_rows_and_rollup():
    service, _, _, ec2, usage_dao = build_service(
        results=[insights_row(caller_arn(), MODEL_A, TODAY, 10, 5, 1)],
        jobs={INSTANCE_ONE: '4242'},
    )
    service.aggregate()
    assert f'dayjob#{TODAY}#4242#{MODEL_A}' in rows_by_usage_id(usage_dao)

    ec2.jobs = {INSTANCE_ONE: '4343'}
    service._instance_cache.clear()
    service.instance_owner_dao.table.items.clear()
    service.aggregate()
    rows = rows_by_usage_id(usage_dao)

    assert f'dayjob#{TODAY}#4242#{MODEL_A}' not in rows
    assert f'job#{PERIOD}#4242' not in rows
    assert rows[f'dayjob#{TODAY}#4343#{MODEL_A}']['total_tokens'] == 15
    assert rows[f'job#{PERIOD}#4343']['total_tokens'] == 15


def test_job_rollups_are_read_for_one_project_only():
    service, _, _, _, usage_dao = build_service(results=[])
    for project_id in (PROJECT_ID, PROJECT_ID_2):
        usage_dao.table.put_item(
            Item={
                'project_id': project_id,
                'usage_id': f'job#{PERIOD}#4242',
                'total_tokens': 1,
            }
        )

    rows = usage_dao.query_job_rollups(PROJECT_ID, PERIOD)

    assert [row['project_id'] for row in rows] == [PROJECT_ID]
    condition = usage_dao.table.calls[-1][1].get_expression()
    assert condition['operator'] == 'AND'
    assert condition['values'][0].get_expression()['values'][0].name == 'project_id'
