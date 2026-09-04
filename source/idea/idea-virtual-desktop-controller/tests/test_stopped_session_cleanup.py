"""
The sweep that deletes desktops stopped longer than a cutoff, and session records whose
instance is gone, through the same path an admin delete takes.

Deleting someone's desktop is destructive, so these also cover what must be left alone: a
keep tag or admin exemption, a stop time that does not parse, an ec2 answer that could not
be read, a desktop running again by the time it is acted on, and a dry run.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ideasdk.utils import Utils
from ideadatamodel import (
    SocaListingPayload,
    SocaPaginator,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    errorcodes,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
    VirtualDesktopAdminAPI,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_api import VirtualDesktopAPI
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    CLEANUP_EXEMPT_TAG,
    CLEANUP_WARNING_NOTIFICATION_PREFIX,
    STATE_TRANSITION_TIME_FORMAT,
    STOPPED_SESSION_CLEANUP_CONFIG_PREFIX,
    VirtualDesktopSessionUtils,
    parse_stop_time,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)

INSTANCE_ID = 'i-00000000000000001'
PREFIX = STOPPED_SESSION_CLEANUP_CONFIG_PREFIX
WARNING_TEMPLATE = 'virtual-desktop-controller.session-cleanup-warning'
DAY = timedelta(days=1)


def now() -> datetime:
    return datetime.now(timezone.utc)


def a_stop_time(days_ago: float) -> datetime:
    # ec2 reports whole seconds, and the cleanup compares the recorded stop time to it
    return (now() - days_ago * DAY).replace(microsecond=0)


def client_error(error_code: str) -> ClientError:
    return ClientError(
        {'Error': {'Code': error_code, 'Message': error_code}}, 'DescribeInstances'
    )


def an_instance(
    stopped_hours_ago: Optional[float] = 200,
    state: str = 'stopped',
    tags: Optional[List[Dict]] = None,
    reason: Optional[str] = None,
    stopped_at: Optional[datetime] = None,
) -> Dict:
    if stopped_at is None and stopped_hours_ago is not None:
        stopped_at = now() - timedelta(hours=stopped_hours_ago)
    if reason is None and stopped_at is not None:
        reason = f'User initiated ({stopped_at.strftime(STATE_TRANSITION_TIME_FORMAT)})'
    return {
        'InstanceId': INSTANCE_ID,
        'State': {'Name': state},
        'StateTransitionReason': reason or '',
        'Tags': [] if tags is None else tags,
    }


class FakeEc2Client:
    """answers describe_instances from a queue of answers, the last one repeating"""

    def __init__(
        self,
        *answers: Any,
        tag_error: Optional[Exception] = None,
        on_describe: Optional[Any] = None,
    ):
        self.answers = list(answers) if answers else [an_instance()]
        self.tag_error = tag_error
        self.on_describe = on_describe
        self.describe_calls: List[Dict] = []
        self.created_tags: List[Dict] = []
        self.deleted_tags: List[Dict] = []

    def describe_instances(self, **kwargs) -> Dict[str, Any]:
        self.describe_calls.append(kwargs)
        if self.on_describe is not None:
            self.on_describe()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return {'Reservations': [{'Instances': [answer]}]}

    def create_tags(self, **kwargs):
        if self.tag_error is not None:
            raise self.tag_error
        self.created_tags.append(kwargs)

    def delete_tags(self, **kwargs):
        if self.tag_error is not None:
            raise self.tag_error
        self.deleted_tags.append(kwargs)


class FakeSessionDB:
    def __init__(
        self,
        pages: Optional[List[List[VirtualDesktopSession]]] = None,
        existing: Optional[VirtualDesktopSession] = None,
    ):
        self.pages = [] if pages is None else pages
        self.existing = existing
        self.requested_cursors: List[Optional[str]] = []
        self.updated: List[VirtualDesktopSession] = []

    def list_all_from_db(self, request) -> SocaListingPayload:
        self.requested_cursors.append(request.cursor)
        index, offset = self._at(request.cursor)
        next_cursor = str(index + 1) if index + 1 < len(self.pages) else None
        return SocaListingPayload(
            listing=self.pages[index][offset:],
            paginator=SocaPaginator(cursor=next_cursor),
        )

    @staticmethod
    def _at(cursor: Optional[str]):
        # 'page', or 'page:offset' for a cursor naming a position part way through a page
        if cursor is None:
            return 0, 0
        page, _, offset = cursor.partition(':')
        return int(page), int(offset or 0)

    def cursor_for(self, session) -> Optional[str]:
        """the real db names the position by primary key, here it is page and offset"""
        for page_index, page in enumerate(self.pages):
            for offset, candidate in enumerate(page):
                if candidate is session:
                    return f'{page_index}:{offset + 1}'
        return None

    def update(self, session: VirtualDesktopSession) -> VirtualDesktopSession:
        self.updated.append(session)
        return session

    def get_from_db(self, idea_session_owner: str, idea_session_id: str):
        return self.existing


class FakeClock:
    """
    a clock that only moves when the sweep reaches ec2, so a pass can be cut off after an
    exact number of desktops rather than at a page boundary
    """

    def __init__(self, step_ms: int):
        self.now = 0
        self.step = step_ms

    def __call__(self) -> int:
        return self.now

    def tick(self):
        self.now += self.step


class FakeServerUtils:
    def start_dcv_hosts(self, servers) -> Dict:
        return {}


class FakeNotificationClient:
    def __init__(self):
        self.sent: List[Any] = []

    def send_notification(self, notification):
        self.sent.append(notification)


class FakeDeletePath:
    """stands in for terminate_sessions, the path an admin delete takes"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.deleted: List[VirtualDesktopSession] = []

    def __call__(self, sessions):
        self.deleted.extend(sessions)
        if self.fail:
            for session in sessions:
                session.failure_reason = 'the broker said no'
            return [], list(sessions)
        return list(sessions), []


