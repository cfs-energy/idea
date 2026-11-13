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
    MyCostsStorageShare,
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
    SETTLE_DAYS,
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


def test_a_run_that_outlasts_the_one_before_is_not_overdue(setup, monkeypatch):
    # idea-dev: a run took 27 s, the next, with seven more people, 37 s. Seconds 27 to 37 read
    # as overdue to everyone with no costs yet, which is who the seven were.
    _, store, _ = setup
    now = arrow.get('2026-11-09T22:33:33Z')
    monkeypatch.setattr(arrow, 'utcnow', lambda: now)
    store.put(SYSTEM, 'heartbeat', {'as_of': now.isoformat()})

    def collecting(**state):
        store.put(SYSTEM, 'state', dict(state, last_duration=27))
        return store.collecting()

    result = collecting(run_started=now.shift(seconds=-33).isoformat())
    assert not result.collecting_delayed and result.collecting_reason is None
    assert arrow.get(result.expected_ready_at) > now
    # due, and the collector looks once a minute: not started yet is not overdue either
    assert not collecting(
        next_run=now.shift(seconds=-50).isoformat()
    ).collecting_delayed
    # control: a minute past the estimate, and past twice the last run, it is
    assert collecting(
        run_started=now.shift(seconds=-(27 + 61)).isoformat()
    ).collecting_delayed
    store.put(
        SYSTEM,
        'state',
        {'run_started': now.shift(minutes=-39).isoformat(), 'last_duration': 1200},
    )
    assert not store.collecting().collecting_delayed, 'a long run gets as long again'
    store.put(
        SYSTEM,
        'state',
        {'run_started': now.shift(minutes=-41).isoformat(), 'last_duration': 1200},
    )
    assert store.collecting().collecting_delayed


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

    def day(user, lower, upper, storage=None):
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


def test_shares_require_complete_same_filesystem_and_dated_evidence(setup):
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


def test_default_quota_rule_persists_zero_for_absent_users(setup):
    context, store, _ = setup
    now = arrow.get('2026-09-02T12:00:00Z')
    snapshot = dict(
        measured_at=now.timestamp(),
        users={'user-a': 10},
        complete=True,
        zero_when_absent=True,
        filesystem_id='fs-test',
        total_bytes=10,
    )
    context.storage_metrics = SimpleNamespace(
        usage_by_filesystem=lambda: {'fs-test': snapshot}
    )
    calc = DailyCostsCalculator(context, store)
    calc.capture_storage(['user-a', 'USER-B'], now)
    stored = store.get(SYSTEM, 'share:2026-09-02:fs-test')
    assert stored['users'] == {'user-a': 10, 'user-b': 0}
    assert snapshot['users'] == {'user-a': 10}
    calc._workers.shutdown()


@pytest.mark.parametrize('exchange_rate,expected', [(0.9, 27.0), (None, None)])
def test_storage_day_uses_cluster_currency(setup, monkeypatch, exchange_rate, expected):
    context, store, _ = setup
    monkeypatch.setattr('ideadatamodel.locale.get_currency_code', lambda: 'EUR')
    context.config().get_float = lambda key, default=None: exchange_rate
    context.config().get_config = lambda *args, **kwargs: {
        'data': {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-test'},
        }
    }
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.daily_storage_rate',
        lambda *args, **kwargs: 120,
    )
    start = arrow.get('2026-09-02')
    store.put_source(
        SYSTEM,
        'share:2026-09-02:fs-test',
        {
            'users': {'user-a': 100, 'user-b': 300},
            'total_bytes': 400,
            'complete': True,
            'measured_at': start.timestamp(),
        },
    )
    calc = DailyCostsCalculator(context, store)
    try:
        result = calc.storage_day('user-a', start, start.shift(days=1))
        assert result.cost == expected
        assert result.status == (
            'unavailable' if expected is None else 'estimated_share'
        )
    finally:
        calc._workers.shutdown()


def test_shared_storage_prices_only_the_dated_complete_share(setup, monkeypatch):
    context, store, _ = setup
    context.config().get_config = lambda *args, **kwargs: {
        'data': {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-test'},
        }
    }
    calc = DailyCostsCalculator(context, store)
    rate = Mock(return_value=20)
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.daily_storage_rate', rate
    )
    start = arrow.get('2026-09-02')
    store.put_source(
        SYSTEM,
        'share:2026-09-02:fs-test',
        {
            'users': {'user-a': 10, 'user-b': 30},
            'total_bytes': 100,
            'capacity_pool_bytes': 9000,
            'filesystem_id': 'fs-test',
            'complete': True,
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
    assert result.source_as_of == start.isoformat()
    assert rate.call_args_list[0].kwargs['capacity_pool_bytes'] == 9000
    rate.return_value = None
    unavailable = calc.storage_day('user-a', start, start.shift(days=1))
    assert unavailable.cost is None and unavailable.status == 'unavailable'
    assert unavailable.coverage.missing_prices == 1
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


def test_attribution_data_conditions_log_once_per_run(setup, monkeypatch, caplog):
    # a missing token denominator is one condition, not one warning per user, project and day
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)
    monkeypatch.setattr(calc, '_usage_rows', lambda *args: [])
    monkeypatch.setattr(calc, '_billing', lambda *args: {('Amazon Bedrock',): 5})
    calc.begin()
    with caplog.at_level(logging.DEBUG, logger='personal-cost-tests'):
        for day in ('2026-09-02', '2026-09-03'):
            for project in ('project-a', 'project-b'):
                result = calc._ai_project(
                    'user-a',
                    SimpleNamespace(project_id=project, name=project),
                    day,
                    day,
                )
                assert result.cost_unavailable
    attribution = [
        r
        for r in caplog.records
        if 'Daily project attribution unavailable' in r.message
    ]
    assert [r.levelno for r in attribution] == [logging.WARNING] + [logging.DEBUG] * 3
    calc.begin()  # a new run says it again, once
    with caplog.at_level(logging.WARNING, logger='personal-cost-tests'):
        calc._ai_project(
            'user-a',
            SimpleNamespace(project_id='project-a', name='project-a'),
            '2026-09-02',
            '2026-09-02',
        )
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 2
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
    context.config().get_config = lambda key, default=None: (
        tree if key == 'shared-storage' else default
    )
    calc = DailyCostsCalculator(context, store)
    seen = []
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.daily_storage_rate',
        lambda *args, **kwargs: seen.append(args) or None,
    )
    line = calc.storage_day('user-a', arrow.get('2026-09-02'), arrow.get('2026-09-03'))
    assert 'costs' not in (line.reason or '')
    assert seen, 'pricing must be reached once the entry parses'
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


