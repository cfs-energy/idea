import arrow

from ideadatamodel import exceptions
from ideasdk.metrics.history_backfill import (
    CapturingPublisher,
    MetricsHistoryBackfill,
    dry_run_value,
)
from ideaclustermanager.app.metrics.cost_metrics_service import (
    CostMetricsService,
    CostMetrics,
    aggregate,
)


class CostMetricsBackfill(MetricsHistoryBackfill):
    def __init__(self, context):
        super().__init__(context, 'cost')

    def start_request(self, payload):
        days = payload.get('days', 400)
        today = arrow.utcnow().floor('day')
        maximum = (today - today.shift(months=-15)).days
        if (
            isinstance(days, bool)
            or not isinstance(days, int)
            or not 1 <= days <= maximum
        ):
            raise exceptions.invalid_params(
                f'days must be an integer from 1 to {maximum}'
            )
        if self.context.aws().aws_partition() != 'aws':
            raise exceptions.invalid_params(
                'Cost Explorer requires the commercial AWS partition'
            )
        return self.start(
            dry_run_value(payload), start=today.shift(days=-days), end=today
        )

    def collect(self, transport, dry_run, start, end):
        service = CostMetricsService(self.context)
        reader = service.reader()
        by_account = self.context.config().get_bool(
            service._config_key('by_account'), False
        )
        capture = CapturingPublisher(self.context)
        metrics = CostMetrics(capture)
        while start < end:
            stop = min(start.shift(days=30), end)
            rows = aggregate(reader.fetch_all(start, stop, by_account))
            self.progress(jobs_scanned=len(rows))
            for row in rows:
                metrics.publish(
                    row.family,
                    int(arrow.get(row.day).timestamp()),
                    row.dimensions,
                    row.amortized,
                    row.unblended,
                )
                if len(capture.entries) >= 500:
                    self.send(capture, transport, dry_run)
            self.send(capture, transport, dry_run)
            start = stop
