"""
Test Cases for the cost metrics collector
Cost Explorer is a recording stub; every request is billed, so what is asked is asserted
alongside what comes out. Nothing reaches AWS.
"""

from ideaclustermanager.app.metrics.cost_metrics_service import (
    CostExplorerReader,
    CostMetricsService,
    CostRow,
    FAMILY,
    FAMILY_BY_SERVICE,
    FAMILY_STORAGE,
    aggregate,
    strip_tag_key,
    tag_value,
)
from ideaclustermanagertests.metrics_fakes import FakeContext

import arrow

MODULE_TAG = 'idea:ModuleId'


def metric(amortized, unblended):
    return {
        'AmortizedCost': {'Amount': str(amortized)},
        'UnblendedCost': {'Amount': str(unblended)},
    }


def result(day, groups):
    return {'TimePeriod': {'Start': day, 'End': day}, 'Groups': groups}


class StubCostExplorer:
    """answers by the module in the filter, the grouping, and the page."""

    def __init__(self, modules, responses):
        self.modules = modules
        self.responses = responses
        self.requests = []

    def get_tags(self, **kwargs):
        self.requests.append(('GetTags', kwargs))
        token = kwargs.get('NextPageToken')
        if token is None:
            return {'Tags': self.modules[:1], 'NextPageToken': 'page2'}
        return {'Tags': self.modules[1:]}

    def get_cost_and_usage(self, **kwargs):
        self.requests.append(('GetCostAndUsage', kwargs))
        selector = self._selector(kwargs)
        pages = self.responses.get(selector, [{'ResultsByTime': []}])
        index = int(kwargs.get('NextPageToken', 0))
        page = dict(pages[index])
        if index + 1 < len(pages):
            page['NextPageToken'] = str(index + 1)
        return page

    @staticmethod
    def _selector(kwargs):
        expression = kwargs.get('Filter')
        groups = tuple(g['Key'] for g in kwargs['GroupBy'])
        if expression is None:
            return ('all', groups)
        tags = expression.get('Tags')
        if tags is not None:
            if 'MatchOptions' in tags:
                return ('untagged', groups)
            return (tags['Values'][0], groups)
        return ('storage', groups)


def responses():
    project_owner = ('idea:Project', 'idea:JobOwner')
    return {
        ('scheduler', project_owner): [
            {
                'ResultsByTime': [
                    result(
                        '2026-09-13',
                        [
                            {
                                'Keys': ['idea:Project$r-and-d', 'idea:JobOwner$alice'],
                                'Metrics': metric(10, 12),
                            },
                            {
                                'Keys': ['idea:Project$', 'idea:JobOwner$'],
                                'Metrics': metric(0, 0),
                            },
                        ],
                    )
                ]
            },
            {
                'ResultsByTime': [
                    result(
                        '2026-09-14',
                        [
                            {
                                'Keys': ['idea:Project$r-and-d', 'idea:JobOwner$alice'],
                                'Metrics': metric(5, 6),
                            },
                        ],
                    )
                ]
            },
        ],
        ('vdc', project_owner): [
            {
                'ResultsByTime': [
                    result(
                        '2026-09-13',
                        [
                            {
                                'Keys': [
                                    'idea:Project$Design Team',
                                    'idea:JobOwner$Bob',
                                ],
                                'Metrics': metric(3, 3),
                            },
                        ],
                    )
                ]
            },
        ],
        ('untagged', project_owner): [
            {
                'ResultsByTime': [
                    result(
                        '2026-09-13',
                        [
                            {
                                'Keys': ['idea:Project$', 'idea:JobOwner$'],
                                'Metrics': metric(-54, 20),
                            },
                        ],
                    )
                ]
            },
        ],
        ('all', (MODULE_TAG, 'SERVICE')): [
            {
                'ResultsByTime': [
                    result(
                        '2026-09-13',
                        [
                            {
                                'Keys': [
                                    'idea:ModuleId$scheduler',
                                    'Amazon Elastic Compute Cloud - Compute',
                                ],
                                'Metrics': metric(10, 12),
                            },
                        ],
                    )
                ]
            },
        ],
        ('storage', ('SERVICE', 'USAGE_TYPE')): [
            {
                'ResultsByTime': [
                    result(
                        '2026-09-13',
                        [
                            {
                                'Keys': ['Amazon FSx', 'USE2-ONTAP-SSD-GB-Mo'],
                                'Metrics': metric(7, 7),
                            },
                        ],
                    )
                ]
            },
        ],
    }


def values(**overrides):
    base = {
        'metrics.provider': 'dogstatsd',
        'cluster-manager.metrics.cost.enabled': True,
        'cluster-manager.metrics.cost.lookback_days': 2,
    }
    base.update(overrides)
    return base


def test_tag_values_and_group_keys():
    assert tag_value(' Design Team ') == 'design_team'
    assert tag_value('') == 'unknown'
    assert tag_value(None) == 'unknown'
    assert strip_tag_key('idea:Project$r-and-d') == 'r-and-d'
    assert strip_tag_key('idea:Project$') == ''
    assert strip_tag_key('Amazon FSx') == 'Amazon FSx'


