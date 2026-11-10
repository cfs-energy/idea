"""
The collectors the cluster manager runs for the cluster as a whole: one per concern, each a
read, each reporting through ideasdk.metrics.health. The module has the rights these need and
holds the directory agent and the ONTAP metrics login already. See docs for what each emits.
"""

from ideaclustermanager.app.accounts.auth_constants import USER_HOME_DIR_BASE
from ideaclustermanager.app.metrics.drift_checks import SIGNED_SESSION_SECURITY
from ideaclustermanager.app.metrics.storage_metrics_service import (
    SVMS_PATH,
    OntapClient,
    OntapRequestFailed,
    fs_id_from_host,
    tag_value,
)
from ideasdk.metrics.health import (
    HOME_PROBE_REASONS,
    HOME_PROBE_SCRIPT,
    HealthCollector,
    HealthMetrics,
    IsolatedProbe,
    SECONDS_PER_DAY,
    bucket_of,
    failure_reason,
    pem_days_left,
    python_probe,
)
from ideasdk.utils import Utils

from collections import Counter
from typing import Dict, List, Optional, Set, Tuple
import re
import time

LISTENERS = {
    'cluster.load_balancers.external_alb.https_listener_arn': 'external_alb',
    'cluster.load_balancers.internal_alb.https_listener_arn': 'internal_alb',
    'cluster.load_balancers.internal_alb.dcv_broker_client_listener_arn': 'internal_alb',
    'cluster.load_balancers.internal_alb.dcv_broker_agent_listener_arn': 'internal_alb',
    'cluster.load_balancers.internal_alb.dcv_broker_gateway_listener_arn': 'internal_alb',
}
DOMAIN_ONLY = re.compile(r'[^a-z0-9.*_-]')

# ListTasks keeps a stopped task for at least an hour. A bucket is only sent while every task in
# it must still be listed: older, and a later read could rewrite it with a smaller total.
STOPPED_WINDOW_SECONDS = 50 * 60
DESCRIBE_TASKS_BATCH = 100
DESCRIBE_SERVICES_BATCH = 10

LDAP_PATH = '/api/name-services/ldap?fields=svm.name,status&return_records=true'
CIFS_PATH = (
    '/api/protocols/cifs/services?fields=svm.name,security.session_security'
    '&return_records=true'
)
DNS_PATH = '/api/name-services/dns?fields=svm.name,servers&return_records=true'


class NoRecords(Exception):
    """a CIFS read that answered with nothing from a role that cannot see its SVM: not an ok."""


class NoControllers(Exception):
    """the directory's domain controllers could not be read, so DNS has nothing to be held to."""


def service_of(group: Optional[str], cluster: str) -> Optional[str]:
    """service:idea-com-bastion-host is bastion-host; a standalone task has no service."""
    if not group or not group.startswith('service:'):
        return None
    return group[len('service:') :].removeprefix(f'{cluster}-')


def classify_stopped_task(task: Dict) -> str:
    """why ECS stopped a task, in one word. An out of memory kill also reads as a plain exit."""
    reason = task.get('stoppedReason') or ''
    code = task.get('stopCode') or ''
    for container in task.get('containers') or []:
        if (container.get('reason') or '').startswith('OutOfMemory'):
            return 'oom'
        if container.get('exitCode') == 137 and code == 'EssentialContainerExited':
            return 'oom'
    lowered = reason.lower()
    if 'health check' in lowered:
        return 'health_check'
    if 'deployment' in lowered:
        return 'deployment'
    if 'maintenance' in lowered or code == 'TerminationNotice':
        return 'maintenance'
    return {
        'EssentialContainerExited': 'container_exit',
        'TaskFailedToStart': 'failed_to_start',
        'UserInitiated': 'user_stop',
        'ServiceSchedulerInitiated': 'scheduler',
        'SpotInterruption': 'spot',
    }.get(code, 'other')


