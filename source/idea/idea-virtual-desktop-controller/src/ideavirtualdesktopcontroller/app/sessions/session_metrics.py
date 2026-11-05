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

from collections import Counter
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
