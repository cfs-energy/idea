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
import errno
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
from ideasdk.utils import Utils
from ideasdk.protocols import SocaContextProtocol
from functools import wraps
from ideasdk.filesystem.user_identity import UserIdentity, os_refusal

import arrow
import mimetypes
import re
import secrets
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from stat import S_IMODE, S_ISDIR, S_ISLNK, S_ISREG
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

# <name>_<time>_<token>.zip: the token is what tells an archive built here from a person's own zip
GENERATED_ARCHIVE_NAME = re.compile(
    r'_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{12}\.zip$'
)

# Names listed in the message when a delete partly fails
MAX_DELETE_FAILURES_SHOWN = 5

# Global rate limiting storage
_tail_rate_limits: Dict[str, List[float]] = {}
_last_global_cleanup = 0
# taken by the limit and its clean-up, which run on any server worker
_tail_state_lock = threading.Lock()

# Archive builds hold a server worker for as long as they run, up to 5 minutes, unlike every
# other file operation: these limits keep workers free for everyone else's quick calls.
# Builds running at once, one person's and everyone's on a task (of 16 server workers):
MAX_ARCHIVE_BUILDS_PER_USER = 2
# shortcut: fixed, not derived from <module>.server.max_workers; revisit if that is lowered
MAX_ARCHIVE_BUILDS = 6
# A build waits this long for a slot before it is refused. Waiting holds a worker too, so
# only this many may wait, one person's and everyone's: the rest are refused at once.
ARCHIVE_BUILD_WAIT_SECONDS = 5
MAX_ARCHIVE_BUILDS_WAITING_PER_USER = 1
MAX_ARCHIVE_BUILDS_WAITING = 2
# Longer than other file work, the builds being limited above, and still inside the 600 s
# the load balancers and the server give one request: a build past that has no one to answer.
ARCHIVE_BUILD_TIMEOUT_SECONDS = 570
_archive_builds: Dict[str, int] = {}
_archive_builds_waiting: Dict[str, int] = {}
_archive_builds_changed = threading.Condition()


def _archive_builds_busy(own: int) -> exceptions.SocaException:
    return exceptions.SocaException(
        error_code=errorcodes.FILE_OPERATION_BUSY,
        message=(
            f'You already have {own} downloads being prepared. '
            'Try again when one of them has finished.'
            if own >= MAX_ARCHIVE_BUILDS_PER_USER
            else 'The server is preparing many downloads right now. '
            'Try again in a minute.'
        ),
    )


def _add(counts: Dict[str, int], username: str, change: int):
    count = counts.get(username, 0) + change
    if count > 0:
        counts[username] = count
    else:
        counts.pop(username, None)


# Entries one ListFiles answer lists, each a stat as the user: a folder holding more is
# answered with these and truncated=True
# shortcut: a cap, not paging; page ListFiles when the portal can ask for the next page
MAX_LISTED_ENTRIES = 10000

# What ReadFile returns, as the editor opens and saves it: larger files are downloaded
READ_FILE_MAX_BYTES = 5 * 1024 * 1024
# What one TailFile answer reads at most, whatever the line count and line length
TAIL_FILE_MAX_BYTES = 8 * 1024 * 1024


def _archive_build_free(username: str) -> bool:
    return _archive_builds.get(username, 0) < MAX_ARCHIVE_BUILDS_PER_USER and (
        sum(_archive_builds.values()) < MAX_ARCHIVE_BUILDS
    )


def _inherited_acl(fd: int) -> bool:
    """whether a file holds an ACL (POSIX or NFSv4); False where there are no attributes"""
    try:
        names = os.listxattr(fd)
    except (AttributeError, OSError):
        return False  # macOS has no listxattr, or the file system has none
    return any(name.startswith(('system.posix_acl_', 'system.nfs4')) for name in names)


def _whole_utf8_length(data: bytes) -> int:
    """how much of data ends on a whole UTF-8 character (all of it, if it is not UTF-8)"""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 != 0x80:  # not a continuation byte: a character starts here
            needed = (
                1
                if byte < 0x80
                else 2
                if byte >> 5 == 6
                else 3
                if byte >> 4 == 14
                else 4
            )
            return len(data) if back >= needed else len(data) - back
    return len(data)


