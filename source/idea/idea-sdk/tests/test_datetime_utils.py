"""
Test Cases for DateTimeUtils.diff

timedelta.seconds is the sub-day remainder, so any span longer than 24 hours drops whole
days. diff() must report total elapsed time.
"""

from ideasdk.utils.datetime_utils import DateTimeUtils

from datetime import datetime, timedelta

BASE = datetime(2026, 1, 1, 0, 0, 0)


def test_diff_within_a_day():
    assert DateTimeUtils.diff(BASE + timedelta(seconds=90), BASE) == 90


def test_diff_spanning_multiple_days():
    delta = timedelta(days=3, seconds=45)
    assert DateTimeUtils.diff(BASE + delta, BASE) == 259245


def test_diff_exactly_whole_days():
    assert DateTimeUtils.diff(BASE + timedelta(days=2), BASE) == 172800


def test_diff_negative_is_signed():
    assert DateTimeUtils.diff(BASE, BASE + timedelta(days=1, hours=1)) == -90000


def test_diff_to_minutes_spanning_multiple_days():
    delta = timedelta(days=2, minutes=30)
    assert DateTimeUtils.diff(BASE + delta, BASE, to_minutes=True) == 2910
