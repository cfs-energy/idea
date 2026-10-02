"""
VirtualDesktopAdmin.GetImageSchedule / UpdateImageSchedule: defaults when the keys are
absent, validated leaf writes, and the pipeline namespaces handing off to the desktop
image pipeline.
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


def test_pipeline_namespaces_hand_off_to_the_pipeline():
    from ideadatamodel import (
        ImageRowKey,
        RefreshImagesRequest,
        RollbackImageRequest,
        SetImagePinnedRequest,
    )

    api, _ = make_api()
    api._image_pipeline = Mock()
    api._image_pipeline.refresh.return_value = []
    context = invocation(
        RefreshImagesRequest(all=True), 'VirtualDesktopAdmin.RefreshImages'
    )
    context.get_username.return_value = 'admin'
    api.refresh_images(context)
    api._image_pipeline.refresh.assert_called_once()
    assert context.success.call_args[0][0].results == []

    with pytest.raises(exceptions.SocaException) as e:
        api.rollback_image(
            invocation(RollbackImageRequest(), 'VirtualDesktopAdmin.RollbackImage')
        )
    assert e.value.error_code == errorcodes.INVALID_PARAMS
    with pytest.raises(exceptions.SocaException):
        api.set_image_pinned(
            invocation(
                SetImagePinnedRequest(row=ImageRowKey(base_os='rocky9')),
                'VirtualDesktopAdmin.SetImagePinned',
            )
        )


@pytest.mark.parametrize(
    'stack_id,ami,validated,expected',
    [
        ('ss-base-rocky9-x86-64-abc', 'ami-mine', set(), True),
        ('ss-base-rocky9-x86-64-abc', 'ami-good', {'ami-good'}, None),
        ('my-own-stack', 'ami-mine', set(), None),
    ],
)
def test_hand_set_base_stack_image_pins_the_stack(
    monkeypatch, stack_id, ami, validated, expected
):
    # the pipeline never overwrites an admin's own image on a base stack
    from ideadatamodel import VirtualDesktopSoftwareStack
    from ideavirtualdesktopcontroller.app.software_stacks import image_pipeline

    api, _ = make_api()
    old = VirtualDesktopSoftwareStack(
        stack_id=stack_id, base_os='rocky9', ami_id='ami-old', projects=[]
    )
    new = VirtualDesktopSoftwareStack(stack_id=stack_id, base_os='rocky9', ami_id=ami)
    api._validate_update_software_stack_request = lambda stack: (stack, True)
    api._logger = Mock()
    api.software_stack_db = Mock()
    api.software_stack_db.get.return_value = old
    api.software_stack_db.update.side_effect = lambda stack: stack
    api.controller_utils = Mock()
    api.controller_utils.describe_image_id.return_value = {'ImageId': ami}
    monkeypatch.setattr(
        image_pipeline,
        'pipeline_for',
        lambda _: Mock(records=Mock(list_all=lambda: [])),
    )
    monkeypatch.setattr(
        'ideasdk.aws.image_builds.validated_image_ids', lambda records: validated
    )
    context = invocation(
        Mock(software_stack=new), 'VirtualDesktopAdmin.UpdateSoftwareStack'
    )

    api.update_software_stack(context)

    stored = api.software_stack_db.update.call_args.args[0]
    assert stored.ami_id == ami
    assert stored.image_pinned is expected
