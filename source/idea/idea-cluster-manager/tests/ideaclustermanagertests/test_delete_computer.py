"""Exercise real sender verification, target selection and SQS acknowledgement together."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import ldap
import pytest
from botocore.exceptions import ClientError

from ideaclustermanager.app.accounts.ad_automation_agent import ADAutomationAgent
from ideaclustermanager.app.accounts.helpers.preset_computer_helper import (
    PresetComputeHelper,
)
from ideaclustermanagertests.metrics_fakes import FakeContext
from ideadatamodel import errorcodes, exceptions
from ideasdk.utils import Utils

TASK_ID = 'a08865093a3b49bda527d44d31bce224'
TASK_ARN = f'arn:aws:ecs:us-east-2:871375171278:task/idea-com-ecs/{TASK_ID}'
OU = 'OU=Computers,OU=idea-com,DC=example,DC=com'
TARGET = 'DESKTOP-123'
DN = f'CN={TARGET},{OU}'


@pytest.fixture
def agent(monkeypatch):
    context = FakeContext(
        {
            'cluster.aws.region': 'us-east-2',
            'cluster.aws.account_id': '871375171278',
            'cluster.cluster_name': 'idea-com',
            'ecs.cluster_name': 'idea-com-ecs',
            'scheduler.task_role_id': 'AROASCHEDULER',
            'scheduler.service_name': 'idea-com-scheduler',
            'directoryservice.computers.ou': OU,
        }
    )
    context._aws = Mock()
    context._aws.aws_partition.return_value = 'aws'
    context.config().get_module_id = lambda module: module
    context.aws_util = Mock()

    def describe(filters):
        instance_id = filters[0]['Values'][0]
        if instance_id != 'i-sender':
            raise ClientError(
                {'Error': {'Code': 'InvalidInstanceID.Malformed'}}, 'DescribeInstances'
            )
        return [SimpleNamespace(soca_node_type='infra', idea_module_id='scheduler')]

    context.aws_util().ec2_describe_instances.side_effect = describe
    context.aws().get_client('ecs').describe_tasks.return_value = {
        'tasks': [
            {
                'taskArn': TASK_ARN,
                'lastStatus': 'RUNNING',
                'group': 'service:idea-com-scheduler',
            }
        ]
    }
    # Old code needs adcli/discovery; keep these local so baseline failures test behavior.
    shell = Mock()
    shell.invoke.return_value = SimpleNamespace(returncode=0, stdout='adcli')
    monkeypatch.setattr(
        'ideaclustermanager.app.accounts.helpers.preset_computer_helper.ShellInvoker',
        lambda **_: shell,
    )
    monkeypatch.setattr(
        PresetComputeHelper, 'get_domain_controller_ip_addresses', lambda _: ['dc']
    )
    agent = ADAutomationAgent.__new__(ADAutomationAgent)
    agent.context = context
    agent.logger = context.logger()
    agent.ldap_client = Mock()
    agent.ldap_client.search_s.side_effect = (
        lambda base, filterstr, **_: [(DN, {})]
        if base == OU and filterstr == f'(&(objectClass=computer)(cn={TARGET}))'
        else []
    )
    agent.ad_automation_dao = Mock()
    agent.ad_automation_dao.list_entries_by_instance_id.return_value = [
        {'nonce': 'old', 'hostname': TARGET, 'status': 'success'}
    ]
    agent.ad_automation_sqs_queue_url = 'queue'
    agent.check_and_reset_admin_password = Mock()
    agent.shell = shell
    return agent


def run_message(agent, sender=f'AROASCHEDULER:{TASK_ID}', **payload):
    agent._stop_event = Mock()
    agent._stop_event.is_set.side_effect = [False, True]
    sqs = agent.context.aws().sqs()
    sqs.reset_mock()
    sqs.receive_message.return_value = {
        'Messages': [
            {
                'MessageId': 'message',
                'ReceiptHandle': 'receipt',
                'Attributes': {'SenderId': sender},
                'Body': json.dumps(
                    {
                        'header': {'namespace': 'ADAutomation.DeleteComputer'},
                        'payload': payload,
                    }
                ),
            }
        ]
    }
    sqs.delete_message_batch.return_value = {}
    agent.automation_loop()
    return sqs.delete_message_batch.called


@pytest.mark.parametrize('field', ['computer_name', 'hostname'])
def test_ec2_deletes_payload_computer(agent, field):
    assert run_message(
        agent,
        sender='AROASCHEDULER:i-sender',
        instance_id='i-target',
        **{field: TARGET},
    )
    agent.ldap_client.delete_s.assert_called_once_with(DN)
    agent.context.aws_util().ec2_describe_instances.assert_called_once_with(
        filters=[
            {'Name': 'instance-id', 'Values': ['i-sender']},
            {'Name': 'instance-state-name', 'Values': ['running']},
        ]
    )
    agent.ad_automation_dao.delete_entry.assert_called_once_with('i-target', 'old')


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('field', ['computer_name', 'hostname'])
def test_task_deletes_payload_computer(agent, legacy, field):
    payload = {'instance_id': 'i-target', field: TARGET}
    if not legacy:
        payload['task_arn'] = TASK_ARN
    assert run_message(agent, **payload)
    agent.ldap_client.delete_s.assert_called_once_with(DN)
    agent.context.aws().get_client('ecs').describe_tasks.assert_called_once_with(
        cluster='idea-com-ecs', tasks=[TASK_ARN]
    )
    agent.context.aws_util().ec2_describe_instances.assert_not_called()
    agent.shell.invoke.assert_not_called()


def test_task_cannot_delete_outside_cluster_ou(agent):
    # The requested computer exists, but only outside the configured computer OU.
    outside_dn = f'CN={TARGET},OU=Other,DC=example,DC=com'
    agent.ldap_client.search_s.side_effect = (
        lambda base, **_: [(outside_dn, {})] if base != OU else []
    )
    assert run_message(
        agent, computer_name=TARGET, instance_id='i-target', task_arn=TASK_ARN
    )
    agent.ldap_client.search_s.assert_called_once_with(
        base=OU, filterstr=f'(&(objectClass=computer)(cn={TARGET}))', attrlist=['dn']
    )
    agent.ldap_client.delete_s.assert_not_called()
    assert any(
        'Refusing AD delete' in line and OU in line for line in agent.logger.lines
    )


def test_legacy_without_target_drops_with_one_diagnostic(agent):
    assert run_message(agent, instance_id='i-target')
    agent.ldap_client.delete_s.assert_not_called()
    agent.context.aws_util().ec2_describe_instances.assert_not_called()
    errors = [line for line in agent.logger.lines if line.startswith('error ')]
    assert len(errors) == 1
    assert 'Dropping' in errors[0] and 'target' in errors[0]


@pytest.mark.parametrize('operation', ['search_s', 'delete_s'])
def test_transient_ad_failure_retries(agent, operation):
    getattr(agent.ldap_client, operation).side_effect = ldap.SERVER_DOWN('temporary')
    assert not run_message(
        agent, computer_name=TARGET, instance_id='i-target', task_arn=TASK_ARN
    )
    assert any('retried' in line for line in agent.logger.lines)
    agent.ad_automation_dao.delete_entry.assert_not_called()


def test_verified_preset_failure_code_from_ad_still_retries(agent):
    agent.ldap_client.delete_s.side_effect = exceptions.soca_exception(
        error_code=errorcodes.AD_AUTOMATION_PRESET_COMPUTER_FAILED,
        message='AD temporarily unavailable',
    )
    assert not run_message(
        agent, computer_name=TARGET, instance_id='i-target', task_arn=TASK_ARN
    )


@pytest.mark.parametrize('legacy', [False, True])
def test_unverifiable_task_drops_with_one_diagnostic(agent, legacy):
    payload = {'computer_name': TARGET, 'instance_id': 'i-target'}
    if not legacy:
        payload['task_arn'] = TASK_ARN
    assert run_message(agent, sender=f'AROASTRANGER:{TASK_ID}', **payload)
    agent.ldap_client.search_s.assert_not_called()
    agent.ldap_client.delete_s.assert_not_called()
    errors = [line for line in agent.logger.lines if line.startswith('error ')]
    assert len(errors) == 1
    assert 'Dropping' in errors[0] and 'not a cluster task role' in errors[0]


def test_target_filter_is_literal(agent):
    assert run_message(agent, computer_name='*', task_arn=TASK_ARN)
    agent.ldap_client.search_s.assert_called_once_with(
        base=OU, filterstr=r'(&(objectClass=computer)(cn=\2a))', attrlist=['dn']
    )
    agent.ldap_client.delete_s.assert_not_called()


def test_concurrent_delete_is_idempotent(agent):
    agent.ldap_client.delete_s.side_effect = ldap.NO_SUCH_OBJECT('already removed')
    assert run_message(
        agent, computer_name=TARGET, instance_id='i-target', task_arn=TASK_ARN
    )
    agent.ad_automation_dao.delete_entry.assert_called_once_with('i-target', 'old')


def test_task_preset_still_uses_its_own_generated_name(agent):
    helper = PresetComputeHelper(
        agent.context,
        agent.ldap_client,
        agent.ad_automation_dao,
        f'AROASCHEDULER:{TASK_ID}',
        {'payload': {'hostname': TARGET, 'nonce': 'join', 'task_arn': TASK_ARN}},
    )
    assert helper.hostname != TARGET
    assert helper.hostname.startswith('IDEA-')
    assert helper.instance_id == f'AROASCHEDULER:{TASK_ID}'


def generated_name(instance_id):
    data = f'us-east-2|871375171278|idea-com|{instance_id}'
    return 'IDEA-' + Utils.shake_256(data, 256)[-10:].upper()


def set_target(agent, name):
    dn = f'CN={name},{OU}'
    agent.ldap_client.search_s.side_effect = lambda **_: [(dn, {})]
    return dn


@pytest.mark.parametrize(
    'node_type,module',
    [
        ('dcv-host', 'virtual-desktop-controller'),
        ('compute', 'scheduler'),
        ('infra', 'bastion-host'),
    ],
)
@pytest.mark.parametrize('own_name', ['generated', 'recorded'])
def test_ec2_delete_is_scoped_to_own_computer(agent, node_type, module, own_name):
    agent.context.aws_util().ec2_describe_instances.side_effect = lambda **_: [
        SimpleNamespace(soca_node_type=node_type, idea_module_id=module)
    ]
    name = generated_name('i-sender') if own_name == 'generated' else 'OLD-WINDOWS'
    agent.ad_automation_dao.list_entries_by_instance_id.side_effect = (
        lambda instance: [{'nonce': 'own', 'hostname': name, 'status': 'success'}]
        if instance == 'i-sender'
        else []
    )
    assert run_message(
        agent,
        sender='AROANODE:i-sender',
        computer_name=TARGET,
        hostname=TARGET,
        instance_id='i-target',
    )
    agent.ldap_client.delete_s.assert_not_called()
    agent.ldap_client.search_s.assert_not_called()
    agent.ad_automation_dao.delete_entry.assert_not_called()
    dn = set_target(agent, name)
    assert run_message(
        agent,
        sender='AROANODE:i-sender',
        computer_name=name.lower(),
        instance_id='i-sender',
    )
    agent.ldap_client.delete_s.assert_called_once_with(dn)
    agent.ad_automation_dao.delete_entry.assert_called_once_with('i-sender', 'own')


@pytest.mark.parametrize('module', ['virtual-desktop-controller', 'scheduler'])
def test_cross_target_ec2_delete_requires_infra_node_type(agent, module):
    agent.ad_automation_dao.list_entries_by_instance_id.return_value = []
    instance = SimpleNamespace(soca_node_type='compute', idea_module_id=module)
    agent.context.aws_util().ec2_describe_instances.side_effect = lambda **_: [instance]
    assert run_message(agent, sender='AROAINFRA:i-sender', computer_name=TARGET)
    agent.ldap_client.delete_s.assert_not_called()
    instance.soca_node_type = 'infra'
    assert run_message(agent, sender='AROAINFRA:i-sender', computer_name=TARGET)
    agent.ldap_client.delete_s.assert_called_once_with(DN)


@pytest.mark.parametrize('legacy', [False, True])
def test_bastion_task_cannot_delete(agent, legacy):
    agent.context.config().values.update(
        {
            'bastion-host.task_role_id': 'AROABASTION',
            'bastion-host.service_name': 'idea-com-bastion',
        }
    )
    task = agent.context.aws().get_client('ecs').describe_tasks.return_value['tasks'][0]
    task['group'] = 'service:idea-com-bastion'
    payload = {'computer_name': TARGET}
    if not legacy:
        payload['task_arn'] = TASK_ARN
    assert run_message(agent, sender=f'AROABASTION:{TASK_ID}', **payload)
    agent.ldap_client.delete_s.assert_not_called()
    agent.ldap_client.search_s.assert_not_called()


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize(
    'status,desired,timestamp',
    [
        ('RUNNING', 'STOPPING', None),
        ('RUNNING', 'STOPPED', None),
        ('DEACTIVATING', 'STOPPED', 'stoppingAt'),
        ('STOPPING', 'STOPPED', 'stoppingAt'),
        ('STOPPED', 'STOPPED', 'stoppedAt'),
    ],
)
def test_rollout_task_can_finish_delete(agent, legacy, status, desired, timestamp):
    task = agent.context.aws().get_client('ecs').describe_tasks.return_value['tasks'][0]
    task.update(lastStatus=status, desiredStatus=desired)
    if timestamp:
        task[timestamp] = datetime.now(timezone.utc) - timedelta(minutes=59)
    payload = {'computer_name': TARGET}
    if not legacy:
        payload['task_arn'] = TASK_ARN
    assert run_message(agent, **payload)
    agent.ldap_client.delete_s.assert_called_once_with(DN)
    # The same sender cannot use the grace period to acquire a fresh join OTP.
    with pytest.raises(exceptions.SocaException):
        PresetComputeHelper(
            agent.context,
            agent.ldap_client,
            agent.ad_automation_dao,
            f'AROASCHEDULER:{TASK_ID}',
            {'payload': {'nonce': 'join', 'task_arn': TASK_ARN}},
        )
    agent.ldap_client.delete_s.reset_mock()
    task['group'] = 'service:unrelated'
    assert run_message(agent, **payload)
    agent.ldap_client.delete_s.assert_not_called()
    task['group'] = 'service:idea-com-scheduler'
    assert run_message(agent, sender=f'AROAOTHER:{TASK_ID}', **payload)
    agent.ldap_client.delete_s.assert_not_called()
    if timestamp:
        for invalid_time in (None, datetime.now(timezone.utc) + timedelta(minutes=5)):
            task[timestamp] = invalid_time
            assert run_message(agent, **payload)
            agent.ldap_client.delete_s.assert_not_called()
        task[timestamp] = datetime.now(timezone.utc) - timedelta(minutes=61)
        assert run_message(agent, **payload)
        agent.ldap_client.delete_s.assert_not_called()


@pytest.mark.parametrize('instance_id', ['i-other', 'i-sender'])
def test_delete_does_not_clear_unrelated_nonces(agent, instance_id):
    agent.ad_automation_dao.list_entries_by_instance_id.return_value = [
        {'nonce': 'unrelated', 'hostname': 'OTHER', 'status': 'success'},
        {'nonce': 'failed', 'hostname': TARGET, 'status': 'fail'},
    ]
    assert run_message(
        agent, computer_name=TARGET, instance_id=instance_id, task_arn=TASK_ARN
    )
    agent.ldap_client.delete_s.assert_called_once_with(DN)
    agent.ad_automation_dao.delete_entry.assert_not_called()


def test_generated_target_clears_only_its_instance_nonces(agent):
    name = generated_name('i-target')
    set_target(agent, name)
    agent.ad_automation_dao.list_entries_by_instance_id.return_value = [
        {'nonce': 'old'}
    ]
    assert run_message(
        agent, computer_name=name, instance_id='i-other', task_arn=TASK_ARN
    )
    agent.ad_automation_dao.delete_entry.assert_not_called()
    assert run_message(
        agent, computer_name=name, instance_id='i-target', task_arn=TASK_ARN
    )
    agent.ad_automation_dao.delete_entry.assert_called_once_with('i-target', 'old')


@pytest.mark.parametrize('state', ['shutting-down', 'terminated'])
def test_ecs_host_termination_resolves_own_computer(agent, state):
    name = generated_name('i-0123456789abcdef0')
    dn = set_target(agent, name)
    host = SimpleNamespace(
        instance_id='i-0123456789abcdef0',
        soca_cluster_name='idea-com',
        idea_module_id='ecs',
        state=state,
    )
    agent.context.aws_util().ec2_describe_instances.side_effect = lambda **_: [host]
    assert run_message(agent, sender='AIDA_EVENTBRIDGE', instance_id=host.instance_id)
    agent.ldap_client.delete_s.assert_called_once_with(dn)
    agent.ldap_client.search_s.assert_called_once_with(
        base=OU, filterstr=f'(&(objectClass=computer)(cn={name}))', attrlist=['dn']
    )
    # An EC2/task role session cannot use the infrastructure event path.
    agent.ldap_client.delete_s.reset_mock()
    for sender in ('AROADCV:i-sender', f'AROASCHEDULER:{TASK_ID}'):
        assert run_message(agent, sender=sender, instance_id=host.instance_id)
        agent.ldap_client.delete_s.assert_not_called()
    # A payload cannot override the independently resolved host name.
    agent.ldap_client.delete_s.reset_mock()
    assert run_message(
        agent,
        sender='AIDA_EVENTBRIDGE',
        instance_id=host.instance_id,
        computer_name=TARGET,
    )
    agent.ldap_client.delete_s.assert_not_called()
    for changes in (
        {'state': 'running'},
        {'idea_module_id': 'scheduler'},
        {'soca_cluster_name': 'another-cluster'},
    ):
        original = vars(host).copy()
        for key, value in changes.items():
            setattr(host, key, value)
        assert run_message(
            agent, sender='AIDA_EVENTBRIDGE', instance_id=host.instance_id
        )
        agent.ldap_client.delete_s.assert_not_called()
        vars(host).update(original)
