"""
Repairing a desktop that launched before its project's Bedrock instance profile existed.

The launch falls back to the shared DCV host profile on purpose, which beats refusing
the request. Nothing used to revisit that choice, so the desktop was denied every
bedrock-runtime call for the life of the instance while the portal listed the models.
"""

from unittest.mock import Mock
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from ideadatamodel import (
    Project,
    ProjectBedrockConfig,
    SocaListingPayload,
    SocaPaginator,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
    VirtualDesktopServerDB,
)
from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    VirtualDesktopControllerUtils,
)

SHARED_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::111111111111:instance-profile/idea-test-vdc-host-instance-profile'
)
PROJECT_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::111111111111:instance-profile/idea/idea-test/projects/'
    'idea-test-p1-project'
)
OLD_PROJECT_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::111111111111:instance-profile/idea/idea-test/projects/'
    'idea-test-p1-project-previous'
)
PROJECT_ID = 'a1b2c3d4-0000-4000-8000-000000000001'
INSTANCE_ID = 'i-00000000000000001'
ASSOCIATION_ID = 'iip-assoc-00000000000000001'


def client_error(error_code: str, operation: str) -> ClientError:
    return ClientError(
        {'Error': {'Code': error_code, 'Message': error_code}}, operation
    )


class FakeEc2Client:
    """records the instance profile calls instead of making them"""

    def __init__(
        self,
        associations: Optional[List[Dict]] = None,
        describe_error: Optional[Exception] = None,
        apply_error: Optional[Exception] = None,
    ):
        self.associations = [] if associations is None else associations
        self.describe_error = describe_error
        self.apply_error = apply_error
        self.describe_calls: List[Dict] = []
        self.replace_calls: List[Dict] = []
        self.associate_calls: List[Dict] = []

    def describe_iam_instance_profile_associations(self, **kwargs) -> Dict[str, Any]:
        self.describe_calls.append(kwargs)
        if self.describe_error is not None:
            raise self.describe_error
        return {'IamInstanceProfileAssociations': self.associations}

    def replace_iam_instance_profile_association(self, **kwargs) -> Dict[str, Any]:
        self.replace_calls.append(kwargs)
        if self.apply_error is not None:
            raise self.apply_error
        return {}

    def associate_iam_instance_profile(self, **kwargs) -> Dict[str, Any]:
        self.associate_calls.append(kwargs)
        if self.apply_error is not None:
            raise self.apply_error
        return {}

    @property
    def apply_calls(self) -> List[Dict]:
        return self.replace_calls + self.associate_calls


class FakeSessionDB:
    def __init__(self, pages: Optional[List[List[VirtualDesktopSession]]] = None):
        self.pages = [] if pages is None else pages
        self.requested_cursors: List[Optional[str]] = []
        self.updated: List[VirtualDesktopSession] = []

    def list_all_from_db(self, request) -> SocaListingPayload:
        self.requested_cursors.append(request.cursor)
        index = 0 if request.cursor is None else int(request.cursor)
        next_cursor = str(index + 1) if index + 1 < len(self.pages) else None
        return SocaListingPayload(
            listing=self.pages[index],
            paginator=SocaPaginator(cursor=next_cursor),
        )

    def update(self, session: VirtualDesktopSession) -> VirtualDesktopSession:
        self.updated.append(session)
        return session


class FakeProjectsClient:
    """serves the stored project, so a payload supplied block is never read"""

    def __init__(self, project: Optional[Project] = None, raises: bool = False):
        self.project = project
        self.raises = raises
        self.requested_project_ids: List[str] = []

    def get_project(self, request):
        self.requested_project_ids.append(request.project_id)
        if self.raises:
            raise Exception('projects client unavailable')
        return type('GetProjectResult', (), {'project': self.project})()


class FakeClusterConfig:
    def __init__(self, bedrock_enabled: bool):
        self.bedrock_enabled = bedrock_enabled

    @staticmethod
    def get_module_id(_module_name: str) -> str:
        return 'cluster-manager'

    def get_bool(self, key: str, default: bool = None) -> bool:
        if key == 'cluster-manager.bedrock.enabled':
            return self.bedrock_enabled
        return default