def test_aggregate_sums_one_point_per_family_day_and_dimensions():
    rows = [
        CostRow(FAMILY, '2026-09-13', {'module': 'a', 'project': 'p'}, 1.0, 2.0),
        CostRow(FAMILY, '2026-09-13', {'project': 'p', 'module': 'a'}, 3.0, 4.0),
        CostRow(FAMILY, '2026-09-14', {'module': 'a', 'project': 'p'}, 1.0, 1.0),
    ]
    totals = aggregate(rows)
    assert [(r.day, r.amortized, r.unblended) for r in totals] == [
        ('2026-09-13', 4.0, 6.0),
        ('2026-09-14', 1.0, 1.0),
    ]


def test_reader_asks_once_per_module_plus_the_partition_cuts_and_reads_every_page():
    stub = StubCostExplorer(['scheduler', 'vdc'], responses())
    reader = CostExplorerReader(stub, MODULE_TAG, 'idea:Project', 'idea:JobOwner')
    start, end = arrow.get('2026-09-13'), arrow.get('2026-09-15')
    rows = aggregate(reader.fetch_all(start, end, by_account=False))

    calls = [name for name, _ in stub.requests]
    assert calls.count('GetTags') == 2, 'both tag pages'
    # scheduler (2 pages) + vdc + untagged + by service + storage
    assert calls.count('GetCostAndUsage') == 6
    periods = {
        kwargs['TimePeriod']['Start'] + '..' + kwargs['TimePeriod']['End']
        for _, kwargs in stub.requests
    }
    assert periods == {'2026-09-13..2026-09-15'}
    for name, kwargs in stub.requests:
        if name == 'GetCostAndUsage':
            assert kwargs['Granularity'] == 'DAILY'
            assert kwargs['Metrics'] == ['AmortizedCost', 'UnblendedCost']

    by_key = {(r.family, r.day, tuple(sorted(r.dimensions.items()))): r for r in rows}
    scheduler_13 = by_key[
        (
            FAMILY,
            '2026-09-13',
            (('module', 'scheduler'), ('owner', 'alice'), ('project', 'r-and-d')),
        )
    ]
    assert (scheduler_13.amortized, scheduler_13.unblended) == (10.0, 12.0)
    assert (
        FAMILY,
        '2026-09-14',
        (('module', 'scheduler'), ('owner', 'alice'), ('project', 'r-and-d')),
    ) in by_key, 'second page'
    assert (
        FAMILY,
        '2026-09-13',
        (('module', 'vdc'), ('owner', 'bob'), ('project', 'design_team')),
    ) in by_key
    untagged = by_key[
        (
            FAMILY,
            '2026-09-13',
            (('module', 'unknown'), ('owner', 'unknown'), ('project', 'unknown')),
        )
    ]
    assert (untagged.amortized, untagged.unblended) == (-54.0, 20.0), (
        'savings arrive as untagged negatives'
    )
    assert (
        FAMILY_BY_SERVICE,
        '2026-09-13',
        (
            ('module', 'scheduler'),
            ('service', 'amazon_elastic_compute_cloud_-_compute'),
        ),
    ) in by_key
    assert (
        FAMILY_STORAGE,
        '2026-09-13',
        (('service', 'amazon_fsx'), ('usage_type', 'use2-ontap-ssd-gb-mo')),
    ) in by_key
    # a zero group is not a point
    assert not any(r.amortized == 0 and r.unblended == 0 for r in rows)


def test_service_publishes_counts_stamped_at_the_day():
    stub = StubCostExplorer(['scheduler', 'vdc'], responses())
    context = FakeContext(values(), cost_explorer=stub)
    service = CostMetricsService(context)
    assert service.is_enabled()

    service.run_once()

    amortized = context.published('cost.amortized')
    assert len(amortized) > 0
    scheduler = [
        e
        for e in amortized
        if context.dimensions(e).get('module') == 'scheduler'
        and context.dimensions(e).get('owner') == 'alice'
    ]
    assert {e['Timestamp'] for e in scheduler} == {
        int(arrow.get('2026-09-13').timestamp()),
        int(arrow.get('2026-09-14').timestamp()),
    }
    assert all(e['MetricType'] == 'Counter' for e in amortized)
    assert context.published('cost.unblended')[0]['Value'] > 0
    assert context.distributed_lock().held == [], 'the lock is released'


def test_window_is_the_trailing_full_days():
    context = FakeContext(values())
    start, end = CostMetricsService(context).window(
        arrow.get('2026-09-15T13:00:00+00:00')
    )
    assert (start.format('YYYY-MM-DD'), end.format('YYYY-MM-DD')) == (
        '2026-09-13',
        '2026-09-15',
    )


def test_disabled_outside_the_commercial_partition_or_without_a_provider():
    assert not CostMetricsService(
        FakeContext(values(), partition='aws-us-gov')
    ).is_enabled()
    assert not CostMetricsService(
        FakeContext(values(**{'metrics.provider': None}))
    ).is_enabled()
    assert not CostMetricsService(
        FakeContext(values(**{'cluster-manager.metrics.cost.enabled': False}))
    ).is_enabled()