@pytest.mark.parametrize('rate', [20, None])
def test_stored_month_carries_latest_storage_shares(setup, monkeypatch, rate):
    context, store, _ = setup
    context.config().get_config = lambda *args, **kwargs: {
        'data': {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-test'},
        },
        'shared': {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-test'},
        },
        'archive': {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-other'},
        },
    }
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.daily_storage_rate',
        lambda *args, **kwargs: rate,
    )
    start = arrow.get('2026-09-01')
    for day, used in [(0, 2), (1, 3)]:
        observed = start.shift(days=day)
        for fs_id in ('fs-test', 'fs-other'):
            if fs_id == 'fs-other' and day == 1:
                continue
            store.put_source(
                SYSTEM,
                f'share:{observed.format("YYYY-MM-DD")}:{fs_id}',
                {
                    'users': {'user-a': used * 1024**3, 'user-b': 0},
                    'total_bytes': 10 * 1024**3,
                    'complete': True,
                    'measured_at': observed.timestamp(),
                },
            )
    calc = DailyCostsCalculator(context, store)
    calc._compute_jobs = lambda *args: MyCostsJobs(cost=0)
    calc._desktops = lambda *args: MyCostsDesktops(cost=0)
    calc.disk_day = lambda *args: calc._line(0, False)
    calc._ai = lambda *args: MyCostsAi(cost=0)
    try:
        result = calc.month('user-a', start, start.shift(days=3))
        store.publish(
            'user-a',
            GetMyCostsResult(currency='USD', state='ready', current=result),
            GetMyCostsSummaryResult(username='user-a'),
        )
        stored = StoredPersonalCostsService(context).get_costs('user-a').current
        assert len(stored.storage) == 2
        assert {item.filesystem for item in stored.storage} == {
            'data, shared',
            'archive',
        }
        for item in stored.storage:
            latest_day = 1 if item.filesystem == 'data, shared' else 0
            assert item.used_bytes == (2 + latest_day) * 1024**3
            assert item.share == pytest.approx((2 + latest_day) / 10)
            assert item.measured_at == start.shift(days=latest_day).timestamp()
            assert item.cost == (
                (10 if latest_day else 4) if rate is not None else None
            )
            assert item.status == ('partial' if rate is not None else 'unavailable')
        assert stored.shared_storage.cost == (14 if rate is not None else None)
        other = calc.month('user-b', start, start.shift(days=2))
        assert all(item.used_bytes == 0 and item.share == 0 for item in other.storage)
        assert calc.month('user-a', start.shift(months=-1), start).storage == []
    finally:
        calc._workers.shutdown()


@pytest.mark.parametrize('captured_day', ['2026-08-19', '2026-09-19'])
@pytest.mark.parametrize('changed_field', ['capacity', 'throughput'])
def test_captured_storage_rate_survives_configuration_change(
    setup, monkeypatch, captured_day, changed_field
):
    context, store, _ = setup
    context.config().get_config = lambda *args, **kwargs: {
        'home': {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-test'},
        }
    }
    filesystem = {
        'StorageCapacity': 100,
        'OntapConfiguration': {
            'DeploymentType': 'SINGLE_AZ_1',
            'ThroughputCapacity': 50,
        },
    }
    context.aws().aws_partition = lambda: 'aws'
    context.aws().aws_region = lambda: 'us-east-2'
    fsx = Mock()
    fsx.describe_file_systems.return_value = {'FileSystems': [filesystem]}
    context.aws().fsx = lambda: fsx
    context.aws().pricing = lambda: None
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.storage_rates.lookup_price',
        lambda *args: (1, None, None),
    )
    start = arrow.get(captured_day)
    snapshot = dict(
        measured_at=start.shift(hours=12).timestamp(),
        users={'user-a': 25},
        complete=True,
        filesystem_id='fs-test',
        total_bytes=100,
    )
    context.storage_metrics = SimpleNamespace(
        usage_by_filesystem=lambda: {'fs-test': snapshot}
    )
    calc = DailyCostsCalculator(context, store)
    try:
        calc.capture_storage(['user-a'], start.shift(hours=13))
        before = calc.storage_day('user-a', start, start.shift(days=1))
        if changed_field == 'capacity':
            filesystem['StorageCapacity'] *= 2
        else:
            filesystem['OntapConfiguration']['ThroughputCapacity'] *= 2
        calc.begin()
        calc.capture_storage(['user-a'], start.shift(days=1, hours=1))
        after = calc.storage_day('user-a', start, start.shift(days=1))
        assert after.cost == before.cost
        record = store.get(SYSTEM, f'share:{captured_day}:fs-test')
        assert record['daily_rate'] == pytest.approx(
            150 / (31 if start.month == 8 else 30)
        )
        fsx.describe_file_systems.assert_called_once()
    finally:
        calc._workers.shutdown()


def test_complete_snapshot_keeps_users_without_default_quota(setup, monkeypatch):
    context, store, _ = setup
    start = arrow.get('2026-09-02')
    context.config().get_config = lambda *args, **kwargs: {
        name: {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': fs_id},
        }
        for name, fs_id in [('home', 'fs-test'), ('shared', 'fs-other')]
    }
    snapshots = {
        fs_id: dict(
            measured_at=start.timestamp(),
            users=users,
            complete=True,
            zero_when_absent=False,
            filesystem_id=fs_id,
            total_bytes=100,
        )
        for fs_id, users in [
            ('fs-test', {'user-a': 10}),
            ('fs-other', {'user-a': 10, 'user-b': 20}),
        ]
    }
    context.storage_metrics = SimpleNamespace(usage_by_filesystem=lambda: snapshots)
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.daily_storage_rate',
        lambda *args, **kwargs: 20,
    )
    calc = DailyCostsCalculator(context, store)
    try:
        calc.capture_storage(['user-a', 'user-b'], start.shift(hours=1))
        record = store.get(SYSTEM, 'share:2026-09-02:fs-test')
        assert record is not None and 'user-b' not in record['users']
        assert calc.storage_day('user-a', start, start.shift(days=1)).cost == 4
        storage = {}
        result = calc.storage_day('user-b', start, start.shift(days=1), storage)
        assert result.cost == 4 and result.status == 'partial'
        assert storage['fs-test'][0].status == 'no_usage_data'
        assert storage['fs-test'][0].used_bytes is None
    finally:
        calc._workers.shutdown()


