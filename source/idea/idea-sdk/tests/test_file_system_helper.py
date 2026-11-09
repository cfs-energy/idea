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

    def child_open(*args, **kwargs):
        assert os.getpid() != parent_pid
        assert os.geteuid() == helper._identity.uid
        return original_open(*args, **kwargs)

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
    monkeypatch.setattr(filesystem_helper, '_active_tail_users', set())
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
    fd, file_path, renamed = server._create_upload(
        helper, os.path.join(path, 'link'), 'data.dat', 'user'
    )
    os.close(fd)
    assert file_path == os.path.join(path, 'link', 'data.dat') and not renamed
    assert os.path.isfile(os.path.join(real, 'data.dat'))
    assert os.stat(os.path.join(real, 'data.dat')).st_uid == helper._identity.uid


def test_tail_keeps_the_indentation_of_a_line(numeric_tree, tmp_path):
    log = tmp_path / 'run.log'
    log.write_text('Traceback:\n  File "a.py", line 3\n')
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
    server._context = helper_context
    server._api_invocation_handler = Mock()
    server._api_invocation_handler.get_username.return_value = username
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
                archive,
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
    server._api_invocation_handler = Mock()
    server._api_invocation_handler.get_username.return_value = None
    response = run_generate_download_url(server, Mock(json={'file': '/data/x'}))
    assert response.status == 401
    assert Utils.from_json(response.body)['error_code'] == errorcodes.UNAUTHORIZED_ACCESS
    server._logger.error.assert_not_called()


def test_a_download_link_request_that_fails_before_its_body_is_read_still_answers():
    from ideasdk.server.soca_server import SocaServer

    server = object.__new__(SocaServer)
    server._logger = Mock()
    server._api_invocation_handler = Mock()
    server._api_invocation_handler.get_username.side_effect = RuntimeError('boom')
    response = run_generate_download_url(server, Mock(json={'file': '/data/x'}))
    assert Utils.from_json(response.body)['error_code'] == errorcodes.GENERAL_ERROR
