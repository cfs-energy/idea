"""
Storage levels from FSx for NetApp ONTAP, published as metrics.

Per shared-storage entry that names an ONTAP file system and carries read-only credentials,
the SVM management endpoint is read for two things: user quota reports, which are the only
per-user accounting the file system keeps, and volumes, for capacity and the SSD versus
capacity-pool footprint. These are levels, stamped at the read, not history.

  storage.used_bytes, storage.files_used        per user, volume, qtree
  storage.volume_size_bytes, storage.volume_used_bytes
  storage.volume_tier_bytes                     tier:ssd and tier:capacity_pool

The credentials belong to an ONTAP role that can read those two paths and nothing else; the
password sits in Secrets Manager and only its ARN is in the settings.
"""

from ideasdk.context import SocaContext
from ideasdk.metrics import BaseMetrics
from ideasdk.service import SocaService
from ideasdk.utils import Utils

from typing import Dict, List, Optional, Tuple
import arrow
import threading

import requests
import urllib3

PROVIDER = 'fsx_netapp_ontap'
QUOTA_REPORTS = (
    '/api/storage/quota/reports?return_records=true&max_records=1000'
    '&fields=svm.name,volume.name,qtree.name,type,users.name,users.id,space.used.total,files.used.total'
)
VOLUMES = (
    '/api/storage/volumes?return_records=true&max_records=1000'
    '&fields=name,svm.name,space.size,space.used,space.performance_tier_footprint,space.capacity_tier_footprint'
)


def fs_id_from_host(host: str) -> str:
    """the file system id is a label of every FSx endpoint name: svm-x.fs-y.fsx...."""
    for label in host.split('.'):
        if label.startswith('fs-'):
            return label
    return ''


def normalize_user(name: Optional[str], user_id: Optional[str]) -> str:
    """one tag per human: MIXED volumes report windows names as DOMAIN\\name and unix names
    bare, so the domain goes and the case folds. An unresolved identity keeps its id."""
    if Utils.is_not_empty(name) and name != '*':
        return name.rsplit('\\', 1)[-1].lower()
    if Utils.is_not_empty(user_id):
        return f'sid:{user_id}' if user_id.startswith('S-1-') else f'uid:{user_id}'
    return ''


def tag_value(value) -> str:
    text = str(value).strip().lower() if value is not None else ''
    return text.replace(' ', '_') if text else 'unknown'


class OntapClient:
    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout: int = 60,
    ):
        host = endpoint.replace('https://', '').replace('http://', '').rstrip('/')
        self.base = f'https://{host}'
        self.fs_id = fs_id_from_host(host)
        self.auth = (username, password)
        self.verify_tls = verify_tls
        self.timeout = timeout

    def get(self, path: str) -> List[Dict]:
        """one collection, every page."""
        records: List[Dict] = []
        while Utils.is_not_empty(path):
            if not self.verify_tls:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            response = requests.get(
                self.base + path,
                auth=self.auth,
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f'GET {path}: {response.status_code}: {response.text[:200]}'
                )
            page = response.json()
            records.extend(Utils.get_value_as_list('records', page, []))
            path = Utils.get_value_as_string(
                'href',
                Utils.get_value_as_dict(
                    'next', Utils.get_value_as_dict('_links', page, {}), {}
                ),
            )
        return records

    def quota_reports(self) -> List[Dict]:
        return self.get(QUOTA_REPORTS)

    def volumes(self) -> List[Dict]:
        return self.get(VOLUMES)


