"""
Test Cases for the per user cost summary

The service runs against fakes for the projects service, the opensearch client and the
pricing helper. The bedrock read path is the real one, so the usage rows are filtered by
username by the same code the projects page uses.
"""

from ideaclustermanager.app.costs.my_costs_service import MyCostsService
from ideaclustermanager.app.projects.bedrock_usage_service import (
    get_project_window_usage,
    usage_window_dates,
)
from ideaclustermanager.app.projects.db.bedrock_usage_dao import build_day_key

from ideadatamodel import (
    exceptions,
    locale,
    ListProjectsResult,
    Project,
    ProjectBedrockConfig,
    SocaAmount,
)

import arrow
import pytest

USER = 'user-a'
OTHER_USER = 'user-b'
PROJECT_ID = 'a1b2c3d4-0000-4000-8000-000000000001'
PROJECT_NAME = 'project-a'
MODEL_A = 'vendor-a.model-1'
MODEL_B = 'vendor-b.model-9'


@pytest.fixture(autouse=True)
def initialized_locale():
    # SocaAmount reads the currency code the app context normally initializes.
    try:
        locale.get_currency_code()
    except Exception:
        locale.init('C')


class FakeUsageDAO:
    """day rows keyed the way the real dao keys them, queried by date range."""

    def __init__(self):
        self.rows = []
        self.table = 'fake-usage-table'
        # the self scoped path must read the window once per project, not twice
        self.query_calls = 0

    def add(self, username, model_id, invocations, input_tokens, output_tokens, day):
        self.rows.append(
            {
                'project_id': PROJECT_ID,
                'usage_id': build_day_key(day, username, model_id),
                'usage_date': day,
                'username': username,
                'model_id': model_id,
                'invocations': invocations,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'updated_on': 0,
            }
        )

    def query_day_rows(self, project_id, start_date, end_date):
        self.query_calls += 1
        return [
            row
            for row in self.rows
            if row['project_id'] == project_id
            and start_date <= row['usage_date'] <= end_date
        ]


class FakeProjectsDAO:
    def __init__(self, projects):
        self.projects = projects

    def list_projects(self, request):
        return ListProjectsResult(listing=self.projects, paginator=None)


class FakeBedrockProvisioner:
    @staticmethod
    def is_enabled():
        return True


class FakeProjectsService:
    """
    stands in for the projects service. get_project_bedrock_usage delegates to the real
    window read so the username filter under test is the shipped one.
    """

    def __init__(self, usage_dao, projects, spend=None):
        self.usage_dao = usage_dao
        # the self scoped path reads the dao itself so one page serves both views
        self.bedrock_usage_dao = usage_dao
        self.bedrock_provisioner = FakeBedrockProvisioner()
        self.projects = projects
        self.spend = spend
        self.projects_dao = FakeProjectsDAO(projects)

    def apply_bedrock_spend(self, usage, project_name):
        if self.spend is not None:
            usage.spend = SocaAmount(amount=self.spend)
        else:
            usage.spend_is_unavailable = True

    def get_user_projects(self, request):
        assert request.username == USER
        return type('Result', (), {'projects': self.projects})()

    def get_project_bedrock_usage(self, project_id, username=None, project_name=None):
        start_date, end_date = usage_window_dates()
        usage = get_project_window_usage(
            self.usage_dao, project_id, start_date, end_date, username=username
        )
        if usage is None or username is not None:
            return usage
        if self.spend is not None:
            usage.spend = SocaAmount(amount=self.spend)
        else:
            usage.spend_is_unavailable = True
        return usage


class FakeConfig:
    def __init__(self, values, scheduler_enabled=True):
        self.values = dict(values)
        self.scheduler_enabled = scheduler_enabled

    def get_string(self, key, default=None, required=False, module_id=None):
        return self.values.get(key, default)

    def is_module_enabled(self, module_name):
        return self.scheduler_enabled

    def get_module_id(self, module_name):
        return module_name


class FakeOsClient:
    def __init__(self, responses):
        self.responses = responses
        self.queries = []

    def search(self, index, body):
        self.queries.append((index, body))
        for prefix, response in self.responses.items():
            if index.startswith(prefix) or prefix in index:
                if isinstance(response, Exception):
                    raise response
                return response
        return {'hits': {'total': {'value': 0}, 'hits': []}, 'aggregations': {}}


class FakeAnalyticsService:
    def __init__(self, os_client):
        self.os_client = type('Wrapper', (), {'os_client': os_client})()


class FakeAwsUtil:
    def __init__(self, prices):
        self.prices = prices

    def get_ec2_instance_type_unit_price(self, instance_type):
        # the shipped helper reports an unanswered lookup as 0.0, not as an error.
        return type('Price', (), {'ondemand': self.prices.get(instance_type, 0.0)})()


