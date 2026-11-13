from unittest.mock import MagicMock

import pytest

from ideadatamodel import (
    ListHpcApplicationsRequest,
    ListHpcLicenseResourcesRequest,
    SocaPaginator,
)
from ideascheduler.app.applications.hpc_applications_dao import HpcApplicationsDAO
from ideascheduler.app.licenses.license_resources_dao import LicenseResourcesDAO
from ideasdk.utils import Utils

KEY = {'name': 'row-2'}
CASES = [
    (HpcApplicationsDAO, 'list_applications', ListHpcApplicationsRequest),
    (LicenseResourcesDAO, 'list_license_resources', ListHpcLicenseResourcesRequest),
]


def dao_for(cls, rows):
    dao = cls.__new__(cls)
    dao.table = MagicMock()
    dao.table.scan.side_effect = rows
    dao.convert_from_db = lambda row, **_: row
    return dao


@pytest.mark.parametrize('cls,method,request_type', CASES)
def test_listing_walks_every_scan_page(cls, method, request_type):
    dao = dao_for(cls, [{'Items': [{}], 'LastEvaluatedKey': KEY}, {'Items': [{}]}])
    result = getattr(dao, method)(request_type())

    assert len(result.listing) == 2
    assert result.paginator.cursor is None
    assert dao.table.scan.call_args_list[1].kwargs == {'ExclusiveStartKey': KEY}


@pytest.mark.parametrize('cls,method,request_type', CASES)
def test_cursor_uses_the_scan_parameter(cls, method, request_type):
    dao = dao_for(cls, [{'Items': []}])
    cursor = Utils.base64_encode(Utils.to_json(KEY))
    getattr(dao, method)(request_type(paginator=SocaPaginator(cursor=cursor)))

    assert dao.table.scan.call_args.kwargs == {'ExclusiveStartKey': KEY}
