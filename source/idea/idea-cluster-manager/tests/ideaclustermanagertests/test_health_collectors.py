"""What the cluster manager's health collectors read and emit, against fake AWS and ONTAP answers."""

from ideaclustermanager.app.accounts.ad_automation_agent import ADAutomationAgent
from ideaclustermanager.app.metrics import health_collectors
from ideaclustermanager.app.metrics.health_collectors import (
    AdAutomationCollector,
    CertificatesCollector,
    DirectoryCollector,
    EcsTasksCollector,
    HomeDirectoryCollector,
    SecretsCollector,
    classify_stopped_task,
    service_of,
)
from ideaclustermanager.app.metrics.storage_metrics_service import OntapRequestFailed
from ideasdk.metrics.health import IsolatedProbe, ProbeResult

from datetime import datetime, timedelta, timezone
import os
import threading

import pytest
from types import SimpleNamespace
from unittest.mock import Mock
import ldap

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


class Recorder:
    def __init__(self):
        self.levels = []
        self.counts = []

    def level(self, name, value, **tags):
        self.levels.append((name, value, tags))

    def add(self, name, value=1, timestamp=None, **tags):
        self.counts.append((name, value, timestamp, tags))

    def value(self, name, **tags):
        return [v for n, v, t in self.levels if n == name and tags.items() <= t.items()]


def settings(values):
    config = Mock()
    config.get_string.side_effect = lambda key, *a, **k: values.get(key)
    config.get_bool.side_effect = lambda key, default=None: values.get(key, default)
    config.get_secret.side_effect = lambda key: values.get(key)
    return config


def context_with(values=None, **clients):
    context = Mock()
    context.cluster_name.return_value = 'idea-com'
    context.module_id.return_value = 'cluster-manager'
    context.config.return_value = settings(values or {})
    aws = context.aws.return_value
    aws.get_client.side_effect = lambda name: clients[name]
    for name, client in clients.items():
        getattr(aws, name).return_value = client
    return context


def stopped(
    minutes_ago,
    reason,
    service='idea-com-bastion-host',
    code='ServiceSchedulerInitiated',
    **extra,
):
    return {
        'lastStatus': 'STOPPED',
        'group': f'service:{service}',
        'stoppedAt': NOW - timedelta(minutes=minutes_ago),
        'stoppedReason': reason,
        'stopCode': code,
        **extra,
    }


def ecs_client(tasks, services=()):
    client = Mock()

    def paginator(name):
        pages = {
            'list_tasks': [{'taskArns': [str(i) for i in range(len(tasks))]}],
            'list_services': [{'serviceArns': [s['serviceName'] for s in services]}],
        }
        return SimpleNamespace(paginate=lambda **kwargs: pages[name])

    client.get_paginator.side_effect = paginator
    client.describe_tasks.side_effect = lambda cluster, tasks: {
        'tasks': [all_tasks[int(i)] for i in tasks]
    }
    all_tasks = tasks
    client.describe_services.return_value = {'services': list(services)}
    return client


def run_ecs(tasks, services=()):
    client = ecs_client(tasks, services)
    context = context_with({'ecs.cluster_name': 'idea-com-ecs'}, ecs=client)
    metrics = Recorder()
    EcsTasksCollector(context).collect(metrics, NOW.timestamp())
    return metrics


def test_every_stopped_task_is_counted_by_service_and_reason_in_its_bucket():
    metrics = run_ecs(
        [
            stopped(1, 'Task failed container health checks'),
            stopped(2, 'Task failed container health checks'),
            stopped(3, 'Scaling activity initiated by (deployment ecs-svc/1)'),
            stopped(20, 'Task failed container health checks'),
        ]
    )
    bucket = int(NOW.timestamp()) // 300 * 300 - 300
    assert (
        'ecs.task_stops',
        2,
        bucket,
        {'ecs_service_name': 'bastion-host', 'reason': 'health_check'},
    ) in metrics.counts
    assert sum(c[1] for c in metrics.counts) == 4


