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
File system access for scratch cleanup. Everything goes through open directory descriptors:
a name is resolved one level at a time with O_NOFOLLOW, so a path swapped for a link after it
was checked cannot redirect a walk or a removal out of the folder that was checked. A subfolder
is entered only if what opens is the directory that was inspected, so a replacement directory is
never walked either. Metadata only: no file is ever opened for reading.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Optional, Tuple
import errno
import os
import stat

from ideascheduler.app.scratch_cleanup.rules import FolderContents

_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY
MAX_DEPTH = 256
PACE_EVERY_ENTRIES = 5000
CHECK_EVERY_OPERATIONS = 100


@dataclass
class RemovalProgress:
    """read after an exception: how much of the folder went before it stopped"""

    entries: int = 0


class ScratchFsError(Exception):
    pass


class NotASafeTarget(ScratchFsError):
    """the name is not a directory reached without following anything"""


class NotAsInspected(NotASafeTarget):
    """a directory has the name, and it is not the one that was inspected"""


class FolderInUse(ScratchFsError):
    """something wrote to the folder while it was being removed"""


@dataclass(frozen=True)
class FolderIdentity:
    device: int
    inode: int
    uid: int


def open_child_dir(parent_fd: int, name: str) -> int:
    if name in ('', '.', '..') or '/' in name or '\0' in name:
        raise NotASafeTarget(f'refused name: {name!r}')
    try:
        return os.open(name, _OPEN_DIR, dir_fd=parent_fd)
    except OSError as e:
        # ELOOP on Linux, EMLINK on some BSDs for a link under O_NOFOLLOW
        if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK, errno.ENOENT):
            raise NotASafeTarget(f'not a directory: {name!r}') from e
        raise


def identity_of(fd: int) -> FolderIdentity:
    info = os.fstat(fd)
    return FolderIdentity(device=info.st_dev, inode=info.st_ino, uid=info.st_uid)


def _open_as_inspected(parent_fd: int, name: str, seen: os.stat_result) -> int:
    """the subfolder that was inspected, or NotASafeTarget: something else now has its name."""
    fd = open_child_dir(parent_fd, name)
    opened = os.fstat(fd)
    # the ctime too: a directory made in its place can be given the same inode
    if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_ctime_ns) != (
        seen.st_dev,
        seen.st_ino,
        seen.st_uid,
        seen.st_ctime_ns,
    ):
        os.close(fd)
        raise NotAsInspected(f'{name!r} is not the directory that was inspected')
    return fd


def list_directory(fd: int) -> Iterator[Tuple[str, bool]]:
    """(name, is a real directory), never following a link"""
    with os.scandir(fd) as entries:
        for entry in entries:
            yield entry.name, entry.is_dir(follow_symlinks=False)


