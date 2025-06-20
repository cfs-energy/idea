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
    FileData,
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
import time
from typing import Dict, List, Any, Tuple
from zipfile import ZipFile

# default lines to prefetch on an initial tail request.
TAIL_FILE_MAX_LINE_COUNT = 10000
# default lines to prefetch on an initial tail request.
TAIL_FILE_DEFAULT_LINE_COUNT = 1000
# min interval during subsequent tail requests. requests less than this interval will return empty lines
TAIL_FILE_MIN_INTERVAL_SECONDS = 5

# Permission cache TTL in seconds
PERMISSION_CACHE_TTL = 60

# Rate limiting configuration
TAIL_FILE_RATE_LIMIT_WINDOW = 60  # 1 minute window
TAIL_FILE_MAX_REQUESTS_PER_WINDOW = 20  # Max 20 requests per minute per user
TAIL_FILE_MAX_CONCURRENT_USERS = 50  # Max 50 concurrent users using tail

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
    'var',
]

# Global rate limiting storage
_tail_rate_limits: Dict[str, List[float]] = {}
_active_tail_users: set = set()


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
        # Permission cache: {(file_path, username, check_type): (result, timestamp)}
        self._permission_cache: Dict[Tuple[str, str, str], Tuple[bool, float]] = {}
        self.group_name_helper = GroupNameHelper(context)

    def get_user_home(self) -> str:
        user_home = self.context.config().get_string(
            'shared-storage.data.mount_dir', default='/data'
        )
        if user_home.endswith('/'):
            user_home = user_home[:-1]
        user_home = f'{user_home}/home/{self.username}'
        return user_home

    def has_access(self, file: str) -> bool:
        try:
            # Check if the access has been cached
            cache_key = (file, self.username, 'access')
            current_time = time.time()

            if cache_key in self._permission_cache:
                result, timestamp = self._permission_cache[cache_key]
                if current_time - timestamp < PERMISSION_CACHE_TTL:
                    return result

            # Original access check logic
            user_home = self.get_user_home()
            if not file.startswith(user_home):
                result = False
            elif '..' in file:
                result = False
            elif Utils.is_empty(file):
                result = False
            elif not Utils.is_file(file):
                result = False
            else:
                result = True

            # Cache the result
            self._permission_cache[cache_key] = (result, current_time)
            return result
        except:  # noqa
            return False

    def _check_shell_permission(
        self, file: str, check_type: str, shell_cmd: List[str]
    ) -> bool:
        """Check permission using shell command with caching"""
        cache_key = (file, self.username, check_type)
        current_time = time.time()

        # Check cache first
        if cache_key in self._permission_cache:
            result, timestamp = self._permission_cache[cache_key]
            if current_time - timestamp < PERMISSION_CACHE_TTL:
                return result

        # Execute shell command
        result_obj = self.shell.invoke(shell_cmd)
        result = result_obj.returncode == 0

        # Cache the result
        self._permission_cache[cache_key] = (result, current_time)
        return result

    def check_access(
        self, file: str, check_dir=False, check_read=True, check_write=True
    ):
        if Utils.is_empty(file):
            raise exceptions.unauthorized_access()
        # only support absolute paths
        if '..' in file:
            raise exceptions.unauthorized_access()

        data_mount_dir = self.context.config().get_string(
            'shared-storage.data.mount_dir'
        )
        is_data_mount = Utils.is_not_empty(data_mount_dir) and file.startswith(
            data_mount_dir
        )

        # do not allow any access to system folders
        tokens = file.split('/')
        if (
            len(tokens) > 1
            and tokens[1] in RESTRICTED_ROOT_FOLDERS
            and not is_data_mount
        ):
            raise exceptions.unauthorized_access()

        if check_dir:
            is_dir = self._check_shell_permission(
                file, 'dir', ['su', self.username, '-c', f'test -d "{file}"']
            )
            if not is_dir:
                raise exceptions.unauthorized_access()
        if check_read:
            can_read = self._check_shell_permission(
                file, 'read', ['su', self.username, '-c', f'test -r "{file}"']
            )
            if not can_read:
                raise exceptions.unauthorized_access()
        if check_write:
            can_write = self._check_shell_permission(
                file, 'write', ['su', self.username, '-c', f'test -w "{file}"']
            )
            if not can_write:
                raise exceptions.unauthorized_access()

    def list_files(self, request: ListFilesRequest) -> ListFilesResult:
        cwd = request.cwd

        if Utils.is_empty(cwd):
            cwd = self.get_user_home()

        self.logger.debug(f'list_files() for CWD ({cwd})')

        self.check_access(cwd, check_dir=True, check_read=True, check_write=False)

        # This is specifically _after_ the check_access so its timing doesnt impact this timing
        if self.logger.isEnabledFor(logging.DEBUG):
            listing_start = Utils.current_time_ms()

        result = []
        with os.scandir(cwd) as scandir:
            for entry in scandir:
                if Utils.is_empty(entry):
                    self.logger.debug(
                        f'Empty Entry found at cwd ({cwd}) Name: ({entry.name})'
                    )
                    continue

                if entry.is_file():
                    # Check for restricted files/dirs and do not list them
                    if cwd == '/' and entry.name in RESTRICTED_ROOT_FOLDERS:
                        # This is only logged at debug since simply browsing to a directory that has
                        # restricted folders isn't a sign of problems
                        self.logger.debug(
                            f'Listing denied for RESTRICTED_ROOT_FOLDERS: cwd ({cwd})  Name: ({entry.name})'
                        )
                        continue

                is_hidden = entry.name.startswith('.')
                file_size = None if entry.is_dir() else entry.stat().st_size
                mod_date = arrow.get(entry.stat().st_mtime).datetime

                result.append(
                    FileData(
                        file_id=Utils.shake_256(f'{entry.name}{entry.is_dir()}', 5),
                        name=entry.name,
                        size=file_size,
                        mod_date=mod_date,
                        is_dir=entry.is_dir(),
                        is_hidden=is_hidden,
                    )
                )

        if self.logger.isEnabledFor(logging.DEBUG):
            listing_end = Utils.current_time_ms()
            self.logger.debug(
                f'Directory Listing Timing: CWD ({cwd}), Len: {len(result)}, Time taken: {listing_end - listing_start} ms'
            )  # listing_start is only set if logging.DEBUG

        return ListFilesResult(cwd=cwd, listing=result)

    def read_file(self, request: ReadFileRequest) -> ReadFileResult:
        file = request.file

        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)

        self.check_access(file, check_dir=False, check_read=True, check_write=False)

        if file.endswith('.que') or file.endswith('.ini'):
            content_type = 'text/plain'
        else:
            content_type, encoding = mimetypes.guess_type(file)

        if Utils.is_binary_file(file):
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_NOT_A_TEXT_FILE,
                message='file is not a text file. download the binary file instead',
            )

        with open(file, 'r') as f:
            content = f.read()

        return ReadFileResult(
            file=file, content_type=content_type, content=Utils.base64_encode(content)
        )

    def tail_file(self, request: TailFileRequest) -> TailFileResult:
        file = request.file
        start_time = Utils.current_time_ms()

        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)

        # Check rate limits before proceeding
        self._check_tail_rate_limit(self.username)

        self.check_access(file, check_dir=False, check_read=True, check_write=False)

        # Check file size and reject files that are too large for tail operations
        file_size = os.path.getsize(file)
        max_file_size = 10 * 1024 * 1024 * 1024  # 10GB limit
        if file_size > max_file_size:
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_NOT_A_TEXT_FILE,
                message=f'file is too large ({file_size / (1024 * 1024 * 1024):.2f} GB) for tail operations. Maximum supported size is {max_file_size / (1024 * 1024 * 1024):.0f} GB.',
            )

        if Utils.is_binary_file(file):
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_NOT_A_TEXT_FILE,
                message='file is not a text file. download the binary file instead.',
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
                raise exceptions.soca_exception(
                    error_code=errorcodes.FILE_BROWSER_TAIL_THROTTLE,
                    message=f'tail file request throttled. subsequent requests should be called at {TAIL_FILE_MIN_INTERVAL_SECONDS} seconds frequency',
                )

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
            # Use efficient tail algorithm that reads from end of file
            prefetch_lines = self._tail_file_efficiently(file, max_line_count)
            for line in prefetch_lines:
                lines.append(line.strip())

            # Get file size for cursor position
            with open(file, 'r') as f:
                f.seek(0, os.SEEK_END)
                cursor_tokens = [f.tell(), Utils.current_time_ms()]

        if cursor_tokens is not None:
            next_token = Utils.base64_encode(f'{cursor_tokens[0]};{cursor_tokens[1]}')

        # Log performance metrics
        end_time = Utils.current_time_ms()
        self.logger.info(
            f'TailFile performance - User: {self.username}, File: {file}, '
            f'Lines: {len(lines)}, Duration: {end_time - start_time}ms, '
            f'ActiveUsers: {len(_active_tail_users)}, IsInitial: {Utils.is_empty(request.next_token)}'
        )

        return TailFileResult(
            file=file, next_token=next_token, lines=lines, line_count=len(lines)
        )

    def _tail_file_efficiently(self, file_path: str, max_lines: int) -> List[str]:
        """
        Efficiently read the last N lines from a file without reading the entire file.
        This method reads from the end of the file backwards in chunks.

        :param file_path: Path to the file
        :param max_lines: Maximum number of lines to return
        :return: List of lines from the end of the file
        """
        if max_lines <= 0:
            return []

        try:
            with open(file_path, 'rb') as f:
                # Get file size
                f.seek(0, os.SEEK_END)
                file_size = f.tell()

                # If file is empty, return empty list
                if file_size == 0:
                    return []

                # For small files, just read normally
                if file_size < 8192:  # 8KB threshold
                    f.seek(0)
                    content = f.read().decode('utf-8', errors='replace')
                    all_lines = content.splitlines()
                    return (
                        all_lines[-max_lines:]
                        if len(all_lines) > max_lines
                        else all_lines
                    )

                # For larger files, read backwards in chunks
                lines = []
                buffer = b''
                chunk_size = min(8192, file_size)  # 8KB chunks
                position = file_size

                while len(lines) < max_lines and position > 0:
                    # Calculate how much to read
                    read_size = min(chunk_size, position)
                    position -= read_size

                    # Read chunk from current position
                    f.seek(position)
                    chunk = f.read(read_size)

                    # Prepend to buffer
                    buffer = chunk + buffer

                    # Split buffer into lines, keeping incomplete line at start
                    try:
                        decoded_buffer = buffer.decode('utf-8')
                    except UnicodeDecodeError:
                        # Handle encoding issues by replacing problematic characters
                        decoded_buffer = buffer.decode('utf-8', errors='replace')

                    buffer_lines = decoded_buffer.split('\n')

                    # Keep the first line (potentially incomplete) in buffer for next iteration
                    if position > 0 and len(buffer_lines) > 1:
                        # Save incomplete first line for next iteration
                        incomplete_line = buffer_lines[0]
                        complete_lines = buffer_lines[1:]
                        buffer = incomplete_line.encode('utf-8')
                    else:
                        # We're at the beginning of file, all lines are complete
                        complete_lines = buffer_lines
                        buffer = b''

                    # Add complete lines to our result (in reverse order since we're reading backwards)
                    complete_lines.reverse()
                    lines.extend(complete_lines)

                    # If we've read enough lines, break
                    if len(lines) >= max_lines:
                        break

                # Reverse the lines to get correct order and limit to max_lines
                lines.reverse()
                return lines[-max_lines:] if len(lines) > max_lines else lines

        except Exception as e:
            # Fallback to simple method for any encoding or IO issues
            self.logger.warning(
                f'Efficient tail failed for {file_path}, falling back to simple method: {str(e)}'
            )
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
                return [line.rstrip('\n\r') for line in all_lines[-max_lines:]]

    def save_file(self, request: SaveFileRequest) -> SaveFileResult:
        file = request.file
        if Utils.is_empty(file):
            raise exceptions.invalid_params('file is required')
        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)
        if Utils.is_binary_file(file):
            raise exceptions.invalid_params(
                'file is not a text file. upload the binary file instead'
            )

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
            return {'success': False}

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
                'files_skipped': files_skipped,
            },
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
            raise exceptions.invalid_params(
                f'a symbolic link already exists at: {downloads_dir}'
            )

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
            raise exceptions.invalid_params(
                f'invalid name: {original_filename}, name cannot contain "/" or special characters'
            )

        # ensure file name is ascii characters and nothing funky going on in file name
        filename = Utils.to_secure_filename(filename)
        if original_filename != filename:
            raise exceptions.invalid_params(
                f'invalid characters in name: {original_filename}'
            )

        create_path = os.path.join(cwd, filename)
        if Utils.is_symlink(create_path):
            raise exceptions.invalid_params(
                f'a symbolic link already exists at: {create_path}'
            )

        is_folder = Utils.get_as_bool(request.is_folder, False)
        if is_folder:
            if Utils.is_dir(create_path):
                raise exceptions.invalid_params(
                    f'directory: {filename} already exists under: {cwd}'
                )
            os.makedirs(create_path)
        else:
            if Utils.is_file(create_path):
                raise exceptions.invalid_params(
                    f'file: {filename} already exists under: {cwd}'
                )
            with open(create_path, 'w') as f:
                f.write('')

        group_name = self.group_name_helper.get_user_group(self.username)
        shutil.chown(create_path, self.username, group_name)

        return CreateFileResult()

    def delete_files(self, request: DeleteFilesRequest) -> DeleteFilesResult:
        files = request.files
        if Utils.is_empty(files):
            raise exceptions.invalid_params('files[] is required')

        deletion_errors = []
        successfully_deleted = []

        for file in files:
            try:
                self.check_access(file, check_read=False, check_write=True)

                # Check file type and delete immediately to minimize race conditions
                if os.path.islink(file):
                    os.unlink(file)
                    self.logger.info(f'{self.username} deleted symlink: "{file}"')
                    successfully_deleted.append(file)
                elif Utils.is_dir(file):
                    shutil.rmtree(file, ignore_errors=False)
                    self.logger.info(f'{self.username} deleted directory: "{file}"')
                    successfully_deleted.append(file)
                elif Utils.is_file(file):
                    os.remove(file)
                    self.logger.info(f'{self.username} deleted file: "{file}"')
                    successfully_deleted.append(file)
                else:
                    # File doesn't exist or is not a recognizable type
                    self.logger.warning(
                        f'{self.username} attempted to delete non-existent or unrecognized file: "{file}"'
                    )
                    deletion_errors.append(
                        f'File not found or unrecognized type: {file}'
                    )

            except FileNotFoundError:
                # File was already deleted or never existed
                self.logger.warning(
                    f'{self.username} attempted to delete already deleted file: "{file}"'
                )
                deletion_errors.append(f'File not found: {file}')
            except PermissionError as e:
                # Permission denied during deletion
                self.logger.error(
                    f'{self.username} permission denied deleting file: "{file}" - {str(e)}'
                )
                deletion_errors.append(f'Permission denied: {file}')
            except OSError as e:
                # Other OS-level errors (file in use, I/O errors, etc.)
                self.logger.error(
                    f'{self.username} OS error deleting file: "{file}" - {str(e)}'
                )
                deletion_errors.append(f'OS error: {file} - {str(e)}')
            except Exception as e:
                # Catch any other unexpected errors
                self.logger.error(
                    f'{self.username} unexpected error deleting file: "{file}" - {str(e)}',
                    exc_info=True,
                )
                deletion_errors.append(f'Unexpected error: {file} - {str(e)}')

        # Log summary of deletion operation
        if deletion_errors:
            self.logger.warning(
                f'{self.username} deletion completed with {len(deletion_errors)} errors. Successfully deleted: {len(successfully_deleted)}, Errors: {len(deletion_errors)}'
            )

        # For now, return success even with some errors (backward compatibility)
        # Consider adding error details to the result model if needed
        return DeleteFilesResult()

    def _check_tail_rate_limit(self, username: str) -> bool:
        """Check if user has exceeded rate limits for tail operations"""
        global _tail_rate_limits, _active_tail_users

        current_time = time.time()

        # Clean up inactive users (no requests in the last 5 minutes)
        inactive_threshold = current_time - 300  # 5 minutes
        inactive_users = []
        for user, timestamps in _tail_rate_limits.items():
            if timestamps and max(timestamps) < inactive_threshold:
                inactive_users.append(user)

        for user in inactive_users:
            _active_tail_users.discard(user)
            _tail_rate_limits.pop(user, None)

        # Check concurrent user limit
        if (
            len(_active_tail_users) >= TAIL_FILE_MAX_CONCURRENT_USERS
            and username not in _active_tail_users
        ):
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_TAIL_THROTTLE,
                message=f'Maximum concurrent tail users ({TAIL_FILE_MAX_CONCURRENT_USERS}) reached. Please try again later.',
            )

        # Add user to active set
        _active_tail_users.add(username)

        # Initialize user's rate limit tracking if not exists
        if username not in _tail_rate_limits:
            _tail_rate_limits[username] = []

        # Clean old entries outside the time window
        window_start = current_time - TAIL_FILE_RATE_LIMIT_WINDOW
        _tail_rate_limits[username] = [
            timestamp
            for timestamp in _tail_rate_limits[username]
            if timestamp > window_start
        ]

        # Check if user has exceeded rate limit
        if len(_tail_rate_limits[username]) >= TAIL_FILE_MAX_REQUESTS_PER_WINDOW:
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_TAIL_THROTTLE,
                message=f'Rate limit exceeded. Maximum {TAIL_FILE_MAX_REQUESTS_PER_WINDOW} tail requests per {TAIL_FILE_RATE_LIMIT_WINDOW} seconds.',
            )

        # Record this request
        _tail_rate_limits[username].append(current_time)
        return True