class FakeLogger:
    def warning(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass


class FakeHistoryTable:
    def __init__(self, rows):
        self.rows = rows

    def query(self, **kwargs):
        return {'Items': list(self.rows)}

    def scan(self, **kwargs):
        return {'Items': list(self.rows)}


class FakeDynamoDB:
    def __init__(self, rows):
        self.rows = rows

    def Table(self, _name):
        return FakeHistoryTable(self.rows)


class FakeAws:
    def __init__(self, rows):
        self._rows = rows

    def dynamodb_table(self):
        return FakeDynamoDB(self._rows)


class FakeContext:
    def __init__(
        self, projects, config, analytics_service, aws_util, history_rows=None
    ):
        self.projects = projects
        self._config = config
        self._analytics_service = analytics_service
        self._aws_util = aws_util
        self._aws = FakeAws(history_rows if history_rows is not None else [])

    def aws(self):
        return self._aws

    def config(self):
        return self._config

    def analytics_service(self):
        return self._analytics_service

    def aws_util(self):
        return self._aws_util

    def cluster_name(self):
        return 'idea-test'

    def module_id(self):
        return 'cluster-manager'

    def logger(self, _name=None):
        return FakeLogger()


def build_project():
    return Project(
        project_id=PROJECT_ID,
        name=PROJECT_NAME,
        title='Project A',
        bedrock=ProjectBedrockConfig(),
    )


def build_service(
    usage_dao=None,
    projects=None,
    spend=None,
    os_responses=None,
    prices=None,
    config_values=None,
    scheduler_enabled=True,
    history_rows=None,
):
    usage_dao = usage_dao if usage_dao is not None else FakeUsageDAO()
    projects = projects if projects is not None else [build_project()]
    os_client = FakeOsClient(os_responses or {})
    context = FakeContext(
        projects=FakeProjectsService(usage_dao, projects, spend=spend),
        config=FakeConfig(
            config_values
            if config_values is not None
            else {
                'virtual-desktop-controller.opensearch.dcv_session.alias': 'idea-test_vdc_user_sessions'
            },
            scheduler_enabled=scheduler_enabled,
        ),
        analytics_service=FakeAnalyticsService(os_client),
        aws_util=FakeAwsUtil(prices or {}),
        history_rows=history_rows,
    )
    return MyCostsService(context), os_client


def today():
    return arrow.utcnow().format('YYYY-MM-DD')


# ai


def test_ai_sums_only_the_callers_rows():
    usage_dao = FakeUsageDAO()
    usage_dao.add(USER, MODEL_A, 2, 100, 50, today())
    usage_dao.add(OTHER_USER, MODEL_A, 8, 700, 350, today())

    service, _ = build_service(usage_dao=usage_dao, spend=120.0)
    ai = service.get_summary(USER).ai

    assert len(ai.projects) == 1
    row = ai.projects[0]
    assert row.total_tokens == 150
    assert row.invocations == 2
    assert ai.total_tokens == 150
    # the other user's 1050 tokens are in the project total but not in the caller's
    assert row.cost == pytest.approx(120.0 * 150 / 1200)
    assert row.estimated is True
    assert ai.estimated is True


def test_ai_apportions_per_model_within_the_project():
    usage_dao = FakeUsageDAO()
    usage_dao.add(USER, MODEL_A, 1, 100, 100, today())
    usage_dao.add(USER, MODEL_B, 1, 50, 50, today())

    service, _ = build_service(usage_dao=usage_dao, spend=60.0)
    row = service.get_summary(USER).ai.projects[0]

    by_model = {model.model_id: model for model in row.by_model}
    assert by_model[MODEL_A].total_tokens == 200
    assert by_model[MODEL_B].total_tokens == 100
    assert by_model[MODEL_A].cost == pytest.approx(40.0)
    assert by_model[MODEL_B].cost == pytest.approx(20.0)
    assert row.cost == pytest.approx(60.0)


def test_ai_marks_cost_unavailable_rather_than_zero_when_spend_is_missing():
    usage_dao = FakeUsageDAO()
    usage_dao.add(USER, MODEL_A, 1, 10, 10, today())

    service, _ = build_service(usage_dao=usage_dao, spend=None)
    row = service.get_summary(USER).ai.projects[0]

    assert row.cost_unavailable is True
    assert row.total_tokens == 20
    assert row.by_model[0].cost is None


def test_ai_skips_projects_the_caller_never_used():
    usage_dao = FakeUsageDAO()
    usage_dao.add(OTHER_USER, MODEL_A, 5, 500, 500, today())

    service, _ = build_service(usage_dao=usage_dao, spend=10.0)
    ai = service.get_summary(USER).ai

    assert ai.projects == []
    assert ai.total_tokens == 0


# jobs


JOBS_RESPONSE = {
    'hits': {
        'total': {'value': 3},
        'hits': [
            {
                '_source': {
                    'job_id': '101',
                    'name': 'solve',
                    'queue': 'normal',
                    'project': PROJECT_NAME,
                    'end_time': '2026-08-30T10:00:00+00:00',
                    'estimated_bom_cost': {'total': {'amount': 4.5, 'unit': 'USD'}},
                }
            }
        ],
    },
    'aggregations': {
        'cost': {'value': 12.75},
        'by_project': {
            'buckets': [{'key': PROJECT_NAME, 'doc_count': 3, 'cost': {'value': 12.75}}]
        },
        'by_queue': {
            'buckets': [
                {'key': 'normal', 'doc_count': 2, 'cost': {'value': 9.0}},
                {'key': 'high', 'doc_count': 1, 'cost': {'value': 3.75}},
            ]
        },
    },
}


def test_jobs_reports_totals_breakdowns_and_recent_jobs():
    service, os_client = build_service(os_responses={'_scheduler_jobs': JOBS_RESPONSE})
    jobs = service.get_summary(USER).jobs

    assert jobs.job_count == 3
    assert jobs.cost == pytest.approx(12.75)
    assert jobs.estimated is True
    assert [group.name for group in jobs.by_queue] == ['normal', 'high']
    assert jobs.by_project[0].job_count == 3
    assert jobs.recent_jobs[0].job_id == '101'
    assert jobs.recent_jobs[0].cost == pytest.approx(4.5)


def test_jobs_query_is_scoped_to_the_caller():
    service, os_client = build_service(os_responses={'_scheduler_jobs': JOBS_RESPONSE})
    service.get_summary(USER)

    index, body = next(
        query for query in os_client.queries if query[0].endswith('_jobs')
    )
    assert index == 'idea-test_scheduler_jobs'
    filters = body['query']['bool']['filter']
    assert {'term': {'owner.raw': USER}} in filters


def test_jobs_unavailable_when_the_scheduler_is_not_deployed():
    service, _ = build_service(scheduler_enabled=False)
    assert service.get_summary(USER).jobs.is_unavailable is True


def test_jobs_unavailable_when_the_search_fails():
    service, _ = build_service(
        os_responses={'_scheduler_jobs': RuntimeError('index_not_found_exception')}
    )
    assert service.get_summary(USER).jobs.is_unavailable is True


# desktops


def session_hit(
    session_id,
    state,
    instance_type,
    created_on,
    updated_on,
    stopped_on=None,
    cleanup_warning_stop_time=None,
):
    source = {
        'idea_session_id': session_id,
        'name': session_id,
        'base_os': 'amazonlinux2023',
        'state': state,
        'created_on': created_on,
        'updated_on': updated_on,
        'server': {'instance_type': instance_type},
    }
    if stopped_on is not None:
        source['stopped_on'] = stopped_on
    if cleanup_warning_stop_time is not None:
        source['cleanup_warning_stop_time'] = cleanup_warning_stop_time
    return {'_source': source}


def test_desktops_prices_recorded_hours_at_the_ondemand_rate():
    now = arrow.utcnow()
    created = now.shift(hours=-4).int_timestamp * 1000
    updated = now.shift(hours=-2).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-1', 'STOPPED', 'm5.large', created, updated)
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    desktops = service.get_summary(USER).desktops

    assert desktops.session_count == 1
    session = desktops.sessions[0]
    assert session.hours == pytest.approx(2.0, abs=0.01)
    assert session.cost == pytest.approx(0.2, abs=0.01)
    assert session.estimated is True
    assert session.price_unavailable is None
    assert desktops.hours == pytest.approx(2.0, abs=0.01)


def test_desktops_running_session_is_measured_to_now():
    now = arrow.utcnow()
    created = now.shift(hours=-3).int_timestamp * 1000
    updated = now.shift(hours=-3).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-2', 'READY', 'm5.large', created, updated)
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    session = service.get_summary(USER).desktops.sessions[0]

    assert session.hours == pytest.approx(3.0, abs=0.01)


def test_desktops_report_hours_without_cost_when_the_price_is_unavailable():
    now = arrow.utcnow()
    created = now.shift(hours=-5).int_timestamp * 1000
    updated = now.shift(hours=-4).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-3', 'STOPPED', 'zz.unknown', created, updated)
                    ]
                }
            }
        },
        prices={},
    )
    desktops = service.get_summary(USER).desktops
    session = desktops.sessions[0]

    assert session.price_unavailable is True
    assert session.cost is None
    assert session.hours == pytest.approx(1.0, abs=0.01)
    assert desktops.cost == 0.0


