"""
How many desktops are in each state, published as session.count by state, base_os and project.
A level, read from the sessions table on the leader. The tag sets are priced per metric on
CloudWatch, so like job.detail.cost this is DogStatsD only.
"""

from ideadatamodel import (
    ListSessionsRequest,
    SocaPaginator,
    VirtualDesktopSessionState,
)
from ideasdk.metrics import BaseMetrics
from ideasdk.metrics.health import HealthCollector, HealthMetrics, pem_days_left

from collections import Counter
from datetime import datetime, timezone
from typing import Set, Tuple

Key = Tuple[str, str, str]


class SessionMetrics(BaseMetrics):
    def __init__(self, context):
        super().__init__(context, split_dimensions=False)

    def publish_count(self, key: Key, count: int):
        self.push_dimensions()
        try:
            for name, value in zip(('state', 'base_os', 'project'), key):
                self.with_dimension(name, value)
            self.gauge(MetricName='session.count', Value=count, Unit='Count')
        finally:
            self.pop_dimensions()


class SessionCountPublisher:
    def __init__(self, context, db):
        self.context = context
        self.db = db
        self.metrics = SessionMetrics(context)
        self.previous: Set[Key] = set()

    def counts(self) -> Counter:
        counts: Counter = Counter()
        cursor = None
        while True:
            page = self.db.list_all_from_db(
                ListSessionsRequest(paginator=SocaPaginator(cursor=cursor))
            )
            for session in page.listing:
                if session.state == VirtualDesktopSessionState.DELETED:
                    continue
                project = session.project.name if session.project else None
                counts[
                    (
                        session.state.value if session.state else 'unknown',
                        session.base_os.value if session.base_os else 'unknown',
                        project or 'unknown',
                    )
                ] += 1
            cursor = page.paginator.cursor
            if cursor is None:
                return counts

    def publish(self):
        if self.metrics.metrics_provider != 'dogstatsd':
            return
        counts = self.counts()
        # A combination that emptied reads zero once, rather than stopping at its last count.
        for key in sorted(self.previous | set(counts)):
            self.metrics.publish_count(key, counts[key])
        self.previous = set(counts)


def stack_kind(session) -> str:
    """built: a base stack the image pipeline feeds. custom: an image someone made by hand."""
    stack = getattr(session, 'software_stack', None)
    if stack is None:
        return 'unknown'
    return 'built' if stack.base_ami_id else 'custom'


def _desktop_tags(session) -> dict:
    return dict(
        base_os=session.base_os.value if session.base_os else 'unknown',
        stack_kind=stack_kind(session),
    )


def publish_provisioning_failure(context, session, reason: str):
    HealthMetrics(context).add(
        'desktop.provisioning_failures', reason=reason, **_desktop_tags(session)
    )


# a desktop is far past provisioning by here; what comes READY this old is a restart.
FIRST_READY_MAX_AGE_SECONDS = 86400


def publish_first_ready(context, session, previous_state):
    """create to READY. resuming, rebooting and recovering come READY again and do not count."""
    if (
        previous_state
        not in (
            VirtualDesktopSessionState.CREATING,
            VirtualDesktopSessionState.INITIALIZING,
        )
        or session.reboot_deadline
        or session.stopped_on is not None
        or session.created_on is None
    ):
        return
    seconds = (datetime.now(timezone.utc) - session.created_on).total_seconds()
    if 0 <= seconds <= FIRST_READY_MAX_AGE_SECONDS:
        HealthMetrics(context).timing(
            'desktop.ready_seconds', seconds, **_desktop_tags(session)
        )


class GatewayCertificateCollector(HealthCollector):
    """days left on the certificate the gateways serve. Only that secret: not the private key."""

    name = 'gateway_certificate'
    interval_seconds = 3600
    time_limit_seconds = 60

    def __init__(self, context):
        self.context = context

    def collect(self, metrics, now=None) -> None:
        arn = self.context.config().get_string(
            'virtual-desktop-controller.dcv_connection_gateway.certificate.certificate_secret_arn'
        )
        if not arn:
            return
        value = (
            self.context.aws()
            .secretsmanager()
            .get_secret_value(SecretId=arn)
            .get('SecretString')
        )
        metrics.level(
            'cert.days_to_expiry',
            pem_days_left(value.encode(), now),
            source='idea',
            lb='none',
            cert='dcv_gateway',
        )
