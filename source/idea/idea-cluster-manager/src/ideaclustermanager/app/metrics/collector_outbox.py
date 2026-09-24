import hashlib
import json

from ideasdk.metrics.datadog_api import DatadogAPI


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

    def save(self):
        if self.db is None:
            self.deliver(self.entries)
            return
        for entry in self.entries:
            identity = [
                entry.get('Namespace'),
                entry['MetricName'],
                entry['Dimensions'],
            ]
            if self.historical:
                identity.append(entry['Timestamp'])
            digest = hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode()
            ).hexdigest()
            self.client.put_object(
                Bucket=self.bucket,
                Key=f'{self.object_prefix}{digest}.json',
                Body=json.dumps(entry, sort_keys=True).encode(),
                ContentType='application/json',
            )

    def replay(self):
        if self.db is None:
            return
        # Retaining delivered points replays historical spend on every collection.
        # HTTP acceptance is required before durable points can be retired.
        request = {'Bucket': self.bucket, 'Prefix': self.object_prefix}
        while True:
            page = self.client.list_objects_v2(**request)
            for row in page.get('Contents', []):
                response = self.client.get_object(Bucket=self.bucket, Key=row['Key'])
                body = response['Body']
                try:
                    self.deliver([json.loads(body.read())])
                    self.client.delete_object(Bucket=self.bucket, Key=row['Key'])
                finally:
                    body.close()
            token = page.get('NextContinuationToken')
            if not token:
                break
            request['ContinuationToken'] = token
