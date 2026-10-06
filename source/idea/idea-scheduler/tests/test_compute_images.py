"""
SchedulerAdmin.ListComputeImages / BuildComputeImage service: per base OS and
architecture classification of what the cluster launches today (scheduler default
first, then queue profiles), built vs stock vs missing, the last build record, and the
base AMI a build starts from.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import threading

import pytest

from ideadatamodel import BuildComputeImageRequest, ImageBuildRecord, exceptions
from ideadatamodel.scheduler import HpcQueueProfile, SocaJobParams
from ideasdk.aws.image_builds import (
    BUILD_STATUS_BUILDING,
    BUILD_STATUS_FAILED,
    ImageBuildRunner,
    ImageBuildRecordsDB,
    check_builder_instance_type,
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

    to_item = staticmethod(ImageBuildRecordsDB.to_item)

    def put_if(self, record, expected):
        existing = self.get(record.base_os, record.row_key().range_key())
        for key, value in expected.items():
            current = getattr(existing, key, None)
            if isinstance(value, (set, frozenset)):
                if current in value:
                    return False
            elif current != value:
                return False
        return bool(self.put(record.model_copy(deep=True)))

    def list_all(self):
        return list(self.items.values())


class FakeConfigDB:
    """the stored settings; FakeConfig.values is the module's (possibly lagging) copy"""

    def __init__(self):
        self.entries = {}

    def set_config_entry(self, key, value, source='sdk'):
        self.entries[key] = value

    def set_config_entry_if(self, key, value, expected, source='sdk'):
        if self.entries.get(key) != expected:
            return False
        self.entries[key] = value
        return True


