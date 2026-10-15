"""
Test Cases for putting the instance id back on desktops deleted before 26.10.6

Their history rows carry no instance id, so their cost was guessed from creation to
deletion. The controller matches each to its RunInstances event in CloudTrail by owner
and launch time; a row it cannot match uniquely is marked unknown, never guessed. No AWS
calls are made.
"""

from ideavirtualdesktopcontroller.app.sessions.desktop_launches import DesktopLaunches
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_history_db import (
    VirtualDesktopSessionHistoryDB,
)

from botocore.exceptions import ClientError

import arrow
import json
from unittest.mock import Mock

import pytest

CLUSTER = 'idea-test'


class FakeConfig:
    @staticmethod
    def get_int(key, default=None):
        return default


class FakeCloudTrail:
    def __init__(self, pages, throttle=0):
        self.pages = list(pages)
        self.throttle = throttle
        self.requests = []

    def lookup_events(self, **request):
        self.requests.append(dict(request))
        if self.throttle:
            self.throttle -= 1
            raise ClientError(
                {'Error': {'Code': 'ThrottlingException', 'Message': 'Rate exceeded'}},
                'LookupEvents',
            )
        index = int(request.get('NextToken', '0'))
        page = {'Events': self.pages[index]}
        if index + 1 < len(self.pages):
            page['NextToken'] = str(index + 1)
        return page


class FakeContext:
    def __init__(self, cloudtrail=None):
        self._cloudtrail = cloudtrail

    def cluster_name(self):
        return CLUSTER

    def module_id(self):
        return 'vdc'

    def config(self):
        return FakeConfig()

    def logger(self, name=None):
        return Mock()

    def aws(self):
        return Mock(cloudtrail=lambda: self._cloudtrail)


class FakeHistoryTable:
    def __init__(self, rows):
        self.rows = {(row['owner'], row['idea_session_id']): row for row in rows}
        self.updates = []

    def scan(self, **kwargs):
        return {'Items': [dict(row) for row in self.rows.values()]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **kwargs):
        key = (Key['owner'], Key['idea_session_id'])
        if key not in self.rows:
            # attribute_exists(idea_session_id): the row expired meanwhile
            assert kwargs['ConditionExpression'] == 'attribute_exists(idea_session_id)'
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': ''}},
                'UpdateItem',
            )
        self.updates.append(Key)
        field = UpdateExpression.split()[1]
        self.rows[key][field] = ExpressionAttributeValues[':value']


def launch(owner, at, instance_id, instance_type='c7i.2xlarge', cluster=CLUSTER):
    """a RunInstances event as LookupEvents returns it; the session tag is still TBD"""
    tags = [
        {'key': 'idea:NodeType', 'value': 'virtual-desktop-dcv-host'},
        {'key': 'idea:ClusterName', 'value': cluster},
        {'key': 'idea:JobOwner', 'value': owner},
        {'key': 'idea:DCVSessionUUID', 'value': 'TBD'},
    ]
    return {
        'EventName': 'RunInstances',
        'EventTime': at.datetime,
        'CloudTrailEvent': json.dumps(
            {
                'eventTime': at.isoformat(),
                'requestParameters': {
                    'tagSpecificationSet': {
                        'items': [{'resourceType': 'instance', 'tags': tags}]
                    }
                },
                'responseElements': {
                    'instancesSet': {
                        'items': [
                            {'instanceId': instance_id, 'instanceType': instance_type}
                        ]
                    }
                },
            }
        ),
    }


def row(session_id, owner, created, **kwargs):
    return dict(
        owner=owner,
        idea_session_id=session_id,
        created_on=created.int_timestamp * 1000,
        instance_type='c7i.2xlarge',
        **kwargs,
    )


def backfill(rows, pages, throttle=0, live=(), table=None):
    cloudtrail = FakeCloudTrail(pages, throttle=throttle)
    context = FakeContext(cloudtrail)
    history = VirtualDesktopSessionHistoryDB(context, logger=Mock())
    history._table_obj = table or FakeHistoryTable(rows)
    history._initialized = True
    sleeps = []
    launches = DesktopLaunches(context, sleep=sleeps.append)
    done = history.backfill_instance_ids(launches, lambda: set(live))
    return history, cloudtrail, sleeps, done


