"""
the effective compute rate: what this account pays per on-demand instance hour as a share of
the public list rate, for savings plans and reserved instances the price list cannot see.

one cluster-level key, read by the scheduler when it prices a job and by cluster-manager when
it prices desktop hours, so both apply the same number. it scales on-demand instance hours
only: spot is already the rate paid, and ebs, fsx and every other line stay at list rate.
"""

import logging
import math
import threading

COMPUTE_RATE_MULTIPLIER_KEY = 'cluster.costs.compute_rate_multiplier'
MAX_COMPUTE_RATE_MULTIPLIER = 1.5

_warned = set()
_warned_lock = threading.Lock()


def parse_compute_rate_multiplier(value):
    """the multiplier when 0 < value <= 1.5, else None. unset is 1.0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 1.0
    if isinstance(value, bool):
        return None
    try:
        multiplier = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(multiplier):
        return None
    if not 0 < multiplier <= MAX_COMPUTE_RATE_MULTIPLIER:
        return None
    return multiplier


def compute_rate_multiplier(config, logger=None) -> float:
    """
    the configured multiplier, or 1.0 when it is unset or invalid. an invalid value is
    logged once per process per value rather than on every price.
    """
    try:
        value = config.get(COMPUTE_RATE_MULTIPLIER_KEY, None)
    except Exception:
        value = None
    multiplier = parse_compute_rate_multiplier(value)
    if multiplier is not None:
        return multiplier
    key = repr(value)
    with _warned_lock:
        first = key not in _warned
        _warned.add(key)
    if first:
        (logger or logging.getLogger(__name__)).warning(
            f'ignoring {COMPUTE_RATE_MULTIPLIER_KEY}={value!r}: it must be a number '
            f'greater than 0 and at most {MAX_COMPUTE_RATE_MULTIPLIER}; '
            f'pricing compute at the on-demand list rate'
        )
    return 1.0


def compute_rate_label(multiplier: float) -> str:
    """the short suffix shown beside an adjusted figure, e.g. 'adjusted (×0.72)'."""
    return f'adjusted (×{multiplier:g})'
