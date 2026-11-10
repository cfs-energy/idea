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
Where a job's state comes from. Three sources, each read so a failure cannot look like an
answer: PBS for what is not over (qselect and qstat, raising instead of returning nothing),
the job cache for what the scheduler is still tracking, and the cache plus the index for
finished records. Any source failing raises StateUnavailable, and the run stops.
"""

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
import re

from ideadatamodel import SocaJob, SocaJobState
from ideascheduler.app.scheduler.openpbs import openpbs_constants
from ideascheduler.app.scratch_cleanup.rules import (
    ActiveJobs,
    JobRecord,
    base_job_id,
)

_DEPENDENCY_ID = re.compile(r':([0-9]{1,18})(?:\[[0-9]*\])?(?=[.:",]|$)', re.ASCII)
_DEPEND_FIELD = re.compile(r'"depend"\s*:\s*"([^"]*)"')


_SUBMIT_ARGUMENTS = re.compile(r'"Submit_arguments"\s*:\s*"([^\n]*)"')
_VARIABLE_LIST = re.compile(
    r'"Variable_List"\s*:\s*\{(.*?)(?:\n\s*\}|\}\s*,|\}\s*\}|\Z)', re.DOTALL
)
_JOB_OWNER = re.compile(r'"Job_Owner"\s*:\s*"([^"@]+)')
_STOPPED_COMMENT = re.compile(r'delete|terminat|kill|abort', re.IGNORECASE)
_NUMBER = re.compile(r'(?<![0-9])[0-9]{1,18}(?![0-9])', re.ASCII)


def _folders_named(text: str, mount_aliases: Sequence[str]) -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for alias in mount_aliases:
        pattern = re.compile(
            re.escape(alias.rstrip('/')) + r'/([^/\s":,\\]+)/([^/\s":,\\]+)'
        )
        found.update(pattern.findall(text))
    return found


class StateUnavailable(Exception):
    pass


class ScratchJobState:
    def __init__(self, context, logger):
        self._context = context
        self._logger = logger
        self._script_cache: Dict[str, str] = {}

    def active_jobs(self, mount_aliases: Sequence[str]) -> ActiveJobs:
        """
        everything not over, or stop: every state PBS lists (no filter is passed), everything
        the job cache tracks and everything on a node. Zero jobs is only believed when nothing
        else says the cluster is busy.
        """
        context = self._context
        try:
            if not context.scheduler.is_ready():
                raise StateUnavailable('PBS is not accepting requests.')
            if not context.job_cache.is_ready():
                raise StateUnavailable('The job cache is not ready.')
            result = context.shell.invoke(
                [openpbs_constants.QSELECT], skip_error_logging=True
            )
            if result.returncode != 0:
                raise StateUnavailable(
                    f'qselect failed with exit code {result.returncode}.'
                )
            pbs_ids = [
                line.strip()
                for line in str(result.stdout or '').splitlines()
                if line.strip()
            ]
            cached = context.job_cache.list_jobs()
            tracked = [job.job_id for job in cached]
            on_nodes = [
                job_id
                for node in context.scheduler.list_nodes()
                for job_id in node.jobs or []
            ]
        except StateUnavailable:
            raise
        except Exception as e:
            raise StateUnavailable(f'Active jobs could not be read: {e}') from e

        base_ids: Set[str] = set()
        for job_id in [*(i.split('.')[0] for i in pbs_ids), *tracked, *on_nodes]:
            base = base_job_id(str(job_id).split('.')[0].split('/')[0].strip())
            if base is None:
                raise StateUnavailable(
                    f'Unexpected job id from the scheduler: {job_id!r}'
                )
            base_ids.add(base)

        if not pbs_ids:
            if tracked:
                raise StateUnavailable(
                    f'PBS lists no active jobs while the job cache tracks {len(tracked)}.'
                )
            if on_nodes:
                raise StateUnavailable(
                    f'PBS lists no active jobs while nodes are running {len(on_nodes)}.'
                )

        text = self._submission_text(bool(pbs_ids))
        dependencies = {
            found
            for field in _DEPEND_FIELD.findall(text)
            for found in _DEPENDENCY_ID.findall(field)
        }
        mentioned = {
            found
            for pattern in (_SUBMIT_ARGUMENTS, _VARIABLE_LIST)
            for field in pattern.findall(text)
            for found in _NUMBER.findall(field)
        }
        owners = {name.lower() for name in _JOB_OWNER.findall(text)}
        if len(_JOB_OWNER.findall(text)) < len(pbs_ids):
            raise StateUnavailable('qstat did not name the owner of every active job.')
        owners.update(job.owner.lower() for job in cached if job.owner)
        referenced = _folders_named(text, mount_aliases)
        for script in self._scripts(pbs_ids):
            mentioned.update(_NUMBER.findall(script))
            referenced.update(_folders_named(script, mount_aliases))
        return ActiveJobs(
            base_ids=frozenset(base_ids),
            dependency_ids=frozenset(dependencies),
            mentioned_ids=frozenset(mentioned),
            referenced_folders=frozenset(referenced),
            owners=frozenset(owners),
        )

    def _submission_text(self, expect_jobs: bool) -> str:
        """
        what active jobs asked for, as raw text: PBS can emit invalid JSON in the environment
        block, and that block is where PBS_O_WORKDIR is. Matching text over-matches at worst,
        which only keeps more.
        """
        result = self._context.shell.invoke(
            [openpbs_constants.QSTAT, '-f', '-F', 'json'], skip_error_logging=True
        )
        if result.returncode != 0:
            raise StateUnavailable(f'qstat failed with exit code {result.returncode}.')
        text = str(result.stdout or '').replace('\\/', '/')
        if expect_jobs and '"Jobs"' not in text:
            raise StateUnavailable('qstat listed none of the active jobs.')
        return text

    def _scripts(self, pbs_ids: List[str]) -> List[str]:
        """
        each active job's script, which can reach a folder its submission never names. A script
        does not change once submitted, so each is read once.
        """
        wanted = set(pbs_ids)
        self._script_cache = {
            k: v for k, v in self._script_cache.items() if k in wanted
        }
        for job_id in pbs_ids:
            if job_id in self._script_cache:
                continue
            try:
                result = self._context.shell.invoke(
                    [openpbs_constants.PRINTJOB, '-s', job_id],
                    skip_error_logging=True,
                )
                if result.returncode == 0:
                    self._script_cache[job_id] = str(result.stdout or '').replace(
                        '\\/', '/'
                    )
                elif self._context.scheduler.is_job_active(job_id):
                    raise StateUnavailable(
                        f'The script of active job {job_id} could not be read.'
                    )
            except StateUnavailable:
                raise
            except Exception as e:
                raise StateUnavailable(
                    f'The script of active job {job_id} could not be read: {e}'
                ) from e
        return list(self._script_cache.values())

    def finished_records(self, job_id_forms: Iterable[str]) -> List[JobRecord]:
        """every record under these numbers, from the cache and the index"""
        jobs: Dict[Tuple, SocaJob] = {}
        context = self._context
        job_id_forms = set(job_id_forms)
        try:
            for form in job_id_forms:
                for job in context.job_cache.list_completed_jobs(job_id=form):
                    jobs[(job.job_uid or job.job_id, job.job_id, job.owner)] = job
            store = context.document_store
            if store.is_enabled():
                if not store.is_initialized():
                    raise StateUnavailable('The job index is not ready.')
                for job in store.find_jobs_by_id(sorted(job_id_forms)):
                    jobs.setdefault(
                        (job.job_uid or job.job_id, job.job_id, job.owner), job
                    )
        except StateUnavailable:
            raise
        except Exception as e:
            raise StateUnavailable(f'Job records could not be read: {e}') from e
        records = [_as_record(job) for job in jobs.values()]
        # an array has one record, which carries how each of its subjobs ended. A subjob's
        # folder is judged by its own entry there.
        records.extend(
            _as_subjob_record(job, subjob)
            for job in jobs.values()
            for subjob in job.subjobs or []
            if subjob.job_id in job_id_forms
        )
        return records

    def find_job(self, record: JobRecord) -> Optional[SocaJob]:
        """the full job behind a record, to mark its folder as removed"""
        context = self._context
        if record.job_uid:
            job = context.job_cache.get_completed_job_by_uid(record.job_uid)
            if job is None:
                job = context.document_store.get_job(job_uid=record.job_uid)
            return job
        return context.job_cache.get_completed_job(record.job_id)


def is_finished(job: SocaJob) -> bool:
    """PBS marks a subjob that is done as expired, and a subjob only"""
    return job.state == SocaJobState.FINISHED or (
        job.state == SocaJobState.SUBJOB_EXPIRED and '[' in str(job.job_id)
    )


def job_succeeded(job: SocaJob) -> bool:
    """
    success has to be shown: PBS Exit_status 0, on a finished job the scheduler recorded as having
    run, with nothing saying it was stopped. Older records carry no disposition, and a job deleted
    while running can still exit 0, so those are failures, which only wait longer. PBS keeps
    the last subjob's status on an array, so an array's own 0 counts only with the subjob
    outcomes it was worked out from; a record without them is from an earlier build.
    """
    if str(job.job_id).endswith('[]') and not job.subjobs:
        return False
    return (
        is_finished(job)
        and job.exit_status == 0
        and job.disposition == 'ran'
        and job.reason_class != 'cancelled'
        and not _stopped(job)
    )


def _stopped(job: SocaJob) -> bool:
    """
    PBS's history substate says whether qdel ended the job. Records from before it was kept fall
    back to the comment, which an error message can replace.
    """
    if job.terminated is not None:
        return job.terminated
    return bool(_STOPPED_COMMENT.search(job.comment or ''))


def _first_run_start(job: SocaJob) -> Optional[datetime]:
    starts = [
        run.start
        for host in job.execution_hosts or []
        if host.execution
        for run in host.execution.runs or []
        if run.start is not None
    ]
    return min(starts) if starts else None


def _as_subjob_record(job: SocaJob, subjob) -> JobRecord:
    """
    one subjob of an array, from the array's record. Success has to be shown for the subjob
    itself (finished, exit status 0, and PBS saying qdel did not end it) and for its array,
    whose folders share one date: the array's end plus the successful time only when every
    subjob succeeded. A subjob can end weeks before its array does, so the date never counts
    from the subjob's own end; that only bounds when its files were written. Without its own
    end the record is incomplete and the folder stays.
    """
    array = _as_record(job)
    finished = subjob.state in (SocaJobState.FINISHED, SocaJobState.SUBJOB_EXPIRED)
    return JobRecord(
        job_id=str(subjob.job_id),
        job_uid=array.job_uid,
        owner=array.owner,
        finished=finished and array.finished,
        succeeded=(
            finished
            and subjob.exit_status == 0
            and subjob.terminated is False
            and array.succeeded
        ),
        start_time=subjob.start_time,
        end_time=array.end_time if subjob.end_time else None,
        dependents=array.dependents,
        array_id=array.job_id,
        run_end=subjob.end_time,
    )


def _as_record(job: SocaJob) -> JobRecord:
    return JobRecord(
        job_id=str(job.job_id),
        job_uid=job.job_uid or None,
        owner=job.owner or None,
        finished=is_finished(job),
        succeeded=job_succeeded(job),
        start_time=job.start_time,
        end_time=job.end_time,
        first_run_start=_first_run_start(job),
        dependents=frozenset(
            base
            for base in (base_job_id(str(i)) for i in job.dependent_job_ids or [])
            if base
        ),
    )