class FakeContext:
    def __init__(self, projects_client: FakeProjectsClient, bedrock_enabled: bool):
        self.projects_client = projects_client
        self._config = FakeClusterConfig(bedrock_enabled)

    def config(self) -> FakeClusterConfig:
        return self._config


def bedrock_project(instance_profile_arn: Optional[str]) -> Project:
    return Project(
        project_id=PROJECT_ID,
        name='test-project',
        bedrock=ProjectBedrockConfig(
            enabled=True,
            model_ids=['vendor.model'],
            instance_profile_arn=instance_profile_arn,
        ),
    )


def build_utils(
    session_db: FakeSessionDB,
    ec2_client: FakeEc2Client,
    projects_client: FakeProjectsClient,
    bedrock_enabled: bool = True,
) -> VirtualDesktopSessionUtils:
    # the repair reaches the project record, ec2 and the session db only; the
    # constructors and their aws clients are skipped
    controller_utils = object.__new__(VirtualDesktopControllerUtils)
    controller_utils.ec2_client = ec2_client
    controller_utils._logger = Mock()

    utils = object.__new__(VirtualDesktopSessionUtils)
    utils.context = FakeContext(projects_client, bedrock_enabled)
    utils._logger = Mock()
    utils._session_db = session_db
    utils._controller_utils = controller_utils
    return utils


def a_session(
    instance_profile_arn: Optional[str] = SHARED_INSTANCE_PROFILE_ARN,
    state: VirtualDesktopSessionState = VirtualDesktopSessionState.READY,
    instance_id: Optional[str] = INSTANCE_ID,
    idea_session_id: str = 'sess-1',
) -> VirtualDesktopSession:
    return VirtualDesktopSession(
        idea_session_id=idea_session_id,
        name='my-desktop',
        owner='test-user',
        state=state,
        project=Project(project_id=PROJECT_ID, name='test-project'),
        server=VirtualDesktopServer(
            instance_id=instance_id,
            instance_type='m5.large',
            instance_profile_arn=instance_profile_arn,
        ),
    )


def an_association(instance_profile_arn: str) -> Dict:
    return {
        'AssociationId': ASSOCIATION_ID,
        'InstanceId': INSTANCE_ID,
        'IamInstanceProfile': {'Arn': instance_profile_arn},
        'State': 'associated',
    }


# what the desktop actually launched under is recorded


def test_the_applied_instance_profile_is_written_to_the_session_record():
    db_dict = VirtualDesktopServerDB.convert_server_object_to_db_dict(
        VirtualDesktopServer(
            instance_id=INSTANCE_ID,
            instance_profile_arn=SHARED_INSTANCE_PROFILE_ARN,
        )
    )
    assert db_dict['instance_profile_arn'] == SHARED_INSTANCE_PROFILE_ARN

    server = VirtualDesktopServerDB.convert_db_entry_to_server_object(db_dict)
    assert server.instance_profile_arn == SHARED_INSTANCE_PROFILE_ARN


def test_an_absent_instance_profile_is_omitted_rather_than_written_empty():
    # update() writes every key it is given, so writing None would erase what a
    # previous launch or repair recorded.
    db_dict = VirtualDesktopServerDB.convert_server_object_to_db_dict(
        VirtualDesktopServer(instance_id=INSTANCE_ID)
    )
    assert 'instance_profile_arn' not in db_dict


def test_a_record_written_before_this_release_reads_back_as_unknown():
    server = VirtualDesktopServerDB.convert_db_entry_to_server_object(
        {'instance_id': INSTANCE_ID, 'instance_type': 'm5.large'}
    )
    assert server.instance_profile_arn is None


# the repair


