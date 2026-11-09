"""
What must hold on every cluster, read once a day by the leader and never changed here.

Each check gives a state: ok, drift, or unknown when it could not be read, because a read that
failed says nothing about the setting. idea.health.drift.state carries it as 0, 2 or 1 per check,
with one log line after it. A cluster can only see itself, so what must agree between clusters is
published as a value, idea.health.drift.value, for Datadog to compare: this module does not know
what the other clusters hold.

Reads settings and metadata: from ONTAP, over the storage metrics login, configuration and volume
names only, never a file or a user. Hung reads cannot reach the service: each group of checks has
its own thread and limit, and one still running is left behind, reported and not started again.
"""

from ideaclustermanager.app.metrics.storage_metrics_service import (
    PROVIDER,
    SVMS_PATH,
    OntapClient,
    fs_id_from_host,
    tag_value,
)
from botocore.exceptions import ClientError
from ideasdk.metrics.health import HealthCollector, HealthMetrics, failure_reason
from ideasdk.utils import Utils

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
import hashlib
import threading
import time
import yaml

OK, DRIFT, UNKNOWN = 'ok', 'drift', 'unknown'
# drift highest, so the largest value in a window is the worst thing seen in it
STATE_VALUES = {OK: 0, UNKNOWN: 1, DRIFT: 2}
# seal encrypts as well as signs: stronger than the sign this asks for at least
SIGNED_SESSION_SECURITY = ('sign', 'seal')

# a little under a day, so a monitor's one day window always holds a reading
RUN_EVERY_SECONDS = 23 * 3600
WAKE_EVERY_SECONDS = 300
GROUP_LIMIT_SECONDS = 60
ONTAP_READ_TIMEOUT_SECONDS = 10

LDAP_PATH = '/api/name-services/ldap?fields=svm.name,session_security,status&return_records=true'
# the whole security block: the AES field moved between ONTAP releases, and asking for one this
# release lacks fails the read
CIFS_PATH = (
    '/api/protocols/cifs/services?fields=svm.name,enabled,security&return_records=true'
)
DNS_PATH = (
    '/api/name-services/dns?fields=svm.name,servers,dynamic_dns&return_records=true'
)
VOLUMES_PATH = (
    '/api/storage/volumes?fields=name,is_svm_root,analytics.state'
    '&return_records=true&max_records=1000'
)

ONTAP_CHECKS = (
    'ontap_cifs_session_security',
    'ontap_ldap_session_security',
    'ontap_dns_domain_controllers',
    'ontap_cifs_aes',
    'ontap_secure_ddns',
    'ontap_machine_account',
    'ontap_analytics',
    'fsx_parity',
)

VALUES_FILE_S3_KEY = 'values/values.yml'
SSH_CLIENT_IP_KEY = 'cluster.network.ssh_client_ip'
# settings values.yml overwrites on every upgrade (VALUES_OWNED_KEYS in ideactl), by values.yml key
VALUES_OWNED = {'ssh_client_ip': SSH_CLIENT_IP_KEY}
# one value that must equal the release's template for the cluster to behave as designed. only
# what the release fixes for everyone: an operator's choice is not drift. a test holds each to the
# template.
MUST_MATCH = {
    # a desktop and a job give up at the same point; the template says these must match
    'virtual-desktop-controller.dcv_session.provisioning_timeout_seconds': 1800,
    'scheduler.job_provisioning.stack_provisioning_timeout_seconds': 1800,
}
BASTION_VALUES = {
    'public': 'bastion-host.public',
    'cloudwatch_logs': 'bastion-host.cloudwatch_logs.enabled',
    'desired_count': 'ecs.tasks.bastion-host.desired',
}


@dataclass
class DriftResult:
    check: str
    state: str
    scope: str = 'cluster'
    # plain words for the log. never a secret; a server address at most
    detail: str = ''


@dataclass
class DriftValue:
    check: str
    setting: str
    value: str
    scope: str = 'cluster'


def digest(values: Iterable[str]) -> str:
    """order and duplicates do not matter, and the addresses stay out of the tag."""
    joined = '\n'.join(sorted({str(value).strip() for value in values}))
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


def read_reason(error: Exception) -> str:
    """an answer from ONTAP is in the message, so only the status is kept."""
    if isinstance(error, ClientError):
        return failure_reason(error)
    text = str(error)
    for status in ('401', '403', '404', '500', '503'):
        if f': {status}:' in text or f'returned {status}' in text:
            return {'401': 'login_rejected', '403': 'forbidden'}.get(
                status, f'http_{status}'
            )
    name = type(error).__name__.lower()
    return 'timeout' if 'timeout' in name else 'unreachable'