def test_a_second_read_or_a_new_leader_rewrites_the_same_point():
    """the intake keeps the last value per metric, tags and time, so equal points cannot add."""
    tasks = [stopped(1, 'Task failed ELB health checks') for _ in range(3)]
    assert run_ecs(tasks).counts == run_ecs(tasks).counts


def test_a_bucket_that_may_have_begun_to_expire_is_not_rewritten_smaller():
    metrics = run_ecs([stopped(55, 'Task failed ELB health checks')])
    assert metrics.counts == []


def test_a_task_still_stopping_waits_for_the_next_read():
    task = stopped(1, 'x')
    task['lastStatus'] = 'DEPROVISIONING'
    assert run_ecs([task]).counts == []


def test_stop_reasons_are_a_small_vocabulary():
    def reason(text='', code='', containers=()):
        return classify_stopped_task(
            {'stoppedReason': text, 'stopCode': code, 'containers': list(containers)}
        )

    assert (
        reason(code='EssentialContainerExited', containers=[{'exitCode': 137}]) == 'oom'
    )
    assert (
        reason(containers=[{'reason': 'OutOfMemoryError: Container killed'}]) == 'oom'
    )
    assert reason('Task failed container health checks') == 'health_check'
    assert (
        reason('Scaling activity initiated by (deployment ecs-svc/1)') == 'deployment'
    )
    assert (
        reason(code='EssentialContainerExited', containers=[{'exitCode': 1}])
        == 'container_exit'
    )
    assert (
        reason('ECS is performing maintenance on the underlying infrastructure')
        == 'maintenance'
    )
    assert reason(code='UserInitiated') == 'user_stop'
    assert reason('anything else') == 'other'
    assert service_of('family:cost-metrics', 'idea-com') is None


def test_a_rollout_that_has_left_the_service_still_counts_under_a_new_leader():
    # two rollouts in one bucket: ECS drops the first from the service when the second
    # completes, and the next leader is a new process that never saw it. Datadog keeps the last
    # value per series and time, so no series may ever be sent a smaller one.
    created = NOW - timedelta(seconds=30)
    first = {'id': 'ecs-svc/1', 'rolloutState': 'COMPLETED', 'createdAt': created}
    second = {'id': 'ecs-svc/2', 'rolloutState': 'COMPLETED', 'createdAt': created}
    service = {'serviceName': 'idea-com-vdc', 'runningCount': 1, 'desiredCount': 1}
    stored = {}
    for deployments in ([first, second], [second]):
        metrics = run_ecs([], [{**service, 'deployments': deployments}])
        for name, value, timestamp, tags in metrics.counts:
            if name == 'ecs.rollouts':
                series = (timestamp, tuple(sorted(tags.items())))
                assert value >= stored.get(series, 0), 'a point was rewritten smaller'
                stored[series] = value
    assert sum(stored.values()) == 2
    assert {dict(tags)['deployment'] for _, tags in stored} == {
        'ecs-svc/1',
        'ecs-svc/2',
    }


def test_a_rollout_too_old_to_send_is_left_alone():
    old = {'id': 'ecs-svc/1', 'createdAt': NOW - timedelta(minutes=55)}
    metrics = run_ecs([], [{'serviceName': 'idea-com-vdc', 'deployments': [old]}])
    assert [c for c in metrics.counts if c[0] == 'ecs.rollouts'] == []