def test_a_desktop_on_the_shared_profile_is_moved_onto_the_project_profile():
    ec2_client = FakeEc2Client(
        associations=[an_association(SHARED_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )
    session = a_session()

    assert utils.repair_project_instance_profile(session) is True
    assert ec2_client.replace_calls == [
        {
            'IamInstanceProfile': {'Arn': PROJECT_INSTANCE_PROFILE_ARN},
            'AssociationId': ASSOCIATION_ID,
        }
    ]
    assert session.server.instance_profile_arn == PROJECT_INSTANCE_PROFILE_ARN
    assert session_db.updated == [session]


def test_a_desktop_on_a_superseded_project_profile_is_moved_onto_the_current_one():
    ec2_client = FakeEc2Client(
        associations=[an_association(OLD_PROJECT_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )

    assert (
        utils.repair_project_instance_profile(
            a_session(instance_profile_arn=OLD_PROJECT_INSTANCE_PROFILE_ARN)
        )
        is True
    )
    assert len(ec2_client.replace_calls) == 1


def test_a_desktop_with_no_association_is_given_one():
    ec2_client = FakeEc2Client(associations=[])
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )

    assert utils.repair_project_instance_profile(a_session()) is True
    assert ec2_client.associate_calls == [
        {
            'IamInstanceProfile': {'Arn': PROJECT_INSTANCE_PROFILE_ARN},
            'InstanceId': INSTANCE_ID,
        }
    ]
    assert ec2_client.replace_calls == []


def test_a_desktop_already_recorded_on_the_project_profile_is_left_alone():
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )

    assert (
        utils.repair_project_instance_profile(
            a_session(instance_profile_arn=PROJECT_INSTANCE_PROFILE_ARN)
        )
        is False
    )
    assert ec2_client.describe_calls == []
    assert session_db.updated == []


def test_a_desktop_ec2_already_has_on_the_project_profile_only_gets_its_record_fixed():
    # sessions launched before this release carry no recorded profile, so what ec2
    # reports is written back instead of the desktop being touched.
    ec2_client = FakeEc2Client(
        associations=[an_association(PROJECT_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )
    session = a_session(instance_profile_arn=None)

    assert utils.repair_project_instance_profile(session) is False
    assert ec2_client.apply_calls == []
    assert session.server.instance_profile_arn == PROJECT_INSTANCE_PROFILE_ARN
    assert session_db.updated == [session]


def test_an_unreadable_association_leaves_the_desktop_untouched():
    # an unknown must not be acted on as though the desktop carried no role.
    ec2_client = FakeEc2Client(
        describe_error=client_error(
            'UnauthorizedOperation', 'DescribeIamInstanceProfileAssociations'
        )
    )
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )
    session = a_session()

    assert utils.repair_project_instance_profile(session) is False
    assert ec2_client.apply_calls == []
    assert session.server.instance_profile_arn == SHARED_INSTANCE_PROFILE_ARN
    assert session_db.updated == []


def test_a_refused_association_change_is_not_recorded_as_applied():
    ec2_client = FakeEc2Client(
        associations=[an_association(SHARED_INSTANCE_PROFILE_ARN)],
        apply_error=client_error(
            'UnauthorizedOperation', 'ReplaceIamInstanceProfileAssociation'
        ),
    )
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )
    session = a_session()

    assert utils.repair_project_instance_profile(session) is False
    assert session.server.instance_profile_arn == SHARED_INSTANCE_PROFILE_ARN
    assert session_db.updated == []


def test_an_unreadable_project_leaves_the_desktop_untouched():
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB()
    utils = build_utils(session_db, ec2_client, FakeProjectsClient(raises=True))

    assert utils.repair_project_instance_profile(a_session()) is False
    assert ec2_client.describe_calls == []
    assert session_db.updated == []


def test_a_project_whose_profile_is_not_provisioned_yet_leaves_the_desktop_untouched():
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db, ec2_client, FakeProjectsClient(bedrock_project(None))
    )

    assert utils.repair_project_instance_profile(a_session()) is False
    assert ec2_client.describe_calls == []
    assert session_db.updated == []


def test_a_non_bedrock_project_leaves_the_desktop_untouched():
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(Project(project_id=PROJECT_ID, name='test-project')),
    )

    assert utils.repair_project_instance_profile(a_session()) is False
    assert ec2_client.describe_calls == []
    assert session_db.updated == []


