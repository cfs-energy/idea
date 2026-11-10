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
What may be removed from a shared scratch file system, as pure rules with no I/O.

A folder is removed only when every rule below holds. Each rule that cannot establish its part
answers with a reason to keep the folder: there is no branch that treats missing knowledge as
permission.

Job scripts make these folders, not IDEA, so nothing on disk names the job that made one. The
name, the owner and the times together are strong evidence, not proof: someone can make a
folder by hand under their own name, with the right number, during their own job's run.

The shapes that can go are the ones the portal's job scripts create, with
JOB_NUMBER=$(echo "$PBS_JOBID" | cut -d. -f1) and scratch_path=/fsx/${USER}/$JOB_NUMBER:
    <mount>/<owner>/<job number>
    <mount>/<owner>/<job number>[<index>]   the same script in an array subjob
Anything else under an owner's folder, and the owner's folder itself, is never a candidate. Nothing directly under the mount qualifies either:
the mount is writable by everyone with no sticky bit, so anyone can rename a tree into a name
there. An owner can keep a folder for good by renaming it to anything but a number.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Sequence
import math
import re

_JOB_ID = r'[0-9]{1,18}'
_PLAIN = re.compile(rf'({_JOB_ID})', re.ASCII)
_SUBJOB = re.compile(rf'({_JOB_ID})\[[0-9]{{1,18}}\]', re.ASCII)
_OWNER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', re.ASCII)

# Lustre timestamps come from the servers and job times from PBS; a job's own trap can also
# touch its folder just as PBS records the end.
CLOCK_SLACK = timedelta(minutes=15)


class Reason(str, Enum):
    ELIGIBLE = 'eligible'
    NOT_A_JOB_FOLDER = 'not_a_job_folder'
    NOT_A_DIRECTORY = 'not_a_directory'
    OWNER_UNKNOWN = 'owner_unknown'
    OWNER_MISMATCH = 'owner_mismatch'
    JOB_ACTIVE = 'job_active'
    REFERENCED_BY_ACTIVE_JOB = 'referenced_by_active_job'
    NO_FINISHED_RECORD = 'no_finished_record'
    RECORD_NOT_FINISHED = 'record_not_finished'
    RECORD_INCOMPLETE = 'record_incomplete'
    STATE_UNAVAILABLE = 'state_unavailable'
    TOO_RECENT = 'too_recent'
    CHANGED_AFTER_JOB = 'changed_after_job'
    OLDER_THAN_JOB = 'older_than_job'
    OTHER_MOUNT_INSIDE = 'other_mount_inside'
    OTHER_OWNERS_FILES = 'other_owners_files'
    UNREADABLE = 'unreadable'
    TOO_LARGE_TO_CHECK = 'too_large_to_check'
    OWNER_HAS_ACTIVE_JOB = 'owner_has_active_job'
    READ_AFTER_JOB = 'read_after_job'
    PARTLY_REMOVED = 'partly_removed'
    NOT_CHECKED = 'not_checked'


REASON_TEXT: Dict[Reason, str] = {
    Reason.ELIGIBLE: "The job finished, and everything in the folder dates from the job's run.",
    Reason.NOT_A_JOB_FOLDER: 'The name does not match a job folder.',
    Reason.NOT_A_DIRECTORY: 'This is a file or a link, not a job folder.',
    Reason.OWNER_UNKNOWN: 'The owner could not be matched to a user account.',
    Reason.OWNER_MISMATCH: 'The folder belongs to someone other than the job owner.',
    Reason.JOB_ACTIVE: 'The job is still running, queued, held or suspended.',
    Reason.REFERENCED_BY_ACTIVE_JOB: 'An active job depends on this job, names this folder, or mentions this job number.',
    Reason.NO_FINISHED_RECORD: 'No finished job with this owner and number is on record.',
    Reason.RECORD_NOT_FINISHED: 'The job record does not say the job finished.',
    Reason.RECORD_INCOMPLETE: 'The job record has no start or end time.',
    Reason.STATE_UNAVAILABLE: 'Job records could not be read.',
    Reason.TOO_RECENT: 'The job ended too recently.',
    Reason.CHANGED_AFTER_JOB: 'The folder changed after the job ended.',
    Reason.OLDER_THAN_JOB: 'The folder holds data older than the job, so it may not be from this job.',
    Reason.OTHER_MOUNT_INSIDE: 'Another file system is mounted inside this folder.',
    Reason.OTHER_OWNERS_FILES: 'The folder holds files owned by someone else.',
    Reason.UNREADABLE: 'The folder could not be read.',
    Reason.TOO_LARGE_TO_CHECK: 'The folder has too many files to check in one run.',
    Reason.OWNER_HAS_ACTIVE_JOB: 'The owner has an active job, which could be using this folder.',
    Reason.READ_AFTER_JOB: 'A file was read after the job ended.',
    Reason.PARTLY_REMOVED: 'Removal stopped part way because the folder changed. What is left stays.',
    Reason.NOT_CHECKED: 'Not checked in this run.',
}


