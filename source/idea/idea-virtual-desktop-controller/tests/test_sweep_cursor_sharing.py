"""
The sweep resume positions belong to the controller, not to one queue handler thread.

Each sweep has a resume cursor so a table larger than one pass's time budget is still
walked to the end across passes. Every queue handler thread builds its own
VirtualDesktopSessionUtils, so a per-instance cursor would give each thread its own
position and a thread torn down as the queue drained would lose its place.

The stopped desktop cleanup stands in for all three sweeps here. They share one shape,
and this is the one whose pass can be cut off part way through a page.
"""

from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)
from test_stopped_session_cleanup import (
    FakeClock,
    FakeEc2Client,
    FakeSessionDB,
    a_session,
    an_instance,
    build_utils,
    sweep,
)


def a_page_of_three():
    return [a_session(idea_session_id=name) for name in ('one', 'two', 'three')]


def a_handler_thread(session_db, clock=None):
    """what one EventsHandlerThread builds for itself"""
    ec2_client = FakeEc2Client(
        an_instance(stopped_hours_ago=1),
        on_describe=None if clock is None else clock.tick,
    )
    return build_utils(session_db, ec2_client)


def test_a_second_handler_thread_resumes_where_the_first_one_stopped(monkeypatch):
    page = a_page_of_three()
    session_db = FakeSessionDB(pages=[page])
    clock = FakeClock(step_ms=10)
    monkeypatch.setattr(Utils, 'current_time_ms', clock)
    thread_one = a_handler_thread(session_db, clock)
    thread_two = a_handler_thread(session_db, clock)

    # the budget runs out on the third desktop, part way through the only page
    assert sweep(thread_one, time_budget_ms=15)['not_due'] == 2

    clock.now = 0
    session_db.requested_cursors.clear()

    assert sweep(thread_two, time_budget_ms=15)['not_due'] == 1
    assert session_db.requested_cursors == [session_db.cursor_for(page[1])], (
        'the second thread picked up the scheduled event, so it walks the rest'
    )


def test_a_thread_that_did_not_exist_yet_resumes_from_the_shared_position(monkeypatch):
    # threads are added as the queue depth rises, so the thread that picks up the next
    # scheduled event may not have existed when the position was taken
    page = a_page_of_three()
    session_db = FakeSessionDB(pages=[page])
    clock = FakeClock(step_ms=10)
    monkeypatch.setattr(Utils, 'current_time_ms', clock)

    sweep(a_handler_thread(session_db, clock), time_budget_ms=15)

    later_thread = a_handler_thread(session_db, clock)
    clock.now = 0
    session_db.requested_cursors.clear()

    sweep(later_thread, time_budget_ms=15)
    assert session_db.requested_cursors == [session_db.cursor_for(page[1])]


def test_a_finished_walk_starts_the_next_thread_at_the_first_page_again():
    session_db = FakeSessionDB(
        pages=[[a_session(idea_session_id='one')], [a_session(idea_session_id='two')]]
    )
    thread_one = a_handler_thread(session_db)
    thread_two = a_handler_thread(session_db)
    VirtualDesktopSessionUtils._stopped_session_cleanup_cursor = '1'

    sweep(thread_one)
    session_db.requested_cursors.clear()
    sweep(thread_two)

    assert session_db.requested_cursors == [None, '1'], (
        'the end of the table was reached, so no thread resumes at a finished page'
    )
