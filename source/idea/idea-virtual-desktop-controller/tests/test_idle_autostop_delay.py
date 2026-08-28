"""
unit tests for the per-session idle autostop delay override
"""

from typing import Dict, Optional

import pytest

from ideadatamodel import VirtualDesktopSession
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.events.handlers.ssm_commands_progress_event_handlers.idea_session_cpu_utilization_command_progress_event_handler import (
    IDEASessionCPUUtilizationCommandProgressEventHandler,
)

CLUSTER_DEFAULT_KEY = 'virtual-desktop-controller.dcv_session.idle_autostop_delay'
MAX_USER_DELAY_KEY = 'virtual-desktop-controller.dcv_session.idle_autostop_delay_max'


class MockClusterConfig:
    def __init__(self, values: Dict[str, int]):
        self._values = values

    def get_int(self, key: str, default: int = None, required: bool = False) -> int:
        if key not in self._values:
            if required:
                raise KeyError(f'config key not found: {key}')
            return default
        return self._values[key]


class MockContext:
    def __init__(self, values: Dict[str, int]):
        self._config = MockClusterConfig(values)

    def config(self) -> MockClusterConfig:
        return self._config


def build_api(cluster_default: int, max_user_delay: int) -> VirtualDesktopAPI:
    # VirtualDesktopAPI.__init__ builds every DDB/EC2 client, none of which this test needs
    api = object.__new__(VirtualDesktopAPI)
    api.context = MockContext(
        {CLUSTER_DEFAULT_KEY: cluster_default, MAX_USER_DELAY_KEY: max_user_delay}
    )
    return api


def build_handler(
    cluster_default: int, max_user_delay: int
) -> IDEASessionCPUUtilizationCommandProgressEventHandler:
    handler = object.__new__(IDEASessionCPUUtilizationCommandProgressEventHandler)
    handler.context = MockContext(
        {CLUSTER_DEFAULT_KEY: cluster_default, MAX_USER_DELAY_KEY: max_user_delay}
    )
    return handler


@pytest.mark.parametrize(
    'override, cluster_default, max_user_delay, expected',
    [
        (None, 60, 240, 60),  # no override, cluster default applies
        (15, 60, 240, 15),  # user shortens the delay
        (120, 60, 240, 120),  # user extends the delay, within the cap
        (600, 60, 240, 240),  # over the cap, clamped down
        (120, 60, 0, 60),  # overrides disabled, override ignored
        (120, 60, -1, 60),  # negative cap treated as disabled
        (0, 60, 240, 60),  # cleared override
        (-30, 60, 240, 60),  # nonsense override ignored
    ],
)
def test_get_effective_idle_autostop_delay(
    override: Optional[int], cluster_default: int, max_user_delay: int, expected: int
):
    session = VirtualDesktopSession(idle_autostop_delay=override)
    assert (
        session.get_effective_idle_autostop_delay(cluster_default, max_user_delay)
        == expected
    )


def test_handler_uses_session_override():
    handler = build_handler(cluster_default=60, max_user_delay=240)
    session = VirtualDesktopSession(idle_autostop_delay=120)
    assert handler._get_idle_autostop_delay(session) == 120.0


def test_handler_clamps_session_override_to_cap():
    handler = build_handler(cluster_default=60, max_user_delay=90)
    session = VirtualDesktopSession(idle_autostop_delay=120)
    assert handler._get_idle_autostop_delay(session) == 90.0


def test_handler_falls_back_to_cluster_default():
    handler = build_handler(cluster_default=60, max_user_delay=240)
    assert handler._get_idle_autostop_delay(VirtualDesktopSession()) == 60.0


def test_handler_falls_back_when_session_is_missing():
    handler = build_handler(cluster_default=60, max_user_delay=240)
    assert handler._get_idle_autostop_delay(None) == 60.0


def test_validate_accepts_delay_within_cap():
    api = build_api(cluster_default=60, max_user_delay=240)
    assert api.validate_idle_autostop_delay(240) is None


def test_validate_rejects_delay_above_cap():
    api = build_api(cluster_default=60, max_user_delay=240)
    failure_reason = api.validate_idle_autostop_delay(241)
    assert failure_reason is not None
    assert '240' in failure_reason


def test_validate_rejects_override_when_cap_is_not_configured():
    api = build_api(cluster_default=60, max_user_delay=0)
    assert api.validate_idle_autostop_delay(30) is not None


def test_validate_allows_clearing_the_override_when_cap_is_not_configured():
    api = build_api(cluster_default=60, max_user_delay=0)
    assert api.validate_idle_autostop_delay(0) is None