def test_desktops_query_is_scoped_to_the_caller():
    service, os_client = build_service(
        os_responses={'user_sessions': {'hits': {'hits': []}}}
    )
    service.get_summary(USER)

    index, body = next(
        query for query in os_client.queries if 'user_sessions' in query[0]
    )
    filters = body['query']['bool']['filter']
    assert {'term': {'owner.raw': USER}} in filters


def test_desktops_unavailable_when_the_alias_is_not_configured():
    service, _ = build_service(config_values={})
    assert service.get_summary(USER).desktops.is_unavailable is True


# summary


def test_summary_reports_the_trailing_window_and_the_caller():
    service, _ = build_service()
    result = service.get_summary(USER)

    start_date, end_date = usage_window_dates()
    assert result.username == USER
    assert result.window == 'last_30_days'
    assert result.start_date == start_date
    assert result.end_date == end_date
    assert result.ai is not None
    assert result.jobs is not None
    assert result.desktops is not None


# unpriced rules


def test_desktops_counts_sessions_that_could_not_be_priced():
    now = arrow.utcnow()
    created = now.shift(hours=-4).int_timestamp * 1000
    updated = now.shift(hours=-2).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-1', 'STOPPED', 'm5.large', created, updated),
                        session_hit(
                            'sess-2', 'STOPPED', 'zz.unknown', created, updated
                        ),
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    desktops = service.get_summary(USER).desktops

    assert desktops.session_count == 2
    assert desktops.unpriced_sessions == 1
    # the subtotal covers only the session that had a price
    assert desktops.cost == pytest.approx(0.2, abs=0.01)


