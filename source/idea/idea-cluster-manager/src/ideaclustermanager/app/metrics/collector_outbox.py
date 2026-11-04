import hashlib
import itertools
import json
import time
import uuid

from ideasdk.metrics.datadog_api import DatadogAPI

# DatadogAPI.log posts 500 series a request, so a saved batch is one request
BATCH_SIZE = 500
BATCH_KEY = 'batch-'
_sequence = itertools.count()


class CollectorOutbox:
    def __init__(self, context, prefix, historical=False):
        self.context = context
        self.prefix = prefix
        self.historical = historical
        self.db = getattr(context.config(), 'db', None)
        self.entries = []
        self.bucket = (
            context.config().get_string('cluster.cluster_s3_bucket', required=True)
            if self.db is not None
            else None
        )
        self.client = context.aws().s3() if self.db is not None else None
        self.object_prefix = f'metrics/outbox/{prefix}/'

    def __getattr__(self, name):
        return getattr(self.context, name)

    def service_registry(self):
        return self

    def get_service(self, name):
        if name == 'metrics-service':
            return self
        return self.context.service_registry().get_service(name)

    def publish(self, metric_data):
        self.entries.extend(metric_data)

    def deliver(self, entries):
        if self.context.config().get_string('metrics.provider') == 'dogstatsd':
            DatadogAPI.from_context(self.context).log(entries)
        else:
            self.context.service_registry().get_service('metrics-service').publish(
                entries, synchronous=True
            )

    def identity(self, entry):
        identity = [entry.get('Namespace'), entry['MetricName'], entry['Dimensions']]
        if self.historical:
            identity.append(entry['Timestamp'])
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def save(self):
        """
        the run's points, one object per batch of at most BATCH_SIZE. a point saved
        again (a correction, or a gauge's newer value) supersedes the earlier one,
        within the run here and across pending batches at replay.
        """
        if self.db is None:
            self.deliver(self.entries)
            return
        latest = {}
        for entry in self.entries:
            latest.pop(self.identity(entry), None)
            latest[self.identity(entry)] = entry
        entries = list(latest.values())
        # keys sort in save order; the suffix keeps two replicas' keys apart
        stamp = f'{time.time_ns():020d}-{next(_sequence):010d}-{uuid.uuid4().hex[:8]}'
        for index, offset in enumerate(range(0, len(entries), BATCH_SIZE)):
            self.client.put_object(
                Bucket=self.bucket,
                Key=f'{self.object_prefix}{BATCH_KEY}{stamp}-{index:05d}.json',
                Body=json.dumps(
                    entries[offset : offset + BATCH_SIZE], sort_keys=True
                ).encode(),
                ContentType='application/json',
            )

    def replay(self):
        """
        deliver the pending batches oldest first, one request each, and retire a
        batch only once it was accepted; a failure keeps it and every later batch for
        the next replay. a point that a later batch saved again is left to that batch.
        objects written by earlier releases hold one point each and count as oldest.
        """
        if self.db is None:
            return
        # Retaining delivered points replays historical spend on every collection.
        # HTTP acceptance is required before durable points can be retired.
        keys = []
        request = {'Bucket': self.bucket, 'Prefix': self.object_prefix}
        while True:
            page = self.client.list_objects_v2(**request)
            keys.extend(row['Key'] for row in page.get('Contents', []))
            token = page.get('NextContinuationToken')
            if not token:
                break
            request['ContinuationToken'] = token
        keys.sort(key=lambda key: (BATCH_KEY in key[len(self.object_prefix) :], key))
        batches = []
        for key in keys:
            body = self.client.get_object(Bucket=self.bucket, Key=key)['Body']
            try:
                value = json.loads(body.read())
            finally:
                body.close()
            batches.append((key, value if isinstance(value, list) else [value]))
        last = {}
        for position, (_, entries) in enumerate(batches):
            for entry in entries:
                last[self.identity(entry)] = position
        for position, (key, entries) in enumerate(batches):
            entries = [e for e in entries if last[self.identity(e)] == position]
            if entries:
                self.deliver(entries)
            self.client.delete_object(Bucket=self.bucket, Key=key)