class FakeClusterConfig:
    def __init__(self, values: Optional[Dict[str, Any]] = None):
        self.values = {
            f'{CLEANUP_WARNING_NOTIFICATION_PREFIX}.email_template': WARNING_TEMPLATE
        }
        self.values.update({} if values is None else values)

    def get_bool(self, key: str, default: bool = None, required: bool = False):
        return self.values.get(key, default)

    def get_string(self, key: str, default: str = None, required: bool = False):
        return self.values.get(key, default)

    def get_int(self, key: str, default: int = None, required: bool = False):
        return self.values.get(key, default)

    def get_list(self, key: str, default: list = None, required: bool = False):
        return self.values.get(key, default)


class FakeContext:
    def __init__(self, config: FakeClusterConfig):
        self._config = config
        self.notification_async_client = FakeNotificationClient()

    def config(self) -> FakeClusterConfig:
        return self._config

    @staticmethod
    def cluster_name() -> str:
        return 'idea-test'


def a_config(**values) -> FakeClusterConfig:
    """
    the cleanup on, not in dry run, a 5 day cutoff and no owner notice unless given; the
    shipped defaults (30 days, 7 day notice) have their own test.
    """
    settings = {
        'enabled': True,
        'dry_run': False,
        'stopped_after_days': 5,
        'warn_days_before': 0,
    }
    settings.update(values)
    return FakeClusterConfig(
        {f'{PREFIX}.{key}': value for key, value in settings.items()}
    )


def build_utils(
    session_db: FakeSessionDB,
    ec2_client: Optional[FakeEc2Client] = None,
    config: Optional[FakeClusterConfig] = None,
    delete_path: Optional[FakeDeletePath] = None,
) -> VirtualDesktopSessionUtils:
    # the sweep reaches ec2, the session db and the delete path only; the constructors
    # and their aws clients are skipped
    controller_utils = object.__new__(VirtualDesktopControllerUtils)
    controller_utils.ec2_client = FakeEc2Client() if ec2_client is None else ec2_client
    controller_utils._logger = Mock()

    utils = object.__new__(VirtualDesktopSessionUtils)
    utils.context = FakeContext(a_config() if config is None else config)
    utils._logger = Mock()
    utils._session_db = session_db
    utils._controller_utils = controller_utils
    utils._server_utils = FakeServerUtils()
    utils.terminate_sessions = FakeDeletePath() if delete_path is None else delete_path
    return utils


