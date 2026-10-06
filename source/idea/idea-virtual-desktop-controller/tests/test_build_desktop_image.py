"""
ideactl build-desktop-image: option defaulting from cluster config, the eVDI base_os
whitelist, the builder status handshake (only 'complete' is snapshotted) and the stack
row update gate (ami_id moves only to a validated image).
"""

from unittest.mock import Mock

import pytest

from ideadatamodel import exceptions
from ideavirtualdesktopcontroller.cli.build_desktop_image import (
    ARCHITECTURE_TO_STACK_KEY,
    DcvHostImageBuilder,
)
from ideavirtualdesktopcontroller.cli.software_stacks import update_software_stack_ami

CONFIG = {
    'virtual-desktop-controller.dcv_host_instance_profile_arn': 'arn:aws:iam::123456789012:instance-profile/dcv-host',
    'virtual-desktop-controller.dcv_host_security_group_id': 'sg-host',
    'virtual-desktop-controller.dcv_session.additional_security_groups': ['sg-extra'],
    'virtual-desktop-controller.dcv_session.network.private_subnets': [],
    'cluster.network.private_subnets': ['subnet-a', 'subnet-b'],
    'cluster.network.ssh_key_pair': 'idea_test',
    'cluster.cluster_name': 'idea-test',
}


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get_string(self, key, required=False, default=None):
        value = self.values.get(key, default)
        if required and value is None:
            raise AssertionError(f'missing required config: {key}')
        return value

    def get_list(self, key, required=False, default=None):
        value = self.values.get(key)
        if value is None:
            value = default
        if required and value is None:
            raise AssertionError(f'missing required config: {key}')
        return list(value) if value is not None else value