class FakeConfig:
    def __init__(self, values):
        self.values = dict(values)
        self.db = FakeConfigDB()

    def get_string(self, key, required=False, default=None):
        return self.values.get(key, default)

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def get_bool(self, key, default=None):
        return self.values.get(key, default)

    def get_int(self, key, default=None):
        return self.values.get(key, default)

    def get_real_key(self, key):
        return key

    def is_module_enabled(self, name):
        return name == 'scheduler'

    def get_module_id(self, name):
        return 'scheduler'

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
    service._live = {}
    service._lock = threading.RLock()
    service._last_sweep = float('inf')  # tests that want the builder sweep call it
    service._save = lambda record: service.records.put(record.model_copy(deep=True))
    context.is_leader.return_value = True
    context.cluster_name.return_value = 'test-cluster'
    context.module_id.return_value = 'scheduler'
    context.module_set.return_value = 'default'
    context.cluster_timezone.return_value = 'UTC'
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
            architecture='x86_64#custom',
            status=BUILD_STATUS_BUILDING,
            instance_id='i-builder',
            started_on=datetime.now(tz=timezone.utc) - timedelta(minutes=2),
        )
    )
    service.records.put(
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64#custom',
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
                architecture=f'{architecture}#custom',
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
            architecture='arm64#custom',
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
    assert stock_default.default_base_ami('rocky9') == 'ami-freshstock000001'


def test_build_rejects_an_unknown_os_and_a_missing_base_ami(monkeypatch):
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    with pytest.raises(exceptions.SocaException):
        service.build(BuildComputeImageRequest(base_os='windows2022'), 'operator')
    monkeypatch.setattr(module, 'find_latest_stock_ami', lambda *args: None)
    with pytest.raises(exceptions.SocaException) as exc_info:
        service.build(BuildComputeImageRequest(base_os='rocky8'), 'operator')
    assert 'no stock rocky8 x86_64 image' in exc_info.value.message


def test_run_build_is_a_custom_build_that_never_touches_the_managed_row():
    service = build_service(DEFAULT_CONFIG, FakeEc2())
    builder = Mock()
    builder.base_os = 'rocky9'
    builder.architecture = 'x86_64'
    builder.base_ami = 'ami-rocky9stock00001'
    builder.get_ami_full_name.return_value = 'idea-compute-node-rocky9-v09012026-120000'
    builder.build.side_effect = lambda progress: 'ami-rocky9built00001'

    record = service.run_build(builder, requested_by='operator', blocking=True)

    assert record.status == 'complete'
    assert record.image_id == 'ami-rocky9built00001'
    assert record.requested_by == 'operator'
    assert record.update_target is False
    assert service.records.get('rocky9', 'x86_64') is None
    assert (
        service.records.get('rocky9', 'x86_64#custom').ami_name
        == 'idea-compute-node-rocky9-v09012026-120000'
    )
    # the pipeline neither lists nor runs it
    assert all(not r.architecture.endswith('#custom') for r in service.list_rows())


def build_with_base_ami(base_ami, instance_type=None, monkeypatch=None):
    service = build_service({**DEFAULT_CONFIG, **BUILDER_CONFIG}, FakeEc2())
    # Explicit-image trust and builder architecture are constructor contracts.
    architecture = IMAGES.get(base_ami, {}).get('Architecture', 'x86_64')
    check_builder_instance_type(instance_type, architecture)
    return module.ComputeNodeAmiBuilder(
        context=service.context,
        base_os='rocky9',
        base_ami=base_ami,
        instance_type=instance_type,
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
        ec2_instance_type.processor_info_supported_architectures = archs
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
    assert build_with_base_ami('ami-rocky9stock00001').instance_type == 'c7i.large'
    arm = build_with_base_ami('ami-rocky9armstock01')
    assert arm.instance_type == 'm8g.large'
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
    assert builder.instance_type == 'm8g.large'


def test_an_x86_64_builder_type_is_refused_for_an_arm64_compute_image():
    with pytest.raises(exceptions.SocaException) as exc_info:
        build_with_base_ami('ami-rocky9armstock01', instance_type='m6i.large')
    assert 'm6i.large is x86_64' in exc_info.value.message


@pytest.mark.parametrize('ami_gb,expected', [(8, 10), (11, 11), (50, 50)])
def test_compute_build_describes_root_only_in_builder(monkeypatch, ami_gb, expected):
    stock = 'ami-rocky9stock00001'
    monkeypatch.setitem(
        IMAGES,
        stock,
        {
            **IMAGES[stock],
            'BlockDeviceMappings': [
                {'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': ami_gb}},
            ],
        },
    )
    ec2 = FakeEc2()
    describe = Mock(wraps=ec2.describe_images)
    ec2.describe_images = describe
    service = build_service({**ROCKY9_STOCK_CONFIG, **BUILDER_CONFIG}, ec2)
    monkeypatch.setattr(service, 'default_base_ami', lambda *a: stock)
    monkeypatch.setattr(service, 'run_build', lambda builder, **_: builder)
    builder = service.build(
        BuildComputeImageRequest(base_os='rocky9', architecture='x86_64'), 'operator'
    )
    assert builder.ebs_volume_size == expected
    describe.assert_called_once()


@pytest.mark.parametrize(
    'ami_gb,requested,expected',
    [(8, None, 10), (8, 8, 10), (11, None, 11), (8, 30, 30)],
)
def test_compute_builder_disk_is_at_least_10_gb(
    monkeypatch, ami_gb, requested, expected
):
    """an 8 GB ubuntu 24.04 root filled up mid bake; every compute image baked on 10 GB through 26.10.1"""
    stock = 'ami-rocky9stock00001'
    monkeypatch.setitem(
        IMAGES,
        stock,
        {
            **IMAGES[stock],
            'BlockDeviceMappings': [
                {'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': ami_gb}}
            ],
        },
    )
    service = build_service({**DEFAULT_CONFIG, **BUILDER_CONFIG}, FakeEc2())
    builder = module.ComputeNodeAmiBuilder(
        context=service.context,
        base_os='rocky9',
        base_ami=stock,
        ebs_volume_size=requested,
    )
    assert builder.ebs_volume_size == expected
