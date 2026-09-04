"""
The sweep that takes a desktop whose host never became usable out of PROVISIONING: it
records why, and releases the instance that is still being billed.

Releasing someone's desktop is destructive, so these also cover what must be left running:
a launch that is merely slow, an EC2 answer that could not be read, and a session record
that could not be read.
"""

from unittest.mock import Mock
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from ideadatamodel import (
    Project,
    SocaListingPayload,
    SocaPaginator,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    VirtualDesktopSoftwareStack,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_db import (
    VirtualDesktopSessionDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    PROVISIONING_TIMEOUT_CONFIG_KEY,
    PROVISIONING_TIMEOUT_SECONDS_DEFAULT,
    VirtualDesktopSessionUtils,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    BOOTSTRAP_STATUS_TAG,
    VirtualDesktopControllerUtils,
)

INSTANCE_ID = 'i-00000000000000001'
PROJECT_ID = 'a1b2c3d4-0000-4000-8000-000000000001'

# the value fail_gpu_drivers writes when the driver install produced nothing usable
GPU_DRIVER_INSTALL_FAILED = 'gpu-driver-install-failed'


def client_error(error_code: str, operation: str) -> ClientError:
    return ClientError(
        {'Error': {'Code': error_code, 'Message': error_code}}, operation
    )


class FakeEc2Client:
    """answers describe_instances with the tags the bootstrap would have written"""

    def __init__(
        self,
        tags: Optional[List[Dict]] = None,
        describe_error: Optional[Exception] = None,
        reservations: Optional[List[Dict]] = None,
    ):
        self.tags = [] if tags is None else tags
        self.describe_error = describe_error
        self.reservations = reservations
        self.describe_calls: List[Dict] = []

    def describe_instances(self, **kwargs) -> Dict[str, Any]:
        self.describe_calls.append(kwargs)
        if self.describe_error is not None:
            raise self.describe_error
        if self.reservations is not None:
            return {'Reservations': self.reservations}
        return {
            'Reservations': [
                {
                    'Instances': [
                        {
                            'InstanceId': INSTANCE_ID,
                            'State': {'Name': 'running'},
                            'Tags': self.tags,
                        }
                    ]
                }
            ]
        }


class FakeSessionDB:
    def __init__(self, pages: Optional[List[List[VirtualDesktopSession]]] = None):
        self.pages = [] if pages is None else pages
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


class FakeServerUtils:
    def __init__(self, response: Optional[Dict] = None):
        self.response = {} if response is None else response
        self.terminated: List[List[VirtualDesktopServer]] = []

    def terminate_dcv_hosts(self, servers, force: bool = False) -> Dict:
        self.terminated.append(servers)
        return self.response


class FakeClusterConfig:
    def __init__(self, timeout_seconds: Optional[int] = None):
        self.timeout_seconds = timeout_seconds
        self.requested_keys: List[str] = []

    def get_int(self, key: str, default: int = None) -> int:
        self.requested_keys.append(key)
        if key == PROVISIONING_TIMEOUT_CONFIG_KEY and self.timeout_seconds is not None:
            return self.timeout_seconds
        return default


class FakeContext:
    def __init__(self, config: FakeClusterConfig):
        self._config = config

    def config(self) -> FakeClusterConfig:
        return self._config


def build_utils(
    session_db: FakeSessionDB,
    ec2_client: FakeEc2Client,
    server_utils: FakeServerUtils,
    config: Optional[FakeClusterConfig] = None,
) -> VirtualDesktopSessionUtils:
    # the sweep reaches ec2, the session db and the server utils only; the constructors
    # and their aws clients are skipped
    controller_utils = object.__new__(VirtualDesktopControllerUtils)
    controller_utils.ec2_client = ec2_client
    controller_utils._logger = Mock()

    utils = object.__new__(VirtualDesktopSessionUtils)
    utils.context = FakeContext(FakeClusterConfig() if config is None else config)
    utils._logger = Mock()
    utils._session_db = session_db
    utils._controller_utils = controller_utils
    utils._server_utils = server_utils
    return utils


def a_session(
    provisioning_for_minutes: Optional[int] = 45,
    state: VirtualDesktopSessionState = VirtualDesktopSessionState.PROVISIONING,
    instance_id: Optional[str] = INSTANCE_ID,
    idea_session_id: str = 'sess-1',
) -> VirtualDesktopSession:
    created_on = None
    if provisioning_for_minutes is not None:
        created_on = Utils.to_datetime(
            Utils.current_time_ms() - (provisioning_for_minutes * 60 * 1000)
        )
    return VirtualDesktopSession(
        idea_session_id=idea_session_id,
        name='my-desktop',
        owner='test-user',
        state=state,
        created_on=created_on,
        project=Project(project_id=PROJECT_ID, name='test-project'),
        server=VirtualDesktopServer(
            instance_id=instance_id,
            instance_type='g5.xlarge',
        ),
    )


def a_bootstrap_status_tag(value: str) -> Dict:
    return {'Key': BOOTSTRAP_STATUS_TAG, 'Value': value}


# the reason the user is given


def test_a_desktop_past_the_timeout_reports_what_its_bootstrap_recorded():
    # a generic timeout would send the user looking in the wrong place.
    ec2_client = FakeEc2Client(
        tags=[
            {'Key': 'Name', 'Value': 'idea-test-my-desktop'},
            a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED),
        ]
    )
    session_db = FakeSessionDB()
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)
    session = a_session()

    assert utils.fail_stuck_provisioning_session(session, 1800) is True
    assert session.state == VirtualDesktopSessionState.ERROR
    assert 'GPU driver' in session.failure_reason
    assert 'minutes' not in session.failure_reason
    assert session_db.updated == [session]
    assert server_utils.terminated == [[session.server]]


