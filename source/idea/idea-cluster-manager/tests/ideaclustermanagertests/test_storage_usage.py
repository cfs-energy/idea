import os
from types import SimpleNamespace
from unittest.mock import Mock
from ideadatamodel import User, Group, exceptions, errorcodes
from ideasdk.filesystem.user_identity import UserIdentity
from ideasdk.filesystem.filesystem_helper import FileSystemHelper

import pytest

from ideaclustermanager.app.filesystem import storage_usage
from ideaclustermanager.app.api.file_browser_api import ClusterFileBrowserAPI


NOW = 1_800_000_000
DAY = 86400


def account_context():
    uid = 65534 if os.geteuid() == 0 else os.getuid()
    gid = 65534 if os.geteuid() == 0 else os.getgid()
    gids = [gid] if os.geteuid() == 0 else sorted(set(os.getgroups()) | {gid})
    groups = {str(g): Group(name=str(g), gid=g) for g in gids}
    user = User(username='alice', uid=uid, gid=gid, additional_groups=list(groups))
    accounts = SimpleNamespace(get_user=lambda _: user, get_group=groups.__getitem__)
    return Mock(accounts=accounts)


@pytest.fixture
def tree(tmp_path):
    import tempfile
    from pathlib import Path

    temporary = tempfile.TemporaryDirectory(dir='/tmp')
    base = Path(temporary.name)
    base.chmod(0o755)
    home = base / 'alice'
    nested = home / 'reports' / 'nested'
    nested.mkdir(parents=True)
    (home / 'empty').mkdir()
    for path, content, age in (
        (home / 'root.txt', b'root', 100),
        (home / 'reports' / 'recent.txt', b'new', 2),
        (nested / 'old.txt', b'old data', 100),
        (nested / 'boundary.txt', b'90', 90),
    ):
        path.write_bytes(content)
        os.utime(path, (NOW - age * DAY, NOW - age * DAY))
    for path in (nested, home / 'reports', home / 'empty', home):
        os.utime(path, (NOW - 3 * DAY, NOW - 3 * DAY))
    outside = tmp_path / 'private.txt'
    outside.write_bytes(b'private' * 100)
    (home / 'linked-file').symlink_to(outside)
    (home / 'linked-dir').symlink_to(tmp_path, target_is_directory=True)
    if os.geteuid() == 0:
        for root, dirs, files in os.walk(home):
            os.chown(root, 65534, 65534)
            for name in dirs + files:
                os.chown(os.path.join(root, name), 65534, 65534, follow_symlinks=False)
    try:
        yield str(home)
    finally:
        temporary.cleanup()


def test_walk_counts_top_level_folders_and_unchanged_bytes(tree):
    result = storage_usage.walk_home(tree, now=NOW)
    assert result['state'] == 'ready'
    assert not result['partial']
    assert result['total']['bytes'] == 17
    assert result['total']['files'] == 4
    assert result['total']['unchanged_90_days_bytes'] == 14
    rows = {row['name']: row for row in result['folders']}
    assert set(rows) == {'reports', 'empty'}
    assert rows['reports']['bytes'] == 13
    assert rows['reports']['files'] == 3
    assert rows['reports']['unchanged_90_days_bytes'] == 10
    assert rows['reports']['newest_mtime'] == NOW - 2 * DAY
    assert rows['reports']['oldest_mtime'] == NOW - 100 * DAY
    assert rows['empty']['oldest_mtime'] is None
    assert rows['empty']['bytes'] == 0
    assert result['directories'][tree + '/reports/nested']['bytes'] == 10


def test_entry_cap_returns_partial_counts(tree):
    result = storage_usage.walk_home(tree, max_entries=2, now=NOW)
    assert result['state'] == 'ready'
    assert result['partial']
    assert result['total']['files'] < 4


def test_time_cap_returns_partial_instead_of_waiting(tree):
    result = storage_usage.walk_home(tree, max_seconds=0, now=NOW)
    assert result['partial']
    assert result['total']['files'] == 0


def test_unreadable_folder_does_not_expose_contents(tree, monkeypatch):
    os.chmod(tree + '/reports', 0)
    try:
        monkeypatch.setattr(
            'pwd.getpwnam', Mock(side_effect=AssertionError('No passwd entry'))
        )
        result = storage_usage.scan_as_user(
            UserIdentity(account_context().accounts, 'alice'), tree, tree
        )
    finally:
        os.chmod(tree + '/reports', 0o700)
    assert result['partial']
    assert result['total']['bytes'] == 4
    assert tree + '/reports/nested' not in result['directories']
    row = next(row for row in result['folders'] if row['name'] == 'reports')
    assert row['partial']
    assert row['unreadable']