def fake_context():
    context = Mock()
    context.config.return_value = FakeConfig(CONFIG)
    context.module_id.return_value = 'vdc'
    context.module_name.return_value = 'virtual-desktop-controller'
    context.module_set.return_value = 'default'
    context.cluster_name.return_value = 'idea-test'
    context.aws().ec2().describe_images.return_value = {
        'Images': [
            {
                'ImageId': 'ami-base',
                'Architecture': 'x86_64',
                'BlockDeviceMappings': [
                    {'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 10}}
                ],
            }
        ]
    }
    return context


def test_defaults_come_from_cluster_config():
    builder = DcvHostImageBuilder(
        context=fake_context(), base_ami='ami-base', base_os='amazonlinux2023'
    )
    assert builder.ami_name == 'idea-dcv-host-amazonlinux2023'
    assert builder.get_ami_full_name().startswith('idea-dcv-host-amazonlinux2023-v')
    assert builder.instance_type == 'm7i.large'
    assert (
        builder.instance_profile_arn
        == 'arn:aws:iam::123456789012:instance-profile/dcv-host'
    )
    assert builder.security_group_ids == ['sg-host', 'sg-extra']
    assert builder.subnet_id == 'subnet-a'
    assert builder.ssh_key_pair == 'idea_test'
    assert builder.block_device_name == '/dev/xvda'
    assert builder.ebs_volume_size == 40


def test_a_smaller_request_uses_the_root_snapshot_and_the_floor():
    context = fake_context()
    context.aws().ec2().describe_images.return_value = {
        'Images': [
            {
                'ImageId': 'ami-base',
                'Architecture': 'x86_64',
                'RootDeviceName': '/dev/sda1',
                'BlockDeviceMappings': [
                    {'DeviceName': '/dev/sdb', 'Ebs': {'VolumeSize': 8}},
                    {'DeviceName': '/dev/sda1', 'Ebs': {'VolumeSize': 11}},
                ],
            }
        ]
    }
    builder = DcvHostImageBuilder(
        context=context, base_ami='ami-base', base_os='rocky8', ebs_volume_size=10
    )
    assert builder.block_device_name == '/dev/sda1'
    assert builder.ebs_volume_size >= 11
    kept = DcvHostImageBuilder(
        context=context, base_ami='ami-base', base_os='rocky8', ebs_volume_size=100
    )
    assert kept.ebs_volume_size == 100


def test_vdi_subnets_win_over_cluster_subnets():
    context = fake_context()
    context.config.return_value = FakeConfig(
        {
            **CONFIG,
            'virtual-desktop-controller.dcv_session.network.private_subnets': [
                'subnet-vdi'
            ],
        }
    )
    builder = DcvHostImageBuilder(
        context=context, base_ami='ami-base', base_os='amazonlinux2023'
    )
    assert builder.subnet_id == 'subnet-vdi'


def test_unsupported_base_os_is_rejected():
    with pytest.raises(exceptions.SocaException):
        DcvHostImageBuilder(
            context=fake_context(), base_ami='ami-base', base_os='rocky10'
        )
    with pytest.raises(exceptions.SocaException):
        DcvHostImageBuilder(
            context=fake_context(), base_ami='ami-base', base_os='windows2016'
        )


def row_table(item):
    table = Mock()
    table.get_item.return_value = {'Item': item}
    return table


def test_stack_row_moves_ami_only_to_a_validated_image():
    table = row_table({'ami_id': 'ami-old'})
    assert (
        update_software_stack_ami(
            table,
            'ss-base-rocky9-x86-64-base',
            'rocky9',
            'ami-new',
            Mock(),
            base_ami_id='ami-stock',
            validated={'ami-new'},
        )
        == 'ami'
    )
    values = table.update_item.call_args.kwargs['ExpressionAttributeValues']
    assert values[':new_ami_id'] == 'ami-new'
    assert values[':base_ami_id'] == 'ami-stock'


def test_stack_row_refuses_an_unvalidated_build():
    table = row_table({'ami_id': 'ami-old'})
    assert (
        update_software_stack_ami(
            table,
            'ss-base-rocky9-x86-64-base',
            'rocky9',
            'ami-built',
            Mock(),
            base_ami_id='ami-stock',
        )
        is False
    )
    table.update_item.assert_not_called()


def test_a_stock_refresh_moves_only_the_base():
    table = row_table({'ami_id': 'ami-old'})
    assert (
        update_software_stack_ami(
            table,
            'ss-base-rocky9-x86-64-base',
            'rocky9',
            'ami-stock2',
            Mock(),
            keep_built=True,
        )
        == 'base'
    )
    kwargs = table.update_item.call_args.kwargs
    assert '#ami_id' not in kwargs['ExpressionAttributeNames']
    assert kwargs['ExpressionAttributeValues'][':base_ami_id'] == 'ami-stock2'


def test_a_pinned_stack_row_never_moves_its_ami():
    table = row_table({'ami_id': 'ami-old', 'image_pinned': True})
    assert (
        update_software_stack_ami(
            table,
            'ss-base-rocky9-x86-64-base',
            'rocky9',
            'ami-new',
            Mock(),
            validated={'ami-new'},
        )
        is False
    )


def builder_with_status(monkeypatch, windows=False):
    from unittest.mock import MagicMock

    builder = DcvHostImageBuilder.__new__(DcvHostImageBuilder)
    builder.context = MagicMock()
    builder.base_os = 'windows2022' if windows else 'rocky9'
    seen = []
    builder.before_snapshot = lambda instance_id, status: seen.append(status)
    return builder, seen


def test_only_complete_is_snapshotted(monkeypatch):
    builder, seen = builder_with_status(monkeypatch)
    with pytest.raises(exceptions.SocaException) as exc_info:
        builder.check_builder_status('i-1', 'failed:lustre_module')
    assert 'in-bake check lustre_module failed' in exc_info.value.message
    assert seen == ['failed:lustre_module']
    builder.check_builder_status('i-1', 'complete')
    assert seen[-1] == 'complete'


def test_a_windows_builder_is_finalized_and_snapshotted_only_once_stopped(monkeypatch):
    from ideavirtualdesktopcontroller.app.software_stacks import (
        dcv_host_image_builder as module,
    )

    builder, _ = builder_with_status(monkeypatch, windows=True)
    states = iter(['running', 'stopping', 'stopped'])
    builder.context.aws().ec2().describe_instances.side_effect = lambda **_: {
        'Reservations': [
            {
                'Instances': [
                    {
                        'State': {'Name': next(states)},
                        'Tags': [{'Key': 'idea:AmiBuilderStatus', 'Value': 'complete'}],
                    }
                ]
            }
        ]
    }
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    builder.check_builder_status('i-win', 'complete')
    command = builder.context.aws().ssm().send_command.call_args.kwargs
    assert command['Parameters']['commands'][0].endswith('Setup.ps1 -Finalize')


def test_a_windows_builder_that_fails_while_finalizing_is_not_snapshotted(monkeypatch):
    from ideavirtualdesktopcontroller.app.software_stacks import (
        dcv_host_image_builder as module,
    )

    builder, _ = builder_with_status(monkeypatch, windows=True)
    builder.context.aws().ec2().describe_instances.return_value = {
        'Reservations': [
            {
                'Instances': [
                    {
                        'State': {'Name': 'running'},
                        'Tags': [
                            {'Key': 'idea:AmiBuilderStatus', 'Value': 'failed:sysprep'}
                        ],
                    }
                ]
            }
        ]
    }
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    with pytest.raises(exceptions.SocaException) as exc_info:
        builder.check_builder_status('i-win', 'complete')
    assert 'failed:sysprep' in exc_info.value.message


def test_architecture_map_covers_ec2_values():
    assert ARCHITECTURE_TO_STACK_KEY == {'x86_64': 'x86-64', 'arm64': 'arm64'}


def test_wait_for_software_packages_gives_up_after_the_deadline(monkeypatch):
    from unittest.mock import MagicMock

    from ideavirtualdesktopcontroller.app.software_stacks import (
        dcv_host_image_builder as module,
    )

    builder = DcvHostImageBuilder.__new__(DcvHostImageBuilder)
    builder.context = MagicMock()
    builder.context.aws().ec2().describe_instances.return_value = {
        'Reservations': [{'Instances': [{'InstanceId': 'i-stuck', 'Tags': []}]}]
    }
    clock = iter([0, 100, 5000])
    monkeypatch.setattr(module.time, 'time', lambda: next(clock))
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)

    with pytest.raises(exceptions.SocaException) as exc_info:
        builder.wait_for_software_packages('i-stuck')
    assert 'did not report ready within 60 minutes' in exc_info.value.message


