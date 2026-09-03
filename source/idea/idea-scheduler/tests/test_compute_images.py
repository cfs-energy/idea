"""
SchedulerAdmin.ListComputeImages / BuildComputeImage service: per base OS and
architecture classification of what the cluster launches today (scheduler default
first, then queue profiles), built vs stock vs missing, the last build record, and the
base AMI a build starts from.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from ideadatamodel import BuildComputeImageRequest, ImageBuildRecord, exceptions
from ideadatamodel.scheduler import HpcQueueProfile, SocaJobParams
from ideasdk.aws.image_builds import (
    BUILD_STATUS_BUILDING,
    BUILD_STATUS_FAILED,
    ImageBuildRunner,
)
from ideascheduler.app.images import compute_images as module
from ideascheduler.app.images.compute_images import ComputeImageService

SELF_ACCOUNT = '111111111111'
RESF = '792107900819'
BLOCK_DEVICES = [{'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 10}}]

IMAGES = {
    'ami-al2023built00001': {
        'ImageId': 'ami-al2023built00001',
        'Name': 'idea-compute-node-amazonlinux2023-v08312026-214021',
        'Architecture': 'x86_64',
        'CreationDate': '2026-08-31T21:40:21.000Z',
        'OwnerId': SELF_ACCOUNT,
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-rocky9stock00001': {
        'ImageId': 'ami-rocky9stock00001',
        'Name': 'Rocky-9-EC2-Base-9.6-20250531.0.x86_64',
        'Architecture': 'x86_64',
        'CreationDate': '2025-05-31T00:00:00.000Z',
        'OwnerId': RESF,
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-rhel9built000001': {
        'ImageId': 'ami-rhel9built000001',
        'Name': 'idea-compute-node-rhel9-v08012026-101010',
        'Architecture': 'x86_64',
        'CreationDate': '2026-08-01T10:10:10.000Z',
        'OwnerId': SELF_ACCOUNT,
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-foreignpublic001': {
        'ImageId': 'ami-foreignpublic001',
        'Name': 'someone-elses-rocky-9',
        'Architecture': 'x86_64',
        'CreationDate': '2026-08-01T10:10:10.000Z',
        'OwnerId': '999999999999',
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
    'ami-rocky9armstock01': {
        'ImageId': 'ami-rocky9armstock01',
        'Name': 'Rocky-9-EC2-Base-9.6-20250531.0.aarch64',
        'Architecture': 'arm64',
        'CreationDate': '2025-05-31T00:00:00.000Z',
        'OwnerId': RESF,
        'BlockDeviceMappings': BLOCK_DEVICES,
    },
}


class FakeEc2:
    def __init__(self, owned=(), region='us-east-2'):
        self.owned = list(owned)
        self.meta = Mock(region_name=region)

    def describe_images(self, **kwargs):
        if 'ImageIds' in kwargs:
            missing = [i for i in kwargs['ImageIds'] if i not in IMAGES]
            if missing:
                raise RuntimeError(f'InvalidAMIID.NotFound: {missing}')
            images = [IMAGES[i] for i in kwargs['ImageIds']]
            owners = kwargs.get('Owners')
            if owners:
                owners = [
                    SELF_ACCOUNT if owner == 'self' else owner for owner in owners
                ]
                images = [image for image in images if image['OwnerId'] in owners]
            return {'Images': images}
        pattern = kwargs['Filters'][0]['Values'][0]
        prefix = pattern.rstrip('*')
        return {
            'Images': [
                IMAGES[i] for i in self.owned if IMAGES[i]['Name'].startswith(prefix)
            ]
        }


class FakeRecords:
    def __init__(self):
        self.items = {}

    def get(self, base_os, architecture):
        return self.items.get((base_os, architecture))

    def put(self, record):
        self.items[(record.base_os, record.architecture)] = record
        return record

    def delete(self, base_os, architecture):
        self.items.pop((base_os, architecture), None)

    def claim(self, record):
        existing = self.items.get((record.base_os, record.architecture))
        if existing is not None and existing.status == 'building':
            return False
        self.put(record)
        return True

    def list_all(self):
        return list(self.items.values())


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get_string(self, key, required=False, default=None):
        return self.values.get(key, default)

    def get_list(self, key, required=False, default=None):
        value = self.values.get(key, default)
        return list(value) if value is not None else value


# what ComputeNodeAmiBuilder reads to launch a builder
BUILDER_CONFIG = {
    'scheduler.compute_node_instance_profile_arn': 'arn:aws:iam::111111111111:instance-profile/compute',
    'scheduler.compute_node_security_group_ids': ['sg-compute'],
    'cluster.network.private_subnets': ['subnet-a'],
    'cluster.network.ssh_key_pair': 'idea_test',
}


def queue_profile(name, base_os=None, instance_ami=None):
    return HpcQueueProfile(
        name=name,
        default_job_params=SocaJobParams(base_os=base_os, instance_ami=instance_ami),
    )


def build_service(config, ec2, profiles=()) -> ComputeImageService:
    service = object.__new__(ComputeImageService)
    context = Mock()
    context.config.return_value = FakeConfig(config)
    context.aws.return_value.ec2.return_value = ec2
    context.queue_profiles.list_queue_profiles.return_value = list(profiles)
    service.context = context
    service._logger = Mock()
    service.records = FakeRecords()
    service.runner = ImageBuildRunner(context, service.records, service._logger)
    return service


DEFAULT_CONFIG = {
    'scheduler.compute_node_os': 'amazonlinux2023',
    'scheduler.compute_node_ami': 'ami-al2023built00001',
}


ROCKY9_STOCK_CONFIG = {
    'scheduler.compute_node_os': 'rocky9',
    'scheduler.compute_node_ami': 'ami-rocky9stock00001',
}


def rows_by_os(service):
    return {row.base_os: row for row in service.list_images()}


def rows_by_key(service):
    return {(row.base_os, row.architecture): row for row in service.list_images()}


def test_only_a_combination_with_an_image_or_a_build_gets_a_row():
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    assert [(row.base_os, row.architecture) for row in service.list_images()] == [
        ('amazonlinux2023', 'x86_64')
    ]


def test_default_and_queue_profile_references_classify_the_row():
    service = build_service(
        DEFAULT_CONFIG,
        FakeEc2(),
        profiles=[
            queue_profile('compute', 'amazonlinux2023', 'ami-al2023built00001'),
            queue_profile('bio', 'rocky9', 'ami-rocky9stock00001'),
            queue_profile('empty'),
        ],
    )
    rows = rows_by_os(service)

    al2023 = rows['amazonlinux2023']
    assert al2023.state == 'built'
    assert al2023.image_id == 'ami-al2023built00001'
    assert al2023.build_date == datetime(2026, 8, 31, 21, 40, 21, tzinfo=timezone.utc)
    assert al2023.referenced_by == ['scheduler default', 'queue profile: compute']

    rocky9 = rows['rocky9']
    assert rocky9.state == 'stock'
    assert rocky9.referenced_by == ['queue profile: bio']
    assert rocky9.build_date is None


def test_an_unreferenced_os_shows_its_newest_build_or_nothing():
    service = build_service(DEFAULT_CONFIG, FakeEc2(owned=['ami-rhel9built000001']))
    rows = rows_by_os(service)

    assert rows['rhel9'].state == 'built'
    assert rows['rhel9'].image_id == 'ami-rhel9built000001'
    assert rows['rhel9'].referenced_by == []
    assert 'rocky8' not in rows


def test_a_deleted_image_is_reported_missing():
    service = build_service(
        {**ROCKY9_STOCK_CONFIG, 'scheduler.compute_node_ami': 'ami-gonegonegone0001'},
        FakeEc2(),
    )
    row = rows_by_os(service)['rocky9']
    assert row.state == 'missing'
    assert row.image_id == 'ami-gonegonegone0001'
    assert row.referenced_by == ['scheduler default']


def test_a_second_image_for_the_same_os_is_noted():
    service = build_service(
        DEFAULT_CONFIG,
        FakeEc2(),
        profiles=[queue_profile('legacy', 'amazonlinux2023', 'ami-rocky9stock00001')],
    )
    row = rows_by_os(service)['amazonlinux2023']
    assert row.image_id == 'ami-al2023built00001'
    assert 'ami-rocky9stock00001 (queue profile: legacy)' in row.notes


def test_the_last_build_record_rides_along_and_building_wins():
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    service.records.put(
        ImageBuildRecord(
            base_os='amazonlinux2023',
            architecture='x86_64',
            status=BUILD_STATUS_BUILDING,
            instance_id='i-builder',
            started_on=datetime.now(tz=timezone.utc) - timedelta(minutes=2),
        )
    )
    service.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status=BUILD_STATUS_FAILED,
            error='boom',
        )
    )
    rows = rows_by_os(service)
    assert rows['amazonlinux2023'].state == 'building'
    assert rows['amazonlinux2023'].last_build.instance_id == 'i-builder'
    assert rows['rocky9'].state == 'none'
    assert rows['rocky9'].last_build.error == 'boom'


def test_builds_on_both_architectures_give_one_os_two_rows():
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    for architecture in ('x86_64', 'arm64'):
        service.records.put(
            ImageBuildRecord(
                base_os='rocky9',
                architecture=architecture,
                status='complete',
                image_id=f'ami-{architecture}',
            )
        )
    rows = rows_by_key(service)
    assert [key for key in rows if key[0] == 'rocky9'] == [
        ('rocky9', 'arm64'),
        ('rocky9', 'x86_64'),
    ]
    assert rows[('rocky9', 'arm64')].last_build.image_id == 'ami-arm64'
    assert rows[('rocky9', 'x86_64')].last_build.image_id == 'ami-x86_64'


def test_an_arm64_build_in_flight_sits_beside_the_stock_x86_64_row():
    service = build_service(ROCKY9_STOCK_CONFIG, FakeEc2())
    service.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='arm64',
            status=BUILD_STATUS_BUILDING,
            instance_id='i-arm-builder',
            started_on=datetime.now(tz=timezone.utc) - timedelta(minutes=2),
        )
    )
    rows = rows_by_key(service)

    arm = rows[('rocky9', 'arm64')]
    assert arm.state == 'building'
    assert arm.image_id is None
    assert arm.last_build.instance_id == 'i-arm-builder'

    x86 = rows[('rocky9', 'x86_64')]
    assert x86.state == 'stock'
    assert x86.image_id == 'ami-rocky9stock00001'
    assert x86.referenced_by == ['scheduler default']
    assert x86.last_build is None


def test_images_on_both_architectures_are_not_reported_as_a_second_image():
    service = build_service(
        ROCKY9_STOCK_CONFIG,
        FakeEc2(),
        profiles=[queue_profile('arm', 'rocky9', 'ami-rocky9armstock01')],
    )
    rows = rows_by_key(service)
    assert rows[('rocky9', 'x86_64')].notes is None
    assert rows[('rocky9', 'arm64')].image_id == 'ami-rocky9armstock01'
    assert rows[('rocky9', 'arm64')].referenced_by == ['queue profile: arm']


def test_default_base_ami_never_stacks_on_a_previous_build(monkeypatch):
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-freshstock000001'
    )
    # the scheduler default is an IDEA build, so the vendor image is used instead
    assert service.default_base_ami('amazonlinux2023') == 'ami-freshstock000001'

    stock_default = build_service(ROCKY9_STOCK_CONFIG, FakeEc2())
    monkeypatch.setattr(
        module, 'find_latest_stock_ami', lambda *args: 'ami-freshstock000001'
    )
    assert stock_default.default_base_ami('rocky9') == 'ami-rocky9stock00001'


def test_build_rejects_an_unknown_os_and_a_missing_base_ami(monkeypatch):
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    with pytest.raises(exceptions.SocaException):
        service.build(BuildComputeImageRequest(base_os='windows2022'), 'operator')
    monkeypatch.setattr(module, 'find_latest_stock_ami', lambda *args: None)
    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(BuildComputeImageRequest(base_os='rocky8'), 'operator')
    assert 'provide base_ami' in exc_info.value.message


def test_run_build_records_the_builder_and_its_result():
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    builder = Mock()
    builder.base_os = 'rocky9'
    builder.base_ami = 'ami-rocky9stock00001'
    builder.get_image_by_id.return_value = IMAGES['ami-rocky9stock00001']
    builder.get_ami_full_name.return_value = 'idea-compute-node-rocky9-v09012026-120000'
    builder.build.side_effect = lambda progress: 'ami-rocky9built00001'

    record = service.run_build(builder, requested_by='operator', blocking=True)

    assert record.status == 'complete'
    assert record.image_id == 'ami-rocky9built00001'
    assert record.requested_by == 'operator'
    assert record.update_target is False
    assert (
        service.records.get('rocky9', 'x86_64').ami_name
        == 'idea-compute-node-rocky9-v09012026-120000'
    )


def build_with_base_ami(base_ami, instance_type=None, monkeypatch=None):
    service = build_service({**DEFAULT_CONFIG, **BUILDER_CONFIG}, FakeEc2())
    # only the builder is under test here, not the runner
    service.run_build = lambda builder, requested_by, blocking: builder
    return service.build(
        BuildComputeImageRequest(
            base_os='rocky9', base_ami=base_ami, instance_type=instance_type
        ),
        'operator',
    )


def test_build_refuses_a_base_ami_from_a_foreign_account():
    with pytest.raises(exceptions.SocaException) as exc_info:
        build_with_base_ami('ami-foreignpublic001')
    assert 'owned by this account or by the rocky9 vendor' in exc_info.value.message


def test_build_accepts_vendor_and_own_base_amis():
    assert (
        build_with_base_ami('ami-rocky9stock00001').base_ami == 'ami-rocky9stock00001'
    )
    assert (
        build_with_base_ami('ami-rhel9built000001').base_ami == 'ami-rhel9built000001'
    )


def test_build_refuses_an_instance_type_outside_the_allowlist():
    with pytest.raises(exceptions.SocaException) as exc_info:
        build_with_base_ami('ami-rocky9stock00001', instance_type='p4d.24xlarge')
    assert 'instance_type must be one of' in exc_info.value.message
    assert (
        build_with_base_ami(
            'ami-rocky9stock00001', instance_type='c6i.xlarge'
        ).instance_type
        == 'c6i.xlarge'
    )


def test_rocky_builds_are_reported_unsupported_in_govcloud():
    service = build_service(
        {**DEFAULT_CONFIG, **BUILDER_CONFIG}, FakeEc2(region='us-gov-west-1')
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(BuildComputeImageRequest(base_os='rocky9'), 'operator')
    assert 'GovCloud' in exc_info.value.message


def instance_type_arch(service, table):
    def get_ec2_instance_type(instance_type):
        archs = table.get(instance_type)
        if archs is None:
            return None
        ec2_instance_type = Mock()
        ec2_instance_type.processor_info_supported_architectures.return_value = archs
        return ec2_instance_type

    service.context.aws_util.return_value.get_ec2_instance_type.side_effect = (
        get_ec2_instance_type
    )


def test_the_architecture_follows_the_instance_type(monkeypatch):
    service = build_service({**DEFAULT_CONFIG, **BUILDER_CONFIG}, FakeEc2())
    instance_type_arch(service, {'c6g.large': ['arm64'], 'c6i.large': ['x86_64']})
    asked = []
    monkeypatch.setattr(
        module,
        'find_latest_stock_ami',
        lambda ec2, os, arch, log: asked.append(arch) or None,
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(
            BuildComputeImageRequest(base_os='rhel9', instance_type='c6g.large'),
            'operator',
        )
    assert 'no stock rhel9 arm64 image' in exc_info.value.message
    assert asked == ['arm64']

    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(
            BuildComputeImageRequest(
                base_os='rhel9', instance_type='c6g.large', architecture='x86_64'
            ),
            'operator',
        )
    assert 'c6g.large is arm64' in exc_info.value.message
    assert service.resolve_architecture('c6i.large', None) == 'x86_64'
    assert service.resolve_architecture(None, None) == 'x86_64'


def test_the_compute_builder_type_follows_the_image_architecture():
    assert build_with_base_ami('ami-rocky9stock00001').instance_type == 'c5.large'
    arm = build_with_base_ami('ami-rocky9armstock01')
    assert arm.instance_type == 'm6g.large'
    assert arm.architecture == 'arm64'


def test_an_arm64_request_with_no_instance_type_uses_the_arm64_builder(monkeypatch):
    service = build_service({**DEFAULT_CONFIG, **BUILDER_CONFIG}, FakeEc2())
    service.run_build = lambda builder, requested_by, blocking: builder
    monkeypatch.setattr(
        module,
        'find_latest_stock_ami',
        lambda ec2, base_os, architecture, log: 'ami-rocky9armstock01'
        if architecture == 'arm64'
        else 'ami-rocky9stock00001',
    )

    builder = service.build(
        BuildComputeImageRequest(base_os='rocky9', architecture='arm64'), 'operator'
    )

    assert builder.base_ami == 'ami-rocky9armstock01'
    assert builder.architecture == 'arm64'
    assert builder.instance_type == 'm6g.large'


def test_an_x86_64_builder_type_is_refused_for_an_arm64_compute_image():
    with pytest.raises(exceptions.SocaException) as exc_info:
        build_with_base_ami('ami-rocky9armstock01', instance_type='m6i.large')
    assert 'm6i.large is x86_64' in exc_info.value.message
