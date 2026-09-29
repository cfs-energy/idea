"""Insights aggregation, source boundaries, caching and authorization."""

import copy
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ideadatamodel import ReportingPeriodRequest, exceptions
from ideaclustermanager.app.reporting.insights_service import (
    InsightsService,
    ranked,
    budget_row,
)
from ideaclustermanager.app.reporting.reporting_service import resolve_period
from ideaclustermanager.app.api.reporting_api import ReportingAPI
from ideaclustermanager.app.api.my_costs_api import MyCostsAPI
from ideaclustermanagertests.test_reporting_api import invocation


@pytest.fixture(autouse=True)
def currency(monkeypatch):
    monkeypatch.setattr('ideadatamodel.locale.get_currency_code', lambda: 'USD')


def period():
    return resolve_period(request(), 'UTC', datetime(2024, 3, 1, tzinfo=timezone.utc))


def request():
    return ReportingPeriodRequest(
        period='custom', start_date='2024-02-01', end_date='2024-02-29'
    )


def job(identity='1', owner='user-a', cpus=4, used=7200, cost='10'):
    return {
        '_id': identity,
        '_source': dict(
            job_id=identity,
            job_uid=identity,
            owner=owner,
            state='finished',
            project='project-a',
            queue='batch',
            start_time='2024-02-01T00:00:00Z',
            end_time='2024-02-01T01:00:00Z',
            params=dict(
                cpus=cpus, nodes=1, walltime='02:00:00', instance_types=['c7i.large']
            ),
            execution_hosts=[
                dict(
                    execution=dict(runs=[dict(resources_used=dict(cpu_time_secs=used))])
                )
            ],
            estimated_bom_cost=dict(
                total=dict(amount=cost, unit='USD'),
                savings_total=dict(amount='2', unit='USD'),
            ),
        ),
    }


def data():
    return dict(
        jobs=[job(), job('2', 'user-b', cpus=12, used=43200, cost='30')],
        desktops=[],
        projections={},
        storage=[],
        projects={},
        project_names={},
        coverage={},
    )


def build(value=None, username=None, context=None):
    return InsightsService(context or Mock()).build(
        value or data(), period(), 'USD', 'UTC', time.monotonic() + 30, username
    )


def test_mean_weighting_and_waste_cost():
    result = build().jobs
    assert result.count == 2
    assert result.cost == Decimal('40')
    assert result.savings == Decimal('4')
    assert result.cpu_efficiency_pct == 75
    assert result.cpu_efficiency_weighted_pct == 87.5
    assert result.wasted_core_hours == 2
    assert result.wasted_cost == Decimal('5')
    assert result.jobs_with_efficiency == 2
    assert result.by_user[0].name == 'user-b'
    assert result.by_instance_family[0].name == 'c7i'
    assert result.costliest[0].job_id == '2'
    assert result.least_efficient[0].job_id == '1'
    assert result.daily_by_project[0].cost == 40


def test_period_owner_dedup_and_state_filters():
    value = data()
    value['jobs'].append(copy.deepcopy(value['jobs'][0]))
    for identity, end, state in [
        ('3', '2024-03-01T00:00:00Z', 'finished'),
        ('4', '2024-02-01T01:00:00Z', 'running'),
        ('5', '2024-01-31T23:59:59Z', 'finished'),
    ]:
        extra = job(identity)
        extra['_source'].update(end_time=end, state=state)
        value['jobs'].append(extra)
    result = build(value, 'user-a')
    assert result.jobs.count == 1
    assert result.jobs.by_user == []
    assert result.jobs.cost == 10
    assert {row.owner for row in result.jobs.costliest} == {'user-a'}


def test_invalid_efficiency_does_not_contribute_to_weight_or_waste():
    value = data()
    value['jobs'][1]['_source']['execution_hosts'][0]['execution']['runs'][0][
        'resources_used'
    ]['cpu_time_secs'] = 1000000
    result = build(value).jobs
    assert result.cpu_efficiency_pct == result.cpu_efficiency_weighted_pct == 50
    assert result.wasted_core_hours == 2
    assert result.wasted_cost == 5
    assert result.jobs_with_efficiency == 1


