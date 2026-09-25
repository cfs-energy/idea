from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from ideaclustermanager.app.api.cluster_settings_api import ClusterSettingsAPI


class FakeEcs:
    def __init__(self):
        self.service_batches = []
        self.definitions = []
        self.task_batches = []

    def list_services(self, **kwargs):
        if 'nextToken' not in kwargs:
            return {'serviceArns': [str(i) for i in range(10)], 'nextToken': 'next'}
        return {'serviceArns': ['10']}

    def describe_services(self, services, **kwargs):
        self.service_batches.append(services)
        return {
            'services': [
                {
                    'serviceName': f'service-{i}',
                    'desiredCount': 2,
                    'runningCount': 1,
                    'pendingCount': 1,
                    'taskDefinition': 'old',
                    'deployments': [
                        {'status': 'ACTIVE', 'taskDefinition': 'old'},
                        {
                            'status': 'PRIMARY',
                            'taskDefinition': 'active',
                            'rolloutState': 'IN_PROGRESS',
                            'updatedAt': datetime(2026, 9, 21, tzinfo=timezone.utc),
                        },
                    ],
                }
                for i in services
            ]
        }

    def describe_task_definition(self, taskDefinition):
        self.definitions.append(taskDefinition)
        return {
            'taskDefinition': {
                'containerDefinitions': [{'image': 'example/service:26.09.4'}]
            }
        }

    def list_tasks(self, **kwargs):
        assert kwargs['desiredStatus'] == 'RUNNING'
        if 'nextToken' not in kwargs:
            return {'taskArns': [str(i) for i in range(100)], 'nextToken': 'next'}
        return {'taskArns': ['100']}

    def describe_tasks(self, tasks, **kwargs):
        self.task_batches.append(tasks)
        return {
            'tasks': [
                {
                    'taskArn': f'task/{i}',
                    'lastStatus': 'RUNNING',
                    'startedAt': datetime(2026, 9, 21, tzinfo=timezone.utc),
                    'healthStatus': 'HEALTHY',
                }
                for i in tasks
            ]
        }


def setup_api():
    from ideaclustermanagertests.test_cluster_settings_scoping import scoped_invocation

    app, invocation = scoped_invocation('administrator')
    app.cluster_name.return_value = 'configured'
    app.config().get_string.return_value = 'configured'
    ecs = FakeEcs()
    app.aws().get_client.return_value = ecs
    api = ClusterSettingsAPI(app)
    invocation.namespace = 'ClusterSettings.ListClusterServices'
    invocation.is_administrator.return_value = True
    return api, invocation, ecs


def test_live_services_paginate_batch_and_use_primary_definition():
    api, invocation, ecs = setup_api()
    api.invoke(invocation)
    result = invocation.success.call_args.args[0]
    assert not result.errors
    assert len(result.listing) == 11
    assert [len(batch) for batch in ecs.service_batches] == [10, 1]
    assert ecs.definitions == ['active']
    row = result.listing[0]
    assert (row.desired, row.running, row.pending) == (2, 1, 1)
    assert row.images == ['example/service:26.09.4']
    assert row.rollout_state == 'IN_PROGRESS'
    assert row.updated_at == '2026-09-21T00:00:00+00:00'
    assert len(row.tasks) == 101
    assert row.tasks[0].health == 'HEALTHY'
    assert row.tasks[0].started_at == row.updated_at
    assert max(map(len, ecs.task_batches)) == 100


@pytest.mark.parametrize(
    'method',
    [
        'describe_services',
        'describe_task_definition',
        'describe_tasks',
        'list_services',
        'list_tasks',
    ],
)
def test_failures_are_reported_without_crashing(method):
    api, invocation, ecs = setup_api()
    setattr(ecs, method, Mock(side_effect=RuntimeError('private details')))
    api.invoke(invocation)
    result = invocation.success.call_args.args[0]
    assert result.errors
    assert 'private details' not in str(result)


@pytest.mark.parametrize('method', ['describe_services', 'describe_tasks'])
def test_describe_failure_entries_are_reported(method):
    api, invocation, ecs = setup_api()
    setattr(ecs, method, Mock(return_value={'failures': [{'reason': 'MISSING'}]}))
    api.invoke(invocation)
    assert invocation.success.call_args.args[0].errors


def test_administrators_only():
    api, invocation, _ = setup_api()
    invocation.is_administrator.return_value = False
    with pytest.raises(Exception):
        api.invoke(invocation)
    api.context.aws().get_client.assert_not_called()


def test_no_container_configuration_needs_no_client():
    api, invocation, _ = setup_api()
    api.context.config().get_string.return_value = None
    api.invoke(invocation)
    assert not invocation.success.call_args.args[0].listing
    api.context.aws().get_client.assert_not_called()
