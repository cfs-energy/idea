"""
The compute image namespaces are for elevated users only: an application token holding
scheduler/write is refused there while the scoped namespaces still honor it.
"""

from unittest.mock import Mock

import pytest

from ideadatamodel import errorcodes, exceptions
from ideascheduler.app.api.scheduler_admin_api import SchedulerAdminAPI


class AppTokenContext:
    """an application token: authorized only by an explicit module scope"""

    def __init__(self, namespace, scope):
        self.namespace = namespace
        self.scope = scope
        self.handled = False

    def is_authorized(self, elevated_access=False, scopes=None):
        return scopes is not None and self.scope in scopes


def api_with_recording_handlers():
    context = Mock()
    context.module_id.return_value = 'scheduler'
    api = SchedulerAdminAPI(context=context)
    for entry in api.acl.values():
        entry['method'] = lambda ctx: setattr(ctx, 'handled', True)
    return api


@pytest.mark.parametrize(
    'namespace',
    ['SchedulerAdmin.BuildComputeImage', 'SchedulerAdmin.ListComputeImages'],
)
def test_an_app_token_cannot_reach_the_image_namespaces(namespace):
    api = api_with_recording_handlers()
    context = AppTokenContext(namespace, 'scheduler/write')
    with pytest.raises(exceptions.SocaException) as exc_info:
        api.invoke(context)
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert context.handled is False


def test_an_app_token_with_the_write_scope_still_reaches_scoped_namespaces():
    api = api_with_recording_handlers()
    context = AppTokenContext('SchedulerAdmin.UpdateQueueProfile', 'scheduler/write')
    api.invoke(context)
    assert context.handled is True


class RecordingContext:
    """an elevated user calling one namespace; captures the success payload"""

    def __init__(self, namespace):
        self.namespace = namespace
        self.payload = None

    def is_authorized(self, elevated_access=False, scopes=None):
        return True

    def get_request_payload_as(self, cls):
        return cls()

    def success(self, payload):
        self.payload = payload


def test_the_image_listing_carries_the_compute_os_and_the_supported_set():
    context = Mock()
    context.module_id.return_value = 'scheduler'
    context.config.return_value.get_string.side_effect = (
        lambda key, default=None, required=False: 'amazonlinux2023'
        if key == 'scheduler.compute_node_os'
        else default
    )
    api = SchedulerAdminAPI(context=context)
    api._compute_images = Mock()
    api._compute_images.list_images.return_value = []
    invocation = RecordingContext('SchedulerAdmin.ListComputeImages')

    api.invoke(invocation)

    assert invocation.payload.compute_node_os == 'amazonlinux2023'
    assert 'rocky9' in invocation.payload.supported_base_os
    assert invocation.payload.listing == []