@dataclass(frozen=True)
class FolderShape:
    """where a folder name says it came from; base_id is the number PBS tracks it under"""

    base_id: str
    job_id_forms: FrozenSet[str]


def parse_folder_name(name: str) -> Optional[FolderShape]:
    """
    a job folder's shape, or None for any other name. Digits are kept as written: 00123 is not
    job 123, so it finds no record and stays.
    """
    match = _PLAIN.fullmatch(name)
    if match:
        return FolderShape(
            base_id=match.group(1), job_id_forms=frozenset({match.group(1)})
        )
    match = _SUBJOB.fullmatch(name)
    if match:
        return FolderShape(
            base_id=match.group(1),
            job_id_forms=frozenset({name, f'{match.group(1)}[]'}),
        )
    return None


def is_owner_folder_name(name: str) -> bool:
    return _OWNER.fullmatch(name) is not None


def base_job_id(pbs_job_id: str) -> Optional[str]:
    """123, 123[] and 123[4] are all tracked under 123; None for anything unexpected"""
    match = re.fullmatch(r'([0-9]{1,18})(\[[0-9]*\])?', pbs_job_id, re.ASCII)
    return match.group(1) if match else None


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    job_uid: Optional[str]
    owner: Optional[str]
    finished: bool
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    # the earliest attempt's start; start_time is only the latest attempt's
    first_run_start: Optional[datetime] = None
    # shown to have completed normally; anything else waits the longer failed time
    succeeded: bool = True
    # numbers of the jobs that waited on this one, which PBS no longer lists on them
    dependents: FrozenSet[str] = frozenset()
    # a subjob's record only: its array's id, under which the array's one removal date is
    # kept, and the subjob's own end. end_time is then the array's, which is what the
    # retention counts from; the subjob's own end closes the window its files must fall in.
    array_id: Optional[str] = None
    run_end: Optional[datetime] = None

    @property
    def window_end(self) -> Optional[datetime]:
        return self.run_end or self.end_time


@dataclass(frozen=True)
class ActiveJobs:
    """what the scheduler says is not over, read as one snapshot"""

    base_ids: FrozenSet[str] = frozenset()
    dependency_ids: FrozenSet[str] = frozenset()
    # any number in an active job's script, arguments or environment: over-matches, and only
    # keeps more
    mentioned_ids: FrozenSet[str] = frozenset()
    # (owner, folder name) seen in an active job's paths or environment
    referenced_folders: FrozenSet[tuple] = frozenset()
    # lowercase names of everyone with a job not over
    owners: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class FolderContents:
    """what a walk of the folder found, metadata only"""

    size_bytes: int = 0
    entries: int = 0
    # the latest change to anything, mtime or ctime
    newest_change: Optional[datetime] = None
    # the earliest ctime: it cannot be set back, so old data cannot pass for new
    oldest_change: Optional[datetime] = None
    # the latest change to anything but a directory: removing a folder's contents changes the
    # directories it empties and nothing else, so this still shows new data during a resume
    newest_file_change: Optional[datetime] = None
    # the latest ctime of a directory, the folder itself included
    newest_dir_change: Optional[datetime] = None
    # the latest read of a file. Mounted noatime this never moves, so it only ever adds a reason
    # to keep; directories are left out because listing one, as this walk does, moves theirs
    newest_access: Optional[datetime] = None
    other_mount: bool = False
    other_owner: bool = False
    unreadable: bool = False
    over_budget: bool = False