def test_replaced_directory_cannot_redirect_scan(tree, tmp_path, monkeypatch):
    original_open = os.open
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'secret').write_bytes(b'secret' * 100)

    def replace(path, flags, **kwargs):
        if path == 'reports':
            os.rename(tree + '/reports', tree + '/moved')
            os.symlink(private, tree + '/reports')
        return original_open(path, flags, **kwargs)

    monkeypatch.setattr(storage_usage.os, 'open', replace)
    result = storage_usage.walk_home(tree, now=NOW)
    assert result['partial']
    assert result['total']['bytes'] <= 17


def make_service(monkeypatch, tree):
    helper = Mock()
    helper.get_user_home.return_value = tree
    helper.run_as_user.side_effect = lambda operation: operation()
    factory = Mock(return_value=helper)
    monkeypatch.setattr(storage_usage, 'FileSystemHelper', factory)
    return storage_usage.StorageUsageService(account_context()), helper, factory


def test_first_call_computes_then_cache_serves_user_and_nested_folder(
    monkeypatch, tree
):
    service, helper, factory = make_service(monkeypatch, tree)
    try:
        assert service.get_usage('alice')['state'] == 'computing'
        future = service._cache[('alice', tree)]['future']
        future.result(timeout=5)
        result = service.get_usage('alice', tree + '/reports/nested')
        assert result['state'] == 'ready'
        assert result['folder']['bytes'] == 10
        assert 'directories' not in result
        assert service._cache[('alice', tree)]['future'] is future
        helper.check_access.assert_not_called()
        assert service.get_usage('bob')['state'] == 'computing'
        assert factory.call_args.args[1] == 'bob'
    finally:
        service._workers.shutdown()


def test_cache_expires_after_one_hour(monkeypatch, tree):
    service, _, _ = make_service(monkeypatch, tree)
    try:
        service.get_usage('alice')
        cached = service._cache[('alice', tree)]
        cached['future'].result(timeout=5)
        cached['created'] -= storage_usage.CACHE_SECONDS + 1
        assert service.get_usage('alice')['state'] == 'computing'
        assert service._cache[('alice', tree)]['future'] is not cached['future']
    finally:
        service._workers.shutdown()


@pytest.mark.parametrize(
    'folder', ['/other/home', '../bob', '/alice-neighbor/folder', 123]
)
def test_folder_lookup_cannot_escape_home(monkeypatch, tree, folder):
    service, _, _ = make_service(monkeypatch, tree)
    try:
        with pytest.raises(Exception):
            service.get_usage('alice', folder)
        assert service._cache == {}
    finally:
        service._workers.shutdown()


def test_api_uses_caller_and_includes_only_available_quotas():
    collector = Mock()
    collector.has_ontap_storage.return_value = True
    collector.get_user_quotas.return_value = [{'used_bytes': 123}]
    api = ClusterFileBrowserAPI(SimpleNamespace(storage_metrics=collector))
    api.storage_usage = Mock()
    api.storage_usage.get_usage.side_effect = lambda *args: {'state': 'computing'}
    invocation = Mock(
        namespace='FileBrowser.GetStorageUsage', request_payload={'username': 'bob'}
    )
    invocation.get_username.return_value = 'alice'
    invocation.is_authorized_user.return_value = True
    api.invoke(invocation)
    api.storage_usage.get_usage.assert_called_once_with('alice', None)
    collector.get_user_quotas.assert_called_once_with('alice')
    assert invocation.success.call_args.args[0]['quotas'] == [{'used_bytes': 123}]
    assert invocation.success.call_args.args[0]['quota_status'] == 'available'
    collector.get_user_quotas.return_value = []
    api.invoke(invocation)
    assert 'quotas' not in invocation.success.call_args.args[0]
    assert invocation.success.call_args.args[0]['quota_status'] == 'unavailable'


def test_api_refuses_non_user_authorization():
    api = ClusterFileBrowserAPI(SimpleNamespace())
    api.storage_usage = Mock()
    invocation = Mock(namespace='FileBrowser.GetStorageUsage')
    invocation.is_authorized_user.return_value = False
    with pytest.raises(Exception):
        api.invoke(invocation)
    api.storage_usage.get_usage.assert_not_called()


def test_failed_scan_can_be_retried(monkeypatch, tree):
    service, _, _ = make_service(monkeypatch, tree)
    monkeypatch.setattr(
        storage_usage, 'scan_as_user', Mock(side_effect=RuntimeError('failed'))
    )
    try:
        assert service.get_usage('alice')['state'] == 'computing'
        with pytest.raises(RuntimeError):
            service._cache[('alice', tree)]['future'].result(timeout=5)
        assert service.get_usage('alice')['state'] == 'error'
        assert service.get_usage('alice')['state'] == 'computing'
    finally:
        service._workers.shutdown()


