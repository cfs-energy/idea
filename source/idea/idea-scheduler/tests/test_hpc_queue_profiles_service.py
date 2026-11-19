"""
Test Cases for HpcQueueProfilesService queue list updates

Pins the invariant that create_queues() / delete_queues() / update_queue_profile()
write the resulting queue list to the queue profile - not just the queues named in
the request, and never a queue list the scheduler does not hold.
"""

from ideadatamodel import (
    SocaAnyPayload,
    exceptions,
    errorcodes,
    Project,
    SocaJobParams,
)
from ideadatamodel.scheduler import HpcQueueProfile, SocaQueueMode
from ideascheduler.app.provisioning.job_provisioning_queue.hpc_queue_profiles_dao import (
    HpcQueueProfilesDAO,
)
from ideascheduler.app.provisioning.job_provisioning_queue.hpc_queue_profiles_service import (
    HpcQueueProfilesService,
)

from typing import Dict, List
import pytest
from types import SimpleNamespace

from ideasdk.utils import Utils


QUEUE_PROFILE_ID = 'queue-profile-1'


class MockScheduler:
    """
    records the scheduler queues created / deleted by the service.
    queues added to busy[] fail to delete, as a queue with jobs in it does.
    """

    def __init__(self):
        self.created: List[str] = []
        self.deleted: List[str] = []
        self.busy: List[str] = []

    def create_queue(self, queue_name: str):
        self.created.append(queue_name)

    def delete_queue(self, queue_name: str):
        if queue_name in self.busy:
            raise exceptions.SocaException(
                error_code=errorcodes.SCHEDULER_QUEUE_BUSY, message='returncode: 179'
            )
        self.deleted.append(queue_name)


@pytest.fixture()
def queue_profiles_service(context, monkeypatch):
    """
    HpcQueueProfilesService wired to in-memory doubles. DAO initialization is
    skipped so that no dynamodb table is created.
    """
    monkeypatch.setattr(HpcQueueProfilesDAO, 'initialize', lambda self: None)
    context.scheduler = MockScheduler()

    service = HpcQueueProfilesService(context=context)
    service.db_updates: List[Dict] = []

    def update(db_queue_profile: Dict, remove_keys=None) -> Dict:
        service.db_updates.append(db_queue_profile)
        return {
            'queue_profile_id': QUEUE_PROFILE_ID,
            'name': 'test-queue-profile',
            'queues': list(db_queue_profile['queues']),
        }

    monkeypatch.setattr(service.queue_profile_dao, 'update', update)
    monkeypatch.setattr(
        service.queue_profile_dao, 'get_queue_profile_by_queue', lambda _: None
    )
    # one profile in this world: no other profile lists its queues (S4)
    monkeypatch.setattr(service.queue_profile_dao, 'list_queue_profiles', lambda: [])
    monkeypatch.setattr(service, 'initialize_job_provisioner', lambda _: None)
    monkeypatch.setattr(service, 'cache_set', lambda _: None)
    return service


def set_queue_profile(service, monkeypatch, queues: List[str]) -> HpcQueueProfile:
    queue_profile = HpcQueueProfile(
        queue_profile_id=QUEUE_PROFILE_ID,
        name='test-queue-profile',
        queues=list(queues),
    )
    monkeypatch.setattr(service, 'get_queue_profile', lambda *_, **__: queue_profile)
    return queue_profile


def test_create_queues_retains_existing_queues(queue_profiles_service, monkeypatch):
    """
    create_queues must write existing + new queues, not only the new ones
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'high'])

    service.create_queues(queue_names=['low'], queue_profile_id=QUEUE_PROFILE_ID)

    assert len(service.db_updates) == 1
    assert service.db_updates[0]['queues'] == ['normal', 'high', 'low']
    assert service.context.scheduler.created == ['low']


def test_create_queues_does_not_duplicate_existing_queue(
    queue_profiles_service, monkeypatch
):
    """
    re-adding an already associated queue must not duplicate it in the profile
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'high'])

    service.create_queues(
        queue_names=['high', 'low'], queue_profile_id=QUEUE_PROFILE_ID
    )

    assert service.db_updates[0]['queues'] == ['normal', 'high', 'low']


def test_create_queues_on_profile_without_queues(queue_profiles_service, monkeypatch):
    """
    queues is Optional on HpcQueueProfile - a profile with no queues must not raise
    """
    service = queue_profiles_service
    queue_profile = HpcQueueProfile(
        queue_profile_id=QUEUE_PROFILE_ID, name='test-queue-profile', queues=None
    )
    monkeypatch.setattr(service, 'get_queue_profile', lambda *_, **__: queue_profile)

    service.create_queues(queue_names=['normal'], queue_profile_id=QUEUE_PROFILE_ID)

    assert service.db_updates[0]['queues'] == ['normal']


