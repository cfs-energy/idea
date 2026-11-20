from unittest.mock import MagicMock

import pytest

from ideadatamodel import (
    ListEmailTemplatesRequest,
    ListGroupsRequest,
    ListProjectsRequest,
    ListUsersRequest,
    SocaFilter,
    SocaPaginator,
)
from ideaclustermanager.app.accounts.db.group_dao import GroupDAO
from ideaclustermanager.app.accounts.db.user_dao import UserDAO
from ideaclustermanager.app.email_templates.email_templates_dao import (
    EmailTemplatesDAO,
)
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO
from ideasdk.utils import Utils

KEY = {'pk': 'row-2'}


def pages(**row_key):
    return [
        {'Items': [{**row_key}], 'LastEvaluatedKey': KEY},
        {'Items': [{**row_key}]},
    ]


def dao_for(cls, rows):
    dao = cls.__new__(cls)
    dao.logger = MagicMock()
    dao.table = MagicMock()
    dao.table.scan.side_effect = rows
    dao.convert_from_db = lambda row: row
    return dao


CASES = [
    (GroupDAO, 'list_groups', ListGroupsRequest),
    (UserDAO, 'list_users', ListUsersRequest),
    (ProjectsDAO, 'list_projects', ListProjectsRequest),
    (EmailTemplatesDAO, 'list_email_templates', ListEmailTemplatesRequest),
]


@pytest.mark.parametrize('cls,method,request_type', CASES)
def test_listing_walks_every_scan_page(cls, method, request_type):
    dao = dao_for(cls, pages(name='x'))
    result = getattr(dao, method)(request_type())

    assert len(result.listing) == 2
    assert result.paginator.cursor is None
    assert dao.table.scan.call_args_list[1].kwargs == {'ExclusiveStartKey': KEY}


@pytest.mark.parametrize('cls,method,request_type', CASES)
def test_listing_resumes_from_a_cursor_with_the_scan_parameter(
    cls, method, request_type
):
    dao = dao_for(cls, [{'Items': []}])
    cursor = Utils.base64_encode(Utils.to_json(KEY))
    getattr(dao, method)(request_type(paginator=SocaPaginator(cursor=cursor)))

    assert dao.table.scan.call_args.kwargs == {'ExclusiveStartKey': KEY}


def test_filter_is_kept_on_every_scan_page():
    dao = dao_for(GroupDAO, pages(name='x'))
    dao.list_groups(ListGroupsRequest(filters=[SocaFilter(key='k', like='v')]))

    assert all('ScanFilter' in call.kwargs for call in dao.table.scan.call_args_list)
