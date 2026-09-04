"""
Test Cases for upgrade_cluster Phase 3 config entries

The write-list is derived from the module config templates: every module that
templates base_os / instance_ami must be updated, or that module keeps its
pre-upgrade OS while the rest of the cluster moves.
"""

import pathlib
import re

import pytest

from ideadatamodel import constants
from ideaadministrator.app_main import (
    HOST_INSTANCE_TYPE_KEYS,
    MODULE_HOST_INSTANCE_TYPE,
    MODULE_HOST_INSTANCE_TYPE_OLD,
    build_ami_update_entries,
    keep_built_compute_image,
    move_module_host_instance_type,
    resolve_compute_ami_keep_keys,
    update_module_host_instance_types,
)


AMI_ID = 'ami-0123456789abcdef0'
BASE_OS = 'amazonlinux2023'

# module config key prefix -> settings template that declares base_os / instance_ami
MODULE_TEMPLATES = {
    'bastion-host': 'bastion-host/settings.yml',
    'cluster-manager.ec2.autoscaling': 'cluster-manager/settings.yml',
    'directoryservice': 'directoryservice/_templates/openldap.yml',
    'scheduler': 'scheduler/settings.yml',
    'vdc.controller.autoscaling': 'virtual-desktop-controller/settings.yml',
    'vdc.dcv_broker.autoscaling': 'virtual-desktop-controller/settings.yml',
    'vdc.dcv_connection_gateway.autoscaling': 'virtual-desktop-controller/settings.yml',
}

# what ClusterConfigDB.get_cluster_modules() returns for a cluster installed with default ids
MODULES = [
    {'module_id': 'bastion-host', 'name': constants.MODULE_BASTION_HOST},
    {'module_id': 'cluster-manager', 'name': constants.MODULE_CLUSTER_MANAGER},
    {'module_id': 'directoryservice', 'name': constants.MODULE_DIRECTORYSERVICE},
    {'module_id': 'scheduler', 'name': constants.MODULE_SCHEDULER},
    {'module_id': 'vdc', 'name': constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER},
    # config-only modules template no AMI and must contribute nothing
    {'module_id': 'cluster', 'name': constants.MODULE_CLUSTER},
]


def parse(entries):
    parsed = {}
    for entry in entries:
        match = re.fullmatch(r'Key=(.+?),Type=(.+?),Value=(.*)', entry)
        assert match is not None, f'malformed entry: {entry}'
        key, data_type, value = match.groups()
        parsed[key] = (data_type, value)
    return parsed


@pytest.fixture(scope='module')
def entries():
    return build_ami_update_entries(ami_id=AMI_ID, base_os=BASE_OS, modules=MODULES)


@pytest.fixture(scope='module')
def entries_by_key(entries):
    return parse(entries)


def test_every_entry_is_parseable_by_the_caller(entries, entries_by_key):
    """
    upgrade_cluster splits each entry on ',' - the format must survive that
    """
    assert len(entries_by_key) == len(entries), 'duplicate keys in the write-list'
    for data_type, value in entries_by_key.values():
        assert data_type == 'string'
        assert value in (AMI_ID, BASE_OS)


def test_every_module_gets_both_base_os_and_ami(entries_by_key):
    """
    an AMI without its matching base_os leaves the module inconsistent
    """
    for prefix in MODULE_TEMPLATES:
        assert entries_by_key[f'{prefix}.base_os'] == ('string', BASE_OS)
        assert entries_by_key[f'{prefix}.instance_ami'] == ('string', AMI_ID)


def test_scheduler_compute_node_keys_are_updated(entries_by_key):
    """
    compute nodes are launched from scheduler.compute_node_ami / compute_node_os;
    updating only the AMI leaves the two disagreeing
    """
    assert entries_by_key['scheduler.compute_node_ami'] == ('string', AMI_ID)
    assert entries_by_key['scheduler.compute_node_os'] == ('string', BASE_OS)