def test_services_report_running_desired_and_rollouts():
    metrics = run_ecs(
        [],
        [
            {
                'serviceName': 'idea-com-vdc',
                'runningCount': 1,
                'desiredCount': 2,
                'pendingCount': 1,
                'deployments': [
                    {
                        'rolloutState': 'IN_PROGRESS',
                        'createdAt': NOW - timedelta(minutes=2),
                    },
                    {'rolloutState': 'COMPLETED', 'createdAt': NOW - timedelta(days=9)},
                ],
            }
        ],
    )
    assert metrics.value('ecs.tasks_running', ecs_service_name='vdc') == [1]
    assert metrics.value('ecs.tasks_desired', ecs_service_name='vdc') == [2]
    assert metrics.value('ecs.rollout_in_progress', ecs_service_name='vdc') == [1]
    assert metrics.value('ecs.rollout_failed', ecs_service_name='vdc') == [0]
    assert [c[1] for c in metrics.counts if c[0] == 'ecs.rollouts'] == [1]
    # the agent adds its own service and ecs_service for the sending task: never reuse them
    for _, _, tags in metrics.levels:
        assert not {'service', 'ecs_service'} & set(tags)
    for *_, tags in metrics.counts:
        assert not {'service', 'ecs_service'} & set(tags)


def test_no_ecs_cluster_means_no_call():
    client = Mock()
    context = context_with({}, ecs=client)
    EcsTasksCollector(context).collect(Recorder(), NOW.timestamp())
    client.get_paginator.assert_not_called()


def certificates(values=None, listeners=None, describe=None):
    elb = Mock()
    elb.describe_listener_certificates.side_effect = listeners or (
        lambda ListenerArn, **kwargs: {
            'Certificates': [{'CertificateArn': 'arn:aws:acm:r:1:certificate/a'}]
        }
    )
    acm = Mock()
    acm.describe_certificate.side_effect = describe or (
        lambda CertificateArn: {
            'Certificate': {
                'NotAfter': NOW + timedelta(days=10),
                'DomainName': '*.Idea.Example',
                'Type': 'IMPORTED',
            }
        }
    )
    return context_with(
        {
            'cluster.load_balancers.internal_alb.https_listener_arn': 'listener',
            **(values or {}),
        },
        elbv2=elb,
        acm=acm,
        secretsmanager=Mock(),
    )


def test_a_listener_certificate_reports_days_left_from_acm():
    metrics = Recorder()
    CertificatesCollector(certificates()).collect(metrics, NOW.timestamp())
    assert metrics.levels == [
        (
            'cert.days_to_expiry',
            10,
            {
                'source': 'acm',
                'lb': 'internal_alb',
                'cert': '*.idea.example',
                'type': 'imported',
            },
        )
    ]


def test_a_certificate_that_will_not_read_does_not_cost_the_others_and_is_reported():
    def describe(CertificateArn):
        if CertificateArn.endswith('/bad'):
            raise RuntimeError('denied')
        return {'Certificate': {'NotAfter': NOW, 'DomainName': 'good', 'Type': 'x'}}

    context = certificates(
        listeners=lambda ListenerArn, **kwargs: {
            'Certificates': [
                {'CertificateArn': 'arn:aws:acm:r:1:certificate/bad'},
                {'CertificateArn': 'arn:aws:acm:r:1:certificate/good'},
            ]
        },
        describe=describe,
    )
    metrics = Recorder()
    try:
        CertificatesCollector(context).collect(metrics, NOW.timestamp())
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert [t['cert'] for _, _, t in metrics.levels] == ['good']


def module_certificate_context(path, secret=None):
    context = certificates(
        values={
            'cluster-manager.server.tls_certificate_file': path,
            'directoryservice.tls_certificate_secret_arn': secret,
        }
    )
    context.aws.return_value.elbv2.return_value.describe_listener_certificates.side_effect = (
        lambda ListenerArn, **kwargs: {'Certificates': []}
    )
    return context


def self_signed_pem():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'idea.example')])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=20))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def test_the_module_certificate_is_read_and_dated(tmp_path):
    path = tmp_path / 'server.pem'
    path.write_bytes(self_signed_pem())
    metrics = Recorder()
    CertificatesCollector(module_certificate_context(str(path))).collect(
        metrics, NOW.timestamp()
    )
    assert metrics.value('cert.days_to_expiry', cert='module_tls') == [20]