def test_top_n_other_and_job_limits():
    groups = {f'group-{i:02}': (Decimal(i), 1) for i in range(20)}
    rows = ranked(groups)
    assert len(rows) == 15
    assert rows[0]['name'] == 'group-19'
    other = next(row for row in rows if row['name'] == 'Other')
    assert other['cost'] == 15
    assert other['count'] == 6
    assert [row['cost'] for row in rows] == sorted(
        (row['cost'] for row in rows), reverse=True
    )
    assert sum(row['share_pct'] for row in rows) == pytest.approx(100)
    value = data()
    value['jobs'] = [job(str(i), cost=str(i)) for i in range(60)]
    result = build(value).jobs
    assert len(result.costliest) == 50
    assert len(result.least_efficient) == 25
    assert result.costliest[0].cost == 59


@pytest.mark.parametrize(
    'forecast,status',
    [
        ('84.99', 'ok'),
        ('85', 'watch'),
        ('99.99', 'watch'),
        ('100', 'over'),
        ('120', 'over'),
        (None, 'ok'),
    ],
)
def test_budget_thresholds(forecast, status):
    value = dict(
        budget_name='budget-a',
        budget_limit=dict(amount='100'),
        actual_spend=dict(amount='20'),
        forecasted_spend=dict(amount=forecast),
    )
    row = budget_row('project-a', value, 'USD')
    assert row['status'] == status
    assert row['headroom'] == (
        100 - Decimal(forecast) if forecast is not None else None
    )
    assert budget_row('project-a', dict(value, is_missing=True), 'USD') is None
    value['budget_limit']['amount'] = 0
    assert budget_row('project-a', value, 'USD')['pct_at_forecast'] is None


def projection(cost):
    points = [
        dict(date='2024-02-01', amount=cost),
        dict(date='2024-01-31', amount='999'),
    ]
    return dict(
        costs=dict(
            currency='USD',
            timezone='UTC',
            current=dict(
                desktops=dict(daily=points), shared_storage=dict(daily=points)
            ),
        )
    )


def test_desktops_storage_scope_and_unknown_tiers():
    value = data()
    value['projections'] = {'user-a': projection('5'), 'user-b': projection('10')}
    value['storage'] = [
        dict(
            date='2024-02-01',
            filesystem_id='fs-a',
            complete=True,
            users={'user-a': 10, 'user-b': 20},
            ssd_bytes=50,
        ),
        dict(
            date='2024-02-02',
            filesystem_id='fs-a',
            complete=True,
            users={'user-a': 15, 'user-b': 25},
            capacity_pool_bytes=80,
        ),
    ]
    result = build(value)
    assert result.desktops.cost == 15
    assert len(result.desktops.daily_top_users) == 2
    assert result.storage.cost == 15
    assert result.storage.used_bytes == 40
    assert result.storage.by_user[0].name == 'user-b'
    assert [(r.tier, r.bytes) for r in result.storage.tier_daily] == [
        ('ssd', 50),
        ('capacity_pool', 80),
    ]
    personal = build(value, 'user-a')
    assert personal.desktops.cost == personal.storage.cost == 5
    assert personal.storage.used_bytes == 15
    assert personal.storage.by_user == personal.storage.tier_daily == []
    assert {p.user for p in personal.desktops.daily_top_users} == {'user-a'}


def test_desktop_history_project_cost_and_overlap():
    value = data()
    value['desktops'] = [
        {
            '_history': True,
            '_source': dict(
                idea_session_id='session-a',
                owner='user-a',
                project_id='project-a',
                instance_type='c7i.large',
                created_on='2024-01-31T23:00:00Z',
                stopped_on='2024-02-01T02:00:00Z',
                deleted_on='2024-02-01T03:00:00Z',
            ),
        }
    ]
    context = Mock()
    context.aws_util().get_ec2_instance_type_unit_price.return_value = SimpleNamespace(
        ondemand=2
    )
    result = build(value, context=context)
    assert result.desktops.hours == 2
    assert result.desktops.by_project[0].cost == 4


