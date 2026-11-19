"""Test cases for idle and unavailable node termination."""

import pytest
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
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda self, raise_on_error=False: ['job'] * 0,
    )

    result = can_terminate(context, caplog, overrun_minutes=10)

    assert result is True
    assert 'but is not deleted yet' in caplog.text


def test_overdue_log_gate_silent_below_5_minute_overrun(context, caplog, monkeypatch):
    """
    idle overrun under 5 minutes must not trigger the "not deleted yet" log line.
    """
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda self, raise_on_error=False: ['job'] * 0,
    )

    result = can_terminate(context, caplog, overrun_minutes=4)

    assert result is True
    assert 'but is not deleted yet' not in caplog.text


def test_overdue_log_gate_only_on_5_minute_boundary(context, caplog, monkeypatch):
    """
    past the 5 minute overrun threshold, the log line is further gated to a 5 minute
    boundary so it does not fire on every housekeeping cycle.
    """
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda self, raise_on_error=False: ['job'] * 0,
    )

    result = can_terminate(context, caplog, overrun_minutes=11)

    assert result is True
    assert 'but is not deleted yet' not in caplog.text


def test_overdue_log_gate_pending_jobs_skips_termination(context, caplog, monkeypatch):
    """
    a node with jobs still queued against its job group is never a termination
    candidate, regardless of idle overrun.
    """
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda self, raise_on_error=False: ['job'] * 1,
    )

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


def test_node_that_never_reported_is_reclaimed_after_timeout_from_launch(context):
    """
    the live leak: a mom that never reached the server leaves a state-unknown node with no
    state change time, so the unavailable timeout never started and the node ran for hours
    """
    context.config().pop(
        'scheduler.job_provisioning.node_unavailable_timeout_seconds', default=None
    )
    from types import SimpleNamespace

    from ideadatamodel import SocaJob, SocaJobState

    node = build_node(last_used_time=None)
    node.states = [SocaComputeNodeState.DOWN, SocaComputeNodeState.UNKNOWN]
    # its job still exists: one that is gone gives its node up at once
    context.job_cache = SimpleNamespace(
        get_job=lambda job_id: SocaJob(job_id=job_id, state=SocaJobState.HELD)
    )
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    instance = build_instance(0)
    instance._instance['LaunchTime'] = arrow.utcnow().shift(minutes=-31).isoformat()
    assert session._can_terminate(instance=instance, node=node) is True
    instance._instance['LaunchTime'] = arrow.utcnow().shift(minutes=-5).isoformat()
    assert session._can_terminate(instance=instance, node=node) is False


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
    monkeypatch.setattr(
        OpenPBSQSelect,
        'list_jobs_ids',
        lambda self, raise_on_error=False: ['job'] * 0,
    )
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


def test_a_stack_timeout_hold_is_on_record_as_the_platforms(context, monkeypatch):
    """the record is what later tells a qdel of this job from a qdel of the owner's hold"""
    from types import SimpleNamespace

    from ideadatamodel import SocaJob, errorcodes

    calls = []
    monkeypatch.setattr(
        context,
        'scheduler',
        SimpleNamespace(hold_job=lambda job_id: True, set_job_comment=lambda **_: True),
    )
    monkeypatch.setattr(
        context,
        'job_cache',
        SimpleNamespace(set_job_provisioning_error=lambda **kw: calls.append(kw)),
    )
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    session._hold_after_stack_timeout(job=SocaJob(job_id='77'), timeout_secs=1800)
    assert [(c['job_id'], c['error_code']) for c in calls] == [
        ('77', errorcodes.JOB_HELD_STACK_TIMEOUT)
    ]
    assert ',' not in calls[0]['message'], 'qalter splits on commas'


def stack_instance(instance_id, stack='stack-1'):
    return EC2Instance(
        {
            'InstanceId': instance_id,
            'PrivateDnsName': f'{instance_id}.ec2.internal',
            'State': {'Name': 'running'},
            'Tags': [
                {
                    'Key': constants.IDEA_TAG_NODE_TYPE,
                    'Value': constants.NODE_TYPE_COMPUTE,
                },
                {'Key': constants.IDEA_TAG_COMPUTE_STACK, 'Value': stack},
                {'Key': constants.IDEA_TAG_JOB_GROUP, 'Value': 'job-group-1'},
                {'Key': constants.IDEA_TAG_QUEUE_TYPE, 'Value': 'compute'},
            ],
        }
    )


