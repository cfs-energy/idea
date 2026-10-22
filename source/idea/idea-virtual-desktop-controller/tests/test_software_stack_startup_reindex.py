"""
The software stack search index is rebuilt from DynamoDB when the controller starts.

Rows get deleted from the table out of band. The upgrade preflight drops end of life
stacks from a container with no route to OpenSearch, leaving the portal listing ghost
stacks that fail to disable.
"""

from typing import Any, Dict, List, Optional
from unittest.mock import Mock

from ideadatamodel import (
    ListSoftwareStackResponse,
    SocaListingPayload,
    SocaPaginator,
    VirtualDesktopSoftwareStack,
)
from ideasdk.analytics.analytics_service import AnalyticsEntry, EntryAction
from ideavirtualdesktopcontroller.app import (
    virtual_desktop_controller_app as app_module,
)
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_utils import (
    VirtualDesktopSoftwareStackUtils,
)

SOFTWARE_STACK_ALIAS = 'idea-test_vdc_software_stacks'


def a_stack(stack_id: str) -> VirtualDesktopSoftwareStack:
    return VirtualDesktopSoftwareStack(stack_id=stack_id, name=stack_id)


class FakeAnalyticsService:
    def __init__(self):
        self.entries: List[AnalyticsEntry] = []

    def post_entry(self, entry: AnalyticsEntry):
        self.entries.append(entry)


class FakeClusterConfig:
    @staticmethod
    def get_string(key: str, default: str = None, required: bool = False):
        if key == 'virtual-desktop-controller.opensearch.software_stack.alias':
            return SOFTWARE_STACK_ALIAS
        return default


class FakeContext:
    def __init__(self, leader=True):
        self._config = FakeClusterConfig()
        self._analytics_service = FakeAnalyticsService()
        self.software_stack_template_version = 3
        self.logs = Mock()
        self.leader = leader

    def is_leader(self) -> bool:
        if isinstance(self.leader, Exception):
            raise self.leader
        return self.leader

    def config(self) -> FakeClusterConfig:
        return self._config

    def analytics_service(self) -> FakeAnalyticsService:
        return self._analytics_service

    def logger(self, name: str = None) -> Mock:
        return self.logs


class FakeSoftwareStackDB:
    """the table as pages of rows, and the index as a flat list of what it holds"""

    def __init__(
        self,
        pages: Optional[List[List[VirtualDesktopSoftwareStack]]] = None,
        indexed: Optional[List[VirtualDesktopSoftwareStack]] = None,
        index_error: Optional[Exception] = None,
    ):
        self.pages = [[]] if pages is None else pages
        self.indexed = [] if indexed is None else indexed
        self.index_error = index_error
        self.scanned_cursors: List[Optional[str]] = []
        self.reads: List[str] = []

    def list_all_from_db(self, request) -> SocaListingPayload:
        self.reads.append('table')
        self.scanned_cursors.append(request.cursor)
        page = 0 if request.cursor is None else int(request.cursor)
        next_cursor = str(page + 1) if page + 1 < len(self.pages) else None
        return SocaListingPayload(
            listing=self.pages[page],
            paginator=SocaPaginator(cursor=next_cursor),
        )

    def list_from_index(self, request) -> ListSoftwareStackResponse:
        self.reads.append('index')
        if self.index_error is not None:
            raise self.index_error
        start = request.page_start
        return ListSoftwareStackResponse(
            listing=self.indexed[start : start + request.page_size]
        )

    @staticmethod
    def convert_software_stack_object_to_index_dict(
        software_stack: VirtualDesktopSoftwareStack,
    ) -> Dict[str, Any]:
        return {'stack_id': software_stack.stack_id, 'enabled': software_stack.enabled}


def build_app(db: FakeSoftwareStackDB, context: FakeContext, monkeypatch):
    # the rebuild reaches the software stack table, the index and the analytics service
    # only, so both constructors and their aws clients are skipped
    utils = object.__new__(VirtualDesktopSoftwareStackUtils)
    utils.context = context
    utils._software_stack_db = db
    utils._logger = Mock()
    monkeypatch.setattr(
        app_module, 'VirtualDesktopSoftwareStackUtils', lambda context, db: utils
    )

    app = object.__new__(app_module.VirtualDesktopControllerApp)
    app.context = context
    app._software_stack_db = db
    app._software_stacks_reindexed = False
    return app


def entry_ids(context: FakeContext, action: EntryAction) -> List[str]:
    return [
        entry.entry_id
        for entry in context.analytics_service().entries
        if entry.entry_action == action
    ]


def test_startup_indexes_every_software_stack_row_in_the_table(monkeypatch):
    context = FakeContext()
    db = FakeSoftwareStackDB(
        pages=[[a_stack('ss-1'), a_stack('ss-2')], [a_stack('ss-3')]]
    )
    app = build_app(db, context, monkeypatch)

    app._reindex_software_stacks()

    assert entry_ids(context, EntryAction.CREATE_ENTRY) == ['ss-1', 'ss-2', 'ss-3']
    assert db.scanned_cursors == [None, '1']