def test_cache_scope_expiry_membership_and_budget_filter(monkeypatch):
    context, sources = Mock(), Mock()
    context.config().get_string.return_value = 'UTC'
    project = Mock()
    project.model_dump.return_value = dict(
        project_id='project-a', name='project-a', budget={'budget_name': 'budget-a'}
    )
    context.projects.get_user_projects.return_value = SimpleNamespace(
        projects=[project]
    )
    context.aws_util().budgets_get_budget.return_value.model_dump.return_value = dict(
        budget_name='budget-a',
        budget_limit=dict(amount=100),
        actual_spend=dict(amount=10),
        forecasted_spend=dict(amount=90),
    )
    sources.read.return_value = dict(
        data(),
        project_records=[
            dict(project_id='project-b', budget={'budget_name': 'budget-b'})
        ],
    )
    service = InsightsService(context, sources)
    result = service.get_insights(request(), lambda: True, 'user-a')
    assert [r.project for r in result.budgets] == ['project-a']
    context.aws_util().budgets_get_budget.assert_called_once_with(
        budget_name='budget-a'
    )
    result.jobs.count = 999
    assert service.get_insights(request(), lambda: True, 'user-a').jobs.count == 1
    assert sources.read.call_count == 1
    service.get_insights(request(), lambda: True, 'user-b')
    assert sources.read.call_count == 2
    context.projects.get_user_projects.return_value = SimpleNamespace(projects=[])
    assert service.get_insights(request(), lambda: True, 'user-a').budgets == []
    assert sources.read.call_count == 3
    for key, (_, cached) in list(service._cache.items()):
        service._cache[key] = (0, cached)
    service.get_insights(request(), lambda: True, 'user-a')
    assert sources.read.call_count == 4
    with pytest.raises(exceptions.SocaException):
        service.get_insights(request(), lambda: False, 'user-a')
    assert sources.read.call_count == 4


@pytest.mark.parametrize('personal', [False, True])
def test_api_auth_and_identity(personal):
    cls = MyCostsAPI if personal else ReportingAPI
    api = cls.__new__(cls)
    api.insights = Mock()
    api.insights.get_insights.return_value = build(
        username='reader' if personal else None
    )
    call = invocation(
        'MyCosts.GetInsights' if personal else 'Reporting.GetInsights',
        allowed=True,
        payload=request().model_dump(),
    )
    call.is_authorized_user = lambda: True
    api.invoke(call)
    if personal:
        assert api.insights.get_insights.call_args.kwargs['username'] == 'reader'
        payload = call.success.call_args.args[0]
        assert all(
            'by_user' not in payload[section]
            for section in ('jobs', 'desktops', 'storage')
        )
    call.is_authenticated_user = lambda: False
    with pytest.raises(exceptions.SocaException):
        api.invoke(call)
    call.is_authenticated_user = lambda: True
    call.request_payload['username'] = 'user-b'
    with pytest.raises(exceptions.SocaException):
        api.invoke(call)


def test_reporting_denies_non_reporting_user():
    api = ReportingAPI.__new__(ReportingAPI)
    api.insights = Mock()
    with pytest.raises(exceptions.SocaException):
        api.invoke(
            invocation('Reporting.GetInsights', payload={'period': 'this_month'})
        )
    api.insights.get_insights.assert_not_called()


def test_storage_share_costs_cover_older_dates_and_do_not_add_projection_twice():
    value = data()
    value['projections'] = {'user-a': projection('99')}
    value['storage'] = [
        dict(
            date='2024-02-01',
            filesystem_id='fs-a',
            complete=True,
            users={'user-a': 20, 'user-b': 30},
            total_bytes=100,
            daily_rate='10',
        ),
        dict(
            date='2024-02-02',
            filesystem_id='fs-a',
            complete=True,
            users={'user-a': 40, 'user-b': 10},
            total_bytes=100,
            daily_rate='10',
        ),
        dict(
            date='2024-01-31',
            filesystem_id='fs-a',
            complete=True,
            users={'user-a': 100},
            total_bytes=100,
            daily_rate='1000',
        ),
    ]
    result = build(value)
    assert result.storage.cost == 10
    assert result.storage.used_bytes == 50
    assert result.storage.by_user[0].cost == 6
    assert build(value, 'user-a').storage.cost == 6


def test_daily_project_dates_use_cluster_timezone():
    value = data()
    selected = resolve_period(
        request(), 'America/New_York', datetime(2024, 3, 1, 12, tzinfo=timezone.utc)
    )
    value['jobs'][0]['_source'].update(
        start_time='2024-02-02T00:00:00Z', end_time='2024-02-02T01:00:00Z'
    )
    result = InsightsService(Mock()).build(
        value, selected, 'USD', 'America/New_York', time.monotonic() + 30
    )
    assert result.jobs.count == 1
    assert result.jobs.daily_by_project[0].date.isoformat() == '2024-02-01'


def test_desktop_top_ten_users_uses_period_totals():
    value = data()
    value['projections'] = {f'user-{i:02}': projection(str(i)) for i in range(12)}
    result = build(value)
    assert len(result.desktops.daily_top_users) == 10
    assert {p.user for p in result.desktops.daily_top_users} == {
        f'user-{i:02}' for i in range(2, 12)
    }


