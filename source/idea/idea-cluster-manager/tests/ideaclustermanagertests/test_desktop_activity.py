"""Idle time from the idle stop's checks: the rules, the half-hour count and the daily records."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import arrow
import pytest
from botocore.exceptions import ClientError

from ideadatamodel.reporting.efficiency import desktop_check_idle
from ideaclustermanager.app.costs.desktop_activity import (
    DesktopActivity,
    checks_from_events,
    tally,
)

THRESHOLD = 30


def check(cpu=1.0, connections=0, logins=0):
    value = {'DCV': {'num-of-connections': connections}}
    if cpu is not None:
        value['CPUAveragePerformanceLast10Secs'] = cpu
    if logins is not None:
        value['SSH_Connection_Count'] = logins
    return value


@pytest.mark.parametrize(
    'sample, idle',
    [
        (check(cpu=29.9), True),
        # the idle stop treats a reading at the threshold as in use
        (check(cpu=30), False),
        (check(cpu=95, connections=None), False),
        (check(connections=1), False),
        (check(logins=1), False),
        # Windows hosts report no login sessions
        (check(logins=None), True),
        (check(cpu=None), None),
        (check(connections=None), None),
        ({'CPUAveragePerformanceLast10Secs': 1}, None),
        (None, None),
    ],
)
def test_check_idle_follows_the_idle_stop_rules(sample, idle):
    assert desktop_check_idle(sample, THRESHOLD) is idle


def at(text):
    return arrow.get(text).int_timestamp * 1000


def test_tally_counts_half_hours_without_filling_gaps():
    days = tally(
        [
            (at('2024-02-01T10:00:05Z'), check()),
            # a second check in the same half hour that was in use wins
            (at('2024-02-01T10:10:00Z'), check(cpu=80)),
            (at('2024-02-01T10:30:05Z'), check()),
            # three hours without checks are not counted either way
            (at('2024-02-01T13:30:05Z'), check()),
            (at('2024-02-01T14:00:05Z'), check(cpu=None)),
            # the cluster's day, not UTC's
            (at('2024-02-02T03:00:00Z'), check()),
        ],
        THRESHOLD,
        'America/New_York',
    )
    assert dict(days) == {'2024-02-01': [4, 3]}


def test_checks_come_from_whole_stdout_streams():
    events = [
        dict(
            logStreamName='cmd-1/i-1/aws-runPowerShellScript/stdout',
            timestamp=2000,
            message='{"DCV": {"num-of-connections": 0},',
        ),
        dict(
            logStreamName='cmd-1/i-1/aws-runPowerShellScript/stdout',
            timestamp=2001,
            message='"CPUAveragePerformanceLast10Secs": 3}',
        ),
        dict(
            logStreamName='cmd-1/i-1/aws-runPowerShellScript/stderr',
            timestamp=2001,
            message='warning',
        ),
        dict(
            logStreamName='cmd-2/i-1/aws-runShellScript/stdout',
            timestamp=5000,
            message='not json',
        ),
    ]
    assert list(checks_from_events(events)) == [
        (2000, {'DCV': {'num-of-connections': 0}, 'CPUAveragePerformanceLast10Secs': 3})
    ]


class Store:
    def __init__(self, state=None):
        self.rows = {}
        if state:
            self.rows['idle-state'] = state

    def get(self, subject, record):
        return self.rows.get(record)

    def put(self, subject, record, value, **kwargs):
        self.rows[record] = value

    put_source = put


def context(sessions, events):
    value = Mock()
    value.cluster_name.return_value = 'cluster-a'
    value.config().is_module_enabled.return_value = True
    value.config().get_module_id.return_value = 'vdc'
    value.config().get_float.return_value = THRESHOLD
    value.config().get_string.return_value = 'sessions-alias'
    value.analytics_service().os_client.os_client.search.return_value = {
        'hits': {'hits': [{'_source': s} for s in sessions]}
    }
    value.aws().dynamodb_table().Table.return_value.scan.return_value = {'Items': []}

    def filter_log_events(logGroupName, **kwargs):
        if logGroupName not in events:
            raise ClientError(
                {'Error': {'Code': 'ResourceNotFoundException'}}, 'FilterLogEvents'
            )
        return {'events': events[logGroupName]}

    value.aws().logs().filter_log_events.side_effect = filter_log_events
    return value


def stream(name, when, sample):
    return dict(
        logStreamName=f'{name}/i-1/aws-runShellScript/stdout',
        timestamp=at(when),
        message=json.dumps(sample),
    )


def test_capture_writes_each_unsettled_day_and_leaves_unchecked_desktops_out():
    sessions = [
        dict(
            idea_session_id='s-1',
            owner='user-a',
            created_on=0,
            server=dict(instance_type='g6.xlarge'),
        ),
        dict(idea_session_id='s-2', owner='user-b', created_on=0),
    ]
    events = {
        '/cluster-a/vdc/dcv-session/s-1/cpu-utilization': [
            stream('c1', '2024-03-01T10:00:05Z', check()),
            stream('c2', '2024-03-02T10:00:05Z', check(connections=1)),
        ]
    }
    store = Store(state={'settled_through': '2024-02-29'})
    value = context(sessions, events)
    DesktopActivity(value, store).capture(arrow.get('2024-03-02T12:00:00+00:00'))
    assert store.rows['idle:2024-03-01'] == {
        'sessions': {
            's-1': dict(owner='user-a', instance_type='g6.xlarge', checks=1, idle=1)
        }
    }
    assert store.rows['idle:2024-03-02']['sessions']['s-1']['idle'] == 0
    assert 'idle:2024-02-29' not in store.rows
    assert store.rows['idle-state'] == {'settled_through': '2024-03-01'}
    call = value.aws().logs().filter_log_events.call_args_list[0].kwargs
    assert call['startTime'] == at('2024-03-01T00:00:00Z')
    assert call['endTime'] == at('2024-03-02T12:00:00Z')
    query = value.analytics_service().os_client.os_client.search.call_args.kwargs[
        'body'
    ]
    assert {'range': {'updated_on': {'gte': at('2024-03-01T00:00:00Z')}}} in query[
        'query'
    ]['bool']['should']


def test_capture_writes_nothing_when_a_log_read_is_refused():
    sessions = [dict(idea_session_id='s-1', owner='user-a', created_on=0)]
    value = context(sessions, {})
    value.aws().logs().filter_log_events.side_effect = ClientError(
        {'Error': {'Code': 'AccessDeniedException'}}, 'FilterLogEvents'
    )
    store = Store()
    with pytest.raises(ClientError):
        DesktopActivity(value, store).capture(arrow.get('2024-03-02T12:00:00+00:00'))
    assert store.rows == {}


def test_capture_skips_clusters_without_desktops():
    value = SimpleNamespace(
        config=lambda: SimpleNamespace(is_module_enabled=lambda name: False)
    )
    store = Store()
    DesktopActivity(value, store).capture(arrow.get('2024-03-02T12:00:00+00:00'))
    assert store.rows == {}
