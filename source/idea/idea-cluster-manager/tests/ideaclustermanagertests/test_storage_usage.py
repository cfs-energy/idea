import os
import time
import threading
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


def test_files_directly_in_home_past_the_entry_cap_are_all_counted(tree, tmp_path):
    # The only way the cost fallback differs from 26.11.1: that walk checked the cap before
    # seeing the listing had ended, and called this partial although it had every byte. A
    # partial home drops every user's storage share, so this is a fix, not a new basis.
    import shutil

    shutil.rmtree(tree + '/reports')
    shutil.rmtree(tree + '/empty')
    for link in ('linked-file', 'linked-dir'):
        os.unlink(f'{tree}/{link}')
    for index in range(5):
        with open(f'{tree}/f{index}', 'wb') as stream:
            stream.write(b'x' * (index + 1))
    for cap in (6, 3):
        result = storage_usage.walk_home(tree, max_entries=cap, now=NOW)
        assert not result['partial']
        assert result['total']['bytes'] == 4 + 15
        assert result['total']['files'] == 6


def test_time_cap_returns_partial_instead_of_waiting(tree):
    result = storage_usage.walk_home(tree, max_seconds=0, now=NOW)
    assert result['partial']
    assert result['total']['files'] == 1


def test_cutoff_still_lists_every_top_level_folder(tree):
    for index in range(20):
        os.mkdir(f'{tree}/z-folder-{index}')
    for limits in (dict(max_seconds=0), dict(max_entries=1)):
        result = storage_usage.walk_home(tree, now=NOW, **limits)
        names = {row['name'] for row in result['folders']}
        assert {'reports', 'empty'} <= names and len(names) == 22
        assert result['partial']
        assert any(row.get('measured') is False for row in result['folders'])
        assert result['scanned_files'] == result['total']['files']


def test_folders_are_largest_first(tree):
    os.mkdir(tree + '/aaa-small')
    with open(tree + '/aaa-small/file', 'wb') as stream:
        stream.write(b'x')
    result = storage_usage.walk_home(tree, now=NOW)
    assert [row['name'] for row in result['folders']] == [
        'reports',
        'aaa-small',
        'empty',
    ]


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
    api.storage_usage.get_usage.assert_called_once_with('alice', None, False)
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


MONTHS = ['2026-W45', '2026-W44', '2026-W43', '2026-W42', '2026-W41', '2026-11', '2026-10', '2026-09',
          '2026-Q4', '2026-Q3', '2026-Q2', '2026-Q1', '2026', '2025', '2024', '2023', '--2022', 'unknown']  # fmt: skip


def analytics_row(name, used, files, values=None, **kwargs):
    # Shape captured from ONTAP 9.18.1P7 on FSx for ONTAP.
    return dict(
        name=name,
        type='directory',
        is_junction=False,
        analytics=dict(
            bytes_used=used,
            file_count=files,
            subdir_count=2,
            incomplete_data=False,
            by_modified_time=dict(
                bytes_used=dict(
                    values=values or [0] * 18,
                    newest_label='2026-W45',
                    oldest_label='2024',
                )
            ),
        ),
        **kwargs,
    )


class FakeTarget:
    name = 'data'
    endpoint = 'svm.fs-1.fsx.example.invalid'
    username = 'reader'
    password_key = 'key'


def fake_analytics(monkeypatch, tree, volumes=None, pages=None, calls=None):
    from ideaclustermanager.app.filesystem import ontap_analytics

    root = os.path.dirname(os.path.dirname(tree))

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            return volumes or [
                dict(
                    name='profiles',
                    uuid='u-1',
                    nas=dict(path='/profiles'),
                    files=dict(used=100),
                    analytics=dict(state='on', file_count=100),
                )
            ]

        def first_page(self, path):
            if 'return_metadata' in path:
                return dict(
                    records=[
                        dict(
                            analytics=dict(
                                by_modified_time=dict(bytes_used=dict(labels=MONTHS))
                            )
                        )
                    ]
                )
            if calls is not None:
                calls.append(path)
            return (pages or {}).get('page', dict(records=[]))

    monkeypatch.setattr(ontap_analytics, 'OntapClient', Client)
    config = Mock()
    config.get_string.side_effect = lambda key, default=None: {
        'shared-storage.data.mount_dir': root,
        'shared-storage.data.fsx_netapp_ontap.volume.volume_path': '/profiles/Users',
    }.get(key, default)
    collector = Mock(targets=lambda: [FakeTarget()])
    context = Mock(config=lambda: config)
    return ontap_analytics.FolderAnalytics(context, collector)