def test_delete_queues_writes_remaining_queues(queue_profiles_service, monkeypatch):
    """
    delete_queues must write the queues that remain, not the deleted ones
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'high', 'low'])

    service.delete_queues(queue_names=['high'], queue_profile_id=QUEUE_PROFILE_ID)

    assert len(service.db_updates) == 1
    assert service.db_updates[0]['queues'] == ['normal', 'low']
    assert service.context.scheduler.deleted == ['high']


def test_delete_queues_all_queues_writes_empty_list(
    queue_profiles_service, monkeypatch
):
    """
    deleting every queue leaves the profile with an empty queue list
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'high'])

    service.delete_queues(
        queue_names=['normal', 'high'], queue_profile_id=QUEUE_PROFILE_ID
    )

    assert service.db_updates[0]['queues'] == []


def test_delete_queues_unknown_queue_leaves_profile_unchanged(
    queue_profiles_service, monkeypatch
):
    """
    validation failure must not mutate the profile or delete scheduler queues
    """
    service = queue_profiles_service
    queue_profile = set_queue_profile(service, monkeypatch, ['normal', 'high'])

    with pytest.raises(exceptions.SocaException):
        service.delete_queues(
            queue_names=['normal', 'unknown'], queue_profile_id=QUEUE_PROFILE_ID
        )

    assert queue_profile.queues == ['normal', 'high']
    assert service.db_updates == []
    assert service.context.scheduler.deleted == []


def test_create_then_delete_round_trip(queue_profiles_service, monkeypatch):
    """
    the queue list written back by create must survive a subsequent delete
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal'])

    service.create_queues(
        queue_names=['high', 'low'], queue_profile_id=QUEUE_PROFILE_ID
    )
    assert service.db_updates[-1]['queues'] == ['normal', 'high', 'low']

    set_queue_profile(service, monkeypatch, service.db_updates[-1]['queues'])
    service.delete_queues(queue_names=['low'], queue_profile_id=QUEUE_PROFILE_ID)
    assert service.db_updates[-1]['queues'] == ['normal', 'high']


def test_delete_queue_profile_with_busy_queue_leaves_profile_intact(
    queue_profiles_service, monkeypatch
):
    """
    a queue with jobs in it cannot be deleted. the profile, its cache entry and its
    provisioner stay as they are, so those jobs keep a servicing provisioner.
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'ansys'])
    service.context.scheduler.busy.append('ansys')
    calls: List[str] = []
    monkeypatch.setattr(
        service, 'stop_job_provisioner', lambda _: calls.append('stop_provisioner')
    )
    monkeypatch.setattr(service, 'cache_clear', lambda _: calls.append('cache_clear'))
    monkeypatch.setattr(
        service.queue_profile_dao,
        'delete_queue_profile',
        lambda **_: calls.append('delete_db_row'),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        service.delete_queue_profile(queue_profile_id=QUEUE_PROFILE_ID)

    assert exc_info.value.error_code == errorcodes.SCHEDULER_QUEUE_BUSY
    assert 'ansys' in exc_info.value.message
    assert calls == []


def test_queue_names_required(queue_profiles_service):
    """
    both entry points reject an empty queue_names[]
    """
    service = queue_profiles_service
    with pytest.raises(exceptions.SocaException):
        service.create_queues(queue_names=[], queue_profile_id=QUEUE_PROFILE_ID)
    with pytest.raises(exceptions.SocaException):
        service.delete_queues(queue_names=[], queue_profile_id=QUEUE_PROFILE_ID)


def update_request(service, monkeypatch, queues: List[str]) -> HpcQueueProfile:
    """
    the queue profile as submitted by an admin. job parameter validation is not
    the subject of these test cases.
    """
    monkeypatch.setattr(service, 'validate_and_sanitize_queue_profile', lambda _: None)
    return HpcQueueProfile(
        queue_profile_id=QUEUE_PROFILE_ID,
        name='test-queue-profile',
        queues=list(queues),
    )


def test_update_queue_profile_writes_profile_after_scheduler(
    queue_profiles_service, monkeypatch
):
    """
    the profile must not be committed before the scheduler queues are reconciled
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'ansys'])
    request = update_request(service, monkeypatch, ['normal', 'comsol'])

    scheduler = service.context.scheduler
    create_queue = scheduler.create_queue
    delete_queue = scheduler.delete_queue
    # queue name and the number of db updates written when the scheduler was called
    reconciled = []

    def record_create(queue_name: str):
        reconciled.append((queue_name, len(service.db_updates)))
        create_queue(queue_name)

    def record_delete(queue_name: str):
        reconciled.append((queue_name, len(service.db_updates)))
        delete_queue(queue_name)

    scheduler.create_queue = record_create
    scheduler.delete_queue = record_delete

    updated = service.update_queue_profile(request)

    assert reconciled == [('comsol', 0), ('ansys', 0)]
    assert service.db_updates[0]['queues'] == ['normal', 'comsol']
    assert updated.queues == ['normal', 'comsol']
    assert scheduler.created == ['comsol']
    assert scheduler.deleted == ['ansys']


def test_update_queue_profile_retains_queue_that_could_not_be_deleted(
    queue_profiles_service, monkeypatch
):
    """
    a busy queue stays on the persisted profile, so its queued jobs keep a provisioner
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'ansys'])
    request = update_request(service, monkeypatch, ['normal'])
    service.context.scheduler.busy = ['ansys']

    provisioned = []
    cached = []
    monkeypatch.setattr(service, 'initialize_job_provisioner', provisioned.append)
    monkeypatch.setattr(service, 'cache_set', cached.append)

    with pytest.raises(exceptions.SocaException) as exc_info:
        service.update_queue_profile(request)

    assert exc_info.value.error_code == errorcodes.SCHEDULER_QUEUE_BUSY
    assert 'ansys' in exc_info.value.message
    assert service.db_updates[0]['queues'] == ['normal', 'ansys']
    assert cached[-1].queues == ['normal', 'ansys']
    assert provisioned[-1].queues == ['normal', 'ansys']


