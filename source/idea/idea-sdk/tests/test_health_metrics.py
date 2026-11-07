"""the frame every collector runs in: it stays out of its service's way, and a hang is bounded."""

from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone
from ideadatamodel import ImageBuildRecord, ImageKind
from ideasdk.aws.image_builds import (
    ImageRowsCollector,
    classify_bake_failure,
    report_bake_failure,
)
from ideasdk.metrics.dogstatsd.dogstatsd_metrics import DogStatsdMetrics
from ideasdk.metrics.health import (
    HOME_PROBE_SCRIPT,
    MOUNT_NOT_HERE,
    MOUNT_PROBE_SCRIPT,
    CollectorRunner,
    HealthCollector,
    HealthMetrics,
    IsolatedProbe,
    StorageMountsCollector,
    bucket_of,
    failure_reason,
    pem_days_left,
    python_probe,
)

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import time


class Recorder:
    def __init__(self):
        self.levels = []
        self.counts = []

    def level(self, name, value, **tags):
        self.levels.append((name, value, tags))

    def add(self, name, value=1, timestamp=None, **tags):
        self.counts.append((name, value, timestamp, tags))


def context(provider='dogstatsd', settings=None):
    published = []
    found = Mock()
    settings = settings or {}
    found.config().get_string.side_effect = lambda key, *a, **k: (
        provider if key == 'metrics.provider' else None
    )
    found.config().get_bool.side_effect = lambda key, default=None: settings.get(
        key, default
    )
    found.service_registry().get_service.side_effect = lambda name: SimpleNamespace(
        publish=published.extend
    )
    found.published = published
    return found


def collector(work, limit=0.2, name='sample'):
    found = HealthCollector()
    found.name = name
    found.time_limit_seconds = limit
    found.collect = work
    return found


def reasons(found):
    return [
        {d['Name']: d['Value'] for d in e['Dimensions']}.get('reason')
        for e in found.published
        if e['MetricName'] == 'health.collector.ok'
    ]


def test_a_hung_check_returns_within_its_limit_and_is_not_repeated():
    release = Event()
    started = []

    def hang(metrics):
        started.append(1)
        release.wait(30)

    found = context()
    runner = CollectorRunner(found, [])
    hung = collector(hang)
    began = time.monotonic()
    assert runner.run_once(hung) == 'timeout'
    assert time.monotonic() - began < 2
    # the stuck call is still there: reported, and no second thread piles up behind it
    assert runner.run_once(hung) == 'stuck'
    assert runner.run_once(hung) == 'stuck'
    assert len(started) == 1
    assert reasons(found) == ['timeout', 'stuck', 'stuck']
    release.set()
    runner._running['sample'].join(5)
    assert runner.run_once(collector(lambda metrics: None)) == 'ok'


def test_a_collector_error_cannot_escape_and_says_only_a_word():
    def denied(metrics):
        raise ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'user/alice is not allowed'}},
            'DescribeCertificate',
        )

    found = context()
    runner = CollectorRunner(found, [])
    assert runner.run_once(collector(denied)) == 'access_denied'
    assert runner.run_once(collector(lambda m: 1 / 0)) == 'error'
    assert reasons(found) == ['access_denied', 'error']
    logged = str(found.logger().warning.call_args_list)
    assert 'alice' not in logged
    assert failure_reason(ValueError('alice')) == 'error'


def test_each_collector_has_an_off_switch_and_all_share_one():
    sample = collector(lambda m: None)
    assert CollectorRunner(context(), []).is_enabled(sample)
    assert not CollectorRunner(
        context(settings={'metrics.health.sample.enabled': False}), []
    ).is_enabled(sample)
    assert not CollectorRunner(
        context(settings={'metrics.health.enabled': False}), []
    ).is_enabled(sample)


def test_other_providers_get_nothing_they_would_be_billed_for():
    found = context(provider='cloudwatch')
    HealthMetrics(found).level('cert.days_to_expiry', 3)
    HealthMetrics(found).add('ad.errors', kind='loop')
    assert found.published == []
    runner = CollectorRunner(found, [collector(lambda m: None)])
    runner.start()
    assert runner._threads == []