def test_budget_sort_missing_and_shared_budget_fetch_once():
    context = Mock()
    values = [
        dict(
            budget_name='budget-a',
            budget_limit=dict(amount=100),
            actual_spend=dict(amount=10),
            forecasted_spend=dict(amount=90),
        ),
        dict(
            budget_name='budget-b',
            budget_limit=dict(amount=100),
            actual_spend=dict(amount=10),
            forecasted_spend=dict(amount=120),
        ),
        dict(budget_name='budget-c', is_missing=True),
    ]
    context.aws_util().budgets_get_budget.side_effect = [
        Mock(model_dump=Mock(return_value=v)) for v in values
    ]
    projects = [
        dict(project_id=f'project-{i}', budget=dict(budget_name=name))
        for i, name in enumerate(['budget-a', 'budget-b', 'budget-c', 'budget-a'])
    ]
    rows = InsightsService(context).budgets(projects, 'USD', time.monotonic() + 30)
    assert [r.pct_at_forecast for r in rows] == [120, 90, 90]
    assert context.aws_util().budgets_get_budget.call_count == 3


def test_sources_fetch_retained_storage_and_scope_job_query():
    import json
    from ideadatamodel import constants
    from ideaclustermanager.app.reporting.reporting_sources import ReportingSources

    context = Mock()
    context.config().is_module_enabled.side_effect = (
        lambda module: module == constants.MODULE_SCHEDULER
    )
    context.accounts.list_users.return_value = SimpleNamespace(
        listing=[], paginator=None
    )
    context.projects.list_projects.return_value = SimpleNamespace(
        listing=[], paginator=None
    )
    context.personal_costs_store.table.scan.return_value = {'Items': []}
    context.personal_costs_store.records.return_value = [
        dict(
            record='share:2024-02-01:fs-a',
            payload=json.dumps(dict(filesystem_id='fs-a', total_bytes=100)),
        ),
        dict(record='share:2024-01-31:fs-a', payload='{}'),
    ]
    context.personal_costs_store.resolve_source.side_effect = (
        lambda subject, value: value
    )
    sources = ReportingSources(context)
    sources.search = Mock(return_value=[])
    result = sources.read(
        period(), time.monotonic() + 30, username='user-a', insights=True
    )
    query = sources.search.call_args.args[1]
    assert {'term': {'owner.raw': 'user-a'}} in query['bool']['filter']
    assert {'params', 'execution_hosts', 'estimated_bom_cost'} <= set(
        sources.search.call_args.kwargs['fields']
    )
    assert result['storage'] == [
        dict(date='2024-02-01', filesystem_id='fs-a', total_bytes=100)
    ]


def test_missing_job_cost_stays_null_and_excludes_cost_rankings():
    value = data()
    value['jobs'] = [job()]
    value['jobs'][0]['_source']['estimated_bom_cost']['price_unavailable'] = True
    result = build(value).jobs
    assert result.count == 1
    assert result.cost is result.savings is result.wasted_cost is None
    assert result.costliest == result.by_queue == []
    assert len(result.least_efficient) == 1


def test_system_users_group_before_ranking_without_affecting_personal_scope():
    value = data()
    owners = ['root', 'uid:1001', '1002', 'scientist-a']
    value['jobs'] = [
        job(str(i), owner=owner, cost='10') for i, owner in enumerate(owners)
    ]
    value['projections'] = {owner: projection('5') for owner in owners}
    value['storage'] = [
        dict(
            date='2024-02-02',
            filesystem_id='fs-a',
            complete=True,
            users={owner: 10 for owner in owners},
        )
    ]
    result = build(value)
    assert [(r.name, r.cost, r.count) for r in result.jobs.by_user] == [
        ('System', 30, 3),
        ('scientist-a', 10, 1),
    ]
    assert result.desktops.by_user[0].name == 'System'
    assert result.desktops.by_user[0].cost == 15
    assert result.storage.by_user[0].name == 'System'
    assert result.storage.by_user[0].bytes == 30
    personal = build(value, 'root')
    assert personal.jobs.count == 1
    assert personal.jobs.costliest[0].owner == 'root'
    assert personal.jobs.costliest[0].requested_cores == 4
    assert personal.jobs.costliest[0].used_cores == 2


def test_storage_total_matches_latest_measured_tiers():
    value = data()
    value['storage'] = [
        dict(
            date='2024-02-02',
            filesystem_id='fs-a',
            complete=True,
            users={'scientist-a': 10},
            ssd_bytes=20,
            capacity_pool_bytes=30,
        )
    ]
    result = build(value)
    assert (
        result.storage.used_bytes
        == sum(row.bytes for row in result.storage.tier_daily)
        == 50
    )
