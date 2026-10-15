"""Stored fractional rates retain their units in job estimates."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pyhocon import ConfigFactory

from ideadatamodel import EC2InstanceUnitPrice, SocaMemory, SocaMemoryUnit, locale
from ideascheduler.app.aws.pricing_helper import PricingHelper, TOTAL_SECONDS_IN_MONTH


@pytest.fixture(autouse=True)
def currency(monkeypatch):
    monkeypatch.setattr(locale, 'get_currency_code', lambda: 'USD')


def helper(lustre=False):
    config = ConfigFactory.from_dict(
        {
            'scheduler': {
                'cost_estimation': {
                    'ebs_gp3_storage': 0.08,
                    'ebs_io1_storage': 0.125,
                    'provisioned_iops': 0.065,
                    'fsx_lustre': 0.000194,
                    'default_fsx_lustre_size': 1200,
                    'ec2_boot_penalty_seconds': 300,
                }
            }
        }
    )
    job = Mock()
    job.params = SimpleNamespace(
        root_storage_size=SocaMemory(value=10, unit=SocaMemoryUnit.GB),
        scratch_storage_size=SocaMemory(value=100, unit=SocaMemoryUnit.GB),
        scratch_storage_iops=1000,
        fsx_lustre=SimpleNamespace(enabled=lustre, size=None),
    )
    job.desired_nodes.return_value = 1
    job.ondemand_nodes.return_value = 1
    job.spot_nodes.return_value = 0
    job.default_instance_type = 'm5.large'
    context = Mock()
    context.config.return_value = config
    context.aws_util.return_value.get_ec2_instance_type_unit_price.return_value = (
        EC2InstanceUnitPrice(ondemand=0.1, reserved=0.05)
    )
    return PricingHelper(context, job, total_time_secs=TOTAL_SECONDS_IN_MONTH - 300)


def test_fractional_io1_iops_contributes_to_bom():
    pricing = helper()
    assert pricing.scratch_storage_iops_unit_price == 0.065
    assert TOTAL_SECONDS_IN_MONTH == 60 * 60 * 24 * 30
    bom = pricing.compute_estimated_bom_cost()
    iops = next(item for item in bom.line_items if item.unit == 'IOPS-month')
    assert iops.unit_price.amount == 0.065
    assert iops.quantity == 1000
    assert iops.total_price.amount == 65
    storage = next(
        item for item in bom.line_items if item.product == 'scratch_storage=io1'
    )
    assert storage.unit == 'GB-month'
    assert storage.total_price.amount == 12.5


def test_lustre_bom_is_gb_hour_without_changing_existing_rate():
    pricing = helper(lustre=True)
    bom = pricing.compute_estimated_bom_cost()
    storage = next(
        item for item in bom.line_items if item.product == 'scratch_storage=lustre'
    )
    assert storage.unit == 'GB-hour'
    assert storage.quantity == 1200 * 720
    assert storage.unit_price.amount == 0.000194
    assert storage.total_price.amount == pytest.approx(167.616)


def test_on_demand_job_is_charged_on_demand_with_no_reserved_discount():
    """the account holds no reservations, so the estimate must not subtract one"""
    bom = helper().compute_estimated_bom_cost()
    compute = next(item for item in bom.line_items if item.service == 'aws.ec2')
    assert compute.unit_price.amount == 0.1
    assert not bom.savings
    assert bom.total.amount == bom.line_items_total.amount
    assert bom.line_items_total.amount == pytest.approx(
        sum(item.total_price.amount for item in bom.line_items)
    )


def rated_helper(multiplier, spot=False):
    pricing = helper()
    config = pricing.config()
    if multiplier is not None:
        config.put('cluster.costs.compute_rate_multiplier', multiplier)
    if spot:
        pricing.job.ondemand_nodes.return_value = 0
        pricing.job.spot_nodes.return_value = 1
        pricing.job.is_mixed_capacity.return_value = False
        pricing.job.is_spot_capacity.return_value = True
        pricing.job.params.spot_price = None
    return pricing


@pytest.mark.parametrize(
    'multiplier,rate',
    [
        (None, 0.1),
        (1.0, 0.1),
        (0.72, 0.072),
        (1.5, 0.15),
        (0, 0.1),
        (-0.5, 0.1),
        (1.51, 0.1),
        ('cheap', 0.1),
    ],
)
def test_on_demand_compute_is_priced_at_the_effective_rate(multiplier, rate):
    bom = rated_helper(multiplier).compute_estimated_bom_cost()
    compute = next(item for item in bom.line_items if item.service == 'aws.ec2')
    assert compute.unit_price.amount == pytest.approx(rate)
    assert ('adjusted' in compute.title) == (rate != 0.1)


def ec2_line(bom):
    return next(item for item in bom.line_items if item.service == 'aws.ec2')


def other_lines(bom):
    return [
        (item.product, item.total_price.amount)
        for item in bom.line_items
        if item.service != 'aws.ec2'
    ]


def test_effective_rate_scales_compute_only_and_only_once():
    list_bom = rated_helper(None).compute_estimated_bom_cost()
    bom = rated_helper(0.5).compute_estimated_bom_cost()
    assert ec2_line(bom).total_price.amount == pytest.approx(
        ec2_line(list_bom).total_price.amount * 0.5
    )
    # ebs and every other line keep the list rate
    assert other_lines(bom) == other_lines(list_bom)
    assert bom.line_items_total.amount == pytest.approx(
        sum(item.total_price.amount for item in bom.line_items)
    )


def test_explicit_spot_compute_is_never_scaled():
    """An explicit spot rate is already the rate paid."""
    pricing = rated_helper(0.5, spot=True)
    pricing.job.params.spot_price = SimpleNamespace(amount=0.1)
    bom = pricing.compute_estimated_bom_cost()
    compute = next(item for item in bom.line_items if item.service == 'aws.ec2')
    assert compute.unit_price.amount == pytest.approx(0.1)


@pytest.mark.parametrize('multiplier,expected', [(0.7, 0.07), (1.0, 0.1), (1.5, 0.1)])
@pytest.mark.parametrize('mixed', [False, True])
def test_spot_fallback_is_capped_at_scaled_on_demand(multiplier, expected, mixed):
    pricing = rated_helper(multiplier, spot=True)
    pricing.job.is_mixed_capacity.return_value = mixed
    pricing.job.ondemand_nodes.return_value = int(mixed)
    bom = pricing.compute_estimated_bom_cost()
    spot = next(
        line for line in bom.line_items if line.product.endswith('lifecycle=spot')
    )
    assert spot.unit_price.amount == pytest.approx(expected)
