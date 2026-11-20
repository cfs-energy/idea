"""Only the configured administrator username skips desktop group and project checks."""

from unittest.mock import Mock

import pytest

from ideadatamodel import VirtualDesktopSession
from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
    VirtualDesktopAdminAPI,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI

# part of the administrator's name, or the administrator's name with more after it
LOOKALIKES = ['admin', 'cluster', 'clusteradmin2']


def an_api(cls):
    api = object.__new__(cls)
    api._logger = Mock()
    api.context = Mock()
    api.context.config.return_value.get_string.return_value = 'clusteradmin'
    api.context.projects_client.get_user_projects.return_value = []
    api.context.accounts_client.get_user.return_value.user.additional_groups = []
    api.VDI_GROUPS = ['vdi-users']
    return api


@pytest.mark.parametrize('owner', LOOKALIKES)
def test_a_lookalike_name_still_needs_the_project(owner):
    api = an_api(VirtualDesktopAPI)
    session, valid = api.validate_create_session_request(
        VirtualDesktopSession(owner=owner, project={'project_id': 'p1'})
    )
    assert not valid
    assert session.failure_reason == "You aren't a member of this project."


@pytest.mark.parametrize('owner', LOOKALIKES)
def test_a_lookalike_name_still_needs_a_desktop_group_on_the_admin_api(owner):
    api = an_api(VirtualDesktopAdminAPI)
    api.validate_create_session_request = Mock()
    _session, valid = api._validate_create_session_request(
        VirtualDesktopSession(owner=owner, project={'project_id': 'p1'})
    )
    assert not valid
    api.validate_create_session_request.assert_not_called()


@pytest.mark.parametrize('owner', LOOKALIKES)
def test_a_lookalike_name_cannot_be_given_a_share_without_a_desktop_group(owner):
    allowed, message = an_api(VirtualDesktopAPI)._validate_user_and_project_membership(
        owner, 'p1'
    )
    assert not allowed
    assert 'not authorized to access Virtual Desktops' in message


def test_the_administrator_itself_skips_the_group_check():
    api = an_api(VirtualDesktopAdminAPI)
    api.validate_create_session_request = Mock(return_value=('ok', True))
    assert api._validate_create_session_request(
        VirtualDesktopSession(owner='clusteradmin')
    ) == ('ok', True)
