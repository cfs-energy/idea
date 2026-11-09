"""
The cluster's own record of what breaks: metrics that outlive the logs.

Names are published as idea.health.<concern>.<metric> on DogStatsD only: the tag sets are priced
per metric on CloudWatch. Each concern is one collector with its own thread, its own time limit
and its own off switch, and nothing a collector does can reach the service it runs in: a raised
error becomes a gauge, and a call that never returns is left behind and reported, not repeated.

Counts of things already recorded elsewhere are stamped at a fixed five minute bucket and carry
that bucket's whole total. Datadog keeps the last value per metric, tags and timestamp, so
reading the same stopped tasks again, from this task or the next leader, rewrites the same
point and never adds to it. Keep such a window under the age where the intake drops points.
"""

from ideasdk.utils import Utils
from ideadatamodel import constants

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from dataclasses import dataclass
from threading import Event, Thread
from typing import Dict, Iterable, List, Optional, Tuple
import random
import subprocess
import sys
import time

BUCKET_SECONDS = 300
SECONDS_PER_DAY = 86400
ENABLED_KEY = 'metrics.health.enabled'
# how soon a collector skipped for not being leader, or switched off, looks again
SKIPPED_RECHECK_SECONDS = 60

ACCESS_DENIED_CODES = {
    'AccessDenied',
    'AccessDeniedException',
    'UnauthorizedOperation',
    'UnrecognizedClientException',
}
THROTTLE_CODES = {'Throttling', 'ThrottlingException', 'RequestLimitExceeded'}


def bucket_of(epoch_seconds: float) -> int:
    return int(epoch_seconds) // BUCKET_SECONDS * BUCKET_SECONDS


def instance_family_of(job) -> str:
    instance_type = getattr(job, 'default_instance_type', None)
    return str(instance_type).split('.')[0] if instance_type else 'unknown'


def failure_reason(error: Exception) -> str:
    """a word safe to tag and log: an exception's message can carry a name or a path."""
    if isinstance(error, ClientError):
        code = error.response.get('Error', {}).get('Code', '')
        if code in ACCESS_DENIED_CODES:
            return 'access_denied'
        if code in THROTTLE_CODES:
            return 'throttled'
        return 'aws_error'
    if isinstance(
        error, (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError)
    ):
        return 'unreachable'
    return 'error'


class HealthMetrics:
    """fresh dimensions per call, so collectors and request threads can share one instance."""

    def __init__(self, context):
        self.context = context

    def _metric(self, tags: Dict):
        from ideasdk.metrics.base_metrics import BaseMetrics

        metric = BaseMetrics(self.context, split_dimensions=False)
        if metric.metrics_provider != constants.METRICS_PROVIDER_DOGSTATSD:
            return None
        for key in sorted(tags):
            metric.with_dimension(key, tags[key])
        return metric

    def level(self, name: str, value: float, **tags):
        try:
            metric = self._metric(tags)
            if metric is not None:
                metric.gauge(MetricName=f'health.{name}', Value=value, Unit='Count')
        except Exception:  # noqa - a metric never costs its caller anything
            pass

    def add(self, name: str, value: float = 1, timestamp: Optional[int] = None, **tags):
        try:
            metric = self._metric(tags)
            if metric is None:
                return
            stamp = {} if timestamp is None else {'Timestamp': timestamp}
            metric.count(MetricName=f'health.{name}', Value=value, **stamp)
        except Exception:  # noqa - a metric never costs its caller anything
            pass

    def timing(self, name: str, seconds: float, **tags):
        try:
            metric = self._metric(tags)
            if metric is not None:
                metric.seconds(MetricName=f'health.{name}', Value=seconds)
        except Exception:  # noqa - a metric never costs its caller anything
            pass


class HealthCollector:
    name = ''
    interval_seconds = 300
    time_limit_seconds = 60
    leader_only = True

    def collect(self, metrics: HealthMetrics) -> None:
        raise NotImplementedError()


