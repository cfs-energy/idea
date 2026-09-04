import botocore.exceptions
import pytest

from ideadatamodel import constants
from ideaadministrator import app_main

CLUSTER_NAME = 'c'
SOFTWARE_STACKS_TABLE = f'{CLUSTER_NAME}.vdc.controller.software-stacks'
USER_SESSIONS_TABLE = f'{CLUSTER_NAME}.vdc.controller.user-sessions'
QUEUE_PROFILES_TABLE = f'{CLUSTER_NAME}.scheduler.queue-profiles'

VDC_MODULE = {
    'module_id': 'vdc',
    'name': constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
}
SCHEDULER_MODULE = {'module_id': 'scheduler', 'name': constants.MODULE_SCHEDULER}


class FakeContext:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    warning = info
    error = info


class FakeTable:
    def __init__(self, items=None):
        self.items = items
        self.deleted = []
        self.updated = []

    def scan(self, **_kwargs):
        if self.items is None:
            raise botocore.exceptions.ClientError(
                {
                    'Error': {
                        'Code': 'ResourceNotFoundException',
                        'Message': 'not found',
                    }
                },
                'Scan',
            )
        return {'Items': self.items}

    def delete_item(self, Key):
        self.deleted.append(Key)

    def update_item(self, Key, ExpressionAttributeValues, **_kwargs):
        self.updated.append((Key, ExpressionAttributeValues))


class FakeAws:
    def __init__(self, tables):
        self.tables = tables

    def dynamodb_table(self):
        return self

    def Table(self, table_name):
        # an absent table is a module that is not deployed: scan raises ResourceNotFoundException
        return self.tables.setdefault(table_name, FakeTable())


def stub_db(monkeypatch, tables, modules, config_entries=()):
    class FakeDb:
        cluster_name = CLUSTER_NAME

        def __init__(self, **_kwargs):
            self.aws = FakeAws(tables)

        def get_cluster_modules(self):
            return modules

        def get_config_entries(self):
            return config_entries

    monkeypatch.setattr(app_main, 'ClusterConfigDB', FakeDb)


def check(context, disable_stacks_in_use=False):
    """the read-only preflight that runs before the upgrade is confirmed"""
    return app_main._check_eol_base_os(
        context=context,
        cluster_name=CLUSTER_NAME,
        aws_region='us-east-2',
        aws_profile=None,
        disable_stacks_in_use=disable_stacks_in_use,
    )


def apply_plans(context, plans):
    """the mutations, which only run once the admin has confirmed"""
    app_main._apply_eol_software_stacks(
        context=context,
        cluster_name=CLUSTER_NAME,
        aws_region='us-east-2',
        aws_profile=None,
        plans=plans,
    )


def stack(stack_id, base_os=constants.OS_AMAZONLINUX2):
    return {
        'base_os': base_os,
        'stack_id': stack_id,
        'name': f'name of {stack_id}',
        'architecture': constants.ARCHITECTURE_X86_64,
    }


def session(idea_session_id, owner, stack_id, state='READY', name=None):
    return {
        'owner': owner,
        'name': name or f'desktop {idea_session_id}',
        'idea_session_id': idea_session_id,
        'state': state,
        'base_os': constants.OS_AMAZONLINUX2,
        'software_stack': {
            'stack_id': stack_id,
            'base_os': constants.OS_AMAZONLINUX2,
        },
    }


def test_no_eol_software_stacks_is_a_no_op(monkeypatch):
    stacks = FakeTable([stack('ss-al2023', constants.OS_AMAZONLINUX2023)])
    stub_db(monkeypatch, {SOFTWARE_STACKS_TABLE: stacks}, [VDC_MODULE])
    context = FakeContext()

    assert check(context) == []

    assert stacks.deleted == []
    assert context.messages == []


def test_the_preflight_reports_but_changes_nothing(monkeypatch):
    # the upgrade is not confirmed yet at this point, so nothing may be written
    stacks = FakeTable([stack('ss-al2'), stack('ss-al2-arm')])
    stub_db(monkeypatch, {SOFTWARE_STACKS_TABLE: stacks}, [VDC_MODULE])
    context = FakeContext()

    plans = check(context)

    assert stacks.deleted == []
    assert stacks.updated == []
    assert any(
        'will delete' in message and 'ss-al2' in message for message in context.messages
    )
    assert [s['stack_id'] for plan in plans for s in plan['to_delete']] == [
        'ss-al2',
        'ss-al2-arm',
    ]


