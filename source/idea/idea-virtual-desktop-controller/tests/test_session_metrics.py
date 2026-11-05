from types import SimpleNamespace
from unittest.mock import Mock

from ideadatamodel import VirtualDesktopBaseOS, VirtualDesktopSessionState
from ideavirtualdesktopcontroller.app.sessions.session_metrics import (
    SessionCountPublisher,
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
