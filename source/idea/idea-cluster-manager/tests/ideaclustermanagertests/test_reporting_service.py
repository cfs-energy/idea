"""Recorded coverage, period boundaries and activity formulas."""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import ReportingPeriodRequest, exceptions
from ideaclustermanager.app.reporting.reporting_service import (
    ReportingService,
    resolve_period,
    selected_money,
    job_values,
    walltime,
)
from ideaclustermanager.app.reporting.reporting_sources import ReportingSources
from ideaclustermanager.app.reporting.snapshot_store import FACETS


def period(
    start='2024-02-01', end='2024-02-29', now='2024-03-01T12:00:00+00:00', zone='UTC'
):
    return resolve_period(
        ReportingPeriodRequest(period='custom', start_date=start, end_date=end),
        zone,
        datetime.fromisoformat(now),
    )


def projection(points, state='ready', currency='USD'):
    return dict(
        state=state,
        head={'as_of': '2024-03-01T00:00:00+00:00'},
        costs=dict(
            currency=currency,
            timezone='UTC',
            current={
                facet: dict(daily=points, source_as_of='2024-03-01T00:00:00+00:00')
                for facet in FACETS
            },
        ),
    )


def data():
    return dict(
        users={'former': 'former'},
        projects={},
        projections={},
        jobs=[],
        desktops=[],
        coverage=dict(
            accounts='ready',
            projects='ready',
            projections='ready',
            jobs='ready',
            desktops='ready',
            desktop_history='ready',
        ),
        warnings=[],
    )


def build(value, selected=None):
    return ReportingService.build(
        value, selected or period(), 'USD', 'UTC', time.monotonic() + 30
    )


def job(identity='one', **kwargs):
    value = dict(
        job_uid=identity,
        job_id='1',
        owner='former',
        state='finished',
        project='recorded',
        start_time='2024-02-02T00:00:00+00:00',
        end_time='2024-02-02T02:00:00+00:00',
        params=dict(walltime='01:00:00', nodes=2),
        estimated_bom_cost=dict(
            total=dict(amount=1000, unit='USD'),
            line_items=[
                dict(service='aws.ec2', total_price=dict(amount=4, unit='USD')),
                dict(service='aws.ebs', total_price=dict(amount=996, unit='USD')),
            ],
        ),
    )
    value.update(kwargs)
    return {'_id': identity, '_source': value}


@pytest.mark.parametrize(
    'now,kind,first,last',
    [
        ('2024-03-01T12:00:00+00:00', 'last_30_days', '2024-02-01', '2024-03-01'),
        ('2025-03-01T12:00:00+00:00', 'last_30_days', '2025-01-31', '2025-03-01'),
        ('2025-01-01T12:00:00+00:00', 'last_month', '2024-12-01', '2024-12-31'),
        ('2024-03-01T12:00:00+00:00', 'last_month', '2024-02-01', '2024-02-29'),
        ('2024-03-31T12:00:00+00:00', 'this_month', '2024-03-01', '2024-03-31'),
    ],
)
def test_calendar_rollovers(now, kind, first, last):
    result = resolve_period(
        ReportingPeriodRequest(period=kind), 'UTC', datetime.fromisoformat(now)
    )
    assert (result.start_date, result.end_date) == (first, last)


@pytest.mark.parametrize('day,hours', [('2024-03-10', 23), ('2024-11-03', 25)])
def test_custom_end_is_inclusive_across_dst(day, hours):
    result = period(day, day, now='2024-12-01T00:00:00+00:00', zone='America/New_York')
    elapsed = (
        datetime.fromisoformat(result.end).timestamp()
        - datetime.fromisoformat(result.start).timestamp()
    )
    assert elapsed == hours * 3600


def test_current_day_is_clipped_and_custom_limits_are_exact():
    result = period('2024-03-01', '2024-03-01')
    assert result.provisional
    assert result.end == '2024-03-01T12:00:00+00:00'
    assert period('2024-01-01', '2024-12-31', now='2025-01-01T00:00:00+00:00')
    for first, last in [
        ('2023-12-31', '2024-12-31'),
        ('2025-01-02', '2025-01-02'),
        ('2024-02-02', '2024-02-01'),
    ]:
        with pytest.raises(exceptions.SocaException):
            period(first, last, now='2025-01-01T00:00:00+00:00')
    with pytest.raises(exceptions.SocaException):
        period(zone='invalid/timezone')
    with pytest.raises(exceptions.SocaException):
        resolve_period(
            ReportingPeriodRequest(period='last_month', start_date='2024-01-01'),
            'UTC',
            datetime.now(timezone.utc),
        )


