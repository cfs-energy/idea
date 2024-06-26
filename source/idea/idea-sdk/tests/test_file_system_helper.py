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
from ideasdk.shell import ShellInvoker, ShellInvocationResult
from ideadatamodel import (
    exceptions,
    errorcodes,
    ListFilesRequest,
    ReadFileRequest
)

import pytest
import os
from subprocess import CompletedProcess


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
bin
boot
data
dev
etc
home
lib
lib64
local
media
mnt
opt
proc
root
run
sbin
srv
sys
tmp
usr
var""",
                stderr=None
            ),
            total_time_ms=1
        )

    def invoke(self, *args, **_) -> ShellInvocationResult:
        command = args[0]

        if command[3].startswith('test'):
            return ShellInvocationResult(
                command='',
                result=CompletedProcess(args='test', returncode=0, stdout='', stderr=None),
                total_time_ms=10
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
def test_file_browser_list_files_root_directory(context, file_system_helper, monkeypatch):
    """
    list files in root directory and ensure only apps and data directories can be listed
    """
    result = file_system_helper.list_files(ListFilesRequest(
        cwd='/'
    ))

    for file_data in result.listing:
        assert file_data.name in ('mnt', 'apps', 'data')


@pytest.mark.parametrize('file_system_helper', [['read-restricted-file']], indirect=True)
def test_file_browser_read_file_restricted_access(context, file_system_helper, monkeypatch):
    """
    try to read a file in restricted directories and unauthorized access exception should be thrown
    """

    monkeypatch.setattr(Utils, 'is_file', lambda *_: True)
    monkeypatch.setattr(Utils, 'is_binary_file', lambda *_: False)

    with pytest.raises(exceptions.SocaException) as exc_info:
        file_system_helper.read_file(ReadFileRequest(
            file='/etc/shadow'
        ))
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