def test_keys_use_the_module_ids_the_cluster_actually_has():
    """
    config keys are prefixed with the module id, which the installer lets you choose;
    a hardcoded id writes settings no module reads
    """
    entries_by_key = parse(
        build_ami_update_entries(
            ami_id=AMI_ID,
            base_os=BASE_OS,
            modules=[
                {
                    'module_id': 'vdc2',
                    'name': constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
                },
                {'module_id': 'sched2', 'name': constants.MODULE_SCHEDULER},
            ],
        )
    )

    assert entries_by_key['sched2.base_os'] == ('string', BASE_OS)
    assert entries_by_key['vdc2.controller.autoscaling.instance_ami'] == (
        'string',
        AMI_ID,
    )
    assert not any(key.startswith('vdc.') for key in entries_by_key)


def test_a_module_the_cluster_does_not_deploy_gets_no_entries():
    """
    writing bastion-host keys on a cluster with no bastion host is settings nothing reads
    """
    entries_by_key = parse(
        build_ami_update_entries(
            ami_id=AMI_ID,
            base_os=BASE_OS,
            modules=[{'module_id': 'scheduler', 'name': constants.MODULE_SCHEDULER}],
        )
    )

    assert sorted(entries_by_key) == [
        'scheduler.base_os',
        'scheduler.compute_node_ami',
        'scheduler.compute_node_os',
        'scheduler.instance_ami',
    ]


def test_write_list_covers_every_templated_module(entries_by_key):
    """
    guards against a new module templating base_os without being added here
    """
    templates_dir = (
        pathlib.Path(__file__).parents[1] / 'resources' / 'config' / 'templates'
    )
    if not templates_dir.is_dir():
        pytest.skip('config templates not available in this checkout')

    templated = set()
    for template in templates_dir.rglob('*.yml'):
        text = template.read_text()
        if re.search(r'^\s*base_os:\s*\{\{\s*base_os\s*\}\}', text, flags=re.MULTILINE):
            templated.add(template.relative_to(templates_dir).as_posix())

    covered = set(MODULE_TEMPLATES.values())
    assert templated == covered, (
        f'config templates declaring base_os changed: {sorted(templated)}; '
        f'update MODULE_TEMPLATES and build_ami_update_entries()'
    )

    for prefix in MODULE_TEMPLATES:
        assert f'{prefix}.base_os' in entries_by_key


# an upgrade must not undo a compute image the operator built and adopted from Custom AMIs

SCHEDULER_ONLY = [{'module_id': 'scheduler', 'name': constants.MODULE_SCHEDULER}]
COMPUTE_KEYS = {'scheduler.compute_node_ami', 'scheduler.compute_node_os'}

BUILT_AMI = 'ami-0b17fd2c9a4e5d360'
STOCK_IMAGE = {
    'ImageId': AMI_ID,
    'Name': 'al2023-ami-2026.0.20260801.0-kernel-6.1-x86_64',
    'CreationDate': '2026-08-01T10:00:00.000Z',
}
NEWER_BUILT_IMAGE = {
    'ImageId': BUILT_AMI,
    'Name': 'idea-compute-node-amazonlinux2023-v09012026-120000',
    'CreationDate': '2026-09-01T12:00:00.000Z',
}
OLDER_BUILT_IMAGE = dict(
    NEWER_BUILT_IMAGE,
    Name='idea-compute-node-amazonlinux2023-v07012026-120000',
    CreationDate='2026-07-01T12:00:00.000Z',
)
ADOPTED_STOCK_IMAGE = {
    'ImageId': BUILT_AMI,
    'Name': 'RHEL-9.6.0_HVM-20260501-x86_64-0-Hourly2-GP3',
    'CreationDate': '2026-09-01T12:00:00.000Z',
}


class FakeConfigDB:
    """the one cluster settings read resolve_compute_ami_keep_keys makes"""

    def __init__(self, compute_node_ami):
        self.compute_node_ami = compute_node_ami

    def get_config_entry(self, key):
        if key != 'scheduler.compute_node_ami':
            return None
        return {'key': key, 'value': self.compute_node_ami}


class FakeEC2:
    def __init__(self, images, error=None):
        self.images = images
        self.error = error
        self.calls = []

    def describe_images(self, **kwargs):
        image_ids = kwargs['ImageIds']
        self.calls.append(list(image_ids))
        if self.error is not None:
            raise self.error
        return {
            'Images': [image for image in self.images if image['ImageId'] in image_ids]
        }


class RecordingContext:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def warning(self, message):
        self.messages.append(message)