def test_desktops_report_every_session_unpriced_when_no_price_is_known():
    now = arrow.utcnow()
    created = now.shift(hours=-4).int_timestamp * 1000
    updated = now.shift(hours=-2).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit(
                            'sess-1', 'STOPPED', 'm6g.xlarge', created, updated
                        ),
                        session_hit(
                            'sess-2', 'STOPPED', 'g4dn.2xlarge', created, updated
                        ),
                    ]
                }
            }
        },
        prices={},
    )
    desktops = service.get_summary(USER).desktops

    # real hours with no prices: the caller must be able to tell that from a genuine
    # zero.
    assert desktops.session_count == 2
    assert desktops.unpriced_sessions == 2
    assert desktops.hours > 0


def test_jobs_counts_jobs_the_scheduler_never_priced():
    response = dict(JOBS_RESPONSE)
    response['aggregations'] = dict(JOBS_RESPONSE['aggregations'])
    response['aggregations']['priced'] = {'value': 2}

    service, _ = build_service(os_responses={'_scheduler_jobs': response})
    jobs = service.get_summary(USER).jobs

    assert jobs.job_count == 3
    assert jobs.unpriced_jobs == 1


def test_a_job_whose_instance_hours_could_not_be_priced_is_not_reported_as_free():
    """
    the scheduler still records an estimate, it is just missing the compute. the amount
    it carries has to read as unavailable rather than as what the job cost.
    """
    response = dict(JOBS_RESPONSE)
    response['hits'] = {
        'total': {'value': 1},
        'hits': [
            {
                '_source': {
                    'job_id': '102',
                    'name': 'solve',
                    'queue': 'normal',
                    'project': PROJECT_NAME,
                    'end_time': '2026-08-30T10:00:00+00:00',
                    'estimated_bom_cost': {
                        'total': {'amount': 0.8, 'unit': 'USD'},
                        'price_unavailable': True,
                    },
                }
            }
        ],
    }
    response['aggregations'] = dict(JOBS_RESPONSE['aggregations'])
    response['aggregations']['unavailable'] = {'doc_count': 1}

    service, _ = build_service(os_responses={'_scheduler_jobs': response})
    jobs = service.get_summary(USER).jobs

    assert jobs.recent_jobs[0].cost_unavailable is True
    assert jobs.cost_unavailable is True


def test_a_job_that_really_cost_nothing_still_reads_as_zero():
    """
    a job cancelled or failed before it ran was priced normally. unavailability is read
    from the flag the scheduler wrote, never inferred from a zero amount.
    """
    response = dict(JOBS_RESPONSE)
    response['hits'] = {
        'total': {'value': 1},
        'hits': [
            {
                '_source': {
                    'job_id': '103',
                    'name': 'cancelled',
                    'queue': 'normal',
                    'project': PROJECT_NAME,
                    'end_time': '2026-08-30T10:00:00+00:00',
                    'estimated_bom_cost': {'total': {'amount': 0.0, 'unit': 'USD'}},
                }
            }
        ],
    }

    service, _ = build_service(os_responses={'_scheduler_jobs': response})
    jobs = service.get_summary(USER).jobs

    assert jobs.recent_jobs[0].cost == 0.0
    assert jobs.recent_jobs[0].cost_unavailable is False
    assert jobs.cost_unavailable is False


