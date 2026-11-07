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
Scratch cleanup: when a job finishes its folder gets a removal date, 30 days on for a success
and 60 for anything else, and when the date comes every check runs again before the folder goes.
Owners hear in one weekly digest, twice per folder, and no folder goes before both.

Off until an administrator turns it on, and a dry run until they say otherwise. The only
source of candidates is that record of finished jobs. The mount is walked once, for the
administrator's backfill of what existed before, and a backfill only adds to the record. Nothing
an API caller sends reaches a path.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Dict, List, Optional, Tuple
import base64
import json
import os
import stat
import time
import uuid

from ideadatamodel import (
    GetUserRequest,
    Notification,
    SocaJob,
    exceptions,
)
from ideasdk.metrics import BaseMetrics
from ideasdk.utils import Utils

from ideascheduler.app.scratch_cleanup.filesystem import (
    FolderIdentity,
    FolderInUse,
    NotASafeTarget,
    RemovalProgress,
    identity_of,
    inspect_folder,
    list_directory,
    lustre_mount_problem,
    open_child_dir,
    remove_folder,
    usage,
)
from ideascheduler.app.scratch_cleanup.holds import (
    DEFAULT_HOLDS_DIR,
    HOLD_TTL_SECONDS,  # noqa: F401
    MAX_HOLD_SECONDS,
    HoldLapsed,
    HoldUnavailable,
    OwnerHolds,
)
from ideascheduler.app.scratch_cleanup.job_state import (
    ScratchJobState,
    StateUnavailable,
    is_finished,
    job_succeeded,
)
from ideascheduler.app.scratch_cleanup.rules import (
    CLOCK_SLACK,
    REASON_TEXT,
    ActiveJobs,
    FolderContents,
    FolderShape,
    JobRecord,
    Limits,
    Reason,
    active_claim,
    backfill_problem,
    decide_contents,
    decide_job,
    is_owner_folder_name,
    job_time_bounds,
    limits_problem,
    parse_folder_name,
    plan_problem,
)
from ideascheduler.app.scratch_cleanup.store import LeaseLost, ScratchCleanupStore

# a fixed word to build a log monitor on: alert emails need SES, and this does not
ALERT_MARKER = 'SCRATCH_CLEANUP_ALERT'
TEMPLATE_DIGEST = 'scheduler.scratch-cleanup-digest'
TEMPLATE_ADMIN_SUMMARY = 'scheduler.scratch-cleanup-admin-summary'

# Names that hold persistent data on every cluster, whatever their provider.
NEVER_SCRATCH = ('apps', 'data')
PREVIEW_LIFETIME = timedelta(hours=1)
MAX_PREVIEW_ROWS = 20000
# ponytail: one budget of metadata reads per scan, single threaded; a folder past it is kept as
# too large to check. Raise it or walk in parallel if real scratch trees ever need more.
SCAN_BUDGET_ENTRIES = 20_000_000
LOOP_SECONDS = 60
STORE_RETRY_SECONDS = 60
# what the next digest will say went does not wait forever for an owner who cannot be told
IDLE_RECORD_SECONDS = 400 * 24 * 3600
# how often due removals are looked for, in the record and nowhere else
REMOVAL_CHECK_SECONDS = 3600
# a folder kept on its date is asked about again this much later
RECHECK_KEPT = timedelta(days=1)
# a folder is first mentioned when this little remains
FIRST_NOTICE_BEFORE = timedelta(days=14)
# and never goes sooner than these after each mention, whatever its date said
AFTER_FIRST_NOTICE = timedelta(days=7)
AFTER_FINAL_NOTICE = timedelta(days=3)
DIGEST_PERIOD = timedelta(days=7)
DIGEST_LARGEST_FOLDERS = 5
SUMMARY_MAX_ITEMS = 20
# ponytail: a check reads this many of the soonest records. Past that many kept folders, later
# dates wait their turn; page through them if a cluster ever holds that many kept at once.
MAX_RECORDS_PER_CHECK = 5000
LEASE_RENEW_SECONDS = 60
# a large folder takes a long time to remove; a job can start wanting it meanwhile
STATE_RECHECK_SECONDS = 15
# the switches are three consistent table reads, and checks come every hundred operations
GATE_SECONDS = 1


class RunStopped(Exception):
    """a safety net said stop: nothing more is removed in this run"""


class SwitchedOff(RunStopped):
    """an administrator's own choice, so no alert"""


@dataclass(frozen=True)
class FileSystem:
    name: str
    title: Optional[str]
    mount_dir: str
    mount_name: Optional[str]
    file_system_id: Optional[str] = None


@dataclass
class Row:
    file_system: str
    path: str
    parent_name: Optional[str]
    name: str
    shape: Optional[FolderShape] = None
    owner: Optional[str] = None
    device: int = 0
    inode: int = 0
    ctime_ns: int = 0
    records: List[JobRecord] = field(default_factory=list)
    end_time: Optional[datetime] = None
    size_bytes: int = 0
    entries: int = 0
    action: str = 'keep'
    reason: Reason = Reason.NOT_CHECKED
    remove_after: Optional[datetime] = None
    owner_uid: Optional[int] = None
    removed_entries: int = 0
    record_key: Optional[str] = None
    record: Optional[Dict] = None

    @property
    def identity(self) -> Tuple[str, int, int]:
        return self.path, self.device, self.inode

    def as_listing(self) -> Dict:
        return {
            'file_system': self.file_system,
            'path': self.path,
            'owner': self.owner,
            'job_id': self.shape.base_id if self.shape else None,
            'job_uid': next((r.job_uid for r in self.records if r.job_uid), None),
            'job_end_time': self.end_time,
            'size_bytes': self.size_bytes,
            'entries': self.entries,
            'action': self.action,
            'reason': self.reason.value,
            'reason_text': REASON_TEXT[self.reason],
            'remove_after': self.remove_after,
        }


@dataclass
class Scan:
    file_system: FileSystem
    rows: List[Row] = field(default_factory=list)
    candidates: List[Row] = field(default_factory=list)
    job_folders: int = 0
    reason_counts: Counter = field(default_factory=Counter)
    truncated_rows: int = 0
    other_kept: int = 0
    capacity_bytes: int = 0
    used_bytes: int = 0
    used_percent: float = 0.0
    # ignore how long ago jobs ended, to find folders for a later date
    any_age: bool = False
    seen_paths: set = field(default_factory=set)

    def keep(self, row: Row, reason: Reason):
        row.action = 'keep'
        row.reason = reason
        self.reason_counts[reason.value] += 1
        if row.shape is None:
            # root can read folder names their owner hid; only job folders are anyone's business
            self.other_kept += 1
        elif len(self.rows) < MAX_PREVIEW_ROWS:
            self.rows.append(row)
        else:
            self.truncated_rows += 1


@dataclass
class Preview:
    preview_id: str
    file_system: str
    created_at: datetime
    rows: List[Row]
    reason_counts: Dict[str, int]
    truncated_rows: int
    problem: Optional[str]
    other_kept: int = 0

    @property
    def expires_at(self) -> datetime:
        return self.created_at + PREVIEW_LIFETIME

    def rows_with(self, action: str) -> List[Row]:
        return [row for row in self.rows if row.action == action]

    @property
    def confirm_phrase(self) -> Optional[str]:
        count = len(self.rows_with('schedule'))
        return f'schedule {count}' if count else None


class ScratchCleanupMetrics(BaseMetrics):
    def __init__(self, context, file_system: str):
        super().__init__(context, split_dimensions=False)
        self.with_dimension('idea_cluster', context.cluster_name())
        self.with_dimension('file_system', file_system)

    def level(self, name: str, value: float):
        self.gauge(MetricName=f'scratch_cleanup.{name}', Value=value)

    def add(self, name: str, value: float = 1):
        self.count(MetricName=f'scratch_cleanup.{name}', Value=value)


