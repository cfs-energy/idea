import heapq
import os
from collections import OrderedDict
from contextlib import contextmanager
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ideadatamodel import exceptions
from ideadatamodel.filesystem import DeleteFilesRequest
from ideasdk.filesystem.filesystem_helper import FileSystemHelper
from ideasdk.filesystem.user_identity import UserIdentity
from ideaclustermanager.app.filesystem.ontap_analytics import FolderAnalytics
from ideaclustermanager.app.metrics.storage_metrics_service import StorageMetricsService


# The entry, time and depth caps bound work on very large homes.
# A partial snapshot preserves useful counts without tying up a worker indefinitely.
# This scan is the fallback for volumes without ONTAP analytics; see ontap_analytics.py.
MAX_ENTRIES = 100_000
MAX_SECONDS = 10
MAX_DEPTH = 128
CACHE_SECONDS = 3600
REFRESH_SECONDS = 60
COSTS_WAIT_SECONDS = 120
SHARED_FOLDER = 'shared'
STUDY_FOLDERS = 200
STUDY_SECONDS = 900
STUDY_REFRESH_SECONDS = 60
MAX_CONCURRENT_STUDIES = 2
MAX_LARGEST = 25
MIN_LARGEST_BYTES = 2**20
OUTSIDE_HOME_MESSAGES = dict(
    on='Storage analytics could not read this folder.',
    failed='Storage analytics could not be reached. Try again in a minute.',
    initializing='Storage analytics is still taking its first measurement of this volume. Try again later.',
)
MAX_CACHED_VIEWS = 1000
# what a person may open is asked again this often, so a permission change shows within a minute
# and the pane's one-second polls during a study do not each fork to ask
VIEW_SECONDS = 60
# to pass through a folder on the way to another takes search permission on it, not read
THROUGH = getattr(os, 'O_PATH', getattr(os, 'O_SEARCH', os.O_RDONLY))
CANNOT_OPEN = 'That folder no longer exists or cannot be opened.'


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
    entries = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def measure(row, info):
        merge(
            row,
            dict(
                bytes=info.st_size,
                files=1,
                newest_mtime=info.st_mtime,
                oldest_mtime=info.st_mtime,
                unchanged_90_days_bytes=info.st_size if info.st_mtime <= cutoff else 0,
                partial=False,
            ),
        )

    def enter(path, fd, stack):
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

    def walk(root_path, root_descriptor):
        # Relative descriptors and O_NOFOLLOW prevent a renamed directory or a replacement
        # symlink from redirecting the walk outside this home.
        nonlocal entries
        stack = []
        incomplete = set()
        try:
            enter(root_path, root_descriptor, stack)
            while stack:
                path, fd, iterator = stack[-1]
                row = directories[path]
                if entries >= max_entries or time.monotonic() >= deadline:
                    return False
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
                        child = directories[child_path] = summary(
                            child_path, info.st_mtime
                        )
                        if len(stack) >= MAX_DEPTH - 1:
                            child['partial'] = True
                            continue
                        enter(child_path, os.open(entry.name, flags, dir_fd=fd), stack)
                    elif stat.S_ISREG(info.st_mode):
                        measure(row, info)
                except OSError:
                    incomplete.add(path)
                    if child_path in directories:
                        directories[child_path]['partial'] = True
                        directories[child_path]['unreadable'] = True
            return True
        finally:
            for _, fd, iterator in reversed(stack):
                iterator.close()
                os.close(fd)

    home_fd = None
    try:
        home_fd = os.open(home, flags) if root_fd is None else os.dup(root_fd)
        info = os.fstat(home_fd)
        total = directories[home]
        total['newest_mtime'] = info.st_mtime
        total['identity'] = directory_identity(info)
        total['partial'] = True
        # The whole top level is listed before any folder is entered, whatever the limits, so a
        # cutoff can leave a folder unmeasured but cannot leave it out.
        pending = []
        home_incomplete = False
        with os.scandir(home_fd) as iterator:
            for entry in iterator:
                entries += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child_path = os.path.join(home, entry.name)
                        child = directories[child_path] = summary(
                            child_path, info.st_mtime
                        )
                        child['partial'] = True
                        child['measured'] = False
                        pending.append((child_path, entry.name))
                    elif stat.S_ISREG(info.st_mode):
                        measure(total, info)
                except OSError:
                    home_incomplete = True
        complete = True
        for child_path, name in sorted(pending):
            if entries >= max_entries or time.monotonic() >= deadline:
                complete = False
                break
            directories[child_path]['measured'] = True
            try:
                if not walk(child_path, os.open(name, flags, dir_fd=home_fd)):
                    complete = False
                    break
            except OSError:
                home_incomplete = True
                directories[child_path]['partial'] = True
                directories[child_path]['unreadable'] = True
        total['partial'] = home_incomplete or not complete
    except OSError:
        directories[home]['partial'] = True
        directories[home]['unreadable'] = True
    finally:
        if home_fd is not None:
            os.close(home_fd)

    for path in reversed(list(directories)):
        if path != home:
            merge(directories[os.path.dirname(path)], directories[path])
    total = directories[home]
    return dict(
        state='ready',
        home=home,
        measured_at=measured_at,
        partial=total['partial'],
        scanned_files=total['files'],
        total=total,
        folders=sorted(
            (
                row
                for path, row in directories.items()
                if path != home and os.path.dirname(path) == home
            ),
            key=lambda row: (-row['bytes'], row['name']),
        ),
        directories=directories,
    )


