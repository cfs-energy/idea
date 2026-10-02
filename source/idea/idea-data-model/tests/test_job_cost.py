"""Job spend is the sum of line items; a reserved-instance savings line is never subtracted."""

from decimal import Decimal

from ideadatamodel.reporting.job_cost import job_spend


def line(service, amount, unit='USD'):
    return dict(service=service, total_price=dict(amount=amount, unit=unit))


ON_DEMAND = [line('aws.ec2', 10), line('aws.ebs', 0.5)]
SPOT = [line('aws.ec2', 3), line('aws.ebs', 0.5), line('aws.fsx', 2)]


def test_on_demand_spot_and_mixed():
    assert job_spend(dict(line_items=ON_DEMAND)) == Decimal('10.5')
    assert job_spend(dict(line_items=SPOT)) == Decimal('5.5')
    assert job_spend(dict(line_items=[line('aws.ec2', 10), *SPOT])) == Decimal('15.5')


def test_historic_reserved_savings_line_does_not_change_spend():
    historic = dict(
        line_items=ON_DEMAND,
        savings=[line('aws.ec2', 4)],
        savings_total=dict(amount=4, unit='USD'),
        total=dict(amount=6.5, unit='USD'),
    )
    assert job_spend(historic) == job_spend(dict(line_items=ON_DEMAND))


def test_unknown_spend_is_none():
    assert job_spend(None) is None
    assert job_spend({}) is None
    assert job_spend(dict(line_items=ON_DEMAND, price_unavailable=True)) is None
    assert job_spend(dict(line_items=[dict(service='aws.ec2', total_price={})])) is None
    assert job_spend(dict(line_items=[line('aws.ec2', None)])) is None
    assert job_spend(dict(line_items=[])) == 0


def test_currency_and_rate_fallback():
    assert job_spend(dict(line_items=ON_DEMAND), 'EUR') is None
    assert job_spend(dict(line_items=ON_DEMAND), 'EUR', lambda a, u: a * 2) == 21
    assert job_spend(dict(line_items=[line('aws.ec2', 2, unit=None)])) == 2
    rated = dict(quantity=2, unit_price=dict(amount='1.25', unit='USD'))
    assert job_spend(dict(line_items=[rated])) == Decimal('2.50')
