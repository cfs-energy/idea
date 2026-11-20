"""Owner holds: while a folder is removed its owner's jobs wait to start, and only that long."""

from types import SimpleNamespace
from unittest.mock import Mock
import ast
import os
import time

import pytest

from ideascheduler.app.scratch_cleanup import filesystem as fs_module
from ideascheduler.app.scratch_cleanup import service as module
from ideascheduler.app.scratch_cleanup.holds import HOLD_TTL_SECONDS
import test_scratch_cleanup_service as base
from test_scratch_cleanup_service import job_folder, run

world = base.world


HOOK = (
    module.__file__.split('/src/')[0]
    + '/resources/openpbs/hooks/openpbs_hook_handler.py'
)


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
        'SCRATCH_HOLD_TTL_SECS': HOLD_TTL_SECONDS,
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
    old = time.time() - HOLD_TTL_SECONDS - 1
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
    base.re_tell(world)
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
    base.re_tell(world)
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