@pytest.mark.parametrize('with_ontap', [True, False])
def test_storage_excludes_providers_without_user_shares(setup, monkeypatch, with_ontap):
    context, store, _ = setup
    entries = {
        name: {'provider': provider, provider: {'file_system_id': 'fs-' + name}}
        for name, provider in [
            ('apps', 'efs'),
            ('scratch', 'fsx_lustre'),
            ('shared', 'fsx_windows_file_server'),
        ]
    }
    if with_ontap:
        entries['home'] = {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-test'},
        }
    context.config().get_config = lambda *args, **kwargs: entries
    start = arrow.get('2026-09-02')
    store.put_source(
        SYSTEM,
        'share:2026-09-02:fs-test',
        {
            'users': {'user-a': 10},
            'total_bytes': 100,
            'complete': True,
            'measured_at': start.timestamp(),
        },
    )
    rate = Mock(return_value=20)
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.daily_storage_rate', rate
    )
    calc = DailyCostsCalculator(context, store)
    try:
        storage = {}
        result = calc.storage_day('user-a', start, start.shift(days=1), storage)
        assert result.status == ('estimated_share' if with_ontap else 'ready')
        assert result.cost == (2 if with_ontap else 0)
        assert result.coverage.missing_prices == 0
        assert set(storage) == ({'fs-test'} if with_ontap else set())
        assert rate.call_count == int(with_ontap)
        assert 'excluded' in result.reason.lower()
        assert all(name in result.reason for name in ('apps', 'scratch', 'shared'))
    finally:
        calc._workers.shutdown()


@pytest.mark.parametrize(
    'stored_rate,expected,calls', [(0, 0, 0), (8, 2, 0), (None, 5, 1)]
)
def test_storage_rate_fallback_preserves_stored_zero(
    setup, monkeypatch, stored_rate, expected, calls
):
    context, store, _ = setup
    context.config().get_config = lambda *args, **kwargs: {
        'home': {
            'provider': 'fsx_netapp_ontap',
            'fsx_netapp_ontap': {'file_system_id': 'fs-test'},
        }
    }
    start = arrow.get('2026-09-02')
    store.put_source(
        SYSTEM,
        'share:2026-09-02:fs-test',
        {
            'users': {'user-a': 25},
            'total_bytes': 100,
            'daily_rate': stored_rate,
            'complete': True,
            'measured_at': start.timestamp(),
        },
    )
    rate = Mock(return_value=20)
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.daily_storage_rate', rate
    )
    calc = DailyCostsCalculator(context, store)
    try:
        result = calc.storage_day('user-a', start, start.shift(days=1))
        assert result.cost == expected and result.status == 'estimated_share'
        assert rate.call_count == calls
        storage = {}
        absent = calc.storage_day('user-b', start, start.shift(days=1), storage)
        assert absent.cost is None and absent.status == 'partial'
        assert storage['fs-test'][0].status == 'no_usage_data'
    finally:
        calc._workers.shutdown()


def settling(context, store, value=None):
    """A calculator whose day sources are a function of (user, day, observed at)."""
    calc = DailyCostsCalculator(context, store)
    calls = []
    value = value or (lambda user, lower, now: float(lower.day))

    def day(user, lower, upper, storage=None):
        calls.append((user, lower.format('YYYY-MM-DD')))
        amount = value(user, lower, arrow.utcnow())
        if storage is not None:
            storage.setdefault('fs-1', []).append(
                MyCostsStorageShare(
                    filesystem='data',
                    used_bytes=lower.day,
                    share=0.25,
                    measured_at=lower.shift(hours=2).float_timestamp,
                    cost=amount,
                    status='estimated_share',
                    note='n',
                )
            )
        lines = {
            facet: calc._line(
                amount + index / 8,
                missing=lower.day % 11 == 0 and facet == 'ai',
                partial=lower.day % 7 == 0,
                reason=f'r{lower.day % 3}',
                missing_prices=int(lower.day % 5 == 0),
            )
            for index, facet in enumerate(FACETS)
        }
        lines['desktop_disks'].source_as_of = lower.shift(hours=1).isoformat()
        return lines

    calc.day = day
    return calc, calls


def empty_store(context):
    store = PersonalCostsStore(context)
    store.table = Table()
    return store


def at(monkeypatch, when):
    monkeypatch.setattr(arrow, 'utcnow', lambda: arrow.get(when))


def both_months(calc, user='user-a'):
    now = arrow.utcnow()
    start = now.floor('month')
    return (
        calc.month(user, start, now),
        calc.month(user, start.shift(months=-1), start),
    )


def test_upgrade_computes_once_then_only_the_open_window(setup, monkeypatch):
    context, store, _ = setup
    calc, calls = settling(context, store)
    at(monkeypatch, '2026-11-15T12:00:00Z')
    both_months(calc)
    # no stored days (an upgraded cluster): one full pass, as before
    assert len(calls) == 15 + 31
    calls.clear()
    at(monkeypatch, '2026-11-15T12:15:00Z')
    both_months(calc)
    # a settled day is not computed again; today and the open days are
    assert calls == [
        ('user-a', f'2026-11-{day}') for day in range(15 - SETTLE_DAYS, 16)
    ]
    calc._workers.shutdown()