def _utc(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _timestamp(info: os.stat_result) -> float:
    return max(info.st_mtime, info.st_ctime)


class _Stop(Exception):
    def __init__(self, contents: FolderContents):
        super().__init__()
        self.contents = contents


def inspect_folder(
    folder_fd: int,
    root_device: int,
    owner_uid: int,
    budget_entries: int,
    pace: Optional[Callable[[], None]] = None,
) -> FolderContents:
    """size, newest change and anything that disqualifies the folder"""
    info = os.fstat(folder_fd)
    size = info.st_blocks * 512
    newest = _timestamp(info)
    oldest = info.st_ctime
    newest_dir = info.st_ctime
    newest_file: Optional[float] = None
    newest_access: Optional[float] = None
    other_owner = info.st_uid != owner_uid
    entries = 0

    def visit(fd: int) -> List[Tuple[str, os.stat_result]]:
        nonlocal size, newest, oldest, newest_dir, newest_file, newest_access
        nonlocal other_owner, entries
        subfolders = []
        for name, is_dir in list_directory(fd):
            entries += 1
            if entries > budget_entries:
                raise _Stop(FolderContents(over_budget=True, entries=entries))
            if pace and entries % PACE_EVERY_ENTRIES == 0:
                pace()
            child = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if child.st_dev != root_device:
                raise _Stop(FolderContents(other_mount=True, entries=entries))
            size += child.st_blocks * 512
            newest = max(newest, _timestamp(child))
            oldest = min(oldest, child.st_ctime)
            if is_dir:
                newest_dir = max(newest_dir, child.st_ctime)
            else:
                changed = _timestamp(child)
                newest_file = (
                    changed if newest_file is None else max(newest_file, changed)
                )
                newest_access = (
                    child.st_atime
                    if newest_access is None
                    else max(newest_access, child.st_atime)
                )
            other_owner = other_owner or child.st_uid != owner_uid
            if is_dir:
                subfolders.append((name, child))
        return subfolders

    # (descriptor, subfolders still to visit); the first descriptor stays the caller's
    stack: List[Tuple[int, List[Tuple[str, os.stat_result]]]] = []
    try:
        stack.append((folder_fd, visit(folder_fd)))
        while stack:
            fd, subfolders = stack[-1]
            if not subfolders:
                stack.pop()
                if fd != folder_fd:
                    os.close(fd)
                continue
            if len(stack) >= MAX_DEPTH:
                raise _Stop(FolderContents(over_budget=True, entries=entries))
            child_name, seen = subfolders.pop()
            child_fd = _open_as_inspected(fd, child_name, seen)
            stack.append((child_fd, []))
            if os.fstat(child_fd).st_dev != root_device:
                raise _Stop(FolderContents(other_mount=True, entries=entries))
            stack[-1] = (child_fd, visit(child_fd))
    except _Stop as stop:
        return stop.contents
    except (OSError, NotASafeTarget):
        return FolderContents(unreadable=True, entries=entries)
    finally:
        for fd, _ in stack:
            if fd != folder_fd:
                os.close(fd)
    return FolderContents(
        size_bytes=size,
        entries=entries,
        newest_change=_utc(newest),
        oldest_change=_utc(oldest),
        newest_dir_change=_utc(newest_dir),
        newest_access=None if newest_access is None else _utc(newest_access),
        newest_file_change=None if newest_file is None else _utc(newest_file),
        other_owner=other_owner,
    )


def _same_folder(parent_fd: int, name: str, expected: FolderIdentity):
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as e:
        raise NotASafeTarget(f'{name!r} was moved during removal') from e
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        expected.device,
        expected.inode,
    ):
        raise NotASafeTarget(f'{name!r} was replaced during removal')