def test_wait_for_image_refuses_a_failed_ami(monkeypatch):
    from unittest.mock import MagicMock

    from ideavirtualdesktopcontroller.app.software_stacks import (
        dcv_host_image_builder as module,
    )

    builder = DcvHostImageBuilder.__new__(DcvHostImageBuilder)
    builder.context = MagicMock()
    builder.context.aws().ec2().describe_images.return_value = {
        'Images': [{'ImageId': 'ami-dead', 'State': 'failed'}]
    }
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    with pytest.raises(exceptions.SocaException) as exc_info:
        builder.wait_for_image('ami-dead')
    assert 'ended in state failed' in exc_info.value.message


def arm64_context():
    context = fake_context()
    context.aws().ec2().describe_images.return_value = {
        'Images': [
            {
                'ImageId': 'ami-arm',
                'Architecture': 'arm64',
                'BlockDeviceMappings': [
                    {'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 10}}
                ],
            }
        ]
    }
    return context


def test_the_builder_type_follows_the_image_architecture():
    x86 = DcvHostImageBuilder(
        context=fake_context(), base_ami='ami-base', base_os='rocky9'
    )
    assert x86.instance_type == 'm7i.large'
    arm = DcvHostImageBuilder(
        context=arm64_context(), base_ami='ami-arm', base_os='rocky9'
    )
    assert arm.instance_type == 'm8g.large'
    assert arm.architecture == 'arm64'


def test_an_x86_64_builder_type_is_refused_for_an_arm64_image():
    with pytest.raises(exceptions.SocaException) as exc_info:
        DcvHostImageBuilder(
            context=arm64_context(),
            base_ami='ami-arm',
            base_os='rocky9',
            instance_type='m6i.large',
        )
    assert 'm6i.large is x86_64' in exc_info.value.message


def _build_ready(monkeypatch, base_os):
    """a builder that has already launched and reported complete, with AWS calls recorded"""
    from unittest.mock import MagicMock

    from ideavirtualdesktopcontroller.app.software_stacks import (
        dcv_host_image_builder as builder_module,
    )
    from ideasdk.aws import image_builds as images

    builder = DcvHostImageBuilder.__new__(DcvHostImageBuilder)
    builder.context = MagicMock()
    builder.base_os = base_os
    builder.no_reboot = False
    builder.stop = False
    builder.terminate = True
    builder.progress = None
    builder.get_image_by_name = lambda: None
    builder.get_ami_full_name = lambda: 'sample-image'
    builder.get_ami_dir = lambda: '/tmp/ami'
    instance = Mock(instance_id='i-builder', private_ip_address='10.0.0.5')
    builder.launch_ec2_instance = lambda: instance
    builder.wait_for_software_packages = lambda instance_id: 'complete'
    builder.check_builder_status = lambda instance_id, status: None
    builder.wait_for_image = lambda image_id: None
    order = []

    def stop_instances(InstanceIds):
        order.append('stop')

    def create_image(instance_id):
        order.append(('create', builder.no_reboot))
        return 'ami-new'

    builder.create_image = create_image
    builder.context.aws().ec2().stop_instances.side_effect = stop_instances
    states = iter(['running', 'stopped'])

    def describe_instances(InstanceIds):
        return {'Reservations': [{'Instances': [{'State': {'Name': next(states)}}]}]}

    builder.context.aws().ec2().describe_instances.side_effect = describe_instances
    monkeypatch.setattr(builder_module.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(images.time, 'sleep', lambda seconds: None)
    return builder, order


@pytest.mark.parametrize(
    'base_os', ['ubuntu2204', 'amazonlinux2023', 'rhel9', 'rocky8']
)
def test_linux_snapshot_stops_the_builder_and_does_not_reboot(monkeypatch, base_os):
    builder, order = _build_ready(monkeypatch, base_os)
    assert builder.build() == 'ami-new'
    assert order == ['stop', ('create', True)]


def test_windows_snapshot_does_not_reboot_a_stopped_builder(monkeypatch):
    builder, order = _build_ready(monkeypatch, 'windows2022')
    assert builder.build() == 'ami-new'
    assert order == [('create', True)]