def test_analytics_rolls_up_without_touching_files_and_is_cached(monkeypatch, tree):
    calls = []
    values = [0] * 18
    values[5] = 100  # 2026-11
    values[7] = 50  # 2026-09
    values[17] = 10  # unknown
    pages = dict(
        page=dict(
            records=[
                analytics_row('..', 9999, 999),
                analytics_row('.', 1000, 40, values),
                analytics_row('reports', 700, 30),
                analytics_row('link-target', 1, 1) | {'is_junction': True},
                dict(name='unscanned', type='directory', is_junction=False),
            ]
        )
    )
    analytics = fake_analytics(monkeypatch, tree, pages=pages, calls=calls)
    found = analytics.folder(tree)
    assert found['self']['bytes'] == 1000
    assert found['self']['unchanged_90_days_bytes'] == 840
    assert [row['name'] for row in found['children']] == ['reports', 'unscanned']
    assert found['children'][1]['measured'] is False and found['children'][1]['partial']
    assert analytics.folder(tree) is found
    assert len(calls) == 1
    assert 'order_by=analytics.bytes_used%20desc' in calls[0]
    assert 'owner' not in calls[0]


def next_link(number):
    return dict(next=dict(href=f'/api/next?page={number}'))


def paged_analytics(monkeypatch, tree, responses):
    # responses: request -> the page, or the error it raises
    calls = []
    pages = {}
    analytics = fake_analytics(monkeypatch, tree, pages=pages, calls=calls)
    from ideaclustermanager.app.filesystem import ontap_analytics

    original = ontap_analytics.OntapClient.first_page

    def first_page(self, path):
        if path.startswith('/api/next'):
            calls.append(path)
            response = responses[path]
            if isinstance(response, Exception):
                raise response
            return response
        return original(self, path)

    monkeypatch.setattr(ontap_analytics.OntapClient, 'first_page', first_page)
    return analytics, pages, calls


def test_every_page_of_a_large_folder_is_read(monkeypatch, tree):
    analytics, pages, calls = paged_analytics(
        monkeypatch,
        tree,
        {
            '/api/next?page=2': dict(
                records=[analytics_row('second', 200, 2)], _links=next_link(3)
            ),
            '/api/next?page=3': dict(records=[analytics_row('third', 100, 1)]),
        },
    )
    pages['page'] = dict(
        records=[analytics_row('.', 1000, 40), analytics_row('first', 300, 3)],
        _links=next_link(2),
    )
    found = analytics.folder(tree)
    assert [r['name'] for r in found['children']] == ['first', 'second', 'third']
    assert found['truncated'] is False
    # one page in size order, then the whole folder in directory order
    assert 'order_by' in calls[0] and 'order_by' not in calls[1]
    assert calls[2:] == ['/api/next?page=2', '/api/next?page=3']


def test_a_folder_past_the_page_limit_says_it_is_truncated(monkeypatch, tree):
    from ideaclustermanager.app.filesystem import ontap_analytics

    limit = ontap_analytics.MAX_PAGES
    analytics, pages, calls = paged_analytics(
        monkeypatch,
        tree,
        {
            f'/api/next?page={n}': dict(
                records=[analytics_row(f'f{n}', 1, 1)], _links=next_link(n + 1)
            )
            for n in range(2, limit + 5)
        },
    )
    pages['page'] = dict(records=[analytics_row('.', 1000, 40)], _links=next_link(2))
    found = analytics.folder(tree)
    assert found['truncated'] is True
    # one page in size order first, so the largest are there, then the limit in directory order
    assert len(calls) == limit + 1
    assert 'order_by=analytics.bytes_used%20desc' in calls[0]
    assert found['children'] == []


class Filer:
    """ONTAP's paging: each page is read afresh and resumes after the last row's place in that
    page's order. Sizes change between pages, as when someone writes into a folder."""

    def __init__(self, count, rows):
        self.sizes = {f'd{n:03}': 1000 - n for n in range(count)}
        self.rows = rows
        self.pages = 0

    def first_page(self, client, path):
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(path).query)
        by_size = 'order_by' in path
        names = sorted(self.sizes)
        if by_size:
            names.sort(key=lambda name: -self.sizes[name])
        after = query.get('after', [None])[0]
        start = names.index(after) + 1 if after else 0
        chunk = names[start : start + self.rows]
        records = [analytics_row(name, self.sizes[name], 1) for name in chunk]
        if not after:
            records.insert(0, analytics_row('.', sum(self.sizes.values()), 40))
        page = dict(records=records)
        if start + self.rows < len(names):
            order = '&order_by=x' if by_size else ''
            page['_links'] = dict(next=dict(href=f'/api/next?after={chunk[-1]}{order}'))
        self.pages += 1
        # someone writes into a folder further down while the next page is asked for
        self.sizes['d025'] += 5000 if self.pages == 1 else 0
        return page


