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
Test Cases for HpcQueueProfilesService queue list updates

Pins the invariant that create_queues() / delete_queues() / update_queue_profile()
write the resulting queue list to the queue profile - not just the queues named in
the request, and never a queue list the scheduler does not hold.
"""

from ideadatamodel import exceptions, errorcodes
from ideadatamodel.scheduler import HpcQueueProfile
from ideascheduler.app.provisioning.job_provisioning_queue.hpc_queue_profiles_dao import (
    HpcQueueProfilesDAO,
)
from ideascheduler.app.provisioning.job_provisioning_queue.hpc_queue_profiles_service import (
    HpcQueueProfilesService,
)

from typing import Dict, List
import pytest


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

    def update(db_queue_profile: Dict) -> Dict:
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
    an update carrying no queues is not a request to remove every queue
    """
    service = queue_profiles_service
    set_queue_profile(service, monkeypatch, ['normal', 'ansys'])
    request = update_request(service, monkeypatch, [])

    service.update_queue_profile(request)

    assert service.db_updates[0]['queues'] == ['normal', 'ansys']
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
