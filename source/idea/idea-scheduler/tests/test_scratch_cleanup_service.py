"""
End to end on a real directory tree, with the scheduler, job records and table faked. The tree
stands in for the Lustre mount; every test that expects nothing removed checks the tree.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import os
import shutil
import time

import pytest

from ideadatamodel import SocaJob, SocaJobState, exceptions
from ideascheduler.app.scratch_cleanup import service as module
from ideascheduler.app.scratch_cleanup.job_state import StateUnavailable
from ideascheduler.app.scratch_cleanup.rules import ActiveJobs, JobRecord, Reason
from ideascheduler.app.scratch_cleanup.service import (
    ALERT_MARKER,
    TEMPLATE_ADMIN_SUMMARY,
    TEMPLATE_DIGEST,
    ScratchCleanupService,
)
from ideascheduler.app.scratch_cleanup.store import LeaseLost

REAL_NOW = datetime.now(timezone.utc)
# folder timestamps are the real now and cannot be set back, so the clock moves forward instead
LATER = REAL_NOW + timedelta(days=90)
GiB = 2**30


class FakeStore:
    def __init__(self):
        self.runs = {}
        self.audit = {}
        self.removals = {}
        self.removed = {}
        self.failures = {}
        self.clock = None
        self.lease_holder = None
        self.lease_free = True
        self.renew_error = None
        self.alerts = {}

    def initialize(self):
        pass

    def acquire_lease(self, holder):
        if not self.lease_free:
            return False
        self.lease_holder = holder
        return True

    def renew_lease(self, holder):
        if self.renew_error:
            raise self.renew_error

    def release_lease(self, holder):
        self.lease_holder = None

    def put_run(self, run):
        self.runs[run['run_id']] = dict(run)

    def list_runs(self, limit=20):
        return sorted(
            (dict(run) for run in self.runs.values()),
            key=lambda run: run['started_at'],
            reverse=True,
        )[:limit]

    def put_audit(self, row):
        self.audit[(row['started_at'], row['path'])] = dict(row)

    def list_audit(self, limit=50, start_key=None):
        return list(self.audit.values())[:limit], None

    def unfinished_removals(self):
        return {
            (row['device'], row['inode']): dict(row)
            for row in self.audit.values()
            if row['status'] in ('started', 'failed')
        }

    def list_due(self, before, limit):
        rows = sorted(
            ({**row, 'key': key} for key, row in self.removals.items()),
            key=lambda row: row['remove_at'],
        )
        return [row for row in rows if row['remove_at'] < f'{before}~'][:limit]

    def page_removals(self, limit, start_key=None):
        rows = sorted(
            ({**row, 'key': key} for key, row in self.removals.items()),
            key=lambda row: row['remove_at'],
        )
        return rows[:limit], None

    def count_removals(self, outcome=None):
        return sum(
            1
            for row in self.removals.values()
            if outcome is None or row.get('outcome') == outcome
        )

    def has_removal(self, key):
        return key in self.removals

    def get_removal(self, key):
        row = self.removals.get(key)
        return {**row, 'key': key} if row else None

    def put_removal(self, key, row):
        self.removals[key] = {k: v for k, v in row.items() if v is not None}

    def delete_removal(self, key):
        self.removals.pop(key, None)

    def list_removed(self):
        return [{**row, 'sk': key} for key, row in self.removed.items()]

    def put_removed(self, key, row):
        self.removed[key] = dict(row)

    def delete_removed(self, key):
        self.removed.pop(key, None)

    def list_failures(self):
        return [{**row, 'sk': key} for key, row in self.failures.items()]

    def put_failure(self, key, row):
        self.failures[key] = dict(row)

    def delete_failure(self, key):
        self.failures.pop(key, None)

    def release_alert(self, reason):
        self.alerts.pop(reason, None)

    def should_alert(self, reason, every_seconds=86400):
        now = self.clock().timestamp()
        if now - self.alerts.get(reason, -1e12) < every_seconds:
            return False
        self.alerts[reason] = now
        return True


class FakeState:
    def __init__(self):
        self.records = {}
        self.active = ActiveJobs()
        self.active_error = None
        self.records_error = None
        self.on_active = None
        self.active_calls = 0

    def active_jobs(self, aliases):
        self.active_calls += 1
        if self.on_active:
            self.on_active(self.active_calls)
        if self.active_error:
            raise self.active_error
        return self.active

    def finished_records(self, forms):
        if self.records_error:
            raise self.records_error
        return [record for form in forms for record in self.records.get(form, [])]

    def find_job(self, record):
        return SimpleNamespace(job_id=record.job_id, scratch_removed_at=None)


class Config:
    def __init__(self, values):
        self.values = values

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def get_string(self, key, default=None):
        return self.values.get(key, default)

    def get_int(self, key, default=None):
        return self.values.get(key, default)

    def get_bool(self, key, default=None):
        return self.values.get(key, default)

    def get_list(self, key, default=None):
        return self.values.get(key, default)

    def get_cluster_external_endpoint(self):
        return 'https://portal.example.com'


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(module, '_now', lambda: world.now)
    mount = tmp_path / 'lustre'
    mount.mkdir()
    mountinfo = tmp_path / 'mountinfo'
    mountinfo.write_text(f'40 1 0:40 / {mount} rw - lustre 10.0.0.9@tcp:/abcdefgh rw\n')

    world = SimpleNamespace()
    world.now = LATER
    world.mount = mount
    world.mountinfo = mountinfo
    world.switches = {
        'enabled': True,
        'dry_run': False,
        'kill_switch': False,
    }
    world.switch_error = None
    world.store = FakeStore()
    world.store.clock = lambda: world.now
    world.state = FakeState()
    world.sent = []
    # 4 GiB capacity, used per test through usage()
    world.used_percent = 95.0
    world.capacity = 1000 * GiB
    monkeypatch.setattr(module, 'usage', lambda _: _usage(world))

    world.config = {
        'shared-storage': {
            'scratch': {
                'provider': 'fsx_lustre',
                'mount_dir': str(mount),
                'title': 'Scratch',
                'fsx_lustre': {'mount_name': 'abcdefgh', 'file_system_id': 'fs-1'},
            },
        },
        'cluster.home_dir': '/apps/idea',
        'cluster.administrator_username': 'admin',
        'cluster.ses.enabled': True,
        'cluster-manager.notifications.email.enabled': True,
        'scheduler.scratch_cleanup.file_systems': ['scratch'],
        'scheduler.scratch_cleanup.enabled': True,
        # the test clock is the real one plus 90 days, so one real day a week it lands on
        # the default digest day, Monday. A test that wants a digest day says so
        # (digests_on); otherwise the day is one no test's clock reaches by an hour or two.
        'scheduler.scratch_cleanup.digest_weekday': (LATER.weekday() + 3) % 7,
    }

    table = Mock()

    def get_item(Key, ConsistentRead):
        assert ConsistentRead
        if world.switch_error:
            raise world.switch_error
        leaf = Key['key'].rsplit('.', 1)[1]
        return {'Item': {'value': world.switches[leaf]}}

    table.get_item.side_effect = get_item
    context = Mock()
    context.cluster_name.return_value = 'idea-test'
    context.module_id.return_value = 'scheduler'
    context.logger.return_value = Mock()
    context.config.return_value = Config(world.config)
    context.aws.return_value.dynamodb_table.return_value.Table.return_value = table
    context.accounts_client.get_user.side_effect = lambda request: SimpleNamespace(
        user=SimpleNamespace(
            uid=world.uids.get(request.username),
            enabled=request.username not in world.disabled,
            email=None if request.username in world.no_email else 'a@example.com',
        )
    )
    context.notifications_client.send_notification.side_effect = lambda notification: (
        world.sent.append(notification)
    )
    world.fsx = context.aws.return_value.fsx.return_value
    world.fsx.describe_file_systems.return_value = {
        'FileSystems': [{'LustreConfiguration': {'DeploymentType': 'SCRATCH_2'}}]
    }
    world.fsx.describe_data_repository_associations.return_value = {'Associations': []}
    world.uids = {name: os.getuid() for name in ('alice', 'bob')}
    world.disabled = set()
    world.no_email = set()
    world.context = context
    world.service = ScratchCleanupService(
        context,
        store=world.store,
        state=world.state,
        mountinfo_path=str(mountinfo),
        holds_dir=str(tmp_path / 'holds'),
    )
    world.service._store_ready = True
    monkeypatch.setattr(module, 'ScratchCleanupMetrics', lambda *_: Mock())
    return world


def _usage(world):
    used = int(world.capacity * world.used_percent / 100)
    return world.capacity, used, world.used_percent


def record(job_id, owner='alice', **overrides):
    values = dict(
        job_id=job_id,
        job_uid=f'uid-of-{job_id}-{owner}',
        owner=owner,
        finished=True,
        start_time=REAL_NOW - timedelta(hours=1),
        end_time=REAL_NOW + timedelta(minutes=1),
    )
    values.update(overrides)
    return JobRecord(**values)


def job_folder(
    world,
    name='123',
    owner='alice',
    with_record=True,
    with_notices=True,
    **record_values,
):
    folder = world.mount / owner / name
    (folder / 'sub').mkdir(parents=True)
    (folder / 'a.dat').write_bytes(b'x' * 8192)
    (folder / 'sub' / 'b.dat').write_bytes(b'y' * 8192)
    if with_record:
        made = record(name, owner, **record_values)
        world.state.records.setdefault(name, []).append(made)
        put_event(world, made)
        if not with_notices:
            put_event(world, made, targets={})
    return folder


GONE_FOLDER = {
    'scratch|555|1': {
        'first_notice_at': (LATER - timedelta(days=30)).isoformat(),
        'final_notice_at': (LATER - timedelta(days=20)).isoformat(),
    }
}


def told_about(
    world,
    job_id,
    owner='alice',
    first=None,
    final=None,
    file_system='scratch',
    mount=None,
):
    """the notices of every folder of this job on this file system, as a digest records them"""
    first = first or (LATER - timedelta(days=30)).isoformat()
    final = final or (LATER - timedelta(days=20)).isoformat()
    owner_dir = (mount or world.mount) / owner
    stem = str(job_id)
    names = (
        [p.name for p in owner_dir.glob('*') if p.name.startswith(f'{stem[:-2]}[')]
        if stem.endswith('[]')
        else [stem]
    )
    return {
        '{}|{}|{}|{}'.format(
            file_system,
            name,
            os.stat(owner_dir / name).st_ino,
            os.stat(owner_dir / name).st_ctime_ns,
        ): {
            'first_notice_at': first,
            'final_notice_at': final,
        }
        for name in names
        if (owner_dir / name).exists()
    }


def re_tell(world):
    """a fixture that fills a folder after its record: the notices are about what it now holds"""
    for row in world.store.removals.values():
        row['targets'] = told_about(world, row['job_id'], row['owner'])


def put_event(world, made, **overrides):
    """what the finished-job path leaves on the record, past its date, its owner told twice"""
    world.store.put_removal(
        module.job_key(made),
        {
            'job_id': made.job_id,
            'job_uid': made.job_uid,
            'owner': made.owner,
            'end_time': (made.end_time or REAL_NOW).isoformat(),
            'targets': told_about(world, made.job_id, made.owner),
            'remove_at': (LATER - timedelta(days=1)).isoformat(),
            'source': 'event',
            **overrides,
        },
    )


def run(world, trigger='schedule'):
    fs = world.service.file_systems()[0]
    return world.service._execute(fs, trigger, 'tester')


def preview(world):
    return world.service.build_preview(world.service.file_systems()[0])


def reasons(result):
    return {row.path.rsplit('/', 1)[1]: row.reason for row in result.rows}


def logged_alert(world):
    return any(
        ALERT_MARKER in str(call) for call in world.service._logger.error.call_args_list
    )


def templates(world):
    return [notification.template_name for notification in world.sent]


def test_a_finished_jobs_folder_goes_and_is_logged(world):
    folder = job_folder(world)
    outcome = run(world)
    assert outcome['status'] == 'finished'
    assert outcome['removed_folders'] == 1
    assert not folder.exists()
    assert (world.mount / 'alice').exists(), 'the owner folder stays'
    rows = list(world.store.audit.values())
    assert [row['status'] for row in rows] == ['removed']
    assert rows[0]['path'] == str(folder)
    assert rows[0]['owner'] == 'alice' and rows[0]['job_id'] == '123'
    assert rows[0]['size_bytes'] > 0
    assert list(world.store.removed) == [f'alice#{folder}']


def test_the_audit_row_is_written_before_anything_is_removed(world, monkeypatch):
    folder = job_folder(world)
    seen = []
    real = world.store.put_audit

    def spy(row):
        seen.append((row['status'], folder.exists()))
        real(row)

    monkeypatch.setattr(world.store, 'put_audit', spy)
    run(world)
    assert seen == [('started', True), ('removed', False)]


def test_no_audit_row_means_no_removal(world, monkeypatch):
    folder = job_folder(world)
    monkeypatch.setattr(
        world.store, 'put_audit', Mock(side_effect=RuntimeError('table down'))
    )
    assert run(world)['status'] == 'failed'
    assert folder.exists()


@pytest.mark.parametrize(
    'switches',
    [
        {'enabled': False, 'dry_run': True, 'kill_switch': False},
        {'enabled': False, 'dry_run': False, 'kill_switch': False},
        {'enabled': True, 'dry_run': False, 'kill_switch': True},
    ],
)
def test_off_or_killed_removes_nothing_and_tells_nobody(world, switches):
    world.switches.update(switches)
    folder = job_folder(world)
    assert run(world)['status'] == 'stopped'
    assert folder.exists()
    assert world.sent == []
    assert not any(r.get('notified_at') for r in world.store.removals.values())


def test_missing_setting_rows_read_as_off_and_dry_run(world):
    table = world.context.aws().dynamodb_table().Table()
    table.get_item.side_effect = lambda **_: {}
    limits = world.service.limits()
    assert limits.enabled is False and limits.dry_run is True
    assert limits.kill_switch is False


def test_an_unreadable_switch_stops_the_run(world):
    folder = job_folder(world)
    world.switch_error = RuntimeError('throttled')
    assert run(world)['status'] == 'failed'
    assert folder.exists()


def test_every_kept_case_at_once(world, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('keep')
    world.state.active = ActiveJobs(
        base_ids=frozenset({'200', '201', '202', '203', '204'}),
        dependency_ids=frozenset({'205'}),
        referenced_folders=frozenset({('alice', '206')}),
    )
    names = {
        '200': Reason.JOB_ACTIVE,  # running
        '201': Reason.JOB_ACTIVE,  # queued
        '202': Reason.JOB_ACTIVE,  # held
        '203': Reason.JOB_ACTIVE,  # suspended
        '204': Reason.JOB_ACTIVE,  # array with live subjobs, which PBS lists under its number
        '205': Reason.REFERENCED_BY_ACTIVE_JOB,
        '206': Reason.REFERENCED_BY_ACTIVE_JOB,
    }
    folders = [job_folder(world, name) for name in names]
    folders.append(job_folder(world, '300', with_record=False))
    names['300'] = Reason.NO_FINISHED_RECORD
    folders.append(job_folder(world, 'apptainer-tmp', with_record=False))
    folders.append(job_folder(world, '301', finished=False))
    names['301'] = Reason.RECORD_NOT_FINISHED
    folders.append(job_folder(world, '302', start_time=None))
    names['302'] = Reason.RECORD_INCOMPLETE
    # the number belongs to a job that ran long after the data was written
    folders.append(
        job_folder(
            world,
            '303',
            start_time=REAL_NOW + timedelta(days=10),
            end_time=REAL_NOW + timedelta(days=11),
        )
    )
    names['303'] = Reason.OLDER_THAN_JOB
    # changed after the job ended
    folders.append(
        job_folder(
            world,
            '304',
            start_time=REAL_NOW - timedelta(days=5),
            end_time=REAL_NOW - timedelta(days=4),
        )
    )
    names['304'] = Reason.CHANGED_AFTER_JOB
    # a link named like a job, pointing out of the mount
    (world.mount / 'alice' / '306').symlink_to(outside)
    names['306'] = Reason.NOT_A_DIRECTORY
    world.state.records['306'] = [record('306')]
    (world.mount / 'alice' / '307').write_text('a file named like a job')
    names['307'] = Reason.NOT_A_DIRECTORY
    world.state.records['307'] = [record('307')]

    result = preview(world)
    assert reasons(result) == names
    assert all(row.action == 'keep' for row in result.rows)
    for name in ('306', '307'):
        put_event(world, record(name))
    outcome = run(world)
    assert outcome['removed_folders'] == 0
    assert all(folder.exists() for folder in folders)
    assert (world.mount / 'alice' / '306').is_symlink()
    assert (outside / 'precious').read_text() == 'keep'


def test_loose_files_and_odd_names_at_the_root_stay(world):
    (world.mount / 'stray.dat').write_text('x')
    (world.mount / 'has space').mkdir()
    (world.mount / '123').mkdir()
    world.state.records['123'] = [record('123')]
    result = preview(world)
    assert result.reason_counts[Reason.NOT_A_DIRECTORY.value] == 1
    assert result.reason_counts[Reason.NOT_A_JOB_FOLDER.value] == 1
    run(world)
    assert (world.mount / 'stray.dat').exists() and (world.mount / '123').exists()


def test_a_link_as_an_owner_folder_is_never_entered(world, tmp_path):
    outside = tmp_path / 'outside'
    (outside / '123').mkdir(parents=True)
    (outside / '123' / 'precious').write_text('keep')
    (world.mount / 'alice').symlink_to(outside)
    world.state.records['123'] = [record('123')]
    assert run(world)['removed_folders'] == 0
    assert (outside / '123' / 'precious').read_text() == 'keep'


def test_a_folder_owned_by_someone_else_stays(world):
    folder = job_folder(world)
    world.uids['alice'] = os.getuid() + 1
    assert reasons(preview(world))['123'] == Reason.OWNER_MISMATCH
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_an_owner_with_no_account_stays(world):
    folder = job_folder(world)
    world.context.accounts_client.get_user.side_effect = RuntimeError('no such user')
    assert reasons(preview(world))['123'] == Reason.OWNER_UNKNOWN
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_reused_number_owned_by_someone_else_stays(world):
    folder = job_folder(world, with_record=False)
    world.state.records['123'] = [record('123', owner='bob')]
    assert reasons(preview(world))['123'] == Reason.NO_FINISHED_RECORD
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_records_unavailable_keeps_everything_and_stops(world):
    folder = job_folder(world)
    world.state.records_error = StateUnavailable('index down')
    assert reasons(preview(world))['123'] == Reason.STATE_UNAVAILABLE
    outcome = run(world)
    assert outcome['status'] == 'stopped'
    assert folder.exists()
    assert logged_alert(world)


def test_active_jobs_unavailable_stops_and_alerts(world):
    folder = job_folder(world)
    world.state.active_error = StateUnavailable('qselect failed')
    outcome = run(world)
    assert outcome['status'] == 'stopped' and 'qselect' in outcome['message']
    assert folder.exists()
    assert logged_alert(world)
    assert world.service._file_system('scratch')
    assert preview(world).problem


def test_a_mount_that_is_not_lustre_stops_the_run(world):
    folder = job_folder(world)
    world.mountinfo.write_text(f'40 1 0:40 / {world.mount} rw - xfs /dev/x rw\n')
    outcome = run(world)
    assert outcome['status'] == 'stopped' and 'not Lustre' in outcome['message']
    assert folder.exists()
    assert world.state.active_calls == 0


def test_another_run_holding_the_lease_stops_this_one(world):
    folder = job_folder(world)
    world.store.lease_free = False
    assert run(world)['status'] == 'stopped'
    assert folder.exists()


def test_a_lost_lease_stops_the_next_removal(world, monkeypatch):
    first = job_folder(world, '123')
    second = job_folder(world, '124')
    monkeypatch.setattr(module, 'LEASE_RENEW_SECONDS', 0)
    world.store.renew_error = LeaseLost('taken')
    outcome = run(world)
    assert outcome['status'] == 'stopped'
    assert first.exists() and second.exists()


def test_a_kill_switch_stops_the_next_removal(world):
    first = job_folder(world, '123', end_time=REAL_NOW + timedelta(minutes=1))
    second = job_folder(world, '124', end_time=REAL_NOW + timedelta(minutes=2))

    def flip(calls):
        if calls == 3:
            world.switches['kill_switch'] = True

    world.state.on_active = flip
    outcome = run(world)
    assert outcome['status'] == 'stopped'
    assert outcome['removed_folders'] == 1
    assert not first.exists() and second.exists()


def test_a_kill_switch_stops_a_removal_in_the_middle(world, monkeypatch):
    folder = job_folder(world)
    for index in range(1500):
        (folder / f'f{index}').touch()
    re_tell(world)
    real = module.remove_folder

    def flipping(*args, **kwargs):
        world.switches['kill_switch'] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(module, 'remove_folder', flipping)
    outcome = run(world)
    assert outcome['status'] == 'stopped' and outcome['removed_folders'] == 0
    assert folder.exists()
    assert [r['status'] for r in world.store.audit.values()] == ['started']


def test_a_stopped_removal_resumes_after_every_check_passes(world, monkeypatch):
    folder = job_folder(world)
    for index in range(1500):
        (folder / f'f{index}').touch()
    re_tell(world)
    real = module.remove_folder

    def flipping(*args, **kwargs):
        world.switches['kill_switch'] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(module, 'remove_folder', flipping)
    run(world)
    monkeypatch.setattr(module, 'remove_folder', real)
    world.switches['kill_switch'] = False
    # the clock moved and the folder changed because of the removal itself
    outcome = run(world)
    assert outcome['status'] == 'finished' and outcome['removed_folders'] == 1
    assert not folder.exists()


def test_a_resumed_removal_still_stops_for_an_active_job(world, monkeypatch):
    folder = job_folder(world)
    info = os.lstat(folder)
    world.store.put_audit(
        {
            'path': str(folder),
            'started_at': 'earlier',
            'status': 'started',
            'device': info.st_dev,
            'inode': info.st_ino,
        }
    )
    world.state.active = ActiveJobs(base_ids=frozenset({'123'}))
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_job_that_starts_after_the_plan_keeps_its_folder(world):
    folder = job_folder(world)

    def start_job(calls):
        if calls == 2:
            world.state.active = ActiveJobs(base_ids=frozenset({'123'}))

    world.state.on_active = start_job
    outcome = run(world)
    assert outcome['status'] == 'finished'
    assert outcome['removed_folders'] == 0 and outcome['skipped_folders'] == 1
    assert folder.exists()


def test_a_record_that_changes_after_the_plan_keeps_the_folder(world):
    folder = job_folder(world)

    def requeue(calls):
        if calls == 2:
            world.state.records['123'] = [record('123', finished=False)]

    world.state.on_active = requeue
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_write_after_the_plan_keeps_the_folder(world, monkeypatch):
    folder = job_folder(world)
    real = module.inspect_folder
    calls = []

    def later(*args, **kwargs):
        calls.append(1)
        found = real(*args, **kwargs)
        if len(calls) == 1:
            return found
        return replace(found, newest_change=REAL_NOW + timedelta(days=10))

    monkeypatch.setattr(module, 'inspect_folder', later)
    outcome = run(world)
    assert len(calls) == 2
    assert outcome['removed_folders'] == 0
    assert folder.exists()


def test_a_folder_swapped_for_a_link_after_the_plan_is_left_alone(world, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('keep')
    folder = job_folder(world)

    def swap(calls):
        if calls == 2:
            os.rename(folder, world.mount / 'alice' / 'moved')
            folder.symlink_to(outside)

    world.state.on_active = swap
    outcome = run(world)
    assert outcome['removed_folders'] == 0
    assert folder.is_symlink()
    assert (outside / 'precious').read_text() == 'keep'
    assert (world.mount / 'alice' / 'moved' / 'a.dat').exists()


def test_a_folder_replaced_by_a_new_one_after_the_plan_is_left_alone(world):
    folder = job_folder(world)

    def replace(calls):
        if calls == 2:
            os.rename(folder, world.mount / 'alice' / 'moved')
            folder.mkdir()
            (folder / 'new').write_text('new data')

    world.state.on_active = replace
    assert run(world)['removed_folders'] == 0
    assert (folder / 'new').exists()


def test_the_owner_folder_swapped_after_the_plan_is_not_followed(world, tmp_path):
    outside = tmp_path / 'outside'
    (outside / '123').mkdir(parents=True)
    (outside / '123' / 'precious').write_text('keep')
    job_folder(world)

    def swap(calls):
        if calls == 2:
            os.rename(world.mount / 'alice', world.mount / 'moved')
            (world.mount / 'alice').symlink_to(outside)

    world.state.on_active = swap
    assert run(world)['removed_folders'] == 0
    assert (outside / '123' / 'precious').read_text() == 'keep'


def test_a_removal_failure_stops_the_run_and_alerts(world, monkeypatch):
    first = job_folder(world, '123')
    second = job_folder(world, '124')
    monkeypatch.setattr(
        module, 'remove_folder', Mock(side_effect=OSError('input/output error'))
    )
    outcome = run(world)
    assert outcome['status'] == 'stopped'
    assert first.exists() and second.exists()
    assert logged_alert(world)
    rows = list(world.store.audit.values())
    assert [row['status'] for row in rows] == ['started'], 'an I/O error can be retried'
    assert rows[0]['message'] == 'input/output error'


def _interrupt(world, monkeypatch, error, entries=7):
    def partial(*args, **kwargs):
        kwargs['progress'].entries = entries
        raise error

    monkeypatch.setattr(module, 'remove_folder', partial)


def test_who_started_a_removal_is_in_the_log(world):
    job_folder(world)
    run(world)
    rows, _ = world.service.list_audit(50, None)
    assert rows[0]['requested_by'] == 'tester'
    assert rows[0]['started_at']
    assert rows[0]['removed_entries'] > 0


def test_oldest_finished_goes_first_not_lowest_number(world):
    newer = job_folder(world, '100', end_time=REAL_NOW + timedelta(minutes=5))
    older = job_folder(world, '900', end_time=REAL_NOW + timedelta(minutes=1))
    world.config['scheduler.scratch_cleanup.max_folders_per_run'] = 1
    outcome = run(world)
    assert outcome['removed_folders'] == 1
    assert not older.exists() and newer.exists()


def test_the_lowest_number_breaks_a_tie_in_age(world):
    end = REAL_NOW + timedelta(minutes=1)
    high = job_folder(world, '90', end_time=end)
    low = job_folder(world, '9', end_time=end)
    world.config['scheduler.scratch_cleanup.max_folders_per_run'] = 1
    run(world)
    assert not low.exists() and high.exists()


def test_the_cap_per_run_holds_and_the_rest_wait(world):
    world.config['scheduler.scratch_cleanup.max_folders_per_run'] = 2
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    folders = [
        job_folder(world, str(index), end_time=REAL_NOW + timedelta(minutes=index))
        for index in range(1, 6)
    ]
    assert run(world)['removed_folders'] == 2
    assert sum(folder.exists() for folder in folders) == 3


def test_a_run_removing_an_implausible_share_of_capacity_stops(world, monkeypatch):
    folder = job_folder(world)
    real = module.inspect_folder

    def huge(*args, **kwargs):
        found = real(*args, **kwargs)
        return replace(found, size_bytes=300 * GiB)

    monkeypatch.setattr(module, 'inspect_folder', huge)
    outcome = run(world)
    assert outcome['status'] == 'stopped' and 'of capacity' in outcome['message']
    assert folder.exists()


def test_bad_settings_stop_the_run(world):
    world.config['scheduler.scratch_cleanup.success_retention_days'] = 1
    folder = job_folder(world)
    assert run(world)['status'] == 'stopped'
    assert folder.exists()


def test_the_job_is_marked_for_the_job_pages(world, monkeypatch):
    job_folder(world)
    job = SimpleNamespace(job_id='123', scratch_removed_at=None)
    monkeypatch.setattr(world.state, 'find_job', lambda _: job)
    run(world)
    assert job.scratch_removed_at == LATER
    world.context.job_cache.add_finished_job.assert_called_once_with(job)


def test_a_failure_to_mark_the_job_does_not_fail_the_run(world, monkeypatch):
    job_folder(world)
    monkeypatch.setattr(world.state, 'find_job', Mock(side_effect=RuntimeError('x')))
    assert run(world)['status'] == 'finished'


def test_a_leftover_removal_does_not_excuse_new_data(world):
    # a run stopped right after its audit row, then the owner wrote new files
    folder = job_folder(
        world,
        start_time=REAL_NOW - timedelta(days=9),
        end_time=REAL_NOW - timedelta(days=8),
    )
    info = os.lstat(folder)
    world.store.put_audit(
        {
            'path': str(folder),
            'job_id': '123',
            'started_at': 'earlier',
            'status': 'started',
            'device': info.st_dev,
            'inode': info.st_ino,
        }
    )
    assert run(world)['removed_folders'] == 0
    assert (folder / 'a.dat').exists()


def test_a_leftover_removal_for_another_job_is_not_a_resume(world):
    folder = job_folder(world, end_time=REAL_NOW + timedelta(minutes=1))
    (folder / 'a.dat').unlink()
    info = os.lstat(folder)
    row = {
        'path': str(folder),
        'job_id': '999',
        'started_at': 'earlier',
        'status': 'started',
        'device': info.st_dev,
        'inode': info.st_ino,
    }
    assert not module._resume_until(
        SimpleNamespace(
            device=info.st_dev,
            inode=info.st_ino,
            path=str(folder),
            shape=SimpleNamespace(base_id='123'),
        ),
        {(info.st_dev, info.st_ino): row},
    )


@pytest.mark.parametrize(
    'leaf, value, expected',
    [
        ('kill_switch', 'on', True),
        ('kill_switch', 1, True),
        ('dry_run', 'off', True),
        ('enabled', 'yes', False),
    ],
)
def test_a_switch_holding_nonsense_reads_as_the_safe_side(world, leaf, value, expected):
    world.switches[leaf] = value
    assert getattr(world.service.limits(), leaf) is expected


def test_the_mount_root_and_owner_folders_are_never_candidates(world):
    # empty owner folder and the root itself survive however much is removed
    job_folder(world)
    (world.mount / 'bob').mkdir()
    run(world)
    assert world.mount.is_dir()
    assert (world.mount / 'alice').is_dir() and (world.mount / 'bob').is_dir()


# file system discovery


def test_a_cluster_without_lustre_shows_nothing(world):
    world.config['shared-storage'] = {
        'apps': {'provider': 'efs', 'mount_dir': '/apps'},
        'data': {'provider': 'fsx_netapp_ontap', 'mount_dir': '/data'},
    }
    assert world.service.file_systems() == []
    assert world.service.status()['available'] is False
    world.service.start()
    assert world.service._thread is None, 'nothing runs either'


def test_a_cluster_with_no_storage_config_shows_nothing(world):
    del world.config['shared-storage']
    assert world.service.file_systems() == []


@pytest.mark.parametrize('name', ['apps', 'data'])
def test_lustre_holding_persistent_data_is_never_scratch(world, name):
    world.config['shared-storage'] = {
        name: {'provider': 'fsx_lustre', 'mount_dir': f'/{name}'}
    }
    assert world.service.file_systems() == []


@pytest.mark.parametrize('home', ['/projects', '/projects/home/users'])
def test_lustre_holding_home_directories_is_never_scratch(world, home):
    world.config['cluster.home_dir'] = home
    world.config['shared-storage'] = {
        'projects': {'provider': 'fsx_lustre', 'mount_dir': '/projects'}
    }
    assert world.service.file_systems() == []


def test_every_lustre_attachment_is_found_and_only_selected_ones_run(world):
    world.config['shared-storage']['team'] = {
        'provider': 'fsx_lustre',
        'mount_dir': '/team',
    }
    names = [fs.name for fs in world.service.file_systems()]
    assert names == ['scratch', 'team']
    status = world.service.status()
    assert {fs['name']: fs['selected'] for fs in status['file_systems']} == {
        'scratch': True,
        'team': False,
    }


# admin runs


def wait_for_run(world):
    deadline = time.time() + 20
    while time.time() < deadline:
        if not world.service._busy.locked():
            return
        time.sleep(0.01)
    raise AssertionError('the run did not finish')


def test_progress_is_real_during_a_scan(world):
    job_folder(world)
    world.service._begin_progress('preview', 'scratch')
    seen = []
    real = world.state.finished_records

    def spy(forms):
        seen.append(dict(world.service._progress))
        return real(forms)

    world.state.finished_records = spy
    world.service.build_preview(world.service.file_systems()[0])
    assert seen[0]['phase'] == 'Reading folders'
    assert world.service.status()['progress']['total'] == 1


def test_a_long_scan_keeps_the_lease(world, monkeypatch):
    job_folder(world)
    monkeypatch.setattr(module, 'LEASE_RENEW_SECONDS', 0)
    renewals = []
    monkeypatch.setattr(world.store, 'renew_lease', renewals.append)
    run(world)
    assert renewals


def test_a_preview_never_touches_the_lease(world, monkeypatch):
    job_folder(world)
    monkeypatch.setattr(module, 'LEASE_RENEW_SECONDS', 0)
    world.store.renew_error = LeaseLost('not held')
    world.store.lease_free = False
    assert preview(world).problem is None


def test_a_run_left_running_by_a_replaced_task_is_closed(world):
    world.store.put_run(
        {
            'run_id': 'old',
            'status': 'running',
            'started_at': '2026-01-01T00:00:00+00:00',
        }
    )
    world.service._settle_earlier_runs()
    assert world.store.runs['old']['status'] == 'interrupted'
    assert world.store.lease_holder is None


def test_real_metrics_accept_the_calls_made():
    context = Mock()
    context.cluster_name.return_value = 'idea-test'
    context.config.return_value = Config({'metrics.provider': None})
    metrics = module.ScratchCleanupMetrics(context, 'scratch')
    metrics.level('used_percent', 91.5)
    metrics.add('removed_folders', 2)


def _source_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def test_a_new_cluster_ships_with_cleanup_off_and_in_dry_run():
    import yaml

    template = (
        _source_root() / 'ideactl/resources/config/templates/scheduler/settings.yml'
    ).read_text()
    start = template.index('scratch_cleanup:')
    end = template.index('job_provisioning:', start)
    settings = yaml.safe_load(template[start:end])['scratch_cleanup']
    assert settings['enabled'] is False
    assert settings['dry_run'] is True
    assert settings['kill_switch'] is False
    assert settings['file_systems'] == []


def test_an_upgraded_cluster_with_no_rows_stays_off(world):
    table = world.context.aws().dynamodb_table().Table()
    table.get_item.side_effect = lambda **_: {}
    world.config = {
        key: value
        for key, value in world.config.items()
        if not key.startswith('scheduler.scratch_cleanup')
    }
    world.context.config.return_value = Config(world.config)
    folder = job_folder(world)
    assert world.service.limits().file_systems == ()
    world.service._run_if_due()
    assert folder.exists() and not world.store.runs


def test_every_email_template_used_exists():
    import yaml

    templates = yaml.safe_load(
        (
            _source_root()
            / 'idea-cluster-manager/resources/defaults/email_templates.yml'
        ).read_text()
    )['templates']
    names = {template['name'] for template in templates}
    assert {
        TEMPLATE_DIGEST,
        TEMPLATE_ADMIN_SUMMARY,
    } <= names


def test_deliberately_no_notice_needs_no_delivery_path(world):
    world.config['cluster.ses.enabled'] = False
    folder = job_folder(world)
    assert run(world)['removed_folders'] == 1
    assert not folder.exists()


def test_removal_is_told_how_late_anything_inside_may_be(world, monkeypatch):
    job_folder(world)
    real = module.remove_folder
    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, 'remove_folder', spy)
    run(world)
    end = record('123').end_time
    assert seen['not_after'] == pytest.approx(
        (end + timedelta(minutes=15)).timestamp(), abs=5
    )
    assert seen['dirs_not_after'] == seen['not_after']
    assert seen['not_before'] < seen['not_after']


@pytest.mark.parametrize(
    'became',
    [
        ActiveJobs(base_ids=frozenset({'123'})),
        ActiveJobs(dependency_ids=frozenset({'123'})),
        ActiveJobs(referenced_folders=frozenset({('alice', '123')})),
    ],
    ids=['submitted', 'dependency', 'names the folder'],
)
def test_a_job_appearing_during_removal_stops_it(world, monkeypatch, became):
    monkeypatch.setattr(module, 'STATE_RECHECK_SECONDS', 0)
    folder = job_folder(world)
    for index in range(1500):
        (folder / f'f{index}').touch()
    re_tell(world)

    def appear(calls):
        if calls == 3:
            world.state.active = became

    world.state.on_active = appear
    outcome = run(world)
    assert outcome['status'] == 'stopped'
    assert outcome['removed_folders'] == 0
    assert folder.exists() and os.listdir(folder)


def test_state_unavailable_during_removal_stops_it(world, monkeypatch):
    monkeypatch.setattr(module, 'STATE_RECHECK_SECONDS', 0)
    folder = job_folder(world)
    for index in range(1500):
        (folder / f'f{index}').touch()
    re_tell(world)

    def fail(calls):
        if calls == 3:
            world.state.active_error = StateUnavailable('qselect failed')

    world.state.on_active = fail
    assert run(world)['status'] == 'stopped'
    assert folder.exists()


def test_a_tree_moved_in_during_an_interruption_is_not_excused(world):
    folder = job_folder(world)
    info = os.lstat(folder)
    world.store.put_audit(
        {
            'path': str(folder),
            'job_id': '123',
            'started_at': (REAL_NOW - timedelta(days=9)).isoformat(),
            'last_removal_at': (REAL_NOW - timedelta(days=9)).isoformat(),
            'status': 'started',
            'device': info.st_dev,
            'inode': info.st_ino,
        }
    )
    record_now = world.state.records['123'][0]
    world.state.records['123'] = [
        replace(
            record_now,
            start_time=REAL_NOW - timedelta(days=10),
            end_time=REAL_NOW - timedelta(days=9, hours=1),
        )
    ]
    # the files here date from the job only because the file limit is checked separately
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_the_audit_row_tracks_the_last_removal(world, monkeypatch):
    monkeypatch.setattr(module, 'STATE_RECHECK_SECONDS', 0)
    folder = job_folder(world)
    for index in range(1500):
        (folder / f'f{index}').touch()
    re_tell(world)
    seen = []
    real = world.store.put_audit

    def spy(row):
        seen.append(row.get('last_removal_at'))
        real(row)

    monkeypatch.setattr(world.store, 'put_audit', spy)
    run(world)
    assert seen[0] is not None
    assert len(seen) > 2, 'written while the removal runs, not only at its ends'


def test_a_resume_limits_directories_to_the_last_removal(world, monkeypatch):
    folder = job_folder(world)
    info = os.lstat(folder)
    last = REAL_NOW + timedelta(minutes=1)
    world.store.put_audit(
        {
            'path': str(folder),
            'job_id': '123',
            'started_at': last.isoformat(),
            'last_removal_at': last.isoformat(),
            'status': 'started',
            'device': info.st_dev,
            'inode': info.st_ino,
        }
    )
    real = module.remove_folder
    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, 'remove_folder', spy)
    run(world)
    assert seen['dirs_not_after'] == pytest.approx(
        (last + timedelta(minutes=15)).timestamp(), abs=1
    )


def test_a_preview_lists_only_job_folders_and_counts_the_rest(world):
    job_folder(world, '123')
    (world.mount / 'alice' / 'merger-model-v2').mkdir()
    (world.mount / 'alice' / 'notes.txt').write_text('x')
    (world.mount / 'secret project').mkdir()
    result = preview(world)
    listed = ' '.join(row.path for row in result.rows)
    assert 'merger' not in listed and 'notes' not in listed and 'secret' not in listed
    summary = module._preview_summary(result)
    assert summary['other_kept'] == 3
    assert summary['action_counts']['keep'] == 3
    assert 'merger' not in str(summary)


def test_starting_a_preview_is_recorded_with_who_started_it(world):
    job_folder(world)
    world.service.start_preview('scratch', 'some-admin')
    wait_for_run(world)
    recorded = [run for run in world.store.runs.values() if run['trigger'] == 'preview']
    assert [run['requested_by'] for run in recorded] == ['some-admin']
    assert recorded[0]['status'] == 'preview'
    # a preview is not a scheduled run and does not delay one
    world.service._run_if_due()
    assert any(run['trigger'] == 'schedule' for run in world.store.runs.values())


def test_the_switches_are_not_read_on_every_check(world, monkeypatch):
    table = world.context.aws().dynamodb_table().Table()
    row = SimpleNamespace(
        shape=SimpleNamespace(base_id='1'), parent_name='a', name='1', path='/x'
    )
    table.get_item.reset_mock()
    world.service._last_gate = 0.0
    world.service._last_state_check = time.monotonic()
    world.service._last_lease_renewal = time.monotonic()
    world.service._keep_going(row, [])
    reads = table.get_item.call_count
    assert reads > 0
    world.service._keep_going(row, [])
    assert table.get_item.call_count == reads
    monkeypatch.setattr(module, 'GATE_SECONDS', 0)
    world.service._keep_going(row, [])
    assert table.get_item.call_count == reads * 2


def _no_removal(world, folder, expected):
    outcome = run(world)
    assert outcome['status'] == 'stopped'
    assert expected in outcome['message']
    assert folder.exists()
    assert not world.store.audit


def test_a_data_repository_association_stops_everything(world):
    folder = job_folder(world)
    world.fsx.describe_data_repository_associations.return_value = {
        'Associations': [{'AssociationId': 'dra-1', 'Lifecycle': 'AVAILABLE'}]
    }
    _no_removal(world, folder, 'data repository')
    assert 'data repository' in preview(world).problem


def test_a_linked_s3_bucket_stops_everything(world):
    folder = job_folder(world)
    world.fsx.describe_file_systems.return_value = {
        'FileSystems': [
            {
                'LustreConfiguration': {
                    'DataRepositoryConfiguration': {'ImportPath': 's3://bucket'}
                }
            }
        ]
    }
    _no_removal(world, folder, 'data repository')


def test_not_knowing_about_data_repositories_stops_everything(world):
    folder = job_folder(world)
    world.fsx.describe_data_repository_associations.side_effect = RuntimeError('denied')
    _no_removal(world, folder, 'could not be checked')


def test_no_file_system_id_stops_everything(world):
    folder = job_folder(world)
    del world.config['shared-storage']['scratch']['fsx_lustre']['file_system_id']
    _no_removal(world, folder, 'could not be checked')


def test_an_association_added_during_a_run_stops_the_next_removal(world):
    first = job_folder(world, '123', end_time=REAL_NOW + timedelta(minutes=1))
    second = job_folder(world, '124', end_time=REAL_NOW + timedelta(minutes=2))
    real = world.store.put_audit

    def add_association(row):
        real(row)
        if row['status'] == 'removed':
            world.fsx.describe_data_repository_associations.return_value = {
                'Associations': [{'AssociationId': 'dra-1'}]
            }

    world.store.put_audit = add_association
    outcome = run(world)
    assert outcome['status'] == 'stopped'
    assert not first.exists() and second.exists()


def test_a_running_job_restarting_from_a_finished_jobs_folder_keeps_it(world):
    # the fixture the owner asked for: job 900 (running, other owner) reads job 123's folder
    folder = job_folder(world, '123')
    world.state.active = ActiveJobs(
        base_ids=frozenset({'900'}), mentioned_ids=frozenset({'123'})
    )
    assert reasons(preview(world))['123'] == Reason.REFERENCED_BY_ACTIVE_JOB
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_job_mentioning_the_folder_during_removal_stops_it(world, monkeypatch):
    monkeypatch.setattr(module, 'STATE_RECHECK_SECONDS', 0)
    folder = job_folder(world)
    for index in range(1500):
        (folder / f'f{index}').touch()
    re_tell(world)

    def appear(calls):
        if calls == 3:
            world.state.active = ActiveJobs(mentioned_ids=frozenset({'123'}))

    world.state.on_active = appear
    assert run(world)['status'] == 'stopped'
    assert folder.exists() and os.listdir(folder)


def test_live_state_is_read_on_a_short_timer():
    assert module.STATE_RECHECK_SECONDS <= 15


def test_an_owner_with_any_active_job_keeps_every_folder(world):
    # the job restarting from 123 names it nowhere PBS can see; the rule closes that anyway
    mine = job_folder(world, '123')
    theirs = job_folder(world, '124', owner='bob')
    world.state.active = ActiveJobs(
        base_ids=frozenset({'900'}), owners=frozenset({'alice'})
    )
    result = reasons(preview(world))
    assert result['123'] == Reason.OWNER_HAS_ACTIVE_JOB
    assert result['124'] == Reason.ELIGIBLE
    outcome = run(world)
    assert outcome['removed_folders'] == 1
    assert mine.exists() and not theirs.exists()


def test_an_owner_starting_a_job_after_the_plan_keeps_the_folder(world):
    folder = job_folder(world)

    def start(calls):
        if calls == 2:
            world.state.active = ActiveJobs(owners=frozenset({'alice'}))

    world.state.on_active = start
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_an_owner_starting_a_job_during_removal_stops_it(world, monkeypatch):
    monkeypatch.setattr(module, 'STATE_RECHECK_SECONDS', 0)
    folder = job_folder(world)
    for index in range(1500):
        (folder / f'f{index}').touch()
    re_tell(world)

    def start(calls):
        if calls == 3:
            world.state.active = ActiveJobs(owners=frozenset({'alice'}))

    world.state.on_active = start
    assert run(world)['status'] == 'stopped'
    assert folder.exists() and os.listdir(folder)


def test_no_setting_can_lift_the_owner_rule(world):
    world.switches['keep_while_owner_active'] = False
    world.config['scheduler.scratch_cleanup.keep_while_owner_active'] = False
    folder = job_folder(world)
    world.state.active = ActiveJobs(owners=frozenset({'alice'}))
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_no_preview_can_lift_the_owner_rule(world):
    job_folder(world)
    world.state.active = ActiveJobs(owners=frozenset({'alice'}))
    result = world.service.build_preview(world.service.file_systems()[0])
    assert reasons(result)['123'] == Reason.OWNER_HAS_ACTIVE_JOB
    with pytest.raises(TypeError):
        world.service.start_preview('scratch', 'root-admin', include_active_owners=True)


# the record: what the finished-job path leaves behind


def finished_job(job_id='123', owner='alice', uid=None, **overrides):
    values = dict(
        job_id=job_id,
        job_uid=uid or f'uid-{job_id}',
        owner=owner,
        state=SocaJobState.FINISHED,
        exit_status=0,
        disposition='ran',
        start_time=REAL_NOW - timedelta(hours=1),
        end_time=REAL_NOW + timedelta(minutes=1),
    )
    values.update(overrides)
    return SocaJob(**values)


def test_a_duplicate_finish_event_changes_nothing(world):
    job = finished_job()
    world.service.record_job_finished(job)
    world.store.removals['uid-123:123']['notified_at'] = 'kept'
    world.service.record_job_finished(job)
    assert world.store.removals['uid-123:123']['notified_at'] == 'kept'


def test_a_job_that_finishes_again_takes_the_new_date(world):
    world.service.record_job_finished(finished_job())
    later = finished_job(end_time=REAL_NOW + timedelta(days=5))
    world.service.record_job_finished(later)
    assert (
        world.store.removals['uid-123:123']['remove_at']
        == (later.end_time + timedelta(days=30)).isoformat()
    )


def test_a_requeued_job_cancels_its_removal(world):
    folder = job_folder(world)
    made = world.state.records['123'][0]
    world.context.job_cache.get_job.side_effect = lambda job_id: SimpleNamespace(
        job_id=job_id, job_uid=made.job_uid
    )
    assert run(world)['removed_folders'] == 0
    assert folder.exists() and not world.store.removals


def test_another_job_under_the_same_number_does_not_cancel_it(world):
    job_folder(world)
    world.context.job_cache.get_job.side_effect = lambda job_id: SimpleNamespace(
        job_id=job_id, job_uid='someone-new'
    )
    world.state.active = ActiveJobs(base_ids=frozenset({'123'}))
    run(world)
    assert world.store.removals, 'kept, and asked about again'


def test_no_lustre_on_the_cluster_records_nothing(world):
    world.config['shared-storage'] = {}
    world.service.record_job_finished(finished_job())
    assert not world.store.removals


def test_the_finished_job_path_does_not_touch_the_file_system(world, monkeypatch):
    monkeypatch.setattr(
        module, '_open_root', Mock(side_effect=AssertionError('walked'))
    )
    monkeypatch.setattr(module, 'usage', Mock(side_effect=AssertionError('statvfs')))
    world.service.record_job_finished(finished_job())
    assert world.store.removals


def test_finishing_a_job_survives_the_record_failing():
    from ideascheduler.app.provisioning.job_monitor.finished_job_processor import (
        ProcessFinishedJob,
    )

    context = Mock()
    context.scratch_cleanup.record_job_finished.side_effect = RuntimeError('table down')
    processor = ProcessFinishedJob(
        context=context,
        logger=Mock(),
        job=SocaJob(job_id='1'),
        job_export_logger=Mock(),
    )
    processor.record_scratch_outcome()
    assert processor._logger.error.called


def restored_processor(context):
    from ideascheduler.app.provisioning.job_monitor.finished_job_processor import (
        FinishedJobProcessor,
    )

    processor = FinishedJobProcessor.__new__(FinishedJobProcessor)
    processor._context = context
    processor._logger = Mock()
    return processor


def test_a_job_restored_from_the_index_gets_its_record():
    # it finished in the hour a lost task's copy did not hold
    job = SocaJob(job_id='1')
    context = Mock()
    context.job_cache.get_completed_job_by_uid.return_value = None
    context.document_store.get_job.return_value = job
    assert restored_processor(context)._recorded_job('uid-1') is job
    context.scratch_cleanup.record_job_finished.assert_called_once_with(job)


def test_a_job_already_recorded_is_not_recorded_again():
    context = Mock()
    context.job_cache.get_completed_job_by_uid.return_value = SocaJob(job_id='1')
    restored_processor(context)._recorded_job('uid-1')
    assert not context.scratch_cleanup.record_job_finished.called


def test_a_failed_record_does_not_cost_a_restored_job():
    job = SocaJob(job_id='1')
    context = Mock()
    context.job_cache.get_completed_job_by_uid.return_value = None
    context.document_store.get_job.return_value = job
    context.scratch_cleanup.record_job_finished.side_effect = RuntimeError('table down')
    assert restored_processor(context)._recorded_job('uid-1') is job


def test_a_job_finishing_reaches_the_record_through_the_finished_path():
    from ideascheduler.app.provisioning.job_monitor import (
        finished_job_processor as path,
    )

    source = open(path.__file__).read()
    assert source.index('self.publish_to_finished_jobs_db()') < source.index(
        'self.record_scratch_outcome()'
    )


def test_only_the_record_supplies_candidates(world, monkeypatch):
    folder = job_folder(world, '123', with_record=False)
    world.state.records['123'] = [record('123')]
    monkeypatch.setattr(
        world.service, '_scan', Mock(side_effect=AssertionError('walked the mount'))
    )
    assert run(world)['removed_folders'] == 0
    assert folder.exists(), 'a finished job with no record is not a candidate'


def test_a_date_not_reached_removes_nothing(world):
    folder = job_folder(world)
    made = world.state.records['123'][0]
    put_event(world, made, remove_at=(LATER + timedelta(days=1)).isoformat())
    assert run(world)['removed_folders'] == 0
    assert folder.exists() and world.store.removals


def test_a_record_whose_folder_is_gone_ends_quietly(world):
    put_event(world, record('555'), targets=GONE_FOLDER)
    outcome = run(world)
    assert outcome['removed_folders'] == 0 and not world.store.removals
    assert world.sent == []


def test_the_record_ends_when_its_folder_goes(world):
    job_folder(world)
    run(world)
    assert not world.store.removals


def test_a_kept_folder_stays_on_the_record_with_its_reason_and_waits(world):
    folder = job_folder(world)
    world.state.active = ActiveJobs(owners=frozenset({'alice'}))
    run(world)
    row = world.store.removals[module.job_key(world.state.records['123'][0])]
    assert row['last_reason'] == Reason.OWNER_HAS_ACTIVE_JOB.value
    world.state.active = ActiveJobs()
    assert run(world)['removed_folders'] == 0, 'not asked again until tomorrow'
    world.now = LATER + timedelta(days=2)
    assert run(world)['removed_folders'] == 1
    assert not folder.exists()


def test_a_folder_changed_since_the_job_ended_is_kept_on_its_date(world):
    folder = job_folder(
        world,
        start_time=REAL_NOW - timedelta(days=5),
        end_time=REAL_NOW - timedelta(days=4),
    )
    assert run(world)['removed_folders'] == 0
    assert folder.exists()
    row = next(iter(world.store.removals.values()))
    assert row['last_reason'] == Reason.CHANGED_AFTER_JOB.value


# armed or not


def test_a_dry_run_reports_due_records_and_touches_nothing(world):
    world.switches['dry_run'] = True
    folder = job_folder(world)
    made = world.state.records['123'][0]
    put_event(world, made)
    outcome = run(world)
    assert outcome['status'] == 'dry_run' and outcome['would_remove_folders'] == 1
    assert folder.exists()
    assert world.sent == [] and not world.store.audit


# notice


# backfill


def backlog(world, name='123', **values):
    folder = job_folder(world, name, with_record=False)
    made = record(name, **values)
    world.state.records[name] = [made]
    return folder


def test_a_backfill_preview_changes_nothing(world):
    folder = backlog(world)
    result = preview(world)
    assert [row.action for row in result.rows] == ['schedule']
    assert result.confirm_phrase == 'schedule 1'
    assert folder.exists() and world.sent == []
    assert not world.store.removals and not world.store.audit


def test_a_backfill_does_not_list_what_is_already_on_the_record(world):
    job_folder(world)
    result = preview(world)
    assert [row.action for row in result.rows] == ['scheduled']
    assert result.confirm_phrase is None


def test_a_backfill_needs_the_exact_phrase(world):
    backlog(world)
    result = preview(world)
    for phrase in ('schedule 2', None, ''):
        with pytest.raises(exceptions.SocaException):
            world.service.start_backfill(result.preview_id, phrase, 'admin')
    with pytest.raises(exceptions.SocaException):
        world.service.start_backfill('unknown', 'schedule 1', 'admin')
    assert not world.store.removals


@pytest.mark.parametrize(
    'switches',
    [{'dry_run': True}, {'enabled': False}, {'kill_switch': True}],
    ids=['dry run', 'off', 'kill switch'],
)
def test_a_backfill_is_refused_unless_cleanup_is_on(world, switches):
    backlog(world)
    result = preview(world)
    world.switches.update(switches)
    with pytest.raises(exceptions.SocaException):
        world.service.start_backfill(result.preview_id, 'schedule 1', 'admin')
    assert not world.store.removals


def test_a_backfill_is_refused_for_a_file_system_not_selected(world):
    backlog(world)
    result = preview(world)
    world.config['scheduler.scratch_cleanup.file_systems'] = []
    with pytest.raises(exceptions.SocaException):
        world.service.start_backfill(result.preview_id, 'schedule 1', 'admin')


def test_an_old_preview_cannot_be_backfilled(world):
    backlog(world)
    result = preview(world)
    world.now = LATER + timedelta(hours=2)
    with pytest.raises(exceptions.SocaException):
        world.service.start_backfill(result.preview_id, 'schedule 1', 'admin')


def test_a_backfill_preview_with_a_problem_cannot_run(world):
    backlog(world)
    world.mountinfo.write_text(f'40 1 0:40 / {world.mount} rw - xfs /dev/x rw\n')
    result = preview(world)
    assert result.problem and not result.rows
    with pytest.raises(exceptions.SocaException):
        world.service.start_backfill(result.preview_id, 'schedule 1', 'admin')


def test_a_backfill_of_an_implausible_share_of_folders_shows_the_problem(world):
    for index in range(1, 13):
        backlog(world, str(index))
    result = preview(world)
    assert 'of job folders' in result.problem
    assert module._preview_summary(result)['confirm_phrase'] is None


def test_numbered_folders_with_no_job_do_not_dilute_the_share(world):
    for index in range(1, 13):
        backlog(world, str(index))
    for index in range(100, 200):
        job_folder(world, str(index), with_record=False)
    assert 'of job folders' in preview(world).problem


def test_only_one_operation_at_a_time(world):
    backlog(world)
    result = preview(world)
    assert world.service._busy.acquire(blocking=False)
    try:
        with pytest.raises(exceptions.SocaException):
            world.service.start_preview('scratch', 'admin')
        with pytest.raises(exceptions.SocaException):
            world.service.start_backfill(result.preview_id, 'schedule 1', 'admin')
    finally:
        world.service._busy.release()


def test_a_preview_runs_in_the_background(world):
    backlog(world)
    world.service.start_preview('scratch', 'admin')
    wait_for_run(world)
    status = world.service.status()
    assert status['progress'] is None
    assert status['previews'][0]['confirm_phrase'] == 'schedule 1'
    assert status['previews'][0]['action_counts'] == {'schedule': 1, 'keep': 0}


def test_listing_a_preview_filters_and_pages(world):
    for index in range(1, 6):
        backlog(world, str(index))
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    (world.mount / 'alice' / 'loose').mkdir()
    result = preview(world)
    rows, total = world.service.list_preview(result.preview_id, 'schedule', 0, 2)
    assert total == 5 and len(rows) == 2 and rows[0]['reason_text']
    assert world.service.list_preview(result.preview_id, 'keep', 0, 50)[1] == 0


# upcoming and failed jobs


# what a finished job gets


def test_a_successful_job_gets_a_date_30_days_after_it_ended(world):
    job = finished_job()
    world.service.record_job_finished(job)
    row = world.store.removals['uid-123:123']
    assert row['remove_at'] == (job.end_time + timedelta(days=30)).isoformat()
    assert row['outcome'] == 'succeeded'


@pytest.mark.parametrize(
    'overrides',
    [
        {'exit_status': 1},
        {'exit_status': 2},
        {'exit_status': 271},
        {'exit_status': None},
        {'disposition': 'deleted'},
    ],
    ids=['failed', 'array part failed', 'killed', 'no status', 'deleted'],
)
def test_anything_else_that_ran_gets_60_days(world, overrides):
    job = finished_job(**overrides)
    world.service.record_job_finished(job)
    row = world.store.removals['uid-123:123']
    assert row['remove_at'] == (job.end_time + timedelta(days=60)).isoformat()
    assert row['outcome'] == 'failed'


@pytest.mark.parametrize(
    'overrides',
    [{'start_time': None}, {'state': SocaJobState.EXIT}],
    ids=['never ran', 'still exiting'],
)
def test_a_job_that_never_ran_or_has_not_finished_gets_nothing(world, overrides):
    world.service.record_job_finished(finished_job(**overrides))
    assert not world.store.removals


def test_both_times_are_settings(world):
    world.config['scheduler.scratch_cleanup.success_retention_days'] = 20
    world.config['scheduler.scratch_cleanup.failed_retention_days'] = 40
    good = finished_job()
    bad = finished_job('124', exit_status=1)
    world.service.record_job_finished(good)
    world.service.record_job_finished(bad)
    assert (
        world.store.removals['uid-123:123']['remove_at']
        == (good.end_time + timedelta(days=20)).isoformat()
    )
    assert (
        world.store.removals['uid-124:124']['remove_at']
        == (bad.end_time + timedelta(days=40)).isoformat()
    )


def test_a_job_that_fails_on_a_later_look_takes_the_longer_date(world):
    world.service.record_job_finished(finished_job())
    world.service.record_job_finished(finished_job(exit_status=1))
    assert world.store.removals['uid-123:123']['outcome'] == 'failed'


def test_a_failed_jobs_folder_goes_after_its_longer_time_only(world):
    folder = job_folder(world, succeeded=False)
    made = world.state.records['123'][0]
    put_event(
        world,
        made,
        remove_at=(REAL_NOW + timedelta(days=1)).isoformat(),
        targets=told_about(
            world, '123', first=REAL_NOW.isoformat(), final=REAL_NOW.isoformat()
        ),
    )
    world.now = REAL_NOW + timedelta(days=45)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()
    assert next(iter(world.store.removals.values()))['last_reason'] == (
        Reason.TOO_RECENT.value
    )
    world.now = REAL_NOW + timedelta(days=61)
    assert run(world)['removed_folders'] == 1
    assert not folder.exists()


def test_nothing_else_under_an_owner_is_ever_a_candidate(world):
    named = job_folder(world, 'run-with-a-name', with_record=False)
    uid_named = job_folder(world, 'job-55-abcdefghijkl', with_record=False)
    other_shape = job_folder(world, '77_1', with_record=False)
    top = world.mount / 'job-55-abcdefghijkl'
    top.mkdir()
    for name in ('55', '77[]', 'run-with-a-name'):
        made = record(name)
        world.state.records[name] = [made]
        put_event(world, made)
    assert run(world)['removed_folders'] == 0
    assert (
        named.exists() and uid_named.exists() and other_shape.exists() and top.exists()
    )
    assert (world.mount / 'alice').is_dir()


def array_of(world, indices=(1, 2), base='77', own=None, **parent):
    """an array's subjob folders, its parent's record on the record"""
    folders = {i: job_folder(world, f'{base}[{i}]', with_record=False) for i in indices}
    made = record(f'{base}[]', **parent)
    world.state.records[f'{base}[]'] = [made]
    put_event(world, made)
    for i, values in (own or {}).items():
        world.state.records[f'{base}[{i}]'] = [
            record(f'{base}[{i}]', job_uid=f'own-{base}-{i}', **values)
        ]
    return folders


def test_a_finished_array_removes_each_subjobs_folder_and_ends_its_record(world):
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    folders = array_of(world)
    sibling = job_folder(world, '770', with_record=False)
    assert run(world)['removed_folders'] == 2
    assert not any(folder.exists() for folder in folders.values())
    assert sibling.exists()
    assert not world.store.removals


def test_the_record_stays_until_the_last_subjob_folder_goes(world):
    world.config['scheduler.scratch_cleanup.max_folders_per_run'] = 1
    folders = array_of(world)
    assert run(world)['removed_folders'] == 1
    assert sum(folder.exists() for folder in folders.values()) == 1
    assert len(world.store.removals) == 1
    assert run(world)['removed_folders'] == 1
    assert not world.store.removals


def test_an_array_with_a_running_subjob_keeps_every_folder(world):
    folders = array_of(world, own={1: {}})
    world.state.active = ActiveJobs(base_ids=frozenset({'77'}))
    assert run(world)['removed_folders'] == 0
    assert all(folder.exists() for folder in folders.values())


def test_a_subjob_starting_during_removal_stops_it(world, monkeypatch):
    monkeypatch.setattr(module, 'STATE_RECHECK_SECONDS', 0)
    folders = array_of(world, indices=(1,))
    for index in range(1500):
        (folders[1] / f'f{index}').touch()
    re_tell(world)

    def appear(calls):
        if calls == 3:
            world.state.active = ActiveJobs(base_ids=frozenset({'77'}))

    world.state.on_active = appear
    assert run(world)['status'] == 'stopped'
    assert folders[1].exists() and os.listdir(folders[1])


def test_mixed_subjob_results_go_by_each_subjobs_own_status(world):
    folders = array_of(world, own={1: {}, 2: {'succeeded': False}}, succeeded=False)
    world.now = REAL_NOW + timedelta(days=45)
    for row in world.store.removals.values():
        row['remove_at'] = (REAL_NOW + timedelta(days=1)).isoformat()
        row['targets'] = told_about(
            world, row['job_id'], first=REAL_NOW.isoformat(), final=REAL_NOW.isoformat()
        )
    assert run(world)['removed_folders'] == 1
    assert not folders[1].exists(), 'its own success, 30 days'
    assert folders[2].exists(), 'its own failure, 60 days'


def test_with_no_subjob_status_a_mixed_array_waits_the_failed_time(world):
    folders = array_of(world, succeeded=False)
    world.now = REAL_NOW + timedelta(days=45)
    for row in world.store.removals.values():
        row['remove_at'] = (REAL_NOW + timedelta(days=1)).isoformat()
    assert run(world)['removed_folders'] == 0
    assert all(folder.exists() for folder in folders.values())


def test_a_subjob_folder_with_no_record_at_all_stays(world):
    folder = job_folder(world, '88[1]', with_record=False)
    assert reasons(preview(world))['88[1]'] == Reason.NO_FINISHED_RECORD
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_subjob_folder_is_not_listed_twice_with_both_records_on_file(world):
    array_of(world, indices=(1,), own={1: {}})
    own = world.state.records['77[1]'][0]
    put_event(world, own)
    assert run(world)['removed_folders'] == 1
    assert not world.store.audit == {}
    assert len(world.store.audit) == 1
    run(world)
    assert not world.store.removals, (
        'the other record ends quietly when its folder is gone'
    )


def test_an_array_goes_through_both_digests_counting_each_folder(world):
    digests_on(world)
    folders = array_of(world)
    row = next(iter(world.store.removals.values()))
    row['remove_at'] = (world.now + timedelta(days=10)).isoformat()
    row['targets'] = {}
    assert run(world)['noticed_folders'] == 2
    assert digests(world)[0].params['first_count'] == 2
    week(world)
    assert run(world)['noticed_folders'] == 2
    world.now += timedelta(days=4)
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    assert run(world)['removed_folders'] == 2
    assert not any(folder.exists() for folder in folders.values())


def test_a_backfill_lists_each_subjob_folder(world):
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    folders = {i: job_folder(world, f'66[{i}]', with_record=False) for i in (1, 2)}
    world.state.records['66[]'] = [record('66[]')]
    result = preview(world)
    assert [row.action for row in result.rows] == ['schedule', 'schedule']
    world.service.start_backfill(result.preview_id, result.confirm_phrase, 'admin')
    wait_for_run(world)
    assert len(world.store.removals) == 1
    assert all(folder.exists() for folder in folders.values())


def test_renaming_a_folder_keeps_it_for_good(world):
    folder = job_folder(world)
    renamed = folder.with_name('keep-this')
    folder.rename(renamed)
    assert run(world)['removed_folders'] == 0
    assert renamed.exists() and not world.store.removals


# digests


DAY = LATER.weekday()


def digests_on(world):
    world.config['scheduler.scratch_cleanup.notify_owners'] = True
    world.config['scheduler.scratch_cleanup.digest_weekday'] = DAY


def due_in(world, name='123', days=10, owner='alice', **values):
    folder = job_folder(world, name, owner=owner, with_record=False)
    made = record(name, owner, **values)
    world.state.records[name] = [made]
    put_event(
        world,
        made,
        remove_at=(world.now + timedelta(days=days)).isoformat(),
        targets={},
    )
    return folder


def digests(world):
    return [n for n in world.sent if n.template_name == TEMPLATE_DIGEST]


def week(world, count=1):
    world.now = world.now + timedelta(days=7 * count)


def test_a_folder_is_in_exactly_two_digests_then_goes(world):
    digests_on(world)
    folder = due_in(world, days=10)
    outcome = run(world)
    assert outcome['noticed_folders'] == 1 and outcome['removed_folders'] == 0
    first = digests(world)[0].params
    assert first['first_count'] == 1 and first['final_count'] == 0
    world.sent.clear()
    week(world)
    outcome = run(world)
    final = digests(world)[0].params
    assert final['first_count'] == 0 and final['final_count'] == 1
    assert outcome['removed_folders'] == 0, 'the final digest gives its own days'
    world.sent.clear()
    world.now += timedelta(days=4)
    assert run(world)['removed_folders'] == 1
    assert not folder.exists()
    assert digests(world) == [], 'no third mention, and not a separate email'
    week(world)
    world.now -= timedelta(days=4)
    run(world)
    summary = digests(world)[0].params
    assert summary['first_count'] == 0 and summary['final_count'] == 0
    assert summary['removed_count'] == 1, 'one summary of what went'
    assert not world.store.removed


def test_nobody_hears_before_14_days_remain(world):
    digests_on(world)
    due_in(world, days=15)
    assert run(world)['noticed_folders'] == 0 and digests(world) == []


def test_a_digest_goes_once_on_its_day_and_not_on_others(world):
    digests_on(world)
    due_in(world, '123', days=10)
    run(world)
    assert len(digests(world)) == 1
    due_in(world, '124', days=10)
    world.now += timedelta(hours=2)
    run(world)
    assert len(digests(world)) == 1, 'once that day, however many checks'
    world.now += timedelta(days=1)
    run(world)
    assert len(digests(world)) == 1, 'not on another day'


def test_one_digest_per_owner_with_totals_the_soonest_and_the_largest_few(world):
    digests_on(world)
    for index in range(1, 9):
        due_in(world, str(index), days=5 + index)
    due_in(world, '50', days=3, owner='bob')
    run(world)
    by_owner = {n.username: n.params for n in digests(world)}
    assert set(by_owner) == {'alice', 'bob'}
    alice = by_owner['alice']
    assert alice['first_count'] == 8 and alice['first_gb'] >= 0
    assert len(alice['largest']) == 5, 'the size is capped'
    assert alice['page_url'].endswith('/home/completed-jobs')
    assert alice['soonest']


def test_a_folder_never_goes_within_7_days_of_its_first_notice(world):
    digests_on(world)
    folder = due_in(world, days=-30)
    run(world)
    world.now += timedelta(days=6)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_folder_never_goes_without_its_final_notice(world):
    digests_on(world)
    folder = due_in(world, days=-30)
    run(world)
    row = next(iter(world.store.removals.values()))
    notices = next(iter(row['targets'].values()))
    assert notices.get('first_notice_at') and not notices.get('final_notice_at')
    world.now += timedelta(days=6, hours=23)
    world.config['scheduler.scratch_cleanup.digest_weekday'] = (DAY + 1) % 7
    world.now += timedelta(days=30)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_digest_that_did_not_go_moves_the_date(world):
    digests_on(world)
    folder = due_in(world, days=1)
    world.context.notifications_client.send_notification.side_effect = RuntimeError(
        'queue down'
    )
    outcome = run(world)
    assert outcome['noticed_folders'] == 0 and 'alice' in outcome['message']
    assert logged_alert(world)
    world.context.notifications_client.send_notification.side_effect = (
        lambda notification: world.sent.append(notification)
    )
    world.now += timedelta(days=3)
    assert run(world)['removed_folders'] == 0, 'the date has passed but nobody was told'
    assert folder.exists()
    world.now += timedelta(days=4)
    assert run(world)['noticed_folders'] == 1
    assert folder.exists()


@pytest.mark.parametrize(
    'broken',
    [
        lambda world: world.config.update({'cluster.ses.enabled': False}),
        lambda world: world.config.update(
            {'cluster-manager.notifications.email.enabled': False}
        ),
        lambda world: world.disabled.add('alice'),
        lambda world: world.no_email.add('alice'),
    ],
    ids=['ses off', 'email off', 'account disabled', 'no address'],
)
def test_a_digest_nobody_can_receive_holds_the_folder_and_alerts(world, broken):
    digests_on(world)
    folder = due_in(world, days=1)
    broken(world)
    outcome = run(world)
    assert outcome['noticed_folders'] == 0
    assert 'alice' in outcome['message'] or 'email' in outcome['message']
    assert logged_alert(world)
    week(world, 3)
    assert run(world)['removed_folders'] == 0
    assert folder.exists() and digests(world) == []


def test_a_folder_a_rule_keeps_is_not_mentioned(world):
    digests_on(world)
    due_in(world, days=10)
    world.state.active = ActiveJobs(owners=frozenset({'alice'}))
    assert run(world)['noticed_folders'] == 0 and digests(world) == []


def test_the_page_says_when_owners_cannot_be_told(world):
    digests_on(world)
    world.config['cluster.ses.enabled'] = False
    assert 'email' in world.service.status()['message']


# administrators


def test_administrators_get_one_weekly_summary_only(world):
    digests_on(world)
    world.state.active_error = StateUnavailable('qselect failed')
    run(world)
    assert templates(world) == [], 'a failure sends no email of its own'
    assert logged_alert(world)
    world.state.active_error = None
    run(world)
    told = [n for n in world.sent if n.template_name == TEMPLATE_ADMIN_SUMMARY]
    assert [n.username for n in told] == ['admin']
    assert 'qselect failed' in told[0].params['failures'][0]
    assert not world.store.failures
    world.sent.clear()
    world.now += timedelta(hours=2)
    run(world)
    assert world.sent == []


def test_a_quiet_week_tells_administrators_nothing(world):
    digests_on(world)
    run(world)
    assert world.sent == []


def test_the_summary_says_what_is_waiting_undelivered(world):
    digests_on(world)
    due_in(world, days=1)
    world.disabled.add('alice')
    run(world)
    told = [n for n in world.sent if n.template_name == TEMPLATE_ADMIN_SUMMARY]
    assert 'alice' in told[0].params['waiting_message']
    assert told[0].params['scheduled_folders'] == 1


def test_a_long_list_of_failures_is_capped(world):
    digests_on(world)
    for index in range(40):
        world.store.put_failure(str(index), {'message': f'stopped {index}'})
    run(world)
    told = [n for n in world.sent if n.template_name == TEMPLATE_ADMIN_SUMMARY][0]
    assert len(told.params['failures']) == module.SUMMARY_MAX_ITEMS
    assert told.params['more_failures'] == 20


def test_a_partial_removal_reaches_the_owner_in_the_next_digest(world, monkeypatch):
    job_folder(world)
    job = SimpleNamespace(job_id='123', scratch_removed_at=None)
    monkeypatch.setattr(world.state, 'find_job', lambda _: job)
    _interrupt(world, monkeypatch, OSError('input/output error'))
    outcome = run(world)
    assert outcome['partial_folders'] == 1
    assert job.scratch_partly_removed_at == LATER
    assert job.scratch_removed_at is None
    assert next(iter(world.store.audit.values()))['removed_entries'] == 7
    assert world.sent == [], 'no email of its own'
    digests_on(world)
    monkeypatch.setattr(module, 'remove_folder', lambda *a, **k: 1)
    week(world)
    world.store.removals.clear()
    run(world)
    assert digests(world)[0].params['partly_removed_count'] == 1


def test_an_interruption_with_nothing_removed_tells_nobody(world, monkeypatch):
    job_folder(world)
    _interrupt(world, monkeypatch, OSError('input/output error'), entries=0)
    assert run(world).get('partial_folders', 0) == 0
    assert not world.store.removed


def test_a_kill_switch_part_way_is_recorded_as_partial(world, monkeypatch):
    job_folder(world)
    _interrupt(world, monkeypatch, module.SwitchedOff('The kill switch is on.'))
    assert run(world)['partial_folders'] == 1
    assert next(iter(world.store.removed.values()))['partly'] is True
    assert next(iter(world.store.audit.values()))['status'] == 'started'


def test_a_folder_that_changed_during_removal_is_labelled_for_what_happened(
    world, monkeypatch
):
    folder = job_folder(world)
    real = module.remove_folder
    _interrupt(world, monkeypatch, module.FolderInUse('written during removal'))
    assert run(world)['status'] == 'stopped'
    assert next(iter(world.store.audit.values()))['status'] == 'failed'
    monkeypatch.setattr(module, 'remove_folder', real)
    world.now += timedelta(days=2)
    assert reasons(preview(world))['123'] == Reason.PARTLY_REMOVED
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_a_removal_row_for_a_folder_since_gone_is_closed(world):
    world.store.put_audit(
        {
            'path': str(world.mount / 'alice' / 'gone'),
            'started_at': 'earlier',
            'status': 'started',
            'device': 1,
            'inode': 2,
        }
    )
    job_folder(world)
    run(world)
    assert (
        world.store.audit[('earlier', str(world.mount / 'alice' / 'gone'))]['status']
        == 'interrupted'
    )


def test_a_failed_row_whose_folder_went_stops_being_tracked(world, monkeypatch):
    folder = job_folder(world)
    _interrupt(world, monkeypatch, module.FolderInUse('written during removal'))
    run(world)
    import shutil

    shutil.rmtree(folder)
    job_folder(world, '124')
    monkeypatch.setattr(module, 'remove_folder', lambda *a, **k: 1)
    run(world)
    statuses = [row['status'] for row in world.store.audit.values()]
    assert 'failed' not in statuses and 'failed_gone' in statuses


# backfill and dates


def test_a_backfill_uses_the_same_dates_and_removes_nothing(world):
    old = backlog(world, '123')
    failed = backlog(
        world, '124', succeeded=False, start_time=REAL_NOW - timedelta(hours=2)
    )
    young = backlog(world, '125')
    world.state.records['125'] = [
        record('125', end_time=REAL_NOW + timedelta(days=89), start_time=REAL_NOW)
    ]
    result = preview(world)
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    result = preview(world)
    world.service.start_backfill(result.preview_id, result.confirm_phrase, 'admin')
    wait_for_run(world)
    rows = {row['job_id']: row for row in world.store.removals.values()}
    end = record('123').end_time
    assert rows['123']['remove_at'] == (end + timedelta(days=30)).isoformat()
    assert rows['124']['remove_at'] == (end + timedelta(days=60)).isoformat()
    assert rows['124']['outcome'] == 'failed'
    assert old.exists() and failed.exists() and young.exists()


def test_a_backfill_goes_through_both_notices(world):
    digests_on(world)
    folder = backlog(world)
    result = preview(world)
    world.service.start_backfill(result.preview_id, 'schedule 1', 'admin')
    wait_for_run(world)
    assert run(world)['noticed_folders'] == 1 and folder.exists()
    week(world)
    assert run(world)['noticed_folders'] == 1 and folder.exists()
    world.now += timedelta(days=4)
    assert run(world)['removed_folders'] == 1


def test_a_backfill_leaves_named_folders(world):
    loose = world.mount / 'alice' / 'old-results'
    loose.mkdir(parents=True)
    result = preview(world)
    assert result.confirm_phrase is None and loose.exists()


def test_large_fixture(world):
    """20,000 job folders across 50 owners: one backfill, then removals capped per check."""
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    world.config['scheduler.scratch_cleanup.max_run_bytes_percent'] = 100
    active = set()
    for job in range(20000):
        owner = f'user{job % 50}'
        world.uids[owner] = os.getuid()
        folder = world.mount / owner / str(job)
        folder.mkdir(parents=True)
        if job % 4 == 0:
            active.add(str(job))
        elif job % 4 == 1:
            pass  # no record
        else:
            world.state.records[str(job)] = [
                record(
                    str(job), owner, end_time=REAL_NOW + timedelta(seconds=job % 600)
                )
            ]
    world.state.active = ActiveJobs(base_ids=frozenset(active))
    started = time.monotonic()
    result = preview(world)
    assert time.monotonic() - started < 60
    counts = {}
    for row in result.rows:
        counts[row.reason] = counts.get(row.reason, 0) + 1
    assert counts[Reason.JOB_ACTIVE] == 5000
    assert counts[Reason.NO_FINISHED_RECORD] == 5000
    assert counts[Reason.ELIGIBLE] == 10000
    world.service.start_backfill(result.preview_id, result.confirm_phrase, 'admin')
    wait_for_run(world)
    assert len(world.store.removals) == 10000
    assert run(world)['removed_folders'] == 0, 'owners have not been told'
    told = (LATER - timedelta(days=20)).isoformat()
    for row in world.store.removals.values():
        row['targets'] = told_about(world, row['job_id'], row['owner'], told, told)
    outcome = run(world)
    assert outcome['removed_folders'] == 25
    removed = [row for row in world.store.audit.values() if row['status'] == 'removed']
    gone = {row['job_id'] for row in removed}
    assert len(removed) == 25 and not gone & active
    assert all(int(job) % 4 > 1 for job in gone)
    assert len(world.store.removals) == 10000 - 25
    assert max(int(job) % 600 for job in gone) <= 2, 'oldest first'


def test_the_digest_says_how_to_keep_a_folder_and_how_windows_users_reach_it():
    import jinja2
    import yaml

    templates_file = (
        _source_root() / 'idea-cluster-manager/resources/defaults/email_templates.yml'
    )
    by_name = {
        template['name']: template
        for template in yaml.safe_load(templates_file.read_text())['templates']
    }
    body = jinja2.Template(by_name[TEMPLATE_DIGEST]['body']).render(
        owner='alice',
        first_count=2,
        first_gb=1.5,
        final_count=1,
        final_gb=0.5,
        soonest='2026-12-01',
        largest=[{'path': '/lustre/alice/1', 'job_id': '1', 'size_gb': 1.0}],
        removed_count=0,
        page_url='https://portal.example.com/#/home/completed-jobs',
    )
    assert 'first notice' in body and 'final notice' in body and '2026-12-01' in body
    assert 'Rename the folder' in body and 'Change a file' in body
    assert 'Windows desktops do not mount' in body
    assert 'completed-jobs' in body
    only_removed = jinja2.Template(by_name[TEMPLATE_DIGEST]['body']).render(
        owner='alice', removed_count=3, removed_gb=2.0, partly_removed_count=1
    )
    assert 'To keep a folder' not in only_removed and 'part way' in only_removed


def test_a_backfill_stopped_by_the_folder_limit_says_what_to_do(world):
    for index in range(1, 13):
        backlog(world, str(index))
    problem = preview(world).problem
    assert 'Most job folders per run' in problem
    assert '100%' in problem
    assert 'only sets dates' in problem
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    assert preview(world).problem is None


def test_a_backfill_stopped_by_the_capacity_limit_says_to_set_it_back(
    world, monkeypatch
):
    real = module.inspect_folder

    def huge(*args, **kwargs):
        return replace(real(*args, **kwargs), size_bytes=300 * GiB)

    monkeypatch.setattr(module, 'inspect_folder', huge)
    backlog(world)
    problem = preview(world).problem
    assert 'Most capacity per run' in problem
    assert '30%' in problem
    assert 'set it back to 25%' in problem


def test_the_folder_limit_never_applies_to_a_check_and_capacity_still_does(
    world, monkeypatch
):
    world.config['scheduler.scratch_cleanup.max_folders_per_run'] = 100
    folders = [job_folder(world, str(index)) for index in range(1, 13)]
    assert run(world)['removed_folders'] == 12
    assert not any(folder.exists() for folder in folders)
    real = module.inspect_folder

    def huge(*args, **kwargs):
        return replace(real(*args, **kwargs), size_bytes=300 * GiB)

    monkeypatch.setattr(module, 'inspect_folder', huge)
    folder = job_folder(world, '50')
    outcome = run(world)
    assert outcome['status'] == 'stopped' and 'of capacity' in outcome['message']
    assert 'Most capacity' not in outcome['message'], 'that advice is for a backfill'
    assert folder.exists()


# two file systems: each folder has its own notices, and the record outlives the first


def second_file_system(world, tmp_path):
    other = tmp_path / 'lustre-b'
    other.mkdir()
    with open(world.mountinfo, 'a') as mountinfo:
        mountinfo.write(f'41 1 0:41 / {other} rw - lustre 10.0.0.9@tcp:/bbbbbbbb rw\n')
    world.config['shared-storage']['other'] = {
        'provider': 'fsx_lustre',
        'mount_dir': str(other),
        'title': 'Other',
        'fsx_lustre': {'mount_name': 'bbbbbbbb', 'file_system_id': 'fs-2'},
    }
    world.config['scheduler.scratch_cleanup.file_systems'] = ['scratch', 'other']
    return other


def folder_on(mount, owner='alice', name='123'):
    folder = mount / owner / name
    (folder / 'sub').mkdir(parents=True)
    (folder / 'a.dat').write_bytes(b'x' * 8192)
    return folder


def run_on(world, name):
    file_system = next(f for f in world.service.file_systems() if f.name == name)
    return world.service._execute(file_system, 'schedule', 'tester')


def test_notices_about_one_file_system_do_not_clear_another(world, tmp_path):
    other = second_file_system(world, tmp_path)
    here = job_folder(world)
    there = folder_on(other)
    assert run_on(world, 'other')['removed_folders'] == 0
    assert there.exists(), 'told only about the folder on the first'
    assert run_on(world, 'scratch')['removed_folders'] == 1
    assert not here.exists()
    # the record stays for the folder not yet resolved
    row = next(iter(world.store.removals.values()))
    assert list(row['targets']) == []
    assert run_on(world, 'other')['removed_folders'] == 0
    assert there.exists()


def test_a_user_removing_one_folder_does_not_let_the_other_go_untold(world, tmp_path):
    other = second_file_system(world, tmp_path)
    here = job_folder(world)
    there = folder_on(other)
    shutil.rmtree(here)
    assert run_on(world, 'scratch')['removed_folders'] == 0
    assert len(world.store.removals) == 1, 'the other file system still has a folder'
    assert run_on(world, 'other')['removed_folders'] == 0
    assert there.exists()


def test_each_file_system_goes_through_its_own_notices_then_the_record_ends(
    world, tmp_path
):
    other = second_file_system(world, tmp_path)
    digests_on(world)
    here = due_in(world, days=-30)
    there = folder_on(other)
    for _ in range(2):
        run_on(world, 'scratch')
        run_on(world, 'other')
        world.now += timedelta(days=7)
    world.now += timedelta(days=4)
    world.config['scheduler.scratch_cleanup.max_run_share_percent'] = 100
    assert run_on(world, 'scratch')['removed_folders'] == 1
    assert len(world.store.removals) == 1
    assert run_on(world, 'other')['removed_folders'] == 1
    assert not here.exists() and not there.exists()
    assert world.store.removals == {}


def test_a_file_system_that_cannot_be_read_keeps_the_record(world, tmp_path):
    other = second_file_system(world, tmp_path)
    here = job_folder(world)
    folder_on(other)
    world.mountinfo.write_text(world.mountinfo.read_text().splitlines()[0] + '\n')
    assert run_on(world, 'scratch')['removed_folders'] == 1
    assert not here.exists()
    assert len(world.store.removals) == 1


def test_a_folder_replaced_under_its_name_is_told_about_again(world):
    digests_on(world)
    folder = job_folder(world)
    # on ext4 the new directory usually gets the old inode back
    shutil.rmtree(folder)
    time.sleep(0.05)
    replacement = folder_on(world.mount)
    assert run(world)['removed_folders'] == 0
    assert replacement.exists()


def test_the_inode_alone_does_not_identify_a_folder():
    same = dict(file_system='scratch', name='123', inode=7)
    assert module._target_key(
        SimpleNamespace(ctime_ns=1, **same)
    ) != module._target_key(SimpleNamespace(ctime_ns=2, **same))


def test_a_folder_changed_since_the_scan_is_left_alone(world, monkeypatch):
    folder = job_folder(world)
    real_remove = world.service._remove_opened

    def remove(root_device, chain, row, *rest):
        row.ctime_ns += 1
        return real_remove(root_device, chain, row, *rest)

    monkeypatch.setattr(world.service, '_remove_opened', remove)
    assert run(world)['removed_folders'] == 0
    assert folder.exists()


def test_each_email_renders_every_value_from_what_the_service_sends(world):
    import jinja2
    import yaml

    templates_file = (
        _source_root() / 'idea-cluster-manager/resources/defaults/email_templates.yml'
    )
    by_name = {
        template['name']: template
        for template in yaml.safe_load(templates_file.read_text())['templates']
    }
    digests_on(world)
    due_in(world, days=1)
    world.store.put_failure('1', {'message': 'stopped once'})
    run(world)
    sent = {n.template_name: n.params for n in world.sent}
    assert {TEMPLATE_DIGEST, TEMPLATE_ADMIN_SUMMARY} <= set(sent)
    strict = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for name in (TEMPLATE_DIGEST, TEMPLATE_ADMIN_SUMMARY):
        for part in ('subject', 'body'):
            # a value the template names and the service never sends fails here
            strict.from_string(by_name[name][part]).render(**sent[name])
    summary = strict.from_string(by_name[TEMPLATE_ADMIN_SUMMARY]['body']).render(
        **sent[TEMPLATE_ADMIN_SUMMARY]
    )
    assert 'and  are' not in summary
