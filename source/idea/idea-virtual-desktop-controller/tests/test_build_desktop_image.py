"""
ideactl build-desktop-image: option defaulting from cluster config, the eVDI base_os
whitelist, and the --update-stack row update with its reindex call.
"""

from unittest.mock import Mock

import pytest

from ideadatamodel import exceptions
from ideavirtualdesktopcontroller.cli.build_desktop_image import (
    ARCHITECTURE_TO_STACK_KEY,
    DcvHostImageBuilder,
    update_base_stack_row,
)

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
    assert builder.instance_type == 'm6i.large'
    assert (
        builder.instance_profile_arn
        == 'arn:aws:iam::123456789012:instance-profile/dcv-host'
    )
    assert builder.security_group_ids == ['sg-host', 'sg-extra']
    assert builder.subnet_id == 'subnet-a'
    assert builder.ssh_key_pair == 'idea_test'
    assert builder.block_device_name == '/dev/xvda'
    assert builder.ebs_volume_size == 10


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
            context=fake_context(), base_ami='ami-base', base_os='windows2022'
        )


def build_for_row_update(existing_item):
    builder = DcvHostImageBuilder.__new__(DcvHostImageBuilder)
    context = fake_context()
    table = Mock()
    table.get_item.return_value = {'Item': existing_item} if existing_item else {}
    context.aws().dynamodb_table().Table.return_value = table
    builder.context = context
    builder.base_os = 'rocky9'
    return builder, context, table


def test_update_stack_row_updates_and_reindexes():
    builder, context, table = build_for_row_update(
        {'stack_id': 'ss-base-rocky9-x86-64-base'}
    )
    assert update_base_stack_row(context, builder, 'ami-new') is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs['Key'] == {
        'base_os': 'rocky9',
        'stack_id': 'ss-base-rocky9-x86-64-base',
    }
    assert kwargs['ExpressionAttributeValues'][':new_ami_id'] == 'ami-new'
    invoked = context.unix_socket_client.invoke_alt.call_args.kwargs
    assert invoked['namespace'] == 'VirtualDesktopAdmin.ReIndexSoftwareStacks'


def test_update_stack_row_skips_missing_stack():
    builder, context, table = build_for_row_update(None)
    assert update_base_stack_row(context, builder, 'ami-new') is False
    table.update_item.assert_not_called()
    context.unix_socket_client.invoke_alt.assert_not_called()


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
    assert x86.instance_type == 'm6i.large'
    arm = DcvHostImageBuilder(
        context=arm64_context(), base_ami='ami-arm', base_os='rocky9'
    )
    assert arm.instance_type == 'm6g.large'
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