def test_a_folder_that_grows_between_pages_is_not_skipped(monkeypatch, tree):
    from ideaclustermanager.app.filesystem import ontap_analytics

    analytics = fake_analytics(monkeypatch, tree)
    filer = Filer(count=30, rows=10)
    monkeypatch.setattr(ontap_analytics, 'MAX_ROWS', 10)
    original = ontap_analytics.OntapClient.first_page
    monkeypatch.setattr(
        ontap_analytics.OntapClient,
        'first_page',
        lambda self, path: (
            original(self, path)
            if 'return_metadata' in path
            else filer.first_page(self, path)
        ),
    )
    found = analytics.folder(tree)
    names = [row['name'] for row in found['children']]
    assert sorted(names) == sorted(filer.sizes)
    assert names[0] == 'd025'
    assert [row['bytes'] for row in found['children']] == sorted(
        (row['bytes'] for row in found['children']), reverse=True
    )
    assert found['truncated'] is False


def filer_listing(monkeypatch, tree, filer):
    from ideaclustermanager.app.filesystem import ontap_analytics

    analytics = fake_analytics(monkeypatch, tree)
    original = ontap_analytics.OntapClient.first_page
    monkeypatch.setattr(
        ontap_analytics.OntapClient,
        'first_page',
        lambda self, path: (
            original(self, path)
            if 'return_metadata' in path
            else filer.first_page(self, path)
        ),
    )
    return analytics


def test_a_folder_too_big_to_list_shows_its_largest_and_nothing_smaller(
    monkeypatch, tree
):
    from ideaclustermanager.app.filesystem import ontap_analytics

    monkeypatch.setattr(ontap_analytics, 'MAX_ROWS', 10)
    # 80 folders, the largest last in directory order: five pages of ten cannot list them
    filer = Filer(count=80, rows=10)
    filer.sizes = {f'd{n:03}': n + 1 for n in range(80)}
    largest = sorted(filer.sizes, key=lambda name: -filer.sizes[name])[:10]
    found = filer_listing(monkeypatch, tree, filer).folder(tree)
    assert found['truncated'] is True
    # every folder listed is larger than every folder left out
    assert [row['name'] for row in found['children']] == largest


def test_a_folder_read_stops_at_its_deadline(monkeypatch, tree):
    from ideaclustermanager.app.filesystem import ontap_analytics

    monkeypatch.setattr(ontap_analytics, 'MAX_ROWS', 10)
    clock = [1000.0]
    monkeypatch.setattr(
        ontap_analytics,
        'time',
        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: NOW),
    )
    started = []

    class Slow(Filer):
        def first_page(self, client, path):
            started.append(clock[0])
            clock[0] += ontap_analytics.MAX_READ_SECONDS / 2 + 1
            return super().first_page(client, path)

    filer = Slow(count=80, rows=10)
    analytics = filer_listing(monkeypatch, tree, filer)
    start = clock[0]
    found = analytics.folder(tree)
    assert found['truncated'] is True
    assert started and all(
        moment < start + ontap_analytics.MAX_READ_SECONDS for moment in started
    )


def test_a_later_page_that_fails_is_truncated_and_trips_nothing(monkeypatch, tree):
    analytics, pages, _ = paged_analytics(
        monkeypatch, tree, {'/api/next?page=2': RuntimeError('ONTAP returned 503')}
    )
    pages['page'] = dict(
        records=[analytics_row('.', 1000, 40), analytics_row('first', 300, 3)],
        _links=next_link(2),
    )
    found = analytics.folder(tree)
    assert found['truncated'] is True
    assert [r['name'] for r in found['children']] == ['first']
    assert analytics._failed_at == 0


def filer(monkeypatch, tree, answers, labels=(200, dict(records=[{}]))):
    """FolderAnalytics over the real ONTAP client, with the HTTP layer answering by path."""
    from ideaclustermanager.app.filesystem import ontap_analytics
    from ideaclustermanager.app.metrics import storage_metrics_service

    analytics = fake_analytics(monkeypatch, tree)
    monkeypatch.setattr(
        ontap_analytics, 'OntapClient', storage_metrics_service.OntapClient
    )
    sent = []

    def get(url, **kwargs):
        path = url.split('.invalid', 1)[1]
        sent.append(path)
        if path.startswith('/api/storage/volumes?'):
            records = [
                dict(
                    name='profiles',
                    uuid='u-1',
                    nas=dict(path='/profiles'),
                    files=dict(used=100),
                    analytics=dict(state='on', file_count=100),
                )
            ]
            return Mock(status_code=200, json=lambda: dict(records=records))
        if 'return_metadata' in path:
            return Mock(status_code=labels[0], json=lambda: labels[1], text='')
        for name, (status, page) in answers.items():
            if path.split('?')[0].endswith(name):
                return Mock(status_code=status, json=lambda page=page: page, text='')
        raise AssertionError(path)

    monkeypatch.setattr(storage_metrics_service.requests, 'get', get)
    return analytics, sent


