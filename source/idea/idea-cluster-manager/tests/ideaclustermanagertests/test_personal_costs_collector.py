"""Stored personal costs, tested without billing, inventory or network services."""

import copy
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import arrow
import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    GetMyCostsResult,
    GetMyCostsSummaryResult,
    MyCostsAmount,
    MyCostsMonth,
    MyCostsJobs,
    MyCostsDesktops,
    MyCostsAi,
)
from ideaclustermanager.app.api.my_costs_api import MyCostsAPI
from ideaclustermanager.app.costs.personal_costs_store import (
    PersonalCostsStore,
    StoredPersonalCostsService,
    SYSTEM,
)
from ideaclustermanager.app.costs.personal_costs_collector import (
    PersonalCostsCollector,
    DailyCostsCalculator,
    FACETS,
)


class Table:
    def __init__(self):
        self.rows = {}
        self.before_get = lambda key: None
        self.before_put = lambda item: None

    def put_item(self, Item, **kwargs):
        self.before_put(Item)
        key = (Item['subject'], Item['record'])
        if kwargs.get('ConditionExpression') and key in self.rows:
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem'
            )
        self.rows[key] = copy.deepcopy(Item)

    def get_item(self, Key, ConsistentRead):
        assert ConsistentRead is True
        self.before_get(Key)
        item = self.rows.get((Key['subject'], Key['record']))
        return {'Item': copy.deepcopy(item)} if item else {}

    def query(self, KeyConditionExpression, ConsistentRead):
        expression = KeyConditionExpression.get_expression()['values']
        subject = expression[0].get_expression()['values'][1]
        prefix = expression[1].get_expression()['values'][1]
        return {
            'Items': [
                copy.deepcopy(row)
                for (user, record), row in self.rows.items()
                if user == subject and record.startswith(prefix)
            ]
        }

    def delete_item(self, Key):
        self.rows.pop((Key['subject'], Key['record']), None)


def month(amount=1):
    return MyCostsMonth(
        start_date='2026-09-01',
        end_date='2026-09-02',
        total=amount * 5,
        incomplete=False,
        **{facet: MyCostsAmount(cost=amount, status='ready') for facet in FACETS},
    )


def costs(amount=1):
    return GetMyCostsResult(
        currency='USD',
        state='ready',
        current=month(amount),
        previous=month(amount + 1),
        refreshed_at='2026-09-02T12:00:00+00:00',
    )


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr('ideadatamodel.locale.get_currency_code', lambda: 'USD')
    config = SimpleNamespace(
        get_string=lambda key, default=None: default,
        get_bool=lambda *args: True,
        get_config=lambda key, default=None: default,
    )
    forbidden = Mock(
        side_effect=AssertionError('Sources must never be called by an API')
    )
    aws = SimpleNamespace(cost_explorer=forbidden, ec2=forbidden, pricing=forbidden)
    lock = Mock(spec=['acquire', 'release', 'assert_held'])
    context = SimpleNamespace(
        config=lambda: config,
        aws=lambda: aws,
        cluster_name=lambda: 'test',
        logger=lambda *args: logging.getLogger('personal-cost-tests'),
        is_leader=lambda: True,
        distributed_lock=lambda: lock,
    )
    store = PersonalCostsStore(context)
    store.table = Table()
    context.personal_costs_store = store
    return context, store, forbidden


def publish(store, amount=1, subject='user-a'):
    return store.publish(
        subject,
        costs(amount),
        GetMyCostsSummaryResult(username=subject, window='last_30_days'),
    )


def test_table_created_with_dedicated_composite_keys(setup):
    context, store, _ = setup
    util = Mock()
    util.dynamodb_check_table_exists.return_value = False
    context.aws_util = lambda: util
    context.aws().dynamodb_table = Mock()
    store.initialize()
    request = util.dynamodb_create_table.call_args.kwargs['create_table_request']
    assert request['TableName'].endswith('.cluster-manager.personal-costs')
    assert request['KeySchema'] == [
        {'AttributeName': 'subject', 'KeyType': 'HASH'},
        {'AttributeName': 'record', 'KeyType': 'RANGE'},
    ]
    assert util.dynamodb_create_table.call_args.kwargs['wait']


