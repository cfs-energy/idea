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
from ideadatamodel import exceptions, errorcodes, ListFilesRequest, ReadFileRequest

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
    DeleteFilesRequest,
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