def stored(history, session_id):
    return next(
        r
        for r in history._table_obj.rows.values()
        if r['idea_session_id'] == session_id
    )


def test_a_unique_launch_puts_the_instance_id_back():
    created = arrow.utcnow().shift(days=-55)
    history, _, _, done = backfill(
        [row('sess-1', 'granetz', created)],
        [
            [launch('granetz', created.shift(seconds=40), 'i-0fd18088aa5535586')],
            # another user's desktop at the same time, another cluster's, a compute node
            [
                launch('someone', created, 'i-0other'),
                launch('granetz', created, 'i-0elsewhere', cluster='idea-dev'),
            ],
        ],
    )
    assert done
    assert stored(history, 'sess-1')['instance_id'] == 'i-0fd18088aa5535586'
    assert 'instance_unknown' not in stored(history, 'sess-1')


def test_ambiguous_missing_and_too_old_rows_are_marked_unknown():
    now = arrow.utcnow()
    twin = now.shift(days=-30)
    history, _, _, _ = backfill(
        [
            row('sess-twin', 'user-a', twin),
            row('sess-none', 'user-b', now.shift(days=-20)),
            row('sess-old', 'user-c', now.shift(days=-120)),
        ],
        [
            [
                launch('user-a', twin.shift(minutes=-5), 'i-0one'),
                launch('user-a', twin.shift(minutes=5), 'i-0two'),
                # launched for user-c, but long after the row was created
                launch('user-c', now.shift(days=-80), 'i-0late'),
            ]
        ],
    )
    for session_id in ('sess-twin', 'sess-none', 'sess-old'):
        assert stored(history, session_id)['instance_unknown'] is True
        assert 'instance_id' not in stored(history, session_id)


def test_throttling_backs_off_and_carries_on():
    created = arrow.utcnow().shift(days=-10)
    history, cloudtrail, sleeps, done = backfill(
        [row('sess-1', 'user-a', created)],
        [[launch('user-a', created, 'i-0abc')]],
        throttle=3,
    )
    assert done
    assert sleeps[:3] == [1, 2, 4]
    assert len(cloudtrail.requests) == 4
    assert stored(history, 'sess-1')['instance_id'] == 'i-0abc'


def test_a_completed_reader_and_backfill_write_nothing_on_second_run():
    created = arrow.utcnow().shift(days=-10)
    history, cloudtrail, _, _ = backfill(
        [row('sess-1', 'user-a', created), row('sess-2', 'user-b', created)],
        [[launch('user-a', created, 'i-0abc')]],
    )
    written = len(history._table_obj.updates)

    launches = DesktopLaunches(history.context, sleep=lambda _: None)
    assert history.backfill_instance_ids(launches) is True
    assert len(history._table_obj.updates) == written
    assert len(cloudtrail.requests) == 2
    # run rows and rows written since 26.10.6 never needed it
    history._table_obj.rows[('user-c', 'sess-3#run#1')] = dict(
        owner='user-c', idea_session_id='sess-3#run#1', run_of='sess-3'
    )
    assert history.backfill_instance_ids(launches) is True
    assert len(cloudtrail.requests) == 2


def test_reading_resumes_from_the_page_it_stopped_at():
    created = arrow.utcnow().shift(days=-10)
    pages = [[launch('user-a', created, 'i-0abc')], [], []]
    cloudtrail = FakeCloudTrail(pages)
    # the budget runs out after the first page
    clock = iter([0, 0, 0])
    launches = DesktopLaunches(
        FakeContext(cloudtrail), sleep=lambda _: None, clock=lambda: next(clock, 100)
    )
    # out of time after one page: not complete, nothing is matched yet
    assert launches.read(budget_seconds=1) is False
    assert len(cloudtrail.requests) == 1
    assert launches.read(budget_seconds=1000) is True
    assert [r.get('NextToken') for r in cloudtrail.requests] == [None, '1', '2']
    assert [launch['instance_id'] for launch in launches.launches] == ['i-0abc']


