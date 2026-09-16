"""
Fakes for the collector tests: a context whose metrics service records what BaseMetrics
publishes, and nothing that reaches AWS.
"""

from typing import Dict, List


class FakeLogger:
    def __init__(self):
        self.lines: List[str] = []

    def _record(self, level, message):
        self.lines.append(f'{level} {message}')

    def debug(self, message, *args, **kwargs):
        self._record('debug', message)

    def info(self, message, *args, **kwargs):
        self._record('info', message)

    def warning(self, message, *args, **kwargs):
        self._record('warning', message)

    def error(self, message, *args, **kwargs):
        self._record('error', message)

    def exception(self, message, *args, **kwargs):
        self._record('exception', message)


class FakeConfig:
    def __init__(self, values: Dict, secrets: Dict = None):
        self.values = {'cluster.cluster_s3_bucket': 'sample-bucket', **values}
        self.secrets = secrets or {}
        self.db = FakeSettingsDB()

    def _get(self, key, default):
        return self.values.get(key, default)

    def get_string(self, key, default=None, required=False, module_id=None):
        value = self._get(key, default)
        return None if value is None else str(value)

    def get_bool(self, key, default=None, required=False, module_id=None):
        value = self._get(key, default)
        return bool(value) if value is not None else default

    def get_int(self, key, default=None, required=False, module_id=None):
        value = self._get(key, default)
        return int(value) if value is not None else default

    def get_config(self, key, default=None, required=False, module_id=None):
        return self._get(key, default)

    def get_secret(self, key, default=None, required=False, module_id=None):
        arn = self._get(key, None)
        return self.secrets.get(arn) if arn else None


class FakeLock:
    def __init__(self):
        self.held: List[str] = []

    def acquire(self, key):
        self.held.append(key)

    def release(self, key):
        self.held.remove(key)


class RecordingMetricsService:
    def __init__(self):
        self.published: List[Dict] = []

    def publish(self, metric_data):
        self.published.extend(metric_data)


class FakeServiceRegistry:
    def __init__(self, metrics_service):
        self.metrics_service = metrics_service

    def register(self, service):
        pass

    def get_service(self, name):
        return self.metrics_service if name == 'metrics-service' else None


class FakeAws:
    def __init__(self, cost_explorer=None, partition='aws'):
        self._cost_explorer = cost_explorer
        self._partition = partition
        self._s3 = FakeObjectStore()

    def s3(self):
        return self._s3

    def cost_explorer(self):
        return self._cost_explorer

    def aws_partition(self):
        return self._partition


class FakeContext:
    def __init__(
        self, values: Dict, cost_explorer=None, partition='aws', secrets: Dict = None
    ):
        self._config = FakeConfig(values, secrets)
        self._logger = FakeLogger()
        self._aws = FakeAws(cost_explorer, partition)
        self._lock = FakeLock()
        self.metrics_service = RecordingMetricsService()
        self._registry = FakeServiceRegistry(self.metrics_service)

    def config(self):
        return self._config

    def logger(self, name=None):
        return self._logger

    def aws(self):
        return self._aws

    def cluster_name(self):
        return 'test-cluster'

    def module_id(self):
        return 'cluster-manager'

    def distributed_lock(self):
        return self._lock

    def service_registry(self):
        return self._registry

    def published(self, name=None):
        entries = [
            e
            for e in self.metrics_service.published
            if name is None or e['MetricName'] == name
        ]
        return entries

    @staticmethod
    def dimensions(entry) -> Dict[str, str]:
        return {d['Name']: d['Value'] for d in entry.get('Dimensions', [])}


class FakeSettingsDB:
    def __init__(self):
        self.cluster_settings_table = self
        self.values = {}
        self.reads = []
        self.writes = []
        self.lock = None
        self.fail_read = False
        self.fail_write = False

    def get_item(self, Key, ConsistentRead=False):
        assert self.lock is None or self.lock.held
        self.reads.append((Key, ConsistentRead))
        if self.fail_read:
            raise RuntimeError('settings read failed')
        key = Key['key']
        return {'Item': {'value': self.values[key]}} if key in self.values else {}

    def set_config_entry(self, key, value):
        assert self.lock is None or self.lock.held
        if self.fail_write:
            raise RuntimeError('settings write failed')
        self.values[key] = value
        self.writes.append((key, value))


class FakeCollectorSource:
    def __init__(self, clock):
        self.clock = clock
        self.calls = 0
        self.fail = False
        self.fs_id = 'fs-test'

    def _read(self):
        self.calls += 1
        self.clock['now'] += 10
        if self.fail:
            raise RuntimeError('source unavailable')

    def fetch_all(self, *args):
        from ideaclustermanager.app.metrics.cost_metrics_service import CostRow

        self._read()
        return [CostRow('cost', '2026-09-13', {'module': 'scheduler'}, 1, 2)]

    def volumes(self):
        self._read()
        return [{'name': 'data', 'space': {'size': 100, 'used': 50}}]

    def quota_reports(self):
        return []


class FakeObjectStore:
    def __init__(self):
        self.values = {}
        self.fail_write = False

    def put_object(self, Bucket, Key, Body, ContentType):
        if self.fail_write:
            raise RuntimeError('outbox write failed')
        self.values[Key] = Body

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        return {
            'Contents': [{'Key': key} for key in self.values if key.startswith(Prefix)]
        }

    def get_object(self, Bucket, Key):
        from io import BytesIO

        return {'Body': BytesIO(self.values[Key])}