def test_march_trailing_window_keeps_january_gap_partial():
    selected = resolve_period(
        ReportingPeriodRequest(period='last_30_days'),
        'UTC',
        datetime.fromisoformat('2025-03-01T12:00:00+00:00'),
    )
    points = [
        dict(date=f'2025-02-{day:02}', amount=1, status='ready') for day in range(1, 29)
    ] + [dict(date='2025-03-01', amount=1, status='ready')]
    amount, cover = selected_money(projection(points), 'jobs', selected, 'USD', 'UTC')
    assert amount == 29
    assert cover['status'] == 'partial'
    assert cover['missing_days'] == 1


def test_missing_zero_stale_and_currency_are_distinct():
    selected = period('2024-02-01', '2024-02-01')
    amount, cover = selected_money(
        projection([dict(date='2024-02-01', amount=0, status='ready')]),
        'jobs',
        selected,
        'USD',
        'UTC',
    )
    assert amount == 0 and cover['status'] == 'estimated'
    amount, cover = selected_money(projection([]), 'jobs', selected, 'USD', 'UTC')
    assert amount is None and cover['status'] == 'unavailable'
    amount, cover = selected_money(
        projection([dict(date='2024-02-01', amount=2, status='ready')], state='stale'),
        'jobs',
        selected,
        'USD',
        'UTC',
    )
    assert amount == 2 and cover['status'] == 'partial' and 'stale' in cover['reason']
    assert (
        selected_money(projection([], currency='EUR'), 'jobs', selected, 'USD', 'UTC')[
            0
        ]
        is None
    )


def test_efficiency_is_ratio_of_sums_and_can_exceed_one_hundred():
    value = data()
    value['jobs'] = [
        job(),
        job('two', params=dict(walltime='03:00:00', nodes=3), total_time_secs=18000),
    ]
    tables, tiles, _, _ = build(value)
    row = tables['user'][0]
    assert row['job_count'] == 2
    assert row['requested_walltime_hours'] == 4
    assert row['elapsed_hours'] == 7
    assert row['efficiency_pct'] == 175
    assert row['node_hours'] == 19
    assert tiles['total']['efficiency_pct'] == 175


def test_never_started_cancelled_missing_elapsed_and_running_jobs():
    value = data()
    value['jobs'] = [
        job(),
        job('cancel', state='cancelled', start_time=None, total_time_secs=None),
        job('running', state='running'),
        job('bad', start_time=None, total_time_secs=-1),
        job('edge', end_time='2024-03-01T00:00:00+00:00'),
    ]
    row = build(value)[0]['user'][0]
    assert row['job_count'] == 3
    assert row['elapsed_hours'] == 2
    assert row['requested_walltime_hours'] == 1
    assert row['coverage']['efficiency_pct']['eligible_count'] == 1
    assert row['coverage']['node_hours']['missing_records'] == 2
    assert (
        job_values(job(start_time=None, total_time_secs=None)['_source'], 'USD')[
            'elapsed'
        ]
        is None
    )


def test_job_identity_never_deduplicates_on_reusable_job_id():
    value = data()
    value['jobs'] = [
        job('first'),
        job('second'),
        job('first'),
        job('document', job_uid=None),
        job('document2', job_uid=None),
    ]
    assert build(value)[0]['user'][0]['job_count'] == 4


def test_compute_only_bom_currency_missing_price_and_units():
    value = job()['_source']
    assert job_values(value, 'USD')['cost'] == 4
    assert job_values(value, 'EUR')['cost'] is None
    value['estimated_bom_cost']['price_unavailable'] = True
    assert job_values(value, 'USD')['cost'] is None
    value['estimated_bom_cost'] = dict(
        line_items=[
            dict(
                service='aws.ec2',
                unit='per hour',
                quantity=2,
                unit_price=dict(amount='1.25', unit='USD'),
            )
        ]
    )
    assert job_values(value, 'USD')['cost'] == Decimal('2.50')
    value['estimated_bom_cost']['line_items'][0]['unit_price']['unit'] = None
    assert job_values(value, 'USD')['cost'] is None
    assert walltime('1-02:30:00') == 95400
    assert walltime('01:99:00') is None