def test_startup_drops_index_entries_whose_table_row_is_gone(monkeypatch):
    context = FakeContext()
    db = FakeSoftwareStackDB(
        pages=[[a_stack('ss-1')]],
        indexed=[a_stack('ss-1'), a_stack('ss-eol')],
    )
    app = build_app(db, context, monkeypatch)

    app._reindex_software_stacks()

    assert entry_ids(context, EntryAction.DELETE_ENTRY) == ['ss-eol']
    # a row the index already holds is refreshed with an update, never re-created
    assert entry_ids(context, EntryAction.CREATE_ENTRY) == []
    assert entry_ids(context, EntryAction.UPDATE_ENTRY) == ['ss-1']


def test_an_opensearch_failure_is_logged_and_does_not_stop_startup(monkeypatch):
    context = FakeContext()
    db = FakeSoftwareStackDB(
        pages=[[a_stack('ss-1')]],
        index_error=RuntimeError('opensearch is unreachable'),
    )
    app = build_app(db, context, monkeypatch)

    app._reindex_software_stacks()

    assert context.logs.error.called


def test_the_rebuild_runs_once_even_if_the_app_initializes_again(monkeypatch):
    context = FakeContext()
    db = FakeSoftwareStackDB(pages=[[a_stack('ss-1')]])
    app = build_app(db, context, monkeypatch)

    app._reindex_software_stacks()
    app._reindex_software_stacks()

    assert entry_ids(context, EntryAction.CREATE_ENTRY) == ['ss-1']


def test_the_index_is_read_before_the_table_so_a_gap_never_drops_a_stack(monkeypatch):
    context = FakeContext()
    # ss-new exists in the table but not yet in the index: created in the gap
    db = FakeSoftwareStackDB(
        pages=[[a_stack('ss-1'), a_stack('ss-new')]], indexed=[a_stack('ss-1')]
    )
    app = build_app(db, context, monkeypatch)

    app._reindex_software_stacks()

    assert db.reads[0] == 'index'
    assert db.reads[-1] == 'table'
    assert entry_ids(context, EntryAction.CREATE_ENTRY) == ['ss-new']
    assert entry_ids(context, EntryAction.DELETE_ENTRY) == []


def test_follower_and_unknown_status_do_not_mark_rebuild_complete(monkeypatch):
    context = FakeContext(leader=False)
    db = FakeSoftwareStackDB(pages=[[a_stack('ss-1')]])
    app = build_app(db, context, monkeypatch)
    for leader in [False, RuntimeError('election not started'), False]:
        context.leader = leader
        app._reindex_software_stacks()
        assert not app._software_stacks_reindexed
        assert not db.reads
    assert context.logs.warning.called


def test_leader_loop_rebuilds_once_after_delayed_election_or_failover(
    monkeypatch, refresh_loop
):
    loop_app, _ = refresh_loop
    context = FakeContext(leader=False)
    db = FakeSoftwareStackDB(pages=[[a_stack('ss-1')]])
    app = build_app(db, context, monkeypatch)
    loop_app.__dict__.update(app.__dict__)
    ticks = 0

    def wait(seconds):
        nonlocal ticks
        assert seconds == 60
        ticks += 1
        # Election can settle after ten minutes; another task can lead until then.
        context.leader = ticks in [12, 13, 15]
        if ticks == 16:
            loop_app._bootstrap_exit.stopped = True

    loop_app._bootstrap_exit.wait = wait
    loop_app._refresh_bootstrap_loop()

    assert ticks == 16
    assert entry_ids(context, EntryAction.CREATE_ENTRY) == ['ss-1']
    assert db.scanned_cursors == [None]
    assert loop_app._software_stacks_reindexed


def test_startup_rewrites_rows_present_in_both_stores_so_dynamodb_edits_reach_the_portal(
    monkeypatch,
):
    context = FakeContext()
    table_row = a_stack('ss-1')
    table_row.enabled = False  # disabled straight in DynamoDB by upgrade-cluster
    index_doc = a_stack('ss-1')
    index_doc.enabled = True  # what the portal still shows
    db = FakeSoftwareStackDB(
        pages=[[table_row, a_stack('ss-new')]],
        indexed=[index_doc, a_stack('ss-eol')],
    )
    app = build_app(db, context, monkeypatch)

    app._reindex_software_stacks()

    assert entry_ids(context, EntryAction.DELETE_ENTRY) == ['ss-eol']
    assert entry_ids(context, EntryAction.CREATE_ENTRY) == ['ss-new']
    assert entry_ids(context, EntryAction.UPDATE_ENTRY) == ['ss-1']
    update = next(
        entry
        for entry in context.analytics_service().entries
        if entry.entry_action == EntryAction.UPDATE_ENTRY
    )
    assert update.entry_content.entry_record == {'stack_id': 'ss-1', 'enabled': False}


def test_failed_rebuild_is_retried_on_the_next_leader_tick(monkeypatch):
    context = FakeContext(leader=True)
    db = FakeSoftwareStackDB(pages=[[a_stack('ss-1')]])
    app = build_app(db, context, monkeypatch)
    real = app_module.VirtualDesktopSoftwareStackUtils(context, db)
    rebuild = real.reindex_from_db
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('index unavailable')
        return rebuild()

    monkeypatch.setattr(real, 'reindex_from_db', flaky)
    app._reindex_software_stacks()
    assert not app._software_stacks_reindexed
    app._reindex_software_stacks()
    assert app._software_stacks_reindexed
    assert len(calls) == 2
    assert entry_ids(context, EntryAction.CREATE_ENTRY) == ['ss-1']
