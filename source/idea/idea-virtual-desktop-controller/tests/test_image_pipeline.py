"""
Desktop image pipeline: every row transition (queued -> resolving -> building -> checking
-> test_launching -> promoting -> current, and failed / waiting_capacity / pinned /
unsupported), resume after a restart, capacity backoff, waves, the promote gate, cleanup
protection, the release and monthly triggers, rollback hold, pins and RefreshImages.
EC2, the builder and the test launch are stubs; nothing here talks to AWS.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from ideadatamodel import (
    ImageBuildRecord,
    ImageCheck,
    ImageRefreshSchedule,
    ImageRowFilter,
    ImageRowKey,
    RefreshImagesRequest,
    SocaListingPayload,
    VirtualDesktopGPU,
    VirtualDesktopSoftwareStack,
    exceptions,
)
from ideasdk.aws.image_builds import ImageNotValidated
from ideavirtualdesktopcontroller.app.sessions.image_validation import CapacityWait
from ideavirtualdesktopcontroller.app.software_stacks import image_pipeline as module
from ideavirtualdesktopcontroller.app.software_stacks.image_pipeline import (
    BAKED_RELEASE_KEY,
    LAST_RUN_KEY,
    DesktopImagePipeline,
)
from image_fakes import FakeRecords

VERSION = '26.10.1'
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.db = Mock()

    def get_real_key(self, key, module_id=None):
        return key.replace('virtual-desktop-controller.', 'vdc.', 1)

    def get_config(self, key, default=None):
        prefix = self.get_real_key(key) + '.'
        found = {
            k[len(prefix) :]: v for k, v in self.values.items() if k.startswith(prefix)
        }
        return found or default

    def get_int(self, key, default=None):
        return self.values.get(self.get_real_key(key), default)

    def get_string(self, key, default=None, required=False):
        return self.values.get(self.get_real_key(key), default)

    def put(self, key, value):
        self.values[key] = value


class FakeEc2:
    def __init__(self):
        self.meta = Mock(region_name='us-east-2')
        self.images = {}
        self.instances = []
        self.deregistered = []
        self.deleted_snapshots = []
        self.terminated = []
        self.stopped = []

    def add_image(
        self, image_id, name='idea-dcv-host-rocky9-v1', snapshot=None, state='available'
    ):
        self.images[image_id] = {
            'ImageId': image_id,
            'Name': name,
            'State': state,
            'Architecture': 'x86_64',
            'BlockDeviceMappings': [
                {'Ebs': {'SnapshotId': snapshot or f'snap-{image_id}'}}
            ],
        }

    def describe_images(self, ImageIds=None, Owners=None, Filters=None):
        if ImageIds:
            return {'Images': [self.images[i] for i in ImageIds if i in self.images]}
        images = list(self.images.values())
        for f in Filters or []:
            if f['Name'] == 'name':
                pattern = f['Values'][0]
                images = [
                    i
                    for i in images
                    if (
                        i['Name'].startswith(pattern[:-1])
                        if pattern.endswith('*')
                        else i['Name'] == pattern
                    )
                ]
            if f['Name'] == 'block-device-mapping.snapshot-id':
                images = [
                    i
                    for i in images
                    if any(
                        m['Ebs']['SnapshotId'] in f['Values']
                        for m in i['BlockDeviceMappings']
                    )
                ]
        return {'Images': images}

    def describe_instances(self, Filters=None, InstanceIds=None):
        found = self.instances
        for f in Filters or []:
            if f['Name'] == 'image-id':
                found = [i for i in found if i.get('ImageId') in f['Values']]
        return {'Reservations': [{'Instances': found}]}

    def deregister_image(self, ImageId):
        self.deregistered.append(ImageId)
        self.images.pop(ImageId, None)

    def delete_snapshot(self, SnapshotId):
        self.deleted_snapshots.append(SnapshotId)

    def terminate_instances(self, InstanceIds):
        self.terminated.extend(InstanceIds)

    def stop_instances(self, InstanceIds):
        self.stopped.extend(InstanceIds)

    def create_tags(self, **kwargs):
        pass


class FakeStackDb:
    def __init__(self, stacks, config):
        self.stacks = {s.stack_id: s for s in stacks}
        self.config = config
        self.updated = []

    def get_base_software_stack_config(self):
        return self.config

    def list_all_from_db(self, request):
        return SocaListingPayload(
            listing=[s.model_copy(deep=True) for s in self.stacks.values()]
        )

    def get(self, stack_id, base_os):
        stack = self.stacks.get(stack_id)
        return stack.model_copy(deep=True) if stack else None

    def update(self, stack):
        self.stacks[stack.stack_id] = stack
        self.updated.append(stack.stack_id)
        return stack

    def repoint_image(self, stack, old_ami_id, ami_id, base_ami_id=None):
        stored = self.stacks.get(stack.stack_id)
        if stored is None or stored.image_pinned or stored.ami_id != old_ami_id:
            return None
        stored = stored.model_copy(deep=True)
        stored.ami_id = ami_id
        if base_ami_id:
            stored.base_ami_id = base_ami_id
        return self.update(stored)


class FakeTester:
    def __init__(self):
        self.result = [
            ImageCheck(name='ready_gate', ok=True, detail='READY in 200 s', seconds=200)
        ]
        self.launched = []
        self.reaped = 0

    def test_launch(self, record, base_stack, settings):
        self.launched.append((record.image_id, base_stack.stack_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def reap(self, settings, older_than):
        self.reaped += 1
        return []


def base_stack(
    base_os, arch='x86-64', ami='ami-old', gpu=None, suffix='base', pinned=None
):
    return VirtualDesktopSoftwareStack(
        stack_id=f'ss-base-{base_os}-{arch}-{suffix}',
        base_os=base_os,
        ami_id=ami,
        base_ami_id='ami-stockold',
        gpu=gpu or VirtualDesktopGPU.NO_GPU,
        image_pinned=pinned,
    )


class Harness:
    def __init__(self, stacks=None, config=None, settings=None):
        stacks = stacks if stacks is not None else [base_stack('rocky9')]
        base_config = {}
        for stack in stacks:
            if stack.stack_id.endswith('-base'):
                body = stack.stack_id[len('ss-base-') : -len('-base')]
                base_os, _, arch = body.partition('-')
                base_config.setdefault(base_os, {})[arch] = {}
        self.ec2 = FakeEc2()
        self.config = FakeConfig(
            {
                BAKED_RELEASE_KEY.replace(
                    'virtual-desktop-controller.', 'vdc.'
                ): VERSION,
                **(config or {}),
            }
        )
        for key, value in (settings or {}).items():
            self.config.values[f'vdc.software_stacks.image_pipeline.{key}'] = value
        context = Mock()
        context.config.return_value = self.config
        context.aws.return_value.ec2.return_value = self.ec2
        context.is_leader.return_value = True
        context.cluster_name.return_value = 'idea-test'
        context.module_id.return_value = 'vdc'
        context.cluster_timezone.return_value = 'America/Chicago'
        self.context = context
        self.stack_db = FakeStackDb(stacks, base_config)
        self.records = FakeRecords()
        self.tester = FakeTester()
        self.pipeline = DesktopImagePipeline(
            context,
            self.stack_db,
            Mock(),
            tester=self.tester,
            records=self.records,
            version=VERSION,
        )
        self.pipeline.host = 'leader'

    def row(self, base_os='rocky9', arch='x86_64', variant=None):
        return self.records.get(base_os, arch, variant)


class FakeBuilder:
    """the DcvHostImageBuilder handshake: before_snapshot with the tag, then 'complete' or a failure"""

    status = 'complete'
    image_id = 'ami-new'
    error = None
    made = []

    def __init__(
        self,
        context,
        base_ami,
        base_os,
        ami_name=None,
        instance_type=None,
        ebs_volume_size=None,
        force=False,
        before_snapshot=None,
        image_tags=None,
    ):
        self.before_snapshot = before_snapshot
        self.ami_name = ami_name
        FakeBuilder.made.append(
            {
                'base_ami': base_ami,
                'instance_type': instance_type,
                'ebs_volume_size': ebs_volume_size,
                'tags': image_tags,
                'ami_name': ami_name,
            }
        )

    def get_ami_full_name(self):
        return f'{self.ami_name}-v10022026-120000-abcd'

    def build(self, progress):
        if FakeBuilder.error:
            raise FakeBuilder.error
        progress({'instance_id': 'i-builder'})
        self.before_snapshot('i-builder', FakeBuilder.status)
        if FakeBuilder.status != 'complete':
            raise exceptions.general_exception(
                f'the in-bake check {FakeBuilder.status.split(":")[1]} failed'
            )
        return FakeBuilder.image_id


IN_BAKE = [ImageCheck(name='kernel_default', ok=True, detail='6.1', seconds=1)]


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    FakeBuilder.status, FakeBuilder.error, FakeBuilder.made = 'complete', None, []
    monkeypatch.setattr(module, 'DcvHostImageBuilder', FakeBuilder)
    monkeypatch.setattr(
        module,
        'read_in_bake_checks',
        lambda context, instance_id, windows: (VERSION, list(IN_BAKE)),
    )
    monkeypatch.setattr(
        module,
        'resolve_stock_image',
        lambda ec2, base_os, arch, logger: {
            'ImageId': f'ami-stock-{base_os}',
            'Architecture': arch,
        },
    )
    monkeypatch.setattr(module.time, 'sleep', lambda s: None)


def queue_and_run(h, key=None):
    request = (
        RefreshImagesRequest(rows=[key]) if key else RefreshImagesRequest(all=True)
    )
    h.pipeline.refresh(request, 'admin')
    h.ec2.add_image('ami-new', name='idea-dcv-host-rocky9-v10022026-120000-abcd')
    h.pipeline.tick(now=T0, blocking=True)


# transitions


def test_a_row_runs_every_step_and_promotes_a_validated_image():
    h = Harness(
        [
            base_stack('rocky9'),
            base_stack('rocky9', suffix='dcv'),
            VirtualDesktopSoftwareStack(
                stack_id='custom-1', base_os='rocky9', ami_id='ami-old'
            ),
        ]
    )
    seen = []
    real_save = h.pipeline._save

    def spy(record, **expected):
        seen.append(record.status)
        real_save(record, **expected)

    h.pipeline._save = spy
    queue_and_run(h)

    row = h.row()
    order = list(dict.fromkeys(seen))
    assert order == [
        'resolving',
        'building',
        'checking',
        'test_launching',
        'promoting',
        'current',
    ]
    assert row.status == 'current'
    # the image the row's stacks ran before (stock here) never validated: no rollback target
    assert (row.current_image_id, row.previous_image_id) == ('ami-new', None)
    assert row.source_ami == 'ami-stock-rocky9' and row.release == VERSION
    assert row.validated_on is not None and row.promoted_on is not None
    assert [c.name for c in row.checks] == ['kernel_default', 'ready_gate']
    # both ss-base stacks move; the custom stack never does
    assert sorted(h.stack_db.updated) == [
        'ss-base-rocky9-x86-64-base',
        'ss-base-rocky9-x86-64-dcv',
    ]
    assert h.stack_db.stacks['custom-1'].ami_id == 'ami-old'
    assert (
        h.stack_db.stacks['ss-base-rocky9-x86-64-base'].base_ami_id
        == 'ami-stock-rocky9'
    )
    assert FakeBuilder.made[0]['tags'] == {'idea:ImagePipeline': 'desktop'}


def test_a_second_promotion_keeps_the_previous_image():
    h = Harness()
    queue_and_run(h)
    FakeBuilder.image_id = 'ami-newer'
    try:
        h.ec2.add_image('ami-newer', name='idea-dcv-host-rocky9-v2')
        h.pipeline.refresh(RefreshImagesRequest(all=True, force=True), 'admin')
        h.pipeline.tick(now=T0, blocking=True)
    finally:
        FakeBuilder.image_id = 'ami-new'
    row = h.row()
    assert (row.current_image_id, row.previous_image_id) == ('ami-newer', 'ami-new')


def test_rollback_after_a_first_promotion_refuses_the_unvalidated_stock_image():
    h = Harness()
    queue_and_run(h)
    h.ec2.add_image('ami-old')
    with pytest.raises(exceptions.SocaException) as exc_info:
        h.pipeline.rollback(
            ImageRowKey(base_os='rocky9', architecture='x86_64'), 'admin'
        )
    assert 'no previous validated image' in exc_info.value.message
    assert h.row().current_image_id == 'ami-new'


def test_a_pin_set_between_the_read_and_the_write_keeps_the_stack():
    """an admin pins the stack after promotion read it: the stack keeps its pin and image"""
    h = Harness()
    real_get = h.stack_db.get

    def get_then_pin(stack_id, base_os):
        fresh = real_get(stack_id, base_os)
        if fresh is not None:
            h.stack_db.stacks[stack_id].image_pinned = True
        return fresh

    h.stack_db.get = get_then_pin
    queue_and_run(h)
    stack = h.stack_db.stacks['ss-base-rocky9-x86-64-base']
    assert stack.image_pinned is True and stack.ami_id == 'ami-old'
    assert h.stack_db.updated == []


def test_a_failed_in_bake_check_fails_the_row_with_that_check_and_keeps_the_old_image():
    h = Harness()
    FakeBuilder.status = 'failed:lustre_module'
    queue_and_run(h)
    row = h.row()
    assert row.status == 'failed'
    assert 'lustre_module' in row.error
    assert row.current_image_id == 'ami-old'
    assert h.stack_db.updated == []
    assert h.tester.launched == []


def test_any_failing_in_bake_result_fails_even_with_a_complete_tag(monkeypatch):
    h = Harness()
    monkeypatch.setattr(
        module,
        'read_in_bake_checks',
        lambda *a: (
            VERSION,
            [
                ImageCheck(
                    name='dcv_installed',
                    ok=False,
                    detail='dcvserver missing',
                    seconds=0,
                )
            ],
        ),
    )
    queue_and_run(h)
    assert h.row().status == 'failed'
    assert 'dcv_installed' in h.row().error


def test_a_stale_bootstrap_release_fails_the_bake(monkeypatch):
    h = Harness()
    monkeypatch.setattr(
        module, 'read_in_bake_checks', lambda *a: ('26.10.0', list(IN_BAKE))
    )
    queue_and_run(h)
    assert 'the 26.10.0 bootstrap' in h.row().error


def test_unreadable_in_bake_results_fail_the_bake(monkeypatch):
    h = Harness()

    def boom(*a):
        raise RuntimeError('InvocationDoesNotExist')

    monkeypatch.setattr(module, 'read_in_bake_checks', boom)
    queue_and_run(h)
    assert h.row().status == 'failed'
    assert 'unreadable' in h.row().error


def test_a_failed_test_launch_blocks_promotion():
    h = Harness()
    h.tester.result = [
        ImageCheck(
            name='ready_gate', ok=False, detail='not READY within 300 s', seconds=300
        )
    ]
    queue_and_run(h)
    row = h.row()
    assert row.status == 'failed'
    assert row.error.startswith('ready_gate: not READY within 300 s')
    assert row.validated_on is None
    assert row.current_image_id == 'ami-old'
    assert h.stack_db.updated == []


def test_govcloud_rocky_rows_are_unsupported_and_windows_rows_bake():
    h = Harness([base_stack('rocky9'), base_stack('windows2022')])
    h.ec2.meta = Mock(region_name='us-gov-west-1')
    results = {
        r.row.base_os: r
        for r in h.pipeline.refresh(RefreshImagesRequest(all=True), 'admin')
    }
    assert results['windows2022'].outcome == 'queued'
    assert results['rocky9'].outcome == 'unsupported'
    assert 'GovCloud' in h.row().error


# the promote gate on the pipeline path


def test_promotion_refuses_a_candidate_that_never_validated():
    h = Harness()
    record = ImageBuildRecord(
        base_os='rocky9',
        architecture='x86_64',
        status='promoting',
        image_id='ami-new',
        host='leader',
        started_on=T0,
    )
    h.records.put(record)
    with pytest.raises(ImageNotValidated):
        h.pipeline._promote(h.row())
    h.pipeline.run(h.row())
    assert h.row().status == 'failed'
    assert h.stack_db.updated == []


# capacity and waves


def test_no_capacity_waits_and_retries_from_the_step_it_reached():
    h = Harness()
    h.tester.result = CapacityWait(
        'InsufficientInstanceCapacity: no g4dn in us-east-2a'
    )
    queue_and_run(h)
    row = h.row()
    assert row.status == 'waiting_capacity'
    assert row.retry_after > T0
    assert row.attempts == 1
    assert 'InsufficientInstanceCapacity' in row.error

    h.tester.result = [ImageCheck(name='ready_gate', ok=True, detail='ok', seconds=1)]
    h.pipeline.tick(now=row.retry_after - timedelta(seconds=1), blocking=True)
    assert h.row().status == 'waiting_capacity'
    built = len(FakeBuilder.made)
    h.pipeline.tick(now=row.retry_after + timedelta(seconds=1), blocking=True)
    assert h.row().status == 'current'
    assert len(FakeBuilder.made) == built  # resumed at the test launch, no rebake


def test_a_builder_capacity_refusal_waits_too():
    h = Harness()
    FakeBuilder.error = RuntimeError(
        'An error occurred (VcpuLimitExceeded) when calling RunInstances'
    )
    queue_and_run(h)
    assert h.row().status == 'waiting_capacity'
    assert h.row().image_id is None


def test_capacity_gives_up_after_the_last_backoff():
    h = Harness()
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='test_launching',
            image_id='ami-new',
            host='leader',
            attempts=4,
        )
    )
    h.tester.result = CapacityWait('InsufficientInstanceCapacity')
    h.ec2.add_image('ami-new')
    h.pipeline.run(h.row())
    assert h.row().status == 'failed'
    assert 'no EC2 capacity after 5 tries' in h.row().error


def test_queued_rows_start_in_waves_of_max_concurrent_bakes(monkeypatch):
    stacks = [
        base_stack(os) for os in ('rocky8', 'rocky9', 'rhel8', 'rhel9', 'ubuntu2204')
    ]
    h = Harness(stacks, settings={'max_concurrent_bakes': 2})
    started = []
    monkeypatch.setattr(
        h.pipeline,
        '_start',
        lambda record, blocking: started.append(record.base_os) or True,
    )
    results = h.pipeline.refresh(RefreshImagesRequest(all=True), 'admin')
    assert {r.outcome for r in results} == {'queued'}
    h.pipeline.tick(now=T0)
    assert len(started) == 2
    # two rows are mid-bake: the next tick starts nothing more
    for base_os in started:
        record = h.row(base_os)
        record.status = 'building'
        h.records.put(record)
    monkeypatch.setattr(h.pipeline, '_alive', lambda record: True)
    started.clear()
    h.pipeline.tick(now=T0)
    assert started == []


# resume after a restart


def test_a_restart_with_the_candidate_built_resumes_at_the_test_launch():
    h = Harness()
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='building',
            host='dead-task',
            ami_name='idea-dcv-host-rocky9-v1',
            instance_id='i-builder',
            started_on=T0,
            release=VERSION,
        )
    )
    h.ec2.add_image('ami-built', name='idea-dcv-host-rocky9-v1')
    h.pipeline._start = lambda record, blocking: True
    h.pipeline.tick(now=T0)
    row = h.row()
    assert row.status == 'test_launching'
    assert row.image_id == 'ami-built'
    assert h.ec2.stopped == []


def test_a_resumed_row_terminates_its_builder_once_the_image_is_available():
    # the bake that would have terminated it died with the old controller
    h = Harness()
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='checking',
            host='dead-task',
            ami_name='idea-dcv-host-rocky9-v1',
            instance_id='i-builder',
            started_on=T0,
            release=VERSION,
        )
    )
    h.ec2.add_image('ami-built', name='idea-dcv-host-rocky9-v1')
    h.pipeline.tick(now=T0, blocking=True)
    assert h.row().status == 'current'
    assert h.ec2.terminated == ['i-builder']
    assert h.ec2.stopped == []


def test_a_restart_before_the_snapshot_stops_the_builder_and_retries_once():
    h = Harness()
    h.pipeline._start = lambda record, blocking: True
    for attempt, expected in ((None, 'queued'), (1, 'failed')):
        h.records.put(
            ImageBuildRecord(
                base_os='rocky9',
                architecture='x86_64',
                status='checking',
                host='dead-task',
                ami_name='idea-dcv-host-rocky9-v1',
                instance_id='i-builder',
                attempts=attempt,
                release=VERSION,
            )
        )
        h.pipeline.tick(now=T0)
        assert h.row().status == expected
    assert h.ec2.stopped == ['i-builder', 'i-builder']


def test_a_row_taken_over_by_another_controller_stops_its_old_thread():
    h = Harness()
    record = ImageBuildRecord(
        base_os='rocky9',
        architecture='x86_64',
        status='test_launching',
        image_id='ami-new',
        host='leader',
    )
    h.records.put(record)
    taken = h.row()
    taken.host = 'new-leader'
    h.records.put(taken)
    h.ec2.add_image('ami-new')
    h.pipeline.run(record)
    assert h.row().host == 'new-leader'
    assert h.row().status == 'test_launching'
    assert h.stack_db.updated == []


def test_the_sdk_sweep_keeps_in_flight_rows_for_the_leader():
    from ideasdk.aws.image_builds import ImageBuildRecordsDB
    import socket

    db = ImageBuildRecordsDB(
        Mock(), 'idea-test.vdc.controller.image-builds', kind='desktop'
    )
    db.list_all = lambda: [
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='building',
            host=socket.gethostname(),
        )
    ]
    db.put = Mock()
    assert db.sweep_orphans(Mock()) == ['rocky9/x86_64']
    db.put.assert_not_called()


# cleanup


def test_cleanup_keeps_current_previous_references_and_bakes_in_flight():
    h = Harness(
        [
            base_stack('rocky9', ami='ami-current'),
            VirtualDesktopSoftwareStack(
                stack_id='custom-1', base_os='rocky9', ami_id='ami-custom-ref'
            ),
        ]
    )
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='current',
            current_image_id='ami-current',
            previous_image_id='ami-previous',
            promoted_on=T0,
        )
    )
    h.records.put(
        ImageBuildRecord(
            base_os='rocky8',
            architecture='x86_64',
            status='building',
            ami_name='idea-dcv-host-rocky8-v9',
        )
    )
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64#custom',
            status='complete',
            image_id='ami-custom-build',
        )
    )
    for image_id in (
        'ami-current',
        'ami-previous',
        'ami-custom-ref',
        'ami-custom-build',
        'ami-old1',
        'ami-running',
    ):
        h.ec2.add_image(image_id, name=f'idea-dcv-host-rocky9-{image_id}')
    h.ec2.add_image('ami-baking', name='idea-dcv-host-rocky8-v9')
    h.ec2.add_image(
        'ami-shared', name='idea-dcv-host-rocky9-shared', snapshot='snap-shared'
    )
    h.ec2.add_image(
        'ami-shared2', name='idea-dcv-host-rocky9-shared2', snapshot='snap-shared'
    )
    h.ec2.instances = [{'InstanceId': 'i-desktop', 'ImageId': 'ami-running'}]
    h.records.put(
        ImageBuildRecord(
            base_os='rhel9',
            architecture='x86_64',
            status='current',
            current_image_id='ami-shared2',
            promoted_on=T0,
        )
    )

    removed = h.pipeline._deregister_unreferenced()

    assert sorted(removed) == ['ami-old1', 'ami-shared']
    assert h.ec2.deleted_snapshots == [
        'snap-ami-old1'
    ]  # snap-shared is still ami-shared2's


def test_cleanup_reaps_leftover_builders_but_not_a_running_bake():
    h = Harness()
    old = T0 - timedelta(hours=3)
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='building',
            instance_id='i-busy',
        )
    )
    h.ec2.instances = [
        {'InstanceId': 'i-busy', 'LaunchTime': old},
        {'InstanceId': 'i-left', 'LaunchTime': old},
        {'InstanceId': 'i-young', 'LaunchTime': datetime.now(tz=timezone.utc)},
    ]
    assert h.pipeline._reap_builders() == ['i-left']


# triggers


def test_the_release_trigger_queues_rows_from_older_releases_and_settles_once_all_are_terminal():
    h = Harness(
        [base_stack('rocky9'), base_stack('rocky8'), base_stack('rhel9')],
        config={'vdc.software_stacks.images_baked_release': '26.10.0'},
    )
    h.records.put(
        ImageBuildRecord(
            base_os='rocky8', architecture='x86_64', status='current', release=VERSION
        )
    )
    h.records.put(
        ImageBuildRecord(
            base_os='rhel9',
            architecture='x86_64',
            status='current',
            release='26.10.0',
            pinned=True,
        )
    )
    h.pipeline._start = lambda record, blocking: True

    h.pipeline.tick(now=T0)

    assert h.row('rocky9').status == 'queued' and h.row('rocky9').trigger == 'release'
    assert h.row('rocky8').status == 'current'
    assert h.row('rhel9').status == 'current'
    assert h.config.values['vdc.software_stacks.images_baked_release'] == '26.10.0'

    record = h.row('rocky9')
    record.status = 'failed'
    h.records.put(record)
    h.pipeline.tick(now=T0)
    assert h.config.values['vdc.software_stacks.images_baked_release'] == VERSION
    assert h.row('rocky9').status == 'failed'  # a failure is not retried by the trigger


def test_the_schedule_runs_on_the_first_sunday_at_two_in_cluster_time():
    tz = ZoneInfo('America/Chicago')
    schedule = ImageRefreshSchedule()
    assert schedule.next_run_after(datetime(2026, 10, 2, tzinfo=tz)) == datetime(
        2026, 10, 4, 2, tzinfo=tz
    )
    assert schedule.next_run_after(datetime(2026, 10, 4, 2, tzinfo=tz)) == datetime(
        2026, 11, 1, 2, tzinfo=tz
    )
    assert (
        ImageRefreshSchedule(enabled=False).next_run_after(
            datetime(2026, 10, 2, tzinfo=tz)
        )
        is None
    )


def monthly_harness(last_run):
    h = Harness(
        [base_stack('rocky9'), base_stack('rocky8')],
        config={
            LAST_RUN_KEY.replace('virtual-desktop-controller.', 'vdc.'): int(
                last_run.timestamp() * 1000
            )
        },
    )
    for base_os in ('rocky9', 'rocky8'):
        h.records.put(
            ImageBuildRecord(
                base_os=base_os,
                architecture='x86_64',
                status='current',
                release=VERSION,
                source_ami=f'ami-stock-{base_os}',
                current_image_id='ami-x',
                promoted_on=T0,
            )
        )
    h.pipeline._start = lambda record, blocking: True
    return h


def test_the_monthly_check_rebakes_only_rows_whose_vendor_base_moved(monkeypatch):
    tz = ZoneInfo('America/Chicago')
    h = monthly_harness(datetime(2026, 9, 6, 2, 5, tzinfo=tz))
    monkeypatch.setattr(
        module,
        'resolve_stock_image',
        lambda ec2, base_os, arch, logger: {
            'ImageId': 'ami-stock-rocky9'
            if base_os == 'rocky9'
            else 'ami-stock-rocky8-new'
        },
    )
    before = datetime(2026, 10, 4, 1, 59, tzinfo=tz)
    h.pipeline.tick(now=before.astimezone(timezone.utc))
    assert h.row('rocky8').status == 'current'  # not due yet (01:59 cluster time)

    due = datetime(2026, 10, 4, 2, 1, tzinfo=tz)
    h.pipeline.tick(now=due.astimezone(timezone.utc))
    assert h.row('rocky8').status == 'queued' and h.row('rocky8').trigger == 'monthly'
    assert h.row('rocky9').status == 'current'
    assert h.config.values['vdc.software_stacks.image_refresh_last_run_on'] == int(
        due.timestamp() * 1000
    )


def test_the_monthly_check_runs_once_when_the_settings_copy_lags(monkeypatch):
    tz = ZoneInfo('America/Chicago')
    h = monthly_harness(datetime(2026, 9, 6, 2, 5, tzinfo=tz))
    stored = {'value': h.config.values['vdc.software_stacks.image_refresh_last_run_on']}

    def set_if(key, value, expected):
        if stored['value'] != expected:
            return False
        stored['value'] = value
        return True

    h.config.db.set_config_entry_if.side_effect = set_if
    h.config.put = lambda key, value: None  # this copy never sees the write
    checks = []
    monkeypatch.setattr(
        module,
        'resolve_stock_image',
        lambda ec2, base_os, arch, logger: checks.append(base_os)
        or {'ImageId': f'ami-stock-{base_os}'},
    )
    due = datetime(2026, 10, 4, 2, 1, tzinfo=tz).astimezone(timezone.utc)
    h.pipeline.tick(now=due)
    h.pipeline.tick(now=due + timedelta(seconds=15))
    assert sorted(checks) == ['rocky8', 'rocky9']


def test_a_quiet_monthly_check_bakes_nothing():
    tz = ZoneInfo('America/Chicago')
    h = monthly_harness(datetime(2026, 9, 6, 2, 5, tzinfo=tz))
    h.pipeline.tick(now=datetime(2026, 10, 4, 3, tzinfo=tz).astimezone(timezone.utc))
    assert {h.row(o).status for o in ('rocky9', 'rocky8')} == {'current'}


def test_the_first_tick_only_starts_the_schedule_clock():
    h = Harness()
    h.pipeline._start = lambda record, blocking: True
    h.pipeline.tick(now=T0)
    assert h.config.values['vdc.software_stacks.image_refresh_last_run_on'] == int(
        T0.timestamp() * 1000
    )
    assert h.row() is None


# rollback, hold and pins


def promoted_harness():
    h = Harness(
        [
            base_stack('rocky9', ami='ami-cur'),
            base_stack('rocky9', suffix='pinned', ami='ami-cur', pinned=True),
        ]
    )
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='current',
            release=VERSION,
            current_image_id='ami-cur',
            previous_image_id='ami-prev',
            promoted_on=T0,
        )
    )
    h.ec2.add_image('ami-prev')
    return h


def test_rollback_flips_to_the_previous_image_and_holds_automatic_promotion():
    h = promoted_harness()
    record = h.pipeline.rollback(
        ImageRowKey(base_os='rocky9', architecture='x86_64'), 'admin'
    )
    assert (record.current_image_id, record.previous_image_id) == (
        'ami-prev',
        'ami-cur',
    )
    assert record.rollback_hold is True
    assert h.stack_db.stacks['ss-base-rocky9-x86-64-base'].ami_id == 'ami-prev'
    assert h.stack_db.stacks['ss-base-rocky9-x86-64-pinned'].ami_id == 'ami-cur'

    # the release and monthly triggers skip a held row
    h.config.values['vdc.software_stacks.images_baked_release'] = '26.10.0'
    h.pipeline._start = lambda r, blocking: True
    h.pipeline.tick(now=T0)
    assert h.row().status == 'current'


def test_a_held_row_validated_automatically_is_not_promoted_but_a_button_refresh_clears_the_hold():
    h = promoted_harness()
    h.pipeline.rollback(ImageRowKey(base_os='rocky9', architecture='x86_64'), 'admin')
    held = h.row()
    held.status, held.trigger, held.image_id, held.host = (
        'promoting',
        'monthly',
        'ami-new',
        'leader',
    )
    held.started_on, held.validated_on = T0, T0 + timedelta(hours=1)
    h.records.put(held)
    h.pipeline.run(h.row())
    assert h.row().current_image_id == 'ami-prev'
    assert 'rollback hold' in h.row().error

    queue_and_run(h)
    assert h.row().current_image_id == 'ami-new'
    assert h.row().rollback_hold is False


def test_rollback_needs_a_previous_image_and_a_settled_row():
    h = Harness()
    h.records.put(
        ImageBuildRecord(base_os='rocky9', architecture='x86_64', status='building')
    )
    with pytest.raises(exceptions.SocaException):
        h.pipeline.rollback(
            ImageRowKey(base_os='rocky9', architecture='x86_64'), 'admin'
        )
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='current',
            current_image_id='ami-cur',
        )
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        h.pipeline.rollback(
            ImageRowKey(base_os='rocky9', architecture='x86_64'), 'admin'
        )
    assert 'no previous validated image' in exc_info.value.message


def test_a_pinned_row_is_never_baked_or_promoted():
    h = Harness()
    record = h.pipeline.set_pinned(
        ImageRowKey(base_os='rocky9', architecture='x86_64'), True
    )
    assert record.status == 'pinned'
    results = h.pipeline.refresh(RefreshImagesRequest(all=True), 'admin')
    assert results[0].outcome == 'pinned'
    unpinned = h.pipeline.set_pinned(
        ImageRowKey(base_os='rocky9', architecture='x86_64'), False
    )
    assert unpinned.pinned is False


def test_a_pin_set_mid_bake_stops_the_promotion():
    h = Harness()
    h.pipeline.refresh(RefreshImagesRequest(all=True), 'admin')
    real = h.tester.test_launch

    def pin_then_launch(record, base, settings):
        h.pipeline.set_pinned(
            ImageRowKey(base_os='rocky9', architecture='x86_64'), True
        )
        return real(record, base, settings)

    h.tester.test_launch = pin_then_launch
    h.ec2.add_image('ami-new')
    h.pipeline.tick(now=T0, blocking=True)
    row = h.row()
    assert row.status == 'pinned' and row.pinned is True
    assert row.current_image_id == 'ami-old'
    assert h.stack_db.updated == []


# RefreshImages


def test_refresh_takes_exactly_one_selector_and_reports_each_row():
    h = Harness([base_stack('rocky9'), base_stack('ubuntu2204')])
    with pytest.raises(exceptions.SocaException):
        h.pipeline.refresh(RefreshImagesRequest(), 'admin')
    with pytest.raises(exceptions.SocaException):
        h.pipeline.refresh(
            RefreshImagesRequest(all=True, filter=ImageRowFilter()), 'admin'
        )

    results = h.pipeline.refresh(
        RefreshImagesRequest(
            rows=[
                ImageRowKey(base_os='rocky9', architecture='x86_64'),
                ImageRowKey(base_os='ghost', architecture='x86_64'),
            ]
        ),
        'admin',
    )
    assert [(r.row.base_os, r.outcome) for r in results] == [
        ('ghost', 'not_found'),
        ('rocky9', 'queued'),
    ]
    again = h.pipeline.refresh(
        RefreshImagesRequest(filter=ImageRowFilter(base_os_family='rocky')), 'admin'
    )
    assert [(r.row.base_os, r.outcome) for r in again] == [('rocky9', 'in_flight')]
    with pytest.raises(exceptions.SocaException):
        h.pipeline.refresh(
            RefreshImagesRequest(filter=ImageRowFilter(kind='compute')), 'admin'
        )


def test_gpu_rows_exist_only_where_a_gpu_stack_does():
    h = Harness([base_stack('rocky9'), base_stack('ubuntu2204')])
    assert [(r.base_os, r.variant) for r in h.pipeline.list_rows()] == [
        ('rocky9', 'cpu'),
        ('ubuntu2204', 'cpu'),
    ]
    h.stack_db.stacks['gpu-custom'] = VirtualDesktopSoftwareStack(
        stack_id='gpu-custom',
        base_os='rocky9',
        architecture='x86_64',
        gpu=VirtualDesktopGPU.NVIDIA,
        ami_id='ami-g',
    )
    rows = h.pipeline.list_rows(ImageRowFilter(variant='nvidia'))
    assert [(r.base_os, r.architecture, r.variant) for r in rows] == [
        ('rocky9', 'x86_64', 'nvidia')
    ]


def test_a_gpu_row_bakes_on_a_gpu_builder_under_its_own_key():
    h = Harness(
        [
            base_stack('rocky9'),
            base_stack('rocky9', suffix='gpu', gpu=VirtualDesktopGPU.NVIDIA),
        ]
    )
    queue_and_run(
        h, ImageRowKey(base_os='rocky9', architecture='x86_64', variant='nvidia')
    )
    gpu = h.row(variant='nvidia')
    assert gpu.status == 'current'
    assert FakeBuilder.made[0]['instance_type'] == 'g4dn.xlarge'
    assert FakeBuilder.made[0]['ami_name'] == 'idea-dcv-host-rocky9-nvidia'
    assert h.stack_db.updated == ['ss-base-rocky9-x86-64-gpu']
    assert h.row() is None  # the CPU row is untouched


def test_the_builder_gets_the_base_stack_root_size_and_the_row_links_its_log_stream():
    from ideadatamodel import SocaMemory, SocaMemoryUnit

    stack = base_stack('rocky9')
    stack.min_storage = SocaMemory(value=20, unit=SocaMemoryUnit.GB)
    h = Harness(stacks=[stack])
    queue_and_run(h)
    # the bake has the room a desktop launched from it has
    assert FakeBuilder.made[0]['ebs_volume_size'] == 20
    # the page links the bootstrap_<instance id> stream, console-escaped
    assert h.row().log_link.endswith(
        '#logsV2:log-groups/log-group/$252Fidea-test$252Fvdc$252Fami-builder/log-events/bootstrap_i-builder'
    )
    assert h.row().log_link.startswith('https://us-east-2.console.aws.amazon.com/')


def test_a_stack_without_a_size_leaves_the_builder_default():
    h = Harness()
    queue_and_run(h)
    assert FakeBuilder.made[0]['ebs_volume_size'] is None


def test_a_failed_in_bake_check_reports_its_detail_and_builder(monkeypatch):
    h = Harness()
    FakeBuilder.status = 'failed:bootstrap'
    monkeypatch.setattr(
        module,
        'read_in_bake_checks',
        lambda *a: (
            VERSION,
            [
                ImageCheck(
                    name='bootstrap',
                    ok=False,
                    detail='a command failed: setup: make rpm (exit 2)',
                    seconds=0,
                )
            ],
        ),
    )
    queue_and_run(h)
    assert h.row().status == 'failed'
    assert h.row().error == (
        'in-bake check bootstrap failed on builder i-builder: a command failed: setup: make rpm (exit 2)'
    )


def test_the_stack_table_repoint_is_conditional_on_the_old_image_and_no_pin():
    from unittest.mock import MagicMock

    from botocore.exceptions import ClientError
    from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
        VirtualDesktopSoftwareStackDB,
    )

    db = VirtualDesktopSoftwareStackDB.__new__(VirtualDesktopSoftwareStackDB)
    db._table_obj = MagicMock()
    db.trigger_update_event = MagicMock()
    db.convert_db_dict_to_software_stack_object = lambda entry: entry
    stack = base_stack('rocky9', ami='ami-old')
    db._table_obj.update_item.return_value = {'Attributes': {'ami_id': 'ami-new'}}
    assert db.repoint_image(stack, 'ami-old', 'ami-new', 'ami-src') == {
        'ami_id': 'ami-new'
    }
    call = db._table_obj.update_item.call_args.kwargs
    assert 'image_pinned' in call['ExpressionAttributeNames'].values()
    assert call['ExpressionAttributeValues'][':old'] == 'ami-old'
    assert 'projects' not in str(call['UpdateExpression'])
    db._table_obj.update_item.side_effect = ClientError(
        {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
    )
    assert db.repoint_image(stack, 'ami-old', 'ami-new') is None


# once a day


def baked_at(h, when, status='current'):
    h.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status=status,
            release=VERSION,
            source_ami='ami-stock-rocky9',
            current_image_id='ami-x',
            started_on=when,
        )
    )


@pytest.mark.parametrize('status', ['current', 'failed'])
def test_a_row_baked_today_is_skipped_until_tomorrow_unless_forced(monkeypatch, status):
    chicago = ZoneInfo('America/Chicago')
    h = Harness()
    baked_at(h, datetime(2026, 10, 3, 0, 30, tzinfo=chicago), status)
    monkeypatch.setattr(
        module, 'now_utc', lambda: datetime(2026, 10, 3, 23, 50, tzinfo=chicago)
    )
    result = h.pipeline.refresh(RefreshImagesRequest(all=True), 'admin')[0]
    assert (result.outcome, h.row().status) == ('baked_today', status)
    assert 'Force rebake' in result.message

    forced = h.pipeline.refresh(RefreshImagesRequest(all=True, force=True), 'admin')
    assert forced[0].outcome == 'queued' and h.row().status == 'queued'

    h = Harness()
    baked_at(h, datetime(2026, 10, 3, 0, 30, tzinfo=chicago), status)
    monkeypatch.setattr(
        module, 'now_utc', lambda: datetime(2026, 10, 4, 0, 5, tzinfo=chicago)
    )
    assert h.pipeline.refresh(RefreshImagesRequest(all=True), 'admin')[0].outcome == (
        'queued'
    )


def test_the_release_and_monthly_triggers_skip_a_row_baked_today(monkeypatch):
    chicago = ZoneInfo('America/Chicago')
    today = datetime(2026, 10, 4, 2, 30, tzinfo=chicago)
    monkeypatch.setattr(module, 'now_utc', lambda: today)
    h = monthly_harness(datetime(2026, 9, 6, 2, 5, tzinfo=chicago))
    monkeypatch.setattr(
        module,
        'resolve_stock_image',
        lambda ec2, base_os, arch, logger: {'ImageId': f'ami-stock-{base_os}-new'},
    )
    rocky9 = h.row('rocky9')
    rocky9.started_on = datetime(2026, 10, 4, 0, 10, tzinfo=chicago)
    h.records.put(rocky9)
    h.pipeline.tick(now=today.astimezone(timezone.utc))
    assert h.row('rocky9').status == 'current'
    assert h.row('rocky8').status == 'queued' and h.row('rocky8').trigger == 'monthly'

    # a new release waits for the next day for the row baked today, and stays unsettled
    h = Harness(config={'vdc.software_stacks.images_baked_release': '26.10.0'})
    baked_at(h, datetime(2026, 10, 4, 0, 10, tzinfo=chicago))
    stale = h.row()
    stale.release = '26.10.0'
    h.records.put(stale)
    h.pipeline._start = lambda record, blocking: True
    h.pipeline.tick(now=today.astimezone(timezone.utc))
    assert h.row().status == 'current'
    assert h.config.values['vdc.software_stacks.images_baked_release'] == '26.10.0'
    tomorrow = datetime(2026, 10, 5, 0, 1, tzinfo=chicago)
    monkeypatch.setattr(module, 'now_utc', lambda: tomorrow)
    h.pipeline.tick(now=tomorrow.astimezone(timezone.utc))
    assert h.row().status == 'queued' and h.row().trigger == 'release'
