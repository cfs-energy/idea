from unittest.mock import MagicMock

from ideadatamodel import ListPermissionProfilesRequest, SocaPaginator
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.permission_profiles.virtual_desktop_permission_profile_db import (
    VirtualDesktopPermissionProfileDB,
)

KEY = {'profile_id': 'row-2'}


def db_for(rows):
    db = VirtualDesktopPermissionProfileDB.__new__(VirtualDesktopPermissionProfileDB)
    db._table_obj = MagicMock()
    db._table_obj.scan.side_effect = rows
    db.convert_db_dict_to_permission_profile_object = lambda row: row
    return db


def test_listing_walks_every_scan_page():
    db = db_for([{'Items': [{}], 'LastEvaluatedKey': KEY}, {'Items': [{}]}])
    result = db.list(ListPermissionProfilesRequest())

    assert len(result.listing) == 2
    assert db._table_obj.scan.call_args_list[1].kwargs == {'ExclusiveStartKey': KEY}


def test_cursor_uses_the_scan_parameter():
    db = db_for([{'Items': []}])
    cursor = Utils.base64_encode(Utils.to_json(KEY))
    db.list(ListPermissionProfilesRequest(paginator=SocaPaginator(cursor=cursor)))

    assert db._table_obj.scan.call_args.kwargs == {'ExclusiveStartKey': KEY}
