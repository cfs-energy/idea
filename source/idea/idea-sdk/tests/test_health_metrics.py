"""the frame every collector runs in: it stays out of its service's way, and a hang is bounded."""

from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone
from ideadatamodel import ImageBuildRecord, ImageKind
from ideasdk.aws.image_builds import (
    ImageRowsCollector,
    bake_time_limit_reason,
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


class Clock:
    """stands in for the runner's exit event and time.monotonic: a wait moves the clock on, and
    the loop stops once the clock reaches `until` seconds after its start."""

    def __init__(self, until):
        self.now = 1000.0
        self.until = self.now + until
        self.seconds = []

    def monotonic(self):
        return self.now

    def is_set(self):
        return self.now >= self.until

    def wait(self, seconds):
        self.seconds.append(seconds)
        self.now += seconds


def run_loop(monkeypatch, sample, found, clock, runner=None):
    from ideasdk.metrics import health

    monkeypatch.setattr(health.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(health.random, 'uniform', lambda low, high: 0)
    runner = runner or CollectorRunner(found, [sample])
    runner._exit = clock
    runner._loop(sample)


def test_a_collector_that_was_not_yet_leader_runs_once_elected_not_an_interval_later(
    monkeypatch,
):
    clock = Clock(3600)
    starts = []
    sample = collector(lambda m: starts.append(clock.now))
    sample.interval_seconds = 6 * 3600
    found = context()
    # elected a minute or two after start, as a new task is
    found.is_leader.side_effect = lambda: clock.now >= 1090
    run_loop(monkeypatch, sample, found, clock)
    assert starts == [1090.0]


def test_a_failed_leader_or_switch_read_looks_again_soon_not_an_interval_later(
    monkeypatch,
):
    clock = Clock(3600)
    starts = []
    sample = collector(lambda m: starts.append(clock.now))
    sample.interval_seconds = 6 * 3600
    found = context()
    found.is_leader.side_effect = [RuntimeError('lock table')] + [True] * 200
    run_loop(monkeypatch, sample, found, clock)
    assert starts == [1030.0]
    assert 'RuntimeError' in str(found.logger().warning.call_args_list)


def test_a_collector_that_ran_and_then_failed_to_report_waits_its_interval(monkeypatch):
    clock = Clock(6 * 3600 - 60)
    starts = []
    sample = collector(lambda m: starts.append(clock.now))
    sample.interval_seconds = 6 * 3600
    found = context()
    runner = CollectorRunner(found, [sample])
    runner._report = Mock(side_effect=RuntimeError('agent gone'))
    run_loop(monkeypatch, sample, found, clock, runner)
    assert starts == [1000.0]


def test_a_read_that_takes_time_does_not_push_the_next_one_back(monkeypatch):
    # each read takes 0.2 s: timed from the end of the last read, a 60 s collector drifts and
    # skips a one-minute bucket every few hours
    clock = Clock(600)
    starts = []

    def work(metrics):
        starts.append(clock.now)
        clock.now += 0.2

    sample = collector(work, limit=5)
    sample.interval_seconds = 60
    found = context()
    run_loop(monkeypatch, sample, found, clock)
    assert [round(s - 1000, 6) for s in starts] == [60.0 * i for i in range(10)]


def test_a_late_read_starts_a_new_cadence_instead_of_reading_twice(monkeypatch):
    clock = Clock(4 * 3600)
    starts = []
    sample = collector(lambda m: starts.append(clock.now))
    sample.interval_seconds = 3600
    found = context()
    # leader for the first read, then not for nearly three hours, then leader again
    found.is_leader.side_effect = lambda: not (1100 < clock.now < 1000 + 3 * 3600)
    run_loop(monkeypatch, sample, found, clock)
    # the first read, then the read once leader again; the next is an interval after that
    assert starts == [1000.0, 1000.0 + 3 * 3600]


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


def test_health_lines_carry_cluster_and_module_and_a_count_keeps_its_bucket(tmp_path):
    found = context()
    HealthMetrics(found).add('ecs.task_stops', 3, timestamp=600, ecs_service_name='vdc')
    HealthMetrics(found).level('collector.ok', 1, collector='ecs')
    stamped, level = found.published[-2], found.published[-1]
    provider_context = Mock()
    provider_context.config().get_string.return_value = 'udp://127.0.0.1:8125'
    provider = DogStatsdMetrics(provider_context, 'idea-com/cluster-manager/task')
    # a bucket total carries no host, so the next leader's read rewrites the same point
    assert provider.format_entry(stamped) == (
        'idea.health.ecs.task_stops:3|c|#idea_cluster:idea-com,'
        'idea_module:cluster-manager,ecs_service_name:vdc,host:|T600|card:none'
    )
    # a level is per sender, and keeps the agent's host
    assert provider.format_entry(level) == (
        'idea.health.collector.ok:1|g|#idea_cluster:idea-com,'
        'idea_module:cluster-manager,collector:ecs|card:none'
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
        ('no EC2 capacity after 4 tries: x', 'capacity'),
        ('in-bake check ssm failed on builder i-0: snap', 'check_ssm'),
        ('in-bake check results unreadable: e', 'checks_unreadable'),
        ('ready_gate: not READY in 600s', 'ready_gate'),
        ('the canary failed', 'canary'),
        # the compute test launch's own sentences, none of which says canary
        ('The validation job timed out before completion.', 'canary'),
        ('PBS did not accept the validation job.', 'canary'),
        ('The validation node must launch on the exact candidate AMI.', 'canary'),
        ('The candidate node must register with PBS and run the validation job.', 'canary'),
        ('The data filesystem probe did not report a result.', 'canary'),
        ('the builder ran the 1 bootstrap, not 2', 'release_mismatch'),
        ('builder i-0 did not report ready within 60 minutes', 'step_timeout'),
        ('image ami-0 was not available within 60 minutes', 'step_timeout'),
        ('builder i-0 did not stop within 10 minutes', 'step_timeout'),
        ('builder i-0 did not stop after sysprep within 30 minutes', 'step_timeout'),
        ('Reading compute in-bake checks timed out.', 'step_timeout'),
        ('something new with i-0abc in it', 'other'),
        (None, 'other'),
    ):
        assert classify_bake_failure(text) == expected


def test_a_bake_stopped_by_its_time_limit_is_counted_as_one():
    # the text the limit writes, not a made up one; a waiting reason must not outrank it
    for error in (None, 'no EC2 capacity after 4 tries: x', 'the canary failed'):
        record = ImageBuildRecord(
            base_os='rocky8', architecture='arm64', status='building', error=error
        )
        reason = bake_time_limit_reason(record, timedelta(minutes=3))
        assert classify_bake_failure(reason) == 'time_limit'


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


def test_a_failure_whose_text_names_no_step_is_counted_under_the_step_that_failed():
    record = ImageBuildRecord(
        base_os='rocky9', architecture='x86_64', release='26.11.2', status='failed'
    )

    def reason(text, step):
        found = context()
        report_bake_failure(
            Mock(), ImageKind.COMPUTE, record, text, context=found, step=step
        )
        return {d['Name']: d['Value'] for d in found.published[-2]['Dimensions']}[
            'reason'
        ]

    # what the node's own probe wrote, which no pattern can list
    assert reason('/data is mounted read-only', 'canary') == 'canary'
    assert reason('/data is mounted read-only', None) == 'other'
    # a text that names its cause keeps it
    limit = bake_time_limit_reason(record, timedelta(minutes=3))
    assert reason(limit, 'canary') == 'time_limit'


def test_a_row_retried_between_two_runs_does_not_stay_failed():
    """
    the failure's 1 is sent by whoever failed the row. refreshed a minute later, the row was
    never seen failed by this collector, which closes only states it saw, so the series
    ended on 1.
    """
    rows = [ImageBuildRecord(base_os='rocky9', architecture='x86_64', status='current')]
    collected = ImageRowsCollector(ImageKind.COMPUTE, lambda: rows)
    collected.collect(Recorder())
    # failed and refreshed between the two runs
    rows[0].status = 'building'
    metrics = Recorder()
    collected.collect(metrics)
    states = {t['status']: v for n, v, t in metrics.levels if n == 'bake.row_state'}
    assert states == {'failed': 0, 'building': 1, 'current': 0}
    rows[0].status = 'failed'
    metrics = Recorder()
    collected.collect(metrics)
    states = {t['status']: v for n, v, t in metrics.levels if n == 'bake.row_state'}
    assert states == {'failed': 1, 'building': 0}


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


def test_an_hourly_collector_reads_soon_after_its_task_becomes_leader(monkeypatch):
    # leader election finishes minutes after start: the first read must not wait an interval
    from ideasdk.metrics import health

    monkeypatch.setattr(health, 'LOOK_EVERY_SECONDS', 0.05, raising=False)
    monkeypatch.setattr(health.random, 'uniform', lambda low, high: 0)
    found = context()
    leader = []
    found.is_leader.side_effect = lambda: bool(leader)
    ran = Event()
    hourly = collector(lambda metrics: ran.set())
    hourly.interval_seconds = 3600
    runner = CollectorRunner(found, [hourly])
    runner.start()
    try:
        assert not ran.wait(0.2)
        leader.append(True)
        assert ran.wait(2)
    finally:
        runner.stop()


def test_a_due_collector_still_waits_its_interval_between_reads(monkeypatch):
    from ideasdk.metrics import health

    monkeypatch.setattr(health, 'LOOK_EVERY_SECONDS', 0.02, raising=False)
    monkeypatch.setattr(health.random, 'uniform', lambda low, high: 0)
    found = context()
    found.is_leader.return_value = True
    runs = []
    hourly = collector(lambda metrics: runs.append(1))
    hourly.interval_seconds = 3600
    runner = CollectorRunner(found, [hourly])
    runner.start()
    try:
        time.sleep(0.3)
        assert runs == [1]
    finally:
        runner.stop()