def notices_sent(utils: VirtualDesktopSessionUtils) -> List[Any]:
    return utils.context.notification_async_client.sent


def a_session(
    state: VirtualDesktopSessionState = VirtualDesktopSessionState.STOPPED,
    idea_session_id: str = 'sess-1',
    instance_id: Optional[str] = INSTANCE_ID,
    cleanup_exempt: Optional[bool] = None,
) -> VirtualDesktopSession:
    return VirtualDesktopSession(
        idea_session_id=idea_session_id,
        name='my-desktop',
        owner='test-user',
        state=state,
        cleanup_exempt=cleanup_exempt,
        server=VirtualDesktopServer(instance_id=instance_id, instance_type='t3.large'),
    )


def sweep(utils: VirtualDesktopSessionUtils, **kwargs) -> Dict[str, int]:
    return utils.clean_up_stopped_sessions(**kwargs)


# the stop time


def test_the_stop_time_is_read_off_the_state_transition_reason():
    stopped_at = parse_stop_time('User initiated (2026-07-30 14:02:11 GMT)')
    assert stopped_at == datetime(2026, 7, 30, 14, 2, 11, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    'reason', [None, '', 'Client.UserInitiatedShutdown', 'User initiated (yesterday)']
)
def test_a_reason_without_a_time_in_it_is_not_guessed_at(reason):
    assert parse_stop_time(reason) is None


def test_a_desktop_whose_stop_time_cannot_be_read_is_skipped_and_counted():
    ec2_client = FakeEc2Client(an_instance(reason='Client.UserInitiatedShutdown'))
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]), ec2_client, delete_path=delete_path
    )

    counters = sweep(utils)
    assert counters['no_stop_time'] == 1
    assert counters['deleted'] == 0
    assert delete_path.deleted == []
    assert utils._logger.warning.called


# the cutoff


def test_a_desktop_stopped_for_less_than_the_cutoff_stays():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(stopped_hours_ago=119.5)),
        config=a_config(stopped_after_days=5),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['candidates'] == 1
    assert counters['not_due'] == 1
    assert delete_path.deleted == []


def test_a_desktop_stopped_past_the_cutoff_is_deleted():
    session = a_session()
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[session]]),
        FakeEc2Client(an_instance(stopped_hours_ago=120.5)),
        config=a_config(stopped_after_days=5),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['deleted'] == 1
    assert delete_path.deleted == [session]


def test_the_shipped_defaults_are_thirty_days_with_a_seven_day_notice():
    """a cluster that only turned the cleanup on warns at day 23 and deletes nothing yet"""
    delete_path = FakeDeletePath()
    only_enabled = FakeClusterConfig(
        {f'{PREFIX}.enabled': True, f'{PREFIX}.dry_run': False}
    )
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(stopped_hours_ago=24 * 24)),
        config=only_enabled,
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['warned'] == 1
    assert counters['deleted'] == 0
    assert delete_path.deleted == []


def test_the_cutoff_is_read_from_the_settings():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(stopped_hours_ago=30)),
        config=a_config(stopped_after_days=1),
        delete_path=delete_path,
    )

    assert sweep(utils)['deleted'] == 1
    assert len(delete_path.deleted) == 1


# what is left alone


@pytest.mark.parametrize('tag_key', ['idea:keep', 'ideal:keep'])
def test_a_keep_tag_exempts_the_desktop(tag_key):
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(tags=[{'Key': tag_key, 'Value': 'anything'}])),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['kept'] == 1
    assert delete_path.deleted == []


