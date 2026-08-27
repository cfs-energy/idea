"""
Test Cases for compute node identity

compute nodes are registered with the scheduler by private ipv4, because they get no
/etc/hosts entry and their private dns name does not resolve unless the VPC uses the
Route53 resolver. a cluster upgraded from an earlier release still carries nodes
registered under the private dns name, so lookups answer to both names while those
nodes drain, and state changes always target the name the scheduler answered with.
"""

import logging

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
from ideascheduler.app.scheduler.openpbs.openpbs_scheduler import OpenPBSScheduler

LOG_TAG = 'test_node_identity'

PRIVATE_IP = '10.0.79.226'
LEGACY_HOST = 'ip-10-0-79-226'
PRIVATE_DNS_NAME = 'ip-10-0-79-226.us-east-2.compute.internal'


def build_instance(
    instance_id: str = 'i-0123456789abcdef0',
    private_ip=PRIVATE_IP,
    private_dns_name=PRIVATE_DNS_NAME,
) -> EC2Instance:
    data = {
        'InstanceId': instance_id,
        'InstanceType': 'c5.large',
        'LaunchTime': '2026-01-01T00:00:00+00:00',
        'Tags': [
            {
                'Key': constants.IDEA_TAG_NODE_TYPE,
                'Value': constants.NODE_TYPE_COMPUTE,
            },
            {'Key': constants.IDEA_TAG_CLUSTER_NAME, 'Value': 'idea-mock'},
            {'Key': constants.IDEA_TAG_JOB_ID, 'Value': '1034'},
            {'Key': constants.IDEA_TAG_JOB_GROUP, 'Value': 'job-group-1'},
            {'Key': constants.IDEA_TAG_QUEUE_TYPE, 'Value': 'compute'},
            {'Key': constants.IDEA_TAG_COMPUTE_STACK, 'Value': 'idea-mock-compute'},
        ],
    }
    if private_ip is not None:
        data['PrivateIpAddress'] = private_ip
    if private_dns_name is not None:
        data['PrivateDnsName'] = private_dns_name
    return EC2Instance(data)


class FakeScheduler:
    """
    records every name looked up so tests can assert on lookup order.
    find_node reuses the real implementation, which only needs get_node.
    """

    def __init__(self, nodes=None):
        self.nodes = dict(nodes or {})
        self.lookups = []
        self.state_changes = []
        self.deleted = []

    def get_node(self, host, **_):
        self.lookups.append(host)
        return self.nodes.get(host)

    def find_node(self, hosts, **kwargs):
        return OpenPBSScheduler.find_node(self, hosts=hosts, **kwargs)

    def set_node_state(self, host, state):
        self.state_changes.append((host, state))
        return True

    def delete_node(self, host):
        self.deleted.append(host)
        return True


def build_session(context, scheduler: FakeScheduler) -> NodeHouseKeepingSession:
    context.scheduler = scheduler
    metrics = SocaAnyPayload()
    metrics.nodes_deleted = lambda **_: None
    context.metrics = metrics
    return NodeHouseKeepingSession(context=context, logger=logging.getLogger(LOG_TAG))


# ------------------------------------------------------------------ derivation


def test_converter_registers_node_by_private_ipv4():
    """
    the node name handed to the scheduler is the private ipv4, not the dns name.
    """
    node = SocaComputeNode.from_ec2_instance(build_instance())

    assert node is not None
    assert node.host == PRIVATE_IP


def test_node_host_is_private_ipv4_and_legacy_host_is_the_dns_label():
    instance = build_instance()

    assert instance.node_host == PRIVATE_IP
    assert instance.legacy_node_host == LEGACY_HOST


def test_node_host_candidates_try_ipv4_before_the_legacy_dns_name():
    assert build_instance().node_host_candidates == [PRIVATE_IP, LEGACY_HOST]


def test_node_host_candidates_skip_names_the_instance_does_not_have():
    assert build_instance(private_ip=None).node_host_candidates == [LEGACY_HOST]
    assert build_instance(private_dns_name=None).node_host_candidates == [PRIVATE_IP]


def test_instance_log_tag_reports_the_registered_name():
    """
    logs have to name the node the way pbsnodes does, or they cannot be correlated.
    """
    assert f'Host: {PRIVATE_IP},' in str(build_instance())


# ------------------------------------------------------------------ lookup