def user_usage(reports: List[Dict]) -> Dict[Tuple[str, str, str, str], Tuple[int, int]]:
    """(svm, volume, qtree, user) -> (bytes, files) from the user-type quota records."""
    usage: Dict[Tuple[str, str, str, str], Tuple[int, int]] = {}
    for record in reports:
        if Utils.get_value_as_string('type', record) != 'user':
            continue
        users = Utils.get_value_as_list('users', record, [])
        if len(users) == 0:
            continue
        user = normalize_user(
            Utils.get_value_as_string('name', users[0]),
            Utils.get_value_as_string('id', users[0]),
        )
        if Utils.is_empty(user):
            continue  # the default rule's row tracks nothing attributable
        key = (
            Utils.get_value_as_string(
                'name', Utils.get_value_as_dict('svm', record, {}), ''
            ),
            Utils.get_value_as_string(
                'name', Utils.get_value_as_dict('volume', record, {}), ''
            ),
            Utils.get_value_as_string(
                'name', Utils.get_value_as_dict('qtree', record, {}), ''
            ),
            user,
        )
        used_bytes = Utils.get_value_as_int(
            'total',
            Utils.get_value_as_dict(
                'used', Utils.get_value_as_dict('space', record, {}), {}
            ),
            0,
        )
        used_files = Utils.get_value_as_int(
            'total',
            Utils.get_value_as_dict(
                'used', Utils.get_value_as_dict('files', record, {}), {}
            ),
            0,
        )
        found = usage.get(key, (0, 0))
        usage[key] = (found[0] + used_bytes, found[1] + used_files)
    return usage


class StorageMetrics(BaseMetrics):
    def __init__(self, context: SocaContext):
        super().__init__(context, split_dimensions=False)
        self.with_required_dimension('host', context.cluster_name())

    def publish_gauge(self, name: str, value: float, dimensions: Dict[str, str]):
        self.push_dimensions()
        try:
            for key in sorted(dimensions):
                self.with_dimension(key, dimensions[key])
            self.gauge(
                MetricName=name,
                Value=value,
                Unit='Bytes' if name.endswith('_bytes') else 'Count',
            )
        finally:
            self.pop_dimensions()


def publish_storage(
    metrics: StorageMetrics, fs_id: str, volumes: List[Dict], reports: List[Dict]
) -> int:
    """gauges from one read: per-user usage, per-volume capacity and tier split. Returns the count."""
    published = 0
    filesystem = tag_value(fs_id)
    for (svm, volume, qtree, user), (used_bytes, used_files) in sorted(
        user_usage(reports).items()
    ):
        dimensions = {
            'user': tag_value(user),
            'volume': tag_value(volume),
            'svm': tag_value(svm),
            'filesystem': filesystem,
        }
        if Utils.is_not_empty(qtree):
            dimensions['qtree'] = tag_value(qtree)
        metrics.publish_gauge('storage.used_bytes', used_bytes, dimensions)
        metrics.publish_gauge('storage.files_used', used_files, dimensions)
        published += 2
    for volume in volumes:
        space = Utils.get_value_as_dict('space', volume, {})
        dimensions = {
            'volume': tag_value(Utils.get_value_as_string('name', volume)),
            'svm': tag_value(
                Utils.get_value_as_string(
                    'name', Utils.get_value_as_dict('svm', volume, {})
                )
            ),
            'filesystem': filesystem,
        }
        metrics.publish_gauge(
            'storage.volume_size_bytes',
            Utils.get_value_as_int('size', space, 0),
            dimensions,
        )
        metrics.publish_gauge(
            'storage.volume_used_bytes',
            Utils.get_value_as_int('used', space, 0),
            dimensions,
        )
        published += 2
        # Tier footprints are release-dependent fields; a used volume reporting zero for both
        # is a file system that does not serve them, so nothing is published rather than zero.
        ssd = Utils.get_value_as_int('performance_tier_footprint', space, 0)
        pool = Utils.get_value_as_int('capacity_tier_footprint', space, 0)
        if ssd > 0 or pool > 0:
            metrics.publish_gauge(
                'storage.volume_tier_bytes', ssd, {**dimensions, 'tier': 'ssd'}
            )
            metrics.publish_gauge(
                'storage.volume_tier_bytes',
                pool,
                {**dimensions, 'tier': 'capacity_pool'},
            )
            published += 2
    return published


class StorageTarget:
    def __init__(self, name: str, endpoint: str, username: str, password_key: str):
        self.name = name
        self.endpoint = endpoint
        self.username = username
        self.password_key = password_key