def test_update_queue_profile_retains_only_the_failed_queue(
    queue_profiles_service, monkeypatch
):
    """
    queues that were deleted successfully are still removed from the profile
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'ansys', 'comsol'])
    request = update_request(service, monkeypatch, ['normal'])
    service.context.scheduler.busy = ['ansys']

    with pytest.raises(exceptions.SocaException):
        service.update_queue_profile(request)

    assert service.db_updates[0]['queues'] == ['normal', 'ansys']
    assert service.context.scheduler.deleted == ['comsol']


def test_update_queue_profile_without_queues_deletes_nothing(
    queue_profiles_service, monkeypatch
):
    """
    an update carrying no queues is not a request to remove every queue: the real
    validator refuses it before anything is deleted or written (SCH-41: this ran with the
    validator replaced and asserted a success the product never gives)
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'ansys'])
    request = HpcQueueProfile(
        queue_profile_id=QUEUE_PROFILE_ID, name='test-queue-profile', queues=[]
    )

    with pytest.raises(exceptions.SocaException) as error:
        service.update_queue_profile(request)

    assert error.value.message.endswith('queue_profile.queues is required')
    assert service.db_updates == []
    assert service.context.scheduler.deleted == []


def test_update_queue_profile_create_failure_deletes_nothing(
    queue_profiles_service, monkeypatch
):
    """
    a scheduler create failure aborts before any queue is deleted or the profile written
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'ansys'])
    request = update_request(service, monkeypatch, ['normal', 'comsol'])

    def create_queue(queue_name: str):
        raise exceptions.SocaException(
            error_code=errorcodes.SCHEDULER_ERROR, message='returncode: 1'
        )

    service.context.scheduler.create_queue = create_queue

    with pytest.raises(exceptions.SocaException) as exc_info:
        service.update_queue_profile(request)

    assert exc_info.value.error_code == errorcodes.SCHEDULER_ERROR
    assert service.db_updates == []
    assert service.context.scheduler.deleted == []


def test_create_queue_profile_rejects_ami_architecture_mismatch(queue_profiles_service):
    """
    the queue profile default job params are validated with the same job param builder a
    submit uses, so an instance_ami paired with instance_types of the other architecture is
    rejected at save rather than at RunInstances.
    """
    service = queue_profiles_service

    queue_profile = HpcQueueProfile(
        name='test-queue-profile',
        queues=['normal'],
        keep_forever=True,
        queue_mode=SocaQueueMode.FIFO,
        projects=[Project(project_id='mock-project')],
        default_job_params=SocaJobParams(
            instance_ami='ami-arm64mockimage00',
            instance_types=['c5.large'],
        ),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        service.create_queue_profile(queue_profile)

    assert exc_info.value.error_code == errorcodes.VALIDATION_FAILED
    messages = [
        entry.message
        for entry in exc_info.value.ref.results
        if entry.message is not None
    ]
    assert any('ami-arm64mockimage00) is arm64' in message for message in messages)


# a queue name PBS cannot hold: the real service and the real scheduler class, over a
# qmgr and qstat that answer as PBS does, and a table that keeps what is put in it


class PbsQueues:
    """qmgr and qstat for queues: 15 characters is the longest name PBS takes"""

    def __init__(self):
        self.queues: List[str] = []
        self.commands: List[str] = []
        self.refuse_create = False

    @staticmethod
    def result(returncode: int = 0, stdout: str = '', stderr: str = ''):
        from subprocess import CompletedProcess

        from ideasdk.shell.shell_invoker import ShellInvocationResult

        return ShellInvocationResult(
            command=['pbs'],
            result=CompletedProcess(['pbs'], returncode, stdout, stderr),
            total_time_ms=5,
        )

    def invoke(self, cmd=None, **_):
        import json

        from ideascheduler.app.scheduler.openpbs import openpbs_constants

        self.commands.append(' '.join(cmd))
        if cmd[0] == openpbs_constants.QSTAT:
            names = cmd[5:] or self.queues
            if any(len(name) > 15 for name in cmd[5:]):
                return self.result(1, stderr="qstat: illegal 'destination' value")
            if any(name not in self.queues for name in names):
                return self.result(
                    openpbs_constants.QSTAT_ERROR_CODE_UNKNOWN_QUEUE,
                    stderr=f'qstat: Unknown queue {names[0]}',
                )
            queue = {
                'queue_type': 'Execution',
                'total_jobs': 0,
                'state_count': 'Transit:0 Queued:0 Held:0 Waiting:0 Running:0 Exiting:0 Begun:0 ',
                'enabled': 'True',
                'started': 'True',
            }
            return self.result(
                stdout=json.dumps({'Queue': {name: queue for name in names}})
            )
        verb, _, name = cmd[2].split(' ')[:3]
        if verb == 'create':
            if len(name) > 15 or self.refuse_create:
                return self.result(
                    1, stderr=f'qmgr obj={name} svr=default: Illegal attribute or name'
                )
            self.queues.append(name)
        elif verb == 'delete':
            self.queues.remove(name)
        return self.result()


class ProfilesTable:
    def __init__(self):
        self.rows: Dict[str, Dict] = {}

    def put_item(self, Item):  # noqa: N803
        self.rows[Item['queue_profile_id']] = Item

    def get_item(self, Key):  # noqa: N803
        row = self.rows.get(Key['queue_profile_id'])
        return {'Item': row} if row else {}

    def delete_item(self, Key):  # noqa: N803
        self.rows.pop(Key['queue_profile_id'], None)

    def scan(self, **_):
        return {'Items': list(self.rows.values())}

    def update_item(  # noqa: N803
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues,
        **_,
    ):
        row = self.rows[Key['queue_profile_id']]
        set_part, _, remove_part = UpdateExpression.partition(' REMOVE ')
        for token in set_part[len('SET ') :].split(', '):
            name, value = token.split(' = ')
            row[ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]
        for name in filter(None, remove_part.split(', ')):
            row.pop(ExpressionAttributeNames[name], None)
        return {'Attributes': dict(row)}


@pytest.fixture()
def pbs_queue_profiles(context, monkeypatch):
    from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import OpenPBSScheduler

    monkeypatch.setattr(HpcQueueProfilesDAO, 'initialize', lambda self: None)
    pbs = PbsQueues()
    scheduler = OpenPBSScheduler(context=context)
    scheduler._shell = pbs
    context.scheduler = scheduler
    service = HpcQueueProfilesService(context=context)
    table = ProfilesTable()
    service.queue_profile_dao.table = table
    # the table's own filter is dynamodb's; one row at most carries a name here
    monkeypatch.setattr(
        service.queue_profile_dao,
        'get_queue_profile_by_name',
        lambda name: next(
            (row for row in table.rows.values() if row['name'] == name), None
        ),
    )
    return service, pbs, table


def profile_named(name: str, queues: List[str]) -> HpcQueueProfile:
    return HpcQueueProfile(
        name=name,
        title=name,
        projects=[Project(project_id='project-1')],
        queues=queues,
        queue_mode=SocaQueueMode.FIFO,
        scaling_mode='single-job',
        terminate_when_idle=0,
        default_job_params=SocaJobParams(
            instance_types=['c5.large'], base_os='amazonlinux2023'
        ),
    )


def test_a_queue_name_longer_than_pbs_allows_is_refused_before_anything_is_written(
    pbs_queue_profiles,
):
    """
    CreateQueueProfile with queue rc9-kernel-proof (16 characters) failed at qmgr, after
    the profile row was written: a profile whose queue could not exist.
    """
    service, pbs, table = pbs_queue_profiles

    with pytest.raises(exceptions.SocaException) as refused:
        service.create_queue_profile(
            profile_named('rc9-kernel-proof', ['rc9-kernel-proof'])
        )

    assert refused.value.error_code == errorcodes.INVALID_PARAMS
    assert (
        'Queue name rc9-kernel-proof is 16 characters long. The scheduler allows 15. '
        'Use a shorter queue name.'
    ) in refused.value.message
    assert table.rows == {}
    assert pbs.commands == []

    # 15 characters is a queue
    created = service.create_queue_profile(
        profile_named('rc9-kernel-proof', ['rc9-kernel-pro'])
    )
    assert pbs.queues == ['rc9-kernel-pro']
    assert [row['queues'] for row in table.rows.values()] == [['rc9-kernel-pro']]

    # and the same check guards a queue added to a profile later
    with pytest.raises(exceptions.SocaException) as refused:
        service.create_queues(
            queue_names=['rc9-kernel-proof'],
            queue_profile_id=created.queue_profile_id,
        )
    assert 'is 16 characters long' in refused.value.message
    assert pbs.queues == ['rc9-kernel-pro']


def test_a_profile_is_not_written_when_the_scheduler_refuses_its_queue(
    pbs_queue_profiles,
):
    service, pbs, table = pbs_queue_profiles
    pbs.refuse_create = True

    with pytest.raises(exceptions.SocaException):
        service.create_queue_profile(profile_named('refused', ['refused']))

    assert table.rows == {}


def test_a_profile_whose_queue_pbs_cannot_have_can_be_deleted(pbs_queue_profiles):
    """
    The half-created profile could not be deleted either: DeleteQueueProfile asked PBS for
    the queue and failed on "qstat: illegal 'destination' value". The queue PBS has under
    a legal name goes with the profile as before.
    """
    service, pbs, table = pbs_queue_profiles
    pbs.queues = ['rc9-kproof']
    table.rows['p-1'] = {
        'queue_profile_id': 'p-1',
        'name': 'rc9-kernel-proof',
        'queues': ['rc9-kernel-proof', 'rc9-kproof'],
        'enabled': False,
    }

    service.delete_queue_profile(queue_profile_id='p-1')

    assert table.rows == {}
    assert pbs.queues == []


def test_fsx_lustre_default_round_trips_through_the_table():
    from ideadatamodel import SocaFSxLustreConfig

    profile = HpcQueueProfile(
        name='fsx',
        default_job_params=SocaJobParams(
            fsx_lustre=SocaFSxLustreConfig(
                enabled=True,
                existing_fsx='fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com',
                deployment_type='PERSISTENT_2',
            )
        ),
    )
    db = HpcQueueProfilesDAO.convert_to_db(profile)
    assert (
        db['param_fsx_lustre_existing_fsx']
        == profile.default_job_params.fsx_lustre.existing_fsx
    )
    loaded = HpcQueueProfilesDAO.convert_from_db(db).default_job_params.fsx_lustre
    assert loaded.existing_fsx == profile.default_job_params.fsx_lustre.existing_fsx
    assert loaded.deployment_type == 'PERSISTENT_2'


def test_a_profile_created_now_stores_new_fsx_lustre_not_allowed(pbs_queue_profiles):
    service, _, table = pbs_queue_profiles
    service.create_queue_profile(profile_named('fresh', ['fresh']))
    (row,) = table.rows.values()
    assert row['allow_new_fsx_lustre'] is False


def test_a_profile_created_with_new_fsx_lustre_allowed_keeps_it(pbs_queue_profiles):
    from ideadatamodel.scheduler import SocaQueueManagementParams

    service, _, table = pbs_queue_profiles
    profile = profile_named('scratch', ['scratch'])
    profile.queue_management_params = SocaQueueManagementParams(
        allow_new_fsx_lustre=True
    )
    service.create_queue_profile(profile)
    (row,) = table.rows.values()
    assert row['allow_new_fsx_lustre'] is True


@pytest.mark.parametrize('stored', [None, True, False])
def test_allow_new_fsx_lustre_round_trips_through_the_table(stored):
    from ideadatamodel.scheduler import SocaQueueManagementParams

    profile = HpcQueueProfile(
        name='q',
        queue_management_params=SocaQueueManagementParams(allow_new_fsx_lustre=stored),
    )
    db = HpcQueueProfilesDAO.convert_to_db(profile)
    # an absent value is not written, so an update without it never changes it
    assert ('allow_new_fsx_lustre' in db) is (stored is not None)
    loaded = HpcQueueProfilesDAO.convert_from_db(db).queue_management_params
    assert loaded.allow_new_fsx_lustre is stored
    assert loaded.allows_new_fsx_lustre() is (stored is not False)


EXISTING_FSX = 'fs-0123456789abcdef0.fsx.us-east-2.amazonaws.com'


def existing_fsx_profile(name: str) -> HpcQueueProfile:
    from ideadatamodel import SocaFSxLustreConfig

    profile = profile_named(name, [name])
    profile.default_job_params = SocaJobParams(
        instance_types=['c5.large'],
        base_os='amazonlinux2023',
        enable_scratch=True,
        scratch_provider='fsx-lustre-existing',
        fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx=EXISTING_FSX),
    )
    return profile


def job_fsx(context, queue_profile: HpcQueueProfile):
    from ideascheduler.app.scheduler import SocaJobBuilder

    builder = SocaJobBuilder(
        context=context,
        params={'nodes': 1, 'cpus': 1, 'instance_type': 'c5.large'},
        queue_profile=queue_profile,
    )
    assert builder.validate().is_valid()
    job_params, _ = builder.build()
    return job_params.fsx_lustre


def portal_update(service, queue_profile_id, change):
    """
    what the portal's Update Queue Profile sends: the stored profile as the API returned it
    (the wizard starts from it), with the form's changes, and null for what it clears
    """
    from ideadatamodel.scheduler import UpdateQueueProfileRequest

    stored = Utils.from_json(
        Utils.to_json(service.get_queue_profile(queue_profile_id=queue_profile_id))
    )
    change(stored)
    return UpdateQueueProfileRequest(**{'queue_profile': stored}).queue_profile


def test_switching_a_queue_from_existing_fsx_to_ebs_stops_mounting_it(
    context, pbs_queue_profiles
):
    service, _, table = pbs_queue_profiles
    profile = existing_fsx_profile('lustre')
    profile.default_job_params.walltime = '02:00:00'
    created = service.create_queue_profile(profile)
    assert (
        job_fsx(
            context,
            service.get_queue_profile(queue_profile_id=created.queue_profile_id),
        ).existing_fsx
        == EXISTING_FSX
    )

    def to_ebs(stored):
        params = stored['default_job_params']
        params['scratch_provider'] = 'ebs'
        params['scratch_storage_size'] = {'value': 10, 'unit': 'gb'}
        params['fsx_lustre'] = None  # clearQueueDefault(queueProfile, 'fsx_lustre')

    service.update_queue_profile(
        portal_update(service, created.queue_profile_id, to_ebs)
    )

    (row,) = table.rows.values()
    assert not [key for key in row if key.startswith('param_fsx_lustre')]
    # a default the form doesn't show, set earlier through the API, is kept
    assert row['param_walltime'] == '02:00:00'
    service.cache_clear(created)
    fsx = job_fsx(
        context, service.get_queue_profile(queue_profile_id=created.queue_profile_id)
    )
    assert fsx.enabled is False
    assert fsx.existing_fsx is None


def test_an_api_update_with_part_of_the_defaults_keeps_the_rest(
    context, pbs_queue_profiles
):
    from ideadatamodel.scheduler import UpdateQueueProfileRequest

    service, _, table = pbs_queue_profiles
    profile = existing_fsx_profile('partial')
    profile.default_job_params.walltime = '02:00:00'
    created = service.create_queue_profile(profile)
    request = UpdateQueueProfileRequest(
        **{
            'queue_profile': {
                'queue_profile_id': created.queue_profile_id,
                'name': 'partial',
                'title': 'partial',
                'queues': ['partial'],
                'queue_mode': 'fifo',
                'scaling_mode': 'single-job',
                'projects': [{'project_id': 'project-1'}],
                'default_job_params': {
                    'instance_types': ['c5.large'],
                    'base_os': 'amazonlinux2023',
                },
            }
        }
    )
    service.update_queue_profile(request.queue_profile)
    (row,) = table.rows.values()
    assert row['param_fsx_lustre_existing_fsx'] == EXISTING_FSX
    assert row['param_walltime'] == '02:00:00'
    assert row['param_scratch_provider'] == 'fsx-lustre-existing'


def test_an_explicit_null_clears_one_fsx_setting(context, pbs_queue_profiles):
    service, _, table = pbs_queue_profiles
    created = service.create_queue_profile(existing_fsx_profile('newfsx'))

    def to_new_fsx(stored):
        params = stored['default_job_params']
        params['scratch_provider'] = 'fsx-lustre-new'
        params['fsx_lustre']['existing_fsx'] = None

    service.update_queue_profile(
        portal_update(service, created.queue_profile_id, to_new_fsx)
    )
    (row,) = table.rows.values()
    assert 'param_fsx_lustre_existing_fsx' not in row
    assert row['param_fsx_lustre_enabled'] is True


def test_scratch_turned_off_in_the_portal_clears_every_stored_fsx_setting(
    context, pbs_queue_profiles
):
    # the portal's scratch-off payload (hpc-utils.ts prepareQueueProfileForSave): kept, a
    # job asking fsx_lustre=true on this queue got the old settings as new-FSx defaults
    service, _, table = pbs_queue_profiles
    created = service.create_queue_profile(existing_fsx_profile('off'))

    def scratch_off(stored):
        params = stored['default_job_params']
        params['enable_scratch'] = False
        params['scratch_provider'] = None
        params['fsx_lustre'] = {
            'enabled': False,
            'existing_fsx': None,
            's3_backend': None,
            'import_path': None,
            'export_path': None,
            'deployment_type': None,
            'per_unit_throughput': None,
            'size': None,
        }

    service.update_queue_profile(
        portal_update(service, created.queue_profile_id, scratch_off)
    )
    (row,) = table.rows.values()
    fsx_keys = {k: v for k, v in row.items() if k.startswith('param_fsx_lustre_')}
    assert fsx_keys == {'param_fsx_lustre_enabled': False}


def test_after_scratch_is_turned_off_the_form_loads_empty_fsx_fields(
    context, pbs_queue_profiles
):
    # rereview3 S3: re-enabling scratch starts from empty fields, as the form says
    service, _, _ = pbs_queue_profiles
    created = service.create_queue_profile(existing_fsx_profile('reon'))

    def scratch_off(stored):
        params = stored['default_job_params']
        params['enable_scratch'] = False
        params['scratch_provider'] = None
        params['fsx_lustre'] = {
            'enabled': False,
            'existing_fsx': None,
            's3_backend': None,
            'import_path': None,
            'export_path': None,
            'deployment_type': None,
            'per_unit_throughput': None,
            'size': None,
        }

    service.update_queue_profile(
        portal_update(service, created.queue_profile_id, scratch_off)
    )
    service.cache_clear(created)
    fsx = service.get_queue_profile(
        queue_profile_id=created.queue_profile_id
    ).default_job_params.fsx_lustre
    assert fsx.enabled is False
    assert (
        fsx.existing_fsx,
        fsx.s3_backend,
        fsx.import_path,
        fsx.export_path,
        fsx.deployment_type,
        fsx.per_unit_throughput,
        fsx.size,
    ) == (None,) * 7


def test_an_unrelated_edit_of_an_api_written_scratch_off_profile_keeps_its_fsx(
    context, pbs_queue_profiles
):
    # rereview3 S3, server half: the portal now sends the stored values back unchanged
    service, _, table = pbs_queue_profiles
    profile = existing_fsx_profile('apiwritten')
    profile.default_job_params.enable_scratch = None
    profile.default_job_params.scratch_provider = None
    created = service.create_queue_profile(profile)

    def walltime_only(stored):
        stored['default_job_params']['walltime'] = '03:00:00'

    service.update_queue_profile(
        portal_update(service, created.queue_profile_id, walltime_only)
    )
    (row,) = table.rows.values()
    assert row['param_fsx_lustre_existing_fsx'] == EXISTING_FSX
    assert row['param_fsx_lustre_enabled'] is True


def test_a_job_asking_for_fsx_on_a_queue_whose_fsx_default_is_off_gets_no_stored_values(
    context, pbs_queue_profiles
):
    # rereview3 S3: settings stored under a turned-off FSx default (an earlier portal
    # save kept them) were silently applied to a job asking fsx_lustre=true
    from ideadatamodel import SocaFSxLustreConfig
    from ideadatamodel.scheduler import SocaQueueManagementParams
    from ideascheduler.app.scheduler import SocaJobBuilder

    profile = profile_named('fsxoff', ['fsxoff'])
    profile.queue_management_params = SocaQueueManagementParams(
        allow_new_fsx_lustre=True
    )
    profile.default_job_params = SocaJobParams(
        instance_types=['c5.large'],
        base_os='amazonlinux2023',
        enable_scratch=False,
        fsx_lustre=SocaFSxLustreConfig(
            enabled=False,
            s3_backend='s3://old-bucket',
            import_path='s3://old-bucket/in',
            deployment_type='persistent_1',
        ),
    )
    builder = SocaJobBuilder(
        context=context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 'c5.large',
            'fsx_lustre': 'true',
        },
        queue_profile=profile,
    )
    result = builder.validate()
    assert result.is_valid(), result
    job_params, _ = builder.build()
    fsx = job_params.fsx_lustre
    assert fsx.enabled is True
    assert fsx.s3_backend is None and fsx.import_path is None
    assert fsx.deployment_type != 'persistent_1'


def test_an_update_that_stores_enabled_false_stops_the_worker(pbs_queue_profiles):
    # SP-29: the profile was stored disabled while its provisioner kept running
    service, _, _ = pbs_queue_profiles
    created = service.create_queue_profile(profile_named('stopme', ['stopme']))
    stopped = []
    service._provisioners[created.queue_profile_id] = SocaAnyPayload(
        stop=lambda: stopped.append(created.queue_profile_id)
    )
    update = profile_named('stopme', ['stopme'])
    update.queue_profile_id = created.queue_profile_id
    update.enabled = False
    service.update_queue_profile(update)
    assert stopped == [created.queue_profile_id]
    assert created.queue_profile_id not in service._provisioners


def test_an_update_without_default_job_params_keeps_them(context, pbs_queue_profiles):
    service, _, table = pbs_queue_profiles
    created = service.create_queue_profile(existing_fsx_profile('keep'))
    update = existing_fsx_profile('keep')
    update.queue_profile_id = created.queue_profile_id
    update.default_job_params = None
    service.update_queue_profile(update)
    (row,) = table.rows.values()
    assert row['param_fsx_lustre_existing_fsx'] == EXISTING_FSX


def test_built_in_queue_profiles_keep_per_job_fsx(context, monkeypatch):
    from ideascheduler.app.scheduler_default_settings import SchedulerDefaultSettings

    created = []

    class Profiles:
        queue_profile_dao = SimpleNamespace(
            get_queue_profile_by_queue=lambda queue: None
        )

        def get_queue_profile(self, **_):
            raise exceptions.soca_exception(
                errorcodes.SCHEDULER_QUEUE_PROFILE_NOT_FOUND, 'not found'
            )

        def create_queue_profile(self, queue_profile):
            created.append(queue_profile)
            queue_profile.queue_profile_id = queue_profile.name
            return queue_profile

        def enable_queue_profile(self, **_):
            pass

    monkeypatch.setattr(context, 'queue_profiles', Profiles(), raising=False)
    monkeypatch.setattr(
        context,
        'projects_client',
        type(
            'P',
            (),
            {
                'get_default_project': lambda self: type(
                    'R', (), {'project': Project(project_id='default')}
                )()
            },
        )(),
        raising=False,
    )
    SchedulerDefaultSettings(context).create_queue_profiles()
    assert {p.name for p in created} == {'compute', 'job-shared', 'test'}
    assert all(p.queue_management_params.allow_new_fsx_lustre is True for p in created)


def test_walltime_and_keep_ebs_volumes_defaults_round_trip():
    profile = HpcQueueProfile(
        name='q',
        default_job_params=SocaJobParams(walltime='01:00:00', keep_ebs_volumes=True),
    )
    loaded = HpcQueueProfilesDAO.convert_from_db(
        HpcQueueProfilesDAO.convert_to_db(profile)
    ).default_job_params
    assert loaded.walltime == '01:00:00'
    assert loaded.keep_ebs_volumes is True


def test_an_api_profile_with_only_an_existing_fsx_loads_it(context):
    # the legacy API shape: fsx_lustre carried existing_fsx and no enabled flag
    loaded = HpcQueueProfilesDAO.convert_from_db(
        {'name': 'api', 'param_fsx_lustre_existing_fsx': EXISTING_FSX}
    )
    assert loaded.default_job_params.fsx_lustre.existing_fsx == EXISTING_FSX
    assert job_fsx(context, loaded).existing_fsx == EXISTING_FSX


def test_a_queue_of_another_profile_is_refused_on_create_and_update(pbs_queue_profiles):
    # SCH-26: two profiles could claim one PBS queue
    service, pbs, table = pbs_queue_profiles
    service.create_queue_profile(profile_named('first', ['shared-q']))
    with pytest.raises(exceptions.SocaException) as created:
        service.create_queue_profile(profile_named('second', ['shared-q']))
    assert 'already associated with queue profile: first' in created.value.message
    other = service.create_queue_profile(profile_named('third', ['own-q']))
    update = profile_named('third', ['own-q', 'shared-q'])
    update.queue_profile_id = other.queue_profile_id
    with pytest.raises(exceptions.SocaException) as updated:
        service.update_queue_profile(update)
    assert 'already associated with queue profile: first' in updated.value.message
    assert sorted(row['name'] for row in table.rows.values()) == ['first', 'third']


def test_a_built_in_profile_whose_queue_is_taken_is_skipped_and_start_goes_on(
    context, pbs_queue_profiles, monkeypatch, caplog
):
    # A-B1: compute was deleted and its queue normal reused; re-creating compute raised
    # and the scheduler did not start
    import logging

    from ideascheduler.app.scheduler_default_settings import SchedulerDefaultSettings

    service, _, table = pbs_queue_profiles
    service.create_queue_profile(profile_named('mine', ['normal']))
    monkeypatch.setattr(context, 'queue_profiles', service, raising=False)
    monkeypatch.setattr(
        context,
        'projects_client',
        SimpleNamespace(
            get_default_project=lambda: SimpleNamespace(
                project=Project(project_id='project-1')
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(service, 'enable_queue_profile', lambda **_: None)
    # the built-ins' instance types are not in the test catalogue; validation is not the
    # subject here
    monkeypatch.setattr(service, 'validate_and_sanitize_queue_profile', lambda _p: None)
    with caplog.at_level(logging.WARNING):
        SchedulerDefaultSettings(context).create_queue_profiles()
    names = sorted(row['name'] for row in table.rows.values())
    assert names == ['job-shared', 'mine', 'test']
    assert 'built-in queue profile compute is not re-created' in caplog.text


def test_a_profile_already_sharing_a_queue_stays_editable(pbs_queue_profiles):
    # A-B1: two stored profiles listing one queue refused every edit of either
    service, _, table = pbs_queue_profiles
    first = service.create_queue_profile(profile_named('first', ['shared-q']))
    second = service.create_queue_profile(profile_named('second', ['own-q']))
    # what an earlier release could store: both list shared-q
    table.rows[second.queue_profile_id]['queues'] = ['own-q', 'shared-q']
    service.cache_clear(second)
    update = profile_named('second', ['own-q', 'shared-q'])
    update.queue_profile_id = second.queue_profile_id
    update.description = 'edited'
    service.update_queue_profile(update)
    assert table.rows[second.queue_profile_id]['description'] == 'edited'
    assert first.queue_profile_id in table.rows


def test_resolving_a_stored_duplicate_queue_keeps_it_in_pbs_for_the_other_profile(
    pbs_queue_profiles,
):
    # S4: removing the duplicate from one profile deleted the PBS queue the other
    # profile still lists
    service, pbs, table = pbs_queue_profiles
    first = service.create_queue_profile(profile_named('first', ['shared-q']))
    second = service.create_queue_profile(profile_named('second', ['own-q']))
    table.rows[second.queue_profile_id]['queues'] = ['own-q', 'shared-q']
    service.cache_clear(second)
    update = profile_named('second', ['own-q'])
    update.queue_profile_id = second.queue_profile_id
    service.update_queue_profile(update)
    assert table.rows[second.queue_profile_id]['queues'] == ['own-q']
    assert table.rows[first.queue_profile_id]['queues'] == ['shared-q']
    assert 'shared-q' in pbs.queues
    assert not any('delete queue shared-q' in command for command in pbs.commands)

    # deleting the second profile entirely keeps it too; deleting the last lister does not
    table.rows[second.queue_profile_id]['queues'] = ['own-q', 'shared-q']
    service.cache_clear(second)
    service.delete_queue_profile(queue_profile_id=second.queue_profile_id)
    assert 'shared-q' in pbs.queues and 'own-q' not in pbs.queues
    service.delete_queue_profile(queue_profile_id=first.queue_profile_id)
    assert 'shared-q' not in pbs.queues


def test_a_profile_with_several_default_subnets_and_a_new_fsx_default_saves(
    pbs_queue_profiles,
):
    # A-S4: the job-level one-subnet rule refused every save of such a stored profile
    from ideadatamodel import SocaFSxLustreConfig

    service, _, table = pbs_queue_profiles
    profile = profile_named('scratch-fsx', ['scratch-fsx'])
    subnets = service.context.config().get_list('cluster.network.private_subnets')[:2]
    profile.default_job_params = SocaJobParams(
        instance_types=['c5.large'],
        base_os='amazonlinux2023',
        subnet_ids=subnets,
        enable_scratch=True,
        scratch_provider='fsx-lustre-new',
        fsx_lustre=SocaFSxLustreConfig(enabled=True),
    )
    created = service.create_queue_profile(profile)
    assert table.rows[created.queue_profile_id]['param_subnet_ids'] == subnets