def test_the_keep_tags_are_read_from_the_settings():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(tags=[{'Key': 'idea:keep', 'Value': 'x'}])),
        config=a_config(keep_tags=['team:keep']),
        delete_path=delete_path,
    )

    assert sweep(utils)['deleted'] == 1


def test_an_admin_exemption_on_the_session_is_honored_without_any_tag():
    ec2_client = FakeEc2Client(an_instance())
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session(cleanup_exempt=True)]]),
        ec2_client,
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['kept'] == 1
    assert delete_path.deleted == []
    assert ec2_client.describe_calls == []


def test_a_desktop_running_again_by_the_time_it_is_acted_on_is_left_alone():
    # the first read found it stopped past the cutoff; the read right before acting did not
    ec2_client = FakeEc2Client(an_instance(), an_instance(state='running'))
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]), ec2_client, delete_path=delete_path
    )

    counters = sweep(utils)
    assert counters['raced'] == 1
    assert delete_path.deleted == []
    assert len(ec2_client.describe_calls) == 2


def test_an_ec2_answer_that_could_not_be_read_does_not_delete_anything():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(client_error('RequestLimitExceeded')),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['failed'] == 1
    assert delete_path.deleted == []


def test_a_desktop_that_is_not_stopped_is_of_no_interest():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(
            pages=[
                [
                    a_session(state=VirtualDesktopSessionState.READY),
                    a_session(state=VirtualDesktopSessionState.ERROR),
                    a_session(instance_id=None),
                ]
            ]
        ),
        FakeEc2Client(an_instance()),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['candidates'] == 0
    assert delete_path.deleted == []


# dry run


def test_a_dry_run_reports_what_it_would_delete_without_deleting():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance()),
        config=a_config(dry_run=True),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['deleted'] == 1
    assert delete_path.deleted == []
    logged = ' '.join(str(call) for call in utils._logger.info.call_args_list)
    assert '[dry run] would delete session sess-1' in logged
    assert 'test-user' in logged
    assert 'stopped for 8 days' in logged


def test_the_cleanup_is_in_dry_run_unless_told_otherwise():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance()),
        # dry_run deliberately unset: the default must be the safe one
        config=FakeClusterConfig(
            {
                f'{PREFIX}.enabled': True,
                f'{PREFIX}.stopped_after_days': 5,
                f'{PREFIX}.warn_days_before': 0,
            }
        ),
        delete_path=delete_path,
    )

    assert sweep(utils)['deleted'] == 1
    assert delete_path.deleted == []


# the live run


def test_a_live_run_takes_the_delete_path_once_per_desktop():
    one, two = a_session(idea_session_id='one'), a_session(idea_session_id='two')
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[one], [two]]),
        FakeEc2Client(an_instance()),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['deleted'] == 2
    assert delete_path.deleted == [one, two]


def test_a_delete_the_path_refused_is_counted_as_failed():
    delete_path = FakeDeletePath(fail=True)
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance()),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['failed'] == 1
    assert counters['deleted'] == 0
    assert utils._logger.error.called


def test_deletions_per_pass_are_capped_and_the_rest_wait_for_the_next_pass():
    sessions = [a_session(idea_session_id=f'sess-{n}') for n in range(3)]
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[sessions]),
        FakeEc2Client(an_instance()),
        config=a_config(max_per_pass=2),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['deleted'] == 2
    assert delete_path.deleted == sessions[:2]


def test_a_dry_run_is_not_capped_since_it_deletes_nothing():
    sessions = [a_session(idea_session_id=f'sess-{n}') for n in range(3)]
    utils = build_utils(
        FakeSessionDB(pages=[sessions]),
        FakeEc2Client(an_instance()),
        config=a_config(max_per_pass=2, dry_run=True),
    )

    assert sweep(utils)['deleted'] == 3


# orphans