def test_a_module_certificate_that_is_not_there_reports_nothing(tmp_path):
    metrics = Recorder()
    CertificatesCollector(module_certificate_context(str(tmp_path / 'no'))).collect(
        metrics, NOW.timestamp()
    )
    assert metrics.levels == []


def test_a_hung_mount_cannot_take_the_collector_thread(tmp_path, monkeypatch):
    # opening a fifo with no writer blocks, as a read on a dead hard mount does
    path = tmp_path / 'hung.pem'
    os.mkfifo(path)
    monkeypatch.setattr(CertificatesCollector, 'PROBE_SECONDS', 1)
    metrics = Recorder()
    collector = CertificatesCollector(module_certificate_context(str(path)))
    worker = threading.Thread(
        target=collector.collect, args=(metrics, NOW.timestamp()), daemon=True
    )
    worker.start()
    worker.join(8)
    alive = worker.is_alive()
    if alive:
        # let the blocked open go, so the test run can end
        os.close(os.open(path, os.O_RDWR | os.O_NONBLOCK))
    assert not alive
    assert metrics.value('cert.unread', source='module_tls', reason='timeout') == [1]


def test_a_directory_secret_that_is_not_a_certificate_does_not_cost_the_module_one(
    tmp_path,
):
    path = tmp_path / 'server.pem'
    path.write_bytes(self_signed_pem())
    context = module_certificate_context(str(path), secret='arn:secret')
    context.aws.return_value.secretsmanager.return_value.get_secret_value.return_value = {
        'SecretString': 'not a certificate'
    }
    metrics = Recorder()
    with pytest.raises(Exception):
        CertificatesCollector(context).collect(metrics, NOW.timestamp())
    assert metrics.value('cert.days_to_expiry', cert='module_tls') == [20]


def test_secrets_are_listed_for_this_cluster_and_never_read():
    client = Mock()
    client.get_paginator.return_value = SimpleNamespace(
        paginate=lambda Filters: [
            {
                'SecretList': [
                    {
                        'Name': 'idea-com-ad-admin',
                        'LastChangedDate': NOW - timedelta(days=30),
                        'NextRotationDate': NOW + timedelta(days=2),
                    }
                ]
            }
        ]
    )
    metrics = Recorder()
    SecretsCollector(context_with(secretsmanager=client)).collect(
        metrics, NOW.timestamp()
    )
    assert metrics.value('secret.age_days', secret='ad-admin') == [30]
    assert metrics.value('secret.days_to_rotation', secret='ad-admin') == [2]
    client.get_secret_value.assert_not_called()


def test_ad_queue_and_a_loop_that_stopped_reaching_it():
    sqs = Mock()
    sqs.get_queue_attributes.return_value = {
        'Attributes': {
            'ApproximateNumberOfMessages': '4',
            'ApproximateNumberOfMessagesNotVisible': '1',
        }
    }
    context = context_with(sqs=sqs)
    context.ad_automation_agent = SimpleNamespace(
        ad_automation_sqs_queue_url='url',
        last_queue_read=NOW.timestamp() - 7200,
        password_days_left=12,
    )
    metrics = Recorder()
    AdAutomationCollector(context).collect(metrics, NOW.timestamp())
    assert metrics.value('ad.queue_depth') == [4]
    assert metrics.value('ad.queue_in_flight') == [1]
    assert metrics.value('ad.seconds_since_queue_read') == [7200]
    assert metrics.value('ad.admin_password_days_to_expiry') == [12]


def agent_with(user=None, error=None):
    agent = ADAutomationAgent.__new__(ADAutomationAgent)
    agent.context = Mock()
    agent.logger = Mock()
    published = []
    agent.context.config().get_string.return_value = 'dogstatsd'
    agent.context.service_registry().get_service.side_effect = lambda name: (
        SimpleNamespace(publish=published.extend)
    )
    agent.password_days_left = None
    agent.ldap_client = Mock(
        ldap_root_username='admin', ds_provider='aws_managed_activedirectory'
    )
    agent.ldap_client.get_user.side_effect = error
    agent.ldap_client.get_user.return_value = user
    agent._recover_root_password = Mock()
    return agent, published


