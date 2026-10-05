"""
The compute builder gives up on a builder instance that never reports ready, and routes
that through keep_for_inspection so the instance is stopped instead of billing forever.
"""

from unittest.mock import MagicMock

import json
import pytest

from ideadatamodel import exceptions
from ideascheduler.app.images import compute_node_ami_builder as module
from ideascheduler.app.images.compute_node_ami_builder import ComputeNodeAmiBuilder


def test_wait_for_software_packages_gives_up_after_the_deadline(monkeypatch):
    builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
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


@pytest.mark.parametrize('state', ['failed', 'error', 'invalid', 'deregistered'])
def test_wait_for_image_raises_on_any_terminal_state_but_available(monkeypatch, state):
    builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
    builder.context = MagicMock()
    builder.context.aws().ec2().describe_images.return_value = {
        'Images': [{'ImageId': 'ami-dead', 'State': state}]
    }
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    with pytest.raises(exceptions.SocaException) as exc_info:
        builder.wait_for_image('ami-dead')
    assert f'ended in state {state}' in exc_info.value.message


def test_wait_for_image_returns_on_available_and_rides_out_a_throttle(monkeypatch):
    from botocore.exceptions import ClientError

    builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
    builder.context = MagicMock()
    answers = iter(
        [
            ClientError({'Error': {'Code': 'RequestLimitExceeded'}}, 'DescribeImages'),
            {'Images': [{'ImageId': 'ami-ok', 'State': 'pending'}]},
            {'Images': [{'ImageId': 'ami-ok', 'State': 'available'}]},
        ]
    )

    def describe_images(ImageIds):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    builder.context.aws().ec2().describe_images.side_effect = describe_images
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    builder.wait_for_image('ami-ok')


@pytest.mark.parametrize('status', ['failed:kernel_default', 'failed', 'ready'])
def test_builder_requires_exact_complete_status(status):
    builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
    builder.context = MagicMock()
    builder.context.aws().ec2().describe_instances.return_value = {
        'Reservations': [
            {
                'Instances': [
                    {
                        'InstanceId': 'i-builder',
                        'Tags': [
                            {'Key': 'idea:AmiBuilderStatus', 'Value': status},
                        ],
                    }
                ]
            }
        ]
    }
    # the tag comes back as reported; the checks read decides, with the failing detail
    assert builder.wait_for_software_packages('i-builder') == status
    builder.context.aws().ssm().get_command_invocation.return_value = {
        'Status': 'Success',
        'StandardOutputContent': json.dumps(
            {'release': module.__version__, 'checks': [{'name': 'ssm', 'ok': True}]}
        ),
    }
    with pytest.raises(exceptions.SocaException, match='complete is required'):
        builder.read_bake_checks('i-builder', builder_status=status)
    builder.context.aws().ssm().get_command_invocation.return_value = {
        'Status': 'Success',
        'StandardOutputContent': json.dumps(
            {
                'release': module.__version__,
                'checks': [
                    {'name': 'bootstrap', 'ok': False, 'detail': 'a command failed: x'}
                ],
            }
        ),
    }
    with pytest.raises(
        exceptions.SocaException,
        match='in-bake check bootstrap failed on builder i-builder: a command failed: x',
    ):
        builder.read_bake_checks('i-builder', builder_status=status)


@pytest.mark.parametrize(
    'release,checks',
    [
        ('old', [{'name': 'kernel_default', 'ok': True}]),
        (module.__version__, []),
        (module.__version__, [{'name': 'kernel_default', 'ok': False}]),
    ],
)
def test_in_bake_evidence_rejects_missing_failed_or_wrong_release(release, checks):
    import json

    builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
    builder.context = MagicMock()
    builder.context.aws().ssm().get_command_invocation.return_value = {
        'Status': 'Success',
        'StandardOutputContent': json.dumps({'release': release, 'checks': checks}),
    }
    with pytest.raises(exceptions.SocaException, match='in-bake check'):
        builder.read_bake_checks('i-builder')


def test_in_bake_evidence_is_copied_before_snapshot():
    import json

    builder = ComputeNodeAmiBuilder.__new__(ComputeNodeAmiBuilder)
    builder.context = MagicMock()
    builder.context.aws().ssm().get_command_invocation.return_value = {
        'Status': 'Success',
        'StandardOutputContent': json.dumps(
            {
                'release': module.__version__,
                'checks': [{'name': 'kernel_default', 'ok': True, 'seconds': 3}],
            }
        ),
    }
    progress = MagicMock()
    checks = builder.read_bake_checks('i-builder', progress)
    assert checks[0].ok is True
    assert checks[0].seconds == 3
    progress.assert_called_once_with({'checks': checks})