@dataclass
class JobDecision:
    reason: Reason
    records: List[JobRecord] = field(default_factory=list)

    @property
    def latest_end(self) -> Optional[datetime]:
        ends = [record.end_time for record in self.records if record.end_time]
        return max(ends) if ends else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def active_claim(
    active: ActiveJobs,
    shape: FolderShape,
    owner_folder: str,
    folder_name: str,
) -> Optional[Reason]:
    """
    whether anything not over has a claim on this folder. A job can use a folder from one of its
    owner's earlier jobs in ways nothing here can see, so while the owner has any job active all
    their folders stay. There is no switch for this.
    """
    if shape.base_id in active.base_ids:
        return Reason.JOB_ACTIVE
    if (
        shape.base_id in active.dependency_ids
        or shape.base_id in active.mentioned_ids
        or (owner_folder, folder_name) in active.referenced_folders
    ):
        return Reason.REFERENCED_BY_ACTIVE_JOB
    if owner_folder.lower() in active.owners:
        return Reason.OWNER_HAS_ACTIVE_JOB
    return None


def decide_job(
    *,
    owner_folder: str,
    folder_name: str,
    shape: FolderShape,
    folder_uid: int,
    owner_folder_uid: int,
    owner_uid: Optional[int],
    active: ActiveJobs,
    records: Sequence[JobRecord],
    now: datetime,
    min_age: timedelta,
    failed_min_age: timedelta,
) -> JobDecision:
    """
    the rules that need no walk of the folder. records are every record the job caches hold
    under this number, whoever owned them.
    """
    claim = active_claim(active, shape, owner_folder, folder_name)
    if claim is not None:
        return JobDecision(claim)

    matching = list(records)
    # the same number under someone else's name says nothing about this folder
    matching = [r for r in matching if r.owner == owner_folder]
    if not matching:
        return JobDecision(Reason.NO_FINISHED_RECORD)
    if any(r.dependents & active.base_ids for r in matching):
        return JobDecision(Reason.REFERENCED_BY_ACTIVE_JOB, matching)
    # a subjob's own record decides. Without one its array's shows only that every subjob is
    # over, since PBS finishes an array when the last one has. It cannot show this subjob
    # succeeded: PBS keeps the last subjob's status on the array, and a later success hides an
    # earlier failure. So the folder waits the longer of the two times.
    own = [r for r in matching if r.job_id == folder_name]
    outcome_shown = bool(own) or len(shape.job_id_forms) == 1
    matching = own or matching

    if owner_uid is None:
        return JobDecision(Reason.OWNER_UNKNOWN, matching)
    if owner_uid != folder_uid or owner_uid != owner_folder_uid:
        return JobDecision(Reason.OWNER_MISMATCH, matching)

    if not all(r.finished for r in matching):
        return JobDecision(Reason.RECORD_NOT_FINISHED, matching)
    if not all(r.start_time and r.end_time for r in matching):
        return JobDecision(Reason.RECORD_INCOMPLETE, matching)

    decision = JobDecision(Reason.ELIGIBLE, matching)
    required = min_age if all(r.succeeded for r in matching) else failed_min_age
    if not outcome_shown:
        required = max(min_age, failed_min_age)
    if _aware(now) - _aware(decision.latest_end) < required:
        decision.reason = Reason.TOO_RECENT
    return decision


def decide_contents(
    contents: FolderContents,
    records: Sequence[JobRecord],
    resume_until: Optional[datetime] = None,
) -> Reason:
    """
    everything in the folder must have appeared and last changed while one job ran, from its
    earliest attempt's start to its end. A later change means something else still uses it,
    and an earlier one means the data came from somewhere else, such as a reused number, a
    folder made by hand before the job, or a tree renamed into place.
    resume_until: when an earlier removal last ran. It emptied directories, so their times are
    excused up to then and not after, when something else must have changed them.
    """
    if contents.unreadable:
        return Reason.UNREADABLE
    if contents.over_budget:
        return Reason.TOO_LARGE_TO_CHECK
    if contents.other_mount:
        return Reason.OTHER_MOUNT_INSIDE
    if contents.other_owner:
        return Reason.OTHER_OWNERS_FILES
    resuming = resume_until is not None
    newest = contents.newest_file_change if resuming else contents.newest_change
    oldest = contents.oldest_change
    if oldest is None or (newest is None and not resuming):
        return Reason.UNREADABLE
    if (
        resuming
        and contents.newest_dir_change is not None
        and _aware(contents.newest_dir_change) > _aware(resume_until) + CLOCK_SLACK
    ):
        return Reason.CHANGED_AFTER_JOB
    windows = [
        (
            _aware(r.first_run_start or r.start_time) - CLOCK_SLACK,
            _aware(r.window_end) + CLOCK_SLACK,
        )
        for r in records
        if r.start_time and r.end_time
    ]
    if not windows:
        return Reason.UNREADABLE
    oldest = _aware(oldest)
    latest = _aware(newest) if newest is not None else oldest
    if any(start <= oldest and latest <= end for start, end in windows):
        if contents.newest_access is not None and _aware(contents.newest_access) > max(
            end for _, end in windows
        ):
            return Reason.READ_AFTER_JOB
        return Reason.ELIGIBLE
    if latest > max(end for _, end in windows):
        return Reason.CHANGED_AFTER_JOB
    return Reason.OLDER_THAN_JOB