class CollectorRunner:
    def __init__(self, context, collectors: Iterable[HealthCollector]):
        self.context = context
        self.logger = context.logger('health-metrics')
        self.metrics = HealthMetrics(context)
        self.collectors = list(collectors)
        self._exit = Event()
        self._threads: List[Thread] = []
        self._running: Dict[str, Thread] = {}

    def is_enabled(self, collector: HealthCollector) -> bool:
        config = self.context.config()
        return config.get_bool(ENABLED_KEY, default=True) and config.get_bool(
            f'metrics.health.{collector.name}.enabled', default=True
        )

    def run_once(self, collector: HealthCollector) -> str:
        """returns ok, a failure reason, timeout or stuck, within the collector's time limit."""
        name = collector.name
        previous = self._running.get(name)
        if previous is not None and previous.is_alive():
            self._report(collector, 'stuck', None)
            return 'stuck'
        outcome: Dict[str, str] = {}

        def work():
            try:
                collector.collect(self.metrics)
                outcome['reason'] = 'ok'
            except Exception as e:
                outcome['reason'] = failure_reason(e)
                self.logger.warning(
                    f'{name} collector failed: {type(e).__name__} ({outcome["reason"]})'
                )

        started = time.monotonic()
        worker = Thread(target=work, name=f'health-{name}-worker', daemon=True)
        worker.start()
        worker.join(collector.time_limit_seconds)
        if worker.is_alive():
            self._running[name] = worker
            self._report(collector, 'timeout', None)
            return 'timeout'
        self._running.pop(name, None)
        reason = outcome.get('reason', 'error')
        self._report(collector, reason, time.monotonic() - started)
        return reason

    def _report(self, collector, reason: str, seconds: Optional[float]):
        self.metrics.level(
            'collector.ok',
            1 if reason == 'ok' else 0,
            collector=collector.name,
            reason=reason,
        )
        if seconds is not None:
            self.metrics.level(
                'collector.duration_seconds', seconds, collector=collector.name
            )

    def _loop(self, collector: HealthCollector):
        # replicas and restarts do not all read at the same second
        self._exit.wait(random.uniform(0, min(30, collector.interval_seconds)))
        while not self._exit.is_set():
            wait = collector.interval_seconds
            try:
                due = (
                    not collector.leader_only or self.context.is_leader()
                ) and self.is_enabled(collector)
            except Exception as e:
                due = False
                self.logger.warning(
                    f'{collector.name} collector loop: {type(e).__name__}'
                )
            if not due:
                # a new task is elected a minute or two after it starts: waiting the
                # whole interval left a six-hourly collector silent for six hours
                wait = min(wait, SKIPPED_RECHECK_SECONDS)
            else:
                try:
                    self.run_once(collector)
                except Exception as e:
                    self.logger.warning(
                        f'{collector.name} collector loop: {type(e).__name__}'
                    )
            self._exit.wait(wait)

    def start(self):
        provider = self.context.config().get_string('metrics.provider')
        if provider != constants.METRICS_PROVIDER_DOGSTATSD:
            self.logger.debug(f'health metrics need dogstatsd, not {provider}. skip.')
            return
        for collector in self.collectors:
            thread = Thread(
                target=self._loop,
                args=(collector,),
                name=f'health-{collector.name}',
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self):
        self._exit.set()
        for thread in self._threads:
            thread.join(1)


@dataclass
class ProbeResult:
    # ok: exited in time; failed: exited nonzero; timeout: killed at the limit; stuck: an earlier
    # run is still in the kernel, so this one did not start
    status: str
    seconds: Optional[float] = None
    returncode: Optional[int] = None
    output: str = ''