def test_a_rejected_admin_bind_is_counted_with_its_code():
    agent, published = agent_with(
        error=ldap.INVALID_CREDENTIALS(
            {'info': 'AcceptSecurityContext error, data 52e, v'}
        )
    )
    try:
        agent.is_password_expired()
    except ldap.INVALID_CREDENTIALS:
        pass
    rejected = [e for e in published if e['MetricName'] == 'health.ad.bind_rejected']
    assert (
        len(rejected) == 2
    )  # the first bind, then the one after re-reading the secret
    assert rejected[0]['Dimensions'] == [{'Name': 'code', 'Value': '52e'}]


def test_a_rejected_bind_is_counted_on_a_directory_idea_does_not_manage():
    agent, published = agent_with(
        error=ldap.INVALID_CREDENTIALS(
            {'info': 'AcceptSecurityContext error, data 775, v'}
        )
    )
    agent.ldap_client.ds_provider = 'activedirectory'
    with pytest.raises(ldap.INVALID_CREDENTIALS):
        agent.is_password_expired()
    rejected = [e for e in published if e['MetricName'] == 'health.ad.bind_rejected']
    assert [e['Dimensions'] for e in rejected] == [[{'Name': 'code', 'Value': '775'}]]
    # counted, and nothing else: there is no password of ours to recover
    agent._recover_root_password.assert_not_called()


def test_the_admin_password_expiry_is_kept_for_the_collector():
    last_set = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    agent, _ = agent_with(user={'password_last_set': last_set, 'password_max_age': 42})
    agent.is_password_expired()
    assert 11 < agent.password_days_left < 13


class FakeOntap:
    """answers per path; the collector's only view of the file server"""

    def __init__(self, answers):
        self.answers = answers
        self.paths = []

    def first_page(self, path):
        self.paths.append(path)
        answer = next(
            (v for k, v in self.answers.items() if path.startswith(k)),
            OntapRequestFailed(403),
        )
        if isinstance(answer, Exception):
            raise answer
        return {'records': answer}


def run_directory(
    answers,
    values=None,
    ds_ips=('10.0.0.1', '10.0.0.2'),
    described=None,
    endpoint='svm-1.fs-abc.fsx.us-east-2.amazonaws.com',
):
    fake = FakeOntap(answers)
    ds = Mock()
    ds.describe_directories.return_value = {
        'DirectoryDescriptions': [{'DnsIpAddrs': list(ds_ips)}]
    }
    if described is not None:
        ds.describe_directories.side_effect = described
    context = context_with(
        {
            'directoryservice.directory_id': 'd-1',
            'secret': 'hunter2',
            **(values or {}),
        },
        ds=ds,
    )
    storage = Mock()
    storage.targets.return_value = [
        SimpleNamespace(
            name='home',
            endpoint=endpoint,
            username='idea-metrics',
            password_key='secret',
        )
    ]
    original = health_collectors.OntapClient
    health_collectors.OntapClient = lambda *a, **k: fake
    try:
        metrics = Recorder()
        DirectoryCollector(context, storage).collect(metrics)
    finally:
        health_collectors.OntapClient = original
    return metrics, fake


LDAP = '/api/name-services/ldap'
CIFS = '/api/protocols/cifs/services'
DNS = '/api/name-services/dns'
SVMS = '/api/svm/svms'
SVM = {'svm': {'name': 'TESTSVM1'}}