def test_the_jobs_query_counts_the_estimates_that_are_missing_their_compute():
    """
    such a job does carry an amount, so the existing priced count finds nothing wrong
    with it and only this aggregation can.
    """
    service, os_client = build_service(os_responses={'_scheduler_jobs': JOBS_RESPONSE})
    service.get_summary(USER)

    _, body = next(query for query in os_client.queries if query[0].endswith('_jobs'))
    assert body['aggs']['unavailable'] == {
        'filter': {'term': {'estimated_bom_cost.price_unavailable': True}}
    }


# admin listing


def test_the_admin_listing_marks_a_user_whose_job_cost_is_incomplete():
    jobs = {
        'hits': {'total': {'value': 0}, 'hits': []},
        'aggregations': {
            'by_user': {
                'buckets': [
                    {
                        'key': USER,
                        'doc_count': 2,
                        'cost': {'value': 1.5},
                        'priced': {'value': 2},
                        'unavailable': {'doc_count': 1},
                    },
                    {
                        'key': OTHER_USER,
                        'doc_count': 2,
                        'cost': {'value': 9.0},
                        'priced': {'value': 2},
                        'unavailable': {'doc_count': 0},
                    },
                ]
            }
        },
    }

    service, _ = build_service(os_responses={'_scheduler_jobs': jobs}, config_values={})
    rows = {row.username: row for row in service.list_user_costs().listing}

    assert rows[USER].job_cost_unavailable is True
    # every job of theirs was priced, so their total is the whole story
    assert rows[OTHER_USER].job_cost_unavailable is None
    assert rows[OTHER_USER].job_cost == pytest.approx(9.0)


def test_list_user_costs_aggregates_across_the_three_sources():
    usage_dao = FakeUsageDAO()
    usage_dao.add(USER, MODEL_A, 2, 100, 50, today())
    usage_dao.add(OTHER_USER, MODEL_A, 8, 700, 350, today())

    now = arrow.utcnow()
    created = now.shift(hours=-4).int_timestamp * 1000
    updated = now.shift(hours=-2).int_timestamp * 1000
    sessions = {
        'hits': {
            'hits': [
                dict(
                    session_hit('sess-1', 'STOPPED', 'm5.large', created, updated),
                    _source=dict(
                        session_hit('sess-1', 'STOPPED', 'm5.large', created, updated)[
                            '_source'
                        ],
                        owner=USER,
                    ),
                )
            ]
        }
    }
    jobs = {
        'hits': {'total': {'value': 0}, 'hits': []},
        'aggregations': {
            'by_user': {
                'buckets': [
                    {
                        'key': OTHER_USER,
                        'doc_count': 4,
                        'cost': {'value': 10.0},
                        'priced': {'value': 4},
                    }
                ]
            }
        },
    }

    service, _ = build_service(
        usage_dao=usage_dao,
        spend=120.0,
        os_responses={'_scheduler_jobs': jobs, 'user_sessions': sessions},
        prices={'m5.large': 0.1},
    )
    result = service.list_user_costs()
    rows = {row.username: row for row in result.listing}

    # both users appear: one from bedrock and desktops, one from bedrock and jobs
    assert set(rows) == {USER, OTHER_USER}
    assert rows[USER].ai_tokens == 150
    assert rows[USER].desktop_hours == pytest.approx(2.0, abs=0.01)
    assert rows[USER].desktop_cost == pytest.approx(0.2, abs=0.01)
    assert rows[USER].job_count == 0
    assert rows[OTHER_USER].ai_tokens == 1050
    assert rows[OTHER_USER].job_count == 4
    assert rows[OTHER_USER].job_cost == pytest.approx(10.0)
    # the ai split follows token share of the project spend
    assert rows[USER].ai_cost == pytest.approx(120.0 * 150 / 1200)
    assert rows[OTHER_USER].ai_cost == pytest.approx(120.0 * 1050 / 1200)
    # total is the sum of the three, and the listing leads with the biggest spender
    assert rows[OTHER_USER].total_cost == pytest.approx(
        rows[OTHER_USER].ai_cost + rows[OTHER_USER].job_cost
    )
    assert result.listing[0].username == OTHER_USER


def test_list_user_costs_reports_a_source_it_could_not_read():
    usage_dao = FakeUsageDAO()
    usage_dao.add(USER, MODEL_A, 1, 10, 10, today())

    service, _ = build_service(
        usage_dao=usage_dao,
        spend=10.0,
        os_responses={'_scheduler_jobs': RuntimeError('index_not_found_exception')},
        config_values={},
    )
    result = service.list_user_costs()

    # a read that failed is flagged, never silently rendered as nobody spending
    assert result.jobs_unavailable is True
    assert result.desktops_unavailable is True
    assert result.ai_unavailable is False
    assert result.listing[0].username == USER


