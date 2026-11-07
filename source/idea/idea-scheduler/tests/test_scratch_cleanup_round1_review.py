"""What the first review found. Each runs against the real service over a real tree."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import ast
import os
import time

import boto3
import pytest
from moto import mock_aws

from ideadatamodel import exceptions
from ideascheduler.app.scratch_cleanup import filesystem as fs_module
from ideascheduler.app.scratch_cleanup import service as module
from ideascheduler.app.scratch_cleanup.store import ScratchCleanupStore
import test_scratch_cleanup_service as base
from test_scratch_cleanup_service import (
    digests,
    digests_on,
    due_in,
    finished_job,
    job_folder,
    put_event,
    record,
    run,
    week,
)
from test_scratch_cleanup_store_and_api import Invocation, api

HOOK = (
    module.__file__.split('/src/')[0]
    + '/resources/openpbs/hooks/openpbs_hook_handler.py'
)


world = base.world


def removal_rows(world):
    return list(world.store.removals.values())


# H1: nothing on the record while off, and nothing read whole


@pytest.mark.parametrize(
    'switches',
    [{'enabled': False}, {'dry_run': True}, {'kill_switch': True}],
    ids=['off', 'dry run', 'kill switch'],
)
def test_nothing_is_written_unless_armed(world, switches):
    world.switches.update(switches)
    world.service.record_job_finished(finished_job())
    assert not world.store.removals


def test_off_does_not_even_read_the_table(world):
    world.config['scheduler.scratch_cleanup.enabled'] = False
    table = world.context.aws().dynamodb_table().Table()
    table.get_item.reset_mock()
    world.store.get_removal = Mock(side_effect=AssertionError('read the record'))
    world.service.record_job_finished(finished_job())
    assert not table.get_item.called


def test_the_page_does_not_read_the_record_whole(world, monkeypatch):
    job_folder(world)
    monkeypatch.setattr(
        world.store, 'list_due', Mock(side_effect=AssertionError('read it all'))
    )
    assert world.service.status()['scheduled_folders'] == 1


@pytest.fixture()
def real_store(monkeypatch):
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.delenv('AWS_CONFIG_FILE', raising=False)
    with mock_aws():
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        resource.create_table(
            TableName='t',
            AttributeDefinitions=[
                {'AttributeName': 'pk', 'AttributeType': 'S'},
                {'AttributeName': 'sk', 'AttributeType': 'S'},
            ],
            KeySchema=[
                {'AttributeName': 'pk', 'KeyType': 'HASH'},
                {'AttributeName': 'sk', 'KeyType': 'RANGE'},
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        instance = ScratchCleanupStore(Mock())
        instance.table = resource.Table('t')
        yield instance


def removal(day, **more):
    return {
        'job_id': '1',
        'owner': 'a',
        'remove_at': f'2026-12-{day:02d}T00:00:00',
        **more,
    }


def test_due_reads_stop_at_the_date_and_the_limit_oldest_first(real_store):
    for day in range(1, 11):
        real_store.put_removal(f'k{day}', removal(day))
    due = real_store.list_due('2026-12-05T12:00:00', limit=100)
    assert [row['key'] for row in due] == ['k1', 'k2', 'k3', 'k4', 'k5']
    assert [row['key'] for row in real_store.list_due('2026-12-31', limit=3)] == [
        'k1',
        'k2',
        'k3',
    ]


def test_a_new_date_replaces_the_old_row(real_store):
    real_store.put_removal('k', removal(1))
    real_store.put_removal('k', removal(9, outcome='failed'))
    assert real_store.count_removals() == 1
    assert real_store.get_removal('k')['outcome'] == 'failed'
    assert real_store.has_removal('k')
    real_store.delete_removal('k')
    assert not real_store.has_removal('k')
    assert real_store.get_removal('k') is None
    assert real_store.count_removals() == 0


def test_counts_do_not_keep_rows(real_store):
    real_store.put_removal('a', removal(1, outcome='failed'))
    real_store.put_removal('b', removal(2, outcome='succeeded'))
    assert real_store.count_removals() == 2
    assert real_store.count_removals(outcome='failed') == 1


def test_upcoming_pages_with_a_cursor(real_store):
    for day in range(1, 6):
        real_store.put_removal(f'k{day}', removal(day))
    rows, cursor = real_store.page_removals(2)
    assert [row['key'] for row in rows] == ['k1', 'k2'] and cursor
    rows, _ = real_store.page_removals(2, cursor)
    assert [row['key'] for row in rows] == ['k3', 'k4']


# Astra 3: no notices means no removal


def test_there_is_no_setting_that_skips_notices(world):
    world.config['scheduler.scratch_cleanup.notify_owners'] = False
    folder = job_folder(world, with_notices=False)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


@pytest.mark.parametrize(
    'broken',
    [
        lambda world: world.config.update({'cluster.ses.enabled': False}),
        lambda world: world.config.update(
            {'cluster-manager.notifications.email.enabled': False}
        ),
    ],
    ids=['ses off', 'email off'],
)
def test_delivery_off_pauses_removal_even_past_every_date(world, broken):
    digests_on(world)
    broken(world)
    folder = job_folder(world, with_notices=False)
    assert run(world)['removed_folders'] == 0
    week(world, 20)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


# M3: the choice lasts one preview and backfill


# M5: a cluster administrator only


@pytest.mark.parametrize(
    'namespace',
    ['SchedulerAdmin.PreviewScratchCleanup', 'SchedulerAdmin.BackfillScratchCleanup'],
)
def test_a_module_manager_cannot_preview_or_backfill(namespace):
    service = Mock()
    invocation = Invocation(namespace, elevated=True)
    invocation.is_administrator = lambda: False
    with pytest.raises(exceptions.SocaException):
        api(service).invoke(invocation)
    assert not service.mock_calls


# L1, L2, L3, L6, L7


def test_a_second_file_system_not_mounted_does_not_stop_the_check(world):
    world.config['shared-storage']['team'] = {
        'provider': 'fsx_lustre',
        'mount_dir': str(world.mount) + '-absent',
    }
    world.config['scheduler.scratch_cleanup.file_systems'] = ['scratch', 'team']
    put_event(world, record('555'))
    outcome = run(world)
    assert outcome['status'] != 'failed', outcome.get('message')


def test_subjobs_of_one_array_do_not_replace_each_others_record(world):
    world.service.record_job_finished(finished_job('77[1]', uid='shared'))
    world.service.record_job_finished(finished_job('77[2]', uid='shared'))
    world.service.record_job_finished(finished_job('77[]', uid='shared'))
    assert {row['job_id'] for row in removal_rows(world)} == {'77[1]', '77[2]', '77[]'}


def test_a_digest_day_hiccup_retries_on_the_next_check(world):
    digests_on(world)
    due_in(world, days=10)
    world.state.records_error = module.StateUnavailable('index busy')
    assert run(world)['noticed_folders'] == 0
    world.state.records_error = None
    world.now += timedelta(hours=1)
    assert run(world)['noticed_folders'] == 1
    assert len(digests(world)) == 1


def test_a_cursor_with_a_bad_key_is_refused(world):
    import base64
    import json

    cursor = base64.urlsafe_b64encode(
        json.dumps({'pk': 'audit', 'sk': 5}).encode()
    ).decode()
    with pytest.raises(exceptions.SocaException):
        world.service.list_audit(10, cursor)


def test_a_job_relearned_after_a_task_loss_goes_on_the_record():
    from ideascheduler.app.provisioning.job_monitor import (
        finished_job_processor as path,
    )

    context = Mock()
    processor = path.FinishedJobProcessor.__new__(path.FinishedJobProcessor)
    processor._context = context
    processor._logger = Mock()
    indexed = SimpleNamespace(job_uid='u')
    context.job_cache.get_completed_job_by_uid.return_value = None
    context.document_store.get_job.return_value = indexed
    assert processor._recorded_job('u') is indexed
    context.scratch_cleanup.record_job_finished.assert_called_once_with(indexed)


def test_the_finished_job_path_never_opens_the_table(world):
    world.service._store_ready = False
    world.store.initialize = Mock(side_effect=AssertionError('blocked finishing a job'))
    world.service.record_job_finished(finished_job())
    assert not world.store.removals


# Astra 2: an exclusion, not polling


def _hook_function():
    source = open(HOOK).read()
    tree = ast.parse(source)
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == 'scratch_hold_reason'
    )
    scope = {
        'os': os,
        'time': time,
        'SCRATCH_HOLDS_DIR': '/run/idea-scratch-holds',
        'SCRATCH_HOLD_TTL_SECS': module.HOLD_TTL_SECONDS,
    }
    exec(compile(ast.Module([node], []), HOOK, 'exec'), scope)
    return scope['scratch_hold_reason']


def owner_event(owner='alice@10.0.0.5'):
    return SimpleNamespace(job=SimpleNamespace(Job_Owner=owner, euser=None))


class BrokenJob:
    @property
    def job(self):
        raise RuntimeError('no such attribute')


def test_a_job_whose_owner_cannot_be_read_waits_while_any_removal_runs(world):
    holds = world.service._holds
    reason = _hook_function()
    with holds.hold('alice'):
        assert 'Scratch cleanup' in reason(BrokenJob(), holds.directory)


def test_a_job_whose_owner_cannot_be_read_starts_when_nothing_is_being_removed(
    world, tmp_path
):
    assert _hook_function()(BrokenJob(), str(tmp_path / 'never-made')) is None
    holds = world.service._holds
    with holds.hold('alice'):
        pass
    assert _hook_function()(BrokenJob(), holds.directory) is None


def test_a_hold_that_cannot_be_decoded_still_holds(world, tmp_path):
    (tmp_path / 'alice').write_bytes(b'\xff\xfe')
    assert 'Scratch cleanup' in _hook_function()(owner_event(), str(tmp_path))


def test_the_hold_starts_after_the_walk_of_the_folder(world, monkeypatch):
    folder = job_folder(world)
    holds = world.service._holds
    seen = {}
    real_inspect = module.inspect_folder

    def inspecting(*args, **kwargs):
        seen.setdefault('walks', []).append(holds.current)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(module, 'inspect_folder', inspecting)
    real_active = world.state.active_jobs

    def active_jobs(aliases):
        seen['last_look'] = holds.current
        return real_active(aliases)

    monkeypatch.setattr(world.state, 'active_jobs', active_jobs)
    assert run(world)['removed_folders'] == 1
    assert not folder.exists()
    assert seen['walks'][-1] is None, 'jobs wait for the removal, not for this walk'
    assert seen['last_look'] is not None


def test_a_job_start_is_refused_for_exactly_as_long_as_a_removal_holds(
    world, monkeypatch
):
    holds = world.service._holds
    reason = _hook_function()
    assert reason(owner_event(), holds.directory) is None
    with holds.hold('alice'):
        assert 'Scratch cleanup' in reason(owner_event(), holds.directory)
        assert reason(owner_event('ALICE@h'), holds.directory), 'whatever the case'
        assert reason(owner_event('bob@h'), holds.directory) is None
    assert reason(owner_event(), holds.directory) is None


def test_an_expired_hold_lets_jobs_start(world):
    holds = world.service._holds
    reason = _hook_function()
    os.makedirs(holds.directory, exist_ok=True)
    with open(os.path.join(holds.directory, 'alice'), 'w') as stale:
        stale.write(str(time.time() - 1))
    assert reason(owner_event(), holds.directory) is None


def test_a_hold_nobody_can_read_lets_jobs_start_only_after_expiry(world):
    holds = world.service._holds
    reason = _hook_function()
    os.makedirs(holds.directory, exist_ok=True)
    with open(os.path.join(holds.directory, 'alice'), 'w') as garbled:
        garbled.write('not a time')
    assert reason(owner_event(), holds.directory), 'unreadable is held, until it is old'
    old = time.time() - module.HOLD_TTL_SECONDS - 1
    os.utime(os.path.join(holds.directory, 'alice'), (old, old))
    assert reason(owner_event(), holds.directory) is None


def test_an_owner_name_cannot_leave_the_holds_folder(world, tmp_path):
    reason = _hook_function()
    outside = tmp_path / 'x'
    outside.write_text(str(time.time() + 500))
    assert reason(owner_event('../x@h'), str(tmp_path / 'holds')) is None


def test_the_hold_is_taken_before_the_last_look_at_the_scheduler(world, monkeypatch):
    job_folder(world)
    holds = world.service._holds
    seen = []
    real = world.state.active_jobs

    def spy(aliases):
        seen.append(os.path.exists(os.path.join(holds.directory, 'alice')))
        return real(aliases)

    world.state.active_jobs = spy
    assert run(world)['removed_folders'] == 1
    assert seen[0] is False, 'none needed for the first look'
    assert seen[-1] is True, 'held during the last look'
    assert not os.path.exists(os.path.join(holds.directory, 'alice')), 'released'


def test_the_hold_lasts_through_the_removal_itself(world, monkeypatch):
    job_folder(world)
    holds = world.service._holds
    real = module.remove_folder
    seen = {}

    def spy(*args, **kwargs):
        seen['held'] = os.path.exists(os.path.join(holds.directory, 'alice'))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, 'remove_folder', spy)
    run(world)
    assert seen['held'] is True


def test_the_hold_is_released_when_a_removal_fails(world, monkeypatch):
    job_folder(world)
    monkeypatch.setattr(module, 'remove_folder', Mock(side_effect=OSError('broken')))
    run(world)
    assert not os.listdir(world.service._holds.directory)


def test_no_removal_without_a_hold(world, monkeypatch):
    folder = job_folder(world)
    monkeypatch.setattr(
        world.service._holds, 'write', Mock(side_effect=OSError('read-only'))
    )
    outcome = run(world)
    assert outcome['status'] == 'stopped' and outcome['removed_folders'] == 0
    assert folder.exists()


def test_a_removal_that_outlives_its_hold_stops_before_the_next_file(
    world, monkeypatch
):
    folder = job_folder(world)
    for index in range(50):
        (folder / f'f{index}').touch()
    holds = world.service._holds
    real = module.remove_folder

    def expired(*args, **kwargs):
        holds.current.expires_at = time.time() - 1
        return real(*args, **kwargs)

    monkeypatch.setattr(module, 'remove_folder', expired)
    outcome = run(world)
    assert outcome['status'] == 'stopped' and outcome['removed_folders'] == 0
    assert len(os.listdir(folder)) >= 50


def test_a_removal_too_long_for_its_owners_jobs_stops_and_resumes_later(
    world, monkeypatch
):
    monkeypatch.setattr(module, 'MAX_HOLD_SECONDS', -1)
    folder = job_folder(world)
    for index in range(300):
        (folder / f'f{index}').touch()
    outcome = run(world)
    assert outcome['status'] == 'stopped' and 'longer' in outcome['message']
    assert folder.exists()
    assert not os.listdir(world.service._holds.directory)


def test_the_stop_check_runs_before_every_file(tmp_path):
    folder = tmp_path / 'job'
    folder.mkdir()
    (folder / 'only-one').touch()
    fd = os.open(tmp_path, os.O_RDONLY)
    folder_fd = fs_module.open_child_dir(fd, 'job')
    calls = []

    def stop():
        calls.append(1)
        raise fs_module.FolderInUse('stop')

    try:
        with pytest.raises(fs_module.FolderInUse):
            fs_module.remove_folder(
                fd,
                'job',
                folder_fd,
                fs_module.identity_of(folder_fd),
                lambda: None,
                not_before=0.0,
                not_after=2.0**40,
                dirs_not_after=2.0**40,
                before_each=stop,
            )
    finally:
        os.close(folder_fd)
        os.close(fd)
    assert calls and (folder / 'only-one').exists()


def test_the_hook_refuses_a_held_job_and_passes_others_without_the_scheduler():
    source = open(HOOK).read()
    entry = source.split('\ne = pbs.event()\n', 1)[1]
    assert 'scratch_hold_reason(e' in entry
    assert entry.index('scratch_hold_reason(e') < entry.index(
        'invoke_soca_scheduler(e)'
    )
