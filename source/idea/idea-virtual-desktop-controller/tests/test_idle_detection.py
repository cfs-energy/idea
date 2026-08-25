#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
unit tests for the virtual desktop idleness decision
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.idea_session_cpu_utilization_command_progress_event_handler import (
    evaluate_session_idle,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
CPU_UTILIZATION_THRESHOLD = 30.0
IDLE_AUTOSTOP_DELAY = 60


def activity_sample(
    cpu_utilization: float = 1.0,
    dcv_connections: int = 0,
    creation_time: Optional[datetime] = None,
    last_disconnection_time: Optional[datetime] = None,
    login_sessions: int = 0,
    last_login_out: Optional[datetime] = None,
) -> Dict:
    if creation_time is None:
        creation_time = NOW - timedelta(hours=8)
    return {
        'DCV': {
            'num-of-connections': dcv_connections,
            'creation-time': creation_time.isoformat(),
            'last-disconnection-time': ''
            if last_disconnection_time is None
            else last_disconnection_time.isoformat(),
        },
        'CPUAveragePerformanceLast10Secs': cpu_utilization,
        'SSH_Connection_Count': login_sessions,
        'SSH_Last_Disconnect_ISO': ''
        if last_login_out is None
        else last_login_out.isoformat(),
    }


def evaluate(sample: Optional[Dict]):
    return evaluate_session_idle(
        ssm_output=sample,
        cpu_utilization_threshold=CPU_UTILIZATION_THRESHOLD,
        idle_autostop_delay=IDLE_AUTOSTOP_DELAY,
        current_time=NOW,
    )


def test_dcv_connection_open_is_not_idle():
    is_idle, reason = evaluate(
        activity_sample(
            dcv_connections=1, last_disconnection_time=NOW - timedelta(hours=3)
        )
    )
    assert is_idle is False
    assert 'connection(s) are still open' in reason


def test_login_session_open_with_dcv_disconnected_is_not_idle():
    is_idle, reason = evaluate(
        activity_sample(
            dcv_connections=0,
            last_disconnection_time=NOW - timedelta(hours=3),
            login_sessions=1,
        )
    )
    assert is_idle is False
    assert 'login session(s) are still open' in reason


def test_every_signal_quiet_is_idle():
    is_idle, _ = evaluate(
        activity_sample(
            cpu_utilization=1.0,
            dcv_connections=0,
            last_disconnection_time=NOW - timedelta(hours=3),
            login_sessions=0,
        )
    )
    assert is_idle is True


def test_session_that_just_started_is_not_idle():
    is_idle, reason = evaluate(
        activity_sample(creation_time=NOW - timedelta(minutes=5))
    )
    assert is_idle is False
    assert 'within the idle autostop delay' in reason


def test_never_connected_session_is_idle_once_the_delay_elapses():
    is_idle, _ = evaluate(activity_sample(creation_time=NOW - timedelta(hours=3)))
    assert is_idle is True


def test_cpu_above_threshold_is_not_idle():
    is_idle, reason = evaluate(
        activity_sample(
            cpu_utilization=85.0, last_disconnection_time=NOW - timedelta(hours=3)
        )
    )
    assert is_idle is False
    assert 'not below threshold' in reason


def test_recent_logout_holds_the_session():
    is_idle, reason = evaluate(
        activity_sample(
            last_disconnection_time=NOW - timedelta(hours=3),
            last_login_out=NOW - timedelta(minutes=5),
        )
    )
    assert is_idle is False
    assert 'within the idle autostop delay' in reason


def test_sample_without_login_fields_still_evaluates():
    sample = activity_sample(last_disconnection_time=NOW - timedelta(hours=3))
    sample.pop('SSH_Connection_Count')
    sample.pop('SSH_Last_Disconnect_ISO')
    is_idle, _ = evaluate(sample)
    assert is_idle is True


def test_missing_cpu_utilization_is_not_idle():
    sample = activity_sample(last_disconnection_time=NOW - timedelta(hours=3))
    sample.pop('CPUAveragePerformanceLast10Secs')
    is_idle, reason = evaluate(sample)
    assert is_idle is False
    assert 'no CPU utilization' in reason


def test_missing_connection_count_is_not_idle():
    sample = activity_sample(last_disconnection_time=NOW - timedelta(hours=3))
    sample['DCV'].pop('num-of-connections')
    is_idle, reason = evaluate(sample)
    assert is_idle is False
    assert 'no connection count' in reason


def test_empty_sample_is_not_idle():
    is_idle, reason = evaluate(None)
    assert is_idle is False
    assert 'empty' in reason


def test_unreadable_login_timestamp_is_not_idle():
    sample = activity_sample(last_disconnection_time=NOW - timedelta(hours=3))
    sample['SSH_Last_Disconnect_ISO'] = 'not-a-timestamp'
    is_idle, reason = evaluate(sample)
    assert is_idle is False
    assert 'unreadable last login timestamp' in reason


def test_unreadable_dcv_timestamp_does_not_fall_back_to_creation_time():
    sample = activity_sample(creation_time=NOW - timedelta(hours=8))
    sample['DCV']['last-disconnection-time'] = 'not-a-timestamp'
    is_idle, reason = evaluate(sample)
    assert is_idle is False
    assert 'unusable DCV timestamp' in reason


def test_naive_timestamp_is_treated_as_utc():
    sample = activity_sample()
    sample['DCV']['last-disconnection-time'] = (
        (NOW - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    )
    is_idle, _ = evaluate(sample)
    assert is_idle is True
