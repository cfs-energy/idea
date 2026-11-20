"""A desktop with no table row leaves the admin listing.

dev listed validate-rhel10-arm64 as DELETING for two weeks with no session row: the
listing reads the search index, and nothing compared the index with the table.
"""

from unittest.mock import Mock

from ideadatamodel import ListSessionsResponse, VirtualDesktopSession
from ideasdk.analytics.analytics_service import EntryAction
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)


def a_session(name, owner='a-person'):
    return VirtualDesktopSession(idea_session_id=name, owner=owner)


def sweep(indexed, rows, get_error=None):
    utils = object.__new__(VirtualDesktopSessionUtils)
    utils.context = Mock()
    utils.context.sessions_template_version = 1
    utils.context.config.return_value.get_string.return_value = 'sessions'
    utils._logger = Mock()
    utils._session_db = Mock()
    utils._session_db.list_from_index.side_effect = lambda request: (
        ListSessionsResponse(
            listing=indexed[
                request.paginator.start : request.paginator.start
                + request.paginator.page_size
            ]
        )
    )

    def get_from_db(owner, idea_session_id):
        if get_error:
            raise get_error
        return rows.get((owner, idea_session_id))

    utils._session_db.get_from_db.side_effect = get_from_db
    return utils


def deleted(utils):
    return [
        call.args[0].entry_id
        for call in utils.context.analytics_service.return_value.post_entry.call_args_list
        if call.args[0].entry_action == EntryAction.DELETE_ENTRY
    ]


def test_a_document_with_no_row_is_dropped_and_one_with_a_row_stays():
    live = a_session('live')
    utils = sweep([live, a_session('gone')], {('a-person', 'live'): live})
    assert utils.drop_index_entries_without_a_row() == 1
    assert deleted(utils) == ['gone']
    assert (
        len(utils.context.analytics_service.return_value.post_entry.call_args_list) == 1
    )


def test_every_page_of_the_index_is_read():
    indexed = [a_session(f's{number}') for number in range(250)]
    rows = {('a-person', item.idea_session_id): item for item in indexed[:249]}
    utils = sweep(indexed, rows)
    assert utils.drop_index_entries_without_a_row() == 1
    assert deleted(utils) == ['s249']
    starts = [
        call.args[0].paginator.start
        for call in utils._session_db.list_from_index.call_args_list
    ]
    assert starts == [0, 100, 200]


def test_a_table_read_that_fails_drops_nothing():
    utils = sweep([a_session('live')], {}, get_error=RuntimeError('throttled'))
    try:
        utils.drop_index_entries_without_a_row()
    except RuntimeError:
        pass
    assert deleted(utils) == []


def test_a_document_that_cannot_be_looked_up_is_left_alone():
    utils = sweep([a_session('no-owner', owner=None)], {})
    assert utils.drop_index_entries_without_a_row() == 0
    utils._session_db.get_from_db.assert_not_called()