@pytest.mark.parametrize('status', (400, 403, 404))
def test_one_persons_refused_path_leaves_analytics_on_for_everyone(
    monkeypatch, tree, status
):
    # GetStorageUsage on a file: ONTAP says 400 not a directory, about that path only
    listing = dict(records=[analytics_row('.', 1000, 40), analytics_row('a', 10, 1)])
    analytics, sent = filer(
        monkeypatch,
        tree,
        {'%2Falice%2F.bashrc': (status, {}), '%2Falice': (200, listing)},
    )
    assert analytics.folder(tree + '/.bashrc') is None
    assert analytics.status(tree)['state'] == 'on'
    assert analytics.folder(tree)['self']['bytes'] == 1000
    assert analytics._failed_at == 0


@pytest.mark.parametrize('status', (401, 429, 500, 503))
def test_a_filer_failure_still_backs_off_for_everyone(monkeypatch, tree, status):
    analytics, sent = filer(monkeypatch, tree, {'%2Falice': (status, {})})
    assert analytics.folder(tree) is None
    reads = len(sent)
    assert analytics.status(tree) == dict(state='failed')
    assert analytics.folder(tree + '/reports') is None
    assert len(sent) == reads


@pytest.mark.parametrize('status', (400, 403, 404))
def test_a_refused_volume_root_backs_off_for_everyone(monkeypatch, tree, status):
    # the labels read asks about the volume root, not the person's path: a refusal there is the
    # role's for the whole volume, so every request would otherwise ask again and get nothing
    listing = dict(records=[analytics_row('.', 1000, 40)])
    analytics, sent = filer(
        monkeypatch, tree, {'%2Falice': (200, listing)}, labels=(status, {})
    )
    assert analytics.folder(tree) is None
    reads = len(sent)
    assert analytics.status(tree) == dict(state='failed')
    assert analytics.folder(tree) is None
    assert len(sent) == reads


def test_a_refused_content_read_is_about_that_path(monkeypatch, tree):
    from ideaclustermanager.app.filesystem import ontap_analytics
    from ideaclustermanager.app.metrics.storage_metrics_service import (
        FileContentsRefused,
    )

    assert ontap_analytics.refused_this_path(FileContentsRefused())
    assert not ontap_analytics.refused_this_path(RuntimeError('timed out'))


def test_analytics_on_but_first_scan_unfinished_is_initializing(monkeypatch, tree):
    volumes = [
        dict(
            name='profiles',
            uuid='u-1',
            nas=dict(path='/profiles'),
            files=dict(used=1000),
            analytics=dict(state='on', file_count=430),
        )
    ]
    analytics = fake_analytics(monkeypatch, tree, volumes=volumes)
    status = analytics.status(tree)
    assert status == dict(state='initializing', progress=43)
    assert analytics.folder(tree) is None


def test_analytics_off_or_unreachable_is_soft(monkeypatch, tree):
    off = [
        dict(
            name='profiles',
            uuid='u-1',
            nas=dict(path='/profiles'),
            analytics=dict(state='off'),
        )
    ]
    analytics = fake_analytics(monkeypatch, tree, volumes=off)
    assert analytics.status(tree) == dict(state='off')
    assert analytics.folder(tree) is None
    analytics = fake_analytics(monkeypatch, tree)
    analytics._volume_list = Mock(side_effect=RuntimeError('unreachable'))
    assert analytics.status(tree) == dict(state='failed')
    analytics._volume_list = Mock(side_effect=AssertionError('backs off'))
    assert analytics.status(tree) == dict(state='failed')


def test_bucket_labels_end_where_they_should():
    from ideaclustermanager.app.filesystem.ontap_analytics import bucket_end
    from datetime import date

    assert bucket_end('2026-W45') == date(2026, 11, 8)
    assert bucket_end('2026-09') == date(2026, 10, 1)
    assert bucket_end('2026-Q4') == date(2027, 1, 1)
    assert bucket_end('--2022') == date(2022, 12, 31)
    assert bucket_end('unknown') is None


def analytics_service(monkeypatch, tree, folders):
    service, _, _ = make_service(monkeypatch, tree)
    source = Mock()
    source.folder.side_effect = lambda path, fresh=False: folders.get(path)
    source.status.return_value = dict(state='on', volume='profiles')
    service._analytics_source = source
    return service, source