def test_a_launch_another_desktop_holds_is_never_borrowed():
    created = arrow.utcnow().shift(days=-30)
    # A's own launch is missing from CloudTrail; B's, of the same owner and type and
    # minutes apart, is held by B: a history row in one case, a live desktop in the other
    pages = [[launch('user-a', created.shift(minutes=4), 'i-0b')]]
    history, _, _, _ = backfill(
        [
            row('sess-a', 'user-a', created),
            row('sess-b', 'user-a', created.shift(minutes=4), instance_id='i-0b'),
        ],
        pages,
    )
    assert stored(history, 'sess-a')['instance_unknown'] is True
    history, _, _, _ = backfill(
        [row('sess-a', 'user-a', created)], pages, live=['i-0b']
    )
    assert stored(history, 'sess-a')['instance_unknown'] is True


def test_two_rows_that_match_one_launch_are_both_unknown():
    created = arrow.utcnow().shift(days=-30)
    history, _, _, _ = backfill(
        [
            row('sess-a', 'user-a', created),
            row('sess-b', 'user-a', created.shift(minutes=10)),
        ],
        [[launch('user-a', created.shift(minutes=5), 'i-0one')]],
    )
    for session_id in ('sess-a', 'sess-b'):
        assert stored(history, session_id)['instance_unknown'] is True
        assert 'instance_id' not in stored(history, session_id)


def test_desktops_deleted_since_runs_were_recorded_are_not_looked_up():
    now = arrow.utcnow()
    recording = now.shift(days=-3)
    run = dict(
        owner='user-a',
        idea_session_id='sess-x#run#1',
        run_of='sess-x',
        started_on=recording.int_timestamp * 1000,
    )
    # deleted before it was given an instance, after this release: nothing to find
    newer = row('sess-new', 'user-a', now.shift(days=-2))
    newer['deleted_on'] = now.shift(days=-1).int_timestamp * 1000
    flagged = row('sess-flagged', 'user-a', now.shift(days=-2), never_ran=True)
    history, cloudtrail, _, done = backfill([run, newer, flagged], [[]])
    assert done
    assert len(cloudtrail.requests) == 1
    assert history._table_obj.updates == []


def test_a_row_its_ttl_removed_is_not_recreated():
    created = arrow.utcnow().shift(days=-30)
    table = FakeHistoryTable([row('sess-1', 'user-a', created)])
    scan = table.scan
    # the scan saw it; it expired before the write
    table.scan = lambda **kwargs: (scan(), table.rows.clear())[0]
    _, _, _, done = backfill([], [[launch('user-a', created, 'i-0a')]], table=table)
    assert done
    assert table.rows == {} and table.updates == []


def test_the_budget_is_a_hard_limit():
    times = iter([0, 0, 0.9, 5, 5])
    cloudtrail = FakeCloudTrail([[]], throttle=5)
    sleeps = []
    launches = DesktopLaunches(
        FakeContext(cloudtrail), sleep=sleeps.append, clock=lambda: next(times)
    )
    assert launches.read(budget_seconds=1) is False
    # one call, a wait cut to what was left of the second, and no call past it
    assert len(cloudtrail.requests) == 1
    assert sleeps == [pytest.approx(0.1)]


def hidden_launch(at, instance_id):
    """CloudTrail hides tag specifications and tag sets on many RunInstances events"""
    return {
        'EventName': 'RunInstances',
        'EventTime': at.datetime,
        'CloudTrailEvent': json.dumps(
            {
                'eventTime': at.isoformat(),
                'requestParameters': {
                    'tagSpecificationSet': 'HIDDEN_DUE_TO_SECURITY_REASONS'
                },
                'responseElements': {
                    'instancesSet': {
                        'items': [
                            {
                                'instanceId': instance_id,
                                'instanceType': 'c7i.2xlarge',
                                'tagSet': 'HIDDEN_DUE_TO_SECURITY_REASONS',
                            }
                        ]
                    }
                },
            }
        ),
    }