def test_failed_publish_cannot_switch_head(setup):
    _, store, _ = setup
    old = publish(store)

    def fail(item):
        if item['record'].endswith('summary:0'):
            raise RuntimeError('interrupted')

    store.table.before_put = fail
    with pytest.raises(RuntimeError):
        publish(store, 50)
    assert store.get('user-a', 'head') == old
    assert (
        StoredPersonalCostsService(store.context).get_costs('user-a').current.total == 5
    )


def test_reader_pins_head_while_writer_publishes_another_generation(setup):
    context, store, _ = setup
    old = publish(store)
    switched = []

    def switch(key):
        if key['record'].startswith('g:') and not switched:
            switched.append(True)
            publish(store, 100)

    store.table.before_get = switch
    result = StoredPersonalCostsService(context).get_costs('user-a')
    assert result.generation == old['generation']
    assert result.current.total == 5 and result.previous.total == 10
    assert store.get('user-a', 'head')['generation'] != old['generation']
    assert store.projection('user-a', old, 'costs')['current']['total'] == 5


def test_large_summary_chunks_are_bounded_and_reassembled(setup):
    _, store, _ = setup
    summary = GetMyCostsSummaryResult(username='x' * 500000)
    head = store.publish('user-a', costs(), summary)
    assert head['parts']['summary'] > 1
    assert all(
        len(row['payload'].encode()) < 300000 for row in store.table.rows.values()
    )
    assert store.projection('user-a', head, 'summary')['username'] == summary.username


@pytest.mark.parametrize(
    'namespace', ['GetCosts', 'GetSummary', 'GetCostTicker', 'Refresh']
)
def test_api_is_stored_only_and_scoped_to_caller(setup, namespace):
    context, store, forbidden = setup
    publish(store)
    invocation = SimpleNamespace(
        namespace='MyCosts.' + namespace,
        is_authorized_user=lambda: True,
        get_username=lambda: 'user-a',
        success=Mock(),
    )
    MyCostsAPI(context).invoke(invocation)
    result = invocation.success.call_args.args[0]
    if namespace == 'GetSummary':
        assert result.window == 'last_30_days'
    elif namespace == 'GetCostTicker':
        assert result.total == 5 and result.as_of == costs().refreshed_at
    else:
        assert result.current.total == 5 and result.refreshed_at == costs().refreshed_at
    forbidden.assert_not_called()
    assert StoredPersonalCostsService(context).get_costs('user-b').state == 'collecting'


def test_cold_eta_uses_next_run_and_measured_duration(setup, monkeypatch):
    context, store, _ = setup
    now = arrow.get('2026-09-02T12:00:00Z')
    monkeypatch.setattr(arrow, 'utcnow', lambda: now)
    assert store.collecting().expected_ready_at == now.shift(minutes=20).isoformat()
    store.put(
        SYSTEM,
        'state',
        {
            'next_run': now.shift(minutes=3).isoformat(),
            'last_duration': 120,
            'heartbeat': now.isoformat(),
        },
    )
    result = StoredPersonalCostsService(context).get_costs('user-a')
    assert result.state == 'collecting' and result.current is None
    assert result.expected_ready_at == now.shift(minutes=5).isoformat()


def test_overdue_eta_is_explicit(setup, monkeypatch):
    _, store, _ = setup
    now = arrow.get('2026-09-02T12:00:00Z')
    monkeypatch.setattr(arrow, 'utcnow', lambda: now)
    store.put(
        SYSTEM,
        'state',
        {'next_run': now.shift(hours=-1).isoformat(), 'last_duration': 60},
    )
    result = store.collecting()
    assert (
        result.collecting_delayed
        and result.expected_ready_at == now.shift(minutes=1).isoformat()
    )


def test_refresh_deduplicates_and_retains_values(setup):
    context, store, _ = setup
    publish(store)
    service = StoredPersonalCostsService(context)
    first = service.refresh('user-a')
    request = store.get('user-a', 'refresh')
    second = service.refresh('user-a')
    assert first.refresh_acknowledged and second.refresh_pending
    assert first.current.total == second.current.total == 5
    assert store.get('user-a', 'refresh') == request
    store.acknowledge('user-a', None)
    assert store.get('user-a', 'refresh') == request
    store.acknowledge('user-a', request)
    assert store.get('user-a', 'refresh') is None


def collector(setup, monkeypatch):
    context, store, _ = setup
    context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[
                SimpleNamespace(
                    username='user-a' if request.cursor is None else 'user-b'
                )
            ],
            paginator=SimpleNamespace(
                cursor='next' if request.cursor is None else None
            ),
        )
    )
    calculator = Mock()
    calculator.month.side_effect = lambda *args: month()
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.MyCostsService.get_summary',
        lambda self, username: GetMyCostsSummaryResult(
            username=username, window='last_30_days'
        ),
    )
    return PersonalCostsCollector(context, store, calculator), calculator