def resolve(current_ami, images, error=None):
    context = RecordingContext()
    ec2 = FakeEC2(images, error)
    keep = resolve_compute_ami_keep_keys(
        context=context,
        db=FakeConfigDB(current_ami),
        modules=SCHEDULER_ONLY,
        ami_id=AMI_ID,
        ec2=ec2,
    )
    return keep, context.messages, ec2


def test_a_built_compute_image_newer_than_the_release_is_kept():
    """
    the operator built and adopted this image after the release; the upgrade would discard it
    """
    keep, messages, ec2 = resolve(BUILT_AMI, [NEWER_BUILT_IMAGE, STOCK_IMAGE])

    assert keep == COMPUTE_KEYS
    assert ec2.calls == [[BUILT_AMI, AMI_ID]], 'both images describe in one call'
    assert any('keeping built compute image' in message for message in messages)

    entries_by_key = parse(
        build_ami_update_entries(
            ami_id=AMI_ID, base_os=BASE_OS, modules=SCHEDULER_ONLY, keep_keys=keep
        )
    )
    assert sorted(entries_by_key) == ['scheduler.base_os', 'scheduler.instance_ami']


def test_a_built_compute_image_older_than_the_release_is_replaced():
    keep, messages, _ = resolve(BUILT_AMI, [OLDER_BUILT_IMAGE, STOCK_IMAGE])

    assert keep == set()
    assert any('Custom AMIs page' in message for message in messages)

    entries_by_key = parse(
        build_ami_update_entries(
            ami_id=AMI_ID, base_os=BASE_OS, modules=SCHEDULER_ONLY, keep_keys=keep
        )
    )
    assert entries_by_key['scheduler.compute_node_ami'] == ('string', AMI_ID)


def test_a_stock_compute_image_is_replaced():
    """
    only a build is worth keeping; a stock image the operator pinned by hand is not recognised
    """
    keep, messages, _ = resolve(BUILT_AMI, [ADOPTED_STOCK_IMAGE, STOCK_IMAGE])

    assert keep == set()
    assert messages == []


def test_an_image_that_cannot_be_described_is_replaced():
    """
    a deregistered image fails the describe; the upgrade proceeds and says why
    """
    keep, messages, _ = resolve(
        BUILT_AMI, [STOCK_IMAGE], error=RuntimeError('InvalidAMIID.NotFound')
    )

    assert keep == set()
    assert any('InvalidAMIID.NotFound' in message for message in messages)


def test_the_current_compute_image_is_not_described_when_it_is_already_the_release():
    keep, _, ec2 = resolve(AMI_ID, [STOCK_IMAGE])

    assert keep == set()
    assert ec2.calls == []


def test_keep_built_compute_image_compares_creation_dates():
    assert keep_built_compute_image(NEWER_BUILT_IMAGE, STOCK_IMAGE)
    assert not keep_built_compute_image(OLDER_BUILT_IMAGE, STOCK_IMAGE)
    assert not keep_built_compute_image(ADOPTED_STOCK_IMAGE, STOCK_IMAGE)
    assert not keep_built_compute_image(None, STOCK_IMAGE)
    assert not keep_built_compute_image(NEWER_BUILT_IMAGE, None)

    same_date = dict(NEWER_BUILT_IMAGE, CreationDate=STOCK_IMAGE['CreationDate'])
    assert not keep_built_compute_image(same_date, STOCK_IMAGE), 'equal is not newer'

    undated = dict(NEWER_BUILT_IMAGE)
    undated.pop('CreationDate')
    assert not keep_built_compute_image(undated, STOCK_IMAGE)


# an upgrade moves a module host still on the instance type default this release replaces

HOST_MODULES = [
    {'module_id': 'bastion-host', 'name': constants.MODULE_BASTION_HOST},
    {'module_id': 'vdc', 'name': constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER},
    # a module that templates no host contributes no key
    {'module_id': 'cluster', 'name': constants.MODULE_CLUSTER},
]
OFFERED = [{'InstanceType': MODULE_HOST_INSTANCE_TYPE}]
CHOSEN_TYPE = 'c6i.4xlarge'


