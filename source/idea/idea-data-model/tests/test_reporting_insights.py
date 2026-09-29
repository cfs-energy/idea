"""Efficiency formulas and the reporting wire contract."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ideadatamodel import ReportingInsights, ReportingPeriodRequest
from ideadatamodel.reporting.efficiency import (
    job_efficiency,
    allocation,
    memory_bytes,
    seconds,
)


def job(cpus=4, nodes=1, used=7200):
    return dict(
        start_time='2024-02-01T00:00:00Z',
        end_time='2024-02-01T01:00:00Z',
        params=dict(
            cpus=cpus,
            nodes=nodes,
            walltime='02:00:00',
            memory=dict(value=8, unit='gib'),
        ),
        execution_hosts=[
            dict(
                execution=dict(
                    runs=[
                        dict(
                            resources_used=dict(
                                cpu_time_secs=used, memory=dict(value=4, unit='gib')
                            )
                        )
                    ]
                )
            )
        ],
    )


def test_single_node_formulas():
    result = job_efficiency(job())
    assert result == dict(
        cpu_efficiency_pct=50,
        memory_efficiency_pct=50,
        walltime_efficiency_pct=50,
        core_hours=4,
        wasted_core_hours=2,
        elapsed_hours=1,
        nodes=1,
    )


def test_multi_node_without_select():
    result = job_efficiency(job(nodes=2))
    assert result['cpu_efficiency_pct'] == 25
    assert result['wasted_core_hours'] == 6


def test_select_heterogeneous_chunks_and_multiple_runs():
    value = job()
    value['params']['custom_params'] = {
        'select': '2:ncpus=4:mem=8gib+1:ncpus=8:mem=16gib'
    }
    value['execution_hosts'] *= 2
    result = job_efficiency(value)
    assert result['core_hours'] == 16
    assert result['cpu_efficiency_pct'] == 25
    assert result['nodes'] == 3
    assert result['memory_efficiency_pct'] == 12.5


@pytest.mark.parametrize(
    'used,expected',
    [(0, 0), (14400, 100), (15120, 100), (15121, None), (None, None), (-1, None)],
)
def test_cpu_zero_missing_and_bad_ratios(used, expected):
    assert job_efficiency(job(used=used))['cpu_efficiency_pct'] == expected


@pytest.mark.parametrize(
    'params',
    [
        {},
        {'cpus': 0},
        {'cpus': 4, 'nodes': 0},
        {'custom_params': {'select': 'broken'}},
        {'custom_params': {'select': '2:mem=8gb'}},
    ],
)
def test_missing_allocated_cpu(params):
    value = job()
    value['params'] = params
    assert job_efficiency(value)['cpu_efficiency_pct'] is None


def test_missing_memory_and_walltime_requests():
    value = job()
    del value['params']['memory']
    del value['params']['walltime']
    result = job_efficiency(value)
    assert result['memory_efficiency_pct'] is None
    assert result['walltime_efficiency_pct'] is None


def test_job_wide_memory_is_peak_not_sum_of_hosts_or_runs():
    value = job()
    value['execution_hosts'] *= 3
    value['execution_hosts'][0]['execution']['runs'] *= 2
    assert job_efficiency(value)['memory_efficiency_pct'] == 50


@pytest.mark.parametrize('field', ['start_time', 'end_time'])
def test_missing_duration(field):
    value = job()
    del value[field]
    assert job_efficiency(value)['cpu_efficiency_pct'] is None
    value['total_time_secs'] = 3600
    assert job_efficiency(value)['cpu_efficiency_pct'] == 50


@pytest.mark.parametrize(
    'wall,expected',
    [
        ('1-02:03:04', 93784),
        ('01:60:00', None),
        ('01:00:60', None),
        ('invalid', None),
        (3600, 3600),
    ],
)
def test_walltime_parsing(wall, expected):
    assert seconds(wall) == expected


def test_memory_units_and_implicit_select_count():
    assert memory_bytes('1gib') == 1024**3
    assert memory_bytes(dict(value=1, unit='gb')) == 1000**3
    assert allocation(dict(custom_params={'select': 'ncpus=4:mem=8gb'})) == (
        4,
        8 * 1000**3,
        1,
    )


def test_wire_contract_money_nulls_dates_and_strict_request():
    result = ReportingInsights(
        period=dict(start='2024-02-01', end='2024-02-29', label='February'),
        currency='USD',
        updated_at=datetime.now(timezone.utc),
        jobs=dict(cost=Decimal('12.34')),
    )
    wire = result.model_dump(mode='json')
    assert set(wire) == {
        'period',
        'currency',
        'updated_at',
        'jobs',
        'desktops',
        'storage',
        'budgets',
        'notes',
    }
    assert set(wire['period']) == {'start', 'end', 'label'}
    assert wire['jobs']['cost'] == '12.34'
    assert wire['jobs']['wasted_cost'] is None
    assert wire['period']['start'] == '2024-02-01'
    with pytest.raises(ValidationError):
        ReportingPeriodRequest(period='this_month', username='user-b')
    with pytest.raises(ValidationError):
        ReportingInsights.model_validate(dict(result.model_dump(), notes=['one'] * 4))


@pytest.mark.parametrize('used_cpus,expected', [(4, 50), (8, 25)])
def test_memory_host_samples_and_repeated_job_totals(used_cpus, expected):
    import copy

    value = job()
    value['params']['custom_params'] = {'select': '2:ncpus=4:mem=8gib'}
    used = value['execution_hosts'][0]['execution']['runs'][0]['resources_used']
    used['cpus'] = used_cpus
    value['execution_hosts'].append(copy.deepcopy(value['execution_hosts'][0]))
    assert job_efficiency(value)['memory_efficiency_pct'] == expected


def test_nullable_nodes_matches_legacy_single_node_records():
    assert job_efficiency(job(nodes=None))['cpu_efficiency_pct'] == 50


def test_incomplete_host_memory_does_not_understate_job_usage():
    value = job()
    value['params']['custom_params'] = {'select': '2:ncpus=4:mem=8gib'}
    value['execution_hosts'][0]['execution']['runs'][0]['resources_used']['cpus'] = 4
    assert job_efficiency(value)['memory_efficiency_pct'] is None