def test_project_recorded_labels_no_membership_allocation_and_partial_top():
    value = data()
    value['projects'] = {'recorded': 'current label'}
    value['project_names'] = {'recorded': 'recorded'}
    value['jobs'] = [
        job(project_name='recorded label'),
        job('unpriced', estimated_bom_cost=None),
        job('unassigned', project=None),
    ]
    value['projections']['former'] = projection(
        [dict(date='2024-02-01', amount=1, status='ready')]
    )
    tables, tiles, _, warnings = build(value)
    project = next(row for row in tables['project'] if row['key'] == 'recorded')
    assert project['label'] == 'recorded label'
    assert project['spend_by_facet']['jobs'] == 4
    assert project['spend_by_facet']['desktops'] is None
    assert project['coverage']['jobs']['status'] == 'partial'
    assert tiles['top_project']['key'] == 'recorded'
    assert any(row['label'] == 'Unassigned' for row in tables['project'])
    assert (
        next(
            row for row in tables['project'] if row['label'] == 'Unallocated to project'
        )['spend_total']
        == 4
    )
    assert tiles['total']['spend_total'] == 5
    assert tiles['job_spend_difference']['spend_total'] == -7
    assert any(
        'by recorded job-compute spend; other facets unallocated' in warning
        for warning in warnings
    )


def test_desktop_history_wins_and_keeps_raw_project():
    value = data()
    source = dict(
        idea_session_id='session',
        owner='former',
        project_id='retained',
        state='READY',
        created_on=1706832000000,
        stopped_on=1706839200000,
    )
    history = dict(
        source, stopped_on=1706835600000, state='DELETED', project_id='historic'
    )
    value['desktops'] = [
        {'_source': source},
        {'_source': history, '_history': True},
        {'_source': source},
    ]
    tables = build(value)[0]
    assert tables['user'][0]['desktop_hours'] == 1
    assert tables['user'][0]['idle_stops'] is None
    assert (
        next(row for row in tables['project'] if row['key'] == 'historic')[
            'desktop_hours'
        ]
        == 1
    )


def test_source_failures_preserve_money_and_missing_optional_metrics():
    value = data()
    value['coverage'].update(
        jobs='unavailable', desktops='unavailable', desktop_history='unavailable'
    )
    value['projections']['former'] = projection(
        [dict(date='2024-02-01', amount=2, status='ready')]
    )
    row = build(value)[0]['user'][0]
    assert row['spend_total'] == 10
    assert row['job_count'] is None and row['desktop_hours'] is None
    value['coverage'].update(
        jobs='not_applicable',
        desktops='not_applicable',
        desktop_history='not_applicable',
    )
    value['projections'] = {}
    assert build(value)[0]['user'][0]['coverage']['jobs']['status'] == 'not_applicable'


def test_pruned_head_retries_and_pins_replacement_generation():
    costs = projection([])['costs']
    head = dict(generation='new', parts={'costs': 2})
    encoded = json.dumps(costs)
    storage = SimpleNamespace(
        get=Mock(side_effect=[None, head, encoded[:10], encoded[10:]])
    )
    source = ReportingSources(SimpleNamespace(personal_costs_store=storage))
    result = source.projection(
        'former', dict(generation='old', parts={'costs': 1}), time.monotonic() + 30
    )
    assert result['head']['generation'] == 'new'
    assert result['costs'] == costs
    assert [call.args[1] for call in storage.get.call_args_list] == [
        'g:old:costs:0',
        'head',
        'g:new:costs:0',
        'g:new:costs:1',
    ]


def test_dynamodb_and_accounts_pagination_have_no_fixed_page_cap():
    table = Mock()
    table.scan.side_effect = [
        dict(Items=[{'key': i}], LastEvaluatedKey={'key': i}) for i in range(1100)
    ] + [dict(Items=[{'key': 1100}])]
    assert len(list(ReportingSources.scan(table, time.monotonic() + 30))) == 1101
    read = Mock(
        side_effect=[
            SimpleNamespace(
                listing=[
                    SimpleNamespace(model_dump=lambda **kwargs: {'username': 'user'})
                ],
                paginator=SimpleNamespace(cursor=str(i)),
            )
            for i in range(1100)
        ]
        + [SimpleNamespace(listing=[], paginator=None)]
    )
    assert (
        len(
            list(
                ReportingSources.listing(
                    read, lambda **kwargs: kwargs, time.monotonic() + 30
                )
            )
        )
        == 1100
    )