@pytest.mark.parametrize(
    'answer',
    [an_instance(state='terminated'), client_error('InvalidInstanceID.NotFound')],
)
@pytest.mark.parametrize(
    'state', [VirtualDesktopSessionState.STOPPED, VirtualDesktopSessionState.ERROR]
)
def test_a_record_whose_instance_is_gone_is_cleaned_up(answer, state):
    session = a_session(state=state)
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[session]]), FakeEc2Client(answer), delete_path=delete_path
    )

    counters = sweep(utils)
    assert counters['orphans_cleaned'] == 1
    assert delete_path.deleted == [session]


def test_a_describe_error_other_than_not_found_skips_the_record():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session(state=VirtualDesktopSessionState.ERROR)]]),
        FakeEc2Client(client_error('UnauthorizedOperation')),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['failed'] == 1
    assert delete_path.deleted == []


def test_an_orphan_cleanup_is_reported_not_done_in_dry_run():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(state='terminated')),
        config=a_config(dry_run=True),
        delete_path=delete_path,
    )

    assert sweep(utils)['orphans_cleaned'] == 1
    assert delete_path.deleted == []


# the pass across every desktop


def test_the_sweep_is_off_unless_enabled():
    session_db = FakeSessionDB(pages=[[a_session()]])
    delete_path = FakeDeletePath()
    utils = build_utils(session_db, config=FakeClusterConfig(), delete_path=delete_path)

    assert sweep(utils) == {}
    assert session_db.requested_cursors == []
    assert delete_path.deleted == []


def test_the_pass_gives_up_its_remaining_work_rather_than_run_past_its_budget():
    # the scheduled event that runs this also triggers desktop schedules, and going past
    # the queue visibility timeout would replay the whole message.
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance()),
        delete_path=delete_path,
    )

    assert sweep(utils, time_budget_ms=0)['deleted'] == 0
    assert delete_path.deleted == []


def test_a_pass_that_runs_out_of_time_keeps_its_place():
    # a table larger than one time budget is never walked past its first pages otherwise
    utils = build_utils(FakeSessionDB(pages=[[a_session()], [a_session()]]))
    VirtualDesktopSessionUtils._stopped_session_cleanup_cursor = '1'

    sweep(utils, time_budget_ms=0)
    assert utils._stopped_session_cleanup_cursor == '1'


def test_a_pass_resumes_from_the_page_the_last_one_stopped_on():
    session_db = FakeSessionDB(
        pages=[[a_session(idea_session_id='one')], [a_session(idea_session_id='two')]]
    )
    delete_path = FakeDeletePath()
    utils = build_utils(
        session_db, FakeEc2Client(an_instance()), delete_path=delete_path
    )
    VirtualDesktopSessionUtils._stopped_session_cleanup_cursor = '1'

    assert sweep(utils)['deleted'] == 1
    assert session_db.requested_cursors == ['1']
    assert [s.idea_session_id for s in delete_path.deleted] == ['two']
    # the end of the table was reached, so the next pass starts at the first page again
    assert utils._stopped_session_cleanup_cursor is None


def test_a_pass_cut_off_inside_a_page_resumes_after_the_last_desktop_it_looked_at(
    monkeypatch,
):
    """
    a page is a whole dynamodb scan page, far more desktops than one budget can reach ec2
    for. keeping the page's own start key would restart that page every pass, leaving a
    table larger than one budget never walked past its first page.
    """
    page = [a_session(idea_session_id=name) for name in ('one', 'two', 'three')]
    session_db = FakeSessionDB(pages=[page])
    clock = FakeClock(step_ms=10)
    utils = build_utils(
        session_db,
        FakeEc2Client(an_instance(stopped_hours_ago=1), on_describe=clock.tick),
    )
    monkeypatch.setattr(Utils, 'current_time_ms', clock)

    # the budget runs out on the third desktop, part way through the only page
    assert sweep(utils, time_budget_ms=15)['not_due'] == 2
    assert utils._stopped_session_cleanup_cursor == session_db.cursor_for(page[1])

    clock.now = 0
    session_db.requested_cursors.clear()

    assert sweep(utils, time_budget_ms=15)['not_due'] == 1
    assert session_db.requested_cursors == [session_db.cursor_for(page[1])]
    # the last page was finished, so the next pass starts at the first page again
    assert utils._stopped_session_cleanup_cursor is None