def first_named(records: List[Dict]) -> Optional[Dict]:
    return records[0] if records else None


# a role without /api/svm/svms gets 200 and no records for a CIFS server that is there
NO_CIFS_RECORDS = (
    'no CIFS server in the answer and the SVM is not visible: the role lacks /api/svm/svms'
)
CIFS_UNSEEN = (UNKNOWN, NO_CIFS_RECORDS)


def no_cifs_server(svm_visible: bool, smb_expected: bool) -> Tuple[str, str]:
    """
    an empty CIFS answer. from a role that cannot see the SVM it says nothing. from one that can,
    the SVM has no CIFS server: drift where the cluster mounts it over SMB, fine where it is NFS.
    """
    if not svm_visible:
        return CIFS_UNSEEN
    if smb_expected:
        return DRIFT, 'no CIFS server on the SVM, and the cluster mounts it over SMB'
    return OK, 'no CIFS server on the SVM, and the cluster mounts it over NFS only'


def check_cifs(
    records: List[Dict], scope: str, missing: Tuple[str, str] = CIFS_UNSEEN
) -> List[DriftResult]:
    """the checks read from the CIFS server. missing is what no record means: no_cifs_server."""
    record = first_named(records)
    if record is None:
        return [
            DriftResult(check, missing[0], scope, missing[1])
            for check in (
                'ontap_cifs_session_security',
                'ontap_cifs_aes',
            )
        ]
    security = Utils.get_value_as_dict('security', record, {})
    results = []
    session_security = security.get('session_security')
    if session_security is None:
        results.append(
            DriftResult(
                'ontap_cifs_session_security', UNKNOWN, scope, 'not in the answer'
            )
        )
    else:
        results.append(
            DriftResult(
                'ontap_cifs_session_security',
                OK if session_security in SIGNED_SESSION_SECURITY else DRIFT,
                scope,
                f'session-security-for-ad-ldap is {session_security}, must be sign or seal',
            )
        )
    advertised = security.get('advertised_kdc_encryptions')
    aes = security.get('kdc_encryption')
    if advertised is not None:
        has_aes = any(str(item).startswith('aes') for item in advertised)
    elif aes is not None:
        has_aes = bool(aes)
    else:
        results.append(
            DriftResult('ontap_cifs_aes', UNKNOWN, scope, 'not in the answer')
        )
        return results
    results.append(
        DriftResult(
            'ontap_cifs_aes',
            OK if has_aes else DRIFT,
            scope,
            'AES is on' if has_aes else 'AES is off for Kerberos to the directory',
        )
    )
    return results


def check_ldap(records: List[Dict], scope: str) -> List[DriftResult]:
    record = first_named(records)
    if record is None:
        return [
            DriftResult(
                'ontap_ldap_session_security', DRIFT, scope, 'no LDAP client on the SVM'
            )
        ]
    session_security = record.get('session_security')
    if session_security is None:
        return [
            DriftResult(
                'ontap_ldap_session_security', UNKNOWN, scope, 'not in the answer'
            )
        ]
    return [
        DriftResult(
            'ontap_ldap_session_security',
            OK if session_security in SIGNED_SESSION_SECURITY else DRIFT,
            scope,
            f'LDAP client session security is {session_security}, must be sign or seal',
        )
    ]


def check_machine_account(
    cifs: List[Dict],
    ldap: List[Dict],
    scope: str,
    missing: Tuple[str, str] = CIFS_UNSEEN,
) -> List[DriftResult]:
    """
    the machine account as ONTAP sees it: the server is on and its LDAP client answers. the
    account's own record would take reading the directory for something ONTAP already acts on.
    """
    cifs_record, ldap_record = first_named(cifs), first_named(ldap)
    if cifs_record is None:
        return [DriftResult('ontap_machine_account', missing[0], scope, missing[1])]
    if ldap_record is None:
        return [
            DriftResult(
                'ontap_machine_account', DRIFT, scope, 'no LDAP client on the SVM'
            )
        ]
    state = Utils.get_value_as_dict('status', ldap_record, {}).get('state')
    enabled = cifs_record.get('enabled')
    if state is None or enabled is None:
        return [
            DriftResult('ontap_machine_account', UNKNOWN, scope, 'not in the answer')
        ]
    healthy = enabled is True and state == 'up'
    return [
        DriftResult(
            'ontap_machine_account',
            OK if healthy else DRIFT,
            scope,
            f'cifs enabled {enabled}, ldap {state}',
        )
    ]