def test_a_metric_failure_never_reaches_the_caller():
    found = context()
    found.service_registry().get_service.side_effect = RuntimeError('agent gone')
    HealthMetrics(found).add('ad.errors', kind='loop')
    HealthMetrics(found).level('ad.queue_depth', 1)
    HealthMetrics(found).timing('desktop.ready_seconds', 1)


def test_health_lines_carry_the_cluster_alone_and_a_count_keeps_its_bucket(tmp_path):
    found = context()
    HealthMetrics(found).add('ecs.task_stops', 3, timestamp=600, service='vdc')
    entry = found.published[-1]
    provider_context = Mock()
    provider_context.config().get_string.return_value = 'udp://127.0.0.1:8125'
    provider = DogStatsdMetrics(provider_context, 'idea-com/cluster-manager/task')
    assert provider.format_entry(entry) == (
        'idea.health.ecs.task_stops:3|c|#idea_cluster:idea-com,service:vdc|T600|card:none'
    )


def test_bucket_is_stable_for_any_read_of_the_same_stop():
    assert bucket_of(1000) == bucket_of(1199) == 900


def test_probe_of_a_hung_command_is_killed_at_its_limit():
    probe = IsolatedProbe(['sleep', '30'], 0.2)
    began = time.monotonic()
    assert probe.run().status == 'timeout'
    assert time.monotonic() - began < 2


def test_a_process_stuck_in_the_kernel_is_never_joined_by_another(monkeypatch):
    class Unkillable:
        created = 0
        returncode = None
        stdout = None

        def __init__(self, *args, **kwargs):
            Unkillable.created += 1
            self.kills = 0

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired('stat', timeout)

        def kill(self):
            self.kills += 1

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, 'Popen', Unkillable)
    monkeypatch.setattr(IsolatedProbe, 'KILL_WAIT_SECONDS', 0)
    probe = IsolatedProbe(['stat', '/data/home'], 0)
    assert probe.run().status == 'timeout'
    assert [probe.run().status for _ in range(5)] == ['stuck'] * 5
    assert Unkillable.created == 1


def test_home_probe_answers_for_a_live_directory_and_not_a_missing_one(tmp_path):
    ok = IsolatedProbe(python_probe(HOME_PROBE_SCRIPT, str(tmp_path)), 10).run()
    assert ok.status == 'ok' and ok.seconds is not None
    missing = IsolatedProbe(
        python_probe(HOME_PROBE_SCRIPT, str(tmp_path / 'gone')), 10
    ).run()
    assert (missing.status, missing.returncode) == ('failed', 4)


def test_mount_probe_skips_a_path_that_is_not_a_mount(tmp_path):
    result = IsolatedProbe(python_probe(MOUNT_PROBE_SCRIPT, str(tmp_path)), 10).run()
    assert result.returncode == MOUNT_NOT_HERE


def test_mounts_report_levels_and_a_dead_one_reports_only_that(monkeypatch):
    found = Mock()
    found.config().get_config.return_value = {
        'home': {'mount_dir': '/data/home'},
        'apps': {'mount_dir': '/apps'},
        'not-a-mount': 'x',
    }
    results = {
        '/data/home': SimpleNamespace(
            status='ok', returncode=0, seconds=0.1, output='100 25 10 5\n'
        ),
        '/apps': SimpleNamespace(
            status='timeout', returncode=None, seconds=15, output=''
        ),
    }
    monkeypatch.setattr(
        IsolatedProbe, 'run', lambda self: results[self.argv[-1]], raising=True
    )
    metrics = Recorder()
    StorageMountsCollector(found).collect(metrics)
    by_name = {(n, t['mount']): v for n, v, t in metrics.levels}
    assert by_name[('storage.mount_ok', 'home')] == 1
    assert by_name[('storage.mount_used_percent', 'home')] == 75
    assert by_name[('storage.mount_inodes_used_percent', 'home')] == 50
    assert by_name[('storage.mount_ok', 'apps')] == 0
    assert ('storage.mount_free_bytes', 'apps') not in by_name