def directory_identity(info):
    # The inode only: each task's NFS mount carries its own device number, so a folder measured by
    # one task and deleted through another would look replaced.
    return dict(inode=str(info.st_ino))


@contextmanager
def open_directory(home, folder, trusted_root=False):
    home = os.path.abspath(home)
    folder = os.path.abspath(folder)
    if os.path.commonpath([home, folder]) != home:
        raise exceptions.unauthorized_access()
    relative = os.path.relpath(folder, home)
    components = [] if relative == '.' else relative.split(os.sep)
    # Only the folder asked for is opened to read. One above it is passed through, as a shell
    # would: a parent that can be entered but not listed does not close what is inside it.
    flags = [THROUGH | os.O_DIRECTORY | os.O_NOFOLLOW] * len(components)
    flags.append(os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    # The mount point comes from cluster settings, a person's home folder does not.
    fd = os.open(home, flags[0] & ~os.O_NOFOLLOW if trusted_root else flags[0])
    try:
        for component, mode in zip(components, flags[1:]):
            child = os.open(component, mode, dir_fd=fd)
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


def inspect_as_user(identity, root, folder, names):
    """What this person can reach, by opening it as them: the folder's identity for deletion, and
    which of its children they could list. ONTAP's login sees the whole SVM, this does not."""

    def inspect():
        readable = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with open_directory(root, folder, trusted_root=True) as directory:
            info = os.fstat(directory)
            for name in names:
                try:
                    os.close(os.open(name, flags, dir_fd=directory))
                    readable.append(name)
                except OSError:
                    pass
        return directory_identity(info), readable

    return identity.run(inspect, timeout=MAX_SECONDS + 10)


def readable_paths_as_user(identity, root, paths):
    def check():
        readable = []
        for path in paths:
            try:
                with open_directory(root, path, trusted_root=True):
                    readable.append(path)
            except OSError:
                pass
        return readable

    return identity.run(check, timeout=MAX_SECONDS + 10)


class StorageUsageService:
    def __init__(self, context):
        self.context = context
        self._cache = {}
        self._lock = threading.Lock()
        # One scan at a time per task: many people walking the volume together is what pegs its IO.
        self._workers = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='storage-usage'
        )
        self._analytics_source = None
        self._studies = {}
        self._study_workers = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_STUDIES, thread_name_prefix='storage-study'
        )
        self._views = OrderedDict()

    def _analytics(self):
        if self._analytics_source is None:
            collector = getattr(self.context, 'storage_metrics', None)
            if isinstance(collector, StorageMetricsService) and collector.is_enabled():
                self._analytics_source = FolderAnalytics(self.context, collector)
        return self._analytics_source

    def _view(self, username, identity, mount, path, fresh):
        """One folder from analytics, cut down to what this person can reach, or None."""
        analytics = self._analytics()
        if analytics is None:
            return None
        found = analytics.folder(path, fresh=fresh)
        if found is None:
            return None
        key = (username, path, found['measured_at'])
        with self._lock:
            checked_at, view = self._views.get(key, (0, None))
        if view is None or time.monotonic() - checked_at >= VIEW_SECONDS:
            names = [row['name'] for row in found['children']]
            try:
                identity_row, readable = inspect_as_user(identity, mount, path, names)
            except OSError:
                raise exceptions.unauthorized_access()
            readable = set(readable)
            # A folder this person cannot open gets no row: its name is theirs to see, a size from
            # ONTAP's login is not. Its bytes stay in the total, ONTAP's rollup of everything
            # below, and in every row above it, so the total less the rows here and the files in
            # this folder, which the person can stat, is what the hidden folders hold together.
            # Taking them out would need a walk as this person.
            children = [row for row in found['children'] if row['name'] in readable]
            view = dict(
                total=dict(found['self'], identity=identity_row),
                folders=children,
                measured_at=found['measured_at'],
            )
            if len(children) < len(names):
                view['hidden_folders'] = len(names) - len(children)
            truncated = bool(found.get('truncated'))
            view['truncated'] = truncated
            # the rest of the parent's total is direct files only if every child is here, with its
            # size known and visible to this person
            if (
                not truncated
                and len(readable) == len(names)
                and not found['self'].get('partial')
                and not any(row.get('partial') for row in found['children'])
            ):
                view['files_here'] = dict(
                    bytes=max(
                        0,
                        found['self']['bytes']
                        - sum(row['bytes'] for row in found['children']),
                    ),
                    files=max(
                        0,
                        found['self']['files']
                        - sum(row['files'] for row in found['children']),
                    ),
                )
            with self._lock:
                self._views[key] = (time.monotonic(), view)
                self._views.move_to_end(key)
                while len(self._views) > MAX_CACHED_VIEWS:
                    self._views.popitem(last=False)
        return view

    def _study(self, study, username, identity, mount, home, fresh):
        """Largest-first descent: ONTAP calls only, no file is touched, so it can run for everyone.
        ONTAP's login sees the whole SVM, so a folder is opened as this person before it is entered
        or named in the progress they poll."""
        progress = study['progress']
        analytics = self._analytics()
        try:
            if not readable_paths_as_user(identity, mount, [home]):
                return
            root = analytics.folder(home, fresh=fresh)
            if root is None:
                return
            floor = max(MIN_LARGEST_BYTES, root['self']['bytes'] // 100)
            queue = [(-root['self']['bytes'], home, root['self'])]
            hot = []
            progress['steps'][1]['state'] = 'done'
            progress['steps'][2]['state'] = 'running'
            while queue and progress['done'] < STUDY_FOLDERS:
                size, path, row = heapq.heappop(queue)
                relative = os.path.relpath(path, home)
                progress['step'] = (
                    'Checking your home folder'
                    if relative == '.'
                    else f'Checking {relative}'
                )
                found = analytics.folder(path, fresh=fresh)
                progress['done'] += 1
                if found is None:
                    continue
                large = [c for c in found['children'] if c['bytes'] >= floor]
                if large:
                    readable = set(
                        readable_paths_as_user(
                            identity, mount, [c['path'] for c in large]
                        )
                    )
                    large = [c for c in large if c['path'] in readable]
                # A folder with no single dominant child is where the deletion decision sits.
                if path != home and (not large or large[0]['bytes'] < -size // 2):
                    hot.append(row)
                for child in large:
                    heapq.heappush(queue, (-child['bytes'], child['path'], child))
                progress['total'] = max(
                    progress['done'], min(STUDY_FOLDERS, progress['done'] + len(queue))
                )
            hot.sort(key=lambda row: -row['bytes'])
            paths = [row['path'] for row in hot[: MAX_LARGEST * 2]]
            readable = set(readable_paths_as_user(identity, mount, paths))
            study['largest'] = [row for row in hot if row['path'] in readable][
                :MAX_LARGEST
            ]
            progress['total'] = progress['done']
            progress['step'] = 'Finished'
            progress['steps'][2]['state'] = 'done'
        except Exception as e:
            # Names and paths stay out of the log: only the kind of failure is recorded.
            self.context.logger('storage-usage').warning(
                f'storage study failed: {type(e).__name__}'
            )
        finally:
            progress['running'] = False
            study['finished'] = time.monotonic()

    def _study_for(self, username, identity, mount, home, refresh):
        now = time.monotonic()
        with self._lock:
            study = self._studies.get(username)
            # A running study or one this recent already answers a Refresh.
            stale = study is None or (
                study['finished'] is not None
                and now - study['finished']
                >= (STUDY_REFRESH_SECONDS if refresh else STUDY_SECONDS)
            )
            if stale:
                study = dict(
                    finished=None,
                    largest=None if study is None else study.get('largest'),
                    progress=dict(
                        running=True,
                        step='Reading your home folder',
                        done=0,
                        total=1,
                        started_at=time.time(),
                        steps=[
                            dict(label='Read your quota total', state='done'),
                            dict(label='Read your home folder', state='running'),
                            dict(label='Find your biggest folders', state='waiting'),
                        ],
                    ),
                )
                self._studies[username] = study
                self._study_workers.submit(
                    self._study, study, username, identity, mount, home, refresh
                )
        return study

    def _usage_from_analytics(
        self, username, identity, mount, home, folder, refresh, study
    ):
        target = folder or home
        # Opened as this person before ONTAP is asked, and one answer whether it is missing or
        # closed to them: ONTAP sees every name, so its answer would say which names exist.
        if (
            folder is not None
            and self._analytics() is not None
            and not readable_paths_as_user(identity, mount, [folder])
        ):
            raise exceptions.invalid_params(CANNOT_OPEN)
        view = self._view(username, identity, mount, target, refresh)
        if view is None:
            return None
        result = dict(
            state='ready',
            source='analytics',
            home=home,
            measured_at=view['measured_at'],
            # the total is ONTAP's rollup for the folder itself, whole even when its list is cut
            partial=bool(view['total'].get('partial')),
            total=view['total'],
            folders=view['folders'],
        )
        if view['truncated']:
            result['truncated'] = True
        if 'files_here' in view:
            result['files_here'] = view['files_here']
        if 'hidden_folders' in view:
            result['hidden_folders'] = view['hidden_folders']
        if folder is not None:
            result['folder'] = view['total']
            return result
        if not study:
            return result
        running = self._study_for(username, identity, mount, home, refresh)
        result['progress'] = running['progress']
        if running['largest']:
            result['largest'] = running['largest']
        status = self._analytics().status(home)
        result['volume'] = status.get('volume')
        target = status.get('target')
        if target is not None:
            result['volume_endpoint'] = target.endpoint
        try:
            shared = self._view(
                username, identity, mount, os.path.join(mount, SHARED_FOLDER), refresh
            )
        except exceptions.SocaException:
            shared = None
        if shared and shared['folders']:
            result['shared'] = shared['folders']
        return result

    def get_usage(
        self, username, folder=None, refresh=False, study=True, scan_only=False
    ):
        helper = FileSystemHelper(self.context, username)
        home = helper.get_user_home()
        home = os.path.normpath(home)
        # get_user_home is <mount>/home/<username>
        mount = os.path.dirname(os.path.dirname(home))
        if folder is not None:
            if not isinstance(folder, str) or not os.path.isabs(folder):
                raise exceptions.invalid_params(
                    'folder must be an absolute path on your shared storage'
                )
            folder = os.path.normpath(folder)
            if os.path.commonpath([mount, folder]) != mount or folder in (home, mount):
                raise exceptions.unauthorized_access()
        identity = UserIdentity(self.context.accounts, username)
        from_analytics = (
            None
            if scan_only
            else self._usage_from_analytics(
                username, identity, mount, home, folder, refresh, study
            )
        )
        if from_analytics is not None:
            return from_analytics
        if folder is not None and os.path.commonpath([home, folder]) != home:
            analytics = self._analytics()
            state = analytics.status(folder)['state'] if analytics else 'off'
            return dict(
                state='unavailable',
                home=home,
                message=OUTSIDE_HOME_MESSAGES.get(
                    state,
                    'Sizes for folders outside your home folder need storage analytics, which is not on for this volume.',
                ),
            )
        if folder is not None:
            try:
                result = scan_as_user(identity, home, folder)
            except OSError:
                raise exceptions.invalid_params(CANNOT_OPEN)
            result['folder'] = result['total']
            result['home'] = home
            result['source'] = 'scan'
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
            if (
                refresh
                and cached is not None
                and cached['future'].done()
                and now - cached['created'] >= REFRESH_SECONDS
            ):
                cached = None
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
        response['source'] = 'scan'
        analytics = self._analytics()
        status = analytics.status(home) if analytics is not None else {}
        if status.get('state') == 'initializing':
            response['analytics_progress'] = status.get('progress', 0)
        return response

    def measure_for_costs(self, username):
        """Wait only in a background cost worker, using the browser's identical scan. Always the
        scan: analytics reports different bytes, and turning it on must not move a user's share."""
        result = self.get_usage(username, study=False, scan_only=True)
        if result['state'] == 'computing':
            with self._lock:
                entry = self._cache.get((username, result['home']))
            if entry is not None:
                result = entry['future'].result(timeout=COSTS_WAIT_SECONDS)
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