class EcsTasksCollector(HealthCollector):
    """
    Every stopped task and rollout, read from ECS itself, so no event is sampled. Needs nothing
    beyond the module's ECS read. The service is tagged ecs_service_name: the agent already puts
    its own service and ecs_service on every point, for the task that sent it.
    """

    name = 'ecs'
    interval_seconds = 300
    time_limit_seconds = 120

    def __init__(self, context):
        self.context = context

    def _client(self):
        return self.context.aws().get_client('ecs')

    def stopped_tasks(self, client, cluster: str) -> List[Dict]:
        arns: List[str] = []
        for page in client.get_paginator('list_tasks').paginate(
            cluster=cluster, desiredStatus='STOPPED'
        ):
            arns.extend(page.get('taskArns', []))
        tasks: List[Dict] = []
        for start in range(0, len(arns), DESCRIBE_TASKS_BATCH):
            tasks.extend(
                client.describe_tasks(
                    cluster=cluster, tasks=arns[start : start + DESCRIBE_TASKS_BATCH]
                ).get('tasks', [])
            )
        return tasks

    def collect(self, metrics: HealthMetrics, now: Optional[float] = None) -> None:
        cluster = self.context.config().get_string('ecs.cluster_name')
        if Utils.is_empty(cluster):
            return
        now = now or time.time()
        client = self._client()
        cluster_name = self.context.cluster_name()
        totals: Counter = Counter()
        for task in self.stopped_tasks(client, cluster):
            stopped_at = task.get('stoppedAt')
            if task.get('lastStatus') != 'STOPPED' or stopped_at is None:
                continue  # still stopping: the next read sees it finished
            service = service_of(task.get('group'), cluster_name) or 'standalone'
            totals[
                (
                    bucket_of(stopped_at.timestamp()),
                    service,
                    classify_stopped_task(task),
                )
            ] += 1
        oldest = now - STOPPED_WINDOW_SECONDS
        for (bucket, service, reason), total in totals.items():
            if bucket >= oldest:
                metrics.add(
                    'ecs.task_stops',
                    total,
                    timestamp=bucket,
                    ecs_service_name=service,
                    reason=reason,
                )
        self._services(client, cluster, cluster_name, metrics, oldest)

    def _services(self, client, cluster, cluster_name, metrics, oldest):
        arns: List[str] = []
        for page in client.get_paginator('list_services').paginate(cluster=cluster):
            arns.extend(page.get('serviceArns', []))
        # one point per deployment, tagged with its id: ECS forgets a finished deployment once
        # the next finishes, and a count of what this read still lists would rewrite a bucket
        # smaller, from this task or from the next leader. a point of its own is only ever 1.
        for start in range(0, len(arns), DESCRIBE_SERVICES_BATCH):
            for service in client.describe_services(
                cluster=cluster, services=arns[start : start + DESCRIBE_SERVICES_BATCH]
            ).get('services', []):
                name = service['serviceName'].removeprefix(f'{cluster_name}-')
                metrics.level(
                    'ecs.tasks_running',
                    service.get('runningCount', 0),
                    ecs_service_name=name,
                )
                metrics.level(
                    'ecs.tasks_desired',
                    service.get('desiredCount', 0),
                    ecs_service_name=name,
                )
                metrics.level(
                    'ecs.tasks_pending',
                    service.get('pendingCount', 0),
                    ecs_service_name=name,
                )
                deployments = service.get('deployments') or []
                states = [d.get('rolloutState') for d in deployments]
                metrics.level(
                    'ecs.rollout_in_progress',
                    1 if 'IN_PROGRESS' in states else 0,
                    ecs_service_name=name,
                )
                metrics.level(
                    'ecs.rollout_failed',
                    1 if 'FAILED' in states else 0,
                    ecs_service_name=name,
                )
                for deployment in deployments:
                    created = deployment.get('createdAt')
                    if created is None or bucket_of(created.timestamp()) < oldest:
                        continue
                    metrics.add(
                        'ecs.rollouts',
                        1,
                        timestamp=bucket_of(created.timestamp()),
                        ecs_service_name=name,
                        deployment=tag_value(deployment.get('id')),
                    )


