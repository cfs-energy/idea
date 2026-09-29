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