class ScratchCleanupService:
    def __init__(
        self,
        context,
        store: Optional[ScratchCleanupStore] = None,
        state: Optional[ScratchJobState] = None,
        mountinfo_path: str = '/proc/self/mountinfo',
        holds_dir: str = DEFAULT_HOLDS_DIR,
    ):
        self.context = context
        self._logger = context.logger('scratch-cleanup')
        self.store = store or ScratchCleanupStore(context)
        self.state = state or ScratchJobState(context, self._logger)
        self._mountinfo_path = mountinfo_path
        self._holds = OwnerHolds(holds_dir)
        self._holder = f'{os.uname().nodename}-{uuid.uuid4().hex[:8]}'
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._busy = Lock()
        self._lock = Lock()
        self._progress: Optional[Dict] = None
        self._previews: Dict[str, Preview] = {}
        self._usage: Dict[str, Dict] = {}
        self._store_ready = False
        self._store_tried = -STORE_RETRY_SECONDS
        self._store_error: Optional[str] = None
        self._last_lease_renewal = 0.0
        self._holding_lease = False
        self._last_state_check = 0.0
        self._last_gate = 0.0
        self._current_audit: Dict = {}

    # lifecycle
    def start(self):
        if not self.file_systems():
            return
        self._thread = Thread(name='scratch-cleanup', target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _ensure_store(self) -> bool:
        if self._store_ready:
            return True
        # the finished-job path calls this too, and must not pay for a table it cannot open
        if time.monotonic() - self._store_tried < STORE_RETRY_SECONDS:
            return False
        self._store_tried = time.monotonic()
        try:
            self.store.initialize()
            self._store_ready = True
            self._store_error = None
        except Exception as e:
            # a task started before its role allowed the table: nothing runs without an audit log
            self._store_error = f'The cleanup table could not be opened: {e}'
            self._logger.error(self._store_error)
        return self._store_ready

    def _loop(self):
        try:
            if self._ensure_store():
                self._settle_earlier_runs()
        except Exception as e:
            self._logger.error(f'could not settle earlier scratch cleanup runs: {e}')
        while True:
            try:
                self._refresh_usage()
                self._run_if_due()
            except Exception as e:
                self._logger.exception(f'scratch cleanup loop failed: {e}')
            if self._stop.wait(LOOP_SECONDS):
                return

    def _settle_earlier_runs(self):
        # a task replaced in the middle of a run leaves its row saying running
        if self.store.acquire_lease(self._holder):
            try:
                self._close_interrupted_runs({'run_id': ''})
            finally:
                self.store.release_lease(self._holder)

    def _refresh_usage(self):
        # read here and served from memory: a hung mount stalls this thread, never an API call
        for file_system in self.file_systems():
            problem = self.mount_problem(file_system)
            reading = {'problem': problem}
            if problem is None:
                capacity, used, percent = usage(file_system.mount_dir)
                reading.update(
                    capacity_bytes=capacity,
                    used_bytes=used,
                    used_percent=round(percent, 1),
                    usage_read_at=_now(),
                )
                self._metrics(file_system.name).level('used_percent', percent)
            with self._lock:
                self._usage[file_system.name] = reading

    def _run_if_due(self):
        limits = self.limits()
        if not limits.enabled or limits.kill_switch or limits_problem(limits):
            return
        if not self._ensure_store():
            return
        for file_system in self.file_systems():
            if file_system.name not in limits.file_systems:
                continue
            last = next(
                (
                    run
                    for run in self.store.list_runs(limit=20)
                    if run.get('file_system') == file_system.name
                    and run.get('trigger') == 'schedule'
                ),
                None,
            )
            if last and _parse_time(last['started_at']) > _now() - timedelta(
                seconds=REMOVAL_CHECK_SECONDS
            ):
                continue
            if self._busy.acquire(blocking=False):
                try:
                    self._execute(file_system, 'schedule', 'scheduler')
                finally:
                    self._busy.release()

    # the finished-job path
    def record_job_finished(self, job: SocaJob):
        """
        called as a job's finished record is written. No file system access: a slow mount must
        not hold up finishing jobs. Writing the same event twice changes nothing, and a later
        finish of the same job replaces the earlier one. A job that never ran has no folder.
        Nothing is written while cleanup is off, in dry run or stopped: a backfill finds those
        folders from the jobs' own records when an administrator chooses that.
        """
        config = self.context.config()
        if not config.get_bool('scheduler.scratch_cleanup.enabled', default=False):
            return
        if job.start_time is None or not is_finished(job):
            return
        # the loop opens the table: this thread finishes jobs and must not wait for that
        if not self._store_ready or not self.file_systems() or not self._arming_now():
            return
        key = job_key(job)
        ended = _iso(_as_utc(job.end_time))
        if not ended:
            return
        outcome = 'succeeded' if job_succeeded(job) else 'failed'
        existing = self.store.get_removal(key)
        if (
            existing
            and existing.get('end_time') == ended
            and existing.get('outcome') == outcome
        ):
            return
        self.store.put_removal(
            key,
            {
                'job_id': str(job.job_id),
                'job_uid': job.job_uid or None,
                'owner': job.owner,
                'end_time': ended,
                'outcome': outcome,
                'source': 'event',
                'remove_at': _iso(
                    _as_utc(job.end_time) + self._retention(outcome == 'succeeded')
                ),
                'recorded_at': _iso(_now()),
            },
        )

    def _retention(self, succeeded: bool) -> timedelta:
        config = self.context.config()
        prefix = 'scheduler.scratch_cleanup'
        if succeeded:
            return timedelta(
                days=config.get_int(f'{prefix}.success_retention_days', default=30)
            )
        return timedelta(
            days=config.get_int(f'{prefix}.failed_retention_days', default=60)
        )

    def _arming_now(self) -> bool:
        try:
            limits = self.limits()
        except Exception as e:
            self._logger.warning(f'could not read cleanup settings: {e}')
            return False
        return limits.enabled and not limits.dry_run and not limits.kill_switch

    # configuration
    def file_systems(self) -> List[FileSystem]:
        """every Lustre attachment that could hold job scratch; none means nothing shows"""
        storage = self.context.config().get_config('shared-storage', default={}) or {}
        found = []
        home_dir = (
            self.context.config().get_string('cluster.home_dir', default='') or ''
        )
        for name in sorted(storage.keys()):
            entry = storage.get(name)
            if not hasattr(entry, 'get') or entry.get('provider') != 'fsx_lustre':
                continue
            mount_dir = entry.get('mount_dir')
            if not mount_dir or name in NEVER_SCRATCH:
                continue
            if home_dir == mount_dir or home_dir.startswith(
                f'{mount_dir.rstrip("/")}/'
            ):
                continue
            lustre = entry.get('fsx_lustre') or {}
            found.append(
                FileSystem(
                    name=name,
                    title=entry.get('title'),
                    mount_dir=mount_dir,
                    mount_name=lustre.get('mount_name'),
                    file_system_id=lustre.get('file_system_id'),
                )
            )
        return found

    def _file_system(self, name: Optional[str]) -> FileSystem:
        for file_system in self.file_systems():
            if file_system.name == name:
                return file_system
        raise exceptions.invalid_params(f'no scratch file system named {name}')

    def mount_problem(self, file_system: FileSystem) -> Optional[str]:
        return lustre_mount_problem(
            file_system.mount_dir, file_system.mount_name, self._mountinfo_path
        )

    def data_repository_problem(self, file_system: FileSystem) -> Optional[str]:
        """
        a file system linked to S3 can export a delete there, so removing a folder could remove
        the only copy. Cleanup does not run on one, and not knowing counts as linked.
        """
        if not file_system.file_system_id:
            return f'Whether {file_system.name} is linked to a data repository could not be checked.'
        filesystem_id = file_system.file_system_id
        try:
            fsx = self.context.aws().fsx()
            described = fsx.describe_file_systems(FileSystemIds=[filesystem_id])
            lustre = described['FileSystems'][0].get('LustreConfiguration') or {}
            associations = fsx.describe_data_repository_associations(
                Filters=[{'Name': 'file-system-id', 'Values': [filesystem_id]}]
            )['Associations']
        except Exception as e:
            return (
                f'Whether {file_system.name} is linked to a data repository could not be '
                f'checked: {e}'
            )
        if lustre.get('DataRepositoryConfiguration') or associations:
            return (
                f'{file_system.name} is linked to a data repository, so removing a folder '
                'could also remove its copy there. Cleanup does not run on it.'
            )
        return None

    def mount_aliases(self, file_system: FileSystem) -> List[str]:
        # job scripts reach the same file system through the job scratch mount point
        aliases = [file_system.mount_dir]
        point = self.context.config().get_string(
            'scheduler.scratch_storage.fsx_lustre.mount_point', default='/fsx'
        )
        if point and point not in aliases:
            aliases.append(point)
        return aliases

    def limits(self) -> Limits:
        """settings from the config tree, with the switches read fresh when possible"""
        config = self.context.config()
        prefix = 'scheduler.scratch_cleanup'
        return Limits(
            enabled=self._switch('enabled', when_missing=False, when_invalid=False),
            dry_run=self._switch('dry_run', when_missing=True, when_invalid=True),
            kill_switch=self._switch(
                'kill_switch', when_missing=False, when_invalid=True
            ),
            file_systems=tuple(
                config.get_list(f'{prefix}.file_systems', default=[]) or []
            ),
            success_retention_days=config.get_int(
                f'{prefix}.success_retention_days', default=30
            ),
            failed_retention_days=config.get_int(
                f'{prefix}.failed_retention_days', default=60
            ),
            digest_weekday=config.get_int(f'{prefix}.digest_weekday', default=0),
            max_folders_per_run=config.get_int(
                f'{prefix}.max_folders_per_run', default=25
            ),
            max_run_bytes_percent=float(
                config.get_int(f'{prefix}.max_run_bytes_percent', default=25)
            ),
            max_run_share_percent=float(
                config.get_int(f'{prefix}.max_run_share_percent', default=50)
            ),
        )

    def _switch(self, leaf: str, when_missing: bool, when_invalid: bool) -> bool:
        """
        the config tree follows the settings table with a lag, and these decide whether
        anything is removed, so they are read from the table. A failed read raises.
        """
        table = (
            self.context.aws()
            .dynamodb_table()
            .Table(f'{self.context.cluster_name()}.cluster-settings')
        )
        key = f'{self.context.module_id()}.scratch_cleanup.{leaf}'
        item = table.get_item(Key={'key': key}, ConsistentRead=True).get('Item')
        if not item or item.get('value') is None:
            return when_missing
        value = item['value']
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ('true', 'false'):
            return value.lower() == 'true'
        return when_invalid

    def _gate(self) -> Limits:
        """every removal passes through here first"""
        limits = self.limits()
        if limits.kill_switch:
            raise SwitchedOff('The kill switch is on.')
        if not limits.enabled:
            raise SwitchedOff('Scratch cleanup is turned off.')
        if limits.dry_run:
            raise SwitchedOff('Scratch cleanup is in dry run.')
        return limits

    # scanning
    def _owner_uid(self, username: Optional[str], cache: Dict) -> Optional[int]:
        if not username:
            return None
        if username not in cache:
            try:
                user = self.context.accounts_client.get_user(
                    GetUserRequest(username=username)
                ).user
                cache[username] = (
                    int(user.uid) if user and user.uid is not None else None
                )
            except Exception as e:
                self._logger.info(f'no account for {username}: {e}')
                cache[username] = None
        return cache[username]

    def _set_progress(self, **values):
        with self._lock:
            if self._progress is not None:
                self._progress.update(values)

    def _scan(
        self,
        file_system: FileSystem,
        limits: Limits,
        active: ActiveJobs,
        unfinished: Dict[Tuple[int, int], Dict],
    ) -> Scan:
        """
        classify every folder, for a backfill and nothing else. Only folders that pass the job
        rules are walked, oldest first.
        """
        # a backfill finds folders of any age: each still takes its date from when its job ended
        scan = Scan(file_system=file_system, any_age=True)
        scan.capacity_bytes, scan.used_bytes, scan.used_percent = usage(
            file_system.mount_dir
        )
        root_fd = _open_root(file_system.mount_dir)
        now = _now()
        owners: Dict = {}
        try:
            root_device = os.fstat(root_fd).st_dev
            self._set_progress(phase='Reading folders', done=0, total=None)
            for name, is_dir in sorted(list_directory(root_fd)):
                if self._stop.is_set():
                    raise SwitchedOff('The scheduler is stopping.')
                path = _join(file_system.mount_dir, name)
                if not is_dir:
                    scan.keep(
                        Row(file_system.name, path, None, name), Reason.NOT_A_DIRECTORY
                    )
                    continue
                if not is_owner_folder_name(name):
                    scan.keep(
                        Row(file_system.name, path, None, name), Reason.NOT_A_JOB_FOLDER
                    )
                    continue
                self._scan_owner_folder(
                    scan, root_fd, root_device, name, active, limits, now, owners
                )
                self._set_progress(done=scan.job_folders)
                self._tick()

            self._inspect_candidates(scan, root_fd, root_device, unfinished, limits)
        finally:
            os.close(root_fd)
        return scan

    def _scan_owner_folder(
        self, scan, root_fd, root_device, owner_name, active, limits, now, owners
    ):
        file_system = scan.file_system
        owner_path = _join(file_system.mount_dir, owner_name)
        try:
            owner_fd = open_child_dir(root_fd, owner_name)
        except (OSError, NotASafeTarget):
            scan.keep(
                Row(file_system.name, owner_path, None, owner_name), Reason.UNREADABLE
            )
            return
        try:
            if os.fstat(owner_fd).st_dev != root_device:
                scan.keep(
                    Row(file_system.name, owner_path, None, owner_name),
                    Reason.OTHER_MOUNT_INSIDE,
                )
                return
            for name, is_dir in sorted(list_directory(owner_fd)):
                path = _join(owner_path, name)
                shape = parse_folder_name(name)
                row = Row(
                    file_system.name,
                    path,
                    owner_name,
                    name,
                    shape=shape,
                    owner=owner_name,
                )
                if shape is None:
                    scan.keep(row, Reason.NOT_A_JOB_FOLDER)
                elif not is_dir:
                    # a link named like a job is not a job folder, wherever it points
                    scan.keep(row, Reason.NOT_A_DIRECTORY)
                else:
                    self._classify(
                        scan,
                        owner_fd,
                        owner_name,
                        name,
                        shape,
                        path,
                        active,
                        limits,
                        now,
                        owners,
                    )
        except OSError:
            scan.keep(
                Row(file_system.name, owner_path, None, owner_name), Reason.UNREADABLE
            )
        finally:
            os.close(owner_fd)

    def _classify(
        self,
        scan,
        parent_fd,
        owner_name,
        name,
        shape,
        path,
        active,
        limits,
        now,
        owners,
        record_key=None,
        record=None,
    ):
        file_system = scan.file_system
        row = Row(
            file_system.name,
            path,
            owner_name,
            name,
            shape=shape,
            owner=owner_name,
            record_key=record_key,
            record=record,
        )
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            row.device, row.inode = info.st_dev, info.st_ino
            row.ctime_ns = info.st_ctime_ns
        except OSError:
            scan.keep(row, Reason.UNREADABLE)
            return
        try:
            records = self.state.finished_records(shape.job_id_forms)
        except StateUnavailable:
            scan.keep(row, Reason.STATE_UNAVAILABLE)
            return
        row.owner_uid = self._owner_uid(owner_name, owners)
        decision = decide_job(
            owner_folder=owner_name,
            folder_name=name,
            shape=shape,
            folder_uid=info.st_uid,
            owner_folder_uid=os.fstat(parent_fd).st_uid,
            owner_uid=row.owner_uid,
            active=active,
            records=records,
            now=now,
            min_age=(
                timedelta(0)
                if scan.any_age
                else timedelta(days=limits.success_retention_days)
            ),
            failed_min_age=(
                timedelta(0)
                if scan.any_age
                else timedelta(days=limits.failed_retention_days)
            ),
        )
        row.records = decision.records
        row.end_time = decision.latest_end
        # anyone can make a numbered folder; only one with a job behind it counts in the share
        if decision.reason != Reason.NO_FINISHED_RECORD:
            scan.job_folders += 1
        if decision.reason != Reason.ELIGIBLE:
            scan.keep(row, decision.reason)
            return
        scan.candidates.append(row)

    def _inspect_candidates(self, scan, root_fd, root_device, unfinished, limits):
        candidates = sorted(scan.candidates, key=_oldest_first)
        self._set_progress(phase='Checking folders', done=0, total=len(candidates))
        budget = SCAN_BUDGET_ENTRIES
        for index, row in enumerate(candidates):
            if self._stop.is_set():
                raise SwitchedOff('The scheduler is stopping.')
            self._set_progress(done=index, current=row.path)
            self._tick()
            if budget <= 0:
                scan.keep(row, Reason.NOT_CHECKED)
                continue
            if _partly_removed(row, unfinished):
                scan.keep(row, Reason.PARTLY_REMOVED)
                continue
            contents = self._inspect(root_fd, root_device, row, budget)
            budget -= contents.entries
            row.size_bytes, row.entries = contents.size_bytes, contents.entries
            reason = decide_contents(
                contents, row.records, resume_until=_resume_until(row, unfinished)
            )
            if reason != Reason.ELIGIBLE:
                scan.keep(row, reason)
                continue
            row.reason = Reason.ELIGIBLE
            row.action = 'later'
            scan.reason_counts[Reason.ELIGIBLE.value] += 1
            scan.rows.append(row)
        self._set_progress(done=len(candidates), current=None)

    def _inspect(self, root_fd, root_device, row: Row, budget: int) -> FolderContents:
        try:
            with _Chain(root_fd, row) as chain:
                return inspect_folder(
                    chain.folder_fd,
                    root_device,
                    row.owner_uid,
                    budget,
                    pace=self._tick,
                )
        except (OSError, NotASafeTarget):
            return FolderContents(unreadable=True)

    # scanning the record
    def _scan_records(
        self, file_system, limits, active, unfinished, records, any_age=False
    ) -> Tuple[Scan, List[str]]:
        """
        the folders these records name, each classified by the same rules as anything else.
        Also returns the records with no folder here.
        """
        scan = Scan(file_system=file_system, any_age=any_age)
        scan.capacity_bytes, scan.used_bytes, scan.used_percent = usage(
            file_system.mount_dir
        )
        absent = []
        now = _now()
        owners: Dict = {}
        root_fd = _open_root(file_system.mount_dir)
        try:
            root_device = os.fstat(root_fd).st_dev
            self._set_progress(phase='Checking folders', done=0, total=len(records))
            for index, record in enumerate(records):
                self._set_progress(done=index)
                self._tick()
                if not self._scan_record(
                    scan, root_fd, record, active, limits, now, owners
                ):
                    absent.append(record['key'])
            self._inspect_candidates(scan, root_fd, root_device, unfinished, limits)
        finally:
            os.close(root_fd)
        return scan, absent

    def _scan_record(self, scan, root_fd, record, active, limits, now, owners) -> bool:
        owner = record.get('owner') or ''
        if not is_owner_folder_name(owner):
            return False
        try:
            owner_fd = open_child_dir(root_fd, owner)
        except (OSError, NotASafeTarget):
            return False
        try:
            found = _named_folders(record, owner_fd)
            for name, is_dir in found:
                path = _join(_join(scan.file_system.mount_dir, owner), name)
                # a subjob's own record and its array's both name its folder
                if path in scan.seen_paths:
                    continue
                scan.seen_paths.add(path)
                shape = parse_folder_name(name)
                if shape is None or not is_dir:
                    scan.keep(
                        Row(
                            scan.file_system.name,
                            path,
                            owner,
                            name,
                            shape=shape,
                            owner=owner,
                            record_key=record['key'],
                            record=record,
                        ),
                        Reason.NOT_A_DIRECTORY,
                    )
                    continue
                self._classify(
                    scan,
                    owner_fd,
                    owner,
                    name,
                    shape,
                    path,
                    active,
                    limits,
                    now,
                    owners,
                    record_key=record['key'],
                    record=record,
                )
            return bool(found)
        finally:
            os.close(owner_fd)

    def _note_kept(self, scan: Scan, now: datetime):
        """a kept folder stays on the record, shown with its reason and asked about tomorrow"""
        for row in scan.rows:
            if row.action == 'keep' and row.record_key:
                record = dict(row.record)
                record.update(
                    last_reason=row.reason.value,
                    next_check_at=_iso(now + RECHECK_KEPT),
                )
                self._put_removal(row.record_key, record)

    def _put_removal(self, key: str, record: Dict):
        self.store.put_removal(
            key, {k: v for k, v in record.items() if k not in ('pk', 'sk')}
        )

    def _cancel_requeued(self, records: List[Dict]) -> List[Dict]:
        """a job the scheduler is tracking again has not finished, so its date no longer holds"""
        kept = []
        for record in records:
            uid = record.get('job_uid')
            current = self.context.job_cache.get_job(record['job_id']) if uid else None
            if current is not None and current.job_uid == uid:
                self._logger.info(
                    f'job {record["job_id"]} is active again; its removal is cancelled'
                )
                self.store.delete_removal(record['key'])
                continue
            kept.append(record)
        return kept

    def _forget_absent(self, absent: List[str], records, limits, file_system):
        by_key = {record['key']: record for record in records}
        for key in absent:
            if not self._on_another_file_system(by_key[key], limits, file_system):
                self.store.delete_removal(key)

    def _on_another_file_system(self, record, limits, file_system) -> bool:
        """
        whether another selected file system may hold a folder of this record. One that cannot
        be read may: the record stays until every target is known to be gone.
        """
        for other in self.file_systems():
            if other.name == file_system.name or other.name not in limits.file_systems:
                continue
            if self.mount_problem(other):
                return True
            try:
                root_fd = _open_root(other.mount_dir)
            except OSError:
                return True
            try:
                owner_fd = open_child_dir(root_fd, record.get('owner') or '')
            except NotASafeTarget:
                continue
            except OSError:
                return True
            else:
                try:
                    if _named_folders(record, owner_fd):
                        return True
                finally:
                    os.close(owner_fd)
            finally:
                os.close(root_fd)
        return False

    # previews
    def start_preview(
        self,
        file_system_name: Optional[str],
        requested_by: str,
    ) -> None:
        file_system = self._file_system(file_system_name)
        if not self._ensure_store():
            raise exceptions.general_exception(self._store_error)
        if not self._busy.acquire(blocking=False):
            raise exceptions.invalid_params('Scratch cleanup is already working.')
        self._logger.info(
            f'scratch cleanup preview of {file_system.name} started by {requested_by}'
        )
        try:
            self.store.put_run(
                {
                    'run_id': uuid.uuid4().hex,
                    'file_system': file_system.name,
                    'trigger': 'preview',
                    'requested_by': requested_by,
                    'status': 'preview',
                    'started_at': _iso(_now()),
                    'message': 'Preview: nothing was removed and nobody was told.',
                }
            )
        except Exception:
            self._busy.release()
            raise
        self._begin_progress('preview', file_system.name)
        Thread(
            name='scratch-cleanup-preview',
            target=self._preview_worker,
            args=(file_system,),
            daemon=True,
        ).start()

    def _preview_worker(self, file_system: FileSystem):
        try:
            self.build_preview(file_system)
        except Exception as e:
            self._logger.exception(f'scratch cleanup preview failed: {e}')
            with self._lock:
                self._previews[file_system.name] = Preview(
                    preview_id=uuid.uuid4().hex,
                    file_system=file_system.name,
                    created_at=_now(),
                    rows=[],
                    reason_counts={},
                    truncated_rows=0,
                    problem=f'The preview failed: {e}',
                )
        finally:
            self._end_progress()
            self._busy.release()

    def build_preview(self, file_system: FileSystem) -> Preview:
        now = _now()
        limits = self.limits()
        problem = (
            limits_problem(limits)
            or self.mount_problem(file_system)
            or self.data_repository_problem(file_system)
        )
        rows: List[Row] = []
        counts: Dict[str, int] = {}
        truncated = 0
        other_kept = 0
        if problem is None:
            try:
                self._set_progress(phase='Asking the scheduler which jobs are active')
                active = self.state.active_jobs(self.mount_aliases(file_system))
                scan = self._scan(
                    file_system, limits, active, self.store.unfinished_removals()
                )
                planned = []
                for row in scan.rows:
                    if row.reason == Reason.ELIGIBLE:
                        row.action = (
                            'scheduled'
                            if self.store.has_removal(_row_key(row))
                            else 'schedule'
                        )
                        if row.action == 'schedule':
                            planned.append(row)
                problem = backfill_problem(
                    planned_folders=len(planned),
                    planned_bytes=sum(row.size_bytes for row in planned),
                    job_folders=scan.job_folders,
                    capacity_bytes=scan.capacity_bytes,
                    limits=limits,
                )
                rows = scan.rows
                counts = dict(scan.reason_counts)
                truncated = scan.truncated_rows
                other_kept = scan.other_kept
            except StateUnavailable as e:
                problem = str(e)
        preview = Preview(
            preview_id=uuid.uuid4().hex,
            file_system=file_system.name,
            created_at=now,
            rows=rows,
            reason_counts=counts,
            truncated_rows=truncated,
            problem=problem,
            other_kept=other_kept,
        )
        with self._lock:
            self._previews[file_system.name] = preview
        return preview

    def _preview_by_id(self, preview_id: Optional[str]) -> Preview:
        with self._lock:
            for preview in self._previews.values():
                if preview.preview_id == preview_id:
                    return preview
        raise exceptions.invalid_params('That preview is gone. Run a new preview.')

    def list_preview(
        self, preview_id: Optional[str], action: Optional[str], start: int, size: int
    ) -> Tuple[List[Dict], int]:
        preview = self._preview_by_id(preview_id)
        rows = [row for row in preview.rows if not action or row.action == action]
        start, size = max(start or 0, 0), min(max(size or 50, 1), 500)
        return [row.as_listing() for row in rows[start : start + size]], len(rows)

    # running
    def start_backfill(
        self,
        preview_id: Optional[str],
        confirm_phrase: Optional[str],
        requested_by: str,
    ) -> str:
        """
        put what the preview listed on the record. Nothing is removed here: each folder then goes the same way any other does, with
        notice to its owner and every check again.
        """
        preview = self._preview_by_id(preview_id)
        if _now() >= preview.expires_at:
            raise exceptions.invalid_params(
                'That preview is too old. Run a new preview.'
            )
        if preview.problem:
            raise exceptions.invalid_params(preview.problem)
        expected = preview.confirm_phrase
        if not expected or confirm_phrase != expected:
            raise exceptions.invalid_params('The confirmation text does not match.')
        try:
            self._gate()
        except SwitchedOff as e:
            raise exceptions.invalid_params(str(e))
        file_system = self._file_system(preview.file_system)
        if file_system.name not in self.limits().file_systems:
            raise exceptions.invalid_params(
                f'{file_system.name} is not selected for cleanup in settings.'
            )
        if not self._busy.acquire(blocking=False):
            raise exceptions.invalid_params('Scratch cleanup is already working.')
        run_id = uuid.uuid4().hex
        self._begin_progress('backfill', file_system.name)

        def work():
            try:
                self._backfill(preview, requested_by, run_id)
            except Exception as e:
                self._logger.exception(f'scratch cleanup backfill failed: {e}')
                self._alert(file_system, f'The backfill failed: {e}', run_id)
            finally:
                self._end_progress()
                self._busy.release()
                with self._lock:
                    self._previews.pop(file_system.name, None)

        Thread(name='scratch-cleanup-backfill', target=work, daemon=True).start()
        return run_id

    def _backfill(self, preview: Preview, requested_by: str, run_id: str):
        now = _now()
        run = {
            'run_id': run_id,
            'file_system': preview.file_system,
            'trigger': 'backfill',
            'requested_by': requested_by,
            'status': 'finished',
            'started_at': _iso(now),
            'scheduled_folders': 0,
        }
        rows = preview.rows_with('schedule')
        self._set_progress(phase='Recording folders', done=0, total=len(rows))
        for index, row in enumerate(rows):
            self._set_progress(done=index, current=row.path)
            record = _newest_record(row)
            self.store.put_removal(
                _row_key(row),
                {
                    'job_id': record.job_id,
                    'job_uid': record.job_uid,
                    'owner': record.owner,
                    'end_time': _iso(_as_utc(record.end_time)),
                    'outcome': 'succeeded' if record.succeeded else 'failed',
                    'source': 'backfill',
                    'remove_at': _iso(
                        _as_utc(record.end_time) + self._retention(record.succeeded)
                    ),
                    'recorded_at': _iso(now),
                    'requested_by': requested_by,
                },
            )
            run['scheduled_folders'] += 1
        run['finished_at'] = _iso(_now())
        run['message'] = (
            f'{run["scheduled_folders"]} folders are on the record. Each still gets both '
            'notices and every check.'
        )
        self.store.put_run(run)

    def _execute(
        self, file_system: FileSystem, trigger: str, requested_by: str
    ) -> Dict:
        """one look at the record for removals that are due"""
        started_progress = False
        with self._lock:
            if self._progress is None:
                self._progress = {
                    'operation': 'run',
                    'file_system': file_system.name,
                    'started_at': _now(),
                }
                started_progress = True
        run = {
            'run_id': uuid.uuid4().hex,
            'file_system': file_system.name,
            'trigger': trigger,
            'requested_by': requested_by,
            'status': 'running',
            'started_at': _iso(_now()),
            'removed_folders': 0,
            'removed_bytes': 0,
            'skipped_folders': 0,
            'noticed_folders': 0,
            'partial_folders': 0,
        }
        have_lease = False
        metrics = self._metrics(file_system.name)
        try:
            have_lease = self.store.acquire_lease(self._holder)
            if not have_lease:
                raise RunStopped('Another scheduler holds the cleanup lease.')
            self._holding_lease = True
            self._last_lease_renewal = time.monotonic()
            self._close_interrupted_runs(run)
            self.store.put_run(run)
            self._run_body(file_system, run, metrics)
        except SwitchedOff as e:
            run.update(status='stopped', message=str(e))
        except (RunStopped, StateUnavailable, LeaseLost) as e:
            run.update(status='stopped', message=str(e))
            self._raise_alert(file_system, run)
        except Exception as e:
            self._logger.exception(f'scratch cleanup run failed: {e}')
            run.update(status='failed', message=f'The run failed: {e}')
            self._raise_alert(file_system, run)
        finally:
            run['finished_at'] = _iso(_now())
            if have_lease:
                self._holding_lease = False
                self.store.release_lease(self._holder)
            try:
                self.store.put_run(run)
            except Exception as e:
                self._logger.error(f'could not record scratch cleanup run: {e}')
            self._logger.info(f'scratch cleanup run: {Utils.to_json(run)}')
            metrics.add('runs')
            metrics.add('removed_folders', run['removed_folders'])
            metrics.add('removed_bytes', run['removed_bytes'])
            if started_progress:
                self._end_progress()
        return run

    def _close_interrupted_runs(self, run: Dict):
        # holding the lease means no earlier run is still going
        for earlier in self.store.list_runs(limit=20):
            if (
                earlier.get('status') == 'running'
                and earlier['run_id'] != run['run_id']
            ):
                earlier.update(
                    status='interrupted',
                    message='The scheduler stopped during this run.',
                )
                for key in ('pk', 'sk', 'expires_at'):
                    earlier.pop(key, None)
                self.store.put_run(earlier)

    def _run_body(self, file_system, run, metrics):
        limits = self.limits()
        problem = limits_problem(limits)
        if problem:
            raise RunStopped(problem)
        if limits.kill_switch:
            raise SwitchedOff('The kill switch is on.')
        if not limits.enabled:
            raise SwitchedOff('Scratch cleanup is turned off.')
        if file_system.name not in limits.file_systems:
            raise SwitchedOff(f'{file_system.name} is not selected for cleanup.')
        problem = self.mount_problem(file_system) or self.data_repository_problem(
            file_system
        )
        if problem:
            raise RunStopped(problem)

        capacity, _, percent = usage(file_system.mount_dir)
        run['used_percent_before'] = round(percent, 1)
        metrics.level('used_percent', percent)

        self._set_progress(phase='Asking the scheduler which jobs are active')
        aliases = self.mount_aliases(file_system)
        active = self.state.active_jobs(aliases)
        unfinished = self.store.unfinished_removals()
        self._close_vanished_removals(unfinished, file_system)
        now = _now()
        records = self._cancel_requeued(self._records_in_range(now))

        if limits.dry_run:
            due = [record for record in records if _reached_its_date(record, now)]
            scan, _ = self._scan_records(file_system, limits, active, unfinished, due)
            self._stop_if_blind(scan)
            run.update(
                status='dry_run',
                dry_run=True,
                would_remove_folders=sum(
                    1 for row in scan.rows if row.reason == Reason.ELIGIBLE
                ),
                used_percent_after=run['used_percent_before'],
                message='Dry run: nothing was removed and nobody was told.',
            )
            return

        run['dry_run'] = False
        self._digest_stage(file_system, records, limits, active, unfinished, run, now)
        due = [
            record
            for record in self._records_in_range(now)
            if _may_be_due(record, file_system.name, limits, now)
        ]
        scan, absent = self._scan_records(file_system, limits, active, unfinished, due)
        self._stop_if_blind(scan)
        self._note_kept(scan, now)
        self._forget_absent(absent, due, limits, file_system)
        ready = sorted(
            self._told_and_due(scan, limits, now, unfinished), key=_oldest_first
        )[
            : limits.max_folders_per_run
        ]
        run['kept_folders'] = (
            sum(1 for row in scan.rows if row.action == 'keep') + scan.truncated_rows
        )
        metrics.level('kept_folders', run['kept_folders'])
        for reason, folders in scan.reason_counts.items():
            self._metrics(file_system.name).with_dimension('reason', reason).level(
                'folders_by_reason', folders
            )
        problem = plan_problem(
            planned_folders=len(ready),
            planned_bytes=sum(row.size_bytes for row in ready),
            job_folders=0,
            capacity_bytes=capacity,
            limits=limits,
        )
        if problem:
            raise RunStopped(problem)
        self._remove_due(file_system, scan, ready, limits, run, metrics, aliases)
        run['status'] = 'finished' if due or run['noticed_folders'] else 'nothing_to_do'
        _, _, after = usage(file_system.mount_dir)
        run['used_percent_after'] = round(after, 1)
        metrics.level('used_percent', after)

    def _told_and_due(
        self, scan: Scan, limits: Limits, now: datetime, unfinished: Dict
    ) -> List[Row]:
        """
        the rows that can go: each folder needs both notices itself. A different folder under the
        same name, or one on another file system, has been told nothing. Stale notices for its
        name are dropped, so the next digest mentions the folder that is there now.
        """
        ready = []
        for row in scan.rows:
            if row.reason != Reason.ELIGIBLE:
                continue
            notices = _notices_of(row)
            if not notices and _resume_until(row, unfinished):
                # its own removal changed the folder: it is still the one its owner was told of
                notices = _notices_of_inode(row)
            if not (notices.get('first_notice_at') and notices.get('final_notice_at')):
                if _forget_replaced(row):
                    self._put_removal(row.record_key, row.record)
                continue
            if _removal_date(row.record, limits, now, notices=notices) <= now:
                ready.append(row)
        return ready

    def _records_in_range(self, now: datetime) -> List[Dict]:
        """what could be mentioned or removed now: nothing further off has to be read"""
        return self.store.list_due(
            _iso(now + FIRST_NOTICE_BEFORE), MAX_RECORDS_PER_CHECK
        )

    @staticmethod
    def _stop_if_blind(scan: Scan):
        if scan.reason_counts[Reason.STATE_UNAVAILABLE.value]:
            raise RunStopped('Job records could not be read.')

    # digests
    def _digest_stage(self, file_system, records, limits, active, unfinished, run, now):
        """
        one email per owner, at most weekly on the set day, only when there is something new.
        A folder is in exactly two: a first mention once 14 days or less remain, and a final one
        in the digest before it goes. A digest that does not go moves the date instead.
        """
        if now.weekday() != limits.digest_weekday:
            return
        # the check is hourly; a window a day short of the period survives clock drift
        if not self.store.should_alert(
            f'digest:{file_system.name}',
            int(DIGEST_PERIOD.total_seconds()) - 24 * 3600,
        ):
            return
        removed_by_owner = defaultdict(list)
        try:
            for row in self.store.list_removed():
                removed_by_owner[row.get('owner')].append(row)
            self._send_digests(
                file_system,
                records,
                removed_by_owner,
                limits,
                active,
                unfinished,
                run,
                now,
            )
        except Exception:
            # the week is not lost to one bad hour: the next check tries again
            self.store.release_alert(f'digest:{file_system.name}')
            raise
        self._send_admin_summary(file_system, records, removed_by_owner, run)

    def _send_digests(
        self,
        file_system,
        records,
        removed_by_owner,
        limits,
        active,
        unfinished,
        run,
        now,
    ):
        wanted = [
            record
            for record in records
            if _may_need_notice(record, file_system.name, now)
        ]
        scan, absent = self._scan_records(
            file_system, limits, active, unfinished, wanted, any_age=True
        )
        self._stop_if_blind(scan)
        self._forget_absent(absent, wanted, limits, file_system)
        for row in scan.rows:
            if row.reason == Reason.ELIGIBLE and _digest_stage_of(
                row.record, now, _notices_of(row)
            ):
                row.action = 'notice'
        warning = self._hold_undeliverable_notices(scan, limits)
        by_owner = defaultdict(list)
        for row in scan.rows:
            if row.action == 'notice':
                by_owner[row.owner].append(row)
        unsent = [
            owner
            for owner in sorted({*by_owner, *removed_by_owner})
            if not self._send_digest(
                owner, by_owner[owner], removed_by_owner[owner], limits, run, now
            )
        ]
        if unsent:
            warning = ' '.join(
                filter(None, [warning, f'No digest went to: {", ".join(unsent)}.'])
            )
        if warning:
            run['message'] = warning
            self._alert(file_system, warning, run['run_id'])

    def _send_digest(self, owner, rows, removed, limits, run, now) -> bool:
        stages = {
            row.path: _digest_stage_of(row.record, now, _notices_of(row))
            for row in rows
        }
        first = [row for row in rows if stages[row.path] == 'first']
        final = [row for row in rows if stages[row.path] == 'final']
        dates = [
            _removal_date(
                row.record, limits, now, stages[row.path], notices=_notices_of(row)
            )
            for row in rows
        ]
        largest = sorted(rows, key=lambda row: row.size_bytes, reverse=True)
        if not self._can_receive_email(owner) or not self._notify(
            owner,
            TEMPLATE_DIGEST,
            {
                'owner': owner,
                'cluster_name': self.context.cluster_name(),
                'first_count': len(first),
                'first_gb': _gigabytes(sum(row.size_bytes for row in first)),
                'final_count': len(final),
                'final_gb': _gigabytes(sum(row.size_bytes for row in final)),
                'soonest': _iso(min(dates)) if dates else None,
                # the size of the email stays the same however many folders there are
                'largest': [
                    _folder_param(row) for row in largest[:DIGEST_LARGEST_FOLDERS]
                ],
                'removed_count': len(removed),
                'removed_gb': _gigabytes(
                    sum(row.get('size_bytes') or 0 for row in removed)
                ),
                'partly_removed_count': sum(1 for row in removed if row.get('partly')),
                'page_url': self._page_url('/home/completed-jobs'),
            },
        ):
            return False
        for row in rows:
            field = (
                'first_notice_at' if stages[row.path] == 'first' else 'final_notice_at'
            )
            # on the shared record, so the next folder of the same job adds to this one
            _forget_replaced(row)
            row.record.setdefault('targets', {}).setdefault(_target_key(row), {})[
                field
            ] = _iso(now)
            self._put_removal(row.record_key, row.record)
            run['noticed_folders'] += 1
        for row in removed:
            self.store.delete_removed(row['sk'])
        return True

    def _send_admin_summary(self, file_system, records, removed_by_owner, run):
        """the one email administrators get, and only when it has something to say"""
        failures = self.store.list_failures()
        removed = [row for rows in removed_by_owner.values() for row in rows]
        if not (failures or removed or run.get('message')):
            return
        sent = self._notify(
            self.context.config().get_string(
                'cluster.administrator_username', default=None
            ),
            TEMPLATE_ADMIN_SUMMARY,
            {
                'cluster_name': self.context.cluster_name(),
                'file_system': file_system.name,
                'scheduled_folders': self.store.count_removals(),
                'removed_count': len(removed),
                'removed_gb': _gigabytes(
                    sum(row.get('size_bytes') or 0 for row in removed)
                ),
                'waiting_message': run.get('message'),
                'failures': [
                    row.get('message') for row in failures[:SUMMARY_MAX_ITEMS]
                ],
                'more_failures': max(0, len(failures) - SUMMARY_MAX_ITEMS),
                'page_url': self._page_url('/soca/scratch-cleanup'),
            },
        )
        if sent:
            for row in failures:
                self.store.delete_failure(row['sk'])

    def _page_url(self, route: str) -> str:
        try:
            endpoint = self.context.config().get_cluster_external_endpoint()
        except Exception:
            return ''
        return f'{endpoint}/#{route}'

    def list_upcoming(
        self, size: int, cursor: Optional[str]
    ) -> Tuple[List[Dict], Optional[str]]:
        if not self._ensure_store():
            raise exceptions.general_exception(self._store_error)
        records, last_key = self.store.page_removals(
            min(max(size or 50, 1), 200), _decode_cursor(cursor, 'removal')
        )
        limits = self.limits()
        listing = []
        for record in records:
            reason = record.get('last_reason')
            told = _all_targets_told(record)
            listing.append(
                {
                    'path': f'{record.get("owner")}/{record.get("job_id")}',
                    'owner': record.get('owner'),
                    'job_id': record.get('job_id'),
                    'job_uid': record.get('job_uid'),
                    'job_end_time': record.get('end_time'),
                    'remove_after': _iso(
                        _removal_date(record, limits, None, notices=told)
                    ),
                    'action': 'scheduled',
                    'outcome': record.get('outcome'),
                    'first_notice_at': told.get('first_notice_at'),
                    'final_notice_at': told.get('final_notice_at'),
                    'reason': reason,
                    'reason_text': REASON_TEXT[Reason(reason)] if reason else None,
                }
            )
        return listing, _encode_cursor(last_key)

    def _close_vanished_removals(self, unfinished, file_system):
        prefix = f'{file_system.mount_dir.rstrip("/")}/'
        for identity, row in list(unfinished.items()):
            path = row.get('path', '')
            if not path.startswith(prefix):
                continue
            try:
                info = os.lstat(path)
                if (info.st_dev, info.st_ino) == identity:
                    continue
            except FileNotFoundError:
                pass
            for key in ('pk', 'sk'):
                row.pop(key, None)
            row.update(
                status='interrupted' if row['status'] == 'started' else 'failed_gone',
                finished_at=_iso(_now()),
            )
            self.store.put_audit(row)
            unfinished.pop(identity)

    def _hold_undeliverable_notices(self, scan, limits) -> Optional[str]:
        """
        a queued email is not a delivered one: the consumer drops it, silently, when mail is
        off or the account cannot receive it. Those folders wait instead of starting a notice
        period nobody hears about. There is no setting to go without: removal pauses.
        """
        rows = [row for row in scan.rows if row.action == 'notice']
        if not rows:
            return None
        if not self._cluster_can_send_email():
            for row in rows:
                row.action = 'later'
                row.remove_after = None
            return (
                f'{len(rows)} folders wait because the cluster cannot send email, so their '
                'owners cannot be told. Turn on email.'
            )
        blocked = []
        reachable: Dict[str, bool] = {}
        for row in rows:
            if row.owner not in reachable:
                reachable[row.owner] = self._can_receive_email(row.owner)
                if not reachable[row.owner]:
                    blocked.append(row.owner)
            if not reachable[row.owner]:
                row.action = 'later'
                row.remove_after = None
        if blocked:
            return (
                'Some folders wait because their owner has no enabled account with an email '
                f'address: {", ".join(sorted(blocked))}.'
            )
        return None

    def _cluster_can_send_email(self) -> bool:
        config = self.context.config()
        return bool(
            config.get_bool('cluster.ses.enabled', default=False)
            and config.get_bool(
                'cluster-manager.notifications.email.enabled', default=False
            )
        )

    def _can_receive_email(self, username: str) -> bool:
        try:
            user = self.context.accounts_client.get_user(
                GetUserRequest(username=username)
            ).user
        except Exception as e:
            self._logger.info(f'no account for {username}: {e}')
            return False
        return bool(user and user.enabled is not False and user.email)

    def _remove_due(self, file_system, scan, due, limits, run, metrics, aliases):
        # an array's record names each subjob's folder, and ends with the last of them
        left_on_record = Counter(row.record_key for row in scan.rows if row.record_key)
        self._set_progress(phase='Removing folders', done=0, total=len(due))
        for index, row in enumerate(due):
            self._set_progress(done=index, current=row.path)
            self._gate()
            self._keep_lease()
            if self._stop.is_set():
                raise SwitchedOff('The scheduler is stopping.')
            problem = self.data_repository_problem(file_system)
            if problem:
                raise RunStopped(problem)
            try:
                outcome = self._remove_one(file_system, row, limits, aliases, run)
            except Exception:
                if row.removed_entries:
                    run['partial_folders'] += 1
                    metrics.add('folder_partly_removed')
                    self._mark_job(row, 'scratch_partly_removed_at')
                    self._for_next_digest(row, partly=True)
                raise
            if outcome is None:
                run['skipped_folders'] += 1
                metrics.add('folder_skipped')
                self._put_removal(
                    row.record_key,
                    {**row.record, 'next_check_at': _iso(_now() + RECHECK_KEPT)},
                )
                continue
            run['removed_folders'] += 1
            run['removed_bytes'] += row.size_bytes
            metrics.add('folder_removed')
            left_on_record[row.record_key] -= 1
            (row.record.get('targets') or {}).pop(_target_key(row), None)
            if left_on_record[row.record_key] <= 0 and not self._on_another_file_system(
                row.record, limits, file_system
            ):
                self.store.delete_removal(row.record_key)
            else:
                self._put_removal(row.record_key, row.record)
            self._mark_job(row, 'scratch_removed_at')
            self._for_next_digest(row, partly=False)
        self._set_progress(done=len(due), current=None)

    def _for_next_digest(self, row: Row, partly: bool):
        self.store.put_removed(
            f'{row.owner}#{row.path}',
            {
                'owner': row.owner,
                'path': row.path,
                'job_id': row.shape.base_id,
                'size_bytes': row.size_bytes,
                'partly': partly,
                'at': _iso(_now()),
                'expires_at': _expiry(),
            },
        )

    def _remove_one(self, file_system, row, limits, aliases, run) -> Optional[Row]:
        """every rule again, on the open folder, immediately before it goes"""
        root_fd = _open_root(file_system.mount_dir)
        try:
            root_device = os.fstat(root_fd).st_dev
            if row.device != root_device:
                self._logger.warning(
                    f'{row.path} is on another file system; left alone'
                )
                return None
            try:
                chain = _Chain(root_fd, row).open()
            except (OSError, NotASafeTarget):
                self._logger.info(f'{row.path} changed or went away; left alone')
                return None
            try:
                return self._remove_opened(
                    root_device, chain, row, limits, aliases, run
                )
            finally:
                chain.close()
        finally:
            os.close(root_fd)

    def _remove_opened(self, root_device, chain, row, limits, aliases, run):
        identity = identity_of(chain.folder_fd)
        if (identity.device, identity.inode, os.fstat(chain.folder_fd).st_ctime_ns) != (
            row.device,
            row.inode,
            row.ctime_ns,
        ):
            self._logger.info(
                f'{row.path} is not the folder that was checked; left alone'
            )
            return None
        # the walk can take a long time, so the scheduler is asked after it, not before
        contents = inspect_folder(
            chain.folder_fd,
            root_device,
            row.owner_uid,
            SCAN_BUDGET_ENTRIES,
            pace=self._tick,
        )
        # after the walk, which can take far longer than a job should wait, and before the last
        # look at the scheduler: a job submitted before this is seen by it, and one submitted
        # after cannot start until this ends
        try:
            with self._holds.hold(row.owner):
                return self._remove_walked(
                    chain, row, identity, contents, limits, aliases, run
                )
        except HoldUnavailable as e:
            raise RunStopped(f'Jobs of {row.owner} could not be held back: {e}') from e

    def _remove_walked(self, chain, row, identity, contents, limits, aliases, run):
        active = self.state.active_jobs(aliases)
        records = self.state.finished_records(row.shape.job_id_forms)
        decision = decide_job(
            owner_folder=row.parent_name,
            folder_name=row.name,
            shape=row.shape,
            folder_uid=identity.uid,
            owner_folder_uid=os.fstat(chain.parent_fd).st_uid,
            owner_uid=row.owner_uid,
            active=active,
            records=records,
            now=_now(),
            min_age=timedelta(days=limits.success_retention_days),
            failed_min_age=timedelta(days=limits.failed_retention_days),
        )
        if decision.reason != Reason.ELIGIBLE:
            self._logger.info(f'{row.path}: now {decision.reason.value}; left alone')
            return None
        # read before this removal writes its own row
        unfinished = self.store.unfinished_removals()
        if _partly_removed(row, unfinished):
            return None
        resume_until = _resume_until(row, unfinished)
        reason = decide_contents(contents, decision.records, resume_until=resume_until)
        if reason != Reason.ELIGIBLE:
            self._logger.info(f'{row.path}: now {reason.value}; left alone')
            return None
        if not chain.path_still_resolves():
            self._logger.info(f'{row.path} was moved or replaced; left alone')
            return None

        row.size_bytes = contents.size_bytes
        row.records = decision.records
        row.end_time = decision.latest_end
        audit = {
            'path': row.path,
            'file_system': row.file_system,
            'owner': row.owner,
            'job_id': row.shape.base_id,
            'job_uid': next((r.job_uid for r in row.records if r.job_uid), None),
            'job_end_time': _iso(row.end_time),
            'size_bytes': contents.size_bytes,
            'entries': contents.entries,
            'device': row.device,
            'inode': row.inode,
            'run_id': run['run_id'],
            'requested_by': run['requested_by'],
            'reason': REASON_TEXT[Reason.ELIGIBLE],
            'started_at': _iso(_now()),
            'last_removal_at': _iso(_now()),
            'status': 'started',
        }
        # checked last, after the slow part: the switch may have moved during the walk
        self._gate()
        # written first: no audit row, no removal
        self.store.put_audit(audit)
        self._logger.info(f'scratch cleanup removing: {Utils.to_json(audit)}')
        not_before, not_after = job_time_bounds(row.records)
        # a resume emptied directories itself, so their times say nothing about the job
        self._last_state_check = time.monotonic()
        progress = RemovalProgress()
        self._current_audit = audit
        self._last_gate = 0.0
        try:
            removed = remove_folder(
                chain.parent_fd,
                row.name,
                chain.folder_fd,
                FolderIdentity(row.device, row.inode, identity.uid),
                lambda: self._keep_going(row, aliases),
                not_before=not_before.timestamp(),
                not_after=not_after.timestamp(),
                dirs_not_after=(
                    (resume_until + CLOCK_SLACK).timestamp()
                    if resume_until
                    else not_after.timestamp()
                ),
                progress=progress,
                before_each=self._holds.assert_held,
            )
        except HoldLapsed as e:
            self._record_interruption(audit, row, progress, 'started', e)
            raise RunStopped(str(e)) from e
        except (SwitchedOff, LeaseLost, RunStopped, StateUnavailable) as e:
            # left as started, so a later run can finish after checking everything again
            self._record_interruption(audit, row, progress, 'started', e)
            raise
        except (FolderInUse, NotASafeTarget) as e:
            # the folder is not what was checked any more, so nothing finishes this by itself
            self._record_interruption(audit, row, progress, 'failed', e)
            raise RunStopped(f'Removing {row.path} failed: {e}') from e
        except Exception as e:
            # an I/O error says nothing about the folder, so a later run can try again
            self._record_interruption(audit, row, progress, 'started', e)
            raise RunStopped(f'Removing {row.path} failed: {e}') from e
        audit.update(
            status='removed', removed_entries=removed, finished_at=_iso(_now())
        )
        self.store.put_audit(audit)
        self._logger.info(f'scratch cleanup removed: {Utils.to_json(audit)}')
        return row

    def _record_interruption(self, audit, row, progress, status, error):
        row.removed_entries = progress.entries
        audit.update(
            status=status,
            message=str(error),
            removed_entries=progress.entries,
            last_removal_at=_iso(_now()),
            finished_at=_iso(_now()),
        )
        self.store.put_audit(audit)

    def _keep_going(self, row: Row, aliases: List[str]):
        """runs during a long removal: any of these stop it where it is"""
        if self._stop.is_set():
            raise SwitchedOff('The scheduler is stopping.')
        if time.monotonic() - self._last_gate >= GATE_SECONDS:
            self._gate()
            self._last_gate = time.monotonic()
        self._keep_lease()
        held = self._holds.current
        if held is not None:
            if time.time() - held.started_at >= MAX_HOLD_SECONDS:
                raise SwitchedOff(
                    f"{row.path} is taking longer than its owner's jobs should wait. A later "
                    'check finishes it.'
                )
            self._holds.renew()
        if time.monotonic() - self._last_state_check < STATE_RECHECK_SECONDS:
            return
        self._last_state_check = time.monotonic()
        # how a later resume tells this removal's changes from anyone else's
        self._current_audit['last_removal_at'] = _iso(_now())
        self.store.put_audit(self._current_audit)
        active = self.state.active_jobs(aliases)
        if (
            active_claim(
                active,
                row.shape,
                row.parent_name,
                row.name,
            )
            is not None
        ):
            raise RunStopped(f'An active job started using {row.path} during removal.')

    def _tick(self):
        """long walks hand the interpreter back to the scheduler and keep a run's lease"""
        time.sleep(0)
        self._holds.renew()
        if self._holding_lease:
            self._keep_lease()

    def _keep_lease(self):
        if time.monotonic() - self._last_lease_renewal >= LEASE_RENEW_SECONDS:
            # the lease lasts minutes and this runs far more often, so a failure ends the run
            self.store.renew_lease(self._holder)
            self._last_lease_renewal = time.monotonic()

    def _mark_job(self, row: Row, field_name: str):
        """the job pages say the folder is gone, through the same path finished jobs index by"""
        for record in row.records:
            try:
                job = self.state.find_job(record)
                if job is not None:
                    setattr(job, field_name, _now())
                    self.context.job_cache.add_finished_job(job)
            except Exception as e:
                self._logger.error(f'could not mark job {record.job_id}: {e}')

    def _notify(self, username: Optional[str], template: str, params: Dict) -> bool:
        if not username:
            return False
        try:
            self.context.notifications_client.send_notification(
                notification=Notification(
                    username=username, template_name=template, params=params
                )
            )
            return True
        except Exception as e:
            self._logger.error(f'could not send {template} to {username}: {e}')
            return False

    def _raise_alert(self, file_system: FileSystem, run: Dict):
        self._alert(
            file_system, run.get('message') or 'The run stopped.', run['run_id']
        )

    def _alert(self, file_system: FileSystem, message: str, run_id: str):
        """what reaches a monitor at once; administrators read about it in the weekly summary"""
        self._logger.error(
            f'{ALERT_MARKER} file_system={file_system.name} run_id={run_id}: {message}'
        )
        self._metrics(file_system.name).add('run_failed')
        try:
            self.store.put_failure(
                f'{file_system.name}:{message}',
                {'message': f'{file_system.name}: {message}', 'at': _iso(_now())},
            )
        except Exception as e:
            self._logger.error(f'could not record the failure: {e}')

    def _metrics(self, file_system: str) -> ScratchCleanupMetrics:
        return ScratchCleanupMetrics(self.context, file_system)

    # progress
    def _begin_progress(self, operation: str, file_system: str):
        with self._lock:
            self._progress = {
                'operation': operation,
                'file_system': file_system,
                'phase': 'Starting',
                'started_at': _now(),
            }

    def _end_progress(self):
        with self._lock:
            self._progress = None

    # the page
    def status(self) -> Dict:
        result: Dict = {'file_systems': [], 'runs': [], 'previews': []}
        file_systems = self.file_systems()
        result['available'] = bool(file_systems)
        if not file_systems:
            return result
        try:
            limits = self.limits()
        except Exception as e:
            result['available'] = False
            result['message'] = f'Cleanup settings could not be read: {e}'
            return result
        result['settings'] = {
            **{
                name: getattr(limits, name)
                for name in (
                    'enabled',
                    'dry_run',
                    'kill_switch',
                    'success_retention_days',
                    'failed_retention_days',
                    'max_folders_per_run',
                    'max_run_bytes_percent',
                    'max_run_share_percent',
                    'digest_weekday',
                )
            },
            'file_systems': list(limits.file_systems),
            'problem': limits_problem(limits),
        }
        with self._lock:
            usage_readings = {k: dict(v) for k, v in self._usage.items()}
            result['progress'] = dict(self._progress) if self._progress else None
            previews = list(self._previews.values())
        for file_system in file_systems:
            reading = usage_readings.get(file_system.name, {})
            result['file_systems'].append(
                {
                    'name': file_system.name,
                    'title': file_system.title,
                    'mount_dir': file_system.mount_dir,
                    'selected': file_system.name in limits.file_systems,
                    **reading,
                }
            )
        if not self._cluster_can_send_email():
            result['message'] = (
                'The cluster cannot send email, so owners cannot be told and nothing is '
                'removed. Turn on email.'
            )
        if self._store_error:
            result['message'] = self._store_error
        if self._ensure_store():
            try:
                result['runs'] = self.store.list_runs(limit=20)
                result['scheduled_folders'] = self.store.count_removals()
                result['failed_folders'] = self.store.count_removals(outcome='failed')
            except Exception as e:
                result['message'] = f'Run history could not be read: {e}'
        result['previews'] = [_preview_summary(preview) for preview in previews]
        return result

    def list_audit(
        self, size: int, cursor: Optional[str]
    ) -> Tuple[List[Dict], Optional[str]]:
        if not self._ensure_store():
            raise exceptions.general_exception(self._store_error)
        start_key = _decode_cursor(cursor, 'audit')
        rows, last_key = self.store.list_audit(min(max(size or 50, 1), 200), start_key)
        listing = [
            {
                **{
                    key: row.get(key)
                    for key in (
                        'file_system',
                        'path',
                        'owner',
                        'job_id',
                        'job_uid',
                        'job_end_time',
                        'size_bytes',
                        'entries',
                        'reason',
                        'started_at',
                        'finished_at',
                        'run_id',
                        'requested_by',
                        'removed_entries',
                    )
                },
                'action': row.get('status'),
                'reason_text': row.get('message') or row.get('reason'),
            }
            for row in rows
        ]
        return listing, _encode_cursor(last_key)


class _Chain:
    """the descriptors from the root to one folder, each opened without following anything"""

    def __init__(self, root_fd: int, row: Row):
        self.root_fd = root_fd
        self.row = row
        self.owner_fd: Optional[int] = None
        self.folder_fd = -1

    @property
    def parent_fd(self) -> int:
        return self.owner_fd

    def open(self) -> '_Chain':
        self.owner_fd = open_child_dir(self.root_fd, self.row.parent_name)
        try:
            self.folder_fd = open_child_dir(self.owner_fd, self.row.name)
        except Exception:
            self.close()
            raise
        return self

    def path_still_resolves(self) -> bool:
        """the names still lead to the descriptors held, so a move by the owner saves the folder"""
        try:
            owner = os.stat(
                self.row.parent_name, dir_fd=self.root_fd, follow_symlinks=False
            )
            folder = os.stat(self.row.name, dir_fd=self.owner_fd, follow_symlinks=False)
        except OSError:
            return False
        held_owner = os.fstat(self.owner_fd)
        held_folder = os.fstat(self.folder_fd)
        return (owner.st_dev, owner.st_ino) == (
            held_owner.st_dev,
            held_owner.st_ino,
        ) and (
            folder.st_dev,
            folder.st_ino,
        ) == (held_folder.st_dev, held_folder.st_ino)

    def close(self):
        if self.folder_fd >= 0:
            os.close(self.folder_fd)
            self.folder_fd = -1
        if self.owner_fd is not None:
            os.close(self.owner_fd)
            self.owner_fd = None

    def __enter__(self) -> '_Chain':
        return self.open()

    def __exit__(self, *_):
        self.close()


def _resume_until(
    row: Row, unfinished: Dict[Tuple[int, int], Dict]
) -> Optional[datetime]:
    """when an unfinished removal of this very folder, for this very job, last ran"""
    started = unfinished.get((row.device, row.inode))
    if not (
        started
        and started.get('status') == 'started'
        and started.get('path') == row.path
        and started.get('job_id') == row.shape.base_id
    ):
        return None
    try:
        return _parse_time(started['last_removal_at'])
    except (KeyError, TypeError, ValueError):
        return None


def job_key(job) -> str:
    # an array's subjobs share a job uid
    return f'{job.job_uid or job.owner}:{job.job_id}'


def _row_key(row: Row) -> str:
    return job_key(_newest_record(row))


def _newest_record(row: Row) -> JobRecord:
    return max(row.records, key=lambda record: _as_utc(record.end_time))


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _expiry() -> int:
    return int(time.time()) + IDLE_RECORD_SECONDS


def _check_is_due(record: Dict, now: datetime) -> bool:
    next_check = record.get('next_check_at')
    return not next_check or _parse_time(next_check) <= now


def _target_key(row: Row) -> str:
    """
    one folder: its file system, its name, and which directory has that name. File systems reuse an
    inode as soon as it is free, so the inode alone cannot say; a directory's ctime moves when it is
    made, and when anything in it changes, after which it stays anyway.
    """
    return f'{row.file_system}|{row.name}|{row.inode}|{row.ctime_ns}'


def _notices_of(row: Row) -> Dict:
    return (row.record.get('targets') or {}).get(_target_key(row)) or {}


def _notices_of_inode(row: Row) -> Dict:
    prefix = f'{row.file_system}|{row.name}|{row.inode}|'
    for key, notices in (row.record.get('targets') or {}).items():
        if key.startswith(prefix):
            return notices
    return {}


def _forget_replaced(row: Row) -> bool:
    """notices given about a directory that no longer has this name told nobody about this one"""
    targets = row.record.get('targets') or {}
    same_name = f'{row.file_system}|{row.name}|'
    current = _target_key(row)
    stale = [key for key in targets if key.startswith(same_name) and key != current]
    for key in stale:
        del targets[key]
    return bool(stale)


def _targets_on(record: Dict, file_system: str) -> List[Dict]:
    return [
        notices
        for key, notices in (record.get('targets') or {}).items()
        if key.startswith(f'{file_system}|')
    ]


def _all_targets_told(record: Dict) -> Dict:
    """for a listing of the record: told once anything has been, twice once everything has"""
    targets = list((record.get('targets') or {}).values())
    told: Dict = {}
    firsts = [t['first_notice_at'] for t in targets if t.get('first_notice_at')]
    if firsts:
        told['first_notice_at'] = min(firsts)
    if targets and all(t.get('final_notice_at') for t in targets):
        told['final_notice_at'] = max(t['final_notice_at'] for t in targets)
    return told


def _digest_stage_of(record: Dict, now: datetime, notices: Dict) -> Optional[str]:
    """which mention one folder is due in this digest, if any"""
    date = _parse_time(record['remove_at'])
    first = notices.get('first_notice_at')
    if not first:
        return 'first' if date - FIRST_NOTICE_BEFORE <= now else None
    if notices.get('final_notice_at'):
        return None
    told = _parse_time(first)
    # not in the digest that gave the first mention, and only if it goes before the next one
    if now - told >= timedelta(days=1) and (
        max(date, told + AFTER_FIRST_NOTICE) <= now + DIGEST_PERIOD
    ):
        return 'final'
    return None


def _may_need_notice(record: Dict, file_system: str, now: datetime) -> bool:
    """a record with no folder known here may have one; the scan says which folder it is"""
    targets = _targets_on(record, file_system)
    if not targets:
        return _digest_stage_of(record, now, {}) is not None
    return any(_digest_stage_of(record, now, notices) for notices in targets)


def _removal_date(
    record: Dict,
    limits: Limits,
    now: Optional[datetime],
    stage: Optional[str] = None,
    notices: Optional[Dict] = None,
) -> datetime:
    """when a folder can go: never before both mentions have had their time"""
    notices = notices or {}
    date = _parse_time(record['remove_at'])
    told = notices.get('first_notice_at')
    if stage == 'first':
        told = _iso(now)
    if told:
        date = max(date, _parse_time(told) + AFTER_FIRST_NOTICE)
    final = notices.get('final_notice_at')
    if stage == 'final':
        final = _iso(now)
    if final:
        date = max(date, _parse_time(final) + AFTER_FINAL_NOTICE)
    return date


def _reached_its_date(record: Dict, now: datetime) -> bool:
    """what a dry run reports on"""
    return _check_is_due(record, now) and _parse_time(record['remove_at']) <= now


def _may_be_due(record: Dict, file_system: str, limits: Limits, now: datetime) -> bool:
    """
    before the scan: some folder here has both notices and has reached its date, so the scan
    is worth its cost. Which folder can go is decided from the scan (_told_and_due).
    """
    if not _check_is_due(record, now):
        return False
    return any(
        notices.get('first_notice_at')
        and notices.get('final_notice_at')
        and _removal_date(record, limits, now, notices=notices) <= now
        for notices in _targets_on(record, file_system)
    )


def _gigabytes(size_bytes: int) -> float:
    return round(size_bytes / 2**30, 2)


def _named_folders(record: Dict, owner_fd: int) -> List[Tuple[str, bool]]:
    """
    the folders a record can mean, looked up by name and never by walking. An array parent's are
    its subjobs', the only case that lists the owner's folder.
    """
    job_id = str(record.get('job_id') or '')
    if job_id.endswith('[]') and parse_folder_name(job_id[:-2]):
        prefix = f'{job_id[:-2]}['
        names = [
            name
            for name, _ in list_directory(owner_fd)
            if name.startswith(prefix) and parse_folder_name(name)
        ]
    elif parse_folder_name(job_id):
        names = [job_id]
    else:
        return []
    found = []
    for name in names:
        try:
            info = os.stat(name, dir_fd=owner_fd, follow_symlinks=False)
        except OSError:
            continue
        found.append((name, stat.S_ISDIR(info.st_mode)))
    return found


def _partly_removed(row: Row, unfinished: Dict[Tuple[int, int], Dict]) -> bool:
    started = unfinished.get((row.device, row.inode))
    return bool(started and started.get('status') == 'failed')


def _encode_cursor(key: Optional[Dict]) -> Optional[str]:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def _decode_cursor(cursor: Optional[str], partition: str) -> Optional[Dict]:
    if not cursor:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if (
            set(key) != {'pk', 'sk'}
            or key['pk'] != partition
            or not isinstance(key['sk'], str)
        ):
            raise ValueError(cursor)
    except Exception:
        raise exceptions.invalid_params('The page cursor is not valid.')
    return key


def _open_root(mount_dir: str) -> int:
    return os.open(
        mount_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )


def _join(parent: str, name: str) -> str:
    return f'{parent.rstrip("/")}/{name}'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _oldest_first(row: Row):
    # the end time first, as an admin would by hand; the job number only breaks ties
    end = row.end_time or datetime.max.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end, int(row.shape.base_id) if row.shape else 0, row.path


def _folder_param(row: Row) -> Dict:
    return {
        'path': row.path,
        'job_id': row.shape.base_id,
        'size_gb': round(row.size_bytes / 2**30, 2),
        'job_end_time': _iso(row.end_time),
    }


def _preview_summary(preview: Preview) -> Dict:
    actions: Counter = Counter()
    action_bytes: Counter = Counter()
    for row in preview.rows:
        actions[row.action] += 1
        action_bytes[row.action] += row.size_bytes
    actions['keep'] += preview.truncated_rows + preview.other_kept
    return {
        'preview_id': preview.preview_id,
        'file_system': preview.file_system,
        'created_at': preview.created_at,
        'expires_at': preview.expires_at,
        'action_counts': dict(actions),
        'action_bytes': dict(action_bytes),
        'reason_counts': preview.reason_counts,
        'reason_text': {key: REASON_TEXT[Reason(key)] for key in preview.reason_counts},
        'confirm_phrase': None if preview.problem else preview.confirm_phrase,
        'problem': preview.problem,
        'truncated_rows': preview.truncated_rows,
        'other_kept': preview.other_kept,
    }