# the file is on the shared mount: read in a child, so a hung read cannot take this thread with it.
# bounded below the pipe's buffer, which the parent only reads once the child has exited
CERTIFICATE_PROBE_SCRIPT = """
import sys
try:
    with open(sys.argv[1], 'rb') as certificate:
        sys.stdout.buffer.write(certificate.read(60000))
except FileNotFoundError:
    sys.exit(3)
"""
CERTIFICATE_NOT_THERE = 3


class CertificatesCollector(HealthCollector):
    """days left on what the load balancers serve and on the certificates IDEA reads itself."""

    name = 'certificates'
    interval_seconds = 3600
    time_limit_seconds = 90
    PROBE_SECONDS = 15

    def __init__(self, context):
        self.context = context
        self._probe: Optional[IsolatedProbe] = None

    def _listener_certificates(self) -> Tuple[Dict[str, str], Optional[Exception]]:
        config = self.context.config()
        found: Dict[str, str] = {}
        failure = None
        provided = config.get_string(
            'cluster.load_balancers.external_alb.certificates.acm_certificate_arn'
        )
        if Utils.is_not_empty(provided):
            found[provided] = 'external_alb'
        client = self.context.aws().elbv2()
        for key, load_balancer in LISTENERS.items():
            listener = config.get_string(key)
            if Utils.is_empty(listener):
                continue
            try:
                marker = None
                while True:
                    kwargs = {'Marker': marker} if marker else {}
                    page = client.describe_listener_certificates(
                        ListenerArn=listener, **kwargs
                    )
                    for certificate in page.get('Certificates', []):
                        found.setdefault(certificate['CertificateArn'], load_balancer)
                    marker = page.get('NextMarker')
                    if not marker:
                        break
            except Exception as e:  # the other listeners still report
                failure = e
        return found, failure

    def collect(self, metrics: HealthMetrics, now: Optional[float] = None) -> None:
        now = now or time.time()
        arns, failure = self._listener_certificates()
        client = self.context.aws().acm()
        for arn, load_balancer in sorted(arns.items()):
            if ':acm:' not in arn:
                metrics.level('cert.unread', 1, lb=load_balancer, source='iam')
                continue
            try:
                detail = client.describe_certificate(CertificateArn=arn)['Certificate']
                not_after = detail.get('NotAfter')
                if not_after is None:
                    metrics.level('cert.unread', 1, lb=load_balancer, source='acm')
                    continue
                metrics.level(
                    'cert.days_to_expiry',
                    (not_after.timestamp() - now) / SECONDS_PER_DAY,
                    source='acm',
                    lb=load_balancer,
                    cert=DOMAIN_ONLY.sub(
                        '_', (detail.get('DomainName') or 'unknown').lower()
                    ),
                    type=str(detail.get('Type') or 'unknown').lower(),
                )
            except Exception as e:
                failure = e
        for read in (self._directory_certificate, self._module_certificate):
            # each on its own: one that will not read does not cost the other
            try:
                certificate = read(metrics, now)
            except Exception as e:
                failure = e
                continue
            if certificate is not None:
                metrics.level(
                    'cert.days_to_expiry',
                    certificate[1],
                    source='idea',
                    lb='none',
                    cert=certificate[0],
                )
        if failure is not None:
            raise failure

    def _directory_certificate(self, metrics, now: float):
        secret_arn = self.context.config().get_string(
            'directoryservice.tls_certificate_secret_arn'
        )
        if Utils.is_empty(secret_arn):
            return None
        value = (
            self.context.aws()
            .secretsmanager()
            .get_secret_value(SecretId=secret_arn)
            .get('SecretString')
        )
        return 'directory_ldap', pem_days_left(value.encode(), now)

    def _module_certificate(self, metrics, now: float):
        path = self.context.config().get_string(
            f'{self.context.module_id()}.server.tls_certificate_file'
        )
        if Utils.is_empty(path):
            return None
        if self._probe is None:
            self._probe = IsolatedProbe(
                python_probe(CERTIFICATE_PROBE_SCRIPT, path), self.PROBE_SECONDS
            )
        result = self._probe.run()
        if result.status == 'ok':
            return 'module_tls', pem_days_left(result.output.encode(), now)
        if result.returncode != CERTIFICATE_NOT_THERE:
            metrics.level(
                'cert.unread', 1, lb='none', source='module_tls', reason=result.status
            )
        return None