def row(path, size, files=1):
    return dict(
        path=path,
        name=os.path.basename(path),
        bytes=size,
        files=files,
        newest_mtime=None,
        oldest_mtime=None,
        unchanged_90_days_bytes=0,
        partial=False,
        subfolders=0,
    )


def test_analytics_view_hides_folders_the_person_cannot_open(monkeypatch, tree):
    os.mkdir(tree + '/secret')
    os.chmod(tree + '/secret', 0)
    folders = {
        tree: dict(
            self=row(tree, 900, 9),
            children=[row(tree + '/secret', 800, 8), row(tree + '/reports', 13, 1)],
            measured_at=NOW,
        )
    }
    service, source = analytics_service(monkeypatch, tree, folders)
    try:
        if os.geteuid() == 0:
            pytest.skip('root opens anything')
        result = service.get_usage('alice')
        rows = {item['name']: item for item in result['folders']}
        assert result['source'] == 'analytics' and not result['partial']
        # left out, not shown at the place its size gives it
        assert 'secret' not in rows and result['hidden_folders'] == 1
        assert rows['reports']['bytes'] == 13
        assert 'files_here' not in result
        assert result['total']['identity']['inode'] == str(os.stat(tree).st_ino)
    finally:
        os.chmod(tree + '/secret', 0o700)
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_a_hidden_folder_leaves_no_gap_that_ranks_its_size(monkeypatch, tree):
    if os.geteuid() == 0:
        pytest.skip('root opens anything')
    for name in ('big', 'locked', 'small'):
        os.mkdir(f'{tree}/{name}')
    os.chmod(tree + '/locked', 0)
    folders = {
        tree: dict(
            self=row(tree, 300, 9),
            children=[
                row(tree + '/big', 127),
                row(tree + '/locked', 50),
                row(tree + '/small', 31),
            ],
            measured_at=NOW,
        )
    }
    service, _ = analytics_service(monkeypatch, tree, folders)
    try:
        result = service.get_usage('alice', study=False)
        assert [item['name'] for item in result['folders']] == ['big', 'small']
        assert result['hidden_folders'] == 1
    finally:
        os.chmod(tree + '/locked', 0o700)
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_a_truncated_folder_is_partial_with_no_direct_file_figure(monkeypatch, tree):
    folders = {
        tree: dict(
            self=row(tree, 900, 9),
            children=[row(tree + '/reports', 13, 1)],
            measured_at=NOW,
            truncated=True,
        )
    }
    service, _ = analytics_service(monkeypatch, tree, folders)
    try:
        result = service.get_usage('alice', study=False)
        # the total is ONTAP's rollup for the folder itself, whole however short the list
        assert result['partial'] is False
        assert 'files_here' not in result
        assert result['truncated'] is True
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


@pytest.mark.parametrize('child', ('unmeasured', 'catching-up'))
def test_an_unmeasured_child_is_not_counted_as_direct_files(monkeypatch, tree, child):
    reports = row(tree + '/reports', 0 if child == 'unmeasured' else 13, 1)
    reports['partial'] = True
    if child == 'unmeasured':
        reports['measured'] = False
    folders = {
        tree: dict(self=row(tree, 900, 9), children=[reports], measured_at=NOW),
    }
    service, _ = analytics_service(monkeypatch, tree, folders)
    try:
        result = service.get_usage('alice', study=False)
        assert 'files_here' not in result
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_a_complete_folder_still_has_its_direct_file_figure(monkeypatch, tree):
    folders = {
        tree: dict(
            self=row(tree, 900, 9),
            children=[row(tree + '/reports', 13, 1)],
            measured_at=NOW,
            truncated=False,
        )
    }
    service, _ = analytics_service(monkeypatch, tree, folders)
    try:
        result = service.get_usage('alice', study=False)
        assert result['partial'] is False
        assert result['files_here'] == dict(bytes=887, files=8)
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_a_scan_still_catching_up_marks_the_analytics_view_partial(monkeypatch, tree):
    own = row(tree, 900, 9)
    own['partial'] = True
    folders = {tree: dict(self=own, children=[], measured_at=NOW)}
    service, _ = analytics_service(monkeypatch, tree, folders)
    try:
        assert service.get_usage('alice', study=False)['partial'] is True
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_cost_measurement_keeps_the_scan_basis_when_analytics_is_on(monkeypatch, tree):
    # a user's share of a file system is what the scan measures, on or off analytics:
    # turning analytics on must not move anyone's storage line
    folders = {tree: dict(self=row(tree, 999999999, 9), children=[], measured_at=NOW)}
    service, source = analytics_service(monkeypatch, tree, folders)
    try:
        result = service.measure_for_costs('alice')
        assert result['state'] == 'ready'
        assert result['total']['bytes'] < 999999999
        assert result['total']['bytes'] == walk_bytes(tree)
        source.folder.assert_not_called()
        # and the browser still gets the analytics view
        assert service.get_usage('alice', study=False)['source'] == 'analytics'
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_a_partial_scan_keeps_the_cost_gate(monkeypatch, tree):
    folders = {tree: dict(self=row(tree, 900, 9), children=[], measured_at=NOW)}
    service, _ = analytics_service(monkeypatch, tree, folders)
    monkeypatch.setattr(
        storage_usage,
        'scan_as_user',
        lambda *args: dict(
            state='ready', partial=True, total=dict(bytes=1), home=tree, directories={}
        ),
    )
    try:
        assert service.measure_for_costs('alice')['partial'] is True
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def walk_bytes(tree):
    return storage_usage.walk_home(tree)['total']['bytes']


