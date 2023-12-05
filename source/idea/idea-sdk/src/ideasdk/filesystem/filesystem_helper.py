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
import logging

from ideadatamodel.filesystem import (
    ListFilesRequest,
    ListFilesResult,
    ReadFileRequest,
    ReadFileResult,
    SaveFileRequest,
    SaveFileResult,
    DownloadFilesRequest,
    CreateFileRequest,
    CreateFileResult,
    DeleteFilesRequest,
    DeleteFilesResult,
    TailFileRequest,
    TailFileResult,
    FileData
)
from ideadatamodel import exceptions, errorcodes
from ideasdk.utils import Utils, GroupNameHelper
from ideasdk.protocols import SocaContextProtocol
from ideasdk.shell import ShellInvoker

import os
import arrow
import mimetypes
import shutil
import aiofiles
from typing import Dict, List, Any
from zipfile import ZipFile
from collections import deque

# default lines to prefetch on an initial tail request.
TAIL_FILE_MAX_LINE_COUNT = 10000
# default lines to prefetch on an initial tail request.
TAIL_FILE_DEFAULT_LINE_COUNT = 1000
# min interval during subsequent tail requests. requests less than this interval will return empty lines
TAIL_FILE_MIN_INTERVAL_SECONDS = 5

RESTRICTED_ROOT_FOLDERS = [
    'boot',
    'bin',
    'dev',
    'etc',
    'home',
    'local',
    'lib',
    'lib64',
    'media',
    'opt',
    'proc',
    'root',
    'run',
    'srv',
    'sys',
    'sbin',
    'tmp',
    'usr',
    'var'
]


