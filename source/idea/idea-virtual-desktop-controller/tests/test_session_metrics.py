from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from ideadatamodel import VirtualDesktopBaseOS, VirtualDesktopSessionState
from ideavirtualdesktopcontroller.app.sessions.session_metrics import (
    GatewayCertificateCollector,
    SessionCountPublisher,
    publish_first_ready,
    publish_provisioning_failure,
    stack_kind,
)

READY = VirtualDesktopSessionState.READY
STOPPED = VirtualDesktopSessionState.STOPPED
DELETED = VirtualDesktopSessionState.DELETED
RHEL9 = VirtualDesktopBaseOS.RHEL9


def session(state, project='fusion', base_os=RHEL9):
    return SimpleNamespace(
        state=state,
        base_os=base_os,
        project=SimpleNamespace(name=project) if project else None,
    )


def harness(pages, provider='dogstatsd'):
    published = []
    context = Mock()
    context.config().get_string.side_effect = lambda key, *args, **kwargs: (
        provider if key == 'metrics.provider' else 'unused'
    )
    context.service_registry().get_service.side_effect = lambda name: SimpleNamespace(
        publish=published.extend
    )
    db = Mock()
    db.pages = pages

    def list_all(request):
        index = int(request.cursor or 0)
        following = index + 1
        return SimpleNamespace(
            listing=db.pages[index],
            paginator=SimpleNamespace(
                cursor=str(following) if following < len(db.pages) else None
            ),
        )

    db.list_all_from_db.side_effect = list_all
    return SessionCountPublisher(context, db), db, published


def points(published):
    return {
        tuple(d['Value'] for d in sorted(e['Dimensions'], key=lambda d: d['Name'])): e[
            'Value'
        ]
        for e in published
        if e['MetricName'] == 'session.count'
    }


def test_counts_every_page_by_state_os_and_project_and_skips_deleted():
    publisher, _, published = harness(
        [
            [session(READY), session(READY), session(DELETED)],
            [session(READY, project=None), session(STOPPED, project='other')],
        ]
    )
    publisher.publish()
    assert all(e['MetricType'] == 'Gauge' for e in published)
    assert points(published) == {
        ('rhel9', 'fusion', 'READY'): 2,
        ('rhel9', 'unknown', 'READY'): 1,
        ('rhel9', 'other', 'STOPPED'): 1,
    }


def test_an_emptied_combination_reads_zero_once():
    publisher, db, published = harness([[session(READY)]])
    publisher.publish()
    db.pages = [[session(STOPPED)]]
    published.clear()
    publisher.publish()
    assert points(published) == {
        ('rhel9', 'fusion', 'READY'): 0,
        ('rhel9', 'fusion', 'STOPPED'): 1,
    }
    published.clear()
    publisher.publish()
    assert points(published) == {('rhel9', 'fusion', 'STOPPED'): 1}


def test_other_providers_publish_nothing_and_read_nothing():
    publisher, db, published = harness([[session(READY)]], provider='cloudwatch')
    publisher.publish()
    assert published == []
    db.list_all_from_db.assert_not_called()


def _desktop_context():
    published = []
    context = Mock()
    context.config().get_string.side_effect = lambda key, *a, **k: (
        'dogstatsd' if key == 'metrics.provider' else None
    )
    context.service_registry().get_service.side_effect = lambda name: SimpleNamespace(
        publish=published.extend
    )
    return context, published


def _desktop(age_seconds=400, base_ami='ami-1', **changes):
    values = dict(
        base_os=RHEL9,
        software_stack=SimpleNamespace(base_ami_id=base_ami),
        created_on=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        stopped_on=None,
        reboot_deadline=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _tags(entry):
    return {d['Name']: d['Value'] for d in entry['Dimensions']}


def test_first_ready_records_create_to_ready_by_os_and_kind_of_stack():
    context, published = _desktop_context()
    publish_first_ready(context, _desktop(), VirtualDesktopSessionState.INITIALIZING)
    assert published[-1]['MetricName'] == 'health.desktop.ready_seconds'
    assert 399 < published[-1]['Value'] < 410
    assert _tags(published[-1]) == {'base_os': RHEL9.value, 'stack_kind': 'built'}


def test_coming_ready_again_is_not_provisioning():
    for session, previous in (
        (_desktop(), VirtualDesktopSessionState.RESUMING),
        (_desktop(), READY),
        (_desktop(reboot_deadline='x'), VirtualDesktopSessionState.INITIALIZING),
        (_desktop(stopped_on='x'), VirtualDesktopSessionState.INITIALIZING),
        (_desktop(age_seconds=90000), VirtualDesktopSessionState.INITIALIZING),
        (_desktop(created_on=None), VirtualDesktopSessionState.CREATING),
    ):
        context, published = _desktop_context()
        publish_first_ready(context, session, previous)
        assert published == []


def test_a_failure_carries_its_reason_and_a_hand_built_stack_reads_custom():
    context, published = _desktop_context()
    publish_provisioning_failure(context, _desktop(base_ami=None), 'timeout')
    assert published[-1]['MetricName'] == 'health.desktop.provisioning_failures'
    assert _tags(published[-1]) == {
        'base_os': RHEL9.value,
        'reason': 'timeout',
        'stack_kind': 'custom',
    }
    assert stack_kind(SimpleNamespace(software_stack=None)) == 'unknown'


def test_gateway_certificate_reads_only_its_certificate_secret():
    context, _ = _desktop_context()
    context.config().get_string.side_effect = lambda key, *a, **k: (
        'arn:cert' if key.endswith('certificate_secret_arn') else None
    )
    client = context.aws().secretsmanager()
    begins = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'gateway')])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(begins)
        .not_valid_after(begins + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    client.get_secret_value.return_value = {
        'SecretString': certificate.public_bytes(serialization.Encoding.PEM).decode()
    }
    metrics = Mock()
    GatewayCertificateCollector(context).collect(metrics, begins.timestamp())
    client.get_secret_value.assert_called_once_with(SecretId='arn:cert')
    metrics.level.assert_called_once_with(
        'cert.days_to_expiry', 30, source='idea', lb='none', cert='dcv_gateway'
    )