def test_opensearch_scroll_is_consistent_and_cleared_after_large_read():
    client = Mock()
    client.search.return_value = dict(
        _scroll_id='context', hits=dict(hits=[{'_id': 'first'}])
    )
    client.scroll.side_effect = [
        dict(
            _scroll_id='context', hits=dict(hits=[{'_id': str(i)} for i in range(1000)])
        )
        for _ in range(11)
    ] + [dict(_scroll_id='context', hits=dict(hits=[]))]
    source = ReportingSources(
        SimpleNamespace(
            analytics_service=lambda: SimpleNamespace(
                os_client=SimpleNamespace(os_client=client)
            )
        )
    )
    assert (
        len(list(source.search('index', {'match_all': {}}, time.monotonic() + 30)))
        == 11001
    )
    assert client.search.call_args.kwargs['body']['sort'] == ['_doc']
    client.clear_scroll.assert_called_once()


def test_retained_heads_include_former_users_and_exclude_reserved_subjects():
    costs = projection([])['costs']
    table = Mock()
    table.scan.return_value = dict(
        Items=[
            dict(
                subject='former',
                record='head',
                payload=json.dumps({'generation': 'pinned', 'parts': {'costs': 1}}),
            ),
            dict(subject='!collector', record='head', payload='{}'),
        ]
    )
    context = SimpleNamespace(
        accounts=SimpleNamespace(
            list_users=lambda request: SimpleNamespace(listing=[], paginator=None)
        ),
        projects=SimpleNamespace(
            list_projects=lambda request: SimpleNamespace(listing=[], paginator=None)
        ),
        personal_costs_store=SimpleNamespace(
            table=table, get=lambda *args: json.dumps(costs)
        ),
        config=lambda: SimpleNamespace(is_module_enabled=lambda module: False),
    )
    result = ReportingSources(context).read(period(), time.monotonic() + 30)
    assert set(result['users']) == {'former'}
    assert result['projections']['former']['head']['generation'] == 'pinned'
    assert result['coverage']['jobs'] == 'not_applicable'


def test_reporting_sources_have_no_fresh_collection_dependencies():
    import inspect
    from ideaclustermanager.app.reporting import reporting_sources

    text = inspect.getsource(reporting_sources)
    for forbidden in (
        'cost_explorer(',
        'get_cost_and_usage(',
        'pricing(',
        'MyCostsService',
        'request_refresh(',
        'sqlite',
        'describe_instances(',
        'list_bedrock_projects',
    ):
        assert forbidden not in text


def test_summary_publishes_all_tables_with_pinned_bounds_and_decimal_totals(
    monkeypatch,
):
    from ideaclustermanagertests.test_reporting_snapshot_store import store
    from ideadatamodel import locale
    from ideaclustermanager.app.reporting.reporting_service import ReportingSummary

    monkeypatch.setattr(locale, 'get_currency_code', lambda: 'USD')
    cache = store()
    value = data()
    value['projections']['former'] = projection(
        [dict(date='2024-02-01', amount='0.10', status='ready')]
    )
    context = SimpleNamespace(
        config=lambda: SimpleNamespace(get_string=lambda *args, **kwargs: 'UTC')
    )
    service = ReportingService(
        context, store=cache, sources=SimpleNamespace(read=lambda *args: value)
    )
    result = service.get_summary(
        'reader',
        ReportingPeriodRequest(
            period='custom', start_date='2024-02-01', end_date='2024-02-01'
        ),
        lambda: True,
    )
    assert isinstance(result, ReportingSummary)
    assert result.tiles['total'].spend_total == Decimal('0.50')
    assert result.model_dump(mode='json')['tiles']['total']['spend_total'] == '0.50'
    metadata = cache.lookup(result.snapshot_id, 'reader', lambda: True)
    assert set(metadata['parts']) == {'user', 'project', 'facet'}
    assert metadata['summary']['period']['end'] == '2024-02-02T00:00:00+00:00'
    assert result.as_of == '2024-03-01T00:00:00+00:00'