def test_hidden_tags_are_skipped_not_fatal():
    created = arrow.utcnow().shift(days=-20)
    history, _, _, done = backfill(
        [row('sess-1', 'granetz', created)],
        [
            [
                hidden_launch(created, 'i-0hidden'),
                launch('granetz', created.shift(seconds=30), 'i-0visible'),
            ]
        ],
    )
    assert done
    assert stored(history, 'sess-1')['instance_id'] == 'i-0visible'


def test_a_resumed_slice_sends_the_same_window_with_its_token():
    created = arrow.utcnow().shift(days=-10)
    cloudtrail = FakeCloudTrail([[launch('user-a', created, 'i-0a')], [], []])
    clock = iter([0, 0, 0])
    launches = DesktopLaunches(
        FakeContext(cloudtrail), sleep=lambda _: None, clock=lambda: next(clock, 100)
    )
    assert launches.read(budget_seconds=1) is False
    assert launches.read(budget_seconds=1000) is True
    first, resumed = cloudtrail.requests[0], cloudtrail.requests[1]
    assert resumed['NextToken'] == '1'
    assert (resumed['StartTime'], resumed['EndTime']) == (
        first['StartTime'],
        first['EndTime'],
    )


class TimedCloudTrail:
    """events newest first, two to a page, with a token CloudTrail refuses once"""

    def __init__(self, events):
        self.events = events
        self.requests = []
        self.refused = False

    def lookup_events(self, **request):
        self.requests.append(dict(request))
        if 'NextToken' in request and not self.refused:
            self.refused = True
            raise ClientError(
                {'Error': {'Code': 'InvalidNextTokenException', 'Message': ''}},
                'LookupEvents',
            )
        end = arrow.get(request['EndTime'])
        window = [e for e in self.events if arrow.get(e['EventTime']) <= end]
        offset = int(request.get('NextToken', '0'))
        page = {'Events': window[offset : offset + 2]}
        if offset + 2 < len(window):
            page['NextToken'] = str(offset + 2)
        return page


def test_a_refused_token_resumes_from_the_oldest_event_read_and_completes():
    now = arrow.utcnow()
    events = [launch('user-a', now.shift(days=-day), f'i-{day}') for day in range(1, 6)]
    cloudtrail = TimedCloudTrail(events)
    launches = DesktopLaunches(FakeContext(cloudtrail), sleep=lambda _: None)

    assert launches.read() is True
    # refused once, then read on without a token from the second day back
    assert 'NextToken' not in cloudtrail.requests[2]
    assert arrow.get(cloudtrail.requests[2]['EndTime']) <= now.shift(days=-2, seconds=1)
    assert sorted(launch['instance_id'] for launch in launches.launches) == [
        f'i-{day}' for day in range(1, 6)
    ]


def test_a_token_refused_again_without_progress_fails_the_slice():
    cloudtrail = FakeCloudTrail([[]])
    cloudtrail.lookup_events = Mock(
        side_effect=ClientError(
            {'Error': {'Code': 'InvalidNextTokenException', 'Message': ''}},
            'LookupEvents',
        )
    )
    launches = DesktopLaunches(FakeContext(cloudtrail), sleep=lambda _: None)
    with pytest.raises(ClientError):
        launches.read()


def test_refused_token_without_progress_is_cleared_for_next_slice():
    cloudtrail = FakeCloudTrail([[]])
    cloudtrail.lookup_events = Mock(
        side_effect=[
            {'Events': [], 'NextToken': 'refused'},
            ClientError(
                {'Error': {'Code': 'InvalidNextTokenException'}}, 'LookupEvents'
            ),
            {'Events': []},
        ]
    )
    launches = DesktopLaunches(FakeContext(cloudtrail), sleep=lambda _: None)
    with pytest.raises(ClientError):
        launches.read()
    assert launches.read() is True
    assert 'NextToken' not in cloudtrail.lookup_events.call_args.kwargs


def test_backfill_does_not_scan_until_cloudtrail_is_complete():
    history = VirtualDesktopSessionHistoryDB.__new__(VirtualDesktopSessionHistoryDB)
    history.list_all = Mock(side_effect=AssertionError('premature scan'))
    launches = Mock()
    launches.read.return_value = False
    assert history.backfill_instance_ids(launches) is False
    history.list_all.assert_not_called()
