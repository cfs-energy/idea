"""
Test Cases for DateTimeUtils.diff

timedelta.seconds is the sub-day remainder, so any span longer than 24 hours drops whole
days. diff() must report total elapsed time.
"""

from ideasdk.utils.datetime_utils import DateTimeUtils

from datetime import datetime, timedelta
import os
import time

import pytest
from pytz import utc

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


@pytest.fixture
def non_utc_timezone(monkeypatch):
    if not hasattr(time, 'tzset'):
        pytest.skip('time.tzset is unavailable')

    original_timezone = os.environ.get('TZ')
    monkeypatch.setenv('TZ', 'UTC+07')
    time.tzset()
    yield
    if original_timezone is None:
        monkeypatch.delenv('TZ')
    else:
        monkeypatch.setenv('TZ', original_timezone)
    time.tzset()


def test_current_datetime_is_utc_on_a_non_utc_host(non_utc_timezone):
    before = datetime.now(utc)
    current = DateTimeUtils.current_datetime()
    after = datetime.now(utc)

    assert before <= current <= after
    assert current.tzinfo is utc


def test_to_datetime_converts_epoch_milliseconds_as_utc(non_utc_timezone):
    timestamp = 1_700_000_000_123

    assert DateTimeUtils.to_datetime(timestamp) == datetime(
        2023, 11, 14, 22, 13, 20, 123000, tzinfo=utc
    )