# username guard


def test_get_summary_refuses_a_missing_username():
    service, _ = build_service()

    # get_username() is Optional, and an empty owner filter would read as every user.
    for missing in (None, ''):
        with pytest.raises(exceptions.SocaException):
            service.get_summary(missing)


def test_desktop_hits_refuses_a_missing_username_unless_all_users_is_asked_for():
    service, os_client = build_service(
        os_responses={'user_sessions': {'hits': {'hits': []}}}
    )

    with pytest.raises(exceptions.SocaException):
        service._desktop_hits(None, 0, 1)

    # the admin listing gets the same query by saying so explicitly
    service._desktop_hits(None, 0, 1, all_users=True)
    _, body = next(query for query in os_client.queries if 'user_sessions' in query[0])
    assert not any('term' in entry for entry in body['query']['bool']['filter'])


# one scoped read per project


def test_ai_reads_the_window_once_per_project():
    usage_dao = FakeUsageDAO()
    usage_dao.add(USER, MODEL_A, 2, 100, 50, today())
    usage_dao.add(OTHER_USER, MODEL_A, 8, 700, 350, today())

    service, _ = build_service(usage_dao=usage_dao, spend=120.0)
    service.get_summary(USER)

    # one project in play: the caller's totals and the project total come off the
    # same page, so the apportionment costs no extra read
    assert usage_dao.query_calls == 1


def test_ai_response_never_carries_another_username():
    usage_dao = FakeUsageDAO()
    usage_dao.add(USER, MODEL_A, 2, 100, 50, today())
    usage_dao.add(OTHER_USER, MODEL_A, 8, 700, 350, today())

    service, _ = build_service(usage_dao=usage_dao, spend=120.0)
    result = service.get_summary(USER)

    # the apportionment needs the project total, not a per user split. nobody else's
    # name may reach the caller.
    assert OTHER_USER not in result.model_dump_json()
    assert result.ai.projects[0].total_tokens == 150
    assert result.ai.projects[0].cost == pytest.approx(120.0 * 150 / 1200)


# stop time


def test_desktop_bills_to_the_recorded_stop_time_not_the_last_write():
    now = arrow.utcnow()
    created = now.shift(days=-26).int_timestamp * 1000
    stopped = now.shift(days=-25).int_timestamp * 1000
    # renamed on day 25: updated_on moved, the desktop did not start costing again
    touched = now.shift(days=-1).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit(
                            'sess-1',
                            'STOPPED',
                            'm5.large',
                            created,
                            touched,
                            stopped_on=stopped,
                        )
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    session = service.get_summary(USER).desktops.sessions[0]

    # one day of running, not the twenty four days of sitting stopped
    assert session.hours == pytest.approx(24.0, abs=0.1)
    assert session.stop_time_estimated is None
    assert session.cost == pytest.approx(2.4, abs=0.05)


def test_live_desktop_still_bills_to_now():
    now = arrow.utcnow()
    created = now.shift(hours=-3).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-2', 'READY', 'm5.large', created, created)
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    session = service.get_summary(USER).desktops.sessions[0]

    # a running desktop has no stop time and must not be cut short by one
    assert session.hours == pytest.approx(3.0, abs=0.01)
    assert session.stop_time_estimated is None


def test_legacy_desktop_falls_back_to_the_last_write_and_says_so():
    now = arrow.utcnow()
    created = now.shift(hours=-5).int_timestamp * 1000
    touched = now.shift(hours=-4).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-3', 'STOPPED', 'm5.large', created, touched)
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    session = service.get_summary(USER).desktops.sessions[0]

    # stopped before the stop time was recorded: still an upper bound, and flagged
    assert session.hours == pytest.approx(1.0, abs=0.01)
    assert session.stop_time_estimated is True


def test_legacy_desktop_prefers_the_cleanup_notice_stop_time_to_the_last_write():
    now = arrow.utcnow()
    created = now.shift(days=-10).int_timestamp * 1000
    real_stop = now.shift(days=-9).int_timestamp * 1000
    touched = now.shift(days=-1).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit(
                            'sess-4',
                            'STOPPED',
                            'm5.large',
                            created,
                            touched,
                            cleanup_warning_stop_time=real_stop,
                        )
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    session = service.get_summary(USER).desktops.sessions[0]

    # the cleanup notice carries a real ec2 stop time, which beats the last write
    assert session.hours == pytest.approx(24.0, abs=0.1)
    assert session.stop_time_estimated is True


# terminated desktops