def test_eol_software_stacks_without_a_live_session_are_deleted(monkeypatch):
    stacks = FakeTable([stack('ss-al2'), stack('ss-al2-arm')])
    sessions = FakeTable(
        [session('sess-old', 'user1', 'ss-al2', state='DELETED')],
    )
    stub_db(
        monkeypatch,
        {SOFTWARE_STACKS_TABLE: stacks, USER_SESSIONS_TABLE: sessions},
        [VDC_MODULE],
    )
    context = FakeContext()

    apply_plans(context, check(context))

    assert stacks.deleted == [
        {'base_os': constants.OS_AMAZONLINUX2, 'stack_id': 'ss-al2'},
        {'base_os': constants.OS_AMAZONLINUX2, 'stack_id': 'ss-al2-arm'},
    ]
    assert any(
        'ss-al2' in message and 'name of ss-al2' in message
        for message in context.messages
    )
    assert any('deleted 2' in message for message in context.messages)


def test_a_stack_a_live_session_uses_aborts_and_deletes_nothing(monkeypatch):
    stacks = FakeTable([stack('ss-al2')])
    sessions = FakeTable([session('sess-1', 'user1', 'ss-al2')])
    stub_db(
        monkeypatch,
        {SOFTWARE_STACKS_TABLE: stacks, USER_SESSIONS_TABLE: sessions},
        [VDC_MODULE],
    )
    context = FakeContext()

    with pytest.raises(SystemExit):
        check(context)

    assert stacks.deleted == []
    assert any(
        'sess-1' in message and 'user1' in message and 'ss-al2' in message
        for message in context.messages
    )


def test_a_stack_in_use_is_disabled_with_the_flag_and_the_upgrade_continues(
    monkeypatch,
):
    stacks = FakeTable([stack('ss-al2'), stack('ss-al2-unused')])
    sessions = FakeTable([session('sess-1', 'user1', 'ss-al2', name='my desktop')])
    stub_db(
        monkeypatch,
        {SOFTWARE_STACKS_TABLE: stacks, USER_SESSIONS_TABLE: sessions},
        [VDC_MODULE],
    )
    context = FakeContext()

    apply_plans(context, check(context, disable_stacks_in_use=True))

    assert stacks.updated == [
        (
            {'base_os': constants.OS_AMAZONLINUX2, 'stack_id': 'ss-al2'},
            {':enabled': False},
        )
    ]
    # an end-of-life stack no live session uses is still deleted
    assert stacks.deleted == [
        {'base_os': constants.OS_AMAZONLINUX2, 'stack_id': 'ss-al2-unused'}
    ]
    assert any(
        'disabled' in message
        and 'ss-al2' in message
        and 'user1' in message
        and 'my desktop' in message
        for message in context.messages
    )


def test_disabling_a_stack_says_the_search_index_still_reads_enabled(monkeypatch):
    # the row is disabled in DynamoDB but the portal lists stacks from the search index
    stacks = FakeTable([stack('ss-al2')])
    sessions = FakeTable([session('sess-1', 'user1', 'ss-al2')])
    stub_db(
        monkeypatch,
        {SOFTWARE_STACKS_TABLE: stacks, USER_SESSIONS_TABLE: sessions},
        [VDC_MODULE],
    )
    context = FakeContext()

    apply_plans(context, check(context, disable_stacks_in_use=True))

    assert any(
        'ideactl reindex-software-stacks' in message
        and 'virtual-desktop-controller' in message
        for message in context.messages
    )


def test_a_session_without_a_stack_id_still_protects_its_stack(monkeypatch):
    # matching only on the session's stack_id deletes a stack a running desktop is still on
    orphan = session('sess-1', 'user1', 'ss-al2')
    orphan['software_stack'] = {'base_os': constants.OS_AMAZONLINUX2}
    stacks = FakeTable([stack('ss-al2')])
    stub_db(
        monkeypatch,
        {SOFTWARE_STACKS_TABLE: stacks, USER_SESSIONS_TABLE: FakeTable([orphan])},
        [VDC_MODULE],
    )
    context = FakeContext()

    apply_plans(context, check(context, disable_stacks_in_use=True))

    assert stacks.deleted == []
    assert stacks.updated == [
        (
            {'base_os': constants.OS_AMAZONLINUX2, 'stack_id': 'ss-al2'},
            {':enabled': False},
        )
    ]


def test_an_eol_queue_profile_is_a_hard_stop(monkeypatch):
    # queue profiles are edited, not deleted, so nothing is done for the admin here
    queue_profiles = FakeTable(
        [
            {
                'queue_profile_name': 'compute',
                'param_base_os': constants.OS_AMAZONLINUX2,
            }
        ]
    )
    stacks = FakeTable([stack('ss-al2')])
    stub_db(
        monkeypatch,
        {QUEUE_PROFILES_TABLE: queue_profiles, SOFTWARE_STACKS_TABLE: stacks},
        [SCHEDULER_MODULE, VDC_MODULE],
    )
    context = FakeContext()

    with pytest.raises(SystemExit):
        check(context)

    assert stacks.deleted == []
    assert any('compute' in message for message in context.messages)
