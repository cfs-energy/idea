from datetime import datetime
import math
import re
import time

import arrow
import requests

from ideasdk.metrics.datadog_format import DatadogFormat


class DatadogAPI(DatadogFormat):
    def __init__(self, api_key: str, site: str, namespace: str):
        super().__init__(namespace)
        if not api_key:
            raise ValueError('Datadog API key is required')
        if not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', site):
            raise ValueError('Datadog site must be a hostname')
        self.api_key = api_key
        self.url = f'https://api.{site}/api/v2/series'

    def series(self, entry):
        kind = entry.get('MetricType')
        if kind not in ('Counter', 'Gauge'):
            raise ValueError('Historical metrics require Counter or Gauge entries')
        timestamp = entry.get('Timestamp')
        if isinstance(timestamp, str):
            try:
                timestamp = arrow.get(timestamp).timestamp()
            except arrow.parser.ParserError:
                timestamp = arrow.get(timestamp, 'YYYY-MM-DD HH:mm:ss ZZ').timestamp()
        elif isinstance(timestamp, datetime):
            timestamp = timestamp.timestamp()
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise TypeError('Historical metrics require an epoch timestamp')
        value = float(entry['Value'])
        if not math.isfinite(value) or not math.isfinite(timestamp):
            raise ValueError('Metric values and timestamps must be finite')
        name, tags = self.name_and_tags(entry)
        series = {
            'metric': name,
            'type': 1 if kind == 'Counter' else 3,
            'points': [{'timestamp': int(timestamp), 'value': value}],
            'tags': tags,
        }
        if kind == 'Counter':
            series['interval'] = 1
        return series

    def log(self, metric_data, on_sent=None):
        # History batches repeat names across times and dimension sets.
        # Deduplicating by name here would discard separate historical events.
        sent = 0
        for offset in range(0, len(metric_data), 500):
            batch = [self.series(entry) for entry in metric_data[offset : offset + 500]]
            for attempt in range(5):
                response = requests.post(
                    self.url,
                    headers={
                        'DD-API-KEY': self.api_key,
                        'Content-Type': 'application/json',
                    },
                    json={'series': batch},
                    timeout=30,
                )
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < 4:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                if response.status_code != 202:
                    raise requests.HTTPError(
                        'Metrics batch was not accepted', response=response
                    )
                break
            sent += len(batch)
            if on_sent is not None:
                on_sent(len(batch))
        return sent

    @classmethod
    def from_context(cls, context):
        config = context.config()
        return cls(
            config.get_secret('ecs.datadog.api_key_secret_arn', required=True),
            config.get_string('ecs.datadog.site', 'datadoghq.com'),
            f'{context.cluster_name()}/{context.module_id()}',
        )