def test_collector_visits_every_page_and_both_calendar_months(setup, monkeypatch):
    worker, calculator = collector(setup, monkeypatch)
    now = arrow.get('2026-09-02T12:00:00Z')
    monkeypatch.setattr(arrow, 'utcnow', lambda: now)
    worker.check()
    assert [
        (c.args[0], c.args[1].format('YYYY-MM'), c.args[2].isoformat())
        for c in calculator.month.call_args_list
    ] == [
        (u, m, end)
        for u in ('user-a', 'user-b')
        for m, end in [
            ('2026-09', now.isoformat()),
            ('2026-08', now.floor('month').isoformat()),
        ]
    ]
    assert all(setup[1].get(u, 'head') for u in ('user-a', 'user-b'))
    worker.check()
    assert calculator.month.call_count == 4
    setup[1].request_refresh('user-b')
    worker.check()
    assert calculator.month.call_count == 6
    assert setup[1].get('user-b', 'refresh') is None


def test_nonleader_does_no_work_and_lost_leader_cannot_publish(setup, monkeypatch):
    worker, calculator = collector(setup, monkeypatch)
    context, store, _ = setup
    context.is_leader = lambda: False
    worker.check()
    calculator.begin.assert_not_called()
    context.is_leader = lambda: True
    calculator.month.side_effect = lambda *args: (
        setattr(context, 'is_leader', lambda: False) or month()
    )
    worker.check()
    assert store.get('user-a', 'head') is None


def test_failed_refresh_keeps_request_and_old_generation(setup, monkeypatch):
    worker, calculator = collector(setup, monkeypatch)
    _, store, _ = setup
    old = publish(store)
    request = store.request_refresh('user-a')
    calculator.month.side_effect = RuntimeError('source failed')
    worker.check()
    assert store.get('user-a', 'head') == old
    assert store.get('user-a', 'refresh') == request


@pytest.mark.parametrize(
    'start,end,length',
    [('2024-02-01', '2024-03-01', 29), ('2026-09-01', '2026-09-03T12:00:00Z', 3)],
)
def test_daily_shape_missing_zero_negative_and_reconciliation(
    setup, start, end, length
):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)

    def day(user, lower, upper):
        value = None if lower.day == 2 else (-1 if lower.day == 3 else 0)
        return {f: calc._line(value, value is None) for f in FACETS}

    calc.day = day
    result = calc.month('user-a', arrow.get(start), arrow.get(end))
    for facet in FACETS:
        line = getattr(result, facet)
        assert len(line.daily) == length
        assert [p.day for p in line.daily] == list(range(1, length + 1))
        assert line.daily[0].amount == 0 and line.daily[1].amount is None
        assert (
            line.cost == -1
            and line.coverage.missing_days == 1
            and line.status == 'partial'
        )
    assert result.total == -5 and result.incomplete
    calc._workers.shutdown()


def test_real_day_reuses_compute_rules_and_never_backfills_inventory(
    setup, monkeypatch
):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)
    monkeypatch.setattr(
        calc,
        '_compute_jobs',
        lambda *args: MyCostsJobs(cost=3, job_count=2, unpriced_jobs=1),
    )
    monkeypatch.setattr(calc, '_desktops', lambda *args: MyCostsDesktops(cost=2))
    monkeypatch.setattr(calc, '_ai', lambda *args: MyCostsAi(cost=1))
    lines = calc.day('user-a', arrow.get('2026-08-01'), arrow.get('2026-08-02'))
    assert lines['jobs'].cost == 3 and lines['jobs'].status == 'partial'
    assert lines['desktops'].cost == 2 and lines['ai'].cost == 1
    assert lines['desktop_disks'].cost is None
    assert lines['shared_storage'].cost == 0  # confirmed no configured file systems
    calc._workers.shutdown()


