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
import os

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
    RenameFileRequest,
    RenameFileResult,
    TailFileRequest,
    TailFileResult,
    CheckFilesPermissionsRequest,
    CheckFilesPermissionsResult,
    FilePermissionResult,
    FileData,
)
from ideadatamodel import exceptions, errorcodes
from ideasdk.utils import Utils, GroupNameHelper
from ideasdk.protocols import SocaContextProtocol
from ideasdk.shell import ShellInvoker

import arrow
import mimetypes
import shutil
import time
from typing import Dict, List, Tuple
from zipfile import ZipFile, ZIP_DEFLATED

# default lines to prefetch on an initial tail request.
TAIL_FILE_MAX_LINE_COUNT = 10000
# default lines to prefetch on an initial tail request.
TAIL_FILE_DEFAULT_LINE_COUNT = 1000
# min interval during subsequent tail requests. requests less than this interval will return empty lines
TAIL_FILE_MIN_INTERVAL_SECONDS = 5

# Permission cache TTL in seconds
PERMISSION_CACHE_TTL = 60
# Maximum cache entries per user to prevent unbounded growth
PERMISSION_CACHE_MAX_ENTRIES = 1000

# Rate limiting configuration
TAIL_FILE_RATE_LIMIT_WINDOW = 60  # 1 minute window
TAIL_FILE_MAX_REQUESTS_PER_WINDOW = 20  # Max 20 requests per minute per user
TAIL_FILE_MAX_CONCURRENT_USERS = 50  # Max 50 concurrent users using tail
# Maximum entries in global rate limiting storage
RATE_LIMIT_MAX_ENTRIES = 200

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
_last_global_cleanup = 0


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
        self._last_permission_cleanup = 0
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
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(
                        f'Permission check (cached) - User: {self.username}, File: {file}, Type: {check_type}, Result: {result}'
                    )
                return result

        # Execute shell command
        result_obj = self.shell.invoke(shell_cmd)
        result = result_obj.returncode == 0

        # Cache the result
        self._permission_cache[cache_key] = (result, current_time)

        # Log permission check result for debugging
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'Permission check (fresh) - User: {self.username}, File: {file}, Type: {check_type}, Result: {result}'
            )

        # Log if cache is getting large
        if len(self._permission_cache) > PERMISSION_CACHE_MAX_ENTRIES * 0.8:
            self.logger.warning(
                f'Permission cache approaching limit - User: {self.username}: {len(self._permission_cache)}/{PERMISSION_CACHE_MAX_ENTRIES}'
            )

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

        # Clean up caches periodically (these have built-in throttling)
        self._cleanup_permission_cache()
        self._cleanup_global_state()

        self.check_access(cwd, check_dir=True, check_read=True, check_write=False)

        # This is specifically _after_ the check_access so its timing doesnt impact this timing
        if self.logger.isEnabledFor(logging.DEBUG):
            listing_start = Utils.current_time_ms()

        result = []
        with os.scandir(cwd) as scandir:
            for entry in scandir:
                if Utils.is_empty(entry):
                    continue

                if entry.is_file():
                    # Check for restricted files/dirs and do not list them
                    if cwd == '/' and entry.name in RESTRICTED_ROOT_FOLDERS:
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
                f'Directory listing - User: {self.username}, Path: {cwd}, Items: {len(result)}, Duration: {listing_end - listing_start}ms'
            )

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

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'File read - User: {self.username}, File: {file}, Size: {len(content)} chars, Type: {content_type}'
            )

        return ReadFileResult(
            file=file, content_type=content_type, content=Utils.base64_encode(content)
        )

    def tail_file(self, request: TailFileRequest) -> TailFileResult:
        file = request.file
        start_time = Utils.current_time_ms()

        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)

        # Clean up caches periodically (these have built-in throttling)
        self._cleanup_permission_cache()
        self._cleanup_global_state()

        # Check rate limits before proceeding
        self._check_tail_rate_limit(self.username)

        # For continuation requests (with next_token), skip permission checks since
        # the user already had access to start the tail operation
        is_initial_request = Utils.is_empty(request.next_token)
        if is_initial_request:
            self.check_access(file, check_dir=False, check_read=True, check_write=False)

        # Check file size and reject files that are too large for tail operations
        file_size = os.path.getsize(file)
        max_file_size = 10 * 1024 * 1024 * 1024  # 10GB limit
        if file_size > max_file_size:
            self.logger.warning(
                f'User {self.username} attempted to tail large file: '
                f'{file} ({file_size / (1024 * 1024 * 1024):.2f} GB)'
            )
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_NOT_A_TEXT_FILE,
                message=f'file is too large ({file_size / (1024 * 1024 * 1024):.2f} GB) for tail operations. Maximum supported size is {max_file_size / (1024 * 1024 * 1024):.0f}GB.',
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

        # Log performance metrics for large files or slow operations
        end_time = Utils.current_time_ms()
        duration = end_time - start_time
        if (
            file_size > 100 * 1024 * 1024 or duration > 1000
        ):  # Large files or >1s operations
            self.logger.info(
                f'TailFile - User: {self.username}, File: {file}, '
                f'Lines: {len(lines)}, Duration: {duration}ms, Size: {file_size // 1024 // 1024}MB'
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
                    all_lines[-max_lines:] if len(all_lines) > max_lines else all_lines
                )

            # For larger files, read backwards in chunks
            lines = []
            buffer = b''
            # Use smaller chunks for tail operations to balance memory usage and performance
            chunk_size = min(
                self._calculate_optimal_chunk_size(file_size) // 4, file_size
            )
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

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'File saved - User: {self.username}, File: {file}, Size: {len(content)} chars'
            )

        return SaveFileResult()

    def download_files(self, request: DownloadFilesRequest) -> str:
        files = Utils.get_as_list(request.files, [])
        if not files:
            raise exceptions.invalid_params('file is required')

        # Calculate total size and identify large files (including directory contents)
        total_size = 0
        large_files = []
        for file in files:
            if Utils.is_file(file):
                file_size = os.path.getsize(file)
                total_size += file_size
                if file_size > 1024 * 1024 * 1024:  # 1GB+
                    large_files.append((file, file_size))
            elif Utils.is_dir(file):
                # Calculate directory size recursively
                dir_size = self._calculate_directory_size(file)
                total_size += dir_size
                if dir_size > 1024 * 1024 * 1024:  # 1GB+
                    large_files.append((file, dir_size))

        # Warn about very large downloads
        if total_size > 5 * 1024 * 1024 * 1024:  # 5GB+
            self.logger.warning(
                f'Large download - User: {self.username}, Total size: {total_size // 1024 // 1024 // 1024}GB, '
                f'Large files: {[f"{f}({s // 1024 // 1024 // 1024}GB)" for f, s in large_files]}'
            )

        download_list = []
        for file in files:
            if Utils.is_empty(file):
                continue
            if '..' in file:
                raise exceptions.unauthorized_access()
            if Utils.is_empty(file):
                raise exceptions.invalid_params('file is required')
            if not Utils.is_file(file) and not Utils.is_dir(file):
                raise exceptions.file_not_found(file)

            # Check permissions based on file type
            if Utils.is_file(file):
                self.check_access(
                    file, check_dir=False, check_read=True, check_write=False
                )
            elif Utils.is_dir(file):
                self.check_access(
                    file, check_dir=True, check_read=True, check_write=False
                )

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

        # Create meaningful filename with timestamp
        timestamp = arrow.now().format('YYYY-MM-DD_HH-mm-ss')

        # Determine appropriate name based on what we're downloading
        if len(download_list) == 1:
            # Single item - use its name
            single_item = download_list[0]
            if Utils.is_dir(single_item):
                # Single directory - use directory name
                # Strip trailing slash if present to get proper basename
                single_item = single_item.rstrip('/')
                base_name = os.path.basename(single_item)
                # If basename is empty (e.g., root directory), use a fallback
                if not base_name:
                    base_name = 'directory'
            else:
                # Single file - use filename without extension for zip name
                base_name = os.path.splitext(os.path.basename(single_item))[0]
        else:
            # Multiple items - try to find common directory or use generic name
            first_item = download_list[0]
            if Utils.is_dir(first_item):
                # If first item is a directory, use its parent directory name
                base_name = os.path.basename(os.path.dirname(first_item.rstrip('/')))
            else:
                # If first item is a file, use its parent directory name
                base_name = os.path.basename(os.path.dirname(first_item))

        # Create filename based on determined name
        if base_name and Utils.is_not_empty(base_name):
            # Sanitize the name and use it
            safe_name = Utils.to_secure_filename(base_name)
            if Utils.is_not_empty(safe_name):
                zip_filename = f'{safe_name}_{timestamp}.zip'
            else:
                # Name couldn't be sanitized, use count-based naming
                file_count = len(download_list)
                if file_count <= 99:
                    zip_filename = f'idea_{file_count}items_{timestamp}.zip'
                else:
                    zip_filename = f'idea_download_{timestamp}.zip'
        else:
            # No name available, use count-based naming
            file_count = len(download_list)
            if file_count <= 99:
                zip_filename = f'idea_{file_count}items_{timestamp}.zip'
            else:
                zip_filename = f'idea_download_{timestamp}.zip'

        zip_file_path = os.path.join(downloads_dir, zip_filename)

        if self.logger.isEnabledFor(logging.DEBUG):
            if len(download_list) == 1:
                if Utils.is_dir(download_list[0]):
                    naming_logic = 'single-directory'
                else:
                    naming_logic = 'single-file'
            else:
                naming_logic = 'multiple-items'

            self.logger.debug(
                f'Download archive filename - User: {self.username}, Items: {len(download_list)}, '
                f'Base name: "{base_name}", Logic: {naming_logic}, Filename: {zip_filename}'
            )

            # Additional debug info for troubleshooting
            for i, item in enumerate(download_list):
                item_type = 'directory' if Utils.is_dir(item) else 'file'
                self.logger.debug(f'Download item {i}: {item} ({item_type})')

        # Log download start for large operations
        if total_size > 100 * 1024 * 1024:  # >100MB
            self.logger.info(
                f'Creating download archive - User: {self.username}, Files: {len(download_list)}, '
                f'Total size: {total_size // 1024 // 1024}MB'
            )

        # Use streaming zip creation to prevent OOM on large files
        # Enable ZIP64 for large files/archives (>4GB or >65k files)
        with ZipFile(
            zip_file_path,
            'w',
            compression=ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as zip_archive:
            # Track file names to avoid conflicts
            used_names = set()

            for download_item in download_list:
                if Utils.is_file(download_item):
                    # Handle single file
                    self._add_file_to_zip(zip_archive, download_item, used_names)
                elif Utils.is_dir(download_item):
                    # Handle directory recursively
                    self._add_directory_to_zip(zip_archive, download_item, used_names)

        shutil.chown(zip_file_path, user=self.username, group=group_name)

        # Log successful completion of large downloads
        zip_size = os.path.getsize(zip_file_path)
        if total_size > 100 * 1024 * 1024:  # 100MB+
            self.logger.info(
                f'Archive creation complete - User: {self.username}, Archive: {zip_filename}, '
                f'Archive size: {zip_size // 1024 // 1024}MB from source: {total_size // 1024 // 1024}MB'
            )

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

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'File created - User: {self.username}, Type: {"directory" if is_folder else "file"}, Path: {create_path}'
            )

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

    def rename_file(self, request: RenameFileRequest) -> RenameFileResult:
        file = request.file
        new_name = request.new_name

        if Utils.is_empty(file):
            raise exceptions.invalid_params('file is required')
        if Utils.is_empty(new_name):
            raise exceptions.invalid_params('new_name is required')

        # Check if source file exists
        if not Utils.is_file(file) and not Utils.is_dir(file):
            raise exceptions.file_not_found(file)

        # Check access permissions for the source file
        self.check_access(file, check_read=True, check_write=True)

        # Extract directory and validate new name
        original_filename = new_name
        new_name = os.path.basename(new_name)
        if original_filename != new_name:
            raise exceptions.invalid_params(
                f'invalid name: {original_filename}, name cannot contain "/" or special characters'
            )

        # Ensure file name is secure and contains no special characters
        secure_new_name = Utils.to_secure_filename(new_name)
        if original_filename != secure_new_name:
            raise exceptions.invalid_params(
                f'invalid characters in name: {original_filename}'
            )

        # Create the new file path
        file_directory = os.path.dirname(file)
        new_file_path = os.path.join(file_directory, new_name)

        # Check if destination already exists
        if Utils.is_file(new_file_path) or Utils.is_dir(new_file_path):
            raise exceptions.invalid_params(
                f'file or directory with name "{new_name}" already exists'
            )

        # Check if destination is a symlink (prevent symlink attacks)
        if Utils.is_symlink(new_file_path):
            raise exceptions.invalid_params(
                f'a symbolic link already exists at: {new_file_path}'
            )

        # Check write access to the parent directory
        self.check_access(
            file_directory, check_dir=True, check_read=True, check_write=True
        )

        try:
            # Perform the rename operation
            os.rename(file, new_file_path)

            # Update ownership to maintain proper permissions
            group_name = self.group_name_helper.get_user_group(self.username)
            shutil.chown(new_file_path, self.username, group_name)

            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(
                    f'File renamed - User: {self.username}, From: {file}, To: {new_file_path}'
                )

        except FileNotFoundError:
            raise exceptions.file_not_found(file)
        except PermissionError as e:
            self.logger.error(
                f'{self.username} permission denied renaming file: "{file}" to "{new_file_path}" - {str(e)}'
            )
            raise exceptions.unauthorized_access()
        except OSError as e:
            self.logger.error(
                f'{self.username} OS error renaming file: "{file}" to "{new_file_path}" - {str(e)}'
            )
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Failed to rename file: {str(e)}',
            )
        except Exception as e:
            self.logger.error(
                f'{self.username} unexpected error renaming file: "{file}" to "{new_file_path}" - {str(e)}',
                exc_info=True,
            )
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Unexpected error during rename: {str(e)}',
            )

        return RenameFileResult()

    def check_files_permissions(
        self, request: CheckFilesPermissionsRequest
    ) -> CheckFilesPermissionsResult:
        """
        Check permissions for multiple files for a given operation
        """
        if not request.files:
            return CheckFilesPermissionsResult(results=[])

        operation = request.operation or 'rename'
        results = []

        # User-specific protected folders
        user_protected_folders = [
            '.ssh',
            'storage-root',
            'Desktop',
            'Downloads',
            'Public',
            'Music',
            'Pictures',
            'Videos',
            'Documents',
        ]

        for file_path in request.files:
            try:
                # Check if file is protected
                is_protected = self._is_protected_file(
                    file_path, user_protected_folders
                )

                if is_protected:
                    results.append(
                        FilePermissionResult(
                            file=file_path,
                            has_permission=False,
                            is_protected=True,
                            error_code='PROTECTED_FILE',
                            error_message='File is protected and cannot be modified',
                        )
                    )
                    continue

                # Check actual filesystem permissions based on operation
                if operation in ['rename', 'delete']:
                    # For rename/delete, check write access to parent directory
                    parent_dir = os.path.dirname(file_path) if file_path != '/' else '/'
                    self.check_access(
                        parent_dir, check_dir=True, check_read=True, check_write=True
                    )
                elif operation == 'read':
                    # For read, check read access to the file/directory
                    if os.path.isdir(file_path):
                        self.check_access(
                            file_path,
                            check_dir=True,
                            check_read=True,
                            check_write=False,
                        )
                    else:
                        self.check_access(
                            file_path,
                            check_dir=False,
                            check_read=True,
                            check_write=False,
                        )
                elif operation == 'write':
                    # For write, check write access to the file/directory
                    if os.path.isdir(file_path):
                        self.check_access(
                            file_path, check_dir=True, check_read=True, check_write=True
                        )
                    else:
                        self.check_access(
                            file_path,
                            check_dir=False,
                            check_read=True,
                            check_write=True,
                        )

                # If we get here, permission check passed
                results.append(
                    FilePermissionResult(
                        file=file_path, has_permission=True, is_protected=False
                    )
                )

            except Exception as e:
                error_code = getattr(e, 'error_code', 'PERMISSION_ERROR')
                results.append(
                    FilePermissionResult(
                        file=file_path,
                        has_permission=False,
                        is_protected=False,
                        error_code=error_code,
                        error_message=str(e),
                    )
                )

        return CheckFilesPermissionsResult(results=results)

    def _is_protected_file(
        self, file_path: str, user_protected_folders: List[str]
    ) -> bool:
        """
        Check if a file is protected from modification
        """
        if not file_path:
            return False

        # Get file/directory name
        file_name = os.path.basename(file_path)

        # Check if it's a user-protected folder
        if file_name in user_protected_folders:
            return True

        # Check if it's in root directory and is a system folder
        parent_dir = os.path.dirname(file_path)
        if parent_dir == '/' and file_name in RESTRICTED_ROOT_FOLDERS:
            return True

        return False

    def _check_tail_rate_limit(self, username: str) -> bool:
        """Check if user has exceeded rate limits for tail operations"""
        global _tail_rate_limits, _active_tail_users

        current_time = time.time()

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

    def _cleanup_permission_cache(self):
        """
        Clean up expired permission cache entries to prevent memory leaks
        """
        current_time = time.time()

        # Only cleanup every 30 seconds to avoid clearing cache during active sessions
        if current_time - self._last_permission_cleanup < 30:
            return

        initial_size = len(self._permission_cache)

        # Remove expired entries
        expired_keys = [
            key
            for key, (result, timestamp) in self._permission_cache.items()
            if current_time - timestamp > PERMISSION_CACHE_TTL
        ]
        for key in expired_keys:
            self._permission_cache.pop(key, None)

        # Limit cache size to prevent unbounded growth
        size_limited = False
        if len(self._permission_cache) > PERMISSION_CACHE_MAX_ENTRIES:
            size_limited = True
            # Remove oldest entries
            sorted_items = sorted(
                self._permission_cache.items(),
                key=lambda x: x[1][1],  # Sort by timestamp
            )
            # Keep only the most recent entries
            self._permission_cache = dict(
                sorted_items[-PERMISSION_CACHE_MAX_ENTRIES // 2 :]
            )

        final_size = len(self._permission_cache)
        # Only log if significant cleanup occurred
        if len(expired_keys) > 10 or size_limited:
            self.logger.debug(
                f'Permission cache cleanup - User: {self.username}, Entries: {initial_size} -> {final_size}, Expired: {len(expired_keys)}, Size limited: {size_limited}'
            )

        self._last_permission_cleanup = current_time

    def _cleanup_global_state(self):
        """
        Clean up global state to prevent memory leaks
        """
        global _tail_rate_limits, _active_tail_users, _last_global_cleanup
        current_time = time.time()

        # Only cleanup every 30 seconds to avoid overhead
        if current_time - _last_global_cleanup < 30:
            return

        initial_users = len(_tail_rate_limits)

        # Clean up expired rate limit entries
        window_start = current_time - TAIL_FILE_RATE_LIMIT_WINDOW
        removed_users = []
        for user in list(_tail_rate_limits.keys()):
            _tail_rate_limits[user] = [
                timestamp
                for timestamp in _tail_rate_limits[user]
                if timestamp > window_start
            ]
            # Remove users with no recent activity
            if not _tail_rate_limits[user]:
                _tail_rate_limits.pop(user, None)
                _active_tail_users.discard(user)
                removed_users.append(user)

        # Limit global storage size
        size_limited = False
        if len(_tail_rate_limits) > RATE_LIMIT_MAX_ENTRIES:
            size_limited = True
            # Remove oldest users
            sorted_users = sorted(
                _tail_rate_limits.items(), key=lambda x: max(x[1]) if x[1] else 0
            )
            # Keep only the most recent users
            _tail_rate_limits = dict(sorted_users[-RATE_LIMIT_MAX_ENTRIES // 2 :])
            _active_tail_users = set(_tail_rate_limits.keys())

        final_users = len(_tail_rate_limits)
        # Only log if significant cleanup occurred
        if len(removed_users) > 5 or size_limited:
            self.logger.debug(
                f'Rate limit cleanup: {initial_users} -> {final_users} users'
            )

        _last_global_cleanup = current_time

    def _calculate_directory_size(self, directory_path: str) -> int:
        """Calculate the total size of all files in a directory recursively"""
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(directory_path):
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    try:
                        total_size += os.path.getsize(file_path)
                    except (OSError, IOError):
                        # Skip files that can't be accessed
                        continue
        except (OSError, IOError):
            # If we can't walk the directory, return 0
            pass
        return total_size

    def _calculate_optimal_chunk_size(self, file_size: int) -> int:
        """
        Calculate optimal chunk size based on file size for better I/O performance.

        Uses a tiered approach:
        - Very small files (< 1MB): 32KB chunks to minimize overhead
        - Small files (1MB - 10MB): 128KB chunks
        - Medium files (10MB - 100MB): 512KB chunks
        - Large files (100MB - 1GB): 2MB chunks
        - Very large files (> 1GB): 4MB chunks for maximum throughput

        :param file_size: Size of the file in bytes
        :return: Optimal chunk size in bytes
        """
        if file_size < 1024 * 1024:  # < 1MB
            return 32 * 1024  # 32KB
        elif file_size < 10 * 1024 * 1024:  # < 10MB
            return 128 * 1024  # 128KB
        elif file_size < 100 * 1024 * 1024:  # < 100MB
            return 512 * 1024  # 512KB
        elif file_size < 1024 * 1024 * 1024:  # < 1GB
            return 2 * 1024 * 1024  # 2MB
        else:  # >= 1GB
            return 4 * 1024 * 1024  # 4MB

    def _add_file_to_zip(
        self,
        zip_archive: ZipFile,
        file_path: str,
        used_names: set,
        archive_path: str = None,
    ):
        """Add a single file to the zip archive"""
        if archive_path is None:
            base_name = os.path.basename(file_path)
        else:
            base_name = archive_path

        file_name = base_name

        # Handle duplicate file names by appending a counter
        counter = 1
        while file_name in used_names:
            name, ext = os.path.splitext(base_name)
            file_name = f'{name}_{counter}{ext}'
            counter += 1
        used_names.add(file_name)

        file_size = os.path.getsize(file_path)

        # Log if file name was changed due to conflicts
        if file_name != base_name:
            self.logger.debug(
                f'Archive conflict rename - User: {self.username}, File: {file_path}, Renamed: {base_name} -> {file_name}'
            )

        try:
            # Use streaming approach for all files to ensure consistent memory usage
            # Enable ZIP64 for large files (>2GB) or when archive will be large
            force_zip64 = file_size >= 2 * 1024 * 1024 * 1024  # 2GB threshold

            with zip_archive.open(file_name, 'w', force_zip64=force_zip64) as zip_entry:
                with open(file_path, 'rb') as source_file:
                    # Calculate optimal chunk size based on file size
                    chunk_size = self._calculate_optimal_chunk_size(file_size)
                    bytes_written = 0
                    while True:
                        chunk = source_file.read(chunk_size)
                        if not chunk:
                            break
                        zip_entry.write(chunk)
                        bytes_written += len(chunk)

                        # Log progress for very large files
                        if (
                            file_size > 1024 * 1024 * 1024
                            and bytes_written % (500 * 1024 * 1024) == 0
                        ):  # Every 500MB
                            progress = (bytes_written / file_size) * 100
                            self.logger.debug(
                                f'Archive progress - User: {self.username}, File: {file_path}, Archive name: {file_name}, Progress: {progress:.0f}%, Chunk size: {chunk_size // 1024}KB'
                            )

        except Exception as e:
            self.logger.error(f'Failed to add {file_name} to archive: {str(e)}')
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Failed to create download archive: {str(e)}',
            )

    def _add_directory_to_zip(
        self, zip_archive: ZipFile, directory_path: str, used_names: set
    ):
        """Add a directory and all its contents to the zip archive recursively"""
        # Strip trailing slashes and get the directory name
        directory_path = directory_path.rstrip('/')
        directory_name = os.path.basename(directory_path)

        # Handle empty directory name (shouldn't happen with proper paths)
        if not directory_name:
            directory_name = 'directory'

        # Handle duplicate directory names
        base_dir_name = directory_name
        counter = 1
        while directory_name in used_names:
            directory_name = f'{base_dir_name}_{counter}'
            counter += 1
        used_names.add(directory_name)

        # Log directory processing
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'Adding directory to archive - User: {self.username}, Source: {directory_path}, Archive name: {directory_name}'
            )

        # Log if directory name was changed due to conflicts
        if directory_name != base_dir_name:
            self.logger.debug(
                f'Archive conflict rename - User: {self.username}, Directory: {directory_path}, Renamed: {base_dir_name} -> {directory_name}'
            )

        try:
            # Verify directory exists and is accessible
            if not os.path.exists(directory_path):
                raise exceptions.file_not_found(directory_path)
            if not os.path.isdir(directory_path):
                raise exceptions.soca_exception(
                    error_code=errorcodes.INVALID_PARAMS,
                    message=f'Path is not a directory: {directory_path}',
                )

            # Walk through the directory and add all files
            files_added = 0
            for root, dirs, files in os.walk(directory_path):
                # Calculate relative path from the source directory
                rel_dir = os.path.relpath(root, directory_path)
                if rel_dir == '.':
                    archive_dir = directory_name
                else:
                    archive_dir = os.path.join(directory_name, rel_dir).replace(
                        '\\', '/'
                    )

                # Create directory entry in zip (optional, but good practice)
                if archive_dir not in used_names:
                    zip_archive.writestr(archive_dir + '/', '')
                    used_names.add(archive_dir + '/')

                # Add all files in this directory
                for filename in files:
                    file_path = os.path.join(root, filename)
                    archive_file_path = os.path.join(archive_dir, filename).replace(
                        '\\', '/'
                    )

                    try:
                        # Explicitly check read permission for each file before processing
                        can_read = self._check_shell_permission(
                            file_path,
                            'read',
                            ['su', self.username, '-c', f'test -r "{file_path}"'],
                        )
                        if not can_read:
                            if self.logger.isEnabledFor(logging.DEBUG):
                                self.logger.debug(
                                    f'Skipping file (no read permission) - User: {self.username}, File: {file_path}'
                                )
                            continue

                        # Check if we can read this file
                        file_size = os.path.getsize(file_path)

                        # Use streaming approach
                        force_zip64 = (
                            file_size >= 2 * 1024 * 1024 * 1024
                        )  # 2GB threshold

                        with zip_archive.open(
                            archive_file_path, 'w', force_zip64=force_zip64
                        ) as zip_entry:
                            with open(file_path, 'rb') as source_file:
                                # Use optimal chunk size based on file size
                                chunk_size = self._calculate_optimal_chunk_size(
                                    file_size
                                )
                                while True:
                                    chunk = source_file.read(chunk_size)
                                    if not chunk:
                                        break
                                    zip_entry.write(chunk)

                        files_added += 1

                    except (OSError, IOError, PermissionError) as e:
                        # Skip files that can't be read, but log the issue
                        self.logger.warning(
                            f'Skipping file in archive - User: {self.username}, File: {file_path}, Error: {str(e)}'
                        )
                        continue

            # Log completion
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(
                    f'Directory archive complete - User: {self.username}, Directory: {directory_path}, Files added: {files_added}'
                )

        except Exception as e:
            self.logger.error(
                f'Failed to add directory {directory_name} to archive: {str(e)}'
            )
            raise exceptions.soca_exception(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'Failed to create download archive: {str(e)}',
            )
