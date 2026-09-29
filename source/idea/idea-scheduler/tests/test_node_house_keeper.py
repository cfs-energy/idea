"""Test cases for idle and unavailable node termination."""

import logging

import arrow

from ideadatamodel import (
    EC2Instance,
    SocaAnyPayload,
    SocaComputeNode,
    SocaComputeNodeState,
    constants,
)
from ideascheduler.app.provisioning.node_monitor.node_house_keeper import (
    NodeHouseKeepingSession,
)
from ideascheduler.app.scheduler.openpbs.openpbs_converter import OpenPBSConverter
from ideascheduler.app.scheduler.openpbs.openpbs_qselect import OpenPBSQSelect

LOG_TAG = 'test_node_house_keeper'


def build_instance(terminate_when_idle: int, keep_forever: bool = False) -> EC2Instance:
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
                {
                    'Key': constants.IDEA_TAG_KEEP_FOREVER,
                    'Value': str(keep_forever).lower(),
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


def test_openpbs_converter_preserves_unknown_node_state():
    states = OpenPBSConverter.to_soca_compute_node_state('down,unknown')

    assert set(states) == {SocaComputeNodeState.DOWN, SocaComputeNodeState.UNKNOWN}


def test_unavailable_node_is_reclaimed_after_timeout(context, caplog):
    context.config().pop(
        'scheduler.job_provisioning.node_unavailable_timeout_seconds', default=None
    )
    node = build_node(last_used_time=None)
    node.states = [SocaComputeNodeState.DOWN, SocaComputeNodeState.UNKNOWN]
    node.last_state_changed_time = arrow.utcnow().shift(minutes=-31).datetime
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )

    with caplog.at_level(logging.INFO, logger=LOG_TAG):
        result = session._can_terminate(instance=build_instance(3), node=node)

    assert result is True
    assert 'scheduler reported node state down,unknown' in caplog.text


def test_unavailable_node_is_retained_before_timeout(context):
    context.config().put(
        'scheduler.job_provisioning.node_unavailable_timeout_seconds', 3600
    )
    node = build_node(last_used_time=None)
    node.states = [SocaComputeNodeState.STALE_UNKNOWN]
    node.last_state_changed_time = arrow.utcnow().shift(minutes=-29).datetime
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )

    assert session._can_terminate(instance=build_instance(3), node=node) is False


def test_unavailable_keep_forever_node_is_retained(context):
    node = build_node(last_used_time=None)
    node.states = [SocaComputeNodeState.DOWN]
    node.last_state_changed_time = arrow.utcnow().shift(days=-1).datetime
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )

    assert (
        session._can_terminate(instance=build_instance(0, keep_forever=True), node=node)
        is False
    )


def test_unavailable_node_with_jobs_is_never_a_candidate(context, monkeypatch):
    node = SocaComputeNode(
        host='ip-10-0-0-1',
        states=[SocaComputeNodeState.DOWN, SocaComputeNodeState.JOB_BUSY],
        cluster_name=context.cluster_name(),
        queue_type='compute',
        instance_id='i-0123456789abcdef0',
        compute_stack='compute-stack',
        jobs=['1034'],
    )
    scheduler = SocaAnyPayload()
    scheduler.list_nodes = lambda: [node]
    context.scheduler = scheduler
    instance_cache = SocaAnyPayload()
    instance_cache.get_instance = lambda **_: build_instance(3)
    context.instance_cache = instance_cache
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    monkeypatch.setattr(session, '_publish_node_metrics', lambda **_: None)
    monkeypatch.setattr(session, '_publish_instance_metrics', lambda **_: None)
    checked = []
    monkeypatch.setattr(
        session,
        '_can_terminate',
        lambda **kwargs: checked.append(kwargs['node']) or False,
    )

    session.pass1_identify_potential_candidates_for_deletion()

    assert checked == []


def test_keep_forever_batch_node_still_terminates_when_idle(context, monkeypatch):
    monkeypatch.setattr(OpenPBSQSelect, 'get_count', lambda self: 0)
    node = build_node(last_used_time=arrow.utcnow().shift(minutes=-20).datetime)
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )

    assert (
        session._can_terminate(instance=build_instance(5, keep_forever=True), node=node)
        is True
    )


def test_keep_forever_node_without_idle_termination_is_retained(context):
    node = build_node(last_used_time=arrow.utcnow().shift(days=-1).datetime)
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )

    assert (
        session._can_terminate(instance=build_instance(0, keep_forever=True), node=node)
        is False
    )