def _holds_a_password(secret: Dict) -> bool:
    # as accounts_service.py tags them, in any cluster: none gets a series of its own
    return _tags(secret).get('idea:Purpose') == 'password-sync'


def _tags(secret: Dict) -> Dict:
    return {tag.get('Key'): tag.get('Value') for tag in secret.get('Tags', [])}


class SecretsCollector(HealthCollector):
    """how long since each of the cluster's secrets changed. Metadata: no secret value is read."""

    name = 'secrets'
    interval_seconds = 6 * 3600
    time_limit_seconds = 60
    MAX_SECRETS = 60

    def __init__(self, context):
        self.context = context

    def collect(self, metrics: HealthMetrics, now: Optional[float] = None) -> None:
        now = now or time.time()
        cluster = self.context.cluster_name()
        client = self.context.aws().secretsmanager()
        secrets: List[Dict] = []
        for page in client.get_paginator('list_secrets').paginate(
            Filters=[
                {'Key': 'tag-key', 'Values': ['idea:ClusterName']},
                {'Key': 'tag-value', 'Values': [cluster]},
            ]
        ):
            secrets.extend(page.get('SecretList', []))
        # A password waiting for its directory sync task is a secret of its own, under
        # a random name: a series each would never end. One count instead, of those
        # an hour old, which the task should have deleted within seconds.
        passwords = [secret for secret in secrets if _holds_a_password(secret)]
        secrets = [secret for secret in secrets if not _holds_a_password(secret)]
        metrics.level(
            'secret.password_sync_waiting',
            sum(
                1
                for secret in passwords
                # the filter above matches a tag value by prefix: this cluster's only
                if _tags(secret).get('idea:ClusterName') == cluster
                and secret.get('CreatedDate') is not None
                and now - secret['CreatedDate'].timestamp() > 3600
            ),
        )
        for secret in sorted(secrets, key=lambda s: s.get('Name', ''))[
            : self.MAX_SECRETS
        ]:
            name = tag_value(secret.get('Name', '').removeprefix(f'{cluster}-'))
            changed = secret.get('LastChangedDate') or secret.get('CreatedDate')
            if changed is not None:
                metrics.level(
                    'secret.age_days',
                    (now - changed.timestamp()) / SECONDS_PER_DAY,
                    secret=name,
                )
            next_rotation = secret.get('NextRotationDate')
            if next_rotation is not None:
                metrics.level(
                    'secret.days_to_rotation',
                    (next_rotation.timestamp() - now) / SECONDS_PER_DAY,
                    secret=name,
                )


class AdAutomationCollector(HealthCollector):
    """the agent's queue and whether its loop still reaches the queue at all."""

    name = 'ad_automation'
    interval_seconds = 60
    time_limit_seconds = 30

    def __init__(self, context):
        self.context = context

    def collect(self, metrics: HealthMetrics, now: Optional[float] = None) -> None:
        agent = getattr(self.context, 'ad_automation_agent', None)
        if agent is None:
            return
        now = now or time.time()
        attributes = (
            self.context.aws()
            .sqs()
            .get_queue_attributes(
                QueueUrl=agent.ad_automation_sqs_queue_url,
                AttributeNames=[
                    'ApproximateNumberOfMessages',
                    'ApproximateNumberOfMessagesNotVisible',
                ],
            )
            .get('Attributes', {})
        )
        metrics.level(
            'ad.queue_depth', int(attributes.get('ApproximateNumberOfMessages', 0))
        )
        metrics.level(
            'ad.queue_in_flight',
            int(attributes.get('ApproximateNumberOfMessagesNotVisible', 0)),
        )
        metrics.level('ad.seconds_since_queue_read', now - agent.last_queue_read)
        if agent.password_days_left is not None:
            metrics.level('ad.admin_password_days_to_expiry', agent.password_days_left)


