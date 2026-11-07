"""
An instance whose queue was deleted while it launched cannot become a node: qmgr refuses the
create on every scan. The monitor asks first, says so once per instance, and runs no create.
"""

from unittest.mock import MagicMock

import pytest

from ideadatamodel import EC2Instance, constants, errorcodes, exceptions
from ideascheduler.app.provisioning.node_monitor.node_monitor import NodeMonitor
from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import OpenPBSScheduler

QUEUE = 'iv-45971021d6cd'
HOST = '10.4.133.96'


def _instance(instance_id='i-0375d81540db0ff08'):
    return EC2Instance(
        {
            'InstanceId': instance_id,
            'InstanceType': 'c7i.large',
            'LaunchTime': '2026-10-21T18:48:11+00:00',
            'PrivateIpAddress': HOST,
            'State': {'Name': 'running'},
            'Tags': [
                {
                    'Key': constants.IDEA_TAG_NODE_TYPE,
                    'Value': constants.NODE_TYPE_COMPUTE,
                },
                {'Key': constants.IDEA_TAG_CLUSTER_NAME, 'Value': 'idea-mock'},
                {'Key': constants.IDEA_TAG_JOB_ID, 'Value': '216'},
                {'Key': constants.IDEA_TAG_JOB_GROUP, 'Value': 'group'},
                {'Key': constants.IDEA_TAG_JOB_QUEUE, 'Value': QUEUE},
                {'Key': constants.IDEA_TAG_QUEUE_TYPE, 'Value': QUEUE},
                {'Key': constants.IDEA_TAG_COMPUTE_STACK, 'Value': 'idea-mock-stack'},
            ],
        }
    )


def _monitor():
    monitor = NodeMonitor.__new__(NodeMonitor)
    monitor._context = MagicMock()
    monitor._context.cluster_name.return_value = 'idea-mock'
    monitor._context.scheduler.find_node.return_value = None
    monitor._context.aws_util.return_value.get_soca_job_from_stack.return_value = None
    monitor._logger = MagicMock()
    monitor._queueless_instances = set()
    return monitor


def test_an_instance_without_a_queue_is_not_created_and_warns_once():
    monitor = _monitor()
    monitor._context.scheduler.get_queue.return_value = None
    for _ in range(3):
        monitor._check_and_provision_node(_instance())
    monitor._context.scheduler.create_node.assert_not_called()
    monitor._logger.exception.assert_not_called()
    assert monitor._logger.warning.call_count == 1
    assert QUEUE in monitor._logger.warning.call_args.args[0]
    monitor._context.scheduler.get_queue.assert_called_with(QUEUE)


def test_each_instance_gets_its_own_warning():
    monitor = _monitor()
    monitor._context.scheduler.get_queue.return_value = None
    monitor._check_and_provision_node(_instance('i-0000000000000000a'))
    monitor._check_and_provision_node(_instance('i-0000000000000000b'))
    assert monitor._logger.warning.call_count == 2


def test_a_queue_that_exists_creates_the_node():
    monitor = _monitor()
    monitor._context.scheduler.get_queue.return_value = MagicMock()
    monitor._check_and_provision_node(_instance())
    monitor._context.scheduler.create_node.assert_called_once()
    monitor._logger.warning.assert_not_called()


def test_a_failed_queue_lookup_is_an_error_not_a_missing_queue():
    monitor = _monitor()
    monitor._context.scheduler.get_queue.side_effect = exceptions.SocaException(
        error_code=errorcodes.SCHEDULER_ERROR, message='connection refused'
    )
    monitor._check_and_provision_node(_instance())
    monitor._context.scheduler.create_node.assert_not_called()
    monitor._logger.exception.assert_called_once()
    assert not monitor._queueless_instances


@pytest.mark.parametrize(
    ('error_code', 'message', 'missing'),
    [
        (errorcodes.SCHEDULER_QUEUE_NOT_FOUND, 'qstat: Unknown queue', True),
        (errorcodes.SCHEDULER_ERROR, f'Unknown queue {QUEUE}', True),
        (errorcodes.SCHEDULER_ERROR, 'connection refused', False),
    ],
)
def test_get_queue_answers_none_only_for_a_missing_queue(error_code, message, missing):
    # qstat's unknown-queue status arrives as SCHEDULER_QUEUE_NOT_FOUND, which used to escape
    scheduler = OpenPBSScheduler.__new__(OpenPBSScheduler)
    scheduler.list_queues = MagicMock(
        side_effect=exceptions.SocaException(error_code=error_code, message=message)
    )
    if missing:
        assert scheduler.get_queue(QUEUE) is None
    else:
        with pytest.raises(exceptions.SocaException):
            scheduler.get_queue(QUEUE)