@contextmanager
def _archive_build_slot(username: str):
    with _archive_builds_changed:
        if not _archive_build_free(username):
            if (
                _archive_builds_waiting.get(username, 0)
                >= MAX_ARCHIVE_BUILDS_WAITING_PER_USER
                or sum(_archive_builds_waiting.values()) >= MAX_ARCHIVE_BUILDS_WAITING
            ):
                raise _archive_builds_busy(_archive_builds.get(username, 0))
            _add(_archive_builds_waiting, username, 1)
            try:
                if not _archive_builds_changed.wait_for(
                    lambda: _archive_build_free(username), ARCHIVE_BUILD_WAIT_SECONDS
                ):
                    raise _archive_builds_busy(_archive_builds.get(username, 0))
            finally:
                _add(_archive_builds_waiting, username, -1)
        _add(_archive_builds, username, 1)
    try:
        yield
    finally:
        with _archive_builds_changed:
            _add(_archive_builds, username, -1)
            _archive_builds_changed.notify_all()


# Running the entire operation keeps metadata probes and file handles out of the parent.
# Nested checks reuse the worker so every access uses the same account credentials.
def as_user(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        return self.run_as_user(lambda: method(self, *args, **kwargs))

    return invoke


def as_user_archive_build(method):
    """as_user, counted against the archive build limits"""

    @wraps(method)
    def invoke(self, *args, **kwargs):
        return self.run_as_user(
            lambda: method(self, *args, **kwargs), archive_build=True
        )

    return invoke


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
        self._identity = None
        # Permission cache: {(file_path, username, check_type): (result, timestamp)}
        self._permission_cache: Dict[Tuple[str, str, str], Tuple[bool, float]] = {}
        self._last_permission_cleanup = 0
        self._archive_being_written = None
        self._archive_identity = None
        self._files_left_out = 0

    def run_as_user(self, operation, archive_build=False):
        if self._identity is None:
            self._identity = UserIdentity(self.context.accounts, self.username)
        # a nested call runs inside the user's own worker, under the slot its caller holds
        if not archive_build or self._identity.in_worker():
            return self._identity.run(operation)
        with _archive_build_slot(self.username):
            return self._identity.run(operation, timeout=ARCHIVE_BUILD_TIMEOUT_SECONDS)

    def own(self, fd: int, keep_group: bool = False):
        """
        gives a file the server created on the user's behalf to the user, by id and by
        descriptor: the server runs in a container that cannot look the user's name up,
        and a path can be swapped for a link between a create and a chown.
        keep_group leaves the group the file was created with: a set-group-id folder's,
        as a file made in a shell there gets
        """
        if self._identity is None:
            self._identity = UserIdentity(self.context.accounts, self.username)
        os.fchown(fd, self._identity.uid, -1 if keep_group else self._identity.gid)

    def get_user_home(self) -> str:
        user_home = self.context.config().get_string(
            'shared-storage.data.mount_dir', default='/data'
        )
        if user_home.endswith('/'):
            user_home = user_home[:-1]
        user_home = f'{user_home}/home/{self.username}'
        return user_home

    @as_user
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

    @as_user
    def _check_permission(self, file: str, check_type: str) -> bool:
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

        # Kernel checks use the same numeric credentials as the actual operation.
        # Directory traversal still requires search permission when entries are opened.
        if check_type == 'dir':
            result = os.path.isdir(file)
        elif check_type == 'read':
            result = os.access(file, os.R_OK)
        else:
            # making an entry in a folder needs search on it as well as write
            result = os.access(file, os.W_OK | (os.X_OK if os.path.isdir(file) else 0))

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

    @as_user
    def check_access(
        self,
        file: str,
        check_dir=False,
        check_read=True,
        check_write=True,
        follow=True,
    ):
        """
        :param follow: test where a link leads. Deleting or renaming a link acts on the
            link itself, so those pass False: the entry's own place is tested, and a link
            in the home that points into a system folder can still be removed or renamed
            (nothing is read or written through it).
        """
        if Utils.is_empty(file):
            raise exceptions.unauthorized_access()
        # only absolute paths, and no step up (a name such as a..b is a name)
        if not file.startswith('/') or '..' in file.split('/'):
            raise exceptions.unauthorized_access()

        restricted = self._in_system_folder

        # the path as written, however many slashes start it, and where it leads as the
        # user: a link in the home to /etc is /etc
        written = os.path.normpath('/' + file.lstrip('/'))
        leads_to = (
            os.path.realpath(file)
            if follow
            else os.path.join(
                os.path.realpath(os.path.dirname(written)), os.path.basename(written)
            )
        )
        # do not allow any access to system folders (a listing of / leaves them out)
        if restricted(written) or restricted(leads_to):
            raise exceptions.unauthorized_access()

        if check_dir:
            is_dir = self._check_permission(file, 'dir')
            if not is_dir:
                raise exceptions.unauthorized_access()
        if check_read:
            can_read = self._check_permission(file, 'read')
            if not can_read:
                raise exceptions.unauthorized_access()
        if check_write:
            can_write = self._check_permission(file, 'write')
            if not can_write:
                raise exceptions.unauthorized_access()

    def _in_system_folder(self, path: str) -> bool:
        """an absolute, resolved path under one of the container's own folders"""
        mount = getattr(self, '_data_mount', None)
        if mount is None:
            mount = (
                self.context.config().get_string('shared-storage.data.mount_dir') or ''
            ).rstrip('/')
            self._data_mount = mount
        in_data_mount = bool(mount) and (path == mount or path.startswith(mount + '/'))
        return not in_data_mount and path.split('/')[1] in RESTRICTED_ROOT_FOLDERS

    def _opened_in_system_folder(self, fd: int, path: str) -> bool:
        """
        where an opened file really is: the descriptor's own path where the platform
        tells it (a link swapped after the open changes nothing), else the path resolved
        """
        try:
            where = os.readlink(f'/proc/self/fd/{fd}')
        except OSError:
            where = os.path.realpath(path)
        return self._in_system_folder(where)

    @as_user
    def identify_download(self, file: str) -> Tuple[bool, int, int]:
        """
        Checks that the user may read a file or folder and opens it as the user, following a
        link of their own as the file browser does. Returns (is a folder, device, inode): the
        server reads a download with its own rights, and streams it only if what it opens is
        this same file.
        """
        self.check_access(file, check_read=True, check_write=False)
        try:
            fd = os.open(file, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            raise exceptions.file_not_found(file)
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        # a pipe or a device is not something to download
        if not (S_ISREG(info.st_mode) or S_ISDIR(info.st_mode)):
            raise exceptions.unauthorized_access()
        return S_ISDIR(info.st_mode), info.st_dev, info.st_ino

    @as_user
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
        truncated = False
        at_root = os.path.realpath(cwd) == '/'
        with os.scandir(cwd) as scandir:
            for entry in scandir:
                if Utils.is_empty(entry):
                    continue
                # the system folders are not listed at the root, folders or not
                if at_root and entry.name in RESTRICTED_ROOT_FOLDERS:
                    continue
                if len(result) >= MAX_LISTED_ENTRIES:
                    truncated = True
                    break

                is_hidden = entry.name.startswith('.')
                try:
                    stat = entry.stat()
                    is_dir = entry.is_dir()
                except OSError:
                    # a symlink whose target lives on another host (a desktop's /var/tmp),
                    # or cannot be reached, lists as the link itself instead of failing
                    # the whole folder
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue  # gone since it was read
                    is_dir = False
                file_size = None if is_dir else stat.st_size
                mod_date = arrow.get(stat.st_mtime).datetime

                result.append(
                    FileData(
                        file_id=Utils.shake_256(f'{entry.name}{is_dir}', 5),
                        name=entry.name,
                        size=file_size,
                        mod_date=mod_date,
                        is_dir=is_dir,
                        is_hidden=is_hidden,
                    )
                )

        if self.logger.isEnabledFor(logging.DEBUG):
            listing_end = Utils.current_time_ms()
            self.logger.debug(
                f'Directory listing - User: {self.username}, Path: {cwd}, Items: {len(result)}, Duration: {listing_end - listing_start}ms'
            )

        return ListFilesResult(cwd=cwd, listing=result, truncated=truncated or None)

    @as_user
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
            too_large = os.fstat(f.fileno()).st_size > READ_FILE_MAX_BYTES
            # no more than the limit is read, even from a file that grows meanwhile
            content = '' if too_large else f.read(READ_FILE_MAX_BYTES + 1)
        if too_large or len(content) > READ_FILE_MAX_BYTES:
            raise exceptions.soca_exception(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'This file is larger than {READ_FILE_MAX_BYTES // (1024 * 1024)} MB '
                'and cannot be opened here. Download it instead.',
            )

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'File read - User: {self.username}, File: {file}, Size: {len(content)} chars, Type: {content_type}'
            )

        return ReadFileResult(
            file=file, content_type=content_type, content=Utils.base64_encode(content)
        )

    def tail_file(self, request: TailFileRequest) -> TailFileResult:
        self._cleanup_permission_cache()
        self._cleanup_global_state()
        self._check_tail_rate_limit(self.username)
        return self._tail_file(request)

    @as_user
    def _tail_file(self, request: TailFileRequest) -> TailFileResult:
        file = request.file
        start_time = Utils.current_time_ms()

        if not Utils.is_file(file):
            raise exceptions.file_not_found(file)

        # Clean up caches periodically (these have built-in throttling)
        self._cleanup_permission_cache()
        self._cleanup_global_state()

        # every request, a continuation too: its token says nothing about the file named
        self.check_access(file, check_dir=False, check_read=True, check_write=False)

        # any size of file can be tailed: what one answer reads is bounded by TAIL_FILE_MAX_BYTES
        file_size = os.path.getsize(file)

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

            with open(file, 'rb') as file_handle:
                offset = max(Utils.get_as_int(cursor_tokens[0], 0), 0)
                # a file truncated or replaced since the last read starts again from the top
                if offset > os.fstat(file_handle.fileno()).st_size:
                    offset = 0
                file_handle.seek(offset)

                # a long line is returned in parts: the next request carries on where this
                # stopped. Bytes are counted, so multi-byte text reads no more than the limit
                budget = TAIL_FILE_MAX_BYTES
                while len(lines) < max_line_count and budget > 0:
                    line = file_handle.readline(budget)
                    if line == b'':
                        break
                    if not line.endswith(b'\n'):
                        # cut by the limit: end on a whole character, the rest comes next
                        whole = _whole_utf8_length(line)
                        if whole < len(line):
                            file_handle.seek(whole - len(line), os.SEEK_CUR)
                            line = line[:whole]
                        if not line:
                            break
                    budget -= len(line)
                    lines.append(line.decode('utf-8', errors='replace'))

                cursor_tokens = [file_handle.tell(), Utils.current_time_ms()]
        else:
            # if cursor file does not exist, prefetch last N lines, and carry on from the
            # end that was read: a line written meanwhile comes with the next request
            prefetch_lines, end = self._tail_file_efficiently(file, max_line_count)
            for line in prefetch_lines:
                lines.append(line.rstrip())
            cursor_tokens = [end, Utils.current_time_ms()]

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

    def _tail_file_efficiently(
        self, file_path: str, max_lines: int
    ) -> Tuple[List[str], int]:
        """
        Efficiently read the last N lines from a file without reading the entire file.
        This method reads from the end of the file backwards in chunks.

        :param file_path: Path to the file
        :param max_lines: Maximum number of lines to return
        :return: the lines from the end of the file, and the offset of the end they were
            read back from
        """
        with open(file_path, 'rb') as f:
            # Get file size
            f.seek(0, os.SEEK_END)
            file_size = f.tell()

            # If file is empty, return empty list
            if file_size == 0 or max_lines <= 0:
                return [], file_size

            # For small files, just read normally
            if file_size < 8192:  # 8KB threshold
                f.seek(0)
                content = f.read(file_size).decode('utf-8', errors='replace')
                all_lines = content.splitlines()
                return (
                    all_lines[-max_lines:] if len(all_lines) > max_lines else all_lines
                ), file_size

            # For larger files, read backwards in chunks
            lines = []
            buffer = b''
            # Use smaller chunks for tail operations to balance memory usage and performance
            chunk_size = min(
                self._calculate_optimal_chunk_size(file_size) // 4, file_size
            )
            position = file_size

            while (
                len(lines) < max_lines
                and position > 0
                and file_size - position < TAIL_FILE_MAX_BYTES
            ):
                # Calculate how much to read: never past the byte limit
                read_size = min(
                    chunk_size, position, TAIL_FILE_MAX_BYTES - (file_size - position)
                )
                position -= read_size

                # Read chunk from current position
                f.seek(position)
                chunk = f.read(read_size)

                # Prepend to buffer
                buffer = chunk + buffer

                # Split into lines as bytes, keeping the incomplete first one for the next
                # read: a character cut by the chunk edge is decoded once it is whole
                buffer_lines = buffer.split(b'\n')
                if position > 0 and len(buffer_lines) > 1:
                    complete_lines = buffer_lines[1:]
                    buffer = buffer_lines[0]
                else:
                    # We're at the beginning of file, all lines are complete
                    complete_lines = buffer_lines
                    buffer = b''

                # Add complete lines to our result (in reverse order since we're reading backwards)
                complete_lines.reverse()
                lines.extend(
                    line.decode('utf-8', errors='replace') for line in complete_lines
                )

                # If we've read enough lines, break
                if len(lines) >= max_lines:
                    break

            if buffer and len(lines) < max_lines:
                # stopped by the byte limit inside a line: show the end of it that was read
                lines.append(buffer.decode('utf-8', errors='replace'))

            # Reverse the lines to get correct order and limit to max_lines
            lines.reverse()
            return (lines[-max_lines:] if len(lines) > max_lines else lines), file_size

    @as_user
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

        self._write_replacing(file, content)

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'File saved - User: {self.username}, File: {file}, Size: {len(content)} chars'
            )

        return SaveFileResult()

    def _write_replacing(self, file: str, content: str):
        """
        Saves new content without the old being lost on the way: written to a new file
        beside it, synced, then renamed over it. A link is followed and its target
        replaced. The new file takes the old one's mode and group.

        Out of space or quota at any step, the save is refused and the file left as it
        was, whatever the new content's length. Any other failure writing, syncing or
        renaming the copy refuses the same way.

        Written over in place, with no truncation before the new bytes are on disk (see
        _write_over), only where a new file could not be the same file or could not be
        made: someone else owns it, it has other hard links or extended attributes, its
        group is not one the account is in, the folder cannot be written, or making the
        copy (mkstemp, fchmod, fchown) fails for a reason other than space.
        Replacing gives the file a new inode: a process holding the old one open (a job
        still appending, tail -f) keeps writing to or reading the old copy.
        """
        target = os.path.realpath(file)
        info = os.stat(target)
        folder = os.path.dirname(target)
        try:
            extended = bool(os.listxattr(target))
        except (AttributeError, OSError):
            extended = False  # macOS has no listxattr, or the file system has none
        if (
            info.st_nlink > 1
            or info.st_uid != os.geteuid()
            or info.st_gid not in (os.getegid(), *os.getgroups())
            or extended
            or not os.access(folder, os.W_OK | os.X_OK)
        ):
            self._write_over(target, content)
            return

        saving = None
        copy = None
        try:
            try:
                # a short name of its own: the file's name may already be near the limit
                fd, saving = tempfile.mkstemp(dir=folder, prefix='.idea-save-')
                copy = open(fd, 'w')
                os.fchmod(copy.fileno(), S_IMODE(info.st_mode))
                # the copy's own group, not the process's: a set-group-ID folder gives a
                # new file its group, and the saved file must keep the one it had
                if os.fstat(copy.fileno()).st_gid != info.st_gid:
                    os.fchown(copy.fileno(), -1, info.st_gid)
                if _inherited_acl(copy.fileno()):
                    # the folder's default ACL went onto the copy, and the original had
                    # none: it could not be the same file, so written over in place
                    raise OSError(errno.ENOTSUP, 'the copy inherited an ACL')
            except OSError as e:
                if e.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise
                # no copy can be made here: written over in place instead, below
                if copy is not None:
                    copy.close()
                copy = None
            if copy is not None:
                with copy:
                    copy.write(content)
                    copy.flush()
                    os.fsync(copy.fileno())
                os.replace(saving, target)
                return
        finally:
            if copy is not None and not copy.closed:
                copy.close()
            if saving is not None and os.path.lexists(saving):
                os.unlink(saving)
        self._write_over(target, content)

    @staticmethod
    def _write_over(target: str, content: str):
        """
        Writes the new content over the old from the start, syncs it, and only then cuts
        the file to the new length: a failed write leaves the old content after whatever
        was written, never an empty file.
        """
        with open(target, 'r+') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            f.truncate()

    def download_files(self, request: DownloadFilesRequest) -> str:
        return self.create_download_archive(request)[0]

    def _downloads_dir(self) -> str:
        return os.path.join(self.get_user_home(), 'idea_downloads')

    def is_generated_archive(self, path: str) -> bool:
        """
        An archive this class built for a download, as opposed to anything else a person keeps
        in that folder: only these may be removed once they have been handed over.
        """
        return (
            GENERATED_ARCHIVE_NAME.search(os.path.basename(path)) is not None
            and os.path.dirname(path) == self._downloads_dir()
            and not os.path.islink(path)
        )

    @as_user
    def remove_generated_archive(self, path: str) -> None:
        if self.is_generated_archive(path):
            os.remove(path)

    @as_user_archive_build
    def create_download_archive(self, request: DownloadFilesRequest) -> Tuple[str, int]:
        """
        :return: the archive's path, and how many files could not be read and were left out
        """
        files = Utils.get_as_list(request.files, [])
        if not files:
            raise exceptions.invalid_params('file is required')

        download_list = []
        for file in files:
            if Utils.is_empty(file):
                continue
            if '..' in file.split('/'):
                raise exceptions.unauthorized_access()
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

        # Calculate total size and identify large files (including directory contents)
        total_size = 0
        large_files = []
        # only what passed the checks above is walked
        for file in download_list:
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

        downloads_dir = self._downloads_dir()
        if Utils.is_symlink(downloads_dir):
            # prevent symlink/hijack attacks
            raise exceptions.invalid_params(
                f'a symbolic link already exists at: {downloads_dir}'
            )

        # private to the person, whatever the server's umask: the archive holds their files
        os.makedirs(downloads_dir, mode=0o700, exist_ok=True)
        # one made by an earlier release keeps its old mode otherwise
        os.chmod(downloads_dir, 0o700)

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

        zip_filename = f'{zip_filename[: -len(".zip")]}_{secrets.token_hex(6)}.zip'
        zip_file_path = os.path.join(downloads_dir, zip_filename)
        self._archive_being_written = zip_file_path
        self._files_left_out = 0

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
        try:
            archive_fd = os.open(
                zip_file_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            archive_info = os.fstat(archive_fd)
            # the archive itself, however it is reached from the folders being archived
            self._archive_identity = (archive_info.st_dev, archive_info.st_ino)
            with (
                open(archive_fd, 'wb') as archive_file,
                ZipFile(
                    archive_file,
                    'w',
                    compression=ZIP_DEFLATED,
                    compresslevel=1,
                    allowZip64=True,
                ) as zip_archive,
            ):
                # Track file names to avoid conflicts
                used_names = set()

                for download_item in download_list:
                    if Utils.is_file(download_item):
                        # Handle single file
                        self._add_file_to_zip(zip_archive, download_item, used_names)
                    elif Utils.is_dir(download_item):
                        # Handle directory recursively
                        self._add_directory_to_zip(
                            zip_archive, download_item, used_names
                        )
        except BaseException:
            # includes being stopped for taking too long: half an archive is only quota used up
            if os.path.exists(zip_file_path):
                os.remove(zip_file_path)
            raise

        # Log successful completion of large downloads
        zip_size = os.path.getsize(zip_file_path)
        if total_size > 100 * 1024 * 1024:  # 100MB+
            self.logger.info(
                f'Archive creation complete - User: {self.username}, Archive: {zip_filename}, '
                f'Archive size: {zip_size // 1024 // 1024}MB from source: {total_size // 1024 // 1024}MB'
            )

        return zip_file_path, self._files_left_out

    @as_user
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
        # created only if nothing is there, decided by the create itself
        try:
            if is_folder:
                os.mkdir(create_path)
            else:
                with open(create_path, 'x'):
                    pass
        except FileExistsError:
            raise exceptions.invalid_params(
                f'{"directory" if is_folder else "file"}: {filename} already exists '
                f'under: {cwd}'
            )

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'File created - User: {self.username}, Type: {"directory" if is_folder else "file"}, Path: {create_path}'
            )

        return CreateFileResult()

    @as_user
    def delete_files(
        self, request: DeleteFilesRequest, allow_directories: bool = False
    ) -> DeleteFilesResult:
        """
        :param allow_directories: remove a folder with everything in it. FileBrowser.DeleteFiles
            leaves it False: the portal confirms files by name, and a file swapped for a folder
            of the same name before the confirmation must not take the folder with it. The
            measured-folder delete, which checks what it removes, passes True.
        """
        files = request.files
        if Utils.is_empty(files):
            raise exceptions.invalid_params('files[] is required')

        # what could not be deleted, and in a word or two why
        failures = []
        successfully_deleted = []

        for file in files:
            try:
                # unlink needs write on the folder, not on the file: a read-only file, or one
                # the server wrote and never handed over, is still the user's to delete
                self.check_access(
                    file, check_read=False, check_write=False, follow=False
                )
                self.check_access(
                    os.path.dirname(file.rstrip('/')) or '/',
                    check_dir=True,
                    check_read=False,
                    check_write=True,
                )

                # the type decided once, and the delete done by it: an entry that becomes
                # a folder after this look is refused by os.remove, never removed whole
                kind = os.lstat(file).st_mode
                if S_ISLNK(kind):
                    os.unlink(file)
                    self.logger.info(f'{self.username} deleted symlink: "{file}"')
                    successfully_deleted.append(file)
                elif S_ISDIR(kind) and not allow_directories:
                    failures.append((file, 'is a folder'))
                elif S_ISDIR(kind):
                    shutil.rmtree(file, ignore_errors=False)
                    self.logger.info(f'{self.username} deleted directory: "{file}"')
                    successfully_deleted.append(file)
                elif S_ISREG(kind):
                    os.remove(file)
                    self.logger.info(f'{self.username} deleted file: "{file}"')
                    successfully_deleted.append(file)
                else:
                    self.logger.warning(
                        f'{self.username} attempted to delete non-existent or unrecognized file: "{file}"'
                    )
                    failures.append((file, 'not found'))

            except FileNotFoundError:
                # File was already deleted or never existed
                self.logger.warning(
                    f'{self.username} attempted to delete already deleted file: "{file}"'
                )
                failures.append((file, 'not found'))
            except PermissionError as e:
                self.logger.error(
                    f'{self.username} permission denied deleting file: "{file}" - {str(e)}'
                )
                failures.append((file, 'permission denied'))
            except exceptions.SocaException as e:
                self.logger.warning(
                    f'{self.username} not allowed to delete file: "{file}" - {e.message}'
                )
                failures.append((file, 'permission denied'))
            except OSError as e:
                # Other OS-level errors (file in use, I/O errors, etc.)
                self.logger.error(
                    f'{self.username} OS error deleting file: "{file}" - {str(e)}'
                )
                failures.append((file, e.strerror or str(e)))
            except Exception as e:
                self.logger.error(
                    f'{self.username} unexpected error deleting file: "{file}" - {str(e)}',
                    exc_info=True,
                )
                failures.append((file, 'unexpected error'))

        if failures:
            self.logger.warning(
                f'{self.username} deletion completed with {len(failures)} errors. Successfully deleted: {len(successfully_deleted)}, Errors: {len(failures)}'
            )
            shown = ', '.join(
                f'{os.path.basename(file.rstrip("/"))} ({reason})'
                for file, reason in failures[:MAX_DELETE_FAILURES_SHOWN]
            )
            more = len(failures) - MAX_DELETE_FAILURES_SHOWN
            if more > 0:
                shown += f', and {more} more'
            raise exceptions.soca_exception(
                error_code=errorcodes.FILE_BROWSER_DELETE_FAILED,
                message=f'Could not delete {len(failures)} of {len(files)}: {shown}',
            )

        return DeleteFilesResult()

    @as_user
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

        # rename needs write on the folder, not on the file: a read-only file, or one the
        # server wrote and never handed over, is still the user's to rename, as to delete
        self.check_access(file, check_read=False, check_write=False, follow=False)
        self.check_access(
            os.path.dirname(file.rstrip('/')) or '/',
            check_dir=True,
            check_read=False,
            check_write=True,
        )

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
            refusal = os_refusal(e)
            if refusal is not None:
                raise refusal
            raise exceptions.general_exception('The file could not be renamed.')

        return RenameFileResult()

    @as_user
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
        current_time = time.time()
        window_start = current_time - TAIL_FILE_RATE_LIMIT_WINDOW

        with _tail_state_lock:
            recent = [
                t for t in _tail_rate_limits.get(username, []) if t > window_start
            ]

            # people tailing now: those with a request inside the window
            if not recent:
                active = sum(
                    1
                    for user, stamps in _tail_rate_limits.items()
                    if user != username and stamps and stamps[-1] > window_start
                )
                if active >= TAIL_FILE_MAX_CONCURRENT_USERS:
                    raise exceptions.soca_exception(
                        error_code=errorcodes.FILE_BROWSER_TAIL_THROTTLE,
                        message=f'Maximum concurrent tail users ({TAIL_FILE_MAX_CONCURRENT_USERS}) reached. Please try again later.',
                    )

            if len(recent) >= TAIL_FILE_MAX_REQUESTS_PER_WINDOW:
                _tail_rate_limits[username] = recent
                raise exceptions.soca_exception(
                    error_code=errorcodes.FILE_BROWSER_TAIL_THROTTLE,
                    message=f'Rate limit exceeded. Maximum {TAIL_FILE_MAX_REQUESTS_PER_WINDOW} tail requests per {TAIL_FILE_RATE_LIMIT_WINDOW} seconds.',
                )

            recent.append(current_time)
            _tail_rate_limits[username] = recent
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
        global _last_global_cleanup
        current_time = time.time()

        with _tail_state_lock:
            # Only cleanup every 30 seconds to avoid overhead
            if current_time - _last_global_cleanup < 30:
                return
            self._cleanup_tail_state(current_time)
            _last_global_cleanup = current_time

    def _cleanup_tail_state(self, current_time: float):
        global _tail_rate_limits

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

        final_users = len(_tail_rate_limits)
        # Only log if significant cleanup occurred
        if len(removed_users) > 5 or size_limited:
            self.logger.debug(
                f'Rate limit cleanup: {initial_users} -> {final_users} users'
            )

    def _skip_generated_archives(self, start: str, root: str, files: List[str]) -> None:
        """
        Archives built here don't belong in a new one, unless the downloads folder is what
        was asked for; a person's own files always do. Call from inside an os.walk.
        """
        downloads_dir = self._downloads_dir()
        if root == downloads_dir and start != downloads_dir:
            files[:] = [f for f in files if not GENERATED_ARCHIVE_NAME.search(f)]

    def _calculate_directory_size(self, directory_path: str) -> int:
        """Calculate the total size of all files in a directory recursively"""
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(directory_path):
                self._skip_generated_archives(directory_path, dirpath, filenames)
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    if self._in_system_folder(os.path.realpath(file_path)):
                        continue  # not archived either
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

    @staticmethod
    def _archive_name(*parts: str) -> str:
        # each name stays one name in the archive: a backslash in it is not a separator
        return '/'.join(part.replace('\\', '_') for part in parts if part)

    def _copy_into_archive(
        self, zip_archive: ZipFile, file_path: str, archive_name: str
    ) -> bool:
        """
        Adds one file. A file that cannot be read, or that is not a regular file (a FIFO
        would hold the build until its deadline), is left out and False returned; an error
        writing the archive, a full disk among them, is the build's and is raised.
        """
        try:
            fd = os.open(file_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return False
        with open(fd, 'rb') as source:
            info = os.fstat(fd)
            # a link in the folder into the container's own folders is left out, as a
            # direct read of it is refused
            if self._opened_in_system_folder(fd, file_path):
                return False
            if not S_ISREG(info.st_mode) or (
                (info.st_dev, info.st_ino) == self._archive_identity
            ):
                return False
            chunk_size = self._calculate_optimal_chunk_size(info.st_size)
            force_zip64 = info.st_size >= 2 * 1024 * 1024 * 1024  # 2GB threshold
            with zip_archive.open(archive_name, 'w', force_zip64=force_zip64) as entry:
                while True:
                    try:
                        chunk = source.read(chunk_size)
                    except OSError:
                        # what was read stays in the entry: the file is counted as left out
                        return False
                    if not chunk:
                        return True
                    entry.write(chunk)

    @staticmethod
    def _unique_name(base_name: str, used_names: set) -> str:
        """the name, or name_1, name_2...: two files never become one member"""
        file_name = base_name
        counter = 1
        while file_name in used_names:
            name, ext = os.path.splitext(base_name)
            file_name = f'{name}_{counter}{ext}'
            counter += 1
        used_names.add(file_name)
        return file_name

    def _add_file_to_zip(
        self,
        zip_archive: ZipFile,
        file_path: str,
        used_names: set,
        archive_path: str = None,
    ):
        """Add a single file to the zip archive"""
        if archive_path is None:
            base_name = self._archive_name(os.path.basename(file_path))
        else:
            base_name = archive_path

        file_name = self._unique_name(base_name, used_names)

        # Log if file name was changed due to conflicts
        if file_name != base_name:
            self.logger.debug(
                f'Archive conflict rename - User: {self.username}, File: {file_path}, Renamed: {base_name} -> {file_name}'
            )

        if not self._copy_into_archive(zip_archive, file_path, file_name):
            self._files_left_out += 1

    def _add_directory_to_zip(
        self, zip_archive: ZipFile, directory_path: str, used_names: set
    ):
        """Add a directory and all its contents to the zip archive recursively"""
        # Strip trailing slashes and get the directory name
        directory_path = directory_path.rstrip('/')
        directory_name = self._archive_name(os.path.basename(directory_path))

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

        # Verify directory exists and is accessible
        if not os.path.exists(directory_path):
            raise exceptions.file_not_found(directory_path)
        if not os.path.isdir(directory_path):
            raise exceptions.soca_exception(
                error_code=errorcodes.INVALID_PARAMS,
                message=f'Path is not a directory: {directory_path}',
            )

        def unreadable_folder(_):
            # a folder that cannot be read is left out, and counted like a file
            self._files_left_out += 1

        # Walk through the directory and add all files
        files_added = 0
        for root, dirs, files in os.walk(directory_path, onerror=unreadable_folder):
            self._skip_generated_archives(directory_path, root, files)
            # Calculate relative path from the source directory
            rel_dir = os.path.relpath(root, directory_path)
            archive_dir = self._archive_name(
                directory_name, *([] if rel_dir == '.' else rel_dir.split(os.sep))
            )

            # a folder entry for each folder, the selected one and empty ones included
            if archive_dir + '/' not in used_names:
                zip_archive.writestr(archive_dir + '/', '')
                used_names.add(archive_dir + '/')

            # Add all files in this directory
            for filename in files:
                file_path = os.path.join(root, filename)
                if file_path == self._archive_being_written:
                    continue
                if self._copy_into_archive(
                    zip_archive,
                    file_path,
                    # a backslash name and its _ form in one folder would both be a_b
                    self._unique_name(
                        self._archive_name(archive_dir, filename), used_names
                    ),
                ):
                    files_added += 1
                else:
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug('Skipping a file that cannot be read')
                    self._files_left_out += 1

        # Log completion
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f'Directory archive complete - User: {self.username}, Directory: {directory_path}, Files added: {files_added}'
            )
