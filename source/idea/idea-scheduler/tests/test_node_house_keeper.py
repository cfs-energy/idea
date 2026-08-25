#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
Test Cases for NodeHouseKeepingSession's idle-overrun logging gate

_can_terminate() logs an info line once a node is overdue for idle termination by more
than 5 minutes, repeating every 5 minutes after that. The gate is built from
Utils.minutes(seconds=...), called both positionally and by keyword at different call
sites in the same method - a fragile mix that these tests pin down.
"""

import logging

import arrow

from ideadatamodel import EC2Instance, SocaComputeNode, constants
from ideascheduler.app.provisioning.node_monitor.node_house_keeper import (
    NodeHouseKeepingSession,
)
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect

LOG_TAG = 'test_node_house_keeper'


def build_instance(terminate_when_idle: int) -> EC2Instance:
    return EC2Instance(
        {
            'InstanceId': 'i-0123456789abcdef0',
            'PrivateDnsName': 'ip-10-0-0-1.ec2.internal',
            'Tags': [
                {
                    'Key': constants.IDEA_TAG_NODE_TYPE,
                    'Value': constants.NODE_TYPE_COMPUTE,
                },
                {'Key': constants.IDEA_TAG_JOB_ID, 'Value': '1034'},
                {'Key': constants.IDEA_TAG_JOB_GROUP, 'Value': 'job-group-1'},
                {'Key': constants.IDEA_TAG_QUEUE_TYPE, 'Value': 'compute'},
                {
                    'Key': constants.IDEA_TAG_TERMINATE_WHEN_IDLE,
                    'Value': str(terminate_when_idle),
                },
            ],
        }
    )


def build_node(last_used_time) -> SocaComputeNode:
    return SocaComputeNode(
        host='ip-10-0-0-1',
        instance_id='i-0123456789abcdef0',
        queue_type='compute',
        job_group='job-group-1',
        last_used_time=last_used_time,
    )


def can_terminate(context, caplog, overrun_minutes: int, terminate_when_idle: int = 5):
    """
    builds a node last used `terminate_when_idle + overrun_minutes` minutes ago and
    invokes _can_terminate(), with no jobs pending on the queue.
    """
    last_used_time = arrow.utcnow().shift(
        minutes=-(terminate_when_idle + overrun_minutes)
    )
    instance = build_instance(terminate_when_idle=terminate_when_idle)
    node = build_node(last_used_time=last_used_time.datetime)

    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    with caplog.at_level(logging.INFO, logger=LOG_TAG):
        result = session._can_terminate(instance=instance, node=node)
    return result


def test_overdue_log_gate_fires_past_5_minute_overrun(context, caplog, monkeypatch):
    """
    once idle overrun exceeds 5 minutes, the "not deleted yet" info log must fire
    """
    monkeypatch.setattr(OpenPBSQSelect, 'get_count', lambda self: 0)

    result = can_terminate(context, caplog, overrun_minutes=10)

    assert result is True
    assert 'but is not deleted yet' in caplog.text


def test_overdue_log_gate_silent_below_5_minute_overrun(context, caplog, monkeypatch):
    """
    idle overrun under 5 minutes must not trigger the "not deleted yet" log line.
    """
    monkeypatch.setattr(OpenPBSQSelect, 'get_count', lambda self: 0)

    result = can_terminate(context, caplog, overrun_minutes=4)

    assert result is True
    assert 'but is not deleted yet' not in caplog.text


def test_overdue_log_gate_only_on_5_minute_boundary(context, caplog, monkeypatch):
    """
    past the 5 minute overrun threshold, the log line is further gated to a 5 minute
    boundary so it does not fire on every housekeeping cycle.
    """
    monkeypatch.setattr(OpenPBSQSelect, 'get_count', lambda self: 0)

    result = can_terminate(context, caplog, overrun_minutes=11)

    assert result is True
    assert 'but is not deleted yet' not in caplog.text


def test_overdue_log_gate_pending_jobs_skips_termination(context, caplog, monkeypatch):
    """
    a node with jobs still queued against its job group is never a termination
    candidate, regardless of idle overrun.
    """
    monkeypatch.setattr(OpenPBSQSelect, 'get_count', lambda self: 1)

    result = can_terminate(context, caplog, overrun_minutes=10)

    assert result is False
    assert 'but is not deleted yet' not in caplog.text