def test_the_conditions_for_a_good_join_read_as_gauges_before_anything_fails():
    metrics, _ = run_directory(
        {
            LDAP: [{**SVM, 'status': {'state': 'up'}}],
            CIFS: [{**SVM, 'security': {'session_security': 'none'}}],
            DNS: [{**SVM, 'servers': ['10.10.0.2']}],
        }
    )
    assert metrics.value('directory.ldap_up', svm='testsvm1', filesystem='fs-abc') == [
        1
    ]
    assert metrics.value('directory.ldap_session_signed', svm='testsvm1') == [0]
    assert metrics.value('directory.dns_on_domain_controllers', svm='testsvm1') == [0]
    assert metrics.value('directory.check_ok', check='ldap', reason='ok') == [1]


def test_good_settings_read_as_good():
    metrics, _ = run_directory(
        {
            LDAP: [{**SVM, 'status': {'state': 'down'}}],
            CIFS: [{**SVM, 'security': {'session_security': 'sign'}}],
            DNS: [{**SVM, 'servers': ['10.0.0.1', '10.0.0.2']}],
        }
    )
    assert metrics.value('directory.ldap_up') == [0]
    assert metrics.value('directory.ldap_session_signed') == [1]
    assert metrics.value('directory.dns_on_domain_controllers') == [1]


def test_a_path_the_login_cannot_read_reports_forbidden_and_the_others_go_on():
    metrics, _ = run_directory({LDAP: [{**SVM, 'status': {'state': 'up'}}]})
    assert metrics.value('directory.ldap_up') == [1]
    assert metrics.value(
        'directory.check_ok', check='cifs_security', reason='forbidden'
    ) == [0]
    # a refusal never reads as good, whatever the check
    assert not [
        tags
        for name, value, tags in metrics.levels
        if name == 'directory.check_ok'
        and value == 1
        and tags['reason'] not in ('ok', 'no_cifs_server')
    ]


GOOD = {
    LDAP: [{**SVM, 'status': {'state': 'up'}}],
    CIFS: [{**SVM, 'security': {'session_security': 'sign'}}],
    DNS: [{**SVM, 'servers': ['10.0.0.1']}],
}


def test_a_reason_is_the_status_ontap_gave_never_a_number_in_the_text():
    # a connection error names the host, and an FSx id or an address can spell a status
    class ConnectionError(Exception):
        pass

    unreachable = ConnectionError(
        "HTTPSConnectionPool(host='svm-0403a.fs-0401b.fsx', port=443): 0x7f403"
    )
    metrics, _ = run_directory({**GOOD, LDAP: unreachable})
    assert metrics.value('directory.check_ok', check='ldap') == [0]
    assert metrics.value('directory.check_ok', check='ldap', reason='unreachable') == [
        0
    ]
    for status, reason in ((401, 'unauthorized'), (403, 'forbidden'), (500, 'error')):
        metrics, _ = run_directory({**GOOD, LDAP: OntapRequestFailed(status)})
        assert metrics.value('directory.check_ok', check='ldap', reason=reason) == [0]


def test_domain_controllers_that_cannot_be_read_are_seen_and_dns_is_not_called_ok():
    for described, reason in (
        (RuntimeError('throttled'), 'error'),
        (lambda **kwargs: {'DirectoryDescriptions': []}, 'not_found'),
    ):
        metrics, _ = run_directory(GOOD, described=described)
        # on a group the monitor evaluates: it groups by filesystem
        assert metrics.value(
            'directory.check_ok', check='dc_addresses', reason=reason, filesystem='none'
        ) == [0]
        assert metrics.value('directory.check_ok', check='dns') == [0]
        assert metrics.value(
            'directory.check_ok', check='dns', reason='no_controllers'
        ) == [0]
        assert metrics.value('directory.dns_on_domain_controllers') == []
        assert metrics.value('directory.check_ok', check='ldap', reason='ok') == [1]


def test_domain_controllers_that_read_say_so_and_no_directory_says_nothing():
    metrics, _ = run_directory(GOOD)
    assert metrics.value(
        'directory.check_ok', check='dc_addresses', reason='ok', filesystem='none'
    ) == [1]
    assert metrics.value('directory.check_ok', check='dns', reason='ok') == [1]
    metrics, _ = run_directory(GOOD, {'directoryservice.directory_id': None})
    assert metrics.value('directory.check_ok', check='dc_addresses') == []
    assert metrics.value('directory.check_ok', check='dns', reason='ok') == [1]
    assert metrics.value('directory.dns_on_domain_controllers') == []