def test_a_desktop_past_the_timeout_with_no_bootstrap_status_reports_the_wait():
    # nothing was recorded on the host, so how long it was waited on is all there is.
    ec2_client = FakeEc2Client(tags=[{'Key': 'Name', 'Value': 'idea-test-my-desktop'}])
    session_db = FakeSessionDB()
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)
    session = a_session()

    assert utils.fail_stuck_provisioning_session(session, 1800) is True
    assert session.state == VirtualDesktopSessionState.ERROR
    assert '30 minutes' in session.failure_reason
    assert server_utils.terminated == [[session.server]]


def test_an_unrecognised_bootstrap_status_is_reported_as_itself():
    ec2_client = FakeEc2Client(tags=[a_bootstrap_status_tag('something-new-failed')])
    utils = build_utils(FakeSessionDB(), ec2_client, FakeServerUtils())
    session = a_session()

    assert utils.fail_stuck_provisioning_session(session, 1800) is True
    assert 'something-new-failed' in session.failure_reason


def test_the_host_is_released_after_the_reason_is_recorded():
    # a release that fails must not leave the desktop stuck in PROVISIONING.
    ec2_client = FakeEc2Client(tags=[a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED)])
    session_db = FakeSessionDB()
    server_utils = FakeServerUtils(response={'ERROR': 'RequestLimitExceeded'})
    utils = build_utils(session_db, ec2_client, server_utils)
    session = a_session()

    assert utils.fail_stuck_provisioning_session(session, 1800) is True
    assert session.state == VirtualDesktopSessionState.ERROR
    assert session_db.updated == [session]


# what must be left running


def test_a_slow_but_healthy_launch_is_left_alone():
    # a gpu driver build and an ec2 mac launch both run long: releasing one of those is
    # worse than the wait.
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB()
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)
    session = a_session(provisioning_for_minutes=25)

    assert utils.fail_stuck_provisioning_session(session, 1800) is False
    assert session.state == VirtualDesktopSessionState.PROVISIONING
    assert session.failure_reason is None
    assert ec2_client.describe_calls == []
    assert session_db.updated == []
    assert server_utils.terminated == []


def test_an_unreadable_ec2_answer_does_not_release_the_host():
    # an unknown must not be acted on as though the host had failed.
    ec2_client = FakeEc2Client(
        describe_error=client_error('RequestLimitExceeded', 'DescribeInstances')
    )
    session_db = FakeSessionDB()
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)
    session = a_session()

    assert utils.fail_stuck_provisioning_session(session, 1800) is False
    assert session.state == VirtualDesktopSessionState.PROVISIONING
    assert session.failure_reason is None
    assert session_db.updated == []
    assert server_utils.terminated == []


def test_an_ec2_answer_that_names_no_instance_does_not_release_the_host():
    ec2_client = FakeEc2Client(reservations=[])
    session_db = FakeSessionDB()
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)

    assert utils.fail_stuck_provisioning_session(a_session(), 1800) is False
    assert session_db.updated == []
    assert server_utils.terminated == []


def test_a_host_ec2_no_longer_knows_about_is_failed_rather_than_left_stuck():
    # the instance is already gone, so there is nothing to protect and nothing to bill.
    ec2_client = FakeEc2Client(
        describe_error=client_error('InvalidInstanceID.NotFound', 'DescribeInstances')
    )
    session_db = FakeSessionDB()
    utils = build_utils(session_db, ec2_client, FakeServerUtils())
    session = a_session()

    assert utils.fail_stuck_provisioning_session(session, 1800) is True
    assert session.state == VirtualDesktopSessionState.ERROR
    assert session_db.updated == [session]