@pytest.mark.parametrize('folder', ['/other/place', '../bob', 123, '/', '/tmp/../etc'])
def test_drill_down_cannot_leave_shared_storage(monkeypatch, tree, folder):
    service, source = analytics_service(monkeypatch, tree, {})
    try:
        with pytest.raises(Exception):
            service.get_usage('alice', folder)
        source.folder.assert_not_called()
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_drill_down_through_a_symlink_is_refused(monkeypatch, tree, tmp_path):
    os.symlink(tmp_path, tree + '/escape')
    folders = {
        tree + '/escape': dict(
            self=row(tree + '/escape', 1), children=[], measured_at=NOW
        )
    }
    service, _ = analytics_service(monkeypatch, tree, folders)
    try:
        with pytest.raises(exceptions.SocaException):
            service.get_usage('alice', tree + '/escape')
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_outside_home_without_analytics_says_so_and_does_not_walk(monkeypatch, tree):
    service, source = analytics_service(monkeypatch, tree, {})
    source.status.return_value = dict(state='off')
    monkeypatch.setattr(
        storage_usage, 'scan_as_user', Mock(side_effect=AssertionError('no walk'))
    )
    sibling = os.path.join(os.path.dirname(tree), 'shared-folder')
    os.mkdir(sibling)
    try:
        result = service.get_usage('alice', sibling)
        assert result['state'] == 'unavailable'
        assert 'storage analytics' in result['message']
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_outside_home_with_analytics_on_does_not_say_it_is_off(monkeypatch, tree):
    # analytics is on but could not read this folder (ONTAP refused it)
    service, source = analytics_service(monkeypatch, tree, {})
    sibling = os.path.join(os.path.dirname(tree), 'shared-folder')
    os.mkdir(sibling)
    try:
        result = service.get_usage('alice', sibling)
        assert result['state'] == 'unavailable'
        assert 'not on' not in result['message']
        assert result['message'] == 'Storage analytics could not read this folder.'
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


@pytest.mark.parametrize(
    'status, message',
    (
        (503, 'Storage analytics could not be reached. Try again in a minute.'),
        (403, 'Storage analytics could not read this folder.'),
    ),
)
def test_outside_home_after_a_filer_answer_says_what_happened(
    monkeypatch, tree, status, message
):
    sibling = os.path.join(os.path.dirname(tree), 'shared-folder')
    os.mkdir(sibling)
    analytics, sent = filer(monkeypatch, tree, {'%2Fshared-folder': (status, {})})
    service, _, _ = make_service(monkeypatch, tree)
    service._analytics_source = analytics
    try:
        # the second request lands inside the backoff after a filer failure
        for _ in range(2):
            result = service.get_usage('alice', sibling)
            assert result['state'] == 'unavailable'
            assert result['message'] == message
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_names_in_a_folder_the_person_cannot_open_get_one_answer(monkeypatch, tree):
    if os.geteuid() == 0:
        pytest.skip('root opens anything')
    other = os.path.join(os.path.dirname(tree), 'bob')
    os.makedirs(other + '/parity')
    folders = {
        other + '/parity': dict(
            self=row(other + '/parity', 50), children=[], measured_at=NOW
        )
    }
    service, source = analytics_service(monkeypatch, tree, folders)
    os.chmod(other, 0)
    answers = []
    try:
        for name in ('parity', 'no-such-folder'):
            with pytest.raises(exceptions.SocaException) as raised:
                service.get_usage('alice', f'{other}/{name}', study=False)
            answers.append((raised.value.error_code, raised.value.message))
        assert answers[0] == answers[1]
        assert 'no longer exists or cannot be opened' in answers[0][1]
        # and ONTAP is not asked about either
        source.folder.assert_not_called()
    finally:
        os.chmod(other, 0o700)
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_study_finds_biggest_folders_with_ontap_calls_only(monkeypatch, tree):
    reports = tree + '/reports'
    nested = reports + '/nested'
    folders = {
        tree: dict(
            self=row(tree, 100 * 2**20),
            children=[row(reports, 90 * 2**20), row(tree + '/empty', 2**20)],
            measured_at=NOW,
        ),
        reports: dict(
            self=row(reports, 90 * 2**20),
            children=[row(nested, 85 * 2**20)],
            measured_at=NOW,
        ),
        nested: dict(self=row(nested, 85 * 2**20), children=[], measured_at=NOW),
        tree + '/empty': dict(
            self=row(tree + '/empty', 2**20), children=[], measured_at=NOW
        ),
    }
    service, source = analytics_service(monkeypatch, tree, folders)
    monkeypatch.setattr(
        storage_usage, 'walk_home', Mock(side_effect=AssertionError('no walk'))
    )
    try:
        first = service.get_usage('alice')
        assert first['progress']['steps'][0]['label'] == 'Read your quota total'
        service._study_workers.shutdown(wait=True)
        result = service.get_usage('alice')
        assert not result['progress']['running']
        assert result['progress']['done'] == 4
        # reports is dominated by nested, so nested is what to look at.
        assert [item['name'] for item in result['largest']] == ['nested', 'empty']
    finally:
        service._workers.shutdown()


