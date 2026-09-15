import logging
import os
import signal
import threading

import boto3

from ideasdk.metrics.dogstatsd.dogstatsd_metrics import DogStatsdMetrics
from ideaclustermanager.app.metrics.cost_metrics_service import CostMetricsService


class EnvironmentConfig:
    def __init__(self, environ):
        self.values = {
            f'cost-metrics.metrics.cost.{name}': environ[
                f'IDEA_COST_METRICS_{name.upper()}'
            ]
            for name in (
                'enabled',
                'interval_hours',
                'lookback_days',
                'module_tag',
                'project_tag',
                'owner_tag',
                'by_account',
            )
            if f'IDEA_COST_METRICS_{name.upper()}' in environ
        }
        self.values.setdefault('cost-metrics.metrics.cost.enabled', 'true')
        self.values['metrics.provider'] = 'dogstatsd'
        self.values['metrics.dogstatsd.url'] = environ.get(
            'DD_DOGSTATSD_URL', 'udp://127.0.0.1:8125'
        )

    def get_string(self, key, default=None):
        return self.values.get(key, default)

    def get_int(self, key, default=None):
        value = self.values.get(key)
        return int(value) if value is not None else default

    def get_bool(self, key, default=None):
        value = self.values.get(key)
        if value is None:
            return default
        if value.lower() not in ('true', 'false'):
            raise ValueError(f'{key} must be true or false')
        return value.lower() == 'true'


class StandaloneAws:
    def __init__(self, region):
        self.session = boto3.Session(region_name=region)
        self.region = region

    def aws_partition(self):
        return self.session.get_partition_for_region(self.region)

    def cost_explorer(self):
        return self.session.client('ce', region_name='us-east-1')


class NoopLock:
    # The service has a single task and stops the old task before starting its replacement.
    # There is no cluster settings table in the billing account to use for coordination.
    def acquire(self, key):
        pass

    def release(self, key):
        pass


class MetricsService:
    def __init__(self, provider):
        self.provider = provider

    def publish(self, metric_data):
        self.provider.log(metric_data)


class ServiceRegistry:
    def __init__(self):
        self.services = {}

    def register(self, service):
        self.services[service.service_id()] = service

    def get_service(self, service_id):
        return self.services.get(service_id)


class StandaloneContext:
    def __init__(self, environ=None):
        environ = os.environ if environ is None else environ
        self._config = EnvironmentConfig(environ)
        self._cluster_name = environ['IDEA_CLUSTER_NAME']
        self._aws = StandaloneAws(environ['AWS_DEFAULT_REGION'])
        self._lock = NoopLock()
        self._registry = ServiceRegistry()
        provider = DogStatsdMetrics(
            self, f'{environ["IDEA_CLUSTER_NAME"]}/cost-metrics'
        )
        self._registry.services['metrics-service'] = MetricsService(provider)

    def config(self):
        return self._config

    def logger(self, name=None):
        return logging.getLogger(name or 'cost-metrics')

    def aws(self):
        return self._aws

    def cluster_name(self):
        return self._cluster_name

    def module_id(self):
        return 'cost-metrics'

    def distributed_lock(self):
        return self._lock

    def service_registry(self):
        return self._registry


def main():
    logging.basicConfig(level=logging.INFO)
    service = CostMetricsService(StandaloneContext())
    stopped = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        service.start()
        stopped.wait()
    finally:
        service.stop()
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    main()