def test_a_session_with_no_creation_time_is_left_alone():
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB()
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)

    assert (
        utils.fail_stuck_provisioning_session(
            a_session(provisioning_for_minutes=None), 1800
        )
        is False
    )
    assert ec2_client.describe_calls == []
    assert server_utils.terminated == []


def test_a_session_record_that_could_not_be_read_is_left_alone():
    ec2_client = FakeEc2Client()
    server_utils = FakeServerUtils()
    utils = build_utils(FakeSessionDB(), ec2_client, server_utils)

    assert utils.fail_stuck_provisioning_session(None, 1800) is False
    assert ec2_client.describe_calls == []
    assert server_utils.terminated == []


def test_a_desktop_that_is_not_provisioning_is_left_alone():
    ec2_client = FakeEc2Client(tags=[a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED)])
    server_utils = FakeServerUtils()
    utils = build_utils(FakeSessionDB(), ec2_client, server_utils)

    for state in (
        VirtualDesktopSessionState.READY,
        VirtualDesktopSessionState.STOPPED,
        VirtualDesktopSessionState.ERROR,
        VirtualDesktopSessionState.DELETING,
    ):
        assert (
            utils.fail_stuck_provisioning_session(
                a_session(provisioning_for_minutes=600, state=state), 1800
            )
            is False
        )

    assert ec2_client.describe_calls == []
    assert server_utils.terminated == []


def test_a_session_with_no_host_recorded_is_left_alone():
    ec2_client = FakeEc2Client()
    server_utils = FakeServerUtils()
    utils = build_utils(FakeSessionDB(), ec2_client, server_utils)

    session = a_session(instance_id=None)
    assert utils.fail_stuck_provisioning_session(session, 1800) is False
    assert ec2_client.describe_calls == []
    assert server_utils.terminated == []


# the threshold


def test_the_default_timeout_matches_the_scheduler_stack_provisioning_timeout():
    # scheduler.job_provisioning.stack_provisioning_timeout_seconds. it has to clear a gpu
    # driver build (about 10 minutes) and an ec2 mac launch (documented at 6 to 20).
    assert PROVISIONING_TIMEOUT_SECONDS_DEFAULT == 1800
    utils = build_utils(FakeSessionDB(), FakeEc2Client(), FakeServerUtils())
    assert utils.provisioning_timeout_seconds() == 1800


def test_an_operator_can_raise_the_timeout_without_a_code_change():
    ec2_client = FakeEc2Client(tags=[a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED)])
    session_db = FakeSessionDB(pages=[[a_session(provisioning_for_minutes=90)]])
    server_utils = FakeServerUtils()
    utils = build_utils(
        session_db,
        ec2_client,
        server_utils,
        config=FakeClusterConfig(timeout_seconds=4 * 60 * 60),
    )

    assert utils.fail_stuck_provisioning_sessions() == 0
    assert ec2_client.describe_calls == []
    assert server_utils.terminated == []


def test_a_timeout_of_zero_turns_the_sweep_off():
    session_db = FakeSessionDB(pages=[[a_session()]])
    server_utils = FakeServerUtils()
    utils = build_utils(
        session_db,
        FakeEc2Client(),
        server_utils,
        config=FakeClusterConfig(timeout_seconds=0),
    )

    assert utils.fail_stuck_provisioning_sessions() == 0
    assert session_db.requested_cursors == []
    assert server_utils.terminated == []


# the pass across every desktop


def test_every_page_of_sessions_is_walked_and_the_failures_counted():
    ec2_client = FakeEc2Client(tags=[a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED)])
    session_db = FakeSessionDB(
        pages=[
            [
                a_session(idea_session_id='one'),
                a_session(idea_session_id='two', provisioning_for_minutes=5),
            ],
            [a_session(idea_session_id='three')],
        ]
    )
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)

    assert utils.fail_stuck_provisioning_sessions() == 2
    assert session_db.requested_cursors == [None, '1']
    assert len(server_utils.terminated) == 2


def test_the_pass_gives_up_its_remaining_work_rather_than_run_past_its_budget():
    # the scheduled event that runs this also triggers desktop schedules, and going past
    # the queue visibility timeout would replay the whole message.
    ec2_client = FakeEc2Client(tags=[a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED)])
    session_db = FakeSessionDB(pages=[[a_session()]])
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)

    assert utils.fail_stuck_provisioning_sessions(time_budget_ms=0) == 0
    assert session_db.updated == []
    assert server_utils.terminated == []


