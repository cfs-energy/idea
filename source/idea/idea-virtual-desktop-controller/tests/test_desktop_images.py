"""
VirtualDesktopAdmin.ListDesktopImages / BuildDesktopImage / BuildAllDesktopImages /
UseBuiltDesktopImages: one row per ss-base-<os>-<arch>-base stack, built vs stock vs
missing, custom stacks sharing the image, the last build record; a custom build kept
apart from the managed row; build all queued through the pipeline; Use built image gated.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from ideadatamodel import (
    BuildAllDesktopImagesRequest,  # noqa: F401
    BuildDesktopImageRequest,
    ImageBuildRecord,
    SocaListingPayload,
    SocaMemory,
    SocaMemoryUnit,
    VirtualDesktopSoftwareStack,
    exceptions,
)
from ideasdk.aws.image_builds import BUILD_STATUS_FAILED, ImageBuildRunner
from image_fakes import FakeRecords
from ideavirtualdesktopcontroller.app.software_stacks import desktop_images as module
from ideavirtualdesktopcontroller.app.software_stacks.desktop_images import (
    DesktopImageService,
    base_stack_id,
    parse_base_stack_id,
)
from ideavirtualdesktopcontroller.app.software_stacks.virtual_desktop_software_stack_db import (
    VirtualDesktopSoftwareStackDB,
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
    def __init__(self, stacks, base_stack_config=None):
        self.stacks = list(stacks)
        self.updated = []
        self.base_stack_config = base_stack_config or {}
        if base_stack_config is None:
            for software_stack in self.stacks:
                parsed = parse_base_stack_id(software_stack.stack_id)
                if parsed is not None:
                    self.base_stack_config.setdefault(parsed[0], {})[
                        module.EC2_ARCH_TO_STACK[parsed[1]]
                    ] = {}

    def get_base_software_stack_config(self):
        return self.base_stack_config

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


def stack(stack_id, base_os, ami_id, base_ami_id=None):
    return VirtualDesktopSoftwareStack(
        stack_id=stack_id, base_os=base_os, ami_id=ami_id, base_ami_id=base_ami_id
    )


def build_service(stacks, base_stack_config=None) -> DesktopImageService:
    service = object.__new__(DesktopImageService)
    context = Mock()
    context.aws.return_value.ec2.return_value = FakeEc2()
    context.config.return_value = FakeConfig()
    context.module_id.return_value = 'vdc'
    context.module_name.return_value = 'virtual-desktop-controller'
    context.module_set.return_value = 'default'
    context.cluster_name.return_value = 'idea-test'
    context.cluster_timezone.return_value = 'UTC'
    service.context = context
    service._software_stack_db = FakeStackDb(stacks, base_stack_config)
    service._software_stack_utils = Mock()
    service._logger = Mock()
    service.records = FakeRecords()
    service.runner = ImageBuildRunner(context, service.records, service._logger)
    service._pipeline = None
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


@pytest.mark.parametrize('table_exists', [True, False])
def test_initialize_merges_base_stacks_for_existing_and_new_tables(table_exists):
    db = object.__new__(VirtualDesktopSoftwareStackDB)
    db.context = Mock()
    db.context.aws_util.return_value.dynamodb_check_table_exists.return_value = (
        table_exists
    )
    db._create_base_software_stacks = Mock()

    db.initialize()

    db._create_base_software_stacks.assert_called_once_with()
    assert db.context.aws_util.return_value.dynamodb_create_table.call_count == (
        0 if table_exists else 1
    )


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
        'ss-base-windows2022-x86-64-base',
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


def test_rows_include_configured_stacks_that_are_missing_or_disabled():
    disabled = stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-rocky9stock00001')
    disabled.enabled = False
    service = build_service(
        [disabled],
        {
            'rocky8': {'arm64': {}},
            'rocky9': {'x86-64': {}},
        },
    )

    rows = {row.stack_id: row for row in service.list_images()}

    assert set(rows) == {
        'ss-base-rocky8-arm64-base',
        'ss-base-rocky9-x86-64-base',
    }
    assert rows['ss-base-rocky8-arm64-base'].state == 'none'
    assert rows['ss-base-rocky9-x86-64-base'].state == 'stock'


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


class InstantBuilder:
    """stands in for DcvHostImageBuilder: no ec2, returns a per-os image id"""

    ebs_volume_size = None

    def __init__(
        self,
        context,
        base_ami,
        base_os,
        instance_type=None,
        force=False,
        ebs_volume_size=None,
        **_,
    ):
        self.base_ami = base_ami
        self.base_os = base_os
        self.ebs_volume_size = ebs_volume_size
        InstantBuilder.ebs_volume_size = ebs_volume_size

    def get_ami_full_name(self):
        return f'idea-dcv-host-{self.base_os}-v09022026-000000'

    def build(self, progress=None):
        if progress:
            progress({'instance_id': 'i-builder'})
        return f'ami-{self.base_os}-built'


def blocking_runner(service, monkeypatch):
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


def test_a_custom_build_never_touches_the_managed_row_or_a_stack(monkeypatch):
    service = build_service(
        [stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-rocky9built00001')]
    )
    managed = ImageBuildRecord(
        base_os='rocky9',
        architecture='x86_64',
        status='current',
        image_id='ami-rocky9built00001',
        current_image_id='ami-rocky9built00001',
        promoted_on=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    service.records.put(managed)
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-freshstock000001'
    )
    monkeypatch.setattr(module, 'DcvHostImageBuilder', InstantBuilder)
    blocking_runner(service, monkeypatch)

    record = service.build(
        BuildDesktopImageRequest(base_os='rocky9', update_stack=False), 'operator'
    )

    assert record.architecture == 'x86_64#custom'
    assert record.base_ami == 'ami-freshstock000001'
    assert service.records.get('rocky9', 'x86_64#custom').image_id == 'ami-rocky9-built'
    still = service.records.get('rocky9', 'x86_64')
    assert still.status == 'current'
    assert still.image_id == 'ami-rocky9built00001'
    assert service._software_stack_db.updated == []
    rows = service.pipeline.list_rows()
    assert [(r.base_os, r.architecture) for r in rows] == [('rocky9', 'x86_64')]
    # the leader loop only ever sees managed rows, so it never adopts or runs a custom build
    assert [r.architecture for r in service.pipeline.managed()] == ['x86_64']
    assert 'ami-rocky9-built' in service.pipeline.protected_images()


def test_a_custom_build_passes_the_base_stack_minimum(monkeypatch):
    software_stack = stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-old')
    software_stack.min_storage = SocaMemory(value=20, unit=SocaMemoryUnit.GB)
    service = build_service([software_stack])
    monkeypatch.setattr(module, 'find_latest_stock_ami', lambda *args: 'ami-8gb')
    monkeypatch.setattr(module, 'DcvHostImageBuilder', InstantBuilder)
    monkeypatch.setattr(service.runner, 'start', lambda record, build, **kwargs: record)
    service.build(BuildDesktopImageRequest(base_os='rocky9'), 'operator')
    assert InstantBuilder.ebs_volume_size == 20

    larger = stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-old')
    larger.min_storage = SocaMemory(value=100, unit=SocaMemoryUnit.GB)
    service = build_service([larger])
    monkeypatch.setattr(module, 'DcvHostImageBuilder', InstantBuilder)
    monkeypatch.setattr(service.runner, 'start', lambda record, build, **kwargs: record)
    service.build(BuildDesktopImageRequest(base_os='rocky9'), 'operator')
    assert InstantBuilder.ebs_volume_size == 100


def test_a_custom_build_cannot_repoint_a_stack():
    service = build_service(
        [stack('ss-base-rocky9-x86-64-base', 'rocky9', 'ami-al2023stock00001')]
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(
            BuildDesktopImageRequest(base_os='rocky9', update_stack=True), 'operator'
        )
    assert 'not validated' in exc_info.value.message


def test_build_rejects_unknown_os_and_missing_stack():
    service = build_service([])
    with pytest.raises(exceptions.SocaException):
        service.build(BuildDesktopImageRequest(base_os='rocky10'), 'operator')
    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(BuildDesktopImageRequest(base_os='rocky9'), 'operator')
    assert 'does not exist' in exc_info.value.message


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


def test_build_all_queues_every_row_for_the_pipeline():
    service = build_service(BASE_STACKS)

    results = {r.stack_id: r for r in service.build_all('operator')}

    assert results['ss-base-rocky9-x86-64-base'].status == 'started'
    assert results['ss-base-amazonlinux2023-x86-64-base'].status == 'started'
    # windows bakes through its own builder component
    assert results['ss-base-windows2022-x86-64-base'].status == 'started'
    assert service.records.get('rocky9', 'x86_64').status == 'queued'
    assert service.records.get('rocky9', 'x86_64').trigger == 'button'


def test_build_all_twice_queues_nothing_more():
    service = build_service(BASE_STACKS)

    first = service.build_all('operator')
    second = service.build_all('operator')

    assert sorted(r.status for r in first) == ['started', 'started', 'started']
    assert sorted(r.status for r in second) == ['skipped', 'skipped', 'skipped']


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


def test_build_all_queues_every_one_of_the_fourteen_base_stacks():
    service = build_service(ALL_BASE_STACKS)
    # a build elsewhere never blocks queueing: the pipeline runs them in waves
    service.records.put(
        ImageBuildRecord(
            base_os='compute-al2023',
            architecture='x86_64',
            status='building',
            started_on=datetime.now(tz=timezone.utc),
        )
    )

    results = service.build_all('admin')

    assert len(results) == 14
    assert {r.status for r in results} == {'started'}
    assert {r.architecture for r in results} == {'x86_64', 'arm64'}


def test_a_build_starts_from_the_vendors_newest_base_not_the_stacks(monkeypatch):
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
        module, 'find_latest_stock_ami', lambda *args: 'ami-rocky9stock00001'
    )
    no_thread(monkeypatch)

    record = service.build(BuildDesktopImageRequest(base_os='rocky9'), 'admin')

    assert record.base_ami == 'ami-rocky9stock00001'


def use_built_service(record):
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
    service.records.put(record)
    return service


def test_use_built_images_repoints_a_stack_at_its_validated_image():
    started = datetime(2026, 10, 1, tzinfo=timezone.utc)
    service = use_built_service(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='current',
            image_id='ami-rocky9built00001',
            base_ami='ami-rocky9stock00001',
            started_on=started,
            validated_on=started.replace(hour=1),
        )
    )

    results = {r.stack_id: r for r in service.use_built_images(None, 'admin')}

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
            ['ss-base-rocky9-x86-64-base', 'nope'], 'admin'
        )
    }
    assert again['ss-base-rocky9-x86-64-base'].status == 'skipped'
    assert again['nope'].status == 'error'


def test_use_built_images_refuses_an_unvalidated_build():
    # a pre-26.10.1 'complete' build migrates to current but was never validated
    service = use_built_service(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='complete',
            update_target=True,
            image_id='ami-rocky9built00001',
            base_ami='ami-rocky9stock00001',
        )
    )

    results = {r.stack_id: r for r in service.use_built_images(None, 'admin')}

    assert results['ss-base-rocky9-x86-64-base'].status == 'error'
    assert 'has not passed validation' in results['ss-base-rocky9-x86-64-base'].message
    assert service._software_stack_db.updated == []


def test_use_built_images_never_moves_a_pinned_stack():
    started = datetime(2026, 10, 1, tzinfo=timezone.utc)
    service = use_built_service(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='current',
            image_id='ami-rocky9built00001',
            started_on=started,
            validated_on=started,
        )
    )
    service._software_stack_db.stacks[0].image_pinned = True

    results = {r.stack_id: r for r in service.use_built_images(None, 'admin')}

    assert results['ss-base-rocky9-x86-64-base'].status == 'skipped'
    assert service._software_stack_db.updated == []


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