def test_open_days_pick_up_revised_sources(setup, monkeypatch):
    context, store, _ = setup
    revision = {'value': 0.0}
    calc, _ = settling(
        context, store, lambda user, lower, now: lower.day + revision['value']
    )
    at(monkeypatch, '2026-11-15T12:00:00Z')
    first, _ = both_months(calc)
    revision['value'] = 100.0
    second, _ = both_months(calc)
    amounts = {point.date: point.amount for point in second.jobs.daily}
    assert amounts['2026-11-11'] == 11
    assert [amounts[f'2026-11-{day}'] for day in (12, 13, 14, 15)] == [
        112,
        113,
        114,
        115,
    ]
    assert second.jobs.cost == first.jobs.cost + 400
    calc._workers.shutdown()


def test_stored_days_reproduce_a_full_recompute_exactly(setup, monkeypatch):
    context, store, _ = setup
    at(monkeypatch, '2026-11-15T12:00:00Z')
    full, _ = settling(context, empty_store(context))
    expected = [m.model_dump(mode='json') for m in both_months(full)]
    calc, calls = settling(context, store)
    both_months(calc)
    calls.clear()
    actual = [m.model_dump(mode='json') for m in both_months(calc)]
    assert len(calls) == SETTLE_DAYS + 1
    assert actual == expected
    assert actual[1]['storage'] and actual[1]['jobs']['coverage']['missing_days'] == 0
    full._workers.shutdown()
    calc._workers.shutdown()


def test_month_rollover_matches_a_full_recompute_after_close(setup, monkeypatch):
    # A day is revised for two days, and once more when the month's bill closes.
    def billed(user, lower, now):
        amount = lower.day * 1.5
        if now < lower.shift(days=2):
            amount -= 1
        if now >= lower.floor('month').shift(months=1, days=3):
            amount += 0.25
        return amount

    context, store, _ = setup
    calc, calls = settling(context, store, billed)
    when = arrow.get('2026-10-20T00:00:00Z')
    while when <= arrow.get('2026-11-06T18:00:00Z'):
        at(monkeypatch, when)
        if when == arrow.get('2026-11-05T00:00:00Z'):
            calls.clear()
        _, previous = both_months(calc)
        if when == arrow.get('2026-11-03T00:00:00Z'):
            before_close = previous
        when = when.shift(hours=6)
    full, _ = settling(context, empty_store(context), billed)
    _, expected = both_months(full)
    assert previous.model_dump(mode='json') == expected.model_dump(mode='json')
    # the close-of-month reconcile is what brings in the late revision
    assert before_close.total != expected.total
    # and it happens once: each October day is computed once more from the 5th
    assert sorted(c[1] for c in calls if c[1] < '2026-11') == [
        f'2026-10-{day:02d}' for day in range(1, 32)
    ]
    full._workers.shutdown()
    calc._workers.shutdown()


def test_a_faulted_day_is_not_stored(setup, monkeypatch):
    context, store, _ = setup
    calc, calls = settling(context, store)
    plain = calc.day

    def flaky(user, lower, upper, storage=None):
        lines = plain(user, lower, upper, storage)
        if lower.day == 3:
            calc.faulted = True
        return lines

    calc.day = flaky
    at(monkeypatch, '2026-11-15T12:00:00Z')
    both_months(calc)
    calls.clear()
    both_months(calc)
    assert [c[1] for c in calls] == ['2026-11-03'] + [
        f'2026-11-{day}' for day in range(12, 16)
    ] + ['2026-10-03']
    calc._workers.shutdown()


def test_a_cached_failed_read_keeps_marking_days_unsettled(setup):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)

    def down():
        raise RuntimeError('source down')

    calc.faulted = False
    assert calc._remember(('home-usage',), down) is None
    assert calc.faulted
    calc.faulted = False
    # served from the cache without a read: still the failed value
    assert calc._remember(('home-usage',), lambda: 'unused') is None
    assert calc.faulted
    calc._reads.clear()
    calc.faulted = False
    assert calc._remember(('home-usage',), lambda: 'ok') == 'ok'
    assert not calc.faulted
    calc._workers.shutdown()


def fifty_users(setup, monkeypatch):
    context, store, _ = setup
    names = [f'user-{index:02d}' for index in range(50)]
    context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[SimpleNamespace(username=name) for name in names],
            paginator=None,
        )
    )
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.MyCostsService.get_summary',
        lambda self, username: GetMyCostsSummaryResult(
            username=username, window='last_30_days'
        ),
    )
    calc, calls = settling(context, store)
    calc.capture_storage = lambda *args: None
    calc.capture_inventory = lambda *args: None
    worker = PersonalCostsCollector(context, store, calc)
    worker.activity.capture = lambda *args: None
    return worker, calls


def test_fifty_users_mid_month_work_per_run(setup, monkeypatch):
    worker, calls = fifty_users(setup, monkeypatch)
    at(monkeypatch, '2026-11-15T12:00:00Z')
    worker.check()
    before = len(calls)
    calls.clear()
    at(monkeypatch, '2026-11-15T12:16:00Z')
    worker.check()
    after = len(calls)
    # every run used to be the first: users x (days so far + all of last month)
    assert (before, after) == (50 * (15 + 31), 50 * (SETTLE_DAYS + 1))
    print(f'per-user-day computations per run: {before} -> {after} ({before / after}x)')
    worker.calculator._workers.shutdown()


def test_a_long_run_cannot_start_the_next_back_to_back(setup, monkeypatch):
    worker, _ = fifty_users(setup, monkeypatch)
    _, store, _ = setup
    at(monkeypatch, '2026-11-15T12:00:00Z')
    clock = iter([0, 1200])
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.time.monotonic',
        lambda: next(clock, 1200),
    )
    worker.check()
    state = store.get(SYSTEM, 'state')
    assert state['last_duration'] == 1200
    assert arrow.get(state['next_run']) == arrow.get('2026-11-15T12:20:00Z')
    worker.calculator._workers.shutdown()


class Counted:
    """A DynamoDB table that counts requests by operation."""

    def __init__(self, counts, name, rows=()):
        self.counts, self.name, self.rows = counts, name, list(rows)

    def query(self, **kwargs):
        self.counts[self.name] = self.counts.get(self.name, 0) + 1
        return {'Items': copy.deepcopy(self.rows)}