def check_dns(
    records: List[Dict], controllers: Optional[Set[str]], scope: str
) -> List[DriftResult]:
    record = first_named(records)
    if record is None:
        return [
            DriftResult(check, DRIFT, scope, 'no DNS configuration on the SVM')
            for check in ('ontap_dns_domain_controllers', 'ontap_secure_ddns')
        ]
    results = []
    servers = set(record.get('servers') or [])
    if controllers is None:
        results.append(
            DriftResult(
                'ontap_dns_domain_controllers',
                UNKNOWN,
                scope,
                'the domain controller addresses could not be read',
            )
        )
    else:
        results.append(
            DriftResult(
                'ontap_dns_domain_controllers',
                OK if servers and servers <= controllers else DRIFT,
                scope,
                f'DNS servers {sorted(servers)}, domain controllers {sorted(controllers)}',
            )
        )
    dynamic = record.get('dynamic_dns')
    if not isinstance(dynamic, dict) or 'use_secure' not in dynamic:
        results.append(
            DriftResult('ontap_secure_ddns', UNKNOWN, scope, 'not in the answer')
        )
    else:
        secure = dynamic.get('enabled') is True and dynamic.get('use_secure') is True
        results.append(
            DriftResult(
                'ontap_secure_ddns',
                OK if secure else DRIFT,
                scope,
                f'dynamic DNS enabled {dynamic.get("enabled")}, '
                f'secure {dynamic.get("use_secure")}',
            )
        )
    return results


def analytics_value(volumes: List[Dict]) -> str:
    """on, off, or mixed across the data volumes. the SVM root has nothing to analyse."""
    states = {
        tag_value(Utils.get_value_as_dict('analytics', volume, {}).get('state'))
        for volume in volumes
        if volume.get('is_svm_root') is not True
    }
    if not states:
        return 'none'
    return states.pop() if len(states) == 1 else 'mixed'