def test_one_desktop_that_cannot_be_checked_does_not_stop_the_rest():
    class HalfBrokenEc2Client(FakeEc2Client):
        def describe_instances(self, **kwargs):
            self.describe_calls.append(kwargs)
            if len(self.describe_calls) == 1:
                raise RuntimeError('ec2 is having a moment')
            return {
                'Reservations': [
                    {'Instances': [{'InstanceId': INSTANCE_ID, 'Tags': self.tags}]}
                ]
            }

    ec2_client = HalfBrokenEc2Client(
        tags=[a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED)]
    )
    session_db = FakeSessionDB(
        pages=[[a_session(idea_session_id='one'), a_session(idea_session_id='two')]]
    )
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)

    assert utils.fail_stuck_provisioning_sessions() == 1
    assert len(ec2_client.describe_calls) == 2
    assert len(server_utils.terminated) == 1


# the reason has to outlive the pass that recorded it


class FakeNestedDB:
    """the session record nests the server, stack and schedules; only its own keys matter"""

    @staticmethod
    def convert_server_object_to_db_dict(_server) -> Dict:
        return {}

    @staticmethod
    def convert_software_stack_object_to_db_dict(_software_stack) -> Dict:
        return {}

    @staticmethod
    def convert_schedule_object_to_db_dict(_schedule) -> Dict:
        return {}

    @staticmethod
    def convert_db_entry_to_server_object(_db_dict):
        return VirtualDesktopServer(instance_id=INSTANCE_ID)

    @staticmethod
    def convert_db_dict_to_software_stack_object(_db_dict):
        return None

    @staticmethod
    def convert_db_dict_to_schedule_object(_db_dict):
        return None


def a_session_db() -> VirtualDesktopSessionDB:
    session_db = object.__new__(VirtualDesktopSessionDB)
    session_db._server_db = FakeNestedDB()
    session_db._software_stack_db = FakeNestedDB()
    session_db._schedule_db = FakeNestedDB()
    return session_db


def a_db_entry(**overrides) -> Dict:
    entry = {
        'owner': 'test-user',
        'idea_session_id': 'sess-1',
        'base_os': 'amazonlinux2023',
        'created_on': Utils.current_time_ms(),
        'updated_on': Utils.current_time_ms(),
        'name': 'my-desktop',
        'session_type': 'VIRTUAL',
        'state': 'ERROR',
    }
    entry.update(overrides)
    return entry


def test_the_recorded_failure_reason_is_written_to_the_session_record():
    session = a_session(state=VirtualDesktopSessionState.ERROR)
    session.software_stack = VirtualDesktopSoftwareStack(base_os='amazonlinux2023')
    session.failure_reason = 'The GPU driver failed to install'

    db_dict = a_session_db().convert_session_object_to_db_dict(session)
    assert db_dict['failure_reason'] == 'The GPU driver failed to install'


def test_the_failure_reason_reads_back_off_the_session_record():
    session = a_session_db().convert_db_dict_to_session_object(
        a_db_entry(failure_reason='The GPU driver failed to install')
    )
    assert session.failure_reason == 'The GPU driver failed to install'


def test_a_record_written_before_this_release_reads_back_with_no_reason():
    session = a_session_db().convert_db_dict_to_session_object(a_db_entry())
    assert session.failure_reason is None


def test_a_pass_resumes_from_the_page_the_last_one_stopped_on():
    ec2_client = FakeEc2Client(tags=[a_bootstrap_status_tag(GPU_DRIVER_INSTALL_FAILED)])
    session_db = FakeSessionDB(
        pages=[[a_session(idea_session_id='one')], [a_session(idea_session_id='two')]]
    )
    server_utils = FakeServerUtils()
    utils = build_utils(session_db, ec2_client, server_utils)
    VirtualDesktopSessionUtils._provisioning_timeout_cursor = '1'

    assert utils.fail_stuck_provisioning_sessions() == 1
    assert session_db.requested_cursors == ['1']
    # the end of the table was reached, so the next pass starts at the first page again
    assert utils._provisioning_timeout_cursor is None


def test_a_pass_that_runs_out_of_time_keeps_its_place():
    # a table larger than one time budget is never walked past its first pages otherwise
    session_db = FakeSessionDB(pages=[[a_session()], [a_session()]])
    utils = build_utils(session_db, FakeEc2Client(), FakeServerUtils())
    VirtualDesktopSessionUtils._provisioning_timeout_cursor = '1'

    assert utils.fail_stuck_provisioning_sessions(time_budget_ms=0) == 0
    assert utils._provisioning_timeout_cursor == '1'