def test_missing_sources_and_failed_heads_remain_explicit():
    table = Mock()
    table.scan.side_effect = RuntimeError('unavailable')
    context = SimpleNamespace(
        accounts=SimpleNamespace(
            list_users=lambda request: SimpleNamespace(listing=[], paginator=None)
        ),
        projects=SimpleNamespace(
            list_projects=lambda request: SimpleNamespace(listing=[], paginator=None)
        ),
        personal_costs_store=SimpleNamespace(table=table),
        config=lambda: SimpleNamespace(is_module_enabled=lambda module: False),
    )
    result = ReportingSources(context).read(period(), time.monotonic() + 30)
    assert result['coverage']['projections'] == 'unavailable'
    assert result['warnings'] == ['projections source unavailable.']
    head = dict(generation='new', parts={'costs': 1})
    context.personal_costs_store.get = Mock(side_effect=[None, head, None, head, None])
    failed = ReportingSources(context).projection(
        'former', dict(generation='old', parts={'costs': 1}), time.monotonic() + 30
    )
    assert failed['costs'] is None and failed['state'] == 'unavailable'
    assert context.personal_costs_store.get.call_count == 5


def test_oldest_head_and_freshness_spread_are_exposed():
    value = data()
    value['users']['other'] = 'other'
    first = projection([dict(date='2024-02-01', amount=1, status='ready')])
    second = projection([dict(date='2024-02-01', amount=1, status='ready')])
    second['head']['as_of'] = '2024-03-01T01:00:00+00:00'
    value['projections'] = {'former': first, 'other': second}
    cover = build(value)[2]['source_projections']
    assert cover['source_as_of'] == '2024-03-01T00:00:00+00:00'
    assert cover['freshness_spread_seconds'] == 3600


def test_missing_desktop_stop_zero_does_not_drop_live_session():
    value = data()
    value['desktops'] = [
        {
            '_source': dict(
                idea_session_id='session',
                owner='former',
                state='READY',
                created_on=1706832000000,
                stopped_on=0,
            )
        }
    ]
    assert build(value)[0]['user'][0]['desktop_hours'] > 0


def test_no_priced_projects_keeps_top_unavailable_and_covered_absence_zero():
    value = data()
    value['projects'] = {'empty': 'empty'}
    tables, tiles, _, _ = build(value)
    assert tables['project'][0]['spend_by_facet']['jobs'] == 0
    assert tiles['top_project']['spend_total'] is None


def test_job_fallback_duration_and_invalid_nodes_do_not_use_queue_wait():
    values = job_values(
        job(
            total_time_secs=-1,
            queue_time='2024-01-01T00:00:00+00:00',
            params={'walltime': '01:00:00', 'nodes': None},
        )['_source'],
        'USD',
    )
    assert values['elapsed'] == 2
    assert values['node_hours'] is None
    values = job_values(
        job(start_time='2024-02-03T00:00:00+00:00', total_time_secs=None)['_source'],
        'USD',
    )
    assert values['elapsed'] is None


def test_failed_search_shards_never_return_complete_capped_results():
    client = Mock()
    client.search.return_value = dict(
        _scroll_id='context', _shards={'failed': 1}, hits={'hits': [{'_id': 'partial'}]}
    )
    source = ReportingSources(
        SimpleNamespace(
            analytics_service=lambda: SimpleNamespace(
                os_client=SimpleNamespace(os_client=client)
            )
        )
    )
    with pytest.raises(ValueError, match='Incomplete'):
        list(source.search('index', {'match_all': {}}, time.monotonic() + 30))
    client.clear_scroll.assert_called_once()


def test_projection_coverage_includes_missing_and_failed_heads():
    value = data()
    value['projections']['former'] = dict(state='unavailable', costs=None, head=None)
    covers = build(value)[2]
    assert covers['source_projections']['status'] == 'unavailable'
    assert covers['source_projections']['missing_records'] == 1
    value['users']['other'] = 'other'
    value['projections']['other'] = projection(
        [dict(date='2024-02-01', amount=0, status='ready')]
    )
    covers = build(value)[2]
    assert covers['source_projections']['status'] == 'partial'
    assert covers['source_projections']['eligible_count'] == 1


def test_build_timeout_bounds_blocked_source_reads_without_publication(monkeypatch):
    import threading
    from ideaclustermanager.app.reporting import reporting_service
    from ideadatamodel import locale
    from ideaclustermanagertests.test_reporting_snapshot_store import store

    monkeypatch.setattr(reporting_service, 'BUILD_SECONDS', 0.05)
    monkeypatch.setattr(locale, 'get_currency_code', lambda: 'USD')
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        release.wait(5)
        return data()

    cache = store()
    context = SimpleNamespace(
        config=lambda: SimpleNamespace(get_string=lambda *args, **kwargs: 'UTC')
    )
    service = ReportingService(
        context, store=cache, sources=SimpleNamespace(read=blocked)
    )
    try:
        with pytest.raises(exceptions.SocaException) as error:
            service.get_summary(
                'reader', ReportingPeriodRequest(period='this_month'), lambda: True
            )
        assert error.value.error_code == 'REPORT_TIMEOUT'
        assert entered.is_set() and not release.is_set()
        assert not cache.table.writes
    finally:
        release.set()
        service._builds.shutdown(wait=True)
    assert not cache.table.writes