def test_one_desktop_that_cannot_be_checked_does_not_stop_the_rest():
    class HalfBrokenEc2Client(FakeEc2Client):
        def describe_instances(self, **kwargs):
            if len(self.describe_calls) == 0:
                self.describe_calls.append(kwargs)
                raise RuntimeError('ec2 is having a moment')
            return super().describe_instances(**kwargs)

    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(
            pages=[[a_session(idea_session_id='one'), a_session(idea_session_id='two')]]
        ),
        HalfBrokenEc2Client(an_instance()),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['failed'] == 1
    assert counters['deleted'] == 1
    assert [s.idea_session_id for s in delete_path.deleted] == ['two']


def test_the_counters_are_logged_once_per_pass():
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]), FakeEc2Client(an_instance())
    )

    sweep(utils)
    summaries = [
        str(call)
        for call in utils._logger.info.call_args_list
        if 'stopped desktop cleanup pass' in str(call)
    ]
    assert len(summaries) == 1
    assert 'deleted=1' in summaries[0]


# warning the owner


def warned_session(
    stopped_at: datetime, warned_days_ago: float
) -> VirtualDesktopSession:
    session = a_session()
    session.cleanup_warning_sent_on = now() - warned_days_ago * DAY
    session.cleanup_warning_stop_time = stopped_at
    return session


def test_the_owner_is_warned_once_the_warning_window_opens_and_it_is_recorded():
    stopped_at = a_stop_time(23.5)
    session = a_session()
    session_db = FakeSessionDB(pages=[[session]])
    delete_path = FakeDeletePath()
    utils = build_utils(
        session_db,
        FakeEc2Client(an_instance(stopped_at=stopped_at)),
        config=a_config(stopped_after_days=30, warn_days_before=7),
        delete_path=delete_path,
    )

    counters = sweep(utils)
    assert counters['warned'] == 1
    assert delete_path.deleted == []
    assert session.cleanup_warning_stop_time == stopped_at
    assert now() - session.cleanup_warning_sent_on < timedelta(minutes=1)
    assert session_db.updated == [session]

    (notice,) = notices_sent(utils)
    assert notice.username == 'test-user'
    assert notice.template_name == WARNING_TEMPLATE
    assert notice.params['session_name'] == 'my-desktop'
    assert notice.params['stopped_days'] == 23
    # warned half a day into the window, so the full window runs past the plain cutoff
    assert notice.params['deletion_date'].startswith(
        (now() + 7 * DAY).strftime('%Y-%m-%d')
    )


def test_a_desktop_short_of_the_warning_window_is_left_alone():
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(stopped_at=a_stop_time(22))),
        config=a_config(stopped_after_days=30, warn_days_before=7),
    )

    assert sweep(utils)['not_due'] == 1
    assert notices_sent(utils) == []


def test_the_warning_is_not_sent_again_on_the_next_pass():
    stopped_at = a_stop_time(25)
    session_db = FakeSessionDB(pages=[[warned_session(stopped_at, warned_days_ago=1)]])
    utils = build_utils(
        session_db,
        FakeEc2Client(an_instance(stopped_at=stopped_at)),
        config=a_config(stopped_after_days=30, warn_days_before=7),
    )

    assert sweep(utils)['not_due'] == 1
    assert notices_sent(utils) == []
    assert session_db.updated == []


def test_deletion_waits_for_the_warning_window_after_the_notice():
    stopped_at = a_stop_time(40)
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[warned_session(stopped_at, warned_days_ago=3)]]),
        FakeEc2Client(an_instance(stopped_at=stopped_at)),
        config=a_config(stopped_after_days=30, warn_days_before=7),
        delete_path=delete_path,
    )

    assert sweep(utils)['not_due'] == 1
    assert delete_path.deleted == []