class IsolatedProbe:
    """
    A command that may hang on a dead mount, run so it cannot take anything with it. It is its
    own process, killed at the limit. A process in uninterruptible sleep survives the kill, so it
    is kept and looked at next time, and no second one starts until it has gone: however long
    the mount stays dead, there is at most one per probe.
    """

    KILL_WAIT_SECONDS = 1

    def __init__(self, argv: List[str], timeout_seconds: float):
        self.argv = argv
        self.timeout_seconds = timeout_seconds
        self._stuck: Optional[subprocess.Popen] = None

    def run(self) -> ProbeResult:
        if self._stuck is not None:
            if self._stuck.poll() is None:
                self._stuck.kill()
                return ProbeResult('stuck')
            self._stuck = None
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return ProbeResult('failed')
        try:
            process.wait(self.timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(self.KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                self._stuck = process
            return ProbeResult('timeout', time.monotonic() - started)
        seconds = time.monotonic() - started
        # already exited: this reads what is buffered in the pipe and cannot block on the child
        output = (
            process.stdout.read().decode('utf-8', 'replace') if process.stdout else ''
        )
        if process.stdout:
            process.stdout.close()
        status = 'ok' if process.returncode == 0 else 'failed'
        return ProbeResult(status, seconds, process.returncode, output)


def python_probe(script: str, *args: str) -> List[str]:
    return [sys.executable, '-I', '-S', '-c', script, *args]


# the home directory answering a lookup, as a user's login would. a name that does not exist
# reaches the server, through its name mapping, and returns nothing about anyone.
HOME_PROBE_SCRIPT = """
import os, sys
root = sys.argv[1]
try:
    os.stat(root)
    try:
        os.stat(os.path.join(root, '.idea-health-probe'))
    except FileNotFoundError:
        pass
except PermissionError:
    sys.exit(3)
except OSError:
    sys.exit(4)
"""
HOME_PROBE_REASONS = {3: 'denied', 4: 'error'}

# levels of one mount; exit 5 when the path is not a mount in this task.
MOUNT_PROBE_SCRIPT = """
import os, sys
path = sys.argv[1]
if not os.path.exists(path) or os.stat(path).st_dev == os.stat(os.path.dirname(path.rstrip('/')) or '/').st_dev:
    sys.exit(5)
v = os.statvfs(path)
print(v.f_blocks * v.f_frsize, v.f_bfree * v.f_frsize, v.f_files, v.f_ffree)
"""
MOUNT_NOT_HERE = 5


class StorageMountsCollector(HealthCollector):
    """capacity of every shared-storage mount this task can see, and how long the read took."""

    name = 'storage_mounts'
    interval_seconds = 300
    time_limit_seconds = 120
    PROBE_SECONDS = 15

    def __init__(self, context):
        self.context = context
        self._probes: Dict[str, IsolatedProbe] = {}

    def mounts(self) -> List[Tuple[str, str]]:
        config = self.context.config()
        shared_storage = config.get_config('shared-storage', default={}) or {}
        found = []
        for name in shared_storage:
            entry = shared_storage.get(name)
            if not isinstance(entry, dict) and not hasattr(entry, 'get'):
                continue
            mount_dir = Utils.get_value_as_string('mount_dir', entry)
            if Utils.is_not_empty(mount_dir):
                found.append((name, mount_dir))
        return found

    def collect(self, metrics: HealthMetrics) -> None:
        for name, mount_dir in self.mounts():
            probe = self._probes.get(name)
            if probe is None:
                probe = IsolatedProbe(
                    python_probe(MOUNT_PROBE_SCRIPT, mount_dir), self.PROBE_SECONDS
                )
                self._probes[name] = probe
            result = probe.run()
            if result.status == 'failed' and result.returncode == MOUNT_NOT_HERE:
                continue
            answered = result.status == 'ok' and len(result.output.split()) == 4
            metrics.level('storage.mount_ok', 1 if answered else 0, mount=name)
            if not answered:
                continue
            total, free, files, files_free = (int(v) for v in result.output.split())
            metrics.level('storage.mount_read_seconds', result.seconds or 0, mount=name)
            metrics.level('storage.mount_free_bytes', free, mount=name)
            if total > 0:
                metrics.level(
                    'storage.mount_used_percent',
                    100 * (total - free) / total,
                    mount=name,
                )
            if files > 0:
                metrics.level(
                    'storage.mount_inodes_used_percent',
                    100 * (files - files_free) / files,
                    mount=name,
                )


def pem_days_left(pem: bytes, now_epoch: Optional[float] = None) -> float:
    """days until the first certificate in a PEM expires; the private key is never read here."""
    from cryptography import x509

    certificate = x509.load_pem_x509_certificate(pem)
    not_after = certificate.not_valid_after_utc.timestamp()
    return (not_after - (now_epoch or time.time())) / SECONDS_PER_DAY