def real_days_fifty_users(setup, monkeypatch):
    """The real day calculation over counted tables: desktops read session history,
    AI lists the user's projects (user-projects query plus a GetItem per project)
    and each project's bedrock day rows."""
    context, store, _ = setup
    counts = {}
    names = [f'user-{index:02d}' for index in range(50)]
    context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[SimpleNamespace(username=name) for name in names],
            paginator=None,
        )
    )
    context.config = lambda: SimpleNamespace(
        get_string=lambda key, default=None: (
            'sessions' if key.endswith('dcv_session.alias') else default
        ),
        get_bool=lambda *args: True,
        get_config=lambda key, default=None: default,
        is_module_enabled=lambda module: module == 'virtual-desktop-controller',
        get_module_id=lambda module: 'vdc',
    )
    history = Counted(counts, 'session-history')
    context.aws = lambda: SimpleNamespace(
        aws_partition=lambda: 'aws-us-gov',
        dynamodb_table=lambda: SimpleNamespace(Table=lambda name: history),
    )
    projects = [
        SimpleNamespace(project_id=f'p{index}', name=f'project-{index}')
        for index in range(2)
    ]

    def user_projects(request):
        counts['user-projects'] = counts.get('user-projects', 0) + 1
        counts['projects'] = counts.get('projects', 0) + len(projects)
        return SimpleNamespace(projects=projects)

    usage = Counted(counts, 'bedrock.usage')
    context.projects = SimpleNamespace(
        get_user_projects=user_projects,
        bedrock_provisioner=SimpleNamespace(is_enabled=lambda: True),
        bedrock_usage_dao=SimpleNamespace(
            table=usage,
            query_day_rows=lambda *args: usage.query(),
        ),
    )
    own = store.table
    for operation in ('get_item', 'put_item', 'query'):

        def counted(*args, _call=getattr(own, operation), _op=operation, **kwargs):
            key = f'personal-costs.{_op}'
            counts[key] = counts.get(key, 0) + 1
            return _call(*args, **kwargs)

        setattr(own, operation, counted)
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.personal_costs_collector.MyCostsService.get_summary',
        lambda self, username: GetMyCostsSummaryResult(
            username=username, window='last_30_days'
        ),
    )
    calc = DailyCostsCalculator(context, store)
    calc._search = lambda index, body: {'hits': {'hits': []}}
    calc.capture_storage = lambda *args: None
    calc.capture_inventory = lambda *args: None
    worker = PersonalCostsCollector(context, store, calc)
    worker.activity.capture = lambda *args: None
    return worker, counts


def test_fifty_users_mid_month_dynamodb_requests_per_run(setup, monkeypatch, capsys):
    worker, counts = real_days_fifty_users(setup, monkeypatch)
    at(monkeypatch, '2026-11-15T12:00:00Z')
    worker.check()
    first = dict(counts)
    counts.clear()
    at(monkeypatch, '2026-11-15T12:16:00Z')
    worker.check()
    with capsys.disabled():
        print(
            f'\nDynamoDB requests, first run (no stored days): {sorted(first.items())}'
        )
        print(f'DynamoDB requests, next run: {sorted(counts.items())}')
    open_days = SETTLE_DAYS + 1
    # closed days read nothing; the open days share one read per user or project
    assert counts['session-history'] == 50
    assert counts['user-projects'] == 50 and counts['projects'] == 100
    assert counts['bedrock.usage'] == 2 * open_days
    worker.calculator._workers.shutdown()


# Inventory change records and sweeps


def legacy_write(store, username, as_of, value):
    # what 26.11.1 wrote for every user on every run
    instances, volumes = value
    store.put_source(
        username,
        f'inventory:{as_of.format("YYYY-MM-DD")}:{as_of.int_timestamp}',
        {
            'as_of': as_of.isoformat(),
            'instances': {
                key: {'State': item.get('State', {}), 'Tags': item.get('Tags', [])}
                for key, item in instances.items()
            },
            'volumes': volumes,
        },
    )


def volume(volume_id, kind='gp3', size=100, created='2026-08-01T00:00:00+00:00'):
    return {
        'VolumeId': volume_id,
        'VolumeType': kind,
        'Size': size,
        'CreateTime': created,
        'Attachments': [],
        'Tags': [],
    }


def inventory_at(username, moment):
    """user-a changes disks and desktop state; user-b never has anything; user-c has an
    unpriced disk; user-d is only seen by its own refreshes."""
    if username == 'user-a':
        volumes = []
        if moment < arrow.get('2026-09-02T14:00:00Z'):
            volumes.append(volume('vol-1'))
        if moment >= arrow.get('2026-09-02T08:00:00Z'):
            volumes.append(volume('vol-2', size=250))
        state = 'running' if moment < arrow.get('2026-09-02T16:00:00Z') else 'stopped'
        return {'i-1': {'State': {'Name': state}, 'Tags': []}}, volumes
    if username == 'user-b':
        return {}, []
    if username == 'user-c':
        return {}, [volume('vol-3', kind='io9')]
    return {}, [volume('vol-4', size=10)]


def inventory_runs():
    """(when, users observed): 15 minute runs, an outage, a slow stretch where runs are
    20 minutes apart, a user arriving mid-day and one leaving, and a lone refresh run."""
    moment = arrow.get('2026-09-01T22:00:07Z')
    runs = []
    while moment < arrow.get('2026-09-03T02:00:00Z'):
        if not (
            arrow.get('2026-09-02T05:00:00Z')
            <= moment
            < arrow.get('2026-09-02T06:30:00Z')
        ):
            users = ['user-a']
            if moment >= arrow.get('2026-09-02T09:00:00Z'):
                users.append('user-b')
            if moment < arrow.get('2026-09-02T12:00:00Z'):
                users.append('user-c')
            runs.append((moment, users))
        slow = (
            arrow.get('2026-09-02T10:00:00Z')
            <= moment
            < arrow.get('2026-09-02T12:00:00Z')
        )
        moment = moment.shift(minutes=20 if slow else 15)
    runs.append((arrow.get('2026-09-02T13:03:21Z'), ['user-d']))
    runs.append((arrow.get('2026-09-02T13:04:50Z'), ['user-d']))
    return sorted(runs, key=lambda run: run[0])


def priced(calc, monkeypatch):
    monkeypatch.setattr(
        calc,
        '_volume_price',
        lambda kind: None if kind == 'io9' else 0.08,
    )
    return calc


