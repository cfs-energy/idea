import os
from contextlib import contextmanager
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ideadatamodel import exceptions
from ideadatamodel.filesystem import DeleteFilesRequest
from ideasdk.filesystem.filesystem_helper import FileSystemHelper
from ideasdk.filesystem.user_identity import UserIdentity


# The entry, time and depth caps bound work on very large homes.
# A partial snapshot preserves useful counts without tying up a worker indefinitely.
MAX_ENTRIES = 100_000
MAX_SECONDS = 10
MAX_DEPTH = 128
CACHE_SECONDS = 3600


def summary(path, mtime=None):
    return dict(
        path=path,
        name=os.path.basename(path),
        bytes=0,
        files=0,
        newest_mtime=mtime,
        oldest_mtime=None,
        unchanged_90_days_bytes=0,
        partial=False,
    )


def merge(parent, child):
    for key in ('bytes', 'files', 'unchanged_90_days_bytes'):
        parent[key] += child[key]
    for key, choose in (('newest_mtime', max), ('oldest_mtime', min)):
        values = [v for v in (parent[key], child[key]) if v is not None]
        parent[key] = choose(values) if values else None
    parent['partial'] |= child['partial']


def walk_home(
    home, max_entries=MAX_ENTRIES, max_seconds=MAX_SECONDS, now=None, root_fd=None
):
    measured_at = time.time() if now is None else now
    cutoff = measured_at - 90 * 86400
    deadline = time.monotonic() + max_seconds
    directories = {home: summary(home)}
    stack = []
    incomplete = set()
    entries = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def enter(path, fd):
        row = directories[path]
        try:
            info = os.fstat(fd)
            row['newest_mtime'] = info.st_mtime
            row['identity'] = directory_identity(info)
            row['partial'] = True
            stack.append((path, fd, os.scandir(fd)))
        except OSError:
            os.close(fd)
            raise

    try:
        enter(home, os.open(home, flags) if root_fd is None else os.dup(root_fd))
        while stack:
            path, fd, iterator = stack[-1]
            row = directories[path]
            if entries >= max_entries or time.monotonic() >= deadline:
                break
            try:
                entry = next(iterator)
            except StopIteration:
                row['partial'] = path in incomplete
                iterator.close()
                os.close(fd)
                stack.pop()
                continue
            entries += 1
            child_path = os.path.join(path, entry.name)
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = directories[child_path] = summary(child_path, info.st_mtime)
                    if len(stack) >= MAX_DEPTH:
                        child['partial'] = True
                        continue
                    # Relative descriptors and O_NOFOLLOW prevent a renamed directory or
                    # a replacement symlink from redirecting the walk outside this home.
                    enter(child_path, os.open(entry.name, flags, dir_fd=fd))
                elif stat.S_ISREG(info.st_mode):
                    merge(
                        row,
                        dict(
                            bytes=info.st_size,
                            files=1,
                            newest_mtime=info.st_mtime,
                            oldest_mtime=info.st_mtime,
                            unchanged_90_days_bytes=info.st_size
                            if info.st_mtime <= cutoff
                            else 0,
                            partial=False,
                        ),
                    )
            except OSError:
                incomplete.add(path)
                if child_path in directories:
                    directories[child_path]['partial'] = True
                    directories[child_path]['unreadable'] = True
    except OSError:
        directories[home]['partial'] = True
        directories[home]['unreadable'] = True
    finally:
        for _, fd, iterator in reversed(stack):
            iterator.close()
            os.close(fd)

    for path in reversed(list(directories)):
        if path != home:
            merge(directories[os.path.dirname(path)], directories[path])
    total = directories[home]
    return dict(
        state='ready',
        home=home,
        measured_at=measured_at,
        partial=total['partial'],
        total=total,
        folders=[
            row
            for path, row in sorted(directories.items())
            if path != home and os.path.dirname(path) == home
        ],
        directories=directories,
    )


def directory_identity(info):
    # The inode only: each task's NFS mount carries its own device number, so a folder measured by
    # one task and deleted through another would look replaced.
    return dict(inode=str(info.st_ino))


@contextmanager
def open_directory(home, folder):
    home = os.path.abspath(home)
    folder = os.path.abspath(folder)
    if os.path.commonpath([home, folder]) != home:
        raise exceptions.unauthorized_access()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(home, flags)
    try:
        relative = os.path.relpath(folder, home)
        for component in [] if relative == '.' else relative.split(os.sep):
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def scan_as_user(identity, home, folder):
    def scan():
        with open_directory(home, folder) as directory:
            return walk_home(folder, root_fd=directory)

    return identity.run(scan, timeout=MAX_SECONDS + 10)


class StorageUsageService:
    def __init__(self, context):
        self.context = context
        self._cache = {}
        self._lock = threading.Lock()
        self._workers = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='storage-usage'
        )

    def get_usage(self, username, folder=None):
        helper = FileSystemHelper(self.context, username)
        home = helper.get_user_home()
        home = os.path.normpath(home)
        if folder is not None:
            if not isinstance(folder, str) or not os.path.isabs(folder):
                raise exceptions.invalid_params(
                    'folder must be an absolute path in your home'
                )
            folder = os.path.normpath(folder)
            if os.path.commonpath([home, folder]) != home or folder == home:
                raise exceptions.unauthorized_access()
        identity = UserIdentity(self.context.accounts, username)
        if folder is not None:
            result = scan_as_user(identity, home, folder)
            result['folder'] = result['total']
            result['home'] = home
            return {k: v for k, v in result.items() if k != 'directories'}
        key = (username, home)
        with self._lock:
            now = time.monotonic()
            self._cache = {
                k: v
                for k, v in self._cache.items()
                if not v['future'].done() or now - v['created'] < CACHE_SECONDS
            }
            cached = self._cache.get(key)
            if cached is None:
                self._cache[key] = dict(
                    created=now,
                    future=self._workers.submit(scan_as_user, identity, home, home),
                )
                return dict(state='computing', home=home)
            future = cached['future']
        if not future.done():
            return dict(state='computing', home=home)
        try:
            result = future.result()
        except Exception:
            with self._lock:
                if self._cache.get(key) is cached:
                    del self._cache[key]
            self.context.logger('storage-usage').exception('Storage usage scan failed')
            return dict(
                state='error', home=home, message='Storage usage could not be read.'
            )
        response = {k: v for k, v in result.items() if k != 'directories'}
        return response

    def measure_for_costs(self, username):
        """Wait only in a background cost worker, using the browser's identical scan."""
        result = self.get_usage(username)
        if result['state'] == 'computing':
            with self._lock:
                entry = self._cache.get((username, result['home']))
            if entry is not None:
                result = entry['future'].result(timeout=MAX_SECONDS + 30)
        return result

    def delete_folder(self, username, path, identity):
        helper = FileSystemHelper(self.context, username)
        home = os.path.normpath(helper.get_user_home())
        if (
            not isinstance(path, str)
            or not os.path.isabs(path)
            or os.path.normpath(path) == home
        ):
            raise exceptions.invalid_params('A folder inside your home is required')

        def delete():
            with open_directory(home, path) as fd:
                measured = identity.get('inode') if isinstance(identity, dict) else None
                if measured != directory_identity(os.fstat(fd))['inode']:
                    raise exceptions.invalid_params(
                        'Folder changed since it was measured'
                    )
                return helper.delete_files(
                    DeleteFilesRequest(files=[os.path.realpath(path)])
                )

        return helper.run_as_user(delete)