def test_every_check_of_a_file_server_carries_one_filesystem_tag():
    # an endpoint that is not an FSx name has no id in it: the target's name, never unknown
    metrics, _ = run_directory(GOOD, endpoint='10.1.2.3')
    tags = {
        t['filesystem']
        for n, _, t in metrics.levels
        if t.get('check') != 'dc_addresses'
    }
    assert tags == {'home'}
    assert metrics.value('directory.check_ok', check='login', reason='ok') == [1]
    for endpoint, filesystem in (
        ('10.1.2.3', 'home'),
        ('svm-1.fs-abc.fsx.us-east-2.amazonaws.com', 'fs-abc'),
    ):
        metrics, _ = run_directory(GOOD, {'secret': None}, endpoint=endpoint)
        assert metrics.value(
            'directory.check_ok',
            check='login',
            reason='no_password',
            filesystem=filesystem,
        ) == [0]
        assert metrics.value('directory.check_ok', check='ldap') == []


def test_no_cifs_record_is_not_read_as_ok():
    # a role without /api/svm/svms gets 200 and no records, which says nothing about signing
    metrics, _ = run_directory({LDAP: [{**SVM, 'status': {'state': 'up'}}], CIFS: []})
    assert metrics.value(
        'directory.check_ok', check='cifs_security', reason='no_records'
    ) == [0]
    assert metrics.value('directory.check_ok', check='cifs_security', reason='ok') == []
    assert metrics.value('directory.check_ok', check='ldap', reason='ok') == [1]


def test_an_svm_the_role_can_see_with_no_cifs_server_is_nfs_only_not_a_failed_read():
    metrics, fake = run_directory(
        {
            LDAP: [{**SVM, 'status': {'state': 'up'}}],
            CIFS: [],
            SVMS: [{'name': 'TESTSVM1'}],
        }
    )
    assert metrics.value(
        'directory.check_ok', check='cifs_security', reason='no_cifs_server'
    ) == [1]
    assert (
        metrics.value('directory.check_ok', check='cifs_security', reason='no_records')
        == []
    )
    assert metrics.value('directory.ldap_session_signed') == []
    assert any(path.startswith(SVMS) for path in fake.paths)


def test_the_event_log_no_svm_login_can_be_granted_is_never_asked_for():
    # ONTAP refuses /api/support/ems/events to an SVM role at every access level, so a secd
    # check could only ever read refused, and a check that cannot fail is not a check
    metrics, fake = run_directory(GOOD)
    assert not [path for path in fake.paths if '/ems' in path]
    assert metrics.value('directory.check_ok', check='secd_events') == []
    assert metrics.counts == []
    assert {t['check'] for n, _, t in metrics.levels if n == 'directory.check_ok'} == {
        'dc_addresses',
        'login',
        'ldap',
        'cifs_security',
        'dns',
    }


def test_home_probe_reports_success_latency_and_each_way_of_failing(monkeypatch):
    collector = HomeDirectoryCollector(context_with())
    for result, expected in (
        (ProbeResult('ok', 0.02, 0), (1, 'ok')),
        (ProbeResult('timeout', 15), (0, 'timeout')),
        (ProbeResult('stuck'), (0, 'stuck')),
        (ProbeResult('failed', 0.1, 3), (0, 'denied')),
    ):
        monkeypatch.setattr(IsolatedProbe, 'run', lambda self, r=result: r)
        assert collector._probe is None or collector._probe.argv[-1] == '/data/home'
        metrics = Recorder()
        collector.collect(metrics)
        ok, tags = [
            (v, t) for n, v, t in metrics.levels if n == 'directory.home_stat_ok'
        ][0]
        assert (ok, tags['reason']) == expected
