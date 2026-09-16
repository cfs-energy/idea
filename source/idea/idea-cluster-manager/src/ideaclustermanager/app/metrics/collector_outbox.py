import hashlib
import json


class CollectorOutbox:
    def __init__(self, context, prefix, historical=False):
        self.context = context
        self.prefix = prefix
        self.historical = historical
        self.db = getattr(context.config(), 'db', None)
        self.entries = []
        self.bucket = (
            context.config().get_string('cluster.cluster_s3_bucket', required=True)
            if self.db is not None else None
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

    def save(self):
        if self.db is None:
            publisher = self.context.service_registry().get_service('metrics-service')
            for entry in self.entries:
                publisher.publish([entry])
            return
        for entry in self.entries:
            identity = [entry.get('Namespace'), entry['MetricName'], entry['Dimensions']]
            if self.historical:
                identity.append(entry['Timestamp'])
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            self.client.put_object(
                Bucket=self.bucket, Key=f'{self.object_prefix}{digest}.json',
                Body=json.dumps(entry, sort_keys=True).encode(), ContentType='application/json',
            )

    def replay(self):
        if self.db is None:
            return
        # Datagram transports cannot acknowledge ingestion, so enqueuing never retires a payload.
        # Object storage keeps historical corrections out of every application's settings cache.
        request = {'Bucket': self.bucket, 'Prefix': self.object_prefix}
        publisher = self.context.service_registry().get_service('metrics-service')
        while True:
            page = self.client.list_objects_v2(**request)
            for row in page.get('Contents', []):
                response = self.client.get_object(Bucket=self.bucket, Key=row['Key'])
                body = response['Body']
                try:
                    publisher.publish([json.loads(body.read())])
                finally:
                    body.close()
            token = page.get('NextContinuationToken')
            if not token:
                break
            request['ContinuationToken'] = token