def recording_scheduler(node):
    scheduler = SocaAnyPayload()
    scheduler.states = []
    scheduler.find_node = lambda hosts: node
    scheduler.set_node_state = lambda host, state: scheduler.states.append(
        (host, state)
    )
    return scheduler


def test_a_free_node_that_gained_a_job_since_pass_1_is_not_set_offline(context):
    # NM-04: PBS reports a partly used node as free; only the busy state was checked
    instance = stack_instance('i-gained')
    node = SocaComputeNode(
        host='ip-gained', states=[SocaComputeNodeState.FREE], jobs=['77']
    )
    context.scheduler = recording_scheduler(node)
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    session.auto_scaling_group_instances = {'asg-1': [instance]}
    session.pass4_set_offline()
    assert context.scheduler.states == []


def test_a_stack_whose_target_reaches_0_with_a_busy_member_is_kept(context):
    # NM-06: weighted members brought the target to 0 and the stack was deleted under a job
    idle, busy = stack_instance('i-idle'), stack_instance('i-busy')
    context.scheduler = recording_scheduler(None)
    instance_cache = SocaAnyPayload()
    instance_cache.list_compute_instances = lambda **_: [idle, busy]
    context.instance_cache = instance_cache
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    session.auto_scaling_group_instances = {'asg-1': [idle]}
    session.auto_scaling_group_desired_capacities = {'asg-1': (6, 0)}
    session.pass6_finalize_resources_to_delete()
    assert session.stacks_to_delete == set()
    assert session.instances_to_terminate == set()
    assert context.scheduler.states == [(idle.node_host, SocaComputeNodeState.FREE)]


def test_a_stack_whose_members_are_all_idle_is_deleted(context):
    idle = stack_instance('i-only')
    instance_cache = SocaAnyPayload()
    instance_cache.list_compute_instances = lambda **_: [idle]
    context.instance_cache = instance_cache
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    session.auto_scaling_group_instances = {'asg-1': [idle]}
    session.auto_scaling_group_desired_capacities = {'asg-1': (1, 0)}
    session.pass6_finalize_resources_to_delete()
    assert session.stacks_to_delete == {'stack-1'}


def test_only_one_sweep_runs_and_stop_waits_for_it(context, monkeypatch):
    # NM-07: the running flag was cleared as soon as the thread started
    import threading

    from ideascheduler.app.provisioning.node_monitor.node_house_keeper import (
        NodeHouseKeeper,
    )

    release = threading.Event()
    started = []
    keeper = NodeHouseKeeper.__new__(NodeHouseKeeper)
    keeper._context = context
    keeper._logger = logging.getLogger(LOG_TAG)
    keeper._house_keeping_thread = None
    keeper._invoke_lock = threading.Lock()
    keeper.do_job = lambda: started.append(1) or release.wait(5)
    monkeypatch.setattr(context, 'is_ready', lambda: True)
    keeper.invoke()
    keeper.invoke()
    assert len(started) == 1
    stopper = threading.Thread(target=keeper.stop)
    stopper.start()
    stopper.join(timeout=0.2)
    assert stopper.is_alive(), 'stop returned while the sweep was still running'
    release.set()
    stopper.join(timeout=5)
    assert not stopper.is_alive()


def test_a_target_raised_since_it_was_read_is_kept_when_capacity_is_removed(
    context, monkeypatch
):
    # NM-08: the older, lower absolute target was written back after the provisioner
    # raised it for a new job
    from unittest.mock import Mock

    aws_util = Mock()
    aws_util.autoscaling_describe_auto_scaling_group.return_value = SocaAnyPayload(
        desired_capacity=5
    )
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    monkeypatch.setattr(type(session), 'aws_util', property(lambda self: aws_util))
    monkeypatch.setattr(context, 'metrics', Mock(), raising=False)
    # read 4, one idle unit removed: 3 then; the provisioner has since set 5
    session.auto_scaling_group_update_capacities = {'asg-1': (4, 3)}
    session.auto_scaling_group_info = {'asg-1': ('compute', 'asg-1')}
    session.cleanup()
    aws_util.autoscaling_update_auto_scaling_group.assert_called_once_with(
        auto_scaling_group_name='asg-1', desired_capacity=4
    )


