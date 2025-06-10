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
from ideasdk.shell import ShellInvocationResult
from ideadatamodel import exceptions, errorcodes, ListFilesRequest, ReadFileRequest

import pytest
import os
from subprocess import CompletedProcess
import datetime
from unittest.mock import MagicMock


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


class MockShellInvoker:
    def __init__(self, test_case: str):
        self.test_case = test_case

    def list_files(self):
        return ShellInvocationResult(
            command='',
            result=CompletedProcess(
                args='ls',
                returncode=0,
                stdout="""apps
data
mnt""",
                stderr=None,
            ),
            total_time_ms=1,
        )

    def invoke(self, *args, **_) -> ShellInvocationResult:
        command = args[0]

        if command[3].startswith('test'):
            return ShellInvocationResult(
                command='',
                result=CompletedProcess(
                    args='test', returncode=0, stdout='', stderr=None
                ),
                total_time_ms=10,
            )
        elif self.test_case == 'list-root-dir':
            return self.list_files()


@pytest.fixture()
def file_system_helper(request, context, monkeypatch):
    username = 'mockuser'

    stat = os.stat_result((0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(os, 'stat', lambda *_, **__: stat)

    helper = FileSystemHelper(
        context=context,
        username=username,
    )
    helper.shell = MockShellInvoker(request.param[0])
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