def test_study_never_descends_into_or_names_what_the_person_cannot_open(
    monkeypatch, tree
):
    if os.geteuid() == 0:
        pytest.skip('root opens anything')
    secret = tree + '/restricted'
    hidden = secret + '/confidential-project'
    open_folder = tree + '/reports'
    os.mkdir(secret)
    folders = {
        tree: dict(
            self=row(tree, 100 * 2**20),
            children=[row(secret, 90 * 2**20), row(open_folder, 10 * 2**20)],
            measured_at=NOW,
        ),
        secret: dict(
            self=row(secret, 90 * 2**20),
            children=[row(hidden, 90 * 2**20)],
            measured_at=NOW,
        ),
        hidden: dict(self=row(hidden, 90 * 2**20), children=[], measured_at=NOW),
        open_folder: dict(
            self=row(open_folder, 10 * 2**20), children=[], measured_at=NOW
        ),
    }
    service, source = analytics_service(monkeypatch, tree, folders)
    os.chmod(secret, 0)
    shown = []

    def folder(path, fresh=False):
        study = service._studies.get('alice')
        if study:
            shown.append(study['progress']['step'])
        return folders.get(path)

    source.folder.side_effect = folder
    try:
        service.get_usage('alice')
        service._study_workers.shutdown(wait=True)
        result = service.get_usage('alice')
        shown.append(result['progress']['step'])
        assert 'Checking reports' in shown
        assert not [step for step in shown if 'restricted' in step or 'confid' in step]
        asked = [call.args[0] for call in source.folder.call_args_list]
        assert tree in asked and secret not in asked and hidden not in asked
        assert 'restricted' not in str(result['largest'])
    finally:
        os.chmod(secret, 0o700)
        service._workers.shutdown()


def test_refresh_requeries_ontap_without_multiplying_studies(monkeypatch, tree):
    folders = {tree: dict(self=row(tree, 10), children=[], measured_at=NOW)}
    service, source = analytics_service(monkeypatch, tree, folders)
    try:
        service.get_usage('alice')
        study = service._studies['alice']
        service.get_usage('alice')
        assert service._studies['alice'] is study
        service.get_usage('alice', refresh=True)
        assert any(call.kwargs.get('fresh') for call in source.folder.call_args_list)
    finally:
        service._study_workers.shutdown()
        service._workers.shutdown()


def test_one_scan_at_a_time_as_the_fallback(monkeypatch, tree):
    service, _, _ = make_service(monkeypatch, tree)
    try:
        assert service._workers._max_workers == 1
    finally:
        service._workers.shutdown()
        service._study_workers.shutdown()


def test_api_reports_bytes_outside_home_and_refresh():
    collector = Mock()
    collector.has_ontap_storage.return_value = True
    collector.get_user_quotas.return_value = [
        dict(used_bytes=1000, volume='profiles', files=5)
    ]
    collector.unattributed_quota.return_value = dict(bytes=77, files=1)
    api = ClusterFileBrowserAPI(SimpleNamespace(storage_metrics=collector))
    api.storage_usage = Mock()
    api.storage_usage.get_usage.side_effect = lambda *args: dict(
        state='ready', total=dict(bytes=400), volume='profiles'
    )
    invocation = Mock(
        namespace='FileBrowser.GetStorageUsage', request_payload={'refresh': True}
    )
    invocation.get_username.return_value = 'alice'
    invocation.is_authorized_user.return_value = True
    api.invoke(invocation)
    api.storage_usage.get_usage.assert_called_once_with('alice', None, True)
    result = invocation.success.call_args.args[0]
    assert result['outside_home_bytes'] == 600
    assert result['unattributed_quota_bytes'] == 77
    assert 'volume' not in result


