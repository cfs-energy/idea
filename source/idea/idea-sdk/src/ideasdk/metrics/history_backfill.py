from copy import deepcopy
from datetime import date
import re
import threading
import time
import uuid

import arrow

from ideadatamodel import exceptions
from ideasdk.metrics.datadog_api import DatadogAPI
from ideasdk.metrics.datadog_format import DatadogFormat


def date_window(start_date, end_date):
    try:
        if not all(
            isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)
            for value in (start_date, end_date)
        ):
            raise ValueError('Invalid date format')
        start = arrow.get(date.fromisoformat(start_date))
        end = arrow.get(date.fromisoformat(end_date))
    except (TypeError, ValueError):
        raise exceptions.invalid_params('start_date and end_date must be YYYY-MM-DD')
    today = arrow.utcnow().floor('day')
    if start < today.shift(months=-15) or end > today or start > end:
        raise exceptions.invalid_params(
            'Dates must be ordered and within the last 15 months'
        )
    return start, end.shift(days=1)


def dry_run_value(payload):
    value = payload.get('dry_run', True)
    if not isinstance(value, bool):
        raise exceptions.invalid_params('dry_run must be a boolean')
    return value


class CapturingPublisher:
    def __init__(self, context):
        self.context = context
        self.entries = []

    def __getattr__(self, name):
        return getattr(self.context, name)

    def config(self):
        return self

    def get_string(self, key, *args, **kwargs):
        if key == 'metrics.provider':
            return 'dogstatsd'
        return self.context.config().get_string(key, *args, **kwargs)

    def service_registry(self):
        return self

    def get_service(self, name):
        if name == 'metrics-service':
            return self
        return self.context.service_registry().get_service(name)

    def publish(self, metric_data):
        self.entries.extend(DatadogFormat.complete_entries(metric_data))


class MetricsHistoryBackfill:
    LEASE_SECONDS = 60
    HEARTBEAT_SECONDS = 10

    def __init__(self, context, kind):
        self.context = context
        self.kind = kind
        self.key = f'{context.module_id()}.metrics.backfill.{kind}'
        self.lock = threading.RLock()
        self.thread = None
        self._status = self.empty_status()
        self._db = None

    @property
    def db(self):
        # Read on first use: the API layer builds this before every context has its settings table.
        if self._db is None:
            self._db = getattr(self.context.config(), 'db', None)
        return self._db

    @staticmethod
    def empty_status():
        return dict(
            state='idle',
            jobs_scanned=0,
            points_sent=0,
            points_built=0,
            points_skipped=0,
            errors=0,
            started_at=None,
            finished_at=None,
            dry_run=True,
            last_error=None,
            owner=None,
            heartbeat_at=None,
            lease_expires_at=None,
        )

    def status(self):
        with self.lock:
            if self.db is not None:
                item = self.db.cluster_settings_table.get_item(
                    Key={'key': self.key}, ConsistentRead=True
                ).get('Item', {})
                status = deepcopy(item.get('value', self.empty_status()))
            else:
                status = deepcopy(self._status)
            if (
                status['state'] == 'running'
                and (status.get('lease_expires_at') or 0) <= time.time()
            ):
                status['state'] = 'interrupted'
            return status

    def save(self):
        if self.db is not None:
            self.db.set_config_entry(self.key, deepcopy(self._status))

    def start(self, dry_run, **options):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise exceptions.invalid_params('A metrics backfill is already running')
            try:
                self.context.distributed_lock().acquire(key=self.key)
            except Exception:
                raise exceptions.invalid_params(
                    'A metrics backfill is already running or its lock is unavailable'
                )
            try:
                self._status = self.empty_status()
                self._status.update(
                    state='running',
                    dry_run=dry_run,
                    started_at=arrow.utcnow().isoformat(),
                    owner=str(uuid.uuid4()),
                    heartbeat_at=int(time.time()),
                    lease_expires_at=int(time.time()) + self.LEASE_SECONDS,
                )
                self.save()
                result = deepcopy(self._status)
                self.thread = threading.Thread(
                    target=self._run,
                    args=(dry_run, options),
                    daemon=True,
                    name=f'{self.kind}-metrics-history',
                )
                self.thread.start()
                return result
            except Exception:
                self.context.distributed_lock().release(key=self.key)
                raise

    def progress(self, **counts):
        with self.lock:
            self.context.distributed_lock().assert_held(key=self.key)
            for name, count in counts.items():
                self._status[name] += count
            self.save()

    def send(self, capture, transport, dry_run):
        if not capture.entries:
            return
        entries = [
            e for e in capture.entries if e['MetricType'] in ('Counter', 'Gauge')
        ]
        self.progress(
            points_built=len(entries),
            points_skipped=len(capture.entries) - len(entries),
        )
        self.context.distributed_lock().assert_held(key=self.key)
        if not dry_run:
            transport.log(
                entries, on_sent=lambda count: self.progress(points_sent=count)
            )
        capture.entries.clear()

    def heartbeat(self, stopped):
        while not stopped.wait(self.HEARTBEAT_SECONDS):
            try:
                with self.lock:
                    self.context.distributed_lock().assert_held(key=self.key)
                    self._status.update(
                        heartbeat_at=int(time.time()),
                        lease_expires_at=int(time.time()) + self.LEASE_SECONDS,
                    )
                    self.save()
            except Exception:
                return

    def _run(self, dry_run, options):
        stopped = threading.Event()
        heartbeat = threading.Thread(
            target=self.heartbeat, args=(stopped,), daemon=True
        )
        heartbeat.start()
        try:
            transport = None if dry_run else DatadogAPI.from_context(self.context)
            self.collect(transport, dry_run, **options)
            self.context.distributed_lock().assert_held(key=self.key)
            with self.lock:
                self._status['state'] = 'completed'
        except Exception as error:
            with self.lock:
                self._status['state'] = 'failed'
                self._status['errors'] += 1
                self._status['last_error'] = type(error).__name__
                response = getattr(error, 'response', None)
                if response is not None:
                    self._status['last_error'] += f' (HTTP {response.status_code})'
        finally:
            stopped.set()
            heartbeat.join()
            try:
                with self.lock:
                    self._status['finished_at'] = arrow.utcnow().isoformat()
                    self.context.distributed_lock().assert_held(key=self.key)
                    self.save()
            except Exception as error:
                self.context.logger().warning(
                    f'{self.kind} metrics backfill status could not be saved: {type(error).__name__}'
                )
            finally:
                self.context.logger().info(
                    f'{self.kind} metrics backfill: {self._status}'
                )
                self.context.distributed_lock().release(key=self.key)

    def collect(self, transport, dry_run, **options):
        raise NotImplementedError