def ephemeral_instance():
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
                {'Key': constants.IDEA_TAG_QUEUE_TYPE, 'Value': 'compute'},
                {'Key': constants.IDEA_TAG_TERMINATE_WHEN_IDLE, 'Value': '0'},
                {'Key': constants.IDEA_TAG_KEEP_FOREVER, 'Value': 'false'},
            ],
        }
    )


def test_the_node_of_a_live_job_missing_from_the_cache_is_kept(context, monkeypatch):
    # NM-14: a queue that failed to sync at start-up left its jobs out of the cache, and
    # their nodes were reclaimed
    job_cache = SocaAnyPayload()
    job_cache.get_job = lambda job_id: None
    monkeypatch.setattr(context, 'job_cache', job_cache, raising=False)
    scheduler = SocaAnyPayload()
    scheduler.is_job_active = lambda job_id: True
    context.scheduler = scheduler
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    node = build_node(last_used_time=arrow.utcnow().shift(days=-1).datetime)
    instance = ephemeral_instance()
    assert instance.is_soca_ephemeral_capacity
    assert session._can_terminate(instance=instance, node=node) is False


@pytest.mark.parametrize('pbs', ['live', 'unreadable'])
def test_the_down_node_of_a_live_job_missing_from_the_cache_waits(
    context, monkeypatch, pbs
):
    # verify-scheduler #4 (NM-14 remainder): a down or unknown node of a job the cache
    # missed was retired at once, without asking PBS or waiting out the time-out
    job_cache = SocaAnyPayload()
    job_cache.get_job = lambda job_id: None
    monkeypatch.setattr(context, 'job_cache', job_cache, raising=False)

    def is_job_active(job_id):
        if pbs == 'unreadable':
            raise RuntimeError('qstat failed')
        return True

    scheduler = SocaAnyPayload()
    scheduler.is_job_active = is_job_active
    context.scheduler = scheduler
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    # down a minute ago: well inside the time-out
    node = build_node(last_used_time=arrow.utcnow().shift(minutes=-1).datetime)
    node.states = [SocaComputeNodeState.DOWN]
    node.last_state_changed_time = arrow.utcnow().shift(minutes=-1).datetime
    instance = ephemeral_instance()
    assert session._can_terminate(instance=instance, node=node) is False


def test_a_node_spot_price_is_read_as_its_amount():
    # PBS-32: the amount was the boolean is_float returned
    node = OpenPBSConverter(
        context=None, logger=logging.getLogger(LOG_TAG)
    ).to_soca_node(
        host='ip-10-0-0-1',
        node={'resources_available': {'spot_price': '0.45'}, 'state': 'free'},
    )
    assert node.spot_price.amount == 0.45


def test_a_failed_pending_jobs_count_keeps_the_node(context, monkeypatch):
    # NM-16: a failed qselect read as "no jobs waiting" and the node was terminated
    def fails(self, raise_on_error=False):
        if raise_on_error:
            raise Exception('qselect failed')
        return []

    monkeypatch.setattr(OpenPBSQSelect, 'list_jobs_ids', fails)
    node = build_node(last_used_time=arrow.utcnow().shift(minutes=-20).datetime)
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    assert session._can_terminate(instance=build_instance(5), node=node) is False


def test_a_kept_candidate_is_set_back_online_under_the_name_it_went_offline_under(
    context,
):
    # B-S6: an older node registered under its private dns name stayed offline
    idle, busy = stack_instance('i-old'), stack_instance('i-busy')
    context.scheduler = recording_scheduler(None)
    instance_cache = SocaAnyPayload()
    instance_cache.list_compute_instances = lambda **_: [idle, busy]
    context.instance_cache = instance_cache
    session = NodeHouseKeepingSession(
        context=context, logger=logging.getLogger(LOG_TAG)
    )
    session.offline_hosts = {'i-old': 'ip-10-0-0-9.ec2.internal'}
    session.auto_scaling_group_instances = {'asg-1': [idle]}
    session.auto_scaling_group_desired_capacities = {'asg-1': (6, 0)}
    session.pass6_finalize_resources_to_delete()
    assert context.scheduler.states == [
        ('ip-10-0-0-9.ec2.internal', SocaComputeNodeState.FREE)
    ]
