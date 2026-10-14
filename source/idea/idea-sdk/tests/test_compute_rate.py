"""the effective compute rate: one validated cluster key, invalid values ignored and logged once."""

from unittest.mock import Mock

import pytest
from pyhocon import ConfigFactory

from ideasdk.aws import compute_rate
from ideasdk.aws.compute_rate import (
    compute_rate_label,
    compute_rate_multiplier,
    parse_compute_rate_multiplier,
)


@pytest.mark.parametrize(
    'value,expected',
    [
        (None, 1.0),
        ('', 1.0),
        (1, 1.0),
        (0.72, 0.72),
        ('0.72', 0.72),
        (1.5, 1.5),
        (0, None),
        (-0.1, None),
        (1.500001, None),
        (float('nan'), None),
        (float('inf'), None),
        (True, None),
        ('cheap', None),
    ],
)
def test_parse(value, expected):
    assert parse_compute_rate_multiplier(value) == expected


def config(value):
    tree = ConfigFactory.from_dict({'cluster': {'timezone': 'UTC'}})
    if value is not None:
        tree.put('cluster.costs.compute_rate_multiplier', value)
    return tree


def test_unset_is_list_rate():
    assert compute_rate_multiplier(config(None)) == 1.0


def test_invalid_value_is_ignored_and_logged_once(monkeypatch):
    monkeypatch.setattr(compute_rate, '_warned', set())
    logger = Mock()
    for _ in range(3):
        assert compute_rate_multiplier(config(2.0), logger) == 1.0
    assert logger.warning.call_count == 1
    assert compute_rate_multiplier(config(0.72), logger) == 0.72
    assert logger.warning.call_count == 1


def test_label():
    assert compute_rate_label(0.72) == 'adjusted (×0.72)'
