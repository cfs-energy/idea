from ideadatamodel import exceptions
from ideasdk.metrics.datadog_format import DatadogFormat
from ideasdk.metrics.history_backfill import (
    CapturingPublisher,
    MetricsHistoryBackfill,
    date_window,
    dry_run_value,
)
from ideascheduler.app.metrics.job_completion_metrics import JobCompletionMetrics


class JobMetricsBackfill(MetricsHistoryBackfill):
    def __init__(self, context):
        super().__init__(context, 'jobs')
        self.formatter = DatadogFormat(
            f'{context.cluster_name()}/{context.module_id()}'
        )

    def start_request(self, payload):
        start, end = date_window(payload.get('start_date'), payload.get('end_date'))
        if (
            not self.context.document_store.is_enabled()
            or not self.context.document_store.is_initialized()
        ):
            raise exceptions.invalid_params('The job document store is unavailable')
        return self.start(dry_run_value(payload), start=start, end=end)

    def collect(self, transport, dry_run, start, end):
        capture = CapturingPublisher(self.context)
        pages = self.context.document_store.finished_job_pages(start, end)
        try:
            for page in pages:
                self.progress(jobs_scanned=len(page))
                for job in page:
                    JobCompletionMetrics(capture, job).publish()
                # Equal timestamps can straddle a page boundary. Keeping the last second
                # together prevents replacement from dropping simultaneous completions.
                cutoff = int(page[-1].end_time.timestamp()) if page else None
                self.send_before(capture, transport, dry_run, cutoff)
            self.send_before(capture, transport, dry_run, None)
        finally:
            pages.close()

    def send_before(self, capture, transport, dry_run, cutoff):
        ready = CapturingPublisher(self.context)
        pending = []
        totals = {}
        for entry in capture.entries:
            if cutoff is not None and entry['Timestamp'] >= cutoff:
                pending.append(entry)
                continue
            if entry['MetricType'] != 'Counter':
                ready.entries.append(entry)
                continue
            name, tags = self.formatter.name_and_tags(entry)
            identity = (name, entry['Timestamp'], tuple(sorted(tags)))
            if identity in totals:
                totals[identity]['Value'] += entry['Value']
            else:
                totals[identity] = dict(entry)
                ready.entries.append(totals[identity])
        self.send(ready, transport, dry_run)
        capture.entries = pending
