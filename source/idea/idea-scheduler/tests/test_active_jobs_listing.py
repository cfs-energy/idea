"""
Test Cases for the search box and sort on the active jobs lists

The job cache can only page newest first, so both are applied in the API layer.
"""

from datetime import datetime, timezone

import pytest
from ideadatamodel import SocaFilter, SocaJob
from ideadatamodel.scheduler import ListJobsRequest
from ideascheduler.app.api.active_jobs import list_active_job_page


class _JobCache:
    def __init__(self, jobs):
        self.jobs = jobs
        self.list_calls = []

    def list_jobs(self, _limit=-1, _offset=0, **kwargs):
        self.list_calls.append({'_limit': _limit, '_offset': _offset, **kwargs})
        order_by = kwargs.pop('order_by', None)
        assert order_by in (None, '-id')
        jobs = [
            job
            for job in self.jobs
            if all(getattr(job, key) == value for key, value in kwargs.items())
        ]
        if _limit > 0:
            return jobs[_offset : _offset + _limit]
        return jobs

    def get_count(self, **kwargs):
        return len(self.list_jobs(**kwargs))


class _Context:
    def __init__(self, jobs):
        self.job_cache = _JobCache(jobs)


def _job(job_id, name, owner='researcher', queue='normal', day=1):
    return SocaJob(
        job_id=job_id,
        name=name,
        owner=owner,
        queue=queue,
        project='fusion',
        queue_time=datetime(2026, 8, day, tzinfo=timezone.utc),
    )


@pytest.fixture()
def jobs():
    return [
        _job('103', 'Plasma scan', day=3),
        _job('102', 'protein study', queue='gpu', day=2),
        _job('101', 'Beam tuning', owner='other', day=1),
    ]


def _request(filters=None, sort_by=None, start=0, page_size=30):
    return ListJobsRequest(
        filters=filters,
        sort_by=sort_by,
        paginator={'start': start, 'page_size': page_size},
    )


def _ids(page):
    return [job.job_id for job in page[0]]


def test_job_id_search_finds_one_job_and_the_total_says_so(jobs):
    page = list_active_job_page(
        _Context(jobs), _request([SocaFilter(key='job_id', value='102')])
    )
    assert _ids(page) == ['102']
    assert page[1] == 1


def test_comma_list_of_job_ids(jobs):
    page = list_active_job_page(
        _Context(jobs), _request([SocaFilter(key='job_id', value=['101', '103'])])
    )
    assert _ids(page) == ['103', '101']


def test_free_text_search_ignores_case_and_covers_the_visible_columns(jobs):
    context = _Context(jobs)

    def search(text):
        return _ids(
            list_active_job_page(
                context, _request([SocaFilter(key='$all', value=text)])
            )
        )

    assert search('PROTEIN') == ['102']
    assert search('gpu') == ['102']
    assert search('no such job') == []


def test_search_stays_inside_the_owner_scope(jobs):
    page = list_active_job_page(
        _Context(jobs),
        _request([SocaFilter(key='$all', value='beam')]),
        owner='researcher',
    )
    assert _ids(page) == []
    assert page[1] == 0


def test_search_pages_over_the_matches(jobs):
    page = list_active_job_page(
        _Context(jobs),
        _request([SocaFilter(key='$all', value='normal')], start=1, page_size=1),
    )
    assert _ids(page) == ['101']
    assert page[1] == 2


def test_sort_applies_across_every_row_not_one_page(jobs):
    page = list_active_job_page(
        _Context(jobs),
        _request(sort_by={'key': 'name', 'order': 'asc'}, page_size=1),
    )
    assert _ids(page) == ['101']
    assert page[1] == 3


def test_jobs_with_no_value_sort_last_either_way():
    jobs = [_job('1', 'b'), SocaJob(job_id='2'), _job('3', 'a')]
    for order, expected in (('asc', ['3', '1', '2']), ('desc', ['1', '3', '2'])):
        page = list_active_job_page(
            _Context(jobs), _request(sort_by={'key': 'name', 'order': order})
        )
        assert _ids(page) == expected


@pytest.mark.parametrize('sort_by', [None, {'key': 'queue_time', 'order': 'desc'}])
def test_plain_listing_keeps_the_paged_read(jobs, sort_by):
    context = _Context(jobs)
    page = list_active_job_page(
        context, _request(sort_by=sort_by, start=1, page_size=1)
    )
    assert _ids(page) == ['102']
    assert page[1] == 3
    assert context.job_cache.list_calls[0]['_limit'] == 1
    assert context.job_cache.list_calls[0]['_offset'] == 1