def test_shares_require_complete_same_filesystem_all_users_and_dated_evidence(setup):
    context, store, _ = setup
    now = arrow.get('2026-09-02T12:00:00Z')
    snapshot = dict(
        measured_at=now.timestamp(),
        users={'user-a': 10, 'user-b': 30},
        complete=True,
        filesystem_id='fs-test',
        total_bytes=100,
    )
    context.storage_metrics = SimpleNamespace(
        usage_by_filesystem=lambda: {'fs-test': snapshot}
    )
    calc = DailyCostsCalculator(context, store)
    for invalid in (
        {'complete': False},
        {'filesystem_id': 'fs-other'},
        {'users': {'user-a': 10}},
        {'total_bytes': 0},
        {'measured_at': now.shift(days=-2).timestamp()},
    ):
        original = snapshot.copy()
        snapshot.update(invalid)
        calc.capture_storage(['user-a', 'user-b'], now)
        assert store.get(SYSTEM, 'share:2026-09-02:fs-test') is None
        snapshot.clear()
        snapshot.update(original)
    calc.capture_storage(['user-a', 'user-b'], now)
    assert store.get(SYSTEM, 'share:2026-09-02:fs-test')['users']['user-a'] == 10
    assert store.get(SYSTEM, 'share:2026-08-02:fs-test') is None
    calc._workers.shutdown()


def test_null_daily_amount_survives_api_serialization():
    from ideadatamodel import MyCostsDaily

    point = MyCostsDaily(date='2026-09-02', day=2, amount=None, status='unavailable')
    assert point.model_dump(exclude_none=True)['amount'] is None


def test_refresh_source_failure_retains_known_facet_with_source_date():
    old = month()
    old.jobs.source_as_of = '2026-09-02T10:00:00Z'
    candidate = month()
    candidate.jobs = MyCostsAmount(status='unavailable')
    assert PersonalCostsCollector.retain_known(candidate, old)
    assert candidate.jobs.cost == 1 and candidate.jobs.status == 'partial'
    assert candidate.jobs.source_as_of == old.jobs.source_as_of
    assert candidate.total == 5 and candidate.incomplete


@pytest.mark.parametrize('period', ['wtd', 'qtd', 'ytd'])
def test_nonmonthly_tickers_read_only_the_stored_projection(setup, period):
    context, store, forbidden = setup
    context.config().get_string = lambda key, default=None: (
        period if key.endswith('.period') else default
    )
    store.publish(
        'user-a',
        costs(),
        GetMyCostsSummaryResult(),
        {period: {'total': 11, 'incomplete': True}},
    )
    result = StoredPersonalCostsService(context).get_ticker('user-a')
    assert result.total == 11 and result.period == period.upper() and result.incomplete
    forbidden.assert_not_called()


def test_dated_disk_intervals_keep_deleted_disks_and_leave_gaps_unknown(
    setup, monkeypatch
):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)
    monkeypatch.setattr(calc, '_volume_price', lambda volume_type: 0.08)
    start = arrow.get('2026-09-02T00:00:00Z')
    volume = {
        'VolumeId': 'vol-test',
        'VolumeType': 'gp3',
        'Size': 100,
        'CreateTime': start.isoformat(),
        'Attachments': [],
    }
    store.put_source(
        'user-a',
        'inventory:2026-09-02:1',
        {'as_of': start.isoformat(), 'instances': {}, 'volumes': [volume]},
    )
    store.put_source(
        'user-a',
        'inventory:2026-09-02:2',
        {'as_of': start.shift(minutes=15).isoformat(), 'instances': {}, 'volumes': []},
    )
    result = calc.disk_day('user-a', start, start.shift(days=1))
    assert result.cost == pytest.approx(round(100 * 0.08 / 30 / 24 / 4, 4))
    assert result.status == 'partial'
    assert calc.disk_day('user-a', start.shift(days=-1), start).cost is None
    calc._workers.shutdown()


def test_source_pages_are_bounded_and_atomic(setup):
    _, store, _ = setup
    source = {'volumes': [{'name': 'x' * 10000} for _ in range(50)]}
    store.put_source('user-a', 'inventory:2026-09-02:1', source)
    record = store.get('user-a', 'inventory:2026-09-02:1')
    assert len(record['source_chunks']) > 1
    assert store.resolve_source('user-a', record) == source
    assert all(
        len(row['payload'].encode()) < 300000 for row in store.table.rows.values()
    )