@pytest.mark.parametrize('replacement', ['symlink_ancestor', 'identity', 'honest'])
def test_delete_is_bound_to_measured_directory(
    monkeypatch, tree, tmp_path, replacement
):
    service, helper, _ = make_service(monkeypatch, tree)
    path = tree + '/reports/nested'
    try:
        measured = service.get_usage('alice', path)['folder']
        assert measured['identity'] == storage_usage.directory_identity(os.stat(path))
        if replacement == 'symlink_ancestor':
            os.rename(tree + '/reports', tree + '/original')
            outside = tmp_path / 'outside'
            (outside / 'nested').mkdir(parents=True)
            os.symlink(outside, tree + '/reports')
        elif replacement == 'identity':
            os.rename(path, path + '-original')
            os.mkdir(path)
        if replacement == 'honest':
            service.delete_folder('alice', path, measured['identity'])
            helper.delete_files.assert_called_once()
            assert helper.delete_files.call_args.args[0].files == [
                os.path.realpath(path)
            ]
        else:
            with pytest.raises(Exception):
                service.delete_folder('alice', path, measured['identity'])
            helper.delete_files.assert_not_called()
    finally:
        service._workers.shutdown()


def test_new_folder_is_measured_fresh_and_can_be_deleted(monkeypatch, tree):
    service, helper, _ = make_service(monkeypatch, tree)
    try:
        service.get_usage('alice')
        service._cache[('alice', tree)]['future'].result(timeout=15)
        path = tree + '/new'
        os.mkdir(path)
        with open(path + '/file', 'wb') as stream:
            stream.write(b'new bytes')
        result = service.get_usage('alice', path)
        assert result['folder']['bytes'] == 9
        service.delete_folder('alice', path, result['folder']['identity'])
        helper.delete_files.assert_called_once()
        with open(path + '/file', 'ab') as stream:
            stream.write(b'!')
        assert service.get_usage('alice', path)['folder']['bytes'] == 10
    finally:
        service._workers.shutdown()


def test_scan_opens_user_files_only_in_worker(monkeypatch, tree):
    original_open = os.open
    parent_pid = os.getpid()

    def child_open(*args, **kwargs):
        assert os.getpid() != parent_pid
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, 'open', child_open)
    try:
        result = storage_usage.scan_as_user(
            UserIdentity(account_context().accounts, 'alice'), tree, tree
        )
        assert result['total']['bytes'] == 17
    finally:
        monkeypatch.setattr(os, 'open', original_open)


def test_delete_runs_binding_and_removal_as_user(monkeypatch, tree):
    context = account_context()
    context.config().get_string.return_value = tree
    monkeypatch.setattr(FileSystemHelper, 'get_user_home', lambda _: tree)
    service = storage_usage.StorageUsageService(context)
    path = tree + '/reports/nested'
    original_open = os.open
    parent_pid = os.getpid()

    def child_open(*args, **kwargs):
        assert os.getpid() != parent_pid
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, 'open', child_open)
    try:
        measured = service.get_usage('alice', path)['folder']
        service.delete_folder('alice', path, measured['identity'])
        assert not os.path.exists(path)
    finally:
        monkeypatch.setattr(os, 'open', original_open)
        service._workers.shutdown()


def test_usage_refuses_account_without_uid(tree, monkeypatch):
    service, _, _ = make_service(monkeypatch, tree)
    service.context.accounts.get_user('alice').uid = None
    try:
        with pytest.raises(exceptions.SocaException) as exc:
            service.get_usage('alice')
        assert exc.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
        assert service._cache == {}
    finally:
        service._workers.shutdown()


def test_delete_api_uses_authenticated_caller_and_measured_identity():
    api = ClusterFileBrowserAPI(SimpleNamespace())
    api.storage_usage = Mock()
    identity = {'device': '1', 'inode': '2'}
    invocation = Mock(
        namespace='FileBrowser.DeleteFolder',
        request_payload={
            'path': '/home/alice/reports',
            'identity': identity,
            'username': 'bob',
        },
    )
    invocation.get_username.return_value = 'alice'
    invocation.is_authorized_user.return_value = True
    api.invoke(invocation)
    api.storage_usage.delete_folder.assert_called_once_with(
        'alice', '/home/alice/reports', identity
    )
    invocation.is_authorized_user.return_value = False
    with pytest.raises(Exception):
        api.invoke(invocation)
    assert api.storage_usage.delete_folder.call_count == 1


def test_delete_accepts_an_identity_measured_by_another_task(monkeypatch, tree):
    # Each task's NFS mount carries its own device number; only the inode names the folder.
    service, helper, _ = make_service(monkeypatch, tree)
    path = tree + '/reports/nested'
    try:
        measured = service.get_usage('alice', path)['folder']['identity']
        assert set(measured) == {'inode'}
        service.delete_folder(
            'alice', path, {'device': '999', 'inode': measured['inode']}
        )
        helper.delete_files.assert_called_once()
        with pytest.raises(Exception):
            service.delete_folder(
                'alice', path, {'inode': str(int(measured['inode']) + 1)}
            )
    finally:
        service._workers.shutdown()