def test_terminated_desktop_inside_the_window_is_billed_to_its_stop_time():
    now = arrow.utcnow()
    created = now.shift(days=-10).int_timestamp * 1000
    stopped = now.shift(days=-9).int_timestamp * 1000
    # the deletion wrote the record again, days after the desktop stopped costing
    deleted = now.shift(days=-2).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit(
                            'sess-1',
                            'DELETED',
                            'm5.large',
                            created,
                            deleted,
                            stopped_on=stopped,
                        )
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    desktops = service.get_summary(USER).desktops
    session = desktops.sessions[0]

    # a desktop that ran and was later deleted still cost what it cost while running
    assert desktops.session_count == 1
    assert session.hours == pytest.approx(24.0, abs=0.1)
    assert session.cost == pytest.approx(2.4, abs=0.05)
    assert session.stop_time_estimated is None
    # the controller's word is DELETED; what a cost reader wants is Terminated
    assert session.state == 'Terminated'


def test_a_desktop_terminated_from_running_bills_to_the_deletion():
    now = arrow.utcnow()
    created = now.shift(hours=-6).int_timestamp * 1000
    deleted = now.shift(hours=-4).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-2', 'DELETED', 'm5.large', created, deleted)
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    session = service.get_summary(USER).desktops.sessions[0]

    # no recorded stop time on an older row: the deletion write is the best answer
    # there is, and it is reported as an estimate rather than as a measurement
    assert session.hours == pytest.approx(2.0, abs=0.01)
    assert session.stop_time_estimated is True
    assert session.state == 'Terminated'


def test_a_desktop_deleted_before_the_window_is_excluded():
    now = arrow.utcnow()
    created = now.shift(days=-60).int_timestamp * 1000
    deleted = now.shift(days=-45).int_timestamp * 1000

    service, os_client = build_service(
        os_responses={'user_sessions': {'hits': {'hits': []}}},
        prices={'m5.large': 0.1},
    )
    service.get_summary(USER)

    # the query itself excludes it: nothing that stopped before the window starts is
    # asked for, so a long dead desktop never reaches the page
    _, body = next(query for query in os_client.queries if 'user_sessions' in query[0])
    ranges = [entry for entry in body['query']['bool']['filter'] if 'range' in entry]
    updated_lower_bound = next(
        entry['range']['updated_on']['gte']
        for entry in ranges
        if 'updated_on' in entry['range']
    )
    assert deleted < updated_lower_bound
    assert created < updated_lower_bound


def test_a_terminating_desktop_reads_as_terminated_too():
    now = arrow.utcnow()
    created = now.shift(hours=-3).int_timestamp * 1000
    deleting = now.shift(hours=-1).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-3', 'DELETING', 'm5.large', created, deleting)
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    session = service.get_summary(USER).desktops.sessions[0]

    assert session.state == 'Terminated'
    assert session.hours == pytest.approx(2.0, abs=0.01)


def test_terminated_desktops_count_toward_the_admin_listing():
    now = arrow.utcnow()
    created = now.shift(hours=-5).int_timestamp * 1000
    stopped = now.shift(hours=-3).int_timestamp * 1000
    hit = session_hit(
        'sess-4', 'DELETED', 'm5.large', created, created, stopped_on=stopped
    )
    hit['_source']['owner'] = USER

    service, _ = build_service(
        os_responses={
            'user_sessions': {'hits': {'hits': [hit]}},
            '_scheduler_jobs': {
                'hits': {'total': {'value': 0}, 'hits': []},
                'aggregations': {'by_user': {'buckets': []}},
            },
        },
        prices={'m5.large': 0.1},
    )
    rows = {row.username: row for row in service.list_user_costs().listing}

    # the admin view reads the same sessions, so a terminated desktop counts there too
    assert rows[USER].desktop_session_count == 1
    assert rows[USER].desktop_hours == pytest.approx(2.0, abs=0.01)


def test_a_session_indexed_under_two_generations_is_counted_once():
    """
    the controller writes and deletes under a versioned index name while this reads the
    alias, which spans every generation of it. a reindexed desktop has a document under
    more than one, and billing both would double its hours.
    """
    now = arrow.utcnow()
    created = now.shift(hours=-4).int_timestamp * 1000
    stopped = now.shift(hours=-2).int_timestamp * 1000

    stale = session_hit('sess-1', 'STOPPED', 'm5.large', created, created)
    current = session_hit(
        'sess-1', 'STOPPED', 'm5.large', created, stopped, stopped_on=stopped
    )

    service, _ = build_service(
        os_responses={'user_sessions': {'hits': {'hits': [stale, current]}}},
        prices={'m5.large': 0.1},
    )
    desktops = service.get_summary(USER).desktops

    assert desktops.session_count == 1
    # the newer document wins, so the recorded stop time is the one that counts
    assert desktops.sessions[0].hours == pytest.approx(2.0, abs=0.01)
    assert desktops.sessions[0].stop_time_estimated is None