class DirectoryCollector(HealthCollector):
    """
    What makes a file server lose the directory, read over the existing metrics login, every
    check on its own so one the login's role lacks reports as forbidden and the others go on.
    Reads settings; never a file or a user. ONTAP's event log is not read: it is kept at
    cluster level and no SVM login can be granted it, so a count of its secd events could
    never be sent from the login this uses. ldap_up and the home directory probe show the same
    failures.
    """

    name = 'directory'
    interval_seconds = 300
    time_limit_seconds = 120
    READ_TIMEOUT_SECONDS = 10

    def __init__(self, context, storage_metrics):
        self.context = context
        self.storage_metrics = storage_metrics

    def domain_controller_addresses(self) -> Optional[Set[str]]:
        directory_id = self.context.config().get_string('directoryservice.directory_id')
        if Utils.is_empty(directory_id):
            return None
        described = (
            self.context.aws()
            .ds()
            .describe_directories(DirectoryIds=[directory_id])
            .get('DirectoryDescriptions', [])
        )
        if not described:
            raise NoControllers()
        return set(described[0].get('DnsIpAddrs', []))

    def collect(self, metrics: HealthMetrics) -> None:
        # None with no directory to ask. One that would not answer is not that: its read is
        # reported, on no file system, and the DNS check that needs it says so, never ok.
        controllers = None
        controllers_unread = False
        try:
            controllers = self.domain_controller_addresses()
            if controllers is not None:
                metrics.level(
                    'directory.check_ok',
                    1,
                    check='dc_addresses',
                    reason='ok',
                    filesystem='none',
                )
        except Exception as e:
            controllers_unread = True
            metrics.level(
                'directory.check_ok',
                0,
                check='dc_addresses',
                reason='not_found'
                if isinstance(e, NoControllers)
                else failure_reason(e),
                filesystem='none',
            )
        verify_tls = self.context.config().get_bool(
            f'{self.context.module_id()}.metrics.storage.verify_tls', False
        )
        for target in self.storage_metrics.targets():
            # the same tag on every check of a target: an endpoint that is not an FSx name
            # (an address, a name of the operator's own) has no id, so it goes by its name
            filesystem = tag_value(fs_id_from_host(target.endpoint) or target.name)
            password = self.context.config().get_secret(target.password_key)
            metrics.level(
                'directory.check_ok',
                0 if Utils.is_empty(password) else 1,
                check='login',
                reason='no_password' if Utils.is_empty(password) else 'ok',
                filesystem=filesystem,
            )
            if Utils.is_empty(password):
                continue
            client = OntapClient(
                target.endpoint,
                target.username,
                password,
                verify_tls=verify_tls,
                timeout=self.READ_TIMEOUT_SECONDS,
            )
            self._check(metrics, client, filesystem, 'ldap', LDAP_PATH, self._ldap)
            self._check(
                metrics,
                client,
                filesystem,
                'cifs_security',
                CIFS_PATH,
                lambda m, f, records: self._cifs(m, f, records, client),
            )
            self._check(
                metrics,
                client,
                filesystem,
                'dns',
                DNS_PATH,
                lambda m, f, records: self._dns(
                    m, f, records, controllers, controllers_unread
                ),
            )

    @staticmethod
    def _check(metrics, client, filesystem, check, path, read):
        try:
            records = Utils.get_value_as_list('records', client.first_page(path), [])
            reason = read(metrics, filesystem, records) or 'ok'
            metrics.level(
                'directory.check_ok',
                1,
                check=check,
                reason=reason,
                filesystem=filesystem,
            )
        except Exception as e:
            metrics.level(
                'directory.check_ok',
                0,
                check=check,
                reason=DirectoryCollector._reason(e),
                filesystem=filesystem,
            )

    @staticmethod
    def _reason(error: Exception) -> str:
        if isinstance(error, FileNotFoundError):
            return 'unsupported'
        if isinstance(error, NoRecords):
            return 'no_records'
        if isinstance(error, NoControllers):
            return 'no_controllers'
        if isinstance(error, OntapRequestFailed):
            # the status ONTAP gave, never the text: a host name or an address in a connection
            # error can spell 401 or 403
            return {401: 'unauthorized', 403: 'forbidden'}.get(
                error.status_code, 'error'
            )
        name = type(error).__name__
        return 'unreachable' if 'imeout' in name or 'Connection' in name else 'error'

    @staticmethod
    def _svm(record) -> str:
        return tag_value(
            Utils.get_value_as_string(
                'name', Utils.get_value_as_dict('svm', record, {})
            )
        )

    def _ldap(self, metrics, filesystem, records):
        for record in records:
            state = Utils.get_value_as_string(
                'state', Utils.get_value_as_dict('status', record, {})
            )
            if Utils.is_empty(state):
                continue  # a release without the field says nothing either way
            metrics.level(
                'directory.ldap_up',
                1 if state == 'up' else 0,
                svm=self._svm(record),
                filesystem=filesystem,
            )

    @staticmethod
    def _svm_visible(client) -> bool:
        try:
            return bool(
                Utils.get_value_as_list('records', client.first_page(SVMS_PATH), [])
            )
        except Exception:
            return False

    def _cifs(self, metrics, filesystem, records, client):
        if not records:
            if not self._svm_visible(client):
                raise NoRecords()
            return 'no_cifs_server'  # an NFS-only SVM: nothing to sign
        for record in records:
            value = Utils.get_value_as_string(
                'session_security', Utils.get_value_as_dict('security', record, {})
            )
            if Utils.is_empty(value):
                continue
            metrics.level(
                'directory.ldap_session_signed',
                # signed is what it is known to be, as in the drift checks: any other value is 0
                1 if value in SIGNED_SESSION_SECURITY else 0,
                svm=self._svm(record),
                filesystem=filesystem,
            )

    def _dns(self, metrics, filesystem, records, controllers, controllers_unread):
        if controllers_unread:
            raise NoControllers()
        if controllers is None:
            return  # not a managed directory: nothing to hold the servers to
        for record in records:
            servers = set(Utils.get_value_as_list('servers', record, []))
            metrics.level(
                'directory.dns_on_domain_controllers',
                1 if servers and servers <= controllers else 0,
                svm=self._svm(record),
                filesystem=filesystem,
            )


class HomeDirectoryCollector(HealthCollector):
    """
    A timed look at the home directory, in its own process with a hard limit: a stat on a dead
    server never returns. One process per task at most, whatever the server does.
    """

    name = 'home_probe'
    interval_seconds = 300
    time_limit_seconds = 30
    PROBE_SECONDS = 15

    def __init__(self, context):
        self.context = context
        self._probe: Optional[IsolatedProbe] = None

    def collect(self, metrics: HealthMetrics) -> None:
        if self._probe is None:
            self._probe = IsolatedProbe(
                python_probe(HOME_PROBE_SCRIPT, USER_HOME_DIR_BASE), self.PROBE_SECONDS
            )
        result = self._probe.run()
        reason = result.status
        if result.status == 'failed':
            reason = HOME_PROBE_REASONS.get(result.returncode, 'error')
        metrics.level(
            'directory.home_stat_ok', 1 if result.status == 'ok' else 0, reason=reason
        )
        if result.seconds is not None:
            metrics.level('directory.home_stat_seconds', result.seconds)
