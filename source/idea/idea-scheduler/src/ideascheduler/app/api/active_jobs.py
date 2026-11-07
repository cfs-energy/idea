from typing import Callable, Dict, List, Optional, Tuple

import ideascheduler
from ideadatamodel import SocaJob, SocaSortOrder
from ideadatamodel.scheduler import ListJobsRequest

# the columns an active jobs list can order. queue_time newest first is the order the
# job cache already pages in.
_SORT_VALUES: Dict[str, Callable[[SocaJob], object]] = {
    'name': lambda job: job.name.lower() if job.name else None,
    'owner': lambda job: job.owner.lower() if job.owner else None,
    'queue': lambda job: job.queue.lower() if job.queue else None,
    'project': lambda job: job.project.lower() if job.project else None,
    'queue_time': lambda job: job.queue_time,
}
_SEARCHED_FIELDS = ('job_id', 'name', 'owner', 'queue', 'project', 'state')


def _text(value: object) -> str:
    return str(getattr(value, 'value', value))


def _matches(job: SocaJob, key: str, value: object) -> bool:
    if key == '$all':
        needle = _text(value).lower()
        return any(
            needle in _text(getattr(job, field)).lower()
            for field in _SEARCHED_FIELDS
            if getattr(job, field, None) is not None
        )
    # a client may name the keyword sub-field the completed jobs index needs
    actual = getattr(job, key.removesuffix('.raw'), None)
    wanted = value if isinstance(value, list) else [value]
    return actual is not None and _text(actual) in [_text(item) for item in wanted]


def _is_cache_order(sort_by) -> bool:
    return sort_by is None or (
        sort_by.key == 'queue_time' and sort_by.order == SocaSortOrder.DESC
    )


def _sort(jobs: List[SocaJob], sort_by) -> List[SocaJob]:
    value_of = _SORT_VALUES.get(sort_by.key)
    if value_of is None:
        return jobs
    with_value = [job for job in jobs if value_of(job) is not None]
    without_value = [job for job in jobs if value_of(job) is None]
    with_value.sort(key=value_of, reverse=sort_by.order == SocaSortOrder.DESC)
    return with_value + without_value


def list_active_job_page(
    context: 'ideascheduler.AppContext',
    payload: ListJobsRequest,
    owner: Optional[str] = None,
) -> Tuple[List[SocaJob], int]:
    """
    one page of active jobs and how many there are in all.

    the job cache can only page newest first, so a search or another order reads every
    job in scope. the cache is local and holds the jobs still in the scheduler, hundreds
    rather than millions, and only a person searching or sorting pays for this: the
    plain listing and auto refresh keep the cheap read.
    """
    # the job cache filters on the literal owner: never leave it out of a user's read
    scope = {} if owner is None else {'owner': owner}
    filters = [
        entry
        for entry in payload.filters or []
        if entry.key and entry.value not in (None, '', [])
    ]

    if not filters and _is_cache_order(payload.sort_by):
        entries = context.job_cache.list_jobs(
            **scope, _limit=payload.page_size, _offset=payload.page_start
        )
        return entries, context.job_cache.get_count(**scope)

    jobs = context.job_cache.list_jobs(**scope, order_by='-id')
    for entry in filters:
        jobs = [job for job in jobs if _matches(job, entry.key, entry.value)]
    if payload.sort_by is not None:
        jobs = _sort(jobs, payload.sort_by)
    start = payload.page_start or 0
    return jobs[start : start + (payload.page_size or len(jobs))], len(jobs)
