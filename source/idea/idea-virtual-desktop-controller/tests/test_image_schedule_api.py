"""
VirtualDesktopAdmin.GetImageSchedule / UpdateImageSchedule: defaults when the keys are
absent, validated leaf writes, and the pipeline namespaces registered ahead of their
implementation fail with NOT_IMPLEMENTED.
"""

from unittest.mock import Mock

import pytest

from ideadatamodel import (
    ImageRefreshSchedule,
    UpdateImageScheduleRequest,
    errorcodes,
    exceptions,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
    VirtualDesktopAdminAPI,
)


class FakeConfig:
    def __init__(self):
        self.values = {}
        self.db = Mock()

    def get_real_key(self, key, module_id=None):
        return key.replace('virtual-desktop-controller.', 'vdc.', 1)

    def get_config(self, key, default=None):
        prefix = self.get_real_key(key) + '.'
        found = {
            k[len(prefix) :]: v for k, v in self.values.items() if k.startswith(prefix)
        }
        return found or default

    def get_int(self, key, default=None):
        return self.values.get(self.get_real_key(key), default)

    def put(self, key, value):
        self.values[key] = value


def make_api():
    api = object.__new__(VirtualDesktopAdminAPI)
    config = FakeConfig()
    api.context = Mock()
    api.context.config.return_value = config
    api.context.cluster_timezone.return_value = 'UTC'
    return api, config


def invocation(request=None, namespace='VirtualDesktopAdmin.GetImageSchedule'):
    context = Mock()
    context.namespace = namespace
    context.get_request_payload_as.return_value = request
    return context


def test_get_defaults_when_keys_absent():
    api, _ = make_api()
    context = invocation()
    api.get_image_schedule(context)
    response = context.success.call_args[0][0]
    assert response.schedule == ImageRefreshSchedule()
    assert response.last_run_on is None
    assert response.next_run_on.weekday() == 6 and response.next_run_on.hour == 2


def test_update_writes_each_leaf_and_reads_back():
    api, config = make_api()
    request = UpdateImageScheduleRequest(
        schedule=ImageRefreshSchedule(enabled=True, day=' Last Friday ', hour=23)
    )
    api.update_image_schedule(invocation(request))
    written = {c.args[0]: c.args[1] for c in config.db.set_config_entry.call_args_list}
    assert written == {
        'vdc.software_stacks.image_refresh_schedule.enabled': True,
        'vdc.software_stacks.image_refresh_schedule.day': 'last friday',
        'vdc.software_stacks.image_refresh_schedule.hour': 23,
    }
    context = invocation()
    api.get_image_schedule(context)
    assert context.success.call_args[0][0].schedule.day == 'last friday'


def test_update_rejects_bad_rule_without_writing():
    api, config = make_api()
    request = UpdateImageScheduleRequest(
        schedule=ImageRefreshSchedule(day='every sunday', hour=2)
    )
    with pytest.raises(exceptions.SocaException) as e:
        api.update_image_schedule(invocation(request))
    assert e.value.error_code == errorcodes.INVALID_PARAMS
    config.db.set_config_entry.assert_not_called()


def test_pending_namespaces_say_not_implemented():
    api, _ = make_api()
    with pytest.raises(exceptions.SocaException) as e:
        api.refresh_images(invocation(namespace='VirtualDesktopAdmin.RefreshImages'))
    assert e.value.error_code == errorcodes.NOT_IMPLEMENTED