class DriftChecks:
    def __init__(self, context, storage_metrics):
        self.context = context
        self.storage_metrics = storage_metrics
        self.logger = context.logger('drift-checks')
        self.metrics = HealthMetrics(context)
        self._running: Dict[str, threading.Thread] = {}

    # the groups

    def domain_controllers(self) -> Optional[Set[str]]:
        directory_id = self.context.config().get_string('directoryservice.directory_id')
        if Utils.is_empty(directory_id):
            return None
        described = (
            self.context.aws()
            .ds()
            .describe_directories(DirectoryIds=[directory_id])
            .get('DirectoryDescriptions', [])
        )
        return set(described[0].get('DnsIpAddrs', [])) if described else None

    def ontap_entries(self) -> List[str]:
        shared_storage = (
            self.context.config().get_config('shared-storage', default={}) or {}
        )
        return [
            name
            for name in shared_storage
            if hasattr(shared_storage.get(name), 'get')
            and Utils.get_value_as_string('provider', shared_storage.get(name))
            == PROVIDER
        ]

    @staticmethod
    def svm_visible(client) -> bool:
        try:
            return len(client.get(SVMS_PATH)) > 0
        except Exception:
            return False

    def check_ontap_target(self, target, controllers) -> Dict:
        scope = tag_value(target.name)
        results: List[DriftResult] = []
        values: List[DriftValue] = []
        password = self.context.config().get_secret(target.password_key)
        if Utils.is_empty(password):
            return dict(
                results=[
                    DriftResult(check, UNKNOWN, scope, 'no_password')
                    for check in ONTAP_CHECKS
                ],
                values=[],
            )
        client = OntapClient(
            target.endpoint,
            target.username,
            password,
            verify_tls=self.context.config().get_bool(
                f'{self.context.module_id()}.metrics.storage.verify_tls', False
            ),
            timeout=ONTAP_READ_TIMEOUT_SECONDS,
        )

        def read(path, checks):
            try:
                return client.get(path)
            except Exception as e:
                reason = read_reason(e)
                results.extend(
                    DriftResult(check, UNKNOWN, scope, reason) for check in checks
                )
                return None

        cifs = read(CIFS_PATH, ['ontap_cifs_session_security', 'ontap_cifs_aes'])
        ldap = read(LDAP_PATH, ['ontap_ldap_session_security'])
        dns = read(DNS_PATH, ['ontap_dns_domain_controllers', 'ontap_secure_ddns'])
        volumes = read(VOLUMES_PATH, ['ontap_analytics'])
        missing = CIFS_UNSEEN
        if cifs == []:
            missing = no_cifs_server(
                self.svm_visible(client),
                Utils.is_not_empty(
                    self.context.config().get_string(
                        f'shared-storage.{target.name}.{PROVIDER}.svm.smb_dns'
                    )
                ),
            )
        if cifs is not None:
            results.extend(check_cifs(cifs, scope, missing))
        if ldap is not None:
            results.extend(check_ldap(ldap, scope))
        if cifs is None or ldap is None:
            results.append(
                DriftResult('ontap_machine_account', UNKNOWN, scope, 'see other reads')
            )
        else:
            results.extend(check_machine_account(cifs, ldap, scope, missing))
        if dns is not None:
            results.extend(check_dns(dns, controllers, scope))
        if volumes is not None:
            # compared across clusters: off is a choice until the owner turns it on everywhere
            results.append(DriftResult('ontap_analytics', OK, scope))
            values.append(
                DriftValue('ontap_analytics', 'state', analytics_value(volumes), scope)
            )
        fsx_results, fsx_values = self.check_fsx(target, scope)
        return dict(results=results + fsx_results, values=values + fsx_values)

    def check_fsx(self, target, scope):
        file_system_id = fs_id_from_host(target.endpoint)
        if Utils.is_empty(file_system_id):
            return [DriftResult('fsx_parity', UNKNOWN, scope, 'no file system id')], []
        try:
            described = (
                self.context.aws()
                .fsx()
                .describe_file_systems(FileSystemIds=[file_system_id])
                .get('FileSystems', [])
            )
        except Exception as e:
            return [
                DriftResult('fsx_parity', UNKNOWN, scope, type(e).__name__.lower())
            ], []
        if not described:
            return [DriftResult('fsx_parity', UNKNOWN, scope, 'not found')], []
        file_system = described[0]
        ontap = file_system.get('OntapConfiguration') or {}
        settings = {
            'deployment_type': ontap.get('DeploymentType'),
            'storage_type': file_system.get('StorageType'),
            # left out of the answer when automatic backups are off
            'backup_retention_days': ontap.get('AutomaticBackupRetentionDays', 0),
            'disk_iops_mode': (ontap.get('DiskIopsConfiguration') or {}).get('Mode'),
            'ha_pairs': ontap.get('HAPairs'),
        }
        return [DriftResult('fsx_parity', OK, scope)], [
            DriftValue('fsx_parity', name, tag_value(value), scope)
            for name, value in settings.items()
        ]

    def check_ad_secret(self) -> Dict:
        """the secret as it is now, not the password the task read at start. a bind, no search."""
        client = getattr(self.context, 'ldap_client', None)
        if client is None or getattr(self.context, 'ad_automation_agent', None) is None:
            return dict(results=[], values=[])
        binds = client.root_password_binds(client.fetch_root_password())
        return dict(
            results=[
                DriftResult(
                    'ad_agent_bind',
                    OK if binds else DRIFT,
                    detail='the directory accepts the secret'
                    if binds
                    else 'the directory rejects the password in the secret',
                )
            ],
            values=[],
        )

    def check_settings(self) -> Dict:
        config = self.context.config()
        results: List[DriftResult] = []
        values: List[DriftValue] = []
        addresses = config.get_list(SSH_CLIENT_IP_KEY, default=[]) or []
        results.append(
            DriftResult(
                'ssh_client_ip',
                OK if addresses else DRIFT,
                detail=f'{len(addresses)} entries',
            )
        )
        values.append(DriftValue('ssh_client_ip', 'digest', digest(addresses)))
        values.append(DriftValue('ssh_client_ip', 'count', str(len(set(addresses)))))
        if Utils.is_not_empty(config.get_config('bastion-host', default=None)):
            results.append(DriftResult('bastion_settings', OK))
            for name, key in BASTION_VALUES.items():
                values.append(
                    DriftValue(
                        'bastion_settings',
                        name,
                        tag_value(config.get_string(key, default=None)),
                    )
                )
        wrong = sorted(
            key
            for key, expected in MUST_MATCH.items()
            # a row that is absent reads as the code's default, which is this value
            if config.get_int(key, default=expected) != expected
        )
        results.append(
            DriftResult(
                'settings_must_match',
                DRIFT if wrong else OK,
                detail=', '.join(wrong) or f'{len(MUST_MATCH)} keys match the release',
            )
        )
        return dict(results=results, values=values)

    def check_values_file(self) -> Dict:
        """values.yml in the cluster bucket overwrites these rows on the next upgrade."""
        config = self.context.config()
        body = (
            self.context.aws()
            .s3()
            .get_object(
                Bucket=config.get_string('cluster.cluster_s3_bucket', required=True),
                Key=VALUES_FILE_S3_KEY,
            )['Body']
            .read()
        )
        values = yaml.safe_load(body) or {}
        stale = sorted(
            key
            for values_key, key in VALUES_OWNED.items()
            if {str(v).strip() for v in (values.get(values_key) or [])}
            != {str(v).strip() for v in (config.get_list(key, default=[]) or [])}
        )
        return dict(
            results=[
                DriftResult(
                    'values_owned_settings',
                    DRIFT if stale else OK,
                    detail=', '.join(stale) or 'the settings match values.yml',
                )
            ],
            values=[],
        )

    # running them

    def _bounded(
        self, group: str, checks: Iterable[str], scope: str, work: Callable[[], Dict]
    ) -> Dict:
        """the group's answer within its limit, or unknown for each of its checks."""
        previous = self._running.get(group)
        if previous is not None and previous.is_alive():
            return dict(
                results=[DriftResult(c, UNKNOWN, scope, 'stuck') for c in checks],
                values=[],
            )
        outcome: Dict = {}

        def run():
            try:
                outcome.update(work())
            except Exception as e:
                # the type, not the text: an error from a read can quote what it read
                outcome.update(
                    results=[
                        DriftResult(c, UNKNOWN, scope, read_reason(e)) for c in checks
                    ],
                    values=[],
                )

        worker = threading.Thread(target=run, name=f'drift-{group}', daemon=True)
        worker.start()
        worker.join(GROUP_LIMIT_SECONDS)
        if worker.is_alive():
            self._running[group] = worker
            return dict(
                results=[DriftResult(c, UNKNOWN, scope, 'timeout') for c in checks],
                values=[],
            )
        self._running.pop(group, None)
        return outcome

    def run_once(self) -> List[DriftResult]:
        results: List[DriftResult] = []
        values: List[DriftValue] = []

        def collect(answer):
            results.extend(answer['results'])
            values.extend(answer['values'])

        entries = self.ontap_entries()
        if entries:
            controller_answer = self._bounded(
                'domain-controllers',
                [],
                'cluster',
                lambda: dict(results=[], values=[], found=self.domain_controllers()),
            )
            controllers = controller_answer.get('found')
            targets = {t.name: t for t in self.storage_metrics.targets()}
            for name in entries:
                target = targets.get(name)
                scope = tag_value(name)
                if target is None:
                    results.extend(
                        DriftResult(c, UNKNOWN, scope, 'no_login') for c in ONTAP_CHECKS
                    )
                    continue
                collect(
                    self._bounded(
                        f'ontap-{name}',
                        ONTAP_CHECKS,
                        scope,
                        lambda t=target: self.check_ontap_target(t, controllers),
                    )
                )
        collect(self._bounded('ad', ['ad_agent_bind'], 'cluster', self.check_ad_secret))
        collect(
            self._bounded(
                'settings',
                ['ssh_client_ip', 'bastion_settings', 'settings_must_match'],
                'cluster',
                self.check_settings,
            )
        )
        collect(
            self._bounded(
                'values-file',
                ['values_owned_settings'],
                'cluster',
                self.check_values_file,
            )
        )
        self._publish(results, values)
        return results

    def _publish(self, results: List[DriftResult], values: List[DriftValue]) -> None:
        for result in results:
            self.metrics.level(
                'drift.state',
                STATE_VALUES[result.state],
                check=result.check,
                scope=result.scope,
            )
            line = (
                f'IDEA_DRIFT_CHECK check={result.check} scope={result.scope} '
                f'state={result.state}'
            )
            line = f'{line} detail={result.detail}' if result.detail else line
            if result.state == OK:
                self.logger.info(line)
            else:
                self.logger.warning(line)
        for value in values:
            self.metrics.level(
                'drift.value',
                1,
                check=value.check,
                setting=value.setting,
                current=value.value,
                scope=value.scope,
            )


class DriftCheckCollector(HealthCollector):
    """the leader, once a day. It wakes often so a new leader does not wait a day to take over."""

    name = 'drift_checks'
    interval_seconds = WAKE_EVERY_SECONDS
    time_limit_seconds = 900

    def __init__(self, context, storage_metrics):
        self.checks = DriftChecks(context, storage_metrics)
        self._last_run: Optional[float] = None

    def collect(self, metrics, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if self._last_run is not None and now - self._last_run < RUN_EVERY_SECONDS:
            return
        self._last_run = now
        self.checks.run_once()