def test_a_desktop_being_deleted_or_already_failed_is_not_repaired():
    ec2_client = FakeEc2Client(
        associations=[an_association(SHARED_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB()
    projects_client = FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN))
    utils = build_utils(session_db, ec2_client, projects_client)

    for state in (
        VirtualDesktopSessionState.DELETING,
        VirtualDesktopSessionState.DELETED,
        VirtualDesktopSessionState.ERROR,
    ):
        assert utils.repair_project_instance_profile(a_session(state=state)) is False

    assert projects_client.requested_project_ids == []
    assert ec2_client.apply_calls == []


def test_a_session_with_no_host_is_not_repaired():
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB()
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )

    assert utils.repair_project_instance_profile(a_session(instance_id=None)) is False
    assert ec2_client.describe_calls == []


# the pass across every desktop


def test_every_page_of_sessions_is_walked_and_the_repairs_counted():
    ec2_client = FakeEc2Client(
        associations=[an_association(SHARED_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB(
        pages=[
            [a_session(idea_session_id='one'), a_session(idea_session_id='two')],
            [
                a_session(
                    idea_session_id='three',
                    instance_profile_arn=PROJECT_INSTANCE_PROFILE_ARN,
                )
            ],
        ]
    )
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )

    assert utils.repair_project_instance_profiles() == 2
    assert session_db.requested_cursors == [None, '1']


def test_a_cluster_with_bedrock_turned_off_is_not_scanned():
    ec2_client = FakeEc2Client()
    session_db = FakeSessionDB(pages=[[a_session()]])
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
        bedrock_enabled=False,
    )

    assert utils.repair_project_instance_profiles() == 0
    assert session_db.requested_cursors == []


def test_the_pass_gives_up_its_remaining_work_rather_than_run_past_its_budget():
    # the scheduled event that runs this also triggers desktop schedules, and going
    # past the queue visibility timeout would replay the whole message.
    ec2_client = FakeEc2Client(
        associations=[an_association(SHARED_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB(pages=[[a_session()]])
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )

    assert utils.repair_project_instance_profiles(time_budget_ms=0) == 0
    assert ec2_client.apply_calls == []
    assert session_db.updated == []


def test_one_desktop_that_cannot_be_repaired_does_not_stop_the_rest():
    class HalfBrokenEc2Client(FakeEc2Client):
        def describe_iam_instance_profile_associations(self, **kwargs):
            self.describe_calls.append(kwargs)
            if len(self.describe_calls) == 1:
                raise RuntimeError('ec2 is having a moment')
            return {'IamInstanceProfileAssociations': self.associations}

    ec2_client = HalfBrokenEc2Client(
        associations=[an_association(SHARED_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB(
        pages=[[a_session(idea_session_id='one'), a_session(idea_session_id='two')]]
    )
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )

    assert utils.repair_project_instance_profiles() == 1
    assert len(ec2_client.describe_calls) == 2


def test_a_pass_resumes_from_the_page_the_last_one_stopped_on():
    ec2_client = FakeEc2Client(
        associations=[an_association(SHARED_INSTANCE_PROFILE_ARN)]
    )
    session_db = FakeSessionDB(
        pages=[[a_session(idea_session_id='one')], [a_session(idea_session_id='two')]]
    )
    utils = build_utils(
        session_db,
        ec2_client,
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )
    utils._instance_profile_repair_cursor = '1'

    assert utils.repair_project_instance_profiles() == 1
    assert session_db.requested_cursors == ['1']
    # the end of the table was reached, so the next pass starts at the first page again
    assert utils._instance_profile_repair_cursor is None


def test_a_pass_that_runs_out_of_time_keeps_its_place():
    # a table larger than one time budget is never walked past its first pages otherwise
    session_db = FakeSessionDB(pages=[[a_session()], [a_session()]])
    utils = build_utils(
        session_db,
        FakeEc2Client(),
        FakeProjectsClient(bedrock_project(PROJECT_INSTANCE_PROFILE_ARN)),
    )
    utils._instance_profile_repair_cursor = '1'

    assert utils.repair_project_instance_profiles(time_budget_ms=0) == 0
    assert utils._instance_profile_repair_cursor == '1'
