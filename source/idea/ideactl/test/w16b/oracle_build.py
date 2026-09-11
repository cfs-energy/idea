#!/usr/bin/env python3
"""Generate the bootstrap archive oracle.

Run where Jinja2 and the IDEA sources are available:

    python3 oracle_build.py                     # writes ./out/<basename>/…
    IDEA_SOURCE_ROOT=/path/to/source/idea python3 oracle_build.py

Copy each `out/<basename>/` tree to the gitignored oracle directory. Use
umask 022 so recorded modes match a default checkout.
"""

import json
import os
import sys

SOURCE_ROOT = os.environ.get(
    'IDEA_SOURCE_ROOT',
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')),
)
sys.path.insert(0, os.path.join(SOURCE_ROOT, 'idea-sdk', 'src'))
sys.path.insert(0, os.path.join(SOURCE_ROOT, 'idea-data-model', 'src'))

from ideasdk.bootstrap.bootstrap_package_builder import BootstrapPackageBuilder  # noqa: E402

SOURCE_DIRECTORY = os.environ.get(
    'IDEA_BOOTSTRAP_SOURCE', os.path.join(SOURCE_ROOT, 'idea-bootstrap')
)
OUT_DIR = os.environ.get('IDEA_ORACLE_OUT', os.path.join(os.getcwd(), 'out'))

VALUES = {
    'cluster.cluster_name': 'sample-cluster',
    'cluster.cluster_s3_bucket': 'sample-bucket',
    'cluster.home_dir': '/apps/sample-cluster',
    'cluster.aws.region': 'us-east-2',
    'virtual-desktop-controller.dcv_broker.gateway_communication_port': '8445',
}


def default_argument(args, kwargs):
    """Returns the default value from a mapping argument."""
    if 'default' in kwargs:
        return kwargs['default']
    for argument in args:
        if isinstance(argument, dict) and 'default' in argument:
            return argument['default']
    return None


def coalesce(*candidates):
    """Returns the first non-None value."""
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


class Config:
    def get_string(self, key, *args, **kwargs):
        return coalesce(VALUES.get(key), default_argument(args, kwargs), 'configured')

    def get_bool(self, key, *args, **kwargs):
        return coalesce(default_argument(args, kwargs), False)

    def get_list(self, key, *args, **kwargs):
        return coalesce(default_argument(args, kwargs), [])

    def get_int(self, key, *args, **kwargs):
        return coalesce(default_argument(args, kwargs), 1)

    def get_config(self, key, *args, **kwargs):
        return coalesce(default_argument(args, kwargs), {})

    def get_cluster_internal_endpoint(self, *args, **kwargs):
        return 'https://example.invalid'


class Utils:
    """Produces compact JSON."""

    def to_json(self, value, *args, **kwargs):
        return json.dumps(value, separators=(',', ':'))

    def to_yaml(self, value, *args, **kwargs):
        return json.dumps(value, separators=(',', ':')) + '\n'


class Vars:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class Context:
    aws_region = 'us-east-2'
    base_os = 'amazonlinux2023'
    module_name = 'virtual-desktop-controller'
    module_id = 'vdc'
    module_set = 'default'
    module_version = '26.09.0'
    cluster_s3_bucket = 'sample-bucket'
    cluster_name = 'sample-cluster'
    cluster_home_dir = '/apps/sample-cluster'
    app_deploy_dir = '/opt/idea/app'
    https_proxy = ''
    no_proxy = ''

    def __init__(self, extra_vars):
        self.config = Config()
        self.utils = Utils()
        self.vars = Vars(
            dcv_connection_gateway_package_uri=(
                's3://sample-bucket/idea/releases/idea-dcv-connection-gateway-26.09.0.tar.gz'
            ),
            **extra_vars,
        )

    def get_cloudwatch_agent_config(self, *args, **kwargs):
        return None

    def get_custom_aws_tags(self, *args, **kwargs):
        return []

    def has_storage_provider(self, *args, **kwargs):
        return False

    def is_metrics_provider_prometheus(self, *args, **kwargs):
        return False


# Matches the TypeScript oracle cases.
CASES = [
    ('bootstrap-vdc-dcv-connection-gateway-deployment', ['dcv-connection-gateway'], {}),
    ('bootstrap-directoryservice-deployment', ['openldap-server'], {}),
    (
        'bootstrap-cluster-manager-deployment',
        ['cluster-manager'],
        {'app_package_uri': 's3://sample-bucket/release.tar.gz'},
    ),
    (
        'bootstrap-scheduler-deployment',
        ['scheduler'],
        {'app_package_uri': 's3://sample-bucket/release.tar.gz'},
    ),
    ('bootstrap-bastion-host-deployment', ['bastion-host'], {}),
    (
        'bootstrap-virtual-desktop-controller-deployment',
        ['virtual-desktop-controller'],
        {'controller_package_uri': 's3://sample-bucket/release.tar.gz'},
    ),
    ('bootstrap-dcv-broker-deployment', ['dcv-broker'], {}),
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for basename, components, extra_vars in CASES:
        archive = BootstrapPackageBuilder(
            bootstrap_context=Context(extra_vars),
            source_directory=SOURCE_DIRECTORY,
            target_package_basename=basename,
            components=list(components),
            tmp_dir=OUT_DIR,
            force_build=True,
            base_os='amazonlinux2023',
        ).build()
        print(f'built {archive}')


if __name__ == '__main__':
    main()
