"""Stacks nothing rebuilds: warn, then hide only once an admin has chosen a date."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jinja2
import pytest
import yaml

from ideadatamodel import (
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopArchitecture,
    VirtualDesktopBaseOS,
    VirtualDesktopGPU,
    VirtualDesktopSession,
    VirtualDesktopServer,
    VirtualDesktopSoftwareStack,
    errorcodes,
    exceptions,
)
from ideavirtualdesktopcontroller.app.software_stacks import stack_expiry as module
from ideavirtualdesktopcontroller.app.software_stacks.stack_expiry import (
    DIGEST_TEMPLATE,
    OrphanInstances,
    StackExpiry,
    StackExpiryService,
)
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
EXEMPT = 'ami-0exempt'


class Config:
    def __init__(self, **values):
        self.values = values
        self.db = Mock()
        self.db.set_config_entry_if.return_value = True

    def key(self, name):
        return f'virtual-desktop-controller.software_stacks.expiry.{name}'

    def get_int(self, key, default=None):
        return self.values.get(key.rsplit('.', 1)[-1], default)

    def get_bool(self, key, default=None):
        return self.values.get(key.rsplit('.', 1)[-1], default)

    def get_list(self, key, default=None):
        return self.values.get(key.rsplit('.', 1)[-1], default)

    def get_string(self, key, default=None, required=False):
        if key == 'cluster.administrator_username':
            return self.values.get('administrator', 'clusteradmin')
        return self.values.get(key.rsplit('.', 1)[-1], default)

    def get_real_key(self, key):
        return key

    def put(self, key, value):
        self.values[key.rsplit('.', 1)[-1]] = value


def context(**values):
    result = Mock()
    result.config.return_value = Config(**values)
    result.cluster_name.return_value = 'idea-test'
    return result


def stack(name='custom', age_days=200, **fields):
    return VirtualDesktopSoftwareStack(
        stack_id=name,
        name=name,
        base_os=VirtualDesktopBaseOS.AMAZON_LINUX2023,
        architecture=VirtualDesktopArchitecture.X86_64,
        gpu=VirtualDesktopGPU.NO_GPU,
        ami_id=f'ami-{name}',
        enabled=True,
        created_on=NOW - timedelta(days=age_days),
        min_storage=SocaMemory(value=20, unit=SocaMemoryUnit.GB),
        min_ram=SocaMemory(value=4, unit=SocaMemoryUnit.GB),
        projects=[],
        **fields,
    )


# a policy date still to come: dates count, nothing is hidden yet
POLICY = '2030-01-01'


def rebuilt(name='base'):
    return stack(name, base_ami_id='ami-stock')


# what a stack's date is


def test_a_stack_on_the_monthly_rebuild_has_no_date():
    assert StackExpiry(context()).expires_on(rebuilt()) is None


def test_a_pinned_base_stack_is_not_on_the_rebuild_so_it_has_one():
    expiry = StackExpiry(context(enforce_from=POLICY))
    assert expiry.expires_on(stack(base_ami_id='ami-stock', image_pinned=True))


def test_a_hand_pinned_stack_counts_ninety_days_from_creation_by_default():
    expected = NOW - timedelta(days=200) + timedelta(days=90)
    assert StackExpiry(context(enforce_from=POLICY)).expires_on(stack()) == expected


def test_the_default_comes_from_the_setting():
    expected = NOW - timedelta(days=200) + timedelta(days=30)
    assert (
        StackExpiry(context(default_days=30, enforce_from=POLICY)).expires_on(stack())
        == expected
    )


def test_its_own_date_wins():
    own = NOW + timedelta(days=400)
    assert StackExpiry(context()).expires_on(stack(expires_on=own)) == own


def test_an_exempt_image_never_expires_and_no_image_is_exempt_in_code():
    assert module.DEFAULT_DAYS == 90
    expiry = StackExpiry(context(enforce_from=POLICY))
    assert expiry.exempt_amis() == set()
    old = stack(name='legacy')
    old.ami_id = EXEMPT
    assert expiry.state(old, NOW) == module.EXPIRED

    by_setting = StackExpiry(context(exempt_amis=[EXEMPT], enforce_from=POLICY))
    assert by_setting.expires_on(old) is None
    assert by_setting.state(old, NOW) is None


@pytest.mark.parametrize(
    'days_left, expected',
    [(-1, 'expired'), (0, 'expired'), (1, 'expiring'), (14, 'expiring'), (15, None)],
)
def test_state_by_date(days_left, expected):
    item = stack(expires_on=NOW + timedelta(days=days_left))
    assert StackExpiry(context()).state(item, NOW) == expected


# off until an admin sets a date


def test_on_upgrade_no_old_stack_is_hidden_or_warned_about():
    # every existing row has no owner and no date
    expiry = StackExpiry(context())
    old, base = stack('old'), rebuilt('base')
    assert old.owner is None and old.expires_on is None
    offered = expiry.offered_for_new_desktops([old, base], NOW)
    assert [s.stack_id for s in offered] == ['old', 'base']
    assert old.expiry_state is None
    assert old.effective_expires_on is None
    assert old.expiry_warning is None
    assert expiry.refusal(old, NOW) is None


def test_setting_a_policy_date_starts_the_counted_dates():
    expiry = StackExpiry(context(enforce_from=POLICY))
    old, base = stack('old'), rebuilt('base')
    offered = expiry.offered_for_new_desktops([old, base], NOW)
    assert [s.stack_id for s in offered] == ['old', 'base']
    assert old.expiry_state == 'expired'
    assert 'Use base instead' in old.expiry_warning
    assert base.expiry_warning is None


@pytest.mark.parametrize('value', ['', None, 'soon'])
def test_no_usable_policy_date_counts_no_date(value):
    assert StackExpiry(context(enforce_from=value)).state(stack(), NOW) is None


def test_a_date_given_to_a_stack_warns_with_no_policy_date():
    item = stack(expires_on=NOW - timedelta(days=1))
    expiry = StackExpiry(context())
    assert expiry.state(item, NOW) == 'expired'
    assert expiry.offered_for_new_desktops([item], NOW) == [item]
    assert expiry.refusal(item, NOW) is None


def test_every_page_of_stacks_is_read():
    pages = {None: (['first'], 'next-page'), 'next-page': (['second'], None)}
    asked = []

    def list_all(request):
        asked.append(request.cursor)
        # a request that loses the cursor starts again from the first page, for ever
        assert len(asked) <= 2
        names, cursor = pages[request.cursor]
        return Mock(listing=[stack(name) for name in names], cursor=cursor)

    svc = service([])
    svc.db.list_all_from_db = list_all
    assert [item.stack_id for item in svc.all_stacks()] == ['first', 'second']
    assert asked == [None, 'next-page']


def test_on_upgrade_no_digest_goes_out():
    svc = service([stack('old')])
    svc.send_digest(svc.all_stacks(), NOW)
    assert sent(svc) == []


def test_extending_with_no_policy_date_never_shortens_the_counted_date():
    item = stack(age_days=10)
    extended = StackExpiry(context()).extended_date(item, 1, NOW)
    assert extended == NOW + timedelta(days=81)


@pytest.mark.parametrize('value', ['', None, 'soon', '2026-13-40'])
def test_an_empty_or_unreadable_date_hides_nothing(value):
    expiry = StackExpiry(context(enforce_from=value))
    assert expiry.is_enforced(NOW) is False
    assert len(expiry.offered_for_new_desktops([stack()], NOW)) == 1


def test_a_date_still_to_come_hides_nothing():
    expiry = StackExpiry(context(enforce_from='2026-12-01'))
    assert len(expiry.offered_for_new_desktops([stack()], NOW)) == 1


def test_from_the_chosen_date_only_expired_stacks_go():
    expiry = StackExpiry(context(enforce_from='2026-11-06'))
    fresh = stack('fresh', age_days=1)
    expiring = stack('soon', expires_on=NOW + timedelta(days=3))
    offered = expiry.offered_for_new_desktops(
        [stack('old'), fresh, expiring, rebuilt()], NOW
    )
    assert [s.stack_id for s in offered] == ['fresh', 'soon', 'base']
    assert 'Expires 2026-11-09' in expiring.expiry_warning


def test_an_expired_stack_is_refused_for_a_new_desktop_once_enforced():
    expiry = StackExpiry(context(enforce_from='2026-11-01'))
    assert 'no longer offered for new desktops' in expiry.refusal(stack(), NOW)
    assert expiry.refusal(rebuilt(), NOW) is None


def test_settings_that_cannot_be_read_refuse_nothing():
    broken = Mock()
    broken.config.side_effect = RuntimeError('no table')
    assert StackExpiry(broken).refusal(stack(), NOW) is None


def test_the_pointer_is_the_same_system_and_something_this_person_can_pick():
    expiry = StackExpiry(context(enforce_from=POLICY))
    old = stack('old')
    other_os = stack('rocky', base_ami_id='ami-stock')
    other_os.base_os = VirtualDesktopBaseOS.ROCKY9
    gpu = stack('gpu', base_ami_id='ami-stock')
    gpu.gpu = VirtualDesktopGPU.NVIDIA
    disabled = stack('off', base_ami_id='ami-stock')
    disabled.enabled = False
    expiry.annotate([old, other_os, gpu, disabled], NOW)
    assert 'Ask an administrator' in old.expiry_warning
    expiry.annotate([old, other_os, gpu, disabled, rebuilt('same')], NOW)
    assert 'Use same instead' in old.expiry_warning


def validate_new_desktop(**values):
    from ideadatamodel import Project
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import (
        VirtualDesktopAPI,
    )

    api = VirtualDesktopAPI.__new__(VirtualDesktopAPI)
    api.context = context(**values)
    api._logger = Mock()
    api.software_stack_db = Mock()
    project = Project(project_id='p', name='p')
    api.context.projects_client.get_user_projects.return_value = [project]
    found = stack()
    found.projects = [project]
    api.software_stack_db.get.return_value = found
    api.validate_base_os = lambda base_os: None
    return api.validate_create_session_request(
        VirtualDesktopSession(
            name='desktop',
            owner='someone',
            project=project,
            software_stack=found,
            server=VirtualDesktopServer(instance_type='m6i.large'),
        )
    )


def test_a_new_desktop_on_an_expired_stack_is_refused_once_enforced():
    session, valid = validate_new_desktop(enforce_from='2026-01-01')
    assert valid is False
    assert 'no longer offered for new desktops' in session.failure_reason


def test_a_new_desktop_on_an_expired_stack_goes_on_while_not_enforced():
    # the next rule's complaint, or success, is what getting past the expiry looks like
    session, _ = validate_new_desktop()
    assert 'no longer offered' not in (session.failure_reason or '')


def test_resuming_an_existing_desktop_never_reads_the_expiry():
    source = Path(module.__file__).parents[1] / 'api/virtual_desktop_api.py'
    text = source.read_text()
    start = text.index('def validate_resume_session_request')
    end = text.index('\n    def ', start + 10)
    assert 'StackExpiry' not in text[start:end]


# extending


def test_extending_starts_from_today_for_an_expired_stack():
    expiry = StackExpiry(context())
    assert expiry.extended_date(stack(), None, NOW) == NOW + timedelta(days=90)


def test_extending_never_shortens():
    expiry = StackExpiry(context())
    later = NOW + timedelta(days=300)
    assert expiry.extended_date(stack(expires_on=later), 30, NOW) == later + timedelta(
        days=30
    )


def admin_api(found):
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
        VirtualDesktopAdminAPI,
    )

    api = VirtualDesktopAdminAPI.__new__(VirtualDesktopAdminAPI)
    api.context = context()
    api._logger = Mock()
    api.software_stack_db = Mock()
    api.software_stack_db.get.return_value = found
    api.software_stack_db.set_expiry.side_effect = lambda s, on: stack(
        s.stack_id, expires_on=on
    )
    api.session_db = Mock()
    return api


def invocation(**payload):
    call = Mock()
    call.get_request_payload_as.side_effect = lambda model: model(**payload)
    return call


def test_one_call_extends_a_stack_and_answers_with_its_new_date():
    api = admin_api(stack())
    call = invocation(stack_id='custom', base_os='amazonlinux2023')
    api.extend_software_stack(call)
    api.software_stack_db.set_expiry.assert_called_once()
    answer = call.success.call_args.args[0].software_stack
    assert answer.effective_expires_on > datetime.now(timezone.utc)
    assert answer.expiry_state is None


@pytest.mark.parametrize('payload', [dict(stack_id='custom'), dict(base_os='x')])
def test_extending_needs_the_stack(payload):
    with pytest.raises(exceptions.SocaException):
        admin_api(stack()).extend_software_stack(invocation(**payload))


@pytest.mark.parametrize('days', [0, -1, 3651])
def test_extending_bounds_the_days(days):
    api = admin_api(stack())
    with pytest.raises(exceptions.SocaException):
        api.extend_software_stack(
            invocation(stack_id='custom', base_os='amazonlinux2023', days=days)
        )
    api.software_stack_db.set_expiry.assert_not_called()


def test_extending_a_stack_that_is_gone_says_so():
    api = admin_api(None)
    with pytest.raises(exceptions.SocaException):
        api.extend_software_stack(invocation(stack_id='x', base_os='amazonlinux2023'))


def test_only_an_elevated_user_can_reach_the_admin_calls():
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
        APP_TOKEN_NAMESPACE_ACCESS,
    )

    assert 'VirtualDesktopAdmin.ExtendSoftwareStack' not in APP_TOKEN_NAMESPACE_ACCESS
    assert 'VirtualDesktopAdmin.ListOrphanInstances' not in APP_TOKEN_NAMESPACE_ACCESS


# the table


def test_a_row_from_before_these_fields_reads_as_empty_and_writes_nothing_new():
    db_dict = VirtualDesktopSoftwareStackDB.convert_software_stack_object_to_db_dict(
        stack()
    )
    assert 'owner' not in db_dict and 'expires_on' not in db_dict
    read = VirtualDesktopSoftwareStackDB.convert_db_dict_to_software_stack_object(
        db_dict
    )
    assert read.owner is None and read.expires_on is None


def edit_api(old, **new_fields):
    api = admin_api(old)
    api._validate_update_software_stack_request = lambda item: (item, True)
    api.software_stack_db.update.side_effect = lambda item, **_: item
    api.software_stack_db.set_expiry.side_effect = lambda item, on: item
    api.controller_utils = Mock()
    api.controller_utils.describe_image_id.return_value = {'ImageId': old.ami_id}
    new = stack(old.stack_id, **new_fields)
    new.projects = []
    call = Mock()
    call.get_request_payload_as.return_value = Mock(software_stack=new)
    api.update_software_stack(call)
    return api


def test_an_edit_that_asks_to_clear_the_date_clears_it():
    old = stack('dated', expires_on=NOW + timedelta(days=5))
    api = edit_api(old, clear_expires_on=True)
    assert api.software_stack_db.update.call_args.args[0].expires_on is None
    assert api.software_stack_db.update.call_args.kwargs == {'clear_expires_on': True}
    api.software_stack_db.set_expiry.assert_not_called()


def test_an_edit_that_says_nothing_about_the_date_keeps_it():
    on = NOW + timedelta(days=5)
    api = edit_api(stack('dated', expires_on=on))
    assert api.software_stack_db.update.call_args.args[0].expires_on == on
    assert api.software_stack_db.update.call_args.kwargs == {'clear_expires_on': False}
    api.software_stack_db.set_expiry.assert_not_called()


class Row:
    """one table row behind update_item, enough of DynamoDB for SET and REMOVE"""

    def __init__(self, item):
        self.item = dict(item)

    def update_item(self, **request):
        old = dict(self.item)
        names = request['ExpressionAttributeNames']
        sets, _, removes = request['UpdateExpression'][4:].partition(' REMOVE ')
        for token in sets.split(', '):
            name, value = token.split(' = ')
            self.item[names[name]] = request['ExpressionAttributeValues'][value]
        for name in filter(None, removes.split(', ')):
            self.item.pop(names[name], None)
        new = request['ReturnValues'] == 'ALL_NEW'
        return {'Attributes': dict(self.item) if new else old}


def edit_through_the_table(monkeypatch, stored, **request_fields):
    """a real edit against one row; returns the row and the index documents the edit's
    events carry, as the update handler builds them"""
    row = Row(
        VirtualDesktopSoftwareStackDB.convert_software_stack_object_to_db_dict(stored)
    )
    monkeypatch.setattr(VirtualDesktopSoftwareStackDB, '_table', row)
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db.context = Mock()
    db._table_name = 'stacks'
    events = []
    db.context.events_client.publish_event.side_effect = lambda event: events.append(
        event
    )
    api = admin_api(None)
    api.software_stack_db = db
    db.get = lambda stack_id, base_os: db.convert_db_dict_to_software_stack_object(
        row.item
    )
    api._validate_update_software_stack_request = lambda item: (item, True)
    api.controller_utils = Mock()
    api.controller_utils.describe_image_id.return_value = {'ImageId': stored.ami_id}
    new = stack(stored.stack_id, **request_fields)
    new.projects = []
    call = Mock()
    call.get_request_payload_as.return_value = Mock(software_stack=new)
    api.update_software_stack(call)
    documents = [
        db.convert_software_stack_object_to_index_dict(
            db.convert_db_dict_to_software_stack_object(event.detail['new_value'])
        )
        for event in events
    ]
    return row, events, documents


def index_after(start, documents):
    """the index merges each update into the stored document"""
    indexed = dict(start)
    for document in documents:
        indexed.update(document)
    return indexed


def test_a_cleared_date_is_gone_from_the_index_whatever_order_its_updates_land_in(
    monkeypatch,
):
    # dev, 6 of 10 clears: the edit sent the old date and then null as two index updates,
    # two controller tasks took one each, and the old date landed last
    import itertools

    on = NOW + timedelta(days=5)
    stored = stack('dated', expires_on=on, owner='a-person')
    row, events, documents = edit_through_the_table(
        monkeypatch, stored, clear_expires_on=True
    )
    assert 'expires_on' not in row.item
    assert documents
    before = {'expires_on': int(on.timestamp() * 1000), 'owner': 'a-person'}
    for order in itertools.permutations(documents):
        assert index_after(before, order)['expires_on'] is None
    assert index_after(before, documents)['owner'] == 'a-person'


def test_an_edit_that_keeps_the_date_keeps_it_in_the_table_and_the_index(monkeypatch):
    on = NOW + timedelta(days=5)
    row, events, documents = edit_through_the_table(
        monkeypatch, stack('dated', expires_on=on), description='new words'
    )
    assert row.item['expires_on'] == int(on.timestamp() * 1000)
    assert [document['expires_on'] for document in documents] == [
        row.item['expires_on']
    ]


def test_a_cleared_owner_is_gone_from_the_index(monkeypatch):
    # dev, 3 of 3: the table held an empty owner and the list kept the old name, since an
    # empty owner reads back as none and the index document then left the key out
    row, events, documents = edit_through_the_table(
        monkeypatch, stack('owned', owner='a-person'), owner=''
    )
    assert row.item['owner'] == ''
    assert index_after({'owner': 'a-person'}, documents)['owner'] is None
    read = VirtualDesktopSoftwareStackDB.__new__(
        VirtualDesktopSoftwareStackDB
    ).convert_index_dict_to_software_stack_object(index_after({}, documents))
    assert not read.owner


def test_every_event_for_one_stack_is_in_one_ordered_group(monkeypatch):
    # a base_os passed as the enum formatted as 'VirtualDesktopBaseOS.NAME-<id>', a second
    # group beside 'amazonlinux2023-<id>', so the queue kept no order between the two
    row = Row({'base_os': 'amazonlinux2023', 'stack_id': 'dated', 'ami_id': 'ami-old'})
    monkeypatch.setattr(VirtualDesktopSoftwareStackDB, '_table', row)
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db.context = Mock()
    db._table_name = 'stacks'
    db.convert_db_dict_to_software_stack_object = Mock()
    item = stack('dated')
    db.update(item)
    db.set_expiry(item, NOW)
    db.repoint_image(item, 'ami-dated', 'ami-new')
    groups = [
        call.kwargs['event'].event_group_id
        for call in db.context.events_client.publish_event.call_args_list
    ]
    assert groups == ['amazonlinux2023-dated'] * 3


def test_clearing_removes_the_attribute_and_only_the_date(monkeypatch):
    table = Mock()
    table.update_item.return_value = {
        'Attributes': {'base_os': 'amazonlinux2023', 'stack_id': 'dated'}
    }
    monkeypatch.setattr(VirtualDesktopSoftwareStackDB, '_table', table)
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db.trigger_update_event = Mock()
    db.convert_db_dict_to_software_stack_object = Mock(return_value='stack')
    assert db.set_expiry(stack('dated'), None) == 'stack'
    request = table.update_item.call_args.kwargs
    assert request['UpdateExpression'] == 'SET #upd = :upd REMOVE #exp'
    assert ':exp' not in request['ExpressionAttributeValues']


def test_an_update_reports_a_date_set_since_the_caller_read_the_stack(monkeypatch):
    # an image repoint that read the stack just before an admin dated it wrote the stack
    # back without the date: the table kept it, but the index was sent null and hid it
    on = NOW + timedelta(days=5)
    stored = VirtualDesktopSoftwareStackDB.convert_software_stack_object_to_db_dict(
        stack('dated', expires_on=on)
    )
    table = Mock()
    table.update_item.return_value = {'Attributes': stored}
    monkeypatch.setattr(VirtualDesktopSoftwareStackDB, '_table', table)
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db.trigger_update_event = Mock()
    read_before = stack('dated')
    read_before.ami_id = 'ami-new'
    updated = db.update(read_before)
    assert 'expires_on' not in table.update_item.call_args.kwargs['UpdateExpression']
    assert updated.expires_on.timestamp() == on.timestamp()
    assert updated.ami_id == 'ami-new'
    sent = db.trigger_update_event.call_args.kwargs['new_entry']
    assert sent['expires_on'] == stored['expires_on']


def test_the_clear_request_is_never_stored():
    db_dict = VirtualDesktopSoftwareStackDB.convert_software_stack_object_to_db_dict(
        stack(clear_expires_on=True)
    )
    assert 'clear_expires_on' not in db_dict


def test_a_cleared_date_reaches_the_index_as_null():
    # the index applies an update as a merge, so a key left out keeps the old date
    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    index_dict = db.convert_software_stack_object_to_index_dict(stack())
    assert 'expires_on' in index_dict and index_dict['expires_on'] is None
    read = db.convert_index_dict_to_software_stack_object(index_dict)
    assert read.expires_on is None
    on = NOW + timedelta(days=5)
    dated = db.convert_software_stack_object_to_index_dict(stack(expires_on=on))
    assert dated['expires_on'] == int(on.timestamp() * 1000)


def test_owner_and_date_round_trip():
    on = NOW + timedelta(days=5)
    db_dict = VirtualDesktopSoftwareStackDB.convert_software_stack_object_to_db_dict(
        stack(owner='a-person', expires_on=on)
    )
    read = VirtualDesktopSoftwareStackDB.convert_db_dict_to_software_stack_object(
        db_dict
    )
    assert read.owner == 'a-person'
    assert read.expires_on.timestamp() == on.timestamp()


def test_the_worked_out_fields_are_never_stored():
    item = stack()
    StackExpiry(context()).annotate([item], NOW)
    db_dict = VirtualDesktopSoftwareStackDB.convert_software_stack_object_to_db_dict(
        item
    )
    assert not {'expiry_state', 'expiry_warning', 'effective_expires_on'} & set(db_dict)


# the weekly digest


def service(stacks, sessions=(), **values):
    ctx = context(**values)
    db = Mock()
    db.list_all_from_db.return_value = SimpleNamespace(listing=stacks, cursor=None)
    session_db = Mock()
    session_db.list_all_from_db.return_value = SimpleNamespace(
        listing=list(sessions), cursor=None
    )
    result = StackExpiryService(ctx, db, session_db)
    result.metrics = Mock()
    return result


def sent(svc):
    return [
        call.args[0]
        for call in svc.context.notification_async_client.send_notification.call_args_list
    ]


def test_one_email_per_owner_and_the_rest_to_the_administrator():
    stacks = [
        stack('a1', owner='alice'),
        stack('a2', owner='alice'),
        stack('b', owner='bob'),
        stack('nobody'),
        stack('blank', owner=''),
        rebuilt(),
    ]
    svc = service(stacks, enforce_from=POLICY)
    assert svc.send_digest(stacks, NOW) == 3
    mail = {m.username: m for m in sent(svc)}
    assert set(mail) == {'alice', 'bob', 'clusteradmin'}
    assert [s['name'] for s in mail['alice'].params['stacks']] == ['a1', 'a2']
    assert [s['name'] for s in mail['clusteradmin'].params['stacks']] == [
        'blank',
        'nobody',
    ]
    assert mail['alice'].template_name == DIGEST_TEMPLATE
    assert mail['alice'].params['hiding'] is False


def test_a_second_pass_inside_the_week_sends_nothing():
    stacks = [stack(owner='alice')]
    svc = service(stacks, enforce_from=POLICY)
    assert svc.send_digest(stacks, NOW) == 1
    assert svc.send_digest(stacks, NOW + timedelta(days=6)) == 0
    assert svc.send_digest(stacks, NOW + timedelta(days=7)) == 1
    assert len(sent(svc)) == 2


def test_another_leader_that_claimed_the_week_means_nothing_sent():
    stacks = [stack(owner='alice')]
    svc = service(stacks)
    svc.context.config().db.set_config_entry_if.return_value = False
    assert svc.send_digest(stacks, NOW) == 0
    assert sent(svc) == []


def test_nothing_to_say_claims_nothing_and_sends_nothing():
    stacks = [rebuilt(), stack('fresh', age_days=1)]
    svc = service(stacks)
    assert svc.send_digest(stacks, NOW) == 0
    svc.context.config().db.set_config_entry_if.assert_not_called()


def test_exempt_stacks_are_not_in_the_digest_and_the_switch_stops_it():
    old = stack(owner='alice')
    old.ami_id = EXEMPT
    svc = service([old], exempt_amis=[EXEMPT])
    assert svc.send_digest([old], NOW) == 0
    off = service([stack(owner='alice')], digest_enabled=False)
    assert off.send_digest([stack(owner='alice')], NOW) == 0


def test_one_owner_failing_does_not_stop_the_others():
    stacks = [stack('a', owner='alice'), stack('b', owner='bob')]
    svc = service(stacks, enforce_from=POLICY)
    svc.context.notification_async_client.send_notification.side_effect = [
        RuntimeError('no such user'),
        None,
    ]
    assert svc.send_digest(stacks, NOW) == 1


def test_the_digest_template_renders_in_plain_words():
    defaults = (
        Path(module.__file__).parents[5]
        / 'idea-cluster-manager/resources/defaults/email_templates.yml'
    )
    templates = {
        t['name']: t for t in yaml.safe_load(defaults.read_text())['templates']
    }
    template = templates[DIGEST_TEMPLATE]
    params = {
        'cluster_name': 'idea-test',
        'username': 'alice',
        'hiding': False,
        'stacks': [{'name': 'custom', 'state': 'expired', 'expires_on': '2026-01-01'}],
    }
    body = jinja2.Template(template['body']).render(**params)
    assert 'custom' in body and 'expired 2026-01-01' in body
    assert 'still offered' in body
    assert 'idea-test' in jinja2.Template(template['subject']).render(**params)


def test_levels_are_counted_from_enabled_stacks():
    stacks = [
        stack('a'),
        stack('b', expires_on=NOW + timedelta(days=3)),
        rebuilt(),
    ]
    disabled = stack('off')
    disabled.enabled = False
    svc = service(stacks + [disabled], enforce_from=POLICY)
    assert disabled not in svc.all_stacks()
    svc.publish_levels(svc.all_stacks(), NOW)
    svc.metrics.level.assert_any_call('stacks.expiry', 1, state='expired')
    svc.metrics.level.assert_any_call('stacks.expiry', 1, state='expiring')
    svc.metrics.level.assert_any_call('stacks.expiry_enforced', 0)


# instances with no session


def instance(instance_id, age_hours=48, state='running'):
    return {
        'InstanceId': instance_id,
        'InstanceType': 'c7i.8xlarge',
        'State': {'Name': state},
        'LaunchTime': NOW - timedelta(hours=age_hours),
        'Tags': [{'Key': 'Name', 'Value': 'orphan'}],
    }


def orphans(instances, known):
    ctx = context()
    paginator = ctx.aws.return_value.ec2.return_value.get_paginator.return_value
    paginator.paginate.return_value = [{'Reservations': [{'Instances': instances}]}]
    session_db = Mock()
    session_db.list_all_from_db.return_value = SimpleNamespace(
        listing=[
            VirtualDesktopSession(server=VirtualDesktopServer(instance_id=i))
            for i in known
        ],
        cursor=None,
    )
    return OrphanInstances(ctx, session_db), paginator


def test_an_instance_with_no_session_row_is_listed_and_nothing_is_done_to_it():
    finder, paginator = orphans(
        [instance('i-orphan'), instance('i-known'), instance('i-new', age_hours=0)],
        ['i-known'],
    )
    listed = finder.list(NOW)
    assert [o.instance_id for o in listed] == ['i-orphan']
    assert listed[0].instance_type == 'c7i.8xlarge' and listed[0].state == 'running'
    filters = paginator.paginate.call_args.kwargs['Filters']
    assert {
        'Name': 'tag:idea:NodeType',
        'Values': ['virtual-desktop-dcv-host'],
    } in filters
    assert {'Name': 'tag:idea:ClusterName', 'Values': ['idea-test']} in filters
    ec2 = finder.context.aws().ec2()
    ec2.terminate_instances.assert_not_called()
    ec2.stop_instances.assert_not_called()


def test_with_no_grace_a_new_instance_is_listed_unless_a_row_names_it():
    # the default hour hides every new desktop, so a check of the row matching made while
    # desktops are new passes even if the matching is broken; with no grace it can fail
    finder, _ = orphans(
        [instance('i-new', age_hours=0), instance('i-new-known', age_hours=0)],
        ['i-new-known'],
    )
    assert finder.list(NOW) == []
    listed = finder.list(NOW, min_age=timedelta(0))
    assert [o.instance_id for o in listed] == ['i-new']


def test_the_admin_list_takes_the_grace_in_minutes_and_refuses_a_negative():
    api = admin_api(stack())
    # the API reads the clock: launched just now
    launched = instance('i-new')
    launched['LaunchTime'] = datetime.now(timezone.utc)
    api.context.aws().ec2().get_paginator().paginate.return_value = [
        {'Reservations': [{'Instances': [launched]}]}
    ]
    api.session_db.list_all_from_db.return_value = SimpleNamespace(
        listing=[], cursor=None
    )
    for minutes, expected in ((None, []), (60, []), (0, ['i-new'])):
        call = invocation(min_age_minutes=minutes)
        api.list_orphan_instances(call)
        listing = call.success.call_args.args[0].listing
        assert [o.instance_id for o in listing] == expected, minutes
    with pytest.raises(exceptions.SocaException):
        api.list_orphan_instances(invocation(min_age_minutes=-1))


@pytest.mark.parametrize('minutes', [525601, 10**13])
def test_the_admin_list_refuses_a_grace_longer_than_a_year(minutes):
    # 10**13 minutes is past what a timedelta holds and came back as a server error
    api = admin_api(stack())
    with pytest.raises(exceptions.SocaException) as error:
        api.list_orphan_instances(invocation(min_age_minutes=minutes))
    assert error.value.error_code == errorcodes.INVALID_PARAMS


def test_the_count_is_a_level_and_a_failed_read_publishes_no_zero():
    svc = service([])
    svc.orphans = Mock()
    svc.orphans.list.return_value = [Mock(), Mock()]
    svc.publish_orphans(NOW)
    svc.metrics.level.assert_called_once_with('desktop.orphan_instances', 2)

    broken = service([])
    broken.orphans = Mock()
    broken.orphans.list.side_effect = RuntimeError('denied')
    broken.publish_orphans(NOW)
    broken.metrics.level.assert_not_called()


def test_the_admin_list_comes_from_the_same_read():
    api = admin_api(stack())
    api.context.aws().ec2().get_paginator().paginate.return_value = []
    api.session_db.list_all_from_db.return_value = SimpleNamespace(
        listing=[], cursor=None
    )
    call = invocation()
    api.list_orphan_instances(call)
    assert call.success.call_args.args[0].listing == []


# the listing the new desktop form reads


def user_api(stacks, **values):
    from ideadatamodel import ListSoftwareStackResponse, SocaPaginator
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_user_api import (
        VirtualDesktopUserAPI,
    )

    api = VirtualDesktopUserAPI.__new__(VirtualDesktopUserAPI)
    api.context = context(**values)
    api._logger = Mock()
    api._list_software_stacks = lambda request: ListSoftwareStackResponse(
        listing=stacks, paginator=SocaPaginator(total=len(stacks))
    )
    return api


def test_the_form_gets_everything_with_warnings_until_the_date_comes():
    api = user_api([stack('old'), rebuilt()], enforce_from=POLICY)
    call = invocation(project_id='p')
    api.list_software_stacks(call)
    answer = call.success.call_args.args[0]
    assert [s.stack_id for s in answer.listing] == ['old', 'base']
    assert 'Use base instead' in answer.listing[0].expiry_warning
    assert answer.paginator.total == 2


def test_the_form_stops_getting_expired_stacks_from_the_date():
    api = user_api([stack('old'), rebuilt()], enforce_from='2026-01-01')
    call = invocation(project_id='p')
    api.list_software_stacks(call)
    answer = call.success.call_args.args[0]
    assert [s.stack_id for s in answer.listing] == ['base']
    assert answer.paginator.total == 1


def test_an_expiry_that_cannot_be_worked_out_still_answers_the_form():
    api = user_api([stack('old')])
    api.context.config.side_effect = RuntimeError('no table')
    call = invocation(project_id='p')
    api.list_software_stacks(call)
    assert [s.stack_id for s in call.success.call_args.args[0].listing] == ['old']


def test_an_owner_that_is_not_a_username_is_refused():
    from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
        STACK_OWNER_PATTERN,
    )

    assert STACK_OWNER_PATTERN.fullmatch('a.person@example')
    assert STACK_OWNER_PATTERN.fullmatch('')
    assert not STACK_OWNER_PATTERN.fullmatch('a person')
    assert not STACK_OWNER_PATTERN.fullmatch('<b>')


def test_the_counts_are_a_health_collector_with_its_own_switch_and_the_digest_is_not():
    stacks = [stack(owner='alice')]
    svc = service(stacks, enforce_from=POLICY)
    svc.orphans = Mock()
    svc.orphans.list.return_value = []
    assert svc.name == 'stack_expiry' and svc.leader_only is True
    svc.collect(None, NOW)
    svc.metrics.level.assert_any_call('stacks.expiry', 1, state='expired')
    svc.metrics.level.assert_any_call('desktop.orphan_instances', 0)
    assert sent(svc) == []
    svc.tick(NOW)
    assert len(sent(svc)) == 1
    # and it waits for the next tick
    svc.tick(NOW + timedelta(days=30))
    assert len(sent(svc)) == 1