def test_bake_failure_reasons_are_a_small_vocabulary():
    for text, expected in (
        ('bake passed its 120 minute time limit', 'time_limit'),
        ('no EC2 capacity after 4 tries: x', 'capacity'),
        ('in-bake check ssm failed on builder i-0: snap', 'check_ssm'),
        ('in-bake check results unreadable: e', 'checks_unreadable'),
        ('ready_gate: not READY in 600s', 'ready_gate'),
        ('the canary failed', 'canary'),
        ('the builder ran the 1 bootstrap, not 2', 'release_mismatch'),
        ('something new with i-0abc in it', 'other'),
        (None, 'other'),
    ):
        assert classify_bake_failure(text) == expected


def test_a_reported_bake_failure_is_also_counted_with_its_row():
    found = context()
    logger = Mock()
    record = ImageBuildRecord(
        base_os='rocky9', architecture='x86_64', release='26.11.2', status='failed'
    )
    report_bake_failure(
        logger, ImageKind.COMPUTE, record, 'the canary failed', context=found
    )
    assert logger.error.call_args.args[0].startswith('IDEA_IMAGE_BAKE_FAILED compute ')
    state = found.published[-1]
    assert state['MetricName'] == 'health.bake.row_state'
    assert {'Name': 'status', 'Value': 'failed'} in state['Dimensions']
    entry = found.published[-2]
    assert entry['MetricName'] == 'health.bake.failures'
    assert {d['Name']: d['Value'] for d in entry['Dimensions']} == {
        'arch': 'x86_64',
        'kind': 'compute',
        'os': 'rocky9',
        'reason': 'canary',
        'variant': 'cpu',
    }


def test_rows_report_state_age_and_duration_and_a_left_state_reads_zero():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    rows = [
        ImageBuildRecord(
            base_os='rocky9',
            architecture='x86_64',
            status='current',
            current_image_id='ami-1',
            promoted_on=now - timedelta(days=3),
            started_on=now - timedelta(days=3, minutes=40),
            finished_on=now - timedelta(days=3),
        )
    ]
    collected = ImageRowsCollector(ImageKind.DESKTOP, lambda: rows)
    metrics = Recorder()
    collected.collect(metrics, now)
    by_name = {n: (v, t) for n, v, t in metrics.levels}
    assert by_name['bake.image_age_days'][0] == 3
    assert by_name['bake.last_duration_seconds'][0] == 2400
    assert by_name['bake.row_state'] == (
        1,
        {
            'status': 'current',
            'kind': 'desktop',
            'os': 'rocky9',
            'arch': 'x86_64',
            'variant': 'cpu',
        },
    )
    rows[0].status = 'failed'
    metrics = Recorder()
    collected.collect(metrics, now)
    states = {t['status']: v for n, v, t in metrics.levels if n == 'bake.row_state'}
    assert states == {'failed': 1, 'current': 0}


def test_pem_days_left_reads_a_certificate(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'probe')])
    begins = datetime(2026, 1, 1, tzinfo=timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(begins)
        .not_valid_after(begins + timedelta(days=10))
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    assert pem_days_left(pem, begins.timestamp()) == 10


def test_rows_behind_this_release_are_counted_and_pins_are_left_out():
    def row(os_, status, release):
        return ImageBuildRecord(
            base_os=os_, architecture='x86_64', status=status, release=release
        )

    rows = [
        row('rocky9', 'current', '26.11.2'),
        row('rhel8', 'current', '26.11.1'),
        row('rhel9', 'failed', '26.11.2'),
        row('rocky8', 'pinned', '26.10.1'),
        row('sles', 'unsupported', None),
    ]
    metrics = Recorder()
    ImageRowsCollector(ImageKind.COMPUTE, lambda: rows, release='26.11.2').collect(
        metrics
    )
    assert [v for n, v, t in metrics.levels if n == 'bake.rows_behind_release'] == [2]
    assert {t['os']: v for n, v, t in metrics.levels if n == 'bake.row_on_release'} == {
        'rocky9': 1,
        'rhel8': 0,
        'rhel9': 0,
    }