def remove_folder(
    parent_fd: int,
    name: str,
    folder_fd: int,
    expected: FolderIdentity,
    keep_going: Callable[[], None],
    *,
    not_before: float,
    not_after: float,
    dirs_not_after: float,
    progress: Optional[RemovalProgress] = None,
    before_each: Optional[Callable[[], None]] = None,
) -> int:
    """
    empty the open folder and remove its name from the open parent, returning the entries
    removed. keep_going runs every so often and raises to stop; what is left stays in place,
    and the audit log's started row lets a later run resume after re-checking the job.

    The folder was walked before this, so anything moved in since has to be caught here: an
    entry whose times fall outside the job's life stops the removal. not_after applies to a
    file's mtime and ctime, dirs_not_after to a directory's ctime, not_before to any ctime.
    """
    actual = identity_of(folder_fd)
    if (actual.device, actual.inode) != (expected.device, expected.inode):
        raise NotASafeTarget(f'{name!r} is no longer the folder that was checked')
    # an owner moving the folder out of the way must be able to save it, even now
    _same_folder(parent_fd, name, expected)
    progress = progress or RemovalProgress()
    since_check = 0

    # (descriptor, its name in the level above, what was inspected under that name, subfolders)
    stack: List[
        Tuple[
            int,
            Optional[str],
            Optional[os.stat_result],
            List[Tuple[str, os.stat_result]],
        ]
    ] = []

    def name_still_means(parent: int, child: str, seen: os.stat_result):
        try:
            current = os.stat(child, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as e:
            raise FolderInUse(f'something in {name!r} was moved during removal') from e
        if (current.st_dev, current.st_ino) != (seen.st_dev, seen.st_ino):
            raise FolderInUse(f'something in {name!r} was replaced during removal')

    def still_in_place():
        """
        a moved directory is still open here: only the names, from the folder down, show it has
        left. Before every delete, since removal is not in a hurry and the wrong delete cannot be
        taken back. What moves between this and the delete itself is out of reach: nothing here
        can stop a rename, though a hold keeps the owner's jobs from starting.
        """
        _same_folder(parent_fd, name, expected)
        for level, entry in enumerate(stack):
            if entry[1] is not None:
                name_still_means(stack[level - 1][0], entry[1], entry[2])

    def step():
        """counts every operation, so no shape of tree can avoid the checks"""
        nonlocal since_check
        progress.entries += 1
        since_check += 1
        if since_check >= CHECK_EVERY_OPERATIONS:
            since_check = 0
            keep_going()

    def empty_level(fd: int) -> List[Tuple[str, os.stat_result]]:
        """unlink everything but subfolders, which are returned with what was inspected"""
        subfolders = []
        entries = list(list_directory(fd))
        still_in_place()
        for child, is_dir in entries:
            # a directory someone else owns can be moved in after the folder was checked
            found = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if found.st_uid != expected.uid or found.st_dev != expected.device:
                raise FolderInUse(f'{name!r} changed during removal')
            latest = found.st_ctime if is_dir else _timestamp(found)
            if (
                latest > (dirs_not_after if is_dir else not_after)
                or found.st_ctime < not_before
            ):
                raise FolderInUse(f'{name!r} holds something from outside the job')
            if is_dir:
                subfolders.append((child, found))
                continue
            if before_each:
                before_each()
            still_in_place()
            os.unlink(child, dir_fd=fd)
            step()
        return subfolders

    try:
        stack.append((folder_fd, None, None, empty_level(folder_fd)))
        while stack:
            fd, own_name, seen, subfolders = stack[-1]
            if not subfolders:
                stack.pop()
                if own_name is not None:
                    os.close(fd)
                    if before_each:
                        before_each()
                    # the name, not the descriptor, is what rmdir acts on
                    name_still_means(stack[-1][0], own_name, seen)
                    still_in_place()
                    os.rmdir(own_name, dir_fd=stack[-1][0])
                    step()
                continue
            if len(stack) >= MAX_DEPTH:
                raise FolderInUse('the folder is nested too deeply to remove')
            child, child_seen = subfolders.pop()
            try:
                child_fd = _open_as_inspected(fd, child, child_seen)
            except NotAsInspected as e:
                raise FolderInUse(str(e)) from e
            stack.append((child_fd, child, child_seen, []))
            opened = os.fstat(child_fd)
            if opened.st_dev != expected.device:
                raise FolderInUse('another file system is mounted inside')
            if opened.st_ctime > dirs_not_after or opened.st_ctime < not_before:
                raise FolderInUse(f'{name!r} holds something from outside the job')
            name_still_means(fd, child, child_seen)
            stack[-1] = (child_fd, child, child_seen, empty_level(child_fd))
    except OSError as e:
        if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
            raise FolderInUse(f'something wrote to {name!r} during removal') from e
        raise
    finally:
        for fd, own_name, _, _ in stack:
            if own_name is not None:
                os.close(fd)

    if before_each:
        before_each()
    _same_folder(parent_fd, name, expected)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as e:
        if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
            raise FolderInUse(f'something wrote to {name!r} during removal') from e
        raise
    progress.entries += 1
    return progress.entries


def usage(mount_dir: str) -> Tuple[int, int, float]:
    """(capacity, used, percent used) the way df counts them"""
    info = os.statvfs(mount_dir)
    used = (info.f_blocks - info.f_bfree) * info.f_frsize
    available = info.f_bavail * info.f_frsize
    capacity = used + available
    percent = 100.0 * used / capacity if capacity > 0 else 0.0
    return capacity, used, percent


def lustre_mount_problem(
    mount_dir: str,
    mount_name: Optional[str],
    mountinfo_path: str = '/proc/self/mountinfo',
) -> Optional[str]:
    """
    why this path is not the configured Lustre file system, or None. A host whose mount failed
    leaves an empty local directory; nothing is ever decided from one of those.
    """
    mounted = None
    try:
        with open(mountinfo_path) as mountinfo:
            for line in mountinfo:
                left, _, right = line.partition(' - ')
                fields = left.split()
                details = right.split()
                # a later entry on the same point covers an earlier one
                if (
                    len(fields) > 4
                    and len(details) > 1
                    and fields[4].rstrip('/') == mount_dir.rstrip('/')
                ):
                    mounted = details
    except OSError as e:
        return f'The mount table could not be read: {e}'
    if mounted is None:
        return f'{mount_dir} is not mounted here.'
    if mounted[0] != 'lustre':
        return f'{mount_dir} is {mounted[0]}, not Lustre.'
    if mount_name and not mounted[1].endswith(f':/{mount_name}'):
        return f'{mount_dir} is not the configured Lustre file system.'
    return None
