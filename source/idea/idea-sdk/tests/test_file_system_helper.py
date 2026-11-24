#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
Test Cases for FileSystemHelper
"""

from ideasdk.filesystem.filesystem_helper import FileSystemHelper
from ideasdk.utils import Utils
from ideadatamodel import (
    exceptions,
    errorcodes,
    DeleteFilesRequest,
    ListFilesRequest,
    ReadFileRequest,
)

import pytest
import os
import datetime
from unittest.mock import MagicMock, Mock
from types import SimpleNamespace
from ideasdk.filesystem.user_identity import UserIdentity
from ideadatamodel import (
    User,
    Group,
    SaveFileRequest,
    CreateFileRequest,
    RenameFileRequest,
    DownloadFilesRequest,
    TailFileRequest,
)


class MockDirEntry:
    def __init__(self, name, is_directory=True):
        self.name = name
        self._is_dir = is_directory
        self._stat = MagicMock()
        self._stat.st_mtime = datetime.datetime.now().timestamp()
        self._stat.st_size = 0 if is_directory else 1024

    def is_dir(self):
        return self._is_dir

    def is_file(self):
        return not self._is_dir

    def stat(self):
        return self._stat


class MockScandir:
    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return self.entries

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def account_context(uid=None, gid=None):
    uid = (65534 if os.geteuid() == 0 else os.getuid()) if uid is None else uid
    gid = (65534 if os.geteuid() == 0 else os.getgid()) if gid is None else gid
    gids = [gid] if os.geteuid() == 0 else sorted(set(os.getgroups()) | {gid})
    groups = {f'group-{g}': Group(name=f'group-{g}', gid=g) for g in gids}
    user = User(
        username='no-passwd-entry', uid=uid, gid=gid, additional_groups=list(groups)
    )
    accounts = SimpleNamespace(get_user=lambda _: user, get_group=groups.__getitem__)
    context = Mock(accounts=accounts)
    context.config().get_string.return_value = '/data'
    return context


@pytest.fixture()
def file_system_helper(request, context, monkeypatch):
    helper = FileSystemHelper(account_context(), 'no-passwd-entry')
    monkeypatch.setattr(helper, '_check_permission', lambda *args: True)
    return helper


@pytest.mark.parametrize('file_system_helper', [['list-root-dir']], indirect=True)
def test_file_browser_list_files_root_directory(
    context, file_system_helper, monkeypatch
):
    """
    list files in root directory and ensure required directories can be listed
    """
    # Mock the scandir function to return only 'apps', 'data', and 'mnt' directories
    mock_entries = [
        MockDirEntry('apps', True),
        MockDirEntry('data', True),
        MockDirEntry('mnt', True),
    ]
    monkeypatch.setattr(os, 'scandir', lambda path: MockScandir(mock_entries))

    result = file_system_helper.list_files(ListFilesRequest(cwd='/'))

    # Make sure the required directories are present
    required_dirs = {'mnt', 'apps', 'data'}
    listed_dirs = {file_data.name for file_data in result.listing}

    # Check that all required directories are in the listed directories
    for required_dir in required_dirs:
        assert required_dir in listed_dirs, (
            f"Required directory '{required_dir}' is missing from the listing"
        )


@pytest.mark.parametrize(
    'file_system_helper', [['read-restricted-file']], indirect=True
)
def test_file_browser_read_file_restricted_access(
    context, file_system_helper, monkeypatch
):
    """
    try to read a file in restricted directories and unauthorized access exception should be thrown
    """

    monkeypatch.setattr(Utils, 'is_file', lambda *_: True)
    monkeypatch.setattr(Utils, 'is_binary_file', lambda *_: False)

    with pytest.raises(exceptions.SocaException) as exc_info:
        file_system_helper.read_file(ReadFileRequest(file='/etc/shadow'))
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


@pytest.fixture
def numeric_tree(monkeypatch):
    import tempfile
    import shutil

    path = tempfile.mkdtemp(prefix='identity-', dir='/tmp')
    context = account_context()
    identity = UserIdentity(context.accounts, 'no-passwd-entry')
    os.chown(path, identity.uid, identity.gid)
    context.config().get_string.return_value = path
    monkeypatch.setattr(
        'pwd.getpwnam', Mock(side_effect=AssertionError('No passwd entry'))
    )
    try:
        yield path, FileSystemHelper(context, 'no-passwd-entry')
    finally:
        for root, dirs, _ in os.walk(path):
            for directory in dirs:
                if not os.path.islink(os.path.join(root, directory)):
                    os.chmod(os.path.join(root, directory), 0o700)
        shutil.rmtree(path)


def test_numeric_access_checks_real_permissions(numeric_tree):
    path, helper = numeric_tree
    readable = os.path.join(path, 'readable')
    unreadable = os.path.join(path, 'unreadable')
    helper.run_as_user(lambda: (os.mkdir(readable), os.mkdir(unreadable, 0)))
    helper.check_access(readable, check_dir=True)
    with pytest.raises(exceptions.SocaException) as exc:
        helper.check_access(unreadable, check_dir=True, check_write=False)
    assert exc.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    with pytest.raises(exceptions.SocaException):
        helper.check_access(unreadable, check_read=False, check_write=True)
    with pytest.raises(exceptions.SocaException):
        helper.check_access(readable + '/../unreadable')


def test_dangling_symlink_does_not_fail_the_listing(numeric_tree):
    path, helper = numeric_tree
    helper.run_as_user(
        lambda: (
            os.mkdir(os.path.join(path, 'kept')),
            os.symlink(
                '/var/tmp/elsewhere/.vscode-server',
                os.path.join(path, '.vscode-server'),
            ),
        )
    )
    listing = helper.list_files(ListFilesRequest(cwd=path)).listing
    assert sorted(f.name for f in listing) == ['.vscode-server', 'kept']
    assert not next(f for f in listing if f.name == '.vscode-server').is_dir


def test_reads_and_writes_run_in_worker(numeric_tree, monkeypatch):
    import builtins

    path, helper = numeric_tree
    parent_pid = os.getpid()
    parent_credentials = (os.getuid(), os.getgid(), os.getgroups())
    original_open = builtins.open

    def child_open(file, *args, **kwargs):
        # the test's own files: multiprocessing opens /dev/null in the child before the
        # credentials are dropped
        if isinstance(file, str) and file.startswith(path):
            assert os.getpid() != parent_pid
            assert os.geteuid() == helper._identity.uid
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', child_open)
    helper.create_file(CreateFileRequest(cwd=path, filename='hello.txt'))
    filename = os.path.join(path, 'hello.txt')
    helper.save_file(
        SaveFileRequest(file=filename, content=Utils.base64_encode('hello'))
    )
    result = helper.read_file(ReadFileRequest(file=filename))
    assert Utils.base64_decode(result.content) == 'hello'
    assert os.stat(filename).st_uid == helper._identity.uid
    assert os.stat(filename).st_gid == helper._identity.gid
    assert (os.getuid(), os.getgid(), os.getgroups()) == parent_credentials


def test_rename_download_and_delete_run_in_worker(numeric_tree, monkeypatch):
    import zipfile

    path, helper = numeric_tree
    monkeypatch.setattr(helper, 'get_user_home', lambda: path)
    helper.create_file(CreateFileRequest(cwd=path, filename='hello.txt'))
    filename = os.path.join(path, 'hello.txt')
    helper.rename_file(RenameFileRequest(file=filename, new_name='renamed.txt'))
    renamed = os.path.join(path, 'renamed.txt')
    archive = helper.download_files(DownloadFilesRequest(files=[renamed]))
    assert os.stat(archive).st_uid == helper._identity.uid
    with zipfile.ZipFile(archive) as stream:
        assert stream.read('renamed.txt') == b''
    helper.delete_files(DeleteFilesRequest(files=[renamed]))
    assert not os.path.exists(renamed)


def test_missing_uid_is_unauthorized_before_worker(monkeypatch):
    context = account_context()
    context.accounts.get_user('no-passwd-entry').uid = None
    fork = Mock(side_effect=AssertionError('Must not fork'))
    monkeypatch.setattr('os.fork', fork)
    with pytest.raises(exceptions.SocaException) as exc:
        FileSystemHelper(context, 'no-passwd-entry').check_access('/data')
    assert exc.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    fork.assert_not_called()


def test_worker_sets_all_credentials_in_order(monkeypatch):
    identity = UserIdentity(account_context(12001, 12002).accounts, 'no-passwd-entry')
    identity.gids = [12002, 12003]
    calls = []
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'setgroups', lambda gids: calls.append(('groups', gids)))
    monkeypatch.setattr(
        os, 'setresgid', lambda *ids: calls.append(('gid', ids)), raising=False
    )
    monkeypatch.setattr(
        os, 'setresuid', lambda *ids: calls.append(('uid', ids)), raising=False
    )
    assert identity.run(lambda: calls) == [
        ('groups', [12002, 12003]),
        ('gid', (12002, 12002, 12002)),
        ('uid', (12001, 12001, 12001)),
    ]
    assert calls == []


def test_worker_returns_permission_error(numeric_tree):
    _, helper = numeric_tree

    def denied():
        raise PermissionError('denied')

    with pytest.raises(exceptions.SocaException) as exc:
        helper.run_as_user(denied)
    assert exc.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_tail_rate_limit_survives_worker_exit(numeric_tree, monkeypatch):
    from ideadatamodel import TailFileRequest
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    monkeypatch.setattr(filesystem_helper, '_tail_rate_limits', {})
    monkeypatch.setattr(filesystem_helper, 'TAIL_FILE_MAX_REQUESTS_PER_WINDOW', 1)
    helper.create_file(CreateFileRequest(cwd=path, filename='log.txt'))
    filename = os.path.join(path, 'log.txt')
    helper.save_file(
        SaveFileRequest(file=filename, content=Utils.base64_encode('line\n'))
    )
    assert helper.tail_file(TailFileRequest(file=filename)).lines == ['line']
    with pytest.raises(exceptions.SocaException) as exc:
        helper.tail_file(TailFileRequest(file=filename))
    assert exc.value.error_code == errorcodes.FILE_BROWSER_TAIL_THROTTLE


def test_failed_credential_drop_does_not_run_operation(monkeypatch, tmp_path):
    identity = UserIdentity(account_context().accounts, 'no-passwd-entry')
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'setgroups', Mock(side_effect=PermissionError('denied')))
    marker = tmp_path / 'must-not-exist'
    with pytest.raises(exceptions.SocaException) as exc:
        identity.run(lambda: marker.touch())
    assert exc.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert not marker.exists()


def test_supplementary_gids_come_from_accounts():
    accounts = Mock()
    accounts.get_user.return_value = User(
        username='no-passwd-entry',
        uid=12001,
        gid=12002,
        additional_groups=['project', 'shared'],
    )
    accounts.get_group.side_effect = lambda name: Group(
        name=name, gid={'project': 12003, 'shared': 12004}[name]
    )
    identity = UserIdentity(accounts, 'no-passwd-entry')
    assert identity.gids == [12002, 12003, 12004]
    accounts.get_user.assert_called_once_with('no-passwd-entry')
    accounts.get_group.assert_any_call('project')
    accounts.get_group.assert_any_call('shared')


def test_delete_removes_read_only_and_server_written_files(numeric_tree):
    # an upload the server wrote and never handed over is root's: unlink needs the folder, not the file
    if os.geteuid() != 0:
        pytest.skip('needs root to create files the user does not own')
    path, helper = numeric_tree
    folder = os.path.join(path, 'uploads')
    helper.run_as_user(lambda: os.mkdir(folder))
    server_written = os.path.join(folder, 'server.dat')
    with open(server_written, 'w') as f:
        f.write('x')
    read_only = os.path.join(folder, 'readonly.dat')
    helper.run_as_user(
        lambda: (open(read_only, 'w').close(), os.chmod(read_only, 0o444))
    )
    locked = os.path.join(path, 'locked')
    os.mkdir(locked, 0o755)  # root's folder: the user may not delete from it
    kept = os.path.join(locked, 'kept.dat')
    open(kept, 'w').close()
    with pytest.raises(exceptions.SocaException) as raised:
        helper.delete_files(DeleteFilesRequest(files=[server_written, read_only, kept]))
    assert raised.value.error_code == errorcodes.FILE_BROWSER_DELETE_FAILED
    assert (
        raised.value.message == 'Could not delete 1 of 3: kept.dat (permission denied)'
    )
    assert not os.path.exists(server_written)
    assert not os.path.exists(read_only)
    assert os.path.exists(kept)


def server_pool():
    # what SocaServer serves with: work off the loop runs where it does in production
    from concurrent.futures import ThreadPoolExecutor
    from ideasdk.server.soca_server import DEFAULT_MAX_WORKERS

    return ThreadPoolExecutor(max_workers=DEFAULT_MAX_WORKERS)


def run_upload(server, request, helper, path):
    # a private loop, not asyncio.run: that unsets the thread's event loop on exit, and the
    # session's server fixture asks for the main thread's loop when it stops
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            server._process_streaming_upload(request, helper, path, 'no-passwd-entry')
        )
    finally:
        loop.close()


def test_streaming_upload_hands_the_file_to_the_user(numeric_tree):
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="data.dat"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\nhello\r\n--b--\r\n'
    )
    chunks = iter([body, None])

    class Stream:
        async def read(self):
            return next(chunks)

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'}, stream=Stream()
    )
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    result = run_upload(server, request, helper, path)
    uploaded = os.path.join(path, 'data.dat')
    assert result['payload']['files_uploaded'] == [uploaded]
    assert open(uploaded).read() == 'hello'
    assert os.stat(uploaded).st_uid == helper._identity.uid
    # and the user can delete what the server wrote for them
    helper.delete_files(DeleteFilesRequest(files=[uploaded]))
    assert not os.path.exists(uploaded)


def test_upload_never_follows_a_planted_link(numeric_tree, tmp_path):
    # a link at the upload's name, dangling or live, is not opened: the next free name is taken
    from ideasdk.server.soca_server import SocaServer

    if os.geteuid() != 0:
        pytest.skip('needs root: the server creates the file')
    path, helper = numeric_tree
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    secret = outside / 'secret'
    secret.write_text('secret')
    planted = outside / 'planted'
    helper.run_as_user(lambda: os.symlink(str(planted), os.path.join(path, 'data.dat')))
    helper.run_as_user(lambda: os.symlink(str(secret), os.path.join(path, 'live.dat')))
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="data.dat"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\nhello\r\n'
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="live.dat"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\nevil\r\n--b--\r\n'
    )
    chunks = iter([body, None])

    class Stream:
        async def read(self):
            return next(chunks)

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'}, stream=Stream()
    )
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    result = run_upload(server, request, helper, path)
    renamed = [os.path.join(path, 'data(1).dat'), os.path.join(path, 'live(1).dat')]
    assert result['payload']['files_uploaded'] == renamed
    assert [f['final_name'] for f in result['payload']['files_renamed']] == [
        'data(1).dat',
        'live(1).dat',
    ]
    assert [open(f).read() for f in renamed] == ['hello', 'evil']
    assert all(os.stat(f).st_uid == helper._identity.uid for f in renamed)
    assert not planted.exists()
    assert secret.read_text() == 'secret' and secret.stat().st_uid == 0
    assert os.path.islink(os.path.join(path, 'data.dat'))


def test_rename_needs_the_folder_not_the_file(numeric_tree):
    if os.geteuid() != 0:
        pytest.skip('needs root to create files the user does not own')
    path, helper = numeric_tree
    read_only = os.path.join(path, 'readonly.dat')
    helper.run_as_user(
        lambda: (open(read_only, 'w').close(), os.chmod(read_only, 0o444))
    )
    helper.rename_file(RenameFileRequest(file=read_only, new_name='renamed.dat'))
    assert os.path.exists(os.path.join(path, 'renamed.dat'))
    server_written = os.path.join(path, 'server.dat')
    with open(server_written, 'w') as f:
        f.write('x')
    helper.rename_file(RenameFileRequest(file=server_written, new_name='mine.dat'))
    assert os.path.exists(os.path.join(path, 'mine.dat'))
    locked = os.path.join(path, 'locked')
    os.mkdir(locked, 0o755)  # root's folder: the user may not rename in it
    kept = os.path.join(locked, 'kept.dat')
    open(kept, 'w').close()
    with pytest.raises(Exception) as exc:
        helper.rename_file(RenameFileRequest(file=kept, new_name='moved.dat'))
    assert exc.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert os.path.exists(kept)


def test_upload_folder_swapped_for_a_link_after_the_check_is_refused(
    numeric_tree, tmp_path
):
    # the check passes on the real folder; a link swapped in before the open is not followed
    from ideasdk.server.soca_server import SocaServer

    if os.geteuid() != 0:
        pytest.skip('needs root: the server creates the file')
    path, helper = numeric_tree
    upper = os.path.join(path, 'upper')
    helper.run_as_user(lambda: os.makedirs(os.path.join(upper, 'inner')))
    outside = tmp_path / 'outside'
    (outside / 'inner').mkdir(parents=True)
    check = helper.check_access

    def check_then_swap(*args, **kwargs):
        check(*args, **kwargs)
        os.rename(upper, upper + '.moved')
        os.symlink(str(outside), upper)

    helper.check_access = check_then_swap
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    with pytest.raises(Exception) as exc:
        server._create_upload(helper, os.path.join(upper, 'inner'), 'data.dat', 'user')
    assert exc.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert list((outside / 'inner').iterdir()) == []


def test_upload_into_a_folder_reached_through_the_users_own_link(numeric_tree):
    from ideasdk.server.soca_server import SocaServer

    if os.geteuid() != 0:
        pytest.skip('needs root: the server creates the file')
    path, helper = numeric_tree
    real = os.path.join(path, 'real')
    helper.run_as_user(
        lambda: (os.mkdir(real), os.symlink(real, os.path.join(path, 'link')))
    )
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    upload, renamed = server._create_upload(
        helper, os.path.join(path, 'link'), 'data.dat', 'user'
    )
    upload.finish()
    assert upload.path == os.path.join(path, 'link', 'data.dat') and not renamed
    assert os.path.isfile(os.path.join(real, 'data.dat'))
    assert os.stat(os.path.join(real, 'data.dat')).st_uid == helper._identity.uid


def test_tail_keeps_the_indentation_of_a_line(numeric_tree):
    import pathlib

    # in the user's folder: as root, tmp_path is root's and closed to the user
    log = pathlib.Path(numeric_tree[0]) / 'run.log'
    numeric_tree[1].run_as_user(
        lambda: log.write_text('Traceback:\n  File "a.py", line 3\n')
    )
    helper = FileSystemHelper(account_context(), 'no-passwd-entry')
    helper.check_access = lambda *args, **kwargs: None

    result = helper._tail_file(TailFileRequest(file=str(log), line_count=10))

    assert result.lines[-1] == '  File "a.py", line 3'


def test_delete_says_what_it_could_not_delete(numeric_tree):
    path, helper = numeric_tree
    here = os.path.join(path, 'here.dat')
    helper.run_as_user(lambda: open(here, 'w').close())
    gone = os.path.join(path, 'gone.dat')

    with pytest.raises(exceptions.SocaException) as raised:
        helper.delete_files(DeleteFilesRequest(files=[here, gone]))

    assert raised.value.error_code == errorcodes.FILE_BROWSER_DELETE_FAILED
    assert raised.value.message == 'Could not delete 1 of 2: gone.dat (not found)'
    assert not os.path.exists(here)


def test_delete_lists_only_the_first_few_failures(numeric_tree):
    path, helper = numeric_tree
    files = [os.path.join(path, f'gone{index}.dat') for index in range(8)]

    with pytest.raises(exceptions.SocaException) as raised:
        helper.delete_files(DeleteFilesRequest(files=files))

    assert raised.value.message.endswith('and 3 more')


def run_download(server, request):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(server.file_download_route(request))
    finally:
        loop.close()


def download_server(username, helper_context):
    from ideasdk.server.soca_server import SocaServer

    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    server._context = helper_context
    server._api_invocation_handler = Mock()
    server._api_invocation_handler.get_file_username.return_value = username
    return server


def test_download_refusals_are_not_saved_as_the_file(numeric_tree):
    path, helper = numeric_tree
    signed_out = SimpleNamespace(args={}, headers={})
    server = download_server('', helper.context)
    server.get_query_param_as_string = lambda *_: None
    assert run_download(server, signed_out).status == 401

    server = download_server('no-passwd-entry', helper.context)
    assert run_download(server, SimpleNamespace(args={})).status == 400
    missing = SimpleNamespace(args={'file': [os.path.join(path, 'gone.dat')]})
    assert run_download(server, missing).status in (403, 404)


def test_slow_work_is_not_reported_as_forbidden(numeric_tree):
    import time

    _, helper = numeric_tree
    helper.run_as_user(lambda: None)

    with pytest.raises(exceptions.SocaException) as raised:
        helper._identity.run(lambda: time.sleep(30), timeout=0.2)

    assert raised.value.error_code == errorcodes.FILE_OPERATION_TIMED_OUT


def archive_tree(numeric_tree, monkeypatch):
    path, helper = numeric_tree
    monkeypatch.setattr(helper, 'get_user_home', lambda: path)
    folder = os.path.join(path, 'run')

    def make():
        os.mkdir(folder)
        open(os.path.join(folder, 'ok.dat'), 'w').close()

    helper.run_as_user(make)
    return path, helper, folder


def test_archive_counts_files_it_could_not_read(numeric_tree, monkeypatch):
    _, helper, folder = archive_tree(numeric_tree, monkeypatch)
    helper.run_as_user(
        lambda: (
            open(os.path.join(folder, 'secret.dat'), 'w').close(),
            os.chmod(os.path.join(folder, 'secret.dat'), 0),
        )
    )

    _, skipped = helper.create_download_archive(DownloadFilesRequest(files=[folder]))

    assert skipped == 1


def test_only_a_generated_archive_is_removed(numeric_tree, monkeypatch):
    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    archive = helper.download_files(DownloadFilesRequest(files=[folder]))
    assert helper.is_generated_archive(archive)
    mine = os.path.join(path, 'idea_downloads', 'results_2026-01-02_03-04-05.zip')
    elsewhere = os.path.join(folder, os.path.basename(archive))
    helper.run_as_user(lambda: (open(mine, 'w').close(), open(elsewhere, 'w').close()))

    helper.remove_generated_archive(mine)
    helper.remove_generated_archive(elsewhere)
    assert os.path.exists(mine) and os.path.exists(elsewhere)

    helper.remove_generated_archive(archive)
    assert not os.path.exists(archive)


def test_archive_leaves_out_earlier_archives(numeric_tree, monkeypatch):
    import zipfile

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    earlier = helper.download_files(DownloadFilesRequest(files=[folder]))
    own_file = os.path.join(path, 'idea_downloads', 'my-notes.txt')
    helper.run_as_user(lambda: open(own_file, 'w').close())

    home_archive = helper.download_files(DownloadFilesRequest(files=[path]))

    with zipfile.ZipFile(home_archive) as stream:
        names = stream.namelist()
    assert any(name.endswith('run/ok.dat') for name in names)
    assert not any(name.endswith(os.path.basename(earlier)) for name in names)
    assert any(name.endswith('idea_downloads/my-notes.txt') for name in names)


def test_downloading_the_downloads_folder_skips_the_archive_being_written(
    numeric_tree, monkeypatch
):
    import zipfile

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    first = helper.download_files(DownloadFilesRequest(files=[folder]))

    archive = helper.download_files(
        DownloadFilesRequest(files=[os.path.dirname(first)])
    )

    with zipfile.ZipFile(archive) as stream:
        names = stream.namelist()
    assert any(name.endswith(os.path.basename(first)) for name in names)
    assert not any(name.endswith(os.path.basename(archive)) for name in names)


def test_archive_stopped_for_time_leaves_nothing_behind(numeric_tree, monkeypatch):
    import time

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    monkeypatch.setattr(helper, '_add_directory_to_zip', lambda *args: time.sleep(30))
    helper.run_as_user(lambda: None)

    with pytest.raises(exceptions.SocaException) as raised:
        helper._identity.run(
            lambda: helper.create_download_archive(
                DownloadFilesRequest(files=[folder])
            ),
            timeout=0.5,
        )

    assert raised.value.error_code == errorcodes.FILE_OPERATION_TIMED_OUT
    assert os.listdir(os.path.join(path, 'idea_downloads')) == []


def test_generated_archive_goes_once_it_has_been_sent(numeric_tree, monkeypatch):
    import asyncio

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    archive = helper.download_files(DownloadFilesRequest(files=[folder]))
    server = download_server('no-passwd-entry', helper.context)
    server.options = SimpleNamespace()

    sent = []

    class Stream:
        async def write(self, data):
            sent.append(data)

    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(
            server._stream_file_download(
                open(archive, 'rb'),
                os.path.basename(archive),
                os.path.getsize(archive),
                after_streamed=lambda: helper.remove_generated_archive(archive),
            )
        )
        assert os.path.exists(archive)
        loop.run_until_complete(response.streaming_fn(Stream()))
    finally:
        loop.close()
    assert sent and not os.path.exists(archive)


def test_refused_upload_is_not_a_success_to_the_browser(numeric_tree):
    import asyncio

    path, helper = numeric_tree
    read_only = os.path.join(path, 'read-only')
    helper.run_as_user(lambda: os.mkdir(read_only, 0o500))
    server = download_server('no-passwd-entry', helper.context)

    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(
            server.file_upload_stream_route(
                SimpleNamespace(args={'cwd': [read_only]}, headers={})
            )
        )
    finally:
        loop.close()

    assert response.status == 403


def test_upload_reports_a_cleaned_up_name(numeric_tree):
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="my data.csv"\r\n'
        b'Content-Type: text/csv\r\n\r\nhello\r\n--b--\r\n'
    )
    chunks = iter([body, None])

    class Stream:
        async def read(self):
            return next(chunks)

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'}, stream=Stream()
    )
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()

    result = run_upload(server, request, helper, path)

    assert result['payload']['files_renamed'] == [
        {'original_name': 'my data.csv', 'final_name': 'my_data.csv'}
    ]


def run_generate_download_url(server, request):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(server.generate_download_url_route(request))
    finally:
        loop.close()


def test_asking_for_a_download_link_with_no_session_is_refused_not_a_server_error():
    # dev: a POST with no token answered 500 and logged two tracebacks, since the error
    # handler named the file before the request body had been read
    from ideasdk.server.soca_server import SocaServer

    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    server._api_invocation_handler = Mock()
    server._api_invocation_handler.get_file_username.return_value = None
    response = run_generate_download_url(server, Mock(json={'file': '/data/x'}))
    assert response.status == 401
    assert (
        Utils.from_json(response.body)['error_code'] == errorcodes.UNAUTHORIZED_ACCESS
    )
    server._logger.error.assert_not_called()


def test_a_download_link_request_that_fails_before_its_body_is_read_still_answers():
    from ideasdk.server.soca_server import SocaServer

    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    server._api_invocation_handler = Mock()
    server._api_invocation_handler.get_username.side_effect = RuntimeError('boom')
    response = run_generate_download_url(server, Mock(json={'file': '/data/x'}))
    assert Utils.from_json(response.body)['error_code'] == errorcodes.GENERAL_ERROR


def download_with_a_swap(server, request, swap_after):
    """
    Runs the real download route, calling swap_after(result) each time a check made as
    the user returns to the server, and returns (status, bytes sent).
    """
    import asyncio

    run_as_user = FileSystemHelper.run_as_user
    server_pid = os.getpid()

    def run_then_swap(self, operation, **kwargs):
        result = run_as_user(self, operation, **kwargs)
        # in the server, not in a nested check inside the user's worker
        if os.getpid() == server_pid:
            swap_after(result)
        return result

    sent = []

    class Stream:
        async def write(self, data):
            sent.append(data)

    loop = asyncio.new_event_loop()
    try:
        FileSystemHelper.run_as_user = run_then_swap
        response = loop.run_until_complete(server.file_download_route(request))
        if getattr(response, 'streaming_fn', None) is not None:
            loop.run_until_complete(response.streaming_fn(Stream()))
    finally:
        FileSystemHelper.run_as_user = run_as_user
        loop.close()
    return response.status, b''.join(sent)


def test_download_sends_the_file_that_was_checked(numeric_tree, tmp_path):
    # the server reads with its own rights: a path that names another file once the
    # user's check is done is refused instead of sent
    path, helper = numeric_tree
    mine = os.path.join(path, 'mine.dat')
    helper.run_as_user(lambda: open(mine, 'w').write('mine'))
    other = tmp_path / 'other.dat'
    other.write_text('other')
    server = download_server('no-passwd-entry', helper.context)
    request = SimpleNamespace(args={'file': [mine]})

    assert download_with_a_swap(server, request, lambda _: None) == (200, b'mine')

    def swap_once(_):
        if not os.path.islink(mine):
            os.remove(mine)
            os.symlink(str(other), mine)

    status, sent = download_with_a_swap(server, request, swap_once)
    assert b'other' not in sent
    assert status == 403


def test_folder_download_sends_the_archive_that_was_checked(
    numeric_tree, monkeypatch, tmp_path
):
    import zipfile
    import io

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    monkeypatch.setattr(
        FileSystemHelper, 'get_user_home', lambda self: path, raising=True
    )
    other = tmp_path / 'other.dat'
    other.write_text('other')
    server = download_server('no-passwd-entry', helper.context)
    request = SimpleNamespace(args={'file': [folder]})

    status, sent = download_with_a_swap(server, request, lambda _: None)
    assert status == 200
    assert any(
        n.endswith('run/ok.dat') for n in zipfile.ZipFile(io.BytesIO(sent)).namelist()
    )

    archives = []

    def swap_the_archive_after_its_check(result):
        # the call after the one that built the archive is the archive's own check
        if archives and not os.path.islink(archives[0]):
            os.remove(archives[0])
            os.symlink(str(other), archives[0])
        if isinstance(result, tuple) and str(result[0]).endswith('.zip'):
            archives.append(result[0])

    status, sent = download_with_a_swap(
        server, request, swap_the_archive_after_its_check
    )
    assert b'other' not in sent
    assert status == 403


def test_an_upload_cut_short_removes_only_the_file_it_created(numeric_tree):
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    upper = os.path.join(path, 'upper')
    other = os.path.join(path, 'other')
    helper.run_as_user(lambda: (os.mkdir(upper), os.mkdir(other)))
    kept = os.path.join(other, 'data.dat')
    helper.run_as_user(lambda: open(kept, 'w').write('keep'))
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="data.dat"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n' + b'x' * 200
    )
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()

    def upload(before_the_end):
        chunks = iter([body])

        class Stream:
            async def read(self):
                chunk = next(chunks, None)
                if chunk is None:
                    before_the_end()
                return chunk

        request = SimpleNamespace(
            headers={'content-type': 'multipart/form-data; boundary=b'},
            stream=Stream(),
        )
        run_upload(server, request, helper, upper)

    upload(lambda: None)
    assert os.listdir(upper) == []

    def swap_the_folder():
        os.rename(upper, upper + '.moved')
        os.symlink(other, upper)

    upload(swap_the_folder)
    assert open(kept).read() == 'keep'


def test_a_bad_download_link_does_not_read_the_signing_secret_each_time():
    import asyncio

    from ideasdk.server.soca_server import SocaServer

    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    server._context = Mock()
    secrets = server._context.aws().secretsmanager()
    secrets.get_secret_value.return_value = {'SecretString': '{"secret": "s"}'}
    server._api_invocation_handler = Mock()
    server._api_invocation_handler.get_file_username.return_value = None
    request = SimpleNamespace(args={'temp_token': ['not-a-token'], 'file': ['/x']})
    loop = asyncio.new_event_loop()
    try:
        for _ in range(3):
            response = loop.run_until_complete(server.file_download_route(request))
            assert response.status == 401
    finally:
        loop.close()
    assert secrets.get_secret_value.call_count == 1


def test_folder_download_is_built_off_the_serving_loop(numeric_tree, monkeypatch):
    import asyncio

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    monkeypatch.setattr(FileSystemHelper, 'get_user_home', lambda self: path)
    run_as_user = FileSystemHelper.run_as_user
    on_loop = []

    def record(self, operation, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return run_as_user(self, operation, **kwargs)

    monkeypatch.setattr(FileSystemHelper, 'run_as_user', record)
    server = download_server('no-passwd-entry', helper.context)
    from ideasdk.server.soca_server import SocaServer

    opened = SocaServer._open_checked_download
    opened_on_loop = []

    def open_and_record(*args):
        try:
            asyncio.get_running_loop()
            opened_on_loop.append(True)
        except RuntimeError:
            opened_on_loop.append(False)
        return opened(*args)

    monkeypatch.setattr(
        SocaServer, '_open_checked_download', staticmethod(open_and_record)
    )
    sent = []

    class Stream:
        async def write(self, data):
            sent.append(data)

    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(
            server.file_download_route(SimpleNamespace(args={'file': [folder]}))
        )
        assert response.status == 200
        loop.run_until_complete(response.streaming_fn(Stream()))
    finally:
        loop.close()

    # built, checked, sent, and removed once sent: none of it as the user on the loop
    assert sent
    assert os.listdir(os.path.join(path, 'idea_downloads')) == []
    assert len(on_loop) >= 3 and not any(on_loop)
    assert opened_on_loop == [False]


def test_downloads_are_never_run_as_a_page(numeric_tree):
    path, helper = numeric_tree
    page = os.path.join(path, 'page.html')
    helper.run_as_user(lambda: open(page, 'w').write('<p>marker</p>'))
    server = download_server('no-passwd-entry', helper.context)
    server._executor = server_pool()

    response = run_download(server, SimpleNamespace(args={'file': [page]}))

    assert response.status == 200
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Content-Security-Policy'] == 'sandbox'
    assert response.headers['Content-Disposition'].startswith('attachment;')


def slow_archives(monkeypatch, seconds):
    """archive builds that take this long, and a Barrier-free way to see them start"""
    import time

    add = FileSystemHelper._add_directory_to_zip

    def slow(self, *args, **kwargs):
        time.sleep(seconds)
        return add(self, *args, **kwargs)

    monkeypatch.setattr(FileSystemHelper, '_add_directory_to_zip', slow)


def wait_for_builds(count, timeout=10):
    import time
    from ideasdk.filesystem import filesystem_helper

    deadline = time.monotonic() + timeout
    while sum(filesystem_helper._archive_builds.values()) < count:
        assert time.monotonic() < deadline, 'archive builds did not start'
        time.sleep(0.02)


def test_quick_file_operations_are_never_refused_for_downloads_in_flight(
    numeric_tree, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from ideasdk.filesystem import filesystem_helper

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    monkeypatch.setattr(filesystem_helper, 'ARCHIVE_BUILD_WAIT_SECONDS', 0.5)
    slow_archives(monkeypatch, 2)
    note = os.path.join(path, 'note.txt')
    helper.run_as_user(lambda: open(note, 'w').write('note'))
    request = DownloadFilesRequest(files=[folder])

    with ThreadPoolExecutor(max_workers=4) as pool:
        builds = [
            pool.submit(helper.download_files, request)
            for _ in range(filesystem_helper.MAX_ARCHIVE_BUILDS_PER_USER)
        ]
        wait_for_builds(filesystem_helper.MAX_ARCHIVE_BUILDS_PER_USER)

        # every quick call of the same person still answers while their builds run
        other = FileSystemHelper(helper.context, 'no-passwd-entry')
        assert other.list_files(ListFilesRequest(cwd=path)).listing
        assert (
            Utils.base64_decode(other.read_file(ReadFileRequest(file=note)).content)
            == 'note'
        )
        other.save_file(SaveFileRequest(file=note, content=Utils.base64_encode('new')))
        other.create_file(CreateFileRequest(cwd=path, filename='made', is_folder=False))
        other.delete_files(DeleteFilesRequest(files=[os.path.join(path, 'made')]))

        # one more build waits for a slot, briefly, and is then refused in plain words
        with pytest.raises(exceptions.SocaException) as busy:
            other.download_files(request)
        assert busy.value.error_code == errorcodes.FILE_OPERATION_BUSY
        assert 'downloads being prepared' in busy.value.message

        for build in builds:
            assert build.result().endswith('.zip')

    # with a slot free again, the next one is built
    assert helper.download_files(request).endswith('.zip')


def test_a_waiting_archive_build_starts_when_a_slot_frees(numeric_tree, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from ideasdk.filesystem import filesystem_helper

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    monkeypatch.setattr(filesystem_helper, 'MAX_ARCHIVE_BUILDS_PER_USER', 1)
    monkeypatch.setattr(filesystem_helper, 'ARCHIVE_BUILD_WAIT_SECONDS', 5)
    slow_archives(monkeypatch, 1)
    request = DownloadFilesRequest(files=[folder])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(helper.download_files, request)
        wait_for_builds(1)
        # waits for the first instead of being refused at once
        assert helper.download_files(request).endswith('.zip')
        assert first.result().endswith('.zip')


def test_archive_builds_leave_server_workers_for_everything_else(
    numeric_tree, monkeypatch
):
    """
    Several people's archive builds in flight, and more asked for: the server's own pool
    still lists files and answers the health check while they run.
    """
    import asyncio
    import time
    from ideasdk.filesystem import filesystem_helper
    from ideasdk.server.soca_server import SocaServer

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    # as if each build were a different person's: only the server-wide limits apply
    monkeypatch.setattr(filesystem_helper, 'MAX_ARCHIVE_BUILDS_PER_USER', 100)
    monkeypatch.setattr(filesystem_helper, 'MAX_ARCHIVE_BUILDS_WAITING_PER_USER', 100)
    slow_archives(monkeypatch, 3)
    server = download_server('no-passwd-entry', helper.context)
    pool_size = server._executor._max_workers
    request = DownloadFilesRequest(files=[folder])

    async def scenario():
        builds = [
            asyncio.ensure_future(server._off_loop(helper.download_files, request))
            for _ in range(pool_size)
        ]
        await asyncio.to_thread(wait_for_builds, filesystem_helper.MAX_ARCHIVE_BUILDS)
        started = time.monotonic()
        listing = await asyncio.wait_for(
            server._off_loop(helper.list_files, ListFilesRequest(cwd=path)), 2
        )
        listed_in = time.monotonic() - started
        health = await asyncio.wait_for(SocaServer.health_check_route(None), 1)
        results = await asyncio.gather(*builds, return_exceptions=True)
        return listing, listed_in, health, results

    loop = asyncio.new_event_loop()
    try:
        listing, listed_in, health, results = loop.run_until_complete(scenario())
    finally:
        loop.close()

    assert listing.listing and listed_in < 1.5
    assert health.status == 200
    built = [r for r in results if isinstance(r, str)]
    refused = [r for r in results if isinstance(r, exceptions.SocaException)]
    assert len(built) <= (
        filesystem_helper.MAX_ARCHIVE_BUILDS
        + filesystem_helper.MAX_ARCHIVE_BUILDS_WAITING
    )
    assert len(built) + len(refused) == pool_size
    assert all(r.error_code == errorcodes.FILE_OPERATION_BUSY for r in refused)


def test_read_file_refuses_more_than_the_editor_opens(numeric_tree, monkeypatch):
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    monkeypatch.setattr(filesystem_helper, 'READ_FILE_MAX_BYTES', 16)
    small = os.path.join(path, 'small.txt')
    large = os.path.join(path, 'large.txt')
    helper.run_as_user(
        lambda: (open(small, 'w').write('x' * 16), open(large, 'w').write('x' * 17))
    )

    assert Utils.base64_decode(
        helper.read_file(ReadFileRequest(file=small)).content
    ) == ('x' * 16)
    with pytest.raises(exceptions.SocaException) as refused:
        helper.read_file(ReadFileRequest(file=large))
    assert refused.value.error_code == errorcodes.INVALID_PARAMS


def test_tail_reads_a_bounded_amount_of_a_file_without_line_breaks(
    numeric_tree, monkeypatch
):
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    monkeypatch.setattr(filesystem_helper, '_tail_rate_limits', {})
    monkeypatch.setattr(filesystem_helper, 'TAIL_FILE_MAX_BYTES', 64)
    filename = os.path.join(path, 'one-line.log')
    helper.run_as_user(lambda: open(filename, 'w').write('x' * 40000))

    first = helper.tail_file(TailFileRequest(file=filename))
    assert sum(len(line) for line in first.lines) == 64

    # a continuation from the start of the file
    resumed = helper.tail_file(
        TailFileRequest(file=filename, next_token=Utils.base64_encode('0;0'))
    )
    assert sum(len(line) for line in resumed.lines) == 64
    offset = int(Utils.base64_decode(resumed.next_token).split(';')[0])
    assert offset == 64


def interrupted_upload(server, helper, folder):
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="data.dat"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n' + b'x' * 200
    )
    chunks = iter([body, None])

    class Stream:
        async def read(self):
            return next(chunks)

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'}, stream=Stream()
    )
    return run_upload(server, request, helper, folder)


def unlink_only_in_a_worker():
    """os.unlink that refuses to run in the server process, restored on exit"""
    import contextlib

    server_pid = os.getpid()
    unlink = os.unlink

    def checked(*args, **kwargs):
        if os.getpid() == server_pid:
            raise AssertionError('removed with the server rights')
        return unlink(*args, **kwargs)

    @contextlib.contextmanager
    def patched():
        os.unlink = checked
        try:
            yield
        finally:
            os.unlink = unlink

    return patched()


def test_an_interrupted_upload_is_removed_as_the_user(numeric_tree):
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()

    with unlink_only_in_a_worker():
        result = interrupted_upload(server, helper, path)

    assert os.listdir(path) == []
    server._logger.warning.assert_not_called()
    # and the browser is not told it worked
    assert result['success'] is False
    assert result['error_code'] == errorcodes.INVALID_PARAMS


def test_an_upload_that_cannot_be_handed_over_is_removed_as_the_user(
    numeric_tree, monkeypatch
):
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    monkeypatch.setattr(
        helper, 'own', Mock(side_effect=RuntimeError('cannot hand over'))
    )

    with unlink_only_in_a_worker(), pytest.raises(RuntimeError):
        interrupted_upload(server, helper, path)

    assert os.listdir(path) == []


def test_transfers_are_never_refused_by_the_archive_limits(numeric_tree, monkeypatch):
    from ideasdk.filesystem import filesystem_helper
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    mine = os.path.join(path, 'mine.dat')
    helper.run_as_user(lambda: open(mine, 'w').write('mine'))
    # as if every archive slot were taken
    monkeypatch.setattr(filesystem_helper, 'MAX_ARCHIVE_BUILDS_PER_USER', 0)
    monkeypatch.setattr(filesystem_helper, 'MAX_ARCHIVE_BUILDS_WAITING_PER_USER', 0)
    with pytest.raises(exceptions.SocaException):
        helper.run_as_user(lambda: None, archive_build=True)

    server = download_server('no-passwd-entry', helper.context)
    assert run_download(server, SimpleNamespace(args={'file': [mine]})).status == 200

    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="up.dat"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\nhello\r\n--b--\r\n'
    )
    chunks = iter([body, None])

    class Stream:
        async def read(self):
            return next(chunks)

    upload_server = object.__new__(SocaServer)
    upload_server._logger = Mock()
    upload_server._executor = server_pool()
    result = run_upload(
        upload_server,
        SimpleNamespace(
            headers={'content-type': 'multipart/form-data; boundary=b'}, stream=Stream()
        ),
        helper,
        path,
    )
    assert result['payload']['files_uploaded'] == [os.path.join(path, 'up.dat')]


def open_descriptors():
    return len(os.listdir('/dev/fd'))


def cancelled_upload(server, helper, folder, body, cancel_when):
    """
    Runs an upload whose browser goes away: the handler is cancelled, as Sanic cancels it
    when the connection is lost, once cancel_when() is true. Returns after the server's
    workers have finished whatever the upload left them.
    """
    import asyncio

    sent = [body]

    class Stream:
        async def read(self):
            if sent:
                return sent.pop()
            await asyncio.Event().wait()  # the rest of the body never comes

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'}, stream=Stream()
    )

    async def scenario():
        task = asyncio.ensure_future(
            server._process_streaming_upload(request, helper, folder, 'no-passwd-entry')
        )
        await asyncio.to_thread(cancel_when)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
    server._executor.shutdown(wait=True)


UPLOAD_HEADERS = (
    b'--b\r\nContent-Disposition: form-data; name="file"; filename="data.dat"\r\n'
    b'Content-Type: application/octet-stream\r\n\r\n'
)


def test_an_upload_cancelled_while_its_file_is_created_leaves_nothing(
    numeric_tree, monkeypatch
):
    import threading
    import time
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    created = threading.Event()
    create = SocaServer._create_upload

    def create_then_linger(self, *args):
        result = create(self, *args)
        created.set()
        time.sleep(0.3)  # the browser goes away while this is still on its thread
        return result

    monkeypatch.setattr(SocaServer, '_create_upload', create_then_linger)
    before = open_descriptors()

    cancelled_upload(server, helper, path, UPLOAD_HEADERS, lambda: created.wait(10))

    assert os.listdir(path) == []
    assert open_descriptors() == before


def test_an_upload_cancelled_before_its_first_byte_leaves_nothing(numeric_tree):
    import time
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    before = open_descriptors()

    def once_created():
        deadline = time.monotonic() + 10
        while not os.listdir(path):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        time.sleep(0.1)  # and waiting for the body

    cancelled_upload(server, helper, path, UPLOAD_HEADERS, once_created)

    assert os.listdir(path) == []
    assert open_descriptors() == before


def test_an_upload_cancelled_part_way_leaves_nothing(numeric_tree):
    import time
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    before = open_descriptors()

    def once_written():
        deadline = time.monotonic() + 10
        while not any(os.path.getsize(os.path.join(path, n)) for n in os.listdir(path)):
            assert time.monotonic() < deadline
            time.sleep(0.02)

    cancelled_upload(server, helper, path, UPLOAD_HEADERS + b'x' * 100000, once_written)

    assert os.listdir(path) == []
    assert open_descriptors() == before


def test_a_file_system_refusal_reaches_the_person_in_plain_words(numeric_tree):
    import errno

    path, helper = numeric_tree

    def quota_reached():
        raise OSError(errno.EDQUOT, 'Disk quota exceeded')

    with pytest.raises(exceptions.SocaException) as refused:
        helper.run_as_user(quota_reached)
    assert refused.value.error_code == errorcodes.FILE_QUOTA_EXCEEDED
    assert 'quota' in refused.value.message

    for code, error_code in [
        (errno.ENOSPC, errorcodes.FILE_SYSTEM_FULL),
        (errno.EFBIG, errorcodes.FILE_TOO_LARGE),
        (errno.ENAMETOOLONG, errorcodes.FILE_NAME_TOO_LONG),
        (errno.EROFS, errorcodes.FILE_SYSTEM_READ_ONLY),
        (errno.EACCES, errorcodes.UNAUTHORIZED_ACCESS),
    ]:

        def fail():
            raise OSError(code, os.strerror(code))

        with pytest.raises(exceptions.SocaException) as refused:
            helper.run_as_user(fail)
        assert refused.value.error_code == error_code

    # anything else stays the server's error, for the generic answer and the log
    def broken():
        raise OSError(errno.EIO, 'Input/output error')

    with pytest.raises(OSError):
        helper.run_as_user(broken)


def test_an_archive_that_fills_the_disk_says_so_and_leaves_nothing(
    numeric_tree, monkeypatch
):
    import errno
    import zipfile
    from ideasdk.filesystem import filesystem_helper

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    helper.run_as_user(lambda: open(os.path.join(folder, 'ok.dat'), 'w').write('data'))

    class FullDisk(zipfile.ZipFile):
        def open(self, name, mode='r', **kwargs):
            entry = super().open(name, mode, **kwargs)
            # a file's data, not the folder entries: the copy is what has to say so
            if mode == 'w' and not str(getattr(name, 'filename', name)).endswith('/'):

                def write(_):
                    raise OSError(errno.ENOSPC, 'No space left on device')

                entry.write = write
            return entry

    # writing the archive fails: the build fails, it does not list the file as unreadable
    monkeypatch.setattr(filesystem_helper, 'ZipFile', FullDisk)
    with pytest.raises(exceptions.SocaException) as refused:
        helper.create_download_archive(DownloadFilesRequest(files=[folder]))
    assert refused.value.error_code == errorcodes.FILE_SYSTEM_FULL
    assert os.listdir(os.path.join(path, 'idea_downloads')) == []


def test_an_upload_onto_a_full_disk_says_so_and_leaves_nothing(
    numeric_tree, monkeypatch
):
    import asyncio
    import errno
    import json
    from ideasdk.server import soca_server

    path, helper = numeric_tree

    class FullDisk(soca_server._Upload):
        def __init__(self, file, *args):
            super().__init__(file, *args)
            real = file

            class Writer:
                def write(self, _):
                    raise OSError(errno.ENOSPC, 'No space left on device')

                def close(self):
                    real.close()

            self.file = Writer()

    monkeypatch.setattr(soca_server, '_Upload', FullDisk)
    server = download_server('no-passwd-entry', helper.context)
    body = UPLOAD_HEADERS + b'x' * 100 + b'\r\n--b--\r\n'
    chunks = iter([body, None])

    class Stream:
        async def read(self):
            return next(chunks)

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'},
        stream=Stream(),
        args={'cwd': [path]},
    )
    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(server.file_upload_stream_route(request))
    finally:
        loop.close()

    assert response.status == 400
    answer = json.loads(response.body)
    assert answer['error_code'] == errorcodes.FILE_SYSTEM_FULL
    assert 'no space left' in answer['message'].lower()
    assert os.listdir(path) == []


def stream_of(*chunks):
    sent = list(chunks)

    class Stream:
        async def read(self):
            return sent.pop(0) if sent else None

    return Stream()


def upload_route(server, path, stream):
    import asyncio
    import json

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'},
        stream=stream,
        args={'cwd': [path]},
    )
    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(server.file_upload_stream_route(request))
    finally:
        loop.close()
    return response.status, json.loads(response.body)


def test_a_part_header_that_never_ends_is_refused_and_not_held(numeric_tree):
    from ideasdk.server import soca_server

    path, helper = numeric_tree
    server = download_server('no-passwd-entry', helper.context)
    held = []
    chunk = b'X-Filler: ' + b'y' * 1000
    big = [b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.dat"\r\n']
    big += [chunk] * 1000  # a megabyte of header with no blank line

    class Counting:
        def __init__(self, chunks):
            self.chunks = chunks

        async def read(self):
            held.append(len(self.chunks))
            return self.chunks.pop(0) if self.chunks else None

    status, answer = upload_route(server, path, Counting(big))
    assert status == 400
    assert answer['error_code'] == errorcodes.INVALID_PARAMS
    assert 'header is too large' in answer['message']
    # refused once past the limit, not after reading the whole body
    chunks_read = 1000 + 1 - held[-1]
    assert chunks_read * len(
        chunk
    ) < soca_server.MAX_UPLOAD_PART_HEADER_BYTES + 2 * len(chunk)
    assert os.listdir(path) == []


def test_a_long_upload_in_many_small_pieces_is_saved_whole(numeric_tree):
    path, helper = numeric_tree
    server = download_server('no-passwd-entry', helper.context)
    data = os.urandom(4 * 1024 * 1024)
    head = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="big.dat"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n'
    )
    body = head + data + b'\r\n--b--\r\n'
    # the header itself one byte at a time, then the data in odd-sized pieces
    pieces = [body[i : i + 1] for i in range(len(head))]
    pieces += [body[i : i + 7919] for i in range(len(head), len(body), 7919)]

    status, answer = upload_route(server, path, stream_of(*pieces))
    assert status == 200 and answer['success']
    with open(os.path.join(path, 'big.dat'), 'rb') as saved:
        assert saved.read() == data


def test_a_closing_delimiter_split_from_its_boundary_ends_the_upload(numeric_tree):
    path, helper = numeric_tree
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server,
        path,
        stream_of(
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.dat"\r\n\r\n'
            b'hello\r\n--b',
            b'--\r\n',
        ),
    )
    assert status == 200 and answer['success']
    assert open(os.path.join(path, 'a.dat')).read() == 'hello'


def test_an_upload_body_that_ends_inside_a_file_is_a_failure(numeric_tree):
    path, helper = numeric_tree
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server,
        path,
        stream_of(
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.dat"\r\n\r\n'
            b'first\r\n--b\r\nContent-Disposition: form-data; name="file"; '
            b'filename="b.dat"\r\n\r\npart of it'
        ),
    )
    assert status == 400 and answer['success'] is False
    assert answer['payload']['files_uploaded'] == [os.path.join(path, 'a.dat')]
    assert sorted(os.listdir(path)) == ['a.dat']


def test_a_listing_is_bounded_and_says_so(numeric_tree, monkeypatch):
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    helper.run_as_user(
        lambda: [open(os.path.join(path, f'f{i}'), 'w').close() for i in range(5)]
    )
    monkeypatch.setattr(filesystem_helper, 'MAX_LISTED_ENTRIES', 3)
    listed = helper.list_files(ListFilesRequest(cwd=path))
    assert len(listed.listing) == 3 and listed.truncated is True
    monkeypatch.setattr(filesystem_helper, 'MAX_LISTED_ENTRIES', 5)
    listed = helper.list_files(ListFilesRequest(cwd=path))
    assert len(listed.listing) == 5 and not listed.truncated


def test_the_root_listing_leaves_out_the_system_folders(numeric_tree):
    from ideasdk.filesystem.filesystem_helper import RESTRICTED_ROOT_FOLDERS

    path, helper = numeric_tree
    names = {
        entry.name for entry in helper.list_files(ListFilesRequest(cwd='/')).listing
    }
    present = set(os.listdir('/')) & set(RESTRICTED_ROOT_FOLDERS)
    assert present, 'this host has none of the system folders at its root'
    assert not names & present


def test_system_folders_stay_out_however_the_path_is_written(numeric_tree):
    path, helper = numeric_tree
    # /usr, not /etc: on macOS /etc is itself a link to /private/etc
    helper.run_as_user(lambda: os.symlink('/usr', os.path.join(path, 'config')))
    for refused in ('//etc/hosts', 'etc/hosts', os.path.join(path, 'config', 'lib')):
        with pytest.raises(exceptions.SocaException) as raised:
            helper.check_access(refused, check_read=True, check_write=False)
        assert raised.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    # a name with two dots in it is a name
    dotted = os.path.join(path, 'a..b')
    helper.run_as_user(lambda: open(dotted, 'w').close())
    helper.check_access(dotted, check_read=True, check_write=False)


def test_a_tail_continuation_is_checked_like_the_first_request(
    numeric_tree, monkeypatch
):
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    monkeypatch.setattr(filesystem_helper, '_tail_rate_limits', {})
    helper.run_as_user(lambda: os.symlink('/usr', os.path.join(path, 'config')))
    readable = next(
        os.path.join(root, name)
        for root, _, names in os.walk('/usr/share')
        for name in sorted(names)
        if os.path.isfile(os.path.join(root, name))
        and not os.path.islink(os.path.join(root, name))
    )
    with pytest.raises(exceptions.SocaException) as raised:
        helper.tail_file(
            TailFileRequest(
                file=os.path.join(path, 'config', os.path.relpath(readable, '/usr')),
                next_token=Utils.base64_encode('0;0'),
            )
        )
    assert raised.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_a_tail_counts_bytes_and_misses_no_line(numeric_tree, monkeypatch):
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    monkeypatch.setattr(filesystem_helper, '_tail_rate_limits', {})
    log = os.path.join(path, 'run.log')
    helper.run_as_user(lambda: open(log, 'w', encoding='utf-8').write('€' * 30 + '\n'))

    # 64 bytes of three-byte characters, not 64 characters
    monkeypatch.setattr(filesystem_helper, 'TAIL_FILE_MAX_BYTES', 64)
    resumed = helper.tail_file(
        TailFileRequest(file=log, next_token=Utils.base64_encode('0;0'))
    )
    assert sum(len(line.encode('utf-8')) for line in resumed.lines) <= 64
    monkeypatch.setattr(filesystem_helper, 'TAIL_FILE_MAX_BYTES', 8 * 1024 * 1024)

    # a line written while the first answer is read comes with the next one
    read_back = FileSystemHelper._tail_file_efficiently

    def read_then_append(self, *args):
        result = read_back(self, *args)
        with open(log, 'a', encoding='utf-8') as f:
            f.write('appended meanwhile\n')
        return result

    monkeypatch.setattr(FileSystemHelper, '_tail_file_efficiently', read_then_append)
    first = helper.tail_file(TailFileRequest(file=log))
    offset = Utils.base64_decode(first.next_token).split(';')[0]
    following = helper.tail_file(
        TailFileRequest(file=log, next_token=Utils.base64_encode(f'{offset};0'))
    )
    assert 'appended meanwhile\n' in following.lines

    # a file cut short since starts again from its top
    helper.run_as_user(lambda: open(log, 'w').write('new\n'))
    restarted = helper.tail_file(
        TailFileRequest(file=log, next_token=Utils.base64_encode('100000;0'))
    )
    assert restarted.lines == ['new\n']


def test_tail_users_leave_the_count_when_their_window_ends(numeric_tree, monkeypatch):
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    monkeypatch.setattr(filesystem_helper, '_tail_rate_limits', {})
    monkeypatch.setattr(filesystem_helper, 'TAIL_FILE_MAX_CONCURRENT_USERS', 3)
    now = [1000.0]
    monkeypatch.setattr(filesystem_helper.time, 'time', lambda: now[0])
    for user in ('a', 'b', 'c'):
        helper._check_tail_rate_limit(user)
    with pytest.raises(exceptions.SocaException):
        helper._check_tail_rate_limit('d')
    # once their minute has passed they are not tailing, with or without a clean-up run
    now[0] += filesystem_helper.TAIL_FILE_RATE_LIMIT_WINDOW + 1
    assert helper._check_tail_rate_limit('d')


@pytest.mark.parametrize(
    'new',
    ['new', 'same length.', 'new content, longer than the original'],
    ids=['shorter', 'equal', 'longer'],
)
def test_a_save_that_cannot_be_written_keeps_the_file(numeric_tree, monkeypatch, new):
    import builtins
    import errno
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    note = os.path.join(path, 'note.txt')
    helper.run_as_user(
        lambda: (open(note, 'w').write('the original'), os.chmod(note, 0o640))
    )
    real_open = builtins.open

    class FullDisk:
        def __init__(self, real):
            self.real = real

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.real.close()
            return False

        def fileno(self):
            return self.real.fileno()

        @property
        def closed(self):
            return self.real.closed

        def close(self):
            self.real.close()

        def write(self, _):
            raise OSError(errno.ENOSPC, 'No space left on device')

    def open_full(file, mode='r', *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return FullDisk(handle) if 'w' in mode else handle

    monkeypatch.setattr(filesystem_helper, 'open', open_full, raising=False)
    with pytest.raises(exceptions.SocaException) as refused:
        helper.save_file(SaveFileRequest(file=note, content=Utils.base64_encode(new)))
    assert refused.value.error_code == errorcodes.FILE_SYSTEM_FULL
    # the original bytes, whatever the new content's length
    assert open(note).read() == 'the original'
    assert sorted(os.listdir(path)) == ['note.txt']


def test_a_save_replaces_the_file_keeping_its_mode_and_follows_a_link(numeric_tree):
    path, helper = numeric_tree
    note = os.path.join(path, 'note.txt')
    link = os.path.join(path, 'link.txt')
    helper.run_as_user(
        lambda: (
            open(note, 'w').write('old'),
            os.chmod(note, 0o640),
            os.symlink(note, link),
        )
    )
    helper.save_file(SaveFileRequest(file=link, content=Utils.base64_encode('new')))
    assert os.path.islink(link)
    assert open(note).read() == 'new'
    assert oct(os.stat(note).st_mode & 0o777) == oct(0o640)
    assert sorted(os.listdir(path)) == ['link.txt', 'note.txt']


def test_a_growing_file_is_sent_as_long_as_it_was_said_to_be(numeric_tree):
    import asyncio

    path, helper = numeric_tree
    log = os.path.join(path, 'growing.log')
    helper.run_as_user(lambda: open(log, 'w').write('x' * 1000))
    server = download_server('no-passwd-entry', helper.context)
    sent = []

    class Stream:
        async def write(self, data):
            sent.append(data)

    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(
            server.file_download_route(SimpleNamespace(args={'file': [log]}))
        )
        assert response.status == 200
        assert 'Accept-Ranges' not in response.headers
        with open(log, 'a') as f:
            f.write('y' * 5000)  # the job writes on after the length was sent
        loop.run_until_complete(response.streaming_fn(Stream()))
    finally:
        loop.close()
    assert b''.join(sent) == b'x' * 1000


def test_a_download_name_cannot_break_its_header(numeric_tree):
    path, helper = numeric_tree
    odd = os.path.join(path, 'a"b\\c.txt')
    helper.run_as_user(lambda: open(odd, 'w').write('x'))
    server = download_server('no-passwd-entry', helper.context)
    response = run_download(server, SimpleNamespace(args={'file': [odd]}))
    header = response.headers['Content-Disposition']
    assert 'filename="a_b_c.txt"' in header
    assert "filename*=UTF-8''a%22b%5Cc.txt" in header


def test_a_disabled_account_or_group_keeps_no_file_access():
    accounts = Mock()
    accounts.get_user.return_value = User(
        username='u', uid=5001, gid=5001, enabled=True, additional_groups=['on', 'off']
    )
    groups = {
        'on': Group(name='on', gid=6001, enabled=True),
        'off': Group(name='off', gid=6002, enabled=False),
    }
    accounts.get_group.side_effect = lambda name: groups[name]
    assert UserIdentity(accounts, 'u').gids == [5001, 6001]

    accounts.get_user.return_value = User(
        username='u', uid=5001, gid=5001, enabled=False
    )
    with pytest.raises(exceptions.SocaException) as refused:
        UserIdentity(accounts, 'u')
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_a_folder_without_search_takes_no_upload(numeric_tree):
    path, helper = numeric_tree
    folder = os.path.join(path, 'no-search')
    helper.run_as_user(lambda: (os.mkdir(folder), os.chmod(folder, 0o600)))
    with pytest.raises(exceptions.SocaException) as refused:
        helper.check_access(folder, check_dir=False, check_read=False, check_write=True)
    assert refused.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_an_archive_is_private_whatever_the_umask(numeric_tree, monkeypatch):
    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    previous = os.umask(0o022)
    try:
        archive = helper.download_files(DownloadFilesRequest(files=[folder]))
    finally:
        os.umask(previous)
    assert oct(os.stat(archive).st_mode & 0o777) == oct(0o600)
    assert oct(os.stat(os.path.dirname(archive)).st_mode & 0o777) == oct(0o700)


def test_an_archive_has_every_folder_odd_names_and_no_fifo(numeric_tree, monkeypatch):
    import zipfile

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)

    def make():
        os.mkdir(os.path.join(folder, 'empty'))
        open(os.path.join(folder, 'back\\slash.dat'), 'w').close()
        os.mkfifo(os.path.join(folder, 'pipe'))
        locked = os.path.join(folder, 'locked')
        os.mkdir(locked)
        open(os.path.join(locked, 'inside.dat'), 'w').close()
        os.chmod(locked, 0)

    helper.run_as_user(make)
    try:
        archive, left_out = helper.create_download_archive(
            DownloadFilesRequest(files=[folder])
        )
    finally:
        helper.run_as_user(lambda: os.chmod(os.path.join(folder, 'locked'), 0o700))
    names = zipfile.ZipFile(archive).namelist()
    assert 'run/' in names and 'run/empty/' in names
    assert 'run/back_slash.dat' in names
    assert not any(n.endswith('pipe') for n in names)
    assert left_out == 2  # the FIFO and the folder that cannot be read


def test_an_archive_is_never_put_inside_itself_through_a_link(
    numeric_tree, monkeypatch
):
    import zipfile

    path, helper = numeric_tree
    real_home = os.path.join(path, 'real-home')
    linked_home = os.path.join(path, 'home-link')
    helper.run_as_user(
        lambda: (os.mkdir(real_home), os.symlink(real_home, linked_home))
    )
    helper.run_as_user(
        lambda: open(os.path.join(real_home, 'mine.dat'), 'w').write('x')
    )
    # the home is reached through a link: the archive's path text is not the one walked
    monkeypatch.setattr(helper, 'get_user_home', lambda: linked_home)
    archive = helper.download_files(DownloadFilesRequest(files=[real_home]))
    names = zipfile.ZipFile(archive).namelist()
    assert 'real-home/mine.dat' in names
    assert not any(n.endswith('.zip') for n in names)


def test_an_archive_build_gets_its_own_deadline(numeric_tree, monkeypatch):
    from ideasdk.filesystem import filesystem_helper

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    timeouts = []
    run = UserIdentity.run

    def record(self, operation, *args, timeout=300, **kwargs):
        if not self.in_worker():
            timeouts.append(timeout)
        return run(self, operation, *args, timeout=timeout, **kwargs)

    monkeypatch.setattr(UserIdentity, 'run', record)
    helper.download_files(DownloadFilesRequest(files=[folder]))
    helper.list_files(ListFilesRequest(cwd=path))
    assert timeouts == [filesystem_helper.ARCHIVE_BUILD_TIMEOUT_SECONDS, 300]


def test_a_worker_that_ignores_being_stopped_does_not_hold_the_server(numeric_tree):
    import signal
    import threading
    import time

    path, helper = numeric_tree
    identity = UserIdentity(helper.context.accounts, 'no-passwd-entry')

    def stubborn():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)

    outcome = {}

    def call():
        try:
            identity.run(stubborn, timeout=1)
        except exceptions.SocaException as e:
            outcome['code'] = e.error_code

    caller = threading.Thread(target=call)
    started = time.monotonic()
    caller.start()
    caller.join(20)
    assert not caller.is_alive(), 'the server worker is still waiting for the child'
    assert outcome['code'] == errorcodes.FILE_OPERATION_TIMED_OUT
    assert time.monotonic() - started < 15


def test_the_file_routes_take_only_people_files_are_for(context_for_routes):
    handler, allowed, outsider = context_for_routes
    assert handler.get_file_username(allowed) == 'member'
    assert handler.get_file_username(outsider) is None


@pytest.fixture
def context_for_routes():
    from ideasdk.auth import ApiAuthorization, ApiAuthorizationType
    from ideasdk.server.soca_server import ApiInvocationHandler

    class Tokens:
        def decode_token(self, token):
            return {'token': token}

        def get_authorization(self, decoded):
            groups = (
                ['test-module-users-module-group']
                if decoded['token'] == 'member'
                else []
            )
            return ApiAuthorization(
                type=ApiAuthorizationType.USER, username=decoded['token'], groups=groups
            )

    group_names = Mock()
    group_names.get_module_users_group.return_value = 'test-module-users-module-group'
    group_names.get_module_administrators_group.return_value = 'admins'
    group_names.get_cluster_administrators_group.return_value = 'cluster-admins'
    group_names.get_cluster_managers_group.return_value = 'managers'
    invoker = Mock()
    invoker.get_token_service.return_value = Tokens()
    invoker.get_namespaces.return_value = ()
    context = account_context()
    from ideasdk.server.soca_server import SocaServer

    handler = ApiInvocationHandler(
        context=context,
        server=SimpleNamespace(
            get_authorization_header=SocaServer.get_authorization_header
        ),
        api_invoker=invoker,
        group_name_helper=group_names,
    )

    def request(token):
        from sanic.compat import Header

        return SimpleNamespace(headers=Header({'authorization': f'Bearer {token}'}))

    return handler, request('member'), request('outsider')


def test_an_upload_into_a_set_group_id_folder_keeps_the_folders_group(numeric_tree):
    import stat

    if os.geteuid() != 0:
        pytest.skip('needs root: the server creates the file')
    path, helper = numeric_tree
    shared = os.path.join(path, 'project')
    project_gid = 4242
    os.mkdir(shared)
    user = UserIdentity(helper.context.accounts, 'no-passwd-entry')
    os.chown(shared, user.uid, project_gid)
    os.chmod(shared, 0o2777)
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server,
        shared,
        stream_of(
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.dat"\r\n\r\n'
            b'x\r\n--b--\r\n'
        ),
    )
    assert status == 200, answer
    info = os.stat(os.path.join(shared, 'a.dat'))
    assert info.st_gid == project_gid
    assert info.st_uid == user.uid
    assert os.stat(shared).st_mode & stat.S_ISGID


def test_a_download_link_keeps_its_five_seconds_after_a_slow_key_read(numeric_tree):
    import asyncio
    import json
    import time

    import jwt

    path, helper = numeric_tree
    mine = os.path.join(path, 'mine.dat')
    helper.run_as_user(lambda: open(mine, 'w').write('x'))
    server = download_server('no-passwd-entry', helper.context)
    server.options = SimpleNamespace(api_path_prefixes=[])

    async def slow_secret():
        await asyncio.sleep(2)  # the key read the first time, from the secret store
        return 'secret'

    server._get_jwt_signing_secret_off_loop = slow_secret
    request = SimpleNamespace(
        json={'file': mine}, scheme='https', host='portal.example', headers={}, args={}
    )
    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(server.generate_download_url_route(request))
    finally:
        loop.close()
    answered = time.time()
    link = json.loads(response.body)['download_url']
    token = link.split('temp_token=')[1]
    claims = jwt.decode(token, 'secret', algorithms=['HS256'])
    assert claims['exp'] - answered > 3


def test_deleting_a_file_never_takes_a_folder_that_took_its_name(numeric_tree):
    path, helper = numeric_tree
    listed = os.path.join(path, 'results')
    helper.run_as_user(lambda: open(listed, 'w').close())

    # between the listing and the confirmation, the file became a folder with work in it
    def swap():
        os.remove(listed)
        os.mkdir(listed)
        open(os.path.join(listed, 'run.dat'), 'w').write('work')

    helper.run_as_user(swap)
    with pytest.raises(exceptions.SocaException) as refused:
        helper.delete_files(DeleteFilesRequest(files=[listed]))
    assert 'is a folder' in refused.value.message
    assert open(os.path.join(listed, 'run.dat')).read() == 'work'

    # the measured-folder delete still removes a folder
    helper.delete_files(DeleteFilesRequest(files=[listed]), allow_directories=True)
    assert not os.path.exists(listed)


def folder_upload_body(*files):
    """parts as a browser sends a folder: the path inside the destination as the file name"""
    body = b''
    for name, data in files:
        body += (
            b'--b\r\nContent-Disposition: form-data; name="files[]"; filename="'
            + name.encode()
            + b'"\r\nContent-Type: application/octet-stream\r\n\r\n'
            + data
            + b'\r\n'
        )
    return body + b'--b--\r\n'


def test_a_folder_upload_keeps_its_tree(numeric_tree):
    path, helper = numeric_tree
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server,
        path,
        stream_of(
            folder_upload_body(
                ('run/inputs/a.dat', b'a'),
                ('run/inputs/b.dat', b'b'),
                ('run/out/deep/c.dat', b'c'),
            )
        ),
    )
    assert status == 200 and answer['success'], answer
    assert answer['payload']['files_uploaded'] == [
        os.path.join(path, 'run/inputs/a.dat'),
        os.path.join(path, 'run/inputs/b.dat'),
        os.path.join(path, 'run/out/deep/c.dat'),
    ]
    assert open(os.path.join(path, 'run/out/deep/c.dat')).read() == 'c'
    user = UserIdentity(helper.context.accounts, 'no-passwd-entry')
    assert os.stat(os.path.join(path, 'run/out/deep')).st_uid == user.uid


def test_a_folder_upload_reuses_folders_and_numbers_a_taken_file_name(numeric_tree):
    path, helper = numeric_tree
    helper.run_as_user(
        lambda: (
            os.makedirs(os.path.join(path, 'run')),
            open(os.path.join(path, 'run', 'a.dat'), 'w').write('mine'),
        )
    )
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server, path, stream_of(folder_upload_body(('run/a.dat', b'new')))
    )
    assert status == 200 and answer['success']
    assert answer['payload']['files_renamed'] == [
        {'original_name': 'run/a.dat', 'final_name': 'run/a(1).dat'}
    ]
    assert open(os.path.join(path, 'run', 'a.dat')).read() == 'mine'
    assert open(os.path.join(path, 'run', 'a(1).dat')).read() == 'new'


@pytest.mark.parametrize(
    'name',
    [
        '/etc/passwd',
        '../outside.dat',
        'run/../../outside.dat',
        'run/./a.dat',
        'run//a.dat',
        'run/',
        'run\\\\x/a.dat',
        'run/\x01/a.dat',
        'x' * 256 + '/a.dat',
        '/'.join(['d'] * 40) + '/a.dat',
    ],
)
def test_a_folder_upload_refuses_every_other_path_form(numeric_tree, tmp_path, name):
    path, helper = numeric_tree
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server,
        path,
        stream_of(folder_upload_body((name, b'x'), ('fine.dat', b'fine'))),
    )
    assert status == 200 and answer['success']
    assert answer['payload']['files_skipped'] == [name]
    # nothing made for the refused part, the other file saved
    assert sorted(os.listdir(path)) == ['fine.dat']
    assert not os.path.exists(os.path.join(os.path.dirname(path), 'outside.dat'))


def test_a_link_where_an_upload_folder_belongs_is_never_followed(
    numeric_tree, tmp_path
):
    path, helper = numeric_tree
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o777)
    os.chmod(outside, 0o777)
    helper.run_as_user(lambda: os.symlink(str(outside), os.path.join(path, 'run')))
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server, path, stream_of(folder_upload_body(('run/a.dat', b'x')))
    )
    assert status == 403
    assert answer['error_code'] == errorcodes.UNAUTHORIZED_ACCESS
    assert list(outside.iterdir()) == []


def test_a_folder_placed_as_a_link_mid_upload_is_never_followed(
    numeric_tree, tmp_path, monkeypatch
):
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    outside = tmp_path / 'outside'
    outside.mkdir()
    os.chmod(outside, 0o777)
    opened = SocaServer._open_upload_folders

    # the first file made run/; before the second, run/ is swapped for a link elsewhere
    calls = []

    def swap_on_second(self, helper_, dir_fd, resolved, folders, made):
        calls.append(folders)
        if len(calls) == 2:
            helper_.run_as_user(
                lambda: (
                    os.rename(os.path.join(path, 'run'), os.path.join(path, 'moved')),
                    os.symlink(str(outside), os.path.join(path, 'run')),
                )
            )
        return opened(self, helper_, dir_fd, resolved, folders, made)

    monkeypatch.setattr(SocaServer, '_open_upload_folders', swap_on_second)
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server,
        path,
        stream_of(folder_upload_body(('run/a.dat', b'a'), ('run/b.dat', b'b'))),
    )
    assert status == 403
    assert list(outside.iterdir()) == []
    assert open(os.path.join(path, 'moved', 'a.dat')).read() == 'a'


def test_a_folder_upload_cut_short_removes_only_what_it_made_and_left_empty(
    numeric_tree,
):
    from ideasdk.server.soca_server import SocaServer

    path, helper = numeric_tree
    helper.run_as_user(lambda: os.mkdir(os.path.join(path, 'kept-empty')))
    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._executor = server_pool()
    body = folder_upload_body(('run/done/a.dat', b'a'))[: -len(b'--b--\r\n')]
    body += (
        b'--b\r\nContent-Disposition: form-data; name="files[]"; '
        b'filename="run/half/deeper/b.dat"\r\n\r\n' + b'x' * 100000
    )

    def once_writing():
        import time

        deadline = time.monotonic() + 10
        target = os.path.join(path, 'run', 'half', 'deeper', 'b.dat')
        while not (os.path.exists(target) and os.path.getsize(target)):
            assert time.monotonic() < deadline
            time.sleep(0.02)

    cancelled_upload(server, helper, path, body, once_writing)

    # the finished file and its folder stay; the folders made only for the cut file go;
    # a folder this upload did not make stays even though it is empty
    assert open(os.path.join(path, 'run', 'done', 'a.dat')).read() == 'a'
    assert not os.path.exists(os.path.join(path, 'run', 'half'))
    assert sorted(os.listdir(path)) == ['kept-empty', 'run']
    assert sorted(os.listdir(os.path.join(path, 'run'))) == ['done']


def test_a_refused_selection_is_not_walked_first(numeric_tree, monkeypatch):
    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    marker = os.path.join(path, 'walked')

    def record(self, directory):
        # in the user's worker, a child process: what it saw is left in a file
        open(marker, 'a').write(directory + '\n')
        return 0

    monkeypatch.setattr(FileSystemHelper, '_calculate_directory_size', record)
    with pytest.raises(exceptions.SocaException):
        helper.download_files(DownloadFilesRequest(files=[folder, '/usr']))
    assert not os.path.exists(marker)


def test_instance_metadata_calls_cannot_wait_forever(monkeypatch):
    import requests
    from ideasdk.aws.instance_metadata_util import InstanceMetadataUtil

    timeouts = []

    def record(*args, **kwargs):
        timeouts.append(kwargs.get('timeout'))
        return SimpleNamespace(status_code=200, text='token')

    monkeypatch.setattr(requests, 'put', record)
    monkeypatch.setattr(requests, 'get', record)
    util = InstanceMetadataUtil()
    util.get_imds_auth_token()
    try:
        util.get_iam_security_credentials()
    except Exception:
        pass  # the answer is not real; only how long the call may wait matters
    assert timeouts and all(t is not None for t in timeouts)


def test_a_save_with_no_room_for_its_copy_is_refused_and_keeps_the_file(
    numeric_tree, monkeypatch
):
    import errno
    import tempfile

    path, helper = numeric_tree
    note = os.path.join(path, 'note.txt')
    helper.run_as_user(lambda: open(note, 'w').write('a long original text'))

    def no_room(*args, **kwargs):
        raise OSError(errno.EDQUOT, 'Disk quota exceeded')

    # no room for a second copy, as for someone at their quota: shorter content too
    monkeypatch.setattr(tempfile, 'mkstemp', no_room)
    with pytest.raises(exceptions.SocaException) as refused:
        helper.save_file(
            SaveFileRequest(file=note, content=Utils.base64_encode('short'))
        )
    assert refused.value.error_code == errorcodes.FILE_QUOTA_EXCEEDED
    assert open(note).read() == 'a long original text'


def test_a_save_written_in_place_never_empties_the_file_when_it_fails(
    numeric_tree, monkeypatch
):
    import builtins
    import errno
    import tempfile
    from ideasdk.filesystem import filesystem_helper

    path, helper = numeric_tree
    note = os.path.join(path, 'note.txt')
    helper.run_as_user(lambda: open(note, 'w').write('the original'))

    def refused(*args, **kwargs):
        raise OSError(errno.EACCES, 'Permission denied')

    # no copy can be made, so the save is written over in place; that write then fails
    monkeypatch.setattr(tempfile, 'mkstemp', refused)
    real_open = builtins.open

    class FailingWrite:
        def __init__(self, real):
            self.real = real

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.real.close()
            return False

        def write(self, _):
            raise OSError(errno.EIO, 'Input/output error')

    def open_failing(file, mode='r', *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        same = isinstance(file, str) and os.path.realpath(file) == os.path.realpath(
            note
        )
        return FailingWrite(handle) if same and mode != 'r' else handle

    monkeypatch.setattr(filesystem_helper, 'open', open_failing, raising=False)
    with pytest.raises(Exception):
        helper.save_file(SaveFileRequest(file=note, content=Utils.base64_encode('new')))
    monkeypatch.undo()
    assert open(note).read() == 'the original'


def test_a_file_whose_name_is_near_the_limit_still_saves(numeric_tree):
    path, helper = numeric_tree
    long_name = os.path.join(path, 'n' * 250 + '.txt')
    helper.run_as_user(lambda: open(long_name, 'w').write('old'))
    before = os.stat(long_name).st_ino
    helper.save_file(
        SaveFileRequest(file=long_name, content=Utils.base64_encode('new'))
    )
    assert open(long_name).read() == 'new'
    assert os.stat(long_name).st_ino != before  # replaced, not written in place
    assert sorted(os.listdir(path)) == [os.path.basename(long_name)]


def test_a_save_that_cannot_make_its_copy_is_written_in_place(
    numeric_tree, monkeypatch
):
    import errno
    import tempfile

    path, helper = numeric_tree
    note = os.path.join(path, 'note.txt')
    helper.run_as_user(lambda: open(note, 'w').write('old'))

    def refused(*args, **kwargs):
        raise OSError(errno.EACCES, 'Permission denied')

    monkeypatch.setattr(tempfile, 'mkstemp', refused)
    helper.save_file(SaveFileRequest(file=note, content=Utils.base64_encode('new')))
    assert open(note).read() == 'new'


def test_a_file_in_a_group_the_account_lacks_is_saved_in_place(numeric_tree):
    if os.geteuid() != 0:
        pytest.skip('needs root to give a file a group the user is not in')
    path, helper = numeric_tree
    note = os.path.join(path, 'note.txt')
    helper.run_as_user(lambda: open(note, 'w').write('old'))
    user = UserIdentity(helper.context.accounts, 'no-passwd-entry')
    os.chown(note, user.uid, 4243)
    os.chmod(note, 0o664)
    before = os.stat(note)
    helper.save_file(SaveFileRequest(file=note, content=Utils.base64_encode('new')))
    after = os.stat(note)
    assert open(note).read() == 'new'
    assert (after.st_ino, after.st_gid) == (before.st_ino, 4243)


def test_a_folder_upload_checks_each_folder_once_not_once_per_file(
    numeric_tree, monkeypatch
):
    path, helper = numeric_tree
    forks = []
    run = UserIdentity.run

    def counting(self, operation, *args, **kwargs):
        if not self.in_worker():
            forks.append(operation)
        return run(self, operation, *args, **kwargs)

    monkeypatch.setattr(UserIdentity, 'run', counting)
    server = download_server('no-passwd-entry', helper.context)
    files = [(f'run/inputs/f{i}.dat', b'x') for i in range(50)]
    status, answer = upload_route(server, path, stream_of(folder_upload_body(*files)))
    assert status == 200 and len(answer['payload']['files_uploaded']) == 50
    # the destination and two folders, each made and checked once: not 3 checks per file
    assert len(forks) <= 8


def test_an_api_client_deletes_a_folder_only_when_it_says_so(numeric_tree):
    from ideasdk.filesystem.filebrowser_api import FileBrowserAPI

    path, helper = numeric_tree
    folder = os.path.join(path, 'results')
    helper.run_as_user(
        lambda: (os.mkdir(folder), open(os.path.join(folder, 'run.dat'), 'w').close())
    )
    api = FileBrowserAPI(helper.context)

    def call(payload):
        context = Mock()
        context.get_username.return_value = 'no-passwd-entry'
        context.get_request_payload_as.side_effect = lambda model: model(**payload)
        api.delete_files(context)
        return context

    with pytest.raises(exceptions.SocaException) as refused:
        call({'files': [folder]})
    assert 'is a folder' in refused.value.message
    assert os.path.isdir(folder)
    call({'files': [folder], 'include_folders': True})
    assert not os.path.exists(folder)


def test_a_plain_name_with_a_backslash_is_saved_cleaned_as_before(numeric_tree):
    path, helper = numeric_tree
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server, path, stream_of(folder_upload_body(('a\\b.dat', b'x')))
    )
    assert status == 200 and answer['payload']['files_skipped'] == []
    assert sorted(os.listdir(path)) == ['ab.dat']


def test_a_destination_replaced_during_an_upload_is_checked_again(
    numeric_tree, monkeypatch
):
    from ideasdk.server.soca_server import SocaServer

    if os.geteuid() != 0:
        pytest.skip('needs root: the server creates the files, and the folder put in')
    path, helper = numeric_tree
    destination = os.path.join(path, 'dest')
    helper.run_as_user(lambda: os.mkdir(destination))
    create = SocaServer._create_upload
    created = []

    def create_then_swap(self, *args, **kwargs):
        result = create(self, *args, **kwargs)
        created.append(result)
        if len(created) == 1:
            # after the first file, the destination becomes a folder the user cannot write
            os.rename(destination, destination + '.moved')
            os.mkdir(destination, 0o755)
        return result

    monkeypatch.setattr(SocaServer, '_create_upload', create_then_swap)
    server = download_server('no-passwd-entry', helper.context)
    status, answer = upload_route(
        server,
        destination,
        stream_of(folder_upload_body(('a.dat', b'a'), ('b.dat', b'b'))),
    )
    assert status == 403
    assert os.listdir(destination) == []
    assert open(os.path.join(destination + '.moved', 'a.dat')).read() == 'a'


def files_only_delete_with_a_swap(numeric_tree, monkeypatch, hook):
    """the entry is a file when it is first looked at, a folder with work in it just after"""
    from ideasdk.utils import Utils as utils

    path, helper = numeric_tree
    target = os.path.join(path, 'results')
    helper.run_as_user(lambda: open(target, 'w').close())
    # the delete runs in the user's worker, a child process: the swap leaves a marker
    marker = os.path.join(path, 'swapped')

    def swap_after_first_look():
        if not os.path.exists(marker):
            open(marker, 'w').close()
            os.remove(target)
            os.mkdir(target)
            open(os.path.join(target, 'work.dat'), 'w').write('work')

    if hook == 'is_dir':
        real = utils.is_dir

        def is_dir(file):
            answer = real(file)
            if file == target:
                swap_after_first_look()
            return answer

        monkeypatch.setattr(utils, 'is_dir', is_dir)
    else:
        real_lstat = os.lstat

        def lstat(file, *args, **kwargs):
            answer = real_lstat(file, *args, **kwargs)
            if file == target:
                swap_after_first_look()
            return answer

        monkeypatch.setattr(os, 'lstat', lstat)
    try:
        helper.delete_files(DeleteFilesRequest(files=[target]))
    except exceptions.SocaException:
        pass
    monkeypatch.undo()
    # a folder that took the file's place keeps its work; with no swap, the file is gone
    if os.path.exists(marker):
        assert open(os.path.join(target, 'work.dat')).read() == 'work'
    else:
        assert not os.path.lexists(target)


def test_the_files_only_delete_asks_the_type_once(numeric_tree, monkeypatch):
    # the type was asked twice: a change between the two reached the recursive delete
    files_only_delete_with_a_swap(numeric_tree, monkeypatch, 'is_dir')


def test_a_change_after_the_files_only_look_removes_no_folder(
    numeric_tree, monkeypatch
):
    # decided by one lstat: a folder there after it is refused by the file removal
    files_only_delete_with_a_swap(numeric_tree, monkeypatch, 'lstat')


def test_a_link_into_a_system_folder_can_be_deleted_and_renamed(numeric_tree):
    path, helper = numeric_tree
    link = os.path.join(path, 'to-usr')
    helper.run_as_user(lambda: os.symlink('/usr/share', link))
    helper.rename_file(RenameFileRequest(file=link, new_name='renamed'))
    renamed = os.path.join(path, 'renamed')
    assert os.path.islink(renamed)
    helper.delete_files(DeleteFilesRequest(files=[renamed]))
    assert not os.path.lexists(renamed)
    assert os.path.isdir('/usr/share')
    # never followed for content
    with pytest.raises(exceptions.SocaException):
        helper.run_as_user(lambda: os.symlink('/usr/share', link))
        helper.check_access(os.path.join(link, 'x'), check_read=True, check_write=False)


def test_an_archive_leaves_out_links_into_system_folders(numeric_tree, monkeypatch):
    import zipfile

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    readable = next(
        os.path.join(root, name)
        for root, _, names in os.walk('/usr/share')
        for name in sorted(names)
        if os.path.isfile(os.path.join(root, name))
        and not os.path.islink(os.path.join(root, name))
        and os.access(os.path.join(root, name), os.R_OK)
    )
    helper.run_as_user(lambda: os.symlink(readable, os.path.join(folder, 'system')))
    archive, left_out = helper.create_download_archive(
        DownloadFilesRequest(files=[folder])
    )
    names = zipfile.ZipFile(archive).namelist()
    assert 'run/ok.dat' in names and 'run/system' not in names
    assert left_out == 1


def test_two_names_that_clean_to_one_stay_two_members(numeric_tree, monkeypatch):
    import zipfile

    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    helper.run_as_user(
        lambda: (
            open(os.path.join(folder, 'a\\b'), 'w').write('one'),
            open(os.path.join(folder, 'a_b'), 'w').write('two'),
        )
    )
    archive = helper.download_files(DownloadFilesRequest(files=[folder]))
    members = zipfile.ZipFile(archive)
    names = [n for n in members.namelist() if n.startswith('run/a_b')]
    assert sorted(names) == ['run/a_b', 'run/a_b_1']
    assert sorted(members.read(n) for n in names) == [b'one', b'two']


def test_an_existing_downloads_folder_is_made_private(numeric_tree, monkeypatch):
    path, helper, folder = archive_tree(numeric_tree, monkeypatch)
    downloads = os.path.join(path, 'idea_downloads')
    helper.run_as_user(lambda: (os.mkdir(downloads), os.chmod(downloads, 0o755)))
    helper.download_files(DownloadFilesRequest(files=[folder]))
    assert oct(os.stat(downloads).st_mode & 0o777) == oct(0o700)


@pytest.mark.parametrize('default_acl', [False, True], ids=['no-acl', 'default-acl'])
def test_a_save_in_a_set_group_id_folder_keeps_group_mode_and_no_acl(
    numeric_tree, default_acl
):
    import shutil as shell_tools
    import subprocess

    if os.geteuid() != 0:
        pytest.skip('needs root: a folder of another group, with the set-group-ID bit')
    if default_acl and shell_tools.which('setfacl') is None:
        pytest.skip('setfacl is not installed')
    path, helper = numeric_tree
    user = UserIdentity(helper.context.accounts, 'no-passwd-entry')
    project = os.path.join(path, 'project')
    os.mkdir(project)
    os.chown(project, user.uid, 4242)
    os.chmod(project, 0o2777)
    note = os.path.join(project, 'private.txt')
    helper.run_as_user(lambda: (open(note, 'w').write('old'), os.chmod(note, 0o640)))
    os.chown(note, user.uid, user.gid)  # moved in with its own group
    acl = default_acl
    if acl:
        # the folder gives new files the project group's access by default
        subprocess.run(['setfacl', '-d', '-m', 'g:4242:rwx', project], check=True)

    helper.save_file(SaveFileRequest(file=note, content=Utils.base64_encode('new')))

    info = os.stat(note)
    assert open(note).read() == 'new'
    assert info.st_gid == user.gid
    assert oct(info.st_mode & 0o7777) == oct(0o640)
    if acl:
        assert not [n for n in os.listxattr(note) if n.startswith('system.posix_acl')]


def test_an_upload_cancelled_during_its_last_close_closes_its_folder_once(
    numeric_tree, monkeypatch
):
    import asyncio
    import threading
    from ideasdk.server import soca_server

    path, helper = numeric_tree
    server = object.__new__(soca_server.SocaServer)
    server._logger = Mock()
    server._executor = server_pool()

    folders = []
    made = soca_server._Upload.__init__

    def remember(self, file, file_path, dir_fd, dev, ino):
        folders.append(dir_fd)
        made(self, file, file_path, dir_fd, dev, ino)

    in_finish, release = threading.Event(), threading.Event()
    finish = soca_server._Upload.finish

    def held_finish(self):
        in_finish.set()
        release.wait(10)
        finish(self)

    closes = []
    real_close = os.close

    def counting_close(fd):
        closes.append(fd)
        real_close(fd)

    monkeypatch.setattr(soca_server._Upload, '__init__', remember)
    monkeypatch.setattr(soca_server._Upload, 'finish', held_finish)
    body = UPLOAD_HEADERS + b'x' * 100 + b'\r\n--b'

    class Stream:
        sent = [body]

        async def read(self):
            if self.sent:
                return self.sent.pop()
            await asyncio.Event().wait()

    request = SimpleNamespace(
        headers={'content-type': 'multipart/form-data; boundary=b'}, stream=Stream()
    )

    async def scenario():
        task = asyncio.ensure_future(
            server._process_streaming_upload(request, helper, path, 'no-passwd-entry')
        )
        await asyncio.to_thread(in_finish.wait, 10)
        monkeypatch.setattr(os, 'close', counting_close)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        release.set()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        release.set()
        loop.close()
        server._executor.shutdown(wait=True)
        monkeypatch.setattr(os, 'close', real_close)

    assert len(folders) == 1
    assert closes.count(folders[0]) == 1
    assert os.listdir(path) == []