def job_time_bounds(records: Sequence[JobRecord]) -> tuple:
    """the earliest and latest a job under this number could have touched its folder"""
    return (
        min(_aware(r.first_run_start or r.start_time) for r in records) - CLOCK_SLACK,
        max(_aware(r.window_end) for r in records) + CLOCK_SLACK,
    )


@dataclass(frozen=True)
class Limits:
    enabled: bool
    dry_run: bool
    kill_switch: bool
    file_systems: tuple
    success_retention_days: int
    failed_retention_days: int
    max_folders_per_run: int
    max_run_bytes_percent: float
    max_run_share_percent: float
    digest_weekday: int = 0


# both notices have to fit before a folder's date
MINIMUM_RETENTION_DAYS = 14


def limits_problem(limits: Limits) -> Optional[str]:
    """why these settings cannot be acted on, or None"""
    if limits.success_retention_days < MINIMUM_RETENTION_DAYS:
        return f'Successful jobs must be kept at least {MINIMUM_RETENTION_DAYS} days.'
    if limits.failed_retention_days < MINIMUM_RETENTION_DAYS:
        return f'Failed jobs must be kept at least {MINIMUM_RETENTION_DAYS} days.'
    if not 0 <= limits.digest_weekday <= 6:
        return 'The digest day must be from 0, Monday, to 6, Sunday.'
    if limits.max_folders_per_run < 1:
        return 'The limit per run must be at least 1 folder.'
    if not 0 < limits.max_run_bytes_percent <= 100:
        return 'The share of capacity a run may remove must be between 1% and 100%.'
    if not 0 < limits.max_run_share_percent <= 100:
        return 'The share of job folders a run may remove must be between 1% and 100%.'
    return None


# A share of a handful of folders says nothing; below this count the per-run cap is the guard.
SHARE_CHECK_MIN_FOLDERS = 10


def plan_problem(
    *,
    planned_folders: int,
    planned_bytes: int,
    job_folders: int,
    capacity_bytes: int,
    limits: Limits,
) -> Optional[str]:
    """a plan too large to be believed stops the run instead of being trimmed to fit"""
    if capacity_bytes <= 0:
        return 'The file system reports no capacity.'
    share = 100 * planned_bytes / capacity_bytes
    if share > limits.max_run_bytes_percent:
        return (
            f'The run would remove {share:.1f}% of capacity, above the '
            f'{limits.max_run_bytes_percent:g}% limit.'
        )
    if job_folders >= SHARE_CHECK_MIN_FOLDERS:
        share = 100 * planned_folders / job_folders
        if share > limits.max_run_share_percent:
            return (
                f'The run would remove {share:.0f}% of job folders, above the '
                f'{limits.max_run_share_percent:g}% limit.'
            )
    return None


def backfill_problem(
    *,
    planned_folders: int,
    planned_bytes: int,
    job_folders: int,
    capacity_bytes: int,
    limits: Limits,
) -> Optional[str]:
    """
    the same limits, with what to do about them: on a first backfill most folders are job folders,
    so these are expected to trip, and a backfill only sets dates.
    """
    problem = plan_problem(
        planned_folders=planned_folders,
        planned_bytes=planned_bytes,
        job_folders=job_folders,
        capacity_bytes=capacity_bytes,
        limits=limits,
    )
    if problem is None or capacity_bytes <= 0:
        return problem
    safe = (
        'That is expected on a first backfill. A backfill only sets dates: nothing goes '
        'until owners have had both notices and every check passes.'
    )
    bytes_share = math.ceil(100 * planned_bytes / capacity_bytes)
    if bytes_share > limits.max_run_bytes_percent:
        return (
            f'{problem} {safe} To go ahead, set Most capacity per run to {bytes_share}% in '
            f'settings, preview again and run the backfill, then set it back to '
            f'{limits.max_run_bytes_percent:g}%, because hourly checks use it too.'
        )
    needed = math.ceil(100 * planned_folders / job_folders)
    return (
        f'{problem} {safe} To go ahead, set Most job folders per run to {needed}% in settings '
        'and preview again. Only backfills use that limit.'
    )