def test_billing_reads_daily_pages_once_and_selects_actual_dates(setup, monkeypatch):
    context, store, _ = setup
    monkeypatch.setattr(arrow, 'utcnow', lambda: arrow.get('2026-09-03T12:00:00Z'))

    def billed(day, amount):
        return {
            'TimePeriod': {'Start': day},
            'Groups': [
                {
                    'Keys': ['SERVICE$AI'],
                    'Metrics': {
                        'UnblendedCost': {'Amount': str(amount), 'Unit': 'USD'}
                    },
                }
            ],
        }

    client = Mock()
    client.get_cost_and_usage.side_effect = [
        {'ResultsByTime': [billed('2026-09-01', 3)], 'NextPageToken': 'next'},
        {'ResultsByTime': [billed('2026-09-02', 7)]},
    ]
    context.aws().cost_explorer = lambda: client
    context.aws().aws_partition = lambda: 'aws'
    calc = DailyCostsCalculator(context, store)
    groups = [{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
    assert calc._billing(
        arrow.get('2026-09-01'), arrow.get('2026-09-02'), [], groups
    ) == {('AI',): 3}
    assert calc._billing(
        arrow.get('2026-09-02'), arrow.get('2026-09-03'), [], groups
    ) == {('AI',): 7}
    assert (
        calc._billing(arrow.get('2026-09-03'), arrow.get('2026-09-04'), [], groups)
        is None
    )
    assert client.get_cost_and_usage.call_count == 2
    assert client.get_cost_and_usage.call_args.kwargs['Granularity'] == 'DAILY'
    calc._workers.shutdown()


def test_shared_storage_prices_only_the_dated_complete_share(setup, monkeypatch):
    context, store, _ = setup
    context.config().get_config = lambda *args, **kwargs: {
        'data': {
            'provider': 'efs',
            'efs': {'file_system_id': 'fs-test'},
            'costs': {'name_tag': 'storage'},
        }
    }
    calc = DailyCostsCalculator(context, store)
    monkeypatch.setattr(calc, '_billing', lambda *args: {('storage',): 20})
    start = arrow.get('2026-09-02')
    store.put_source(
        SYSTEM,
        'share:2026-09-02:fs-test',
        {
            'users': {'user-a': 10, 'user-b': 30},
            'total_bytes': 100,
            'measured_at': start.timestamp(),
        },
    )
    result = calc.storage_day('user-a', start, start.shift(days=1))
    assert (
        result.cost == 2 and result.status == 'estimated_share'
    )  # system space remains unassigned
    assert (
        calc.storage_day(
            'user-a', start.shift(months=-1), start.shift(months=-1, days=1)
        ).cost
        is None
    )
    calc._workers.shutdown()


def test_ai_spend_without_token_denominator_is_not_a_free_day(setup, monkeypatch):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)
    monkeypatch.setattr(calc, '_usage_rows', lambda *args: [])
    monkeypatch.setattr(calc, '_billing', lambda *args: {('Amazon Bedrock',): 5})
    result = calc._ai_project(
        'user-a',
        SimpleNamespace(project_id='project-a', name='project-a'),
        '2026-09-02',
        '2026-09-02',
    )
    assert result.cost_unavailable
    calc._workers.shutdown()


def test_minute_check_honours_a_request_arriving_during_collection(setup, monkeypatch):
    worker, calculator = collector(setup, monkeypatch)
    _, store, _ = setup
    requested = []

    def calculate(*args):
        if not requested:
            requested.append(True)
            store.request_refresh('user-a')
            worker.check_requests()
        return month()

    calculator.month.side_effect = calculate
    worker.check()
    assert (
        calculator.month.call_count == 6
    )  # two users, then the request's extra generation
    assert store.get('user-a', 'refresh') is None
    assert store.get(SYSTEM, 'heartbeat')


def test_minute_checker_is_leader_only(setup, monkeypatch):
    worker, _ = collector(setup, monkeypatch)
    context, store, _ = setup
    context.is_leader = lambda: False
    worker.check_requests()
    assert not worker._refresh_users and store.get(SYSTEM, 'heartbeat') is None


def test_partial_refresh_failure_keeps_previously_known_daily_amounts():
    from ideadatamodel import MyCostsDaily

    old = month()
    old.jobs = MyCostsAmount(
        cost=5,
        source_as_of='2026-09-02T10:00:00Z',
        daily=[
            MyCostsDaily(date='2026-09-01', day=1, amount=3, status='ready'),
            MyCostsDaily(date='2026-09-02', day=2, amount=2, status='ready'),
        ],
    )
    candidate = month()
    candidate.jobs = MyCostsAmount(
        cost=3,
        source_as_of='2026-09-02T11:00:00Z',
        daily=[
            MyCostsDaily(date='2026-09-01', day=1, amount=3, status='ready'),
            MyCostsDaily(date='2026-09-02', day=2, amount=None, status='unavailable'),
        ],
    )
    assert PersonalCostsCollector.retain_known(candidate, old)
    assert candidate.jobs.cost == 5 and candidate.jobs.daily[1].status == 'partial'
    assert candidate.jobs.source_as_of == old.jobs.source_as_of


@pytest.mark.parametrize(
    'partial,root_bytes,qualified',
    [(False, 100, True), (True, 100, False), (False, 0, False), (False, 20, False)],
)
def test_home_fallback_requires_the_complete_filesystem_denominator(
    setup, monkeypatch, partial, root_bytes, qualified
):
    context, store, _ = setup
    now = arrow.get('2026-09-02T12:00:00Z')
    context.config().get_config = lambda *args, **kwargs: {
        'data': {
            'provider': 'efs',
            'efs': {'file_system_id': 'fs-test'},
            'mount_dir': '/data',
        }
    }
    calc = DailyCostsCalculator(context, store)
    monkeypatch.setattr(
        calc,
        '_home_usage',
        lambda: {'measured_at': now.timestamp(), 'users': {'user-a': 10, 'user-b': 30}},
    )
    monkeypatch.setattr(calc, '_homes_on_filesystem', lambda *args: True)
    monkeypatch.setattr(calc, '_filesystem_root', lambda *args: True)
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.walk_home',
        lambda *args: {
            'state': 'ready',
            'partial': partial,
            'measured_at': now.timestamp(),
            'total': {'bytes': root_bytes},
        },
    )
    calc.capture_storage(['user-a', 'user-b'], now)
    snapshot = store.get(SYSTEM, 'share:2026-09-02:fs-test')
    assert bool(snapshot) is qualified
    if qualified:
        assert snapshot['users']['user-a'] / snapshot['total_bytes'] == 0.1
        assert snapshot['total_bytes'] > sum(snapshot['users'].values())
    calc._workers.shutdown()