class FakeHostConfigDB:
    """the module host instance type settings update_module_host_instance_types reads"""

    def __init__(self, values):
        self.values = values
        self.written = {}

    def get_config_entry(self, key):
        value = self.values.get(key)
        return {'key': key, 'value': value} if value is not None else None

    def set_config_entry(self, key, value):
        self.written[key] = value


class FakeOfferingsEC2:
    def __init__(self, offerings, error=None):
        self.offerings = offerings
        self.error = error
        self.calls = []

    def describe_instance_type_offerings(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {'InstanceTypeOfferings': self.offerings}


def move_hosts(values, offerings, error=None):
    context = RecordingContext()
    db = FakeHostConfigDB(values)
    ec2 = FakeOfferingsEC2(offerings, error)
    update_module_host_instance_types(
        context=context, db=db, modules=HOST_MODULES, ec2=ec2
    )
    return db.written, context.messages, ec2


def test_the_host_move_decision_needs_the_old_default_and_a_region_that_offers_it():
    assert move_module_host_instance_type(
        MODULE_HOST_INSTANCE_TYPE_OLD, [MODULE_HOST_INSTANCE_TYPE]
    )
    # the region does not offer the new type
    assert not move_module_host_instance_type(MODULE_HOST_INSTANCE_TYPE_OLD, [])
    # a type the operator chose is theirs to keep
    assert not move_module_host_instance_type(CHOSEN_TYPE, [MODULE_HOST_INSTANCE_TYPE])
    # the offerings call failed
    assert not move_module_host_instance_type(MODULE_HOST_INSTANCE_TYPE_OLD, None)


def test_hosts_on_the_old_default_move_and_a_chosen_type_is_kept():
    written, messages, ec2 = move_hosts(
        {
            'bastion-host.instance_type': MODULE_HOST_INSTANCE_TYPE_OLD,
            'vdc.controller.autoscaling.instance_type': MODULE_HOST_INSTANCE_TYPE_OLD,
            'vdc.dcv_broker.autoscaling.instance_type': CHOSEN_TYPE,
        },
        OFFERED,
    )

    assert written == {
        'bastion-host.instance_type': MODULE_HOST_INSTANCE_TYPE,
        'vdc.controller.autoscaling.instance_type': MODULE_HOST_INSTANCE_TYPE,
    }
    assert len(ec2.calls) == 1, 'one offerings call for the whole cluster'
    assert ec2.calls[0]['LocationType'] == 'region'
    assert any('keeping vdc.dcv_broker' in message for message in messages)
    assert any('next replaced' in message for message in messages)


def test_no_host_moves_when_the_region_does_not_offer_the_new_type():
    written, messages, _ = move_hosts(
        {'bastion-host.instance_type': MODULE_HOST_INSTANCE_TYPE_OLD}, []
    )

    assert written == {}
    assert any('is not offered in this region' in message for message in messages)


def test_a_failed_offerings_call_leaves_every_host_alone():
    written, messages, _ = move_hosts(
        {'bastion-host.instance_type': MODULE_HOST_INSTANCE_TYPE_OLD},
        OFFERED,
        error=RuntimeError('AuthFailure'),
    )

    assert written == {}
    assert any('AuthFailure' in message for message in messages)


def test_a_cluster_with_no_host_on_the_old_default_makes_no_api_call():
    written, _, ec2 = move_hosts({'bastion-host.instance_type': CHOSEN_TYPE}, OFFERED)

    assert written == {}
    assert ec2.calls == []


def test_host_instance_type_keys_cover_every_templated_host():
    """
    guards against a module adding or renaming a host instance type key
    """
    templates_dir = (
        pathlib.Path(__file__).parents[1] / 'resources' / 'config' / 'templates'
    )
    if not templates_dir.is_dir():
        pytest.skip('config templates not available in this checkout')

    templated = sum(
        len(
            re.findall(
                r'^\s*instance_type:\s*\{\{\s*instance_type\s*\}\}',
                template.read_text(),
                flags=re.MULTILINE,
            )
        )
        for template in templates_dir.rglob('*.yml')
    )
    covered = sum(len(keys) for keys in HOST_INSTANCE_TYPE_KEYS.values())
    assert templated == covered, (
        f'config templates rendering instance_type changed: {templated} found against '
        f'{covered} covered; update HOST_INSTANCE_TYPE_KEYS'
    )