def capture_run(calc, moment, users):
    calc.begin()
    calc._user_inventory = lambda username: inventory_at(username, moment)
    calc._inventory_as_of = moment
    for username in users:
        calc.capture_inventory(username, moment)
    calc.flush_sweeps()


def inventory_store(setup):
    context, _, _ = setup
    store = PersonalCostsStore(context)
    store.table = Table()
    return store


DISK_WINDOWS = [
    ('2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z'),
    ('2026-09-02T00:00:00Z', '2026-09-03T00:00:00Z'),
    ('2026-09-03T00:00:00Z', '2026-09-04T00:00:00Z'),
    ('2026-09-02T13:00:00Z', '2026-09-02T13:10:00Z'),
]


def disk_lines(calc):
    return {
        (username, start): calc.disk_day(
            username, arrow.get(start), arrow.get(end)
        ).model_dump()
        for username in ('user-a', 'user-b', 'user-c', 'user-d', 'user-e')
        for start, end in DISK_WINDOWS
    }


def legacy_lines(setup, monkeypatch):
    context, _, _ = setup
    store = inventory_store(setup)
    for moment, users in inventory_runs():
        for username in users:
            legacy_write(store, username, moment, inventory_at(username, moment))
    calc = priced(DailyCostsCalculator(context, store), monkeypatch)
    lines = disk_lines(calc)
    calc._workers.shutdown()
    return lines, store


def test_change_records_cost_exactly_what_a_sample_every_run_cost(setup, monkeypatch):
    context, _, _ = setup
    expected, legacy = legacy_lines(setup, monkeypatch)
    # the fixture is not vacuous: real cost, gaps, unpriced disks, evidence of nothing
    day = '2026-09-02T00:00:00Z'
    assert expected[('user-a', day)]['cost'] > 0
    assert expected[('user-c', day)]['coverage']['missing_prices'] > 0
    assert expected[('user-b', day)]['cost'] == 0
    assert expected[('user-e', day)]['cost'] is None
    full = 350 * 0.08 / 30
    assert expected[('user-a', day)]['cost'] < full

    store = inventory_store(setup)
    writer = priced(DailyCostsCalculator(context, store), monkeypatch)
    for moment, users in inventory_runs():
        capture_run(writer, moment, users)
    writer._workers.shutdown()
    # read back by a calculator that holds nothing in memory
    reader = priced(DailyCostsCalculator(context, store), monkeypatch)
    assert disk_lines(reader) == expected
    reader._workers.shutdown()

    def rows(table, prefix):
        return [key for key in table.rows if key[1].startswith(prefix)]

    # three calendar days and user-a's three disk changes (its state change included)
    assert len(rows(store.table, 'inventory:')) <= 3 + 4 + 2 + 2 + 2
    assert len(rows(legacy.table, 'inventory:')) > 200


def test_an_upgrade_mid_day_reads_old_samples_without_counting_any_twice(
    setup, monkeypatch
):
    context, _, _ = setup
    expected, _ = legacy_lines(setup, monkeypatch)
    upgraded = arrow.get('2026-09-02T09:30:00Z')
    store = inventory_store(setup)
    writer = priced(DailyCostsCalculator(context, store), monkeypatch)
    for moment, users in inventory_runs():
        if moment < upgraded:
            for username in users:
                legacy_write(store, username, moment, inventory_at(username, moment))
        else:
            capture_run(writer, moment, users)
    writer._workers.shutdown()
    reader = priced(DailyCostsCalculator(context, store), monkeypatch)
    assert disk_lines(reader) == expected
    reader._workers.shutdown()


def test_a_run_reads_its_own_sweep_before_it_is_written(setup, monkeypatch):
    context, store, _ = setup
    calc = priced(DailyCostsCalculator(context, store), monkeypatch)
    start = arrow.get('2026-09-02T00:00:00Z')
    first, second = start.shift(hours=1), start.shift(hours=1, minutes=15)
    capture_run(calc, first, ['user-a'])
    calc.begin()
    calc._user_inventory = lambda username: inventory_at(username, second)
    calc._inventory_as_of = second
    calc._sweeps_flushed = 1e18  # not yet due
    calc.capture_inventory('user-a', second)
    assert [k[1] for k in store.table.rows if k[1].startswith('inventory-sweep')] == [
        f'inventory-sweep:2026-09-02:{first.int_timestamp}'
    ]
    assert calc.disk_day('user-a', start, start.shift(days=1)).cost == pytest.approx(
        round(100 * 0.08 / 30 / 24 / 2, 4)
    )
    calc._workers.shutdown()


def test_a_sweep_not_written_leaves_a_gap_and_is_retried(setup, monkeypatch):
    context, store, _ = setup
    calc = priced(DailyCostsCalculator(context, store), monkeypatch)
    start = arrow.get('2026-09-02T00:00:00Z')
    first, second = start.shift(hours=1), start.shift(hours=1, minutes=15)
    capture_run(calc, first, ['user-a'])

    def refuse(item):
        if item['record'].startswith('inventory-sweep'):
            raise RuntimeError('throttled')

    store.table.before_put = refuse
    capture_run(calc, second, ['user-a'])
    # a task killed here: what is durable never claims the unwritten sample
    lost = priced(DailyCostsCalculator(context, store), monkeypatch)
    one = round(100 * 0.08 / 30 / 24 / 4, 4)
    assert lost.disk_day('user-a', start, start.shift(days=1)).cost == pytest.approx(
        one
    )
    lost._workers.shutdown()
    store.table.before_put = lambda item: None
    calc.begin()
    calc.flush_sweeps()
    kept = priced(DailyCostsCalculator(context, store), monkeypatch)
    assert kept.disk_day('user-a', start, start.shift(days=1)).cost == pytest.approx(
        round(100 * 0.08 / 30 / 24 / 2, 4)
    )
    kept._workers.shutdown()
    calc._workers.shutdown()


