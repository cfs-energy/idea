"""What a finished job was charged, read from its estimated_bom_cost."""

from decimal import Decimal, InvalidOperation


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def job_spend(bom, currency='USD', convert=None):
    """
    The job's spend: the sum of its line items, which price compute at the rate it ran at
    (on-demand or spot) plus its own storage. Savings are never subtracted: records written
    before 26.10.1 carry a reserved-instance discount nobody paid, so their `total` is low.

    None when the job has no estimate, any line lacks an amount, or price_unavailable is
    set. A line in another currency goes through convert(amount, unit); without convert it
    is unknown. A missing unit is USD, the unit every instance price is quoted in.
    """
    bom = bom or {}
    lines = bom.get('line_items')
    if lines is None or bom.get('price_unavailable'):
        return None
    spend = Decimal(0)
    for line in lines:
        price = line.get('total_price') or {}
        value, unit = _decimal(price.get('amount')), price.get('unit')
        if not price:
            # an older record without a line total: rate x quantity is the same figure
            rate = line.get('unit_price') or {}
            rate_amount, quantity = (
                _decimal(rate.get('amount')),
                _decimal(line.get('quantity')),
            )
            if rate_amount is not None and quantity is not None:
                value, unit = rate_amount * quantity, rate.get('unit')
        if value is None:
            return None
        unit = unit or 'USD'
        if unit != currency:
            value = _decimal(convert(value, unit)) if convert else None
            if value is None:
                return None
        spend += value
    return spend
