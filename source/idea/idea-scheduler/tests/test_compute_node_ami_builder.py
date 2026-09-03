"""
The compute builder gives up on a builder instance that never reports ready, and routes
that through keep_for_inspection so the instance is stopped instead of billing forever.
"""

from unittest.mock import MagicMock

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