# terminated desktops kept as history


def history_row(
    session_id, owner, created_on, stopped_on, deleted_on, instance_type='m5.large'
):
    return {
        'owner': owner,
        'idea_session_id': session_id,
        'name': session_id,
        'base_os': 'amazonlinux2023',
        'instance_type': instance_type,
        'project_id': 'project-1',
        'created_on': created_on,
        'stopped_on': stopped_on,
        'deleted_on': deleted_on,
    }


def test_a_terminated_desktop_is_costed_from_its_history_record():
    now = arrow.utcnow()
    created = now.shift(hours=-6).int_timestamp * 1000
    stopped = now.shift(hours=-4).int_timestamp * 1000
    deleted = now.shift(hours=-1).int_timestamp * 1000

    service, _ = build_service(
        os_responses={'user_sessions': {'hits': {'hits': []}}},
        prices={'m5.large': 0.1},
        history_rows=[history_row('sess-1', USER, created, stopped, deleted)],
    )
    desktops = service.get_summary(USER).desktops

    # the session row and its search document are both long gone
    assert desktops.session_count == 1
    session = desktops.sessions[0]
    assert session.state == 'Terminated'
    assert session.hours == pytest.approx(2.0, abs=0.01)
    assert session.cost == pytest.approx(0.2, abs=0.01)
    assert session.stop_time_estimated is None


def test_a_desktop_deleted_before_the_window_is_left_out_of_history():
    now = arrow.utcnow()
    created = now.shift(days=-90).int_timestamp * 1000
    stopped = now.shift(days=-89).int_timestamp * 1000
    deleted = now.shift(days=-88).int_timestamp * 1000

    service, _ = build_service(
        os_responses={'user_sessions': {'hits': {'hits': []}}},
        prices={'m5.large': 0.1},
        history_rows=[history_row('sess-old', USER, created, stopped, deleted)],
    )
    desktops = service.get_summary(USER).desktops

    # the retention window is far longer than the costing window, so old rows are read
    # and then dropped rather than billed
    assert desktops.session_count == 0


def test_history_wins_over_a_search_document_that_was_never_removed():
    now = arrow.utcnow()
    created = now.shift(hours=-8).int_timestamp * 1000
    stopped = now.shift(hours=-6).int_timestamp * 1000
    deleted = now.shift(hours=-5).int_timestamp * 1000

    # the leaked document says the desktop is still stopped and was written later
    leaked = session_hit('sess-1', 'STOPPED', 'm5.large', created, deleted)

    service, _ = build_service(
        os_responses={'user_sessions': {'hits': {'hits': [leaked]}}},
        prices={'m5.large': 0.1},
        history_rows=[history_row('sess-1', USER, created, stopped, deleted)],
    )
    desktops = service.get_summary(USER).desktops

    # counted once, and the record written at the deletion is the one believed
    assert desktops.session_count == 1
    assert desktops.sessions[0].state == 'Terminated'
    assert desktops.sessions[0].hours == pytest.approx(2.0, abs=0.01)


def test_terminated_desktops_reach_the_administrator_listing():
    now = arrow.utcnow()
    created = now.shift(hours=-5).int_timestamp * 1000
    stopped = now.shift(hours=-3).int_timestamp * 1000
    deleted = now.shift(hours=-2).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {'hits': {'hits': []}},
            '_scheduler_jobs': {
                'hits': {'total': {'value': 0}, 'hits': []},
                'aggregations': {'by_user': {'buckets': []}},
            },
        },
        prices={'m5.large': 0.1},
        history_rows=[history_row('sess-1', OTHER_USER, created, stopped, deleted)],
    )
    rows = {row.username: row for row in service.list_user_costs().listing}

    assert rows[OTHER_USER].desktop_session_count == 1
    assert rows[OTHER_USER].desktop_hours == pytest.approx(2.0, abs=0.01)


def test_a_history_read_that_fails_leaves_the_live_desktops_alone():
    now = arrow.utcnow()
    created = now.shift(hours=-3).int_timestamp * 1000

    service, _ = build_service(
        os_responses={
            'user_sessions': {
                'hits': {
                    'hits': [
                        session_hit('sess-live', 'READY', 'm5.large', created, created)
                    ]
                }
            }
        },
        prices={'m5.large': 0.1},
    )
    # the fake context has no history table wired for this case; the read fails and is
    # swallowed, because history is additive and must not take the page down
    desktops = service.get_summary(USER).desktops

    assert desktops.session_count == 1
    assert desktops.sessions[0].name == 'sess-live'