def test_source_records_encode_boto_datetimes(setup):
    from datetime import datetime, timezone

    _, store, _ = setup
    attached = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    store.put_source(
        'user-a',
        'inventory:2026-09-01:1',
        {'volumes': [{'VolumeId': 'vol-1', 'Attachments': [{'AttachTime': attached}]}]},
    )
    stored = store.get('user-a', 'inventory:2026-09-01:1')
    assert stored['volumes'][0]['Attachments'][0]['AttachTime'] == attached.isoformat()


def test_storage_day_tolerates_entries_without_a_costs_block(setup, monkeypatch):
    from pyhocon import ConfigFactory

    context, store, _ = setup
    tree = ConfigFactory.parse_string(
        'data { provider = fsx_netapp_ontap, mount_dir = /data, fsx_netapp_ontap { file_system_id = fs-1 } }'
    )
    context.config().get_config = (
        lambda key, default=None: tree if key == 'shared-storage' else default
    )
    calc = DailyCostsCalculator(context, store)
    seen = []
    monkeypatch.setattr(
        calc, '_billing', lambda start, end, filters, groups: seen.append(filters) or {}
    )
    line = calc.storage_day('user-a', arrow.get('2026-09-02'), arrow.get('2026-09-03'))
    assert 'costs' not in (line.reason or '')
    assert seen, 'billing must be reached once the entry parses'
    calc._workers.shutdown()


def test_facet_failures_log_once_per_run_and_keep_authored_reasons(
    setup, monkeypatch, caplog
):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)

    def boom(*args):
        raise RuntimeError('billing down')

    def no_rows(*args):
        raise ValueError('AI spend has no token denominator')

    for name in ('_compute_jobs', '_desktops', 'disk_day', 'storage_day'):
        monkeypatch.setattr(calc, name, boom)
    monkeypatch.setattr(calc, '_ai', no_rows)
    calc.begin()
    with caplog.at_level(logging.WARNING, logger='personal-cost-tests'):
        first = calc.day('user-a', arrow.get('2026-09-02'), arrow.get('2026-09-03'))
        calc.day('user-a', arrow.get('2026-09-03'), arrow.get('2026-09-04'))
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(errors) == 4 and all(r.exc_info for r in errors)
    assert len(warnings) == 1 and warnings[0].exc_info is None
    assert first['ai'].reason == 'AI spend has no token denominator'
    assert first['jobs'].reason == 'Source unavailable.'
    calc._workers.shutdown()