def test_a_failed_sweep_read_does_not_settle_the_day(setup, monkeypatch):
    context, store, _ = setup
    calc = priced(DailyCostsCalculator(context, store), monkeypatch)
    start = arrow.get('2026-09-02T00:00:00Z')
    capture_run(calc, start.shift(hours=1), ['user-a'])
    calc = priced(DailyCostsCalculator(context, store), monkeypatch)
    records = store.records

    def failing(subject, prefix, last=None):
        if prefix.startswith('inventory-sweep'):
            raise RuntimeError('throttled')
        return records(subject, prefix, last)

    store.records = failing
    calc.faulted = False
    line = calc.day('user-a', start, start.shift(days=1))['desktop_disks']
    assert line.cost is None and calc.faulted
    calc._workers.shutdown()


def test_inventory_order_alone_writes_nothing_new(setup, monkeypatch):
    context, store, _ = setup
    calc = priced(DailyCostsCalculator(context, store), monkeypatch)
    moment = arrow.get('2026-09-02T10:00:00Z')
    volumes = [volume('vol-1'), volume('vol-2')]
    calc.begin()
    calc._user_inventory = lambda username: ({}, volumes)
    calc._inventory_as_of = moment
    calc.capture_inventory('user-a', moment)
    calc.begin()
    calc._user_inventory = lambda username: ({}, list(reversed(volumes)))
    calc._inventory_as_of = moment.shift(minutes=15)
    calc.capture_inventory('user-a', moment.shift(minutes=15))
    assert len(list(store.records('user-a', 'inventory:'))) == 1
    calc._workers.shutdown()


def test_fifty_users_one_day_inventory_requests(setup, monkeypatch, capsys):
    import json as _json
    import math

    context, _, _ = setup
    names = [f'user-{index:02d}' for index in range(50)]
    start = arrow.get('2026-09-02T00:00:00Z')
    value = {}, [volume('vol-1')]

    def wcu(table):
        return sum(
            math.ceil(len(_json.dumps(row)) / 1024) for row in table.rows.values()
        )

    legacy, store = inventory_store(setup), inventory_store(setup)
    calc = priced(DailyCostsCalculator(context, store), monkeypatch)
    puts = {'n': 0}
    store.table.before_put = lambda item: puts.update(n=puts['n'] + 1)
    for index in range(96):
        moment = start.shift(minutes=15 * index)
        calc.begin()
        calc._user_inventory = lambda username: value
        calc._inventory_as_of = moment
        for name in names:
            legacy_write(legacy, name, moment, value)
            calc.capture_inventory(name, moment)
        calc.flush_sweeps()
    before_puts = 50 * 96
    assert puts['n'] == 50 + 96
    assert len(legacy.table.rows) == before_puts

    def read(table_store):
        reader = priced(DailyCostsCalculator(context, table_store), monkeypatch)
        reader.begin()
        counted = {'queries': 0, 'bytes': 0}
        query = table_store.table.query

        def counting(**kwargs):
            page = query(**kwargs)
            counted['queries'] += 1
            counted['bytes'] += sum(len(_json.dumps(i)) for i in page['Items'])
            return page

        table_store.table.query = counting
        lines = [
            reader.disk_day(
                name,
                start.shift(days=1),
                start.shift(days=1, minutes=1),
            ).model_dump()
            for name in names
        ]
        table_store.table.query = query
        reader._workers.shutdown()
        counted['rcu'] = math.ceil(counted['bytes'] / 4096)
        return lines, counted

    before_lines, before = read(legacy)
    after_lines, after = read(store)
    assert before_lines == after_lines
    assert wcu(store.table) * 5 < wcu(legacy.table)
    assert after['rcu'] * 5 < before['rcu']
    with capsys.disabled():
        print(
            f'\ninventory, 50 users, one day: PutItem {before_puts} -> {puts["n"]}, '
            f'WCU {wcu(legacy.table)} -> {wcu(store.table)}, '
            f'items {len(legacy.table.rows)} -> {len(store.table.rows)}; '
            f'reads for one user-day each: Query {before["queries"]} -> {after["queries"]}, '
            f'RCU {before["rcu"]} -> {after["rcu"]}'
        )
    calc._workers.shutdown()


def observed_day(store, start, missed=()):
    """One disk kept all day, seen at a healthy collector's pace: a sweep every 937 s
    (93 a day, 926 to 938 s apart on a real cluster), less the hours in missed."""
    volume = {
        'VolumeId': 'vol-test',
        'VolumeType': 'gp3',
        'Size': 100,
        'CreateTime': start.shift(days=-1).isoformat(),
        'Attachments': [],
    }
    day = start.format('YYYY-MM-DD')
    store.put_source(
        'user-a',
        f'inventory:{day}:{start.int_timestamp}',
        {'as_of': start.isoformat(), 'instances': {}, 'volumes': [volume]},
    )
    seen = start
    while (seen := seen.shift(seconds=937)) < start.shift(days=1):
        if seen.hour not in missed:
            store.put_source(
                SYSTEM,
                f'inventory-sweep:{day}:{seen.int_timestamp}',
                {'as_of': seen.isoformat(), 'users': ['user-a']},
            )


def test_a_disk_day_is_partial_only_when_observations_missed_part_of_it(
    setup, monkeypatch
):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)
    monkeypatch.setattr(calc, '_volume_price', lambda volume_type: 0.08)
    whole, broken = arrow.get('2026-11-07T00:00:00Z'), arrow.get('2026-11-09T00:00:00Z')
    observed_day(store, whole)
    observed_day(store, broken, missed=range(6, 12))
    result = calc.disk_day('user-a', whole, whole.shift(days=1))
    # every day was partial, so a fully counted month read as days not counted
    assert result.status == 'ready'
    # 93 samples of 15 minutes: 96.9% of the day
    assert result.cost == pytest.approx(100 * 0.08 / 30 * 93 * 900 / 86400, abs=5e-4)
    gap = calc.disk_day('user-a', broken, broken.shift(days=1))
    assert gap.status == 'partial' and gap.cost < result.cost * 0.8
    calc._workers.shutdown()


# One user's settled days as a 26.11.1 cluster stored them: a disk amount on every
# day, every disk day partial, and no AI cost with Bedrock off.
STORED_BEFORE = {
    1: 0.1062,
    2: 0.1019,
    3: 0.1191,
    4: 0.1022,
    5: 0.1021,
    6: 0.069,
    7: 0.0,
    8: 0.0,
    9: 0.0208,
}