def test_deletion_proceeds_once_the_warning_window_has_elapsed():
    stopped_at = a_stop_time(40)
    session = warned_session(stopped_at, warned_days_ago=7.5)
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[session]]),
        FakeEc2Client(an_instance(stopped_at=stopped_at)),
        config=a_config(stopped_after_days=30, warn_days_before=7),
        delete_path=delete_path,
    )

    assert sweep(utils)['deleted'] == 1
    assert delete_path.deleted == [session]
    assert notices_sent(utils) == []


def test_a_desktop_already_past_the_cutoff_is_warned_first_and_given_the_whole_window():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(stopped_at=a_stop_time(40))),
        config=a_config(stopped_after_days=30, warn_days_before=7),
        delete_path=delete_path,
    )

    assert sweep(utils)['warned'] == 1
    assert delete_path.deleted == []
    (notice,) = notices_sent(utils)
    assert notice.params['deletion_date'].startswith(
        (now() + 7 * DAY).strftime('%Y-%m-%d')
    )


def test_a_new_stop_episode_is_warned_again():
    # the recorded warning was for an earlier stop; the desktop ran in between
    stopped_at = a_stop_time(25)
    session = warned_session(stopped_at - 10 * DAY, warned_days_ago=30)
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[session]]),
        FakeEc2Client(an_instance(stopped_at=stopped_at)),
        config=a_config(stopped_after_days=30, warn_days_before=7),
        delete_path=delete_path,
    )

    assert sweep(utils)['warned'] == 1
    assert len(notices_sent(utils)) == 1
    assert session.cleanup_warning_stop_time == stopped_at
    assert delete_path.deleted == []


def test_no_warning_window_keeps_the_plain_cutoff():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(stopped_at=a_stop_time(40))),
        config=a_config(stopped_after_days=30, warn_days_before=0),
        delete_path=delete_path,
    )

    assert sweep(utils)['deleted'] == 1
    assert notices_sent(utils) == []


def test_an_orphan_is_cleaned_up_without_a_warning():
    delete_path = FakeDeletePath()
    utils = build_utils(
        FakeSessionDB(pages=[[a_session()]]),
        FakeEc2Client(an_instance(state='terminated')),
        config=a_config(stopped_after_days=30, warn_days_before=7),
        delete_path=delete_path,
    )

    assert sweep(utils)['orphans_cleaned'] == 1
    assert notices_sent(utils) == []


def test_a_dry_run_reports_the_warning_and_records_nothing():
    session = a_session()
    session_db = FakeSessionDB(pages=[[session]])
    utils = build_utils(
        session_db,
        FakeEc2Client(an_instance(stopped_at=a_stop_time(25))),
        config=a_config(stopped_after_days=30, warn_days_before=7, dry_run=True),
    )

    assert sweep(utils)['warned'] == 1
    assert notices_sent(utils) == []
    assert session_db.updated == []
    assert session.cleanup_warning_sent_on is None
    logged = ' '.join(str(call) for call in utils._logger.info.call_args_list)
    assert '[dry run] would warn session sess-1' in logged


def test_a_disabled_notice_is_not_sent_but_the_window_still_applies():
    session_db = FakeSessionDB(pages=[[a_session()]])
    config = a_config(stopped_after_days=30, warn_days_before=7)
    config.values[f'{CLEANUP_WARNING_NOTIFICATION_PREFIX}.enabled'] = False
    utils = build_utils(
        session_db,
        FakeEc2Client(an_instance(stopped_at=a_stop_time(25))),
        config=config,
    )

    assert sweep(utils)['warned'] == 1
    assert notices_sent(utils) == []
    assert len(session_db.updated) == 1


def test_resuming_a_desktop_clears_its_deletion_notice():
    session = warned_session(a_stop_time(25), warned_days_ago=1)
    session_db = FakeSessionDB(existing=session)
    utils = build_utils(session_db)

    resumed, failed = utils.resume_sessions([session])
    assert failed == []
    assert resumed[0].cleanup_warning_sent_on is None
    assert resumed[0].cleanup_warning_stop_time is None
    assert session_db.updated == [session]