def test_find_node_matches_ipv4_without_trying_the_legacy_name():
    scheduler = FakeScheduler({PRIVATE_IP: SocaComputeNode(host=PRIVATE_IP)})

    node = scheduler.find_node(hosts=build_instance().node_host_candidates)

    assert node.host == PRIVATE_IP
    assert scheduler.lookups == [PRIVATE_IP]


def test_find_node_falls_back_to_a_node_registered_by_dns_name():
    scheduler = FakeScheduler({LEGACY_HOST: SocaComputeNode(host=LEGACY_HOST)})

    node = scheduler.find_node(hosts=build_instance().node_host_candidates)

    assert node.host == LEGACY_HOST
    assert scheduler.lookups == [PRIVATE_IP, LEGACY_HOST]


def test_find_node_returns_none_after_trying_every_name():
    scheduler = FakeScheduler()

    assert scheduler.find_node(hosts=build_instance().node_host_candidates) is None
    assert scheduler.lookups == [PRIVATE_IP, LEGACY_HOST]


def test_find_node_skips_empty_names():
    scheduler = FakeScheduler({PRIVATE_IP: SocaComputeNode(host=PRIVATE_IP)})

    node = scheduler.find_node(hosts=[None, '', PRIVATE_IP])

    assert node.host == PRIVATE_IP
    assert scheduler.lookups == [PRIVATE_IP]


# ------------------------------------------------------------------ offline / delete


def test_pass4_offlines_a_node_by_its_private_ipv4(context):
    scheduler = FakeScheduler({PRIVATE_IP: SocaComputeNode(host=PRIVATE_IP)})
    session = build_session(context, scheduler)
    session.auto_scaling_group_instances['asg-1'] = [build_instance()]

    session.pass4_set_offline()

    assert scheduler.state_changes == [(PRIVATE_IP, SocaComputeNodeState.OFFLINE)]


def test_pass4_offlines_a_legacy_node_by_the_name_it_is_registered_under(context):
    """
    a node carried over from before the ipv4 switch must be offlined by its dns name.
    offlining it by ipv4 would fail and the instance would be terminated while online.
    """
    scheduler = FakeScheduler({LEGACY_HOST: SocaComputeNode(host=LEGACY_HOST)})
    session = build_session(context, scheduler)
    session.auto_scaling_group_instances['asg-1'] = [build_instance()]

    session.pass4_set_offline()

    assert scheduler.state_changes == [(LEGACY_HOST, SocaComputeNodeState.OFFLINE)]


def test_pass4_offlines_spot_fleet_nodes_by_the_same_rule(context):
    scheduler = FakeScheduler({LEGACY_HOST: SocaComputeNode(host=LEGACY_HOST)})
    session = build_session(context, scheduler)
    session.spot_fleet_instances['sfr-1'] = [build_instance()]

    session.pass4_set_offline()

    assert scheduler.state_changes == [(LEGACY_HOST, SocaComputeNodeState.OFFLINE)]


def test_pass4_leaves_a_busy_node_online(context):
    scheduler = FakeScheduler(
        {
            PRIVATE_IP: SocaComputeNode(
                host=PRIVATE_IP, states=[SocaComputeNodeState.JOB_BUSY]
            )
        }
    )
    session = build_session(context, scheduler)
    session.auto_scaling_group_instances['asg-1'] = [build_instance()]

    session.pass4_set_offline()

    assert scheduler.state_changes == []


def test_pass4_skips_an_instance_the_scheduler_does_not_know(context):
    scheduler = FakeScheduler()
    session = build_session(context, scheduler)
    session.auto_scaling_group_instances['asg-1'] = [build_instance()]

    session.pass4_set_offline()

    assert scheduler.state_changes == []


def test_cleanup_deletes_a_node_by_the_name_it_is_registered_under(context):
    scheduler = FakeScheduler({LEGACY_HOST: SocaComputeNode(host=LEGACY_HOST)})
    session = build_session(context, scheduler)
    session.nodes_to_delete.add(build_instance())

    session.cleanup()

    assert scheduler.deleted == [LEGACY_HOST]


def test_cleanup_deletes_a_scheduler_node_by_its_own_host(context):
    scheduler = FakeScheduler()
    session = build_session(context, scheduler)
    session.nodes_to_delete.add(
        SocaComputeNode(host=PRIVATE_IP, queue_type='compute', instance_id='i-1')
    )

    session.cleanup()

    assert scheduler.deleted == [PRIVATE_IP]