def stored_before(calc, start):
    def line(cost, status):
        return MyCostsAmount(
            cost=cost,
            status=status,
            reason='r',
            coverage={
                'known_days': 0,
                'missing_days': 0,
                'missing_prices': 0,
                'inferred_intervals': 0,
            },
        ).model_dump(mode='json')

    days = {}
    for number, disks in STORED_BEFORE.items():
        cursor = start.shift(days=number - 1)
        days[cursor.format('YYYY-MM-DD')] = dict(
            start=cursor.isoformat(),
            closed=False,
            lines={
                'jobs': line(0.0, 'ready'),
                'desktops': line(0.0, 'ready'),
                'desktop_disks': line(disks, 'partial'),
                'shared_storage': line(0.0, 'ready'),
                'ai': line(None, 'unavailable'),
            },
            shares={},
        )
    calc._store_days('user-a', start.format('YYYY-MM'), days)


def test_a_month_with_every_disk_day_counted_and_bedrock_off_is_complete(
    setup, monkeypatch
):
    context, store, _ = setup
    context.config().get_bool = lambda key, default=None: (
        not key.endswith('bedrock.enabled')
    )
    calc = DailyCostsCalculator(context, store)
    calc.day = lambda user, lower, upper, storage=None: {
        facet: calc._line(0.0, missing=facet == 'ai') for facet in FACETS
    }
    at(monkeypatch, '2026-11-13T15:26:00Z')
    start = arrow.utcnow().floor('month')
    stored_before(calc, start)
    result = calc.month('user-a', start, arrow.utcnow())
    disks = result.desktop_disks
    assert [point.amount for point in disks.daily[:9]] == list(STORED_BEFORE.values())
    assert {point.status for point in disks.daily} == {'ready'}
    assert disks.status == 'ready' and disks.coverage.missing_days == 0
    assert result.ai.cost is None and result.ai.status == 'not_applicable'
    assert {point.status for point in result.ai.daily} == {'not_applicable'}
    assert result.ai.coverage.missing_days == 0
    assert not result.incomplete
    # with Bedrock on, the same days are a cost still to come
    context.config().get_bool = lambda key, default=None: True
    result = calc.month('user-a', start, arrow.utcnow())
    assert result.ai.status == 'unavailable' and result.ai.coverage.missing_days == 13
    assert result.incomplete
    calc._workers.shutdown()


def test_a_stored_disk_day_with_a_missing_price_stays_partial(setup, monkeypatch):
    context, store, _ = setup
    calc = DailyCostsCalculator(context, store)
    calc.day = lambda user, lower, upper, storage=None: {
        facet: calc._line(0.0) for facet in FACETS
    }
    at(monkeypatch, '2026-11-13T15:26:00Z')
    start = arrow.utcnow().floor('month')
    stored_before(calc, start)
    days = calc._stored_days('user-a', '2026-11')
    days['2026-11-02']['lines']['desktop_disks']['coverage']['missing_prices'] = 1
    calc._store_days('user-a', '2026-11', days)
    daily = calc.month('user-a', start, arrow.utcnow()).desktop_disks.daily
    assert [point.status for point in daily[:3]] == ['ready', 'partial', 'ready']
    calc._workers.shutdown()


def test_a_day_settled_as_unknown_keeps_its_amount_without_staleness():
    from ideadatamodel import MyCostsDaily

    def storage(third, status, as_of):
        return MyCostsAmount(
            cost=0.5 if third is None else 0.5 + third,
            status=status,
            reason='Rate estimate.',
            source_as_of=as_of,
            daily=[
                MyCostsDaily(date='2026-09-03', day=3, amount=third, status='partial'),
                MyCostsDaily(
                    date='2026-09-06', day=6, amount=0.5, status='estimated_share'
                ),
            ],
        )

    old = month()
    old.shared_storage = storage(0.0, 'partial', '2026-09-06T16:01:41-05:00')
    candidate = month()
    candidate.shared_storage = storage(None, 'partial', '2026-09-13T10:26:15-05:00')
    assert not PersonalCostsCollector.retain_known(candidate, old, {'2026-09-03'})
    line = candidate.shared_storage
    assert line.daily[0].amount == 0.0 and line.daily[0].status == 'partial'
    assert line.cost == 0.5 and line.status == 'partial'
    # the min against the last result could only go back, for the rest of the month
    assert line.source_as_of == '2026-09-13T10:26:15-05:00'
    assert 'Source unavailable' not in line.reason
    # a day still being computed is a failed read, as before
    candidate.shared_storage = storage(None, 'partial', '2026-09-13T10:26:15-05:00')
    assert PersonalCostsCollector.retain_known(candidate, old)
    assert candidate.shared_storage.source_as_of == '2026-09-06T16:01:41-05:00'


def test_the_header_time_is_the_collection_and_a_settled_gap_is_not_stale(
    setup, monkeypatch
):
    worker, _ = fifty_users(setup, monkeypatch)
    _, store, _ = setup
    calc = worker.calculator
    inner = calc.day

    def day(user, lower, upper, storage=None):
        lines = inner(user, lower, upper, storage)
        if lower.day == 11 and arrow.utcnow() < arrow.get('2026-11-14'):
            # known while the day was open, then unknown for good (day 11 has no AI)
            lines['ai'] = calc._line(1.0)
        return lines

    calc.day = day

    def collected(when):
        at(monkeypatch, when)
        worker.check()
        return GetMyCostsResult(
            **store.projection('user-00', store.get('user-00', 'head'), 'costs')
        )

    first = collected('2026-11-13T12:00:00Z')
    assert first.current.ai.daily[10].amount == 1.0 and first.state == 'ready'
    for when in ('2026-11-15T12:00:00Z', '2026-11-15T12:16:00Z'):
        result = collected(when)
        assert result.current.ai.daily[10].amount == 1.0
        assert result.current.ai.daily[10].status == 'partial'
        # stale on every run, and the header pinned to the oldest source, until Dec 1
        assert result.state == 'ready'
        assert arrow.get(result.refreshed_at) == arrow.get(when)
    calc._workers.shutdown()