# the admin exemption


def test_setting_the_exemption_records_the_flag_and_tags_the_instance():
    session_db = FakeSessionDB()
    ec2_client = FakeEc2Client()
    utils = build_utils(session_db, ec2_client)
    session = a_session()

    session = utils.set_cleanup_exemption(
        session, exempt=True, reason='kept for an audit', actor='admin1'
    )
    assert session.cleanup_exempt is True
    assert session.cleanup_exempt_reason == 'kept for an audit'
    assert session_db.updated == [session]
    assert ec2_client.created_tags == [
        {
            'Resources': [INSTANCE_ID],
            'Tags': [{'Key': CLEANUP_EXEMPT_TAG, 'Value': 'kept for an audit'}],
        }
    ]


def test_an_empty_reason_falls_back_to_the_admin_username():
    ec2_client = FakeEc2Client()
    utils = build_utils(FakeSessionDB(), ec2_client)

    session = utils.set_cleanup_exemption(
        a_session(), True, reason='  ', actor='admin1'
    )
    assert session.cleanup_exempt_reason == 'admin1'
    assert ec2_client.created_tags[0]['Tags'][0]['Value'] == 'admin1'


def test_clearing_the_exemption_removes_the_flag_and_only_its_own_tag():
    session_db = FakeSessionDB()
    ec2_client = FakeEc2Client()
    utils = build_utils(session_db, ec2_client)
    session = a_session(cleanup_exempt=True)
    session.cleanup_exempt_reason = 'old reason'

    session = utils.set_cleanup_exemption(session, False, reason=None, actor='admin1')
    assert session.cleanup_exempt is None
    assert session.cleanup_exempt_reason is None
    assert session_db.updated == [session]
    assert ec2_client.created_tags == []
    assert ec2_client.deleted_tags == [
        {'Resources': [INSTANCE_ID], 'Tags': [{'Key': CLEANUP_EXEMPT_TAG}]}
    ]


def test_a_tag_that_cannot_be_written_does_not_fail_the_exemption():
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db, FakeEc2Client(tag_error=client_error('UnauthorizedOperation'))
    )

    session = utils.set_cleanup_exemption(a_session(), True, 'why', 'admin1')
    assert session.cleanup_exempt is True
    assert session_db.updated == [session]
    assert utils._logger.warning.called


def test_a_non_admin_cannot_reach_the_exemption_namespace():
    class NonAdminContext:
        namespace = 'VirtualDesktopAdmin.SetSessionCleanupExemption'

        @staticmethod
        def is_authorized(elevated_access: bool, scopes=None) -> bool:
            return not elevated_access

    api = object.__new__(VirtualDesktopAdminAPI)
    with pytest.raises(Exception) as raised:
        api.invoke(NonAdminContext())
    assert raised.value.error_code == errorcodes.UNAUTHORIZED_ACCESS


def test_a_create_request_cannot_carry_the_exemption_in():
    api = object.__new__(VirtualDesktopAPI)
    session = VirtualDesktopSession(cleanup_exempt=True, cleanup_exempt_reason='mine')

    # a request with no project fails before anything else is looked at
    session, is_valid = api.validate_create_session_request(session)
    assert is_valid is False
    assert session.cleanup_exempt is None
    assert session.cleanup_exempt_reason is None


def test_an_update_request_cannot_carry_the_exemption_in():
    old_session = a_session()
    session_db = FakeSessionDB(existing=old_session)
    api = object.__new__(VirtualDesktopAPI)
    api.session_db = session_db
    api._logger = Mock()

    api._update_session(
        VirtualDesktopSession(
            idea_session_id='sess-1', owner='test-user', cleanup_exempt=True
        )
    )
    assert old_session.cleanup_exempt is None
    assert session_db.updated == []