@pytest.mark.parametrize('kind', ['users', 'projects'])
@pytest.mark.parametrize('through_adapter', [False, True])
def test_real_listing_models_continue_two_dao_pages(kind, through_adapter):
    from ideadatamodel import ListUsersRequest, ListProjectsRequest, SocaPaginator
    from ideaclustermanager.app.accounts.db.user_dao import UserDAO
    from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO

    field = 'username' if kind == 'users' else 'project_id'
    request_type = ListUsersRequest if kind == 'users' else ListProjectsRequest
    dao = object.__new__(UserDAO if kind == 'users' else ProjectsDAO)
    dao.logger = Mock()
    calls = []

    def scan(**request):
        calls.append(request)
        assert len(calls) <= 2, 'listing repeated the first page'
        if len(calls) == 1:
            assert request == {}
            return dict(
                Items=[
                    {
                        field: 'first',
                        'created_on': 1706832000000,
                        'updated_on': 1706832000000,
                    }
                ],
                LastEvaluatedKey={field: 'first'},
            )
        assert request == {'ExclusiveStartKey': {field: 'first'}}
        return dict(
            Items=[
                {
                    field: 'second',
                    'created_on': 1706832000000,
                    'updated_on': 1706832000000,
                }
            ]
        )

    dao.table = SimpleNamespace(scan=scan)
    read = dao.list_users if kind == 'users' else dao.list_projects
    if through_adapter:
        rows = list(ReportingSources.listing(read, request_type, time.monotonic() + 30))
    else:
        first = read(request_type())
        second = read(
            request_type(paginator=SocaPaginator(cursor=first.paginator.cursor))
        )
        rows = [row.model_dump() for row in first.listing + second.listing]
    assert [row[field] for row in rows] == ['first', 'second']
    assert len(calls) == 2


def test_project_names_resolve_to_ids_from_source_listing():
    from ideadatamodel import ListProjectsResult, Project

    config = Mock()
    config.is_module_enabled.return_value = False
    context = SimpleNamespace(
        config=lambda: config,
        accounts=SimpleNamespace(
            list_users=lambda request: SimpleNamespace(listing=[], paginator=None)
        ),
        projects=SimpleNamespace(
            list_projects=lambda request: ListProjectsResult(
                listing=[
                    Project(
                        project_id='project-id',
                        name='project-name',
                        title='Project title',
                    )
                ]
            )
        ),
        personal_costs_store=SimpleNamespace(
            table=Mock(scan=Mock(return_value={'Items': []}))
        ),
    )
    value = ReportingSources(context).read(period(), time.monotonic() + 30)
    value['coverage'].update(jobs='ready', desktops='ready')
    value['users'] = {'former': 'former'}
    value['jobs'] = [job(project='project-name')]
    value['desktops'] = [
        {
            '_source': dict(
                idea_session_id='desktop',
                owner='former',
                project_id='project-id',
                created_on=1706832000000,
                stopped_on=1706835600000,
                state='STOPPED',
            )
        }
    ]
    tables, _, _, _ = build(value)
    rows = [row for row in tables['project'] if not row['key'].startswith('!')]
    assert len(rows) == 1
    assert rows[0]['project_id'] == 'project-id'
    assert rows[0]['label'] == 'Project title'
    assert rows[0]['job_count'] == 1
    assert rows[0]['desktop_hours'] == 1


def test_unresolved_project_name_is_distinct_from_historical_id():
    value = data()
    value['jobs'] = [job(project='retired')]
    value['desktops'] = [
        {
            '_source': dict(
                idea_session_id='desktop',
                owner='former',
                project_id='retired',
                created_on=1706832000000,
                stopped_on=1706835600000,
                state='STOPPED',
            )
        }
    ]
    rows = build(value)[0]['project']
    historical = next(row for row in rows if row['key'] == '!historical-name:retired')
    assert historical['label'] == 'Historical project name: retired'
    assert historical['project_id'] is None
    assert any(row['project_id'] == 'retired' for row in rows)
