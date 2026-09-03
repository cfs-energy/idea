"""
VirtualDesktopAdmin.ListDesktopImages / BuildDesktopImage service: one row per
ss-base-<os>-<arch>-base stack, built vs stock vs missing, custom stacks sharing the
image, the last build record, and the repoint plus reindex that follows a successful build.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from ideadatamodel import (
    BuildAllDesktopImagesRequest,  # noqa: F401
    BuildDesktopImageRequest,
    ImageBuildRecord,
    SocaListingPayload,
    VirtualDesktopSoftwareStack,
    exceptions,
)
from ideasdk.aws.image_builds import BUILD_STATUS_FAILED, ImageBuildRunner
from ideavirtualdesktopcontroller.app.software_stacks import desktop_images as module
from ideavirtualdesktopcontroller.app.software_stacks.desktop_images import (
    DesktopImageService,
    base_stack_id,
    parse_base_stack_id,
)

SELF_ACCOUNT = '111111111111'
RESF = '792107900819'
BLOCK_DEVICES = [{'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 10}}]

IMAGES = {
    'ami-rocky9built00001': {
        'ImageId': 'ami-rocky9built00001',
        'Name': 'idea-dcv-host-rocky9-v08312026-214021',
        'Architecture': 'x86_64',
        'OwnerId': SELF_ACCOUNT,
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-al2023stock00001': {
        'ImageId': 'ami-al2023stock00001',
        'Name': 'al2023-ami-2023.12.20260817.0-kernel-6.1-x86_64',
        'Architecture': 'x86_64',
        'OwnerId': 'amazon',
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-rocky9stock00001': {
        'ImageId': 'ami-rocky9stock00001',
        'Name': 'Rocky-9-EC2-Base-9.6-20250531.0.x86_64',
        'Architecture': 'x86_64',
        'OwnerId': RESF,
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-foreignpublic001': {
        'ImageId': 'ami-foreignpublic001',
        'Name': 'someone-elses-rocky-9',
        'Architecture': 'x86_64',
        'OwnerId': '999999999999',
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-rocky9armbuilt01': {
        'ImageId': 'ami-rocky9armbuilt01',
        'Name': 'idea-dcv-host-rocky9-v09022026-000000',
        'Architecture': 'arm64',
        'OwnerId': SELF_ACCOUNT,
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
}


class FakeEc2:
    meta = Mock(region_name='us-east-2')

    def describe_images(self, **kwargs):
        ids = kwargs.get('ImageIds', [])
        missing = [i for i in ids if i not in IMAGES]
        if missing:
            raise RuntimeError(f'InvalidAMIID.NotFound: {missing}')
        images = [IMAGES[i] for i in ids]
        owners = kwargs.get('Owners')
        if owners:
            owners = [SELF_ACCOUNT if owner == 'self' else owner for owner in owners]
            images = [image for image in images if image['OwnerId'] in owners]
        return {'Images': images}


class FakeConfig:
    """what DcvHostImageBuilder reads to launch a builder"""

    VALUES = {
        'virtual-desktop-controller.dcv_host_instance_profile_arn': 'arn:aws:iam::111111111111:instance-profile/dcv-host',
        'virtual-desktop-controller.dcv_host_security_group_id': 'sg-host',
        'virtual-desktop-controller.dcv_session.additional_security_groups': [],
        'virtual-desktop-controller.dcv_session.network.private_subnets': [
            'subnet-vdi'
        ],
        'cluster.network.private_subnets': ['subnet-a'],
        'cluster.network.ssh_key_pair': 'idea_test',
        'cluster.cluster_name': 'idea-test',
    }

    def get_string(self, key, required=False, default=None):
        return self.VALUES.get(key, default)

    def get_list(self, key, required=False, default=None):
        value = self.VALUES.get(key, default)
        return list(value) if value is not None else value


class FakeStackDb:
    def __init__(self, stacks):
        self.stacks = list(stacks)
        self.updated = []

    def list_all_from_db(self, request):
        return SocaListingPayload(listing=list(self.stacks))

    def get(self, stack_id, base_os):
        for stack in self.stacks:
            if stack.stack_id == stack_id and stack.base_os == base_os:
                return stack
        return None

    def update(self, stack):
        self.updated.append(stack)
        return stack


class FakeRecords:
    def __init__(self):
        self.items = {}

    def get(self, base_os, architecture):
        return self.items.get((base_os, architecture))

    def put(self, record):
        self.items[(record.base_os, record.architecture)] = record
        return record

    def claim(self, record):
        existing = self.items.get((record.base_os, record.architecture))
        if existing is not None and existing.status == 'building':
            return False
        self.put(record)
        return True

    def delete(self, base_os, architecture):
        self.items.pop((base_os, architecture), None)

    def list_all(self):
        return list(self.items.values())


def stack(stack_id, base_os, ami_id, base_ami_id=None):
    return VirtualDesktopSoftwareStack(
        stack_id=stack_id, base_os=base_os, ami_id=ami_id, base_ami_id=base_ami_id
    )


def build_service(stacks) -> DesktopImageService:
    service = object.__new__(DesktopImageService)
    context = Mock()
    context.aws.return_value.ec2.return_value = FakeEc2()
    context.config.return_value = FakeConfig()
    context.module_id.return_value = 'vdc'
    context.module_name.return_value = 'virtual-desktop-controller'
    context.module_set.return_value = 'default'
    context.cluster_name.return_value = 'idea-test'
    service.context = context
    service._software_stack_db = FakeStackDb(stacks)
    service._software_stack_utils = Mock()
    service._logger = Mock()
    service.records = FakeRecords()
    service.runner = ImageBuildRunner(context, service.records, service._logger)
    return service


def test_base_stack_ids_round_trip():
    assert parse_base_stack_id('ss-base-rocky9-x86-64-base') == ('rocky9', 'x86_64')
    assert parse_base_stack_id('ss-base-amazonlinux2023-arm64-base') == (
        'amazonlinux2023',
        'arm64',
    )
    assert parse_base_stack_id('ss-base-rocky9-x86-64-dcv') is None
    assert parse_base_stack_id('my-custom-stack') is None
    assert base_stack_id('rocky9', 'x86_64') == 'ss-base-rocky9-x86-64-base'


def test_rows_classify_base_stacks_and_count_custom_stacks_on_the_image():
    service = build_service(
        [
            stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-rocky9built00001'),
            stack(
                'ss-base-amazonlinux2023-x86-64-base',
                'amazonlinux2023',
                'ami-al2023stock00001',
            ),
            stack('ss-base-rhel9-x86-64-base', 'rhel9', 'ami-gonegonegone0001'),
            stack('ss-base-windows2022-x86-64-base', 'windows2022', 'ami-win'),
            stack('ss-base-rocky9-x86-64-dcv', 'rocky9', 'ami-rocky9built00001'),
            stack('custom-1', 'rocky9', 'ami-rocky9built00001'),
        ]
    )
    rows = {row.stack_id: row for row in service.list_images()}

    assert set(rows) == {
        'ss-base-amazonlinux2023-x86-64-base',
        'ss-base-rhel9-x86-64-base',
        'ss-base-rocky9-x86-64-base',
    }
    rocky = rows['ss-base-rocky9-x86-64-base']
    assert rocky.state == 'built'
    assert rocky.build_date == datetime(2026, 8, 31, 21, 40, 21, tzinfo=timezone.utc)
    assert rocky.referenced_by == [
        'ss-base-rocky9-x86-64-base',
        '2 custom stacks on the same image',
    ]
    assert rows['ss-base-amazonlinux2023-x86-64-base'].state == 'stock'
    assert rows['ss-base-rhel9-x86-64-base'].state == 'missing'


def test_the_last_build_record_rides_along():
    service = build_service(
        [stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-rocky9built00001')]
    )
    service.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status=BUILD_STATUS_FAILED,
            error='boom',
        )
    )
    row = service.list_images()[0]
    assert row.state == 'built'
    assert row.last_build.error == 'boom'


def test_build_repoints_the_base_stack_and_reindexes(monkeypatch):
    service = build_service(
        [stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-al2023stock00001')]
    )
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-freshstock000001'
    )

    class FakeBuilder:
        def __init__(
            self, context, base_ami, base_os, instance_type=None, force=False, **_
        ):
            self.base_ami = base_ami
            self.base_os = base_os
            self.instance_type = instance_type
            self.force = force

        def get_ami_full_name(self):
            return f'idea-dcv-host-{self.base_os}-v09012026-120000'

        def build(self, progress=None):
            progress({'instance_id': 'i-builder'})
            return 'ami-rocky9built00001'

    monkeypatch.setattr(module, 'DcvHostImageBuilder', FakeBuilder)
    monkeypatch.setattr(
        service.runner,
        'start',
        lambda record,
        build,
        on_success=None,
        blocking=False: service.runner.__class__.start(
            service.runner, record, build, on_success, blocking=True
        ),
    )

    record = service.build(
        BuildDesktopImageRequest(base_os='rocky9'), requested_by='operator'
    )

    assert record.status == 'complete'
    assert record.base_ami == 'ami-freshstock000001'
    assert record.instance_id == 'i-builder'
    assert record.update_target is True
    updated = service._software_stack_db.updated
    assert [s.ami_id for s in updated] == ['ami-rocky9built00001']
    assert updated[0].base_ami_id == 'ami-freshstock000001'
    service._software_stack_utils.update_software_stack_entry_to_opensearch.assert_called_once()


def test_build_rejects_unknown_os_and_missing_stack():
    service = build_service([])
    with pytest.raises(exceptions.SocaException):
        service.build(BuildDesktopImageRequest(base_os='rocky10'), 'operator')
    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(BuildDesktopImageRequest(base_os='rocky9'), 'operator')
    assert 'does not exist' in exc_info.value.message


class InstantBuilder:
    """stands in for DcvHostImageBuilder: no ec2, returns a per-os image id"""

    def __init__(
        self, context, base_ami, base_os, instance_type=None, force=False, **_
    ):
        self.base_ami = base_ami
        self.base_os = base_os

    def get_ami_full_name(self):
        return f'idea-dcv-host-{self.base_os}-v09022026-000000'

    def build(self, progress=None):
        return f'ami-{self.base_os}-built'


def build_all_service(monkeypatch, stacks, failing_os=None):
    service = build_service(stacks)
    monkeypatch.setattr(module, 'DcvHostImageBuilder', InstantBuilder)
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-freshstock000001'
    )
    if failing_os is not None:
        real_build = service.build

        def build(request, requested_by):
            if request.base_os == failing_os:
                raise RuntimeError('RunInstances denied')
            return real_build(request, requested_by)

        service.build = build
    return service


def no_thread(monkeypatch):
    """builds stay 'building': the thread never runs, like the real async path mid-flight"""

    class DeadThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

    from ideasdk.aws import image_builds

    monkeypatch.setattr(image_builds.threading, 'Thread', DeadThread)


BASE_STACKS = [
    stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-al2023stock00001'),
    stack(
        'ss-base-amazonlinux2023-x86-64-base', 'amazonlinux2023', 'ami-al2023stock00001'
    ),
    stack('ss-base-windows2022-x86-64-base', 'windows2022', 'ami-win'),
    stack('custom-1', 'rocky9', 'ami-al2023stock00001'),
]


def test_build_all_starts_every_supported_base_stack(monkeypatch):
    service = build_all_service(monkeypatch, BASE_STACKS)
    no_thread(monkeypatch)

    results = service.build_all('operator')

    assert [(r.stack_id, r.status) for r in results] == [
        ('ss-base-amazonlinux2023-x86-64-base', 'started'),
        ('ss-base-rocky9-x86-64-base', 'started'),
    ]
    assert service.records.get('rocky9', 'x86_64').status == 'building'
    assert service.records.get('amazonlinux2023', 'x86_64').status == 'building'


def test_build_all_skips_a_row_already_building(monkeypatch):
    from datetime import datetime, timedelta, timezone

    service = build_all_service(monkeypatch, BASE_STACKS)
    no_thread(monkeypatch)
    service.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='building',
            instance_id='i-busy',
            started_on=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        )
    )

    results = {r.stack_id: r for r in service.build_all('operator')}

    assert results['ss-base-rocky9-x86-64-base'].status == 'skipped'
    assert 'already running' in results['ss-base-rocky9-x86-64-base'].message
    assert results['ss-base-amazonlinux2023-x86-64-base'].status == 'started'


def test_build_all_one_failure_does_not_stop_the_rest(monkeypatch):
    service = build_all_service(monkeypatch, BASE_STACKS, failing_os='amazonlinux2023')
    no_thread(monkeypatch)

    results = {r.stack_id: r for r in service.build_all('operator')}

    assert results['ss-base-amazonlinux2023-x86-64-base'].status == 'error'
    assert 'RuntimeError' in results['ss-base-amazonlinux2023-x86-64-base'].message
    assert results['ss-base-rocky9-x86-64-base'].status == 'started'


def test_build_all_twice_skips_everything(monkeypatch):
    service = build_all_service(monkeypatch, BASE_STACKS)
    no_thread(monkeypatch)

    first = service.build_all('operator')
    second = service.build_all('operator')

    assert {r.status for r in first} == {'started'}
    assert {r.status for r in second} == {'skipped'}


def build_with(monkeypatch, base_ami=None, instance_type=None):
    service = build_service(
        [stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-al2023stock00001')]
    )
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-rocky9stock00001'
    )
    no_thread(monkeypatch)
    return service.build(
        BuildDesktopImageRequest(
            base_os='rocky9', base_ami=base_ami, instance_type=instance_type
        ),
        'operator',
    )


def test_build_refuses_a_base_ami_from_a_foreign_account(monkeypatch):
    with pytest.raises(exceptions.SocaException) as exc_info:
        build_with(monkeypatch, base_ami='ami-foreignpublic001')
    assert 'owned by this account or by the rocky9 vendor' in exc_info.value.message


def test_build_accepts_vendor_and_own_base_amis(monkeypatch):
    assert (
        build_with(monkeypatch, base_ami='ami-rocky9stock00001').base_ami
        == 'ami-rocky9stock00001'
    )
    assert (
        build_with(monkeypatch, base_ami='ami-rocky9built00001').base_ami
        == 'ami-rocky9built00001'
    )


def test_build_refuses_an_instance_type_outside_the_allowlist(monkeypatch):
    with pytest.raises(exceptions.SocaException) as exc_info:
        build_with(monkeypatch, instance_type='p4d.24xlarge')
    assert 'instance_type must be one of' in exc_info.value.message


def test_build_all_stops_at_the_concurrency_cap_and_says_so_on_the_row(monkeypatch):
    from ideasdk.aws.image_builds import MAX_CONCURRENT_BUILDS

    service = build_all_service(monkeypatch, BASE_STACKS)
    no_thread(monkeypatch)
    for index in range(MAX_CONCURRENT_BUILDS):
        service.records.put(
            ImageBuildRecord(
                base_os=f'other{index}',
                architecture='x86_64',
                status='building',
                started_on=datetime.now(tz=timezone.utc),
            )
        )

    results = service.build_all('steve')

    assert {r.status for r in results} == {'skipped'}
    assert all(
        f'{MAX_CONCURRENT_BUILDS} builds already in progress' in r.message
        for r in results
    )
    noted = service.records.get('rocky9', 'x86_64')
    assert noted.status == 'skipped'
    assert f'{MAX_CONCURRENT_BUILDS} builds already in progress' in noted.error


def test_the_repoint_refuses_an_image_of_the_other_architecture(monkeypatch):
    service = build_service(
        [stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-al2023stock00001')]
    )
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-rocky9stock00001'
    )

    class ArmBuilder(InstantBuilder):
        def build(self, progress=None):
            return 'ami-rocky9armbuilt01'

    monkeypatch.setattr(module, 'DcvHostImageBuilder', ArmBuilder)
    monkeypatch.setattr(
        service.runner,
        'start',
        lambda record,
        build,
        on_success=None,
        blocking=False: service.runner.__class__.start(
            service.runner, record, build, on_success, blocking=True
        ),
    )

    record = service.build(
        BuildDesktopImageRequest(base_os='rocky9'), requested_by='operator'
    )

    assert record.status == 'complete'
    assert 'stack not repointed' in record.error
    assert service._software_stack_db.updated == []


def test_rocky_builds_are_reported_unsupported_in_govcloud(monkeypatch):
    service = build_service(
        [stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-al2023stock00001')]
    )
    service.context.aws.return_value.ec2.return_value.meta = Mock(
        region_name='us-gov-west-1'
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(BuildDesktopImageRequest(base_os='rocky9'), 'operator')
    assert 'GovCloud' in exc_info.value.message


ALL_BASE_STACKS = [
    stack(f'ss-base-{base_os}-{arch}-base', base_os, 'ami-al2023stock00001')
    for base_os in (
        'amazonlinux2023',
        'rhel8',
        'rhel9',
        'rocky8',
        'rocky9',
        'ubuntu2204',
        'ubuntu2404',
    )
    for arch in ('x86-64', 'arm64')
]


def test_build_all_starts_every_one_of_the_fourteen_base_stacks(monkeypatch):
    service = build_all_service(monkeypatch, ALL_BASE_STACKS)
    no_thread(monkeypatch)
    # a compute build elsewhere still leaves room for every desktop row
    service.records.put(
        ImageBuildRecord(
            base_os='compute-al2023',
            architecture='x86_64',
            status='building',
            started_on=datetime.now(tz=timezone.utc),
        )
    )

    results = service.build_all('steve')

    assert len(results) == 14
    assert {r.status for r in results} == {'started'}
    assert {r.architecture for r in results} == {'x86_64', 'arm64'}


def test_a_row_that_fails_to_start_shows_why(monkeypatch):
    service = build_all_service(monkeypatch, BASE_STACKS, failing_os='amazonlinux2023')
    no_thread(monkeypatch)

    service.build_all('steve')

    noted = service.records.get('amazonlinux2023', 'x86_64')
    assert noted.status == 'failed'
    assert 'RuntimeError' in noted.error


def test_a_build_starts_from_the_stacks_base_when_it_has_one(monkeypatch):
    service = build_service(
        [
            stack(
                'ss-base-rocky9-x86-64-base',
                'rocky9',
                'ami-rocky9built00001',
                base_ami_id='ami-rocky9stock00001',
            )
        ]
    )
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-freshstock000001'
    )
    no_thread(monkeypatch)

    record = service.build(BuildDesktopImageRequest(base_os='rocky9'), 'steve')

    assert record.base_ami == 'ami-rocky9stock00001'


def test_use_built_images_repoints_a_stack_at_its_last_completed_build():
    service = build_service(
        [
            stack(
                'ss-base-rocky9-x86-64-base',
                'rocky9',
                'ami-al2023stock00001',
                base_ami_id='ami-al2023stock00001',
            ),
            stack(
                'ss-base-amazonlinux2023-x86-64-base',
                'amazonlinux2023',
                'ami-al2023stock00001',
            ),
        ]
    )
    service.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='complete',
            image_id='ami-rocky9built00001',
            base_ami='ami-rocky9stock00001',
        )
    )

    results = {r.stack_id: r for r in service.use_built_images(None, 'steve')}

    assert results['ss-base-rocky9-x86-64-base'].status == 'updated'
    assert results['ss-base-amazonlinux2023-x86-64-base'].status == 'skipped'
    updated = service._software_stack_db.updated
    assert [(s.stack_id, s.ami_id, s.base_ami_id) for s in updated] == [
        ('ss-base-rocky9-x86-64-base', 'ami-rocky9built00001', 'ami-rocky9stock00001')
    ]
    service._software_stack_utils.update_software_stack_entry_to_opensearch.assert_called_once()

    again = {
        r.stack_id: r
        for r in service.use_built_images(
            ['ss-base-rocky9-x86-64-base', 'nope'], 'steve'
        )
    }
    assert again['ss-base-rocky9-x86-64-base'].status == 'skipped'
    assert again['nope'].status == 'error'


def test_the_row_says_built_base_outdated_when_the_base_moved_past_the_build():
    service = build_service(
        [
            stack(
                'ss-base-rocky9-x86-64-base',
                'rocky9',
                'ami-rocky9built00001',
                base_ami_id='ami-al2023stock00001',
            )
        ]
    )
    service.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='complete',
            image_id='ami-rocky9built00001',
            base_ami='ami-rocky9stock00001',
        )
    )

    row = service.list_images()[0]

    assert row.state == 'built_outdated'
    assert row.base_ami_id == 'ami-al2023stock00001'
    assert 'built from ami-rocky9stock00001' in row.notes