def quota_api(collector, **usage):
    api = ClusterFileBrowserAPI(SimpleNamespace(storage_metrics=collector))
    api.storage_usage = Mock()
    api.storage_usage.get_usage.side_effect = lambda *args: dict(
        state='ready', total=dict(bytes=400), **usage
    )
    invocation = Mock(namespace='FileBrowser.GetStorageUsage', request_payload={})
    invocation.get_username.return_value = 'alice'
    invocation.is_authorized_user.return_value = True
    api.invoke(invocation)
    return invocation.success.call_args.args[0]


def test_no_outside_home_figure_from_a_partial_home_total():
    # a home total still catching up is a floor, so quota minus it could be too high
    collector = Mock()
    collector.has_ontap_storage.return_value = True
    collector.get_user_quotas.side_effect = lambda user: [
        dict(used_bytes=1000, volume='vol1', files=5, endpoint='svm.fs-1')
    ]
    collector.unattributed_quota.return_value = dict(bytes=0, files=0)
    result = quota_api(
        collector, volume='vol1', volume_endpoint='svm.fs-1', partial=True
    )
    assert 'outside_home_bytes' not in result
    assert (
        quota_api(collector, volume='vol1', volume_endpoint='svm.fs-1')[
            'outside_home_bytes'
        ]
        == 600
    )


def test_bytes_outside_home_count_this_file_systems_volume_only():
    collector = Mock()
    collector.has_ontap_storage.return_value = True
    collector.get_user_quotas.return_value = [
        dict(used_bytes=1000, volume='vol1', files=5, endpoint='svm.fs-1'),
        dict(used_bytes=9000, volume='vol1', files=5, endpoint='svm.fs-2'),
    ]
    collector.unattributed_quota.return_value = dict(bytes=0, files=0)
    result = quota_api(collector, volume='vol1', volume_endpoint='svm.fs-1')
    assert result['outside_home_bytes'] == 600
    assert 'volume_endpoint' not in result
    assert all('endpoint' not in row for row in result['quotas'])


def test_unattributed_bytes_are_asked_of_the_home_file_system():
    collector = Mock()
    collector.has_ontap_storage.return_value = True
    collector.get_user_quotas.return_value = [
        dict(used_bytes=1000, volume='other', files=5, endpoint='svm.fs-2'),
        dict(used_bytes=1000, volume='home', files=5, endpoint='svm.fs-1'),
    ]
    collector.unattributed_quota.return_value = dict(bytes=0, files=0)
    quota_api(collector, volume='home', volume_endpoint='svm.fs-1')
    collector.unattributed_quota.assert_called_once_with('home', 'svm.fs-1')


def test_a_slow_filer_does_not_hold_up_the_request(monkeypatch):
    from ideaclustermanager.app.api import file_browser_api

    monkeypatch.setattr(file_browser_api, 'QUOTA_WAIT_SECONDS', 0.05)
    release = threading.Event()
    collector = Mock()
    collector.has_ontap_storage.return_value = True
    collector.refresh_quota_reports.side_effect = lambda age: release.wait(20)
    collector.get_user_quotas.return_value = []
    started = time.monotonic()
    try:
        result = quota_api(collector)
        assert time.monotonic() - started < 5
        assert result['quota_status'] == 'unavailable'
    finally:
        release.set()


def test_a_quick_filer_answers_in_the_same_request():
    reports = []
    collector = Mock()
    collector.has_ontap_storage.return_value = True
    collector.refresh_quota_reports.side_effect = lambda age: reports.append(age)
    collector.get_user_quotas.side_effect = lambda user: (
        [dict(used_bytes=1, volume='v', files=1)] if reports else []
    )
    collector.unattributed_quota.return_value = dict(bytes=0, files=0)
    assert quota_api(collector)['quota_status'] == 'available'


@pytest.mark.parametrize(
    'error', [FileNotFoundError('gone'), NotADirectoryError('symlink')]
)
def test_a_folder_that_cannot_be_opened_is_a_plain_message(monkeypatch, tree, error):
    service, _, _ = make_service(monkeypatch, tree)
    monkeypatch.setattr(storage_usage, 'scan_as_user', Mock(side_effect=error))
    try:
        with pytest.raises(exceptions.SocaException, match='no longer exists'):
            service.get_usage('alice', tree + '/reports')
    finally:
        service._workers.shutdown()