class StorageMetricsService(SocaService):
    def __init__(self, context: SocaContext):
        super().__init__(context)
        self.context = context
        self.logger = context.logger('storage-metrics')
        self._provider_warning_logged = False
        self._exit = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name='storage-metrics', daemon=True
        )

    def service_id(self) -> str:
        return 'storage-metrics'

    def _config_key(self, suffix: str) -> str:
        return f'{self.context.module_id()}.metrics.storage.{suffix}'

    def is_enabled(self) -> bool:
        if not self.context.config().get_bool(self._config_key('enabled'), False):
            return False
        provider = self.context.config().get_string('metrics.provider')
        if provider not in ('dogstatsd', 'cloudwatch'):
            if not self._provider_warning_logged:
                self.logger.warning(
                    f'storage metrics disabled for provider {provider!r}: storage gauges require gauge support'
                )
                self._provider_warning_logged = True
            return False
        return True

    def get_interval_seconds(self) -> int:
        return (
            max(
                1,
                self.context.config().get_int(self._config_key('interval_minutes'), 60),
            )
            * 60
        )

    def targets(self) -> List[StorageTarget]:
        """every shared-storage entry on ONTAP that carries metrics credentials."""
        found: List[StorageTarget] = []
        config = self.context.config()
        shared_storage = config.get_config('shared-storage', default={}) or {}
        for name in shared_storage:
            entry = shared_storage.get(name)
            if not isinstance(entry, dict) and not hasattr(entry, 'get'):
                continue
            if Utils.get_value_as_string('provider', entry) != PROVIDER:
                continue
            prefix = f'shared-storage.{name}.{PROVIDER}'
            username = config.get_string(f'{prefix}.metrics.username')
            endpoint = config.get_string(f'{prefix}.svm.management_dns')
            if Utils.is_empty(username) or Utils.is_empty(endpoint):
                continue
            found.append(
                StorageTarget(
                    name, endpoint, username, f'{prefix}.metrics.password_secret_arn'
                )
            )
        return found

    def start(self):
        if not self.is_enabled():
            self.logger.debug('storage metrics are disabled. skip.')
            return
        self._thread.start()

    def stop(self):
        self._exit.set()
        if self._thread.is_alive():
            self._thread.join()

    def _loop(self):
        while not self._exit.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.logger.exception(f'storage metrics failed: {e}')
            finally:
                self._exit.wait(self.get_interval_seconds())

    def run_once(self):
        lock_key = f'{self.context.module_id()}-storage-metrics'
        try:
            self.context.distributed_lock().acquire(key=lock_key)
        except Exception as e:
            self.logger.info(f'storage metrics are running elsewhere: {e}')
            return
        try:
            # The standalone collector has no settings table: one task, no checkpoint.
            db = getattr(self.context.config(), 'db', None)
            checkpoint_key = self._config_key('last_published')
            # A replica's settings cache can lag behind the previous lock holder.
            # A consistent read prevents a completed interval from being published twice.
            entry = (
                db.cluster_settings_table.get_item(
                    Key={'key': checkpoint_key}, ConsistentRead=True
                ).get('Item', {})
                if db is not None
                else {}
            )
            last_published = entry.get('value')
            if last_published is not None and (
                arrow.utcnow().timestamp() - float(last_published)
                < self.get_interval_seconds() / 2
            ):
                return
            verify_tls = self.context.config().get_bool(
                self._config_key('verify_tls'), False
            )
            metrics = StorageMetrics(self.context)
            succeeded = True
            for target in self.targets():
                try:
                    password = self.context.config().get_secret(target.password_key)
                    if Utils.is_empty(password):
                        succeeded = False
                        self.logger.warning(
                            f'{target.name}: no password at {target.password_key}. skip.'
                        )
                        continue
                    client = OntapClient(
                        target.endpoint,
                        target.username,
                        password,
                        verify_tls=verify_tls,
                    )
                    volumes = client.volumes()
                    reports = client.quota_reports()
                    published = publish_storage(metrics, client.fs_id, volumes, reports)
                    self.logger.info(
                        f'{target.name}: {published} storage gauges from {len(volumes)} volumes and {len(reports)} quota records'
                    )
                except Exception as e:
                    succeeded = False
                    self.logger.warning(f'{target.name}: storage read failed: {e}')
            if succeeded and db is not None:
                db.set_config_entry(checkpoint_key, arrow.utcnow().timestamp())
        finally:
            self.context.distributed_lock().release(key=lock_key)
