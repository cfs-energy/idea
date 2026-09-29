"""Personal costs: real calculations with local billing, inventory and price-list fakes."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import time

import arrow
import pytest

from ideadatamodel import (
    locale,
    MyCostsAi,
    MyCostsJobs,
    MyCostsDesktops,
    GetMyCostsResult,
    MyCostsAmount,
    MyCostsMonth,
)
from ideaclustermanager.app.costs.monthly_costs_service import (
    MonthlyCostsService,
    CACHE_SECONDS,
    period_bounds,
)
from ideaclustermanager.app.metrics.storage_metrics_service import StorageMetricsService
from ideasdk.aws.ec2_price_list import parse_ec2_offer_csv

START = arrow.get('2026-09-01')
END = arrow.get('2026-09-16')


class Config:
    def __init__(self):
        self.entries = {
            'data': {
                'provider': 'efs',
                'efs': {'file_system_id': 'fs-test'},
            }
        }

    def get_config(self, key, default=None):
        return self.entries if key == 'shared-storage' else default

    def get_string(self, key, default=None):
        return {'cluster.aws.region': 'us-east-1'}.get(key, default)

    def get_float(self, key, default=None):
        return default


class Billing:
    def __init__(self):
        self.calls = []
        self.groups = []

    def get_cost_and_usage(self, **request):
        self.calls.append(request.copy())
        return {
            'ResultsByTime': [
                {
                    'Groups': [
                        {
                            'Keys': [key],
                            'Metrics': {
                                'UnblendedCost': {'Amount': amount, 'Unit': 'USD'}
                            },
                        }
                        for key, amount in self.groups
                    ]
                }
            ]
        }


class EC2:
    def __init__(self):
        self.calls = []
        self.instances = [
            {'InstanceId': 'i-test-running', 'State': {'Name': 'running'}},
            {'InstanceId': 'i-test-stopped', 'State': {'Name': 'stopped'}},
        ]
        self.volumes = [
            {
                'VolumeId': 'vol-test-running',
                'VolumeType': 'gp3',
                'Size': 100,
                'CreateTime': START.datetime,
                'Attachments': [{'InstanceId': 'i-test-running'}],
            },
            {
                'VolumeId': 'vol-test-stopped',
                'VolumeType': 'gp2',
                'Size': 200,
                'CreateTime': START.shift(days=5).datetime,
                'Attachments': [{'InstanceId': 'i-test-stopped'}],
            },
        ]

    def get_paginator(self, operation):
        def paginate(**request):
            self.calls.append((operation, request))
            if operation == 'describe_instances':
                # Multiple pages ensure no desktop disappears at a page boundary.
                return [
                    {'Reservations': [{'Instances': [instance]}]}
                    for instance in self.instances
                ]
            return [{'Volumes': [volume]} for volume in self.volumes]

        return SimpleNamespace(paginate=paginate)


class Pricing:
    def __init__(self):
        self.calls = []

    def get_products(self, **request):
        self.calls.append(request)
        volume_type = next(
            f['Value'] for f in request['Filters'] if f['Field'] == 'volumeApiName'
        )
        price = {'gp3': '0.08', 'gp2': '0.10'}[volume_type]
        return {
            'PriceList': [
                json.dumps(
                    {
                        'terms': {
                            'OnDemand': {
                                'sku': {
                                    'priceDimensions': {
                                        'rate': {
                                            'unit': 'GB-Mo',
                                            'pricePerUnit': {'USD': price},
                                        }
                                    }
                                }
                            }
                        }
                    }
                )
            ]
        }


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(locale, 'get_currency_code', lambda: 'USD')
    config, billing, ec2, pricing = Config(), Billing(), EC2(), Pricing()
    aws = SimpleNamespace(
        cost_explorer=lambda: billing,
        ec2=lambda: ec2,
        pricing=lambda: pricing,
        aws_partition=lambda: 'aws',
    )
    snapshot = {
        'fs-test': {
            'measured_at': arrow.utcnow().timestamp(),
            'users': {'user-a': 100, 'user-b': 300, 'user-empty': 0},
        }
    }
    context = SimpleNamespace(
        config=lambda: config,
        aws=lambda: aws,
        cluster_name=lambda: 'test',
        logger=lambda _: Mock(),
        service_registry=lambda: Mock(),
        module_id=lambda: 'cluster-manager',
        storage_metrics=SimpleNamespace(usage_by_filesystem=lambda: snapshot),
    )
    rate = Mock(return_value=8)
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.monthly_costs_service.daily_storage_rate', rate
    )
    result = MonthlyCostsService(context)
    result.fakes = SimpleNamespace(
        config=config,
        billing=billing,
        ec2=ec2,
        pricing=pricing,
        snapshot=snapshot,
        rate=rate,
    )
    yield result
    result._workers.shutdown(wait=True)


def test_storage_shares_and_missing_user(service):
    first, missing = service._storage('user-a', START, END)
    second, _ = service._storage('user-b', START, END)
    empty, _ = service._storage('user-empty', START, END)
    absent, missing_user = service._storage('user-absent', START, END)
    assert not missing
    assert first[0].cost == 30
    assert second[0].cost == 90
    assert empty[0].cost == 0  # measured zero differs from no measurement
    assert (
        absent[0].cost is None and absent[0].status == 'no_usage_data' and missing_user
    )
    assert service.fakes.billing.calls == []
    assert service.fakes.rate.call_args.args[1:3] == ('efs', 'fs-test')
    assert service.fakes.rate.call_args.kwargs['capacity_pool_bytes'] == 0


@pytest.mark.parametrize('exchange_rate,expected', [(0.9, 27.0), (None, None)])
def test_storage_share_uses_cluster_currency(
    service, monkeypatch, exchange_rate, expected
):
    monkeypatch.setattr(locale, 'get_currency_code', lambda: 'EUR')
    service.fakes.config.get_float = lambda key, default=None: exchange_rate
    rows, unavailable = service._storage('user-a', START, END)
    assert rows[0].cost == expected
    assert unavailable is (expected is None)
    assert rows[0].status == ('unavailable' if expected is None else 'ready')


def test_storage_default_quota_rule_and_missing_rate(service):
    service.fakes.snapshot['fs-test']['zero_when_absent'] = True
    rows, unavailable = service._storage('user-absent', START, END)
    assert not unavailable and rows[0].cost == 0 and rows[0].used_bytes == 0
    service.fakes.rate.return_value = None
    rows, unavailable = service._storage('user-a', START, END)
    assert unavailable and rows[0].cost is None and rows[0].status == 'unavailable'


def test_storage_uses_snapshot_denominator_and_capacity_pool(service):
    service.fakes.snapshot['fs-test'].update(total_bytes=1000, capacity_pool_bytes=9000)
    rows, unavailable = service._storage('user-a', START, END)
    assert not unavailable and rows[0].cost == 12
    assert service.fakes.rate.call_args.kwargs['capacity_pool_bytes'] == 9000


def test_storage_without_any_usage_is_not_zero(service, monkeypatch):
    service.fakes.snapshot.clear()
    monkeypatch.setattr(service, '_home_usage', lambda: None)
    rows, unavailable = service._storage('user-a', START, END)
    assert unavailable and rows[0].cost is None
    assert rows[0].status == 'no_usage_data'


def test_home_fallback_uses_complete_measurements_for_all_users(service):
    service.fakes.snapshot.clear()
    service.context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[
                SimpleNamespace(username='user-a'),
                SimpleNamespace(username='user-b'),
            ],
            paginator=None,
        )
    )
    service.context.storage_usage = SimpleNamespace(
        measure_for_costs=lambda username: {
            'state': 'ready',
            'partial': False,
            'total': {'bytes': 10 if username == 'user-a' else 30},
        }
    )
    rows, missing = service._storage('user-a', START, END)
    assert not missing and rows[0].cost == 30


def test_partial_home_scan_cannot_be_used_as_denominator(service):
    service.context.accounts = SimpleNamespace(
        list_users=lambda request: SimpleNamespace(
            listing=[SimpleNamespace(username='user-a')], paginator=None
        )
    )
    service.context.storage_usage = SimpleNamespace(
        measure_for_costs=lambda username: {
            'state': 'ready',
            'partial': True,
            'total': {'bytes': 10},
        }
    )
    assert service._home_usage() is None


def test_disk_proration_and_stopped_desktop_are_counted(service):
    rows, missing = service._disks('user-a', START, END)
    assert not missing and len(rows) == 2  # volumes from both queries are deduplicated
    assert rows[0].state == 'running' and rows[0].cost == 4.0  # 100 × .08 × 15/30
    assert rows[1].state == 'stopped' and rows[1].cost == pytest.approx(
        6.6667
    )  # 200 × .10 × 10/30
    assert {row.volume_type for row in rows} == {'gp2', 'gp3'}
    filters = service.fakes.ec2.calls[0][1]['Filters']
    assert {'Name': 'tag:idea:JobOwner', 'Values': ['user-a']} in filters
    assert {'Name': 'tag:idea:ClusterName', 'Values': ['test']} in filters
    assert not any(f['Name'] == 'instance-state-name' for f in filters)
    service._disks('user-a', START, END)
    assert len(service.fakes.pricing.calls) == 2
    assert len(service.fakes.ec2.calls) == 3


def test_disk_month_length_creation_and_previous_month(service):
    volume = service.fakes.ec2.volumes[0]
    volume['CreateTime'] = arrow.get('2026-08-01').datetime
    rows, _ = service._disks('user-a', START.shift(months=-1), START)
    assert len(rows) == 1 and rows[0].cost == 8.0


@pytest.mark.parametrize(
    'period,now,expected',
    [
        ('wtd', '2026-09-23T12:00:00Z', '2026-09-21T00:00:00-04:00'),
        ('qtd', '2026-10-01T04:30:00Z', '2026-10-01T00:00:00-04:00'),
        ('ytd', '2026-01-01T05:30:00Z', '2026-01-01T00:00:00-05:00'),
    ],
)
def test_period_boundaries_use_cluster_timezone(period, now, expected):
    start, _ = period_bounds(period, arrow.get(now), 'America/New_York')
    assert start.isoformat() == expected


def test_ticker_disabled_does_not_schedule_work(service, monkeypatch):
    monkeypatch.setattr(
        service.fakes.config, 'get_bool', lambda *args: False, raising=False
    )
    submitted = []
    monkeypatch.setattr(
        service._workers, 'submit', lambda *args: submitted.append(args)
    )
    result = service.get_ticker('user-a')
    assert result.model_dump(exclude_none=True) == {'enabled': False}
    assert submitted == []


def test_mtd_ticker_reuses_billboard_cache(service, monkeypatch):
    monkeypatch.setattr(
        service.fakes.config, 'get_bool', lambda *args: True, raising=False
    )
    monkeypatch.setattr(
        service.fakes.config,
        'get_string',
        lambda key, default=None: {
            'cluster-manager.web_portal.cost_ticker.period': 'mtd'
        }.get(key, default),
    )
    line = MyCostsAmount(cost=1, status='ready')
    month = MyCostsMonth(
        start_date='2026-09-01',
        end_date='2026-09-21',
        total=5,
        jobs=line,
        desktops=line,
        desktop_disks=line,
        shared_storage=line,
        ai=line,
    )
    key = ('user-a', arrow.utcnow().format('YYYY-MM'))
    service._cache[key] = (
        __import__('time').monotonic(),
        GetMyCostsResult(
            currency='USD',
            state='ready',
            refreshed_at='2026-09-21T12:00:00Z',
            current=month,
        ),
    )
    monkeypatch.setattr(
        service, '_month', lambda *args: pytest.fail('cache should be reused')
    )
    result = service.get_ticker('user-a')
    assert (result.period, result.total, result.currency) == ('MTD', 5, 'USD')


def test_missing_disk_price_is_unavailable(service, monkeypatch):
    monkeypatch.setattr(service, '_volume_price', lambda _: None)
    rows, unavailable = service._disks('user-a', START, END)
    assert unavailable and all(row.cost is None for row in rows)


def test_currency_conversion_is_explicit(service, monkeypatch):
    monkeypatch.setattr(locale, 'get_currency_code', lambda: 'EUR')
    with pytest.raises(ValueError):
        service._convert(1)
    monkeypatch.setattr(service.fakes.config, 'get_float', lambda *args: 0.9)
    rows, unavailable = service._disks('user-a', START, END)
    assert not unavailable and rows[0].cost == 3.6


def test_cold_cache_is_nonblocking_single_flight_and_user_scoped(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def month(username, start, end):
        calls.append(username)
        entered.set()
        assert release.wait(3)
        return None

    monkeypatch.setattr(service, '_month', month)
    try:
        assert service.get_costs('user-a').state == 'computing'
        assert entered.wait(1)
        assert service.get_costs('user-a').state == 'computing'
        assert len(calls) == 1
    finally:
        release.set()
    service._workers.shutdown(wait=True)
    assert service.get_costs('user-a').state == 'ready'
    assert calls == ['user-a', 'user-a']  # two calendar months, one refresh
    assert ('user-b', arrow.utcnow().format('YYYY-MM')) not in service._cache


def test_stale_cache_returns_immediately_and_schedules_one_refresh(
    service, monkeypatch
):
    month = arrow.utcnow().format('YYYY-MM')
    result = GetMyCostsResult(currency='USD', state='ready')
    # A fresh runner's monotonic clock can read below an hour; age the entry relative to it.
    service._cache[('user-a', month)] = (time.monotonic() - CACHE_SECONDS - 1, result)
    calls = []
    monkeypatch.setattr(service._workers, 'submit', lambda *args: calls.append(args))
    assert service.get_costs('user-a').state == 'refreshing'
    assert service.get_costs('user-a').state == 'refreshing'
    assert len(calls) == 1
    assert CACHE_SECONDS >= 3600


def test_cache_expires_after_an_hour_and_failures_are_cached(service, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        'ideaclustermanager.app.costs.monthly_costs_service.time.monotonic',
        lambda: clock[0],
    )
    calls = []

    def read():
        calls.append(1)
        raise RuntimeError('unavailable')

    assert service._remember('test', read) is None
    clock[0] += 3599
    assert service._remember('test', read) is None
    assert len(calls) == 1
    clock[0] += 1
    service._remember('test', read)
    assert len(calls) == 2


def test_job_compute_excludes_disks_and_scratch(service, monkeypatch):
    monkeypatch.setattr(service, '_jobs_index', lambda: 'test-index')
    monkeypatch.setattr(
        service,
        '_search',
        lambda *args: {
            'hits': {
                'hits': [
                    {
                        '_source': {
                            'job_id': 'job-test',
                            'estimated_bom_cost': {
                                'total': {'amount': 999},
                                'line_items': [
                                    {
                                        'service': 'aws.ec2',
                                        'total_price': {'amount': 10, 'unit': 'USD'},
                                    },
                                    {
                                        'service': 'aws.ebs',
                                        'total_price': {'amount': 20, 'unit': 'USD'},
                                    },
                                    {
                                        'service': 'aws.fsx',
                                        'total_price': {'amount': 30, 'unit': 'USD'},
                                    },
                                ],
                            },
                        }
                    }
                ]
            }
        },
    )
    jobs = service._compute_jobs('user-a', START, END)
    assert jobs.cost == 10 and jobs.job_count == 1 and jobs.recent_jobs[0].cost == 10


def test_total_is_marked_partial_when_storage_has_no_usage(service, monkeypatch):
    monkeypatch.setattr(service, '_compute_jobs', lambda *args: MyCostsJobs(cost=10))
    monkeypatch.setattr(service, '_desktops', lambda *args: MyCostsDesktops(cost=20))
    monkeypatch.setattr(service, '_ai', lambda *args: MyCostsAi(cost=5))
    month = service._month('user-absent', START, END)
    assert month.incomplete and month.shared_storage.cost is None
    assert month.shared_storage.status == 'no_usage_data'
    assert month.total == pytest.approx(45.6667)


def test_public_price_list_also_reads_volume_prices():
    lines = ['metadata'] * 5 + [
        'Region Code,TermType,Product Family,Unit,Volume API Name,PricePerUnit',
        'us-east-1,OnDemand,Storage,GB-Mo,gp3,0.08',
        'us-east-1,OnDemand,Storage,IOPS-Mo,gp3,0.005',
        'us-west-2,OnDemand,Storage,GB-Mo,gp3,0.09',
    ]
    prices = {}
    parse_ec2_offer_csv(lines, 'us-east-1', prices)
    assert prices == {'gp3': 0.08}


def test_collector_snapshot_sums_volumes_per_filesystem(service):
    collector = StorageMetricsService(service.context)
    collector.targets = lambda: [
        SimpleNamespace(name='data', endpoint='svm-test.fs-test.fsx.example')
    ]
    collector._quota_reports['data'] = (
        123,
        [
            {
                'type': 'user',
                'users': [{'name': 'user-a'}],
                'volume': {'name': volume},
                'space': {'used': {'total': used}},
            }
            for volume, used in [('volume-a', 100), ('volume-b', 200)]
        ],
    )
    assert collector.usage_by_filesystem()['fs-test']['users'] == {'user-a': 300}


def test_real_config_tree_with_no_optional_billing_settings(service):
    from pyhocon import ConfigFactory

    service.fakes.config.entries = ConfigFactory.from_dict(
        {'data': {'provider': 'efs', 'efs': {'file_system_id': 'fs-test'}}}
    )
    rows, unavailable = service._storage('user-a', START, END)
    assert not unavailable and rows[0].cost == 30


def test_stored_collector_snapshot_preserves_user_keys(service, monkeypatch):
    from pyhocon import ConfigFactory

    stored = ConfigFactory.from_dict(
        {
            'snapshot': json.dumps(
                {
                    'fs-test': {
                        'measured_at': 123,
                        'users': {'user.a': 100, 'user-b': 300},
                    }
                }
            )
        }
    )
    monkeypatch.setattr(
        service.fakes.config, 'get_string', lambda *args: stored.get_string('snapshot')
    )
    collector = StorageMetricsService(service.context)
    collector.targets = lambda: []
    assert collector.usage_by_filesystem()['fs-test']['users'] == {
        'user.a': 100,
        'user-b': 300,
    }