class FileSystemHelper:
    """
    File System Helper
    Used for supporting web based file browser APIs
    """

    def __init__(self, context: SocaContextProtocol, username: str):
        self.context = context
        self.logger = context.logger('file-system-api')

        if Utils.is_empty(username):
            raise exceptions.invalid_params('username is required')
        self.username = username
        self.shell = ShellInvoker(logger=self.logger)
        self.group_name_helper = GroupNameHelper(context)

    def get_user_home(self) -> str:
        return os.path.join(self.context.config().get_string('shared-storage.data.mount_dir', required=True), 'home', self.username)

    def has_access(self, file: str) -> bool:
        try:
            user_home = self.get_user_home()
            if not file.startswith(user_home):
                raise exceptions.unauthorized_access()
            if '..' in file:
                raise exceptions.unauthorized_access()
            if Utils.is_empty(file):
                raise exceptions.invalid_params('file is required')
            if not Utils.is_file(file):
                raise exceptions.file_not_found(file)
            return True
        except:  # noqa
            return False

    def check_access(self, file: str, check_dir=False, check_read=True, check_write=True):
        if Utils.is_empty(file):
            raise exceptions.unauthorized_access()
        # only support absolute paths
        if '..' in file:
            raise exceptions.unauthorized_access()

        data_mount_dir = self.context.config().get_string('shared-storage.data.mount_dir')
        is_data_mount = Utils.is_not_empty(data_mount_dir) and file.startswith(data_mount_dir)

        # do not allow any access to system folders
        tokens = file.split('/')
        if len(tokens) > 1 and tokens[1] in RESTRICTED_ROOT_FOLDERS and not is_data_mount:
            raise exceptions.unauthorized_access()

        if check_dir:
            is_dir = self.shell.invoke(['su', self.username, '-c', f'test -d "{file}"'])
            if is_dir.returncode != 0:
                raise exceptions.unauthorized_access()
        if check_read:
            can_read = self.shell.invoke(['su', self.username, '-c', f'test -r "{file}"'])
            if can_read.returncode != 0:
                raise exceptions.unauthorized_access()
        if check_write:
            can_write = self.shell.invoke(['su', self.username, '-c', f'test -w "{file}"'])
            if can_write.returncode != 0:
                raise exceptions.unauthorized_access()

    def list_files(self, request: ListFilesRequest) -> ListFilesResult:
        cwd = request.cwd


        if Utils.is_empty(cwd):
            cwd = self.get_user_home()

        self.logger.debug(f"list_files() for CWD ({cwd})")

        self.check_access(cwd, check_dir=True, check_read=True, check_write=False)

        # This is specifically _after_ the check_access so its timing doesnt impact this timing
        if self.logger.isEnabledFor(logging.DEBUG):
            listing_start = Utils.current_time_ms()

        result = []
        with os.scandir(cwd) as scandir:
            for entry in scandir:

                if Utils.is_empty(entry):
                    self.logger.debug(f"Empty Entry found at cwd ({cwd}) Name: ({entry.name})")
                    continue

                if entry.is_file():
                    # Check for restricted files/dirs and do not list them
                    if cwd == '/' and entry.name in RESTRICTED_ROOT_FOLDERS:
                        # This is only logged at debug since simply browsing to a directory that has
                        # restricted folders isn't a sign of problems
                        self.logger.debug(f"Listing denied for RESTRICTED_ROOT_FOLDERS: cwd ({cwd})  Name: ({entry.name})")
                        continue

                is_hidden = entry.name.startswith('.')
                file_size = None if entry.is_dir() else entry.stat().st_size
                mod_date = arrow.get(entry.stat().st_mtime).datetime

                result.append(FileData(
                    file_id=Utils.shake_256(f'{entry.name}{entry.is_dir()}', 5),
                    name=entry.name,
                    size=file_size,
                    mod_date=mod_date,
                    is_dir=entry.is_dir(),
                    is_hidden=is_hidden
                ))

        if self.logger.isEnabledFor(logging.DEBUG):
            listing_end = Utils.current_time_ms()
            self.logger.debug(f"Directory Listing Timing: CWD ({cwd}), Len: {len(result)}, Time taken: {listing_end - listing_start} ms")  # noqa: listing_start is only set if logging.DEBUG

        return ListFilesResult(
            cwd=cwd,
            listing=result
        )

    def read_file(self, request: ReadFileRequest) -> ReadFileResult:
        file = request.file

        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)

        self.check_access(file, check_dir=False, check_read=True, check_write=False)

        if file.endswith('.que'):
            content_type = 'text/plain'
        else:
            content_type, encoding = mimetypes.guess_type(file)

        if Utils.is_binary_file(file):
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_NOT_A_TEXT_FILE,
                message='file is not a text file. download the binary file instead'
            )

        with open(file, 'r') as f:
            content = f.read()

        return ReadFileResult(
            file=file,
            content_type=content_type,
            content=Utils.base64_encode(content)
        )

    def tail_file(self, request: TailFileRequest) -> TailFileResult:
        file = request.file

        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)

        self.check_access(file, check_dir=False, check_read=True, check_write=False)

        if Utils.is_binary_file(file):
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_NOT_A_TEXT_FILE,
                message='file is not a text file. download the binary file instead.'
            )

        next_token = request.next_token

        lines = []
        line_count = Utils.get_as_int(request.line_count, TAIL_FILE_DEFAULT_LINE_COUNT)
        max_line_count = min(line_count, TAIL_FILE_MAX_LINE_COUNT)

        if Utils.is_not_empty(next_token):
            cursor_tokens = Utils.base64_decode(next_token).split(';')
            last_read = Utils.get_as_int(cursor_tokens[1])
            now = Utils.current_time_ms()

            if (now - last_read) < TAIL_FILE_MIN_INTERVAL_SECONDS * 1000:
                raise exceptions.soca_exception(error_code=errorcodes.FILE_BROWSER_TAIL_THROTTLE, message=f'tail file request throttled. subsequent requests should be called at {TAIL_FILE_MIN_INTERVAL_SECONDS} seconds frequency')

            file_handle = None
            try:
                file_handle = open(file, 'r')
                offset = Utils.get_as_int(cursor_tokens[0])
                file_handle.seek(offset)

                while len(lines) < max_line_count:
                    line = file_handle.readline()
                    if line == '':
                        break
                    lines.append(line)

                cursor_tokens = [file_handle.tell(), Utils.current_time_ms()]
            finally:
                if file_handle is not None:
                    file_handle.close()
        else:
            # if cursor file does not exist, prefetch last N lines
            with open(file, 'r') as f:
                prefetch_lines = list(deque(f, max_line_count))
                for line in prefetch_lines:
                    lines.append(line.strip())
                # seek to end of file and update cursor
                f.seek(0, os.SEEK_END)
                cursor_tokens = [f.tell(), Utils.current_time_ms()]

        if cursor_tokens is not None:
            next_token = Utils.base64_encode(f'{cursor_tokens[0]};{cursor_tokens[1]}')

        return TailFileResult(
            file=file,
            next_token=next_token,
            lines=lines,
            line_count=len(lines)
        )

    def save_file(self, request: SaveFileRequest) -> SaveFileResult:
        file = request.file
        if Utils.is_empty(file):
            raise exceptions.invalid_params('file is required')
        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)
        if Utils.is_binary_file(file):
            raise exceptions.invalid_params('file is not a text file. upload the binary file instead')

        self.check_access(file, check_dir=False, check_read=True, check_write=True)

        content_base64 = request.content
        content = Utils.base64_decode(content_base64)

        with open(file, 'w') as f:
            f.write(content)

        return SaveFileResult()

    async def upload_files(self, cwd: str, files: List[Any]) -> Dict:
        """
        called from SocaServer to handle file upload routes
        :param cwd
        :param files:
        :return:
        """

        if Utils.is_empty(files):
            return {
                'success': False
            }

        user_home = self.get_user_home()
        if Utils.is_empty(cwd):
            raise exceptions.unauthorized_access()
        if not Utils.is_dir(cwd):
            raise exceptions.unauthorized_access()
        if not cwd.startswith(user_home):
            raise exceptions.unauthorized_access()
        if '..' in cwd:
            raise exceptions.unauthorized_access()

        files_uploaded = []
        files_skipped = []
        for file in files:
            secure_file_name = Utils.to_secure_filename(file.name)
            if Utils.is_empty(secure_file_name):
                files_skipped.append(file.name)
                continue
            file_path = os.path.join(cwd, secure_file_name)
            # use aiofiles to not block the event loop
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(file.body)

            group_name = self.group_name_helper.get_user_group(self.username)
            shutil.chown(file_path, user=self.username, group=group_name)
            files_uploaded.append(file_path)

        return {
            'success': True,
            'payload': {
                'files_uploaded': files_uploaded,
                'files_skipped': files_skipped
            }
        }

    def download_files(self, request: DownloadFilesRequest) -> str:
        files = Utils.get_as_list(request.files, [])
        if Utils.is_empty(files):
            raise exceptions.invalid_params('file is required')

        download_list = []
        for file in files:
            if Utils.is_empty(file):
                continue
            if '..' in file:
                raise exceptions.unauthorized_access()
            if Utils.is_empty(file):
                raise exceptions.invalid_params('file is required')
            if not Utils.is_file(file):
                raise exceptions.file_not_found(file)
            self.check_access(file, check_dir=False, check_read=True, check_write=False)
            download_list.append(file)

        group_name = self.group_name_helper.get_user_group(self.username)
        downloads_dir = os.path.join(self.get_user_home(), 'idea_downloads')
        if Utils.is_symlink(downloads_dir):
            # prevent symlink/hijack attacks
            raise exceptions.invalid_params(f'a symbolic link already exists at: {downloads_dir}')

        os.makedirs(downloads_dir, exist_ok=True)
        shutil.chown(downloads_dir, user=self.username, group=group_name)

        short_uuid = Utils.short_uuid()
        zip_file_path = os.path.join(downloads_dir, f'{short_uuid}.zip')
        with ZipFile(zip_file_path, 'w') as zipfile:
            for download_file in download_list:
                zipfile.write(download_file)

        shutil.chown(zip_file_path, user=self.username, group=group_name)
        return zip_file_path

    def create_file(self, request: CreateFileRequest) -> CreateFileResult:

        cwd = request.cwd

        filename = request.filename
        if Utils.is_empty(filename):
            raise exceptions.invalid_params('filename is required')

        self.check_access(cwd, check_dir=True, check_read=True, check_write=True)

        original_filename = filename

        # ensure file name is the leaf name and not directory
        filename = os.path.basename(filename)
        if original_filename != filename:
            raise exceptions.invalid_params(f'invalid name: {original_filename}, name cannot contain "/" or special characters')

        # ensure file name is ascii characters and nothing funky going on in file name
        filename = Utils.to_secure_filename(filename)
        if original_filename != filename:
            raise exceptions.invalid_params(f'invalid characters in name: {original_filename}')

        create_path = os.path.join(cwd, filename)
        if Utils.is_symlink(create_path):
            raise exceptions.invalid_params(f'a symbolic link already exists at: {create_path}')

        is_folder = Utils.get_as_bool(request.is_folder, False)
        if is_folder:
            if Utils.is_dir(create_path):
                raise exceptions.invalid_params(f'directory: {filename} already exists under: {cwd}')
            os.makedirs(create_path)
        else:
            if Utils.is_file(create_path):
                raise exceptions.invalid_params(f'file: {filename} already exists under: {cwd}')
            with open(create_path, 'w') as f:
                f.write('')

        group_name = self.group_name_helper.get_user_group(self.username)
        shutil.chown(create_path, self.username, group_name)

        return CreateFileResult()

    def delete_files(self, request: DeleteFilesRequest) -> DeleteFilesResult:
        files = request.files
        if Utils.is_empty(files):
            raise exceptions.invalid_params('files[] is required')

        directories = []
        regular_files = []
        symlinks = []

        for file in files:

            self.check_access(file, check_read=False, check_write=True)

            if Utils.is_dir(file):
                directories.append(file)
            elif os.path.islink(file):
                symlinks.append(file)
            else:
                regular_files.append(file)

        for file in regular_files:
            os.remove(file)
            self.logger.info(f'{self.username} deleted file: "{file}"')

        for link in symlinks:
            os.unlink(link)
            self.logger.info(f'{self.username} deleted symlink: "{link}"')

        for directory in directories:
            shutil.rmtree(directory, ignore_errors=True)
            self.logger.info(f'{self.username} deleted directory: "{directory}"')

        return DeleteFilesResult()
