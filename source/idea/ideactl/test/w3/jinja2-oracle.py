"""
Renders the policy templates with the real Jinja2 so the port can be diffed against it.

`custom-kms-key.yml` and the `use_stable_server_name` block in `scheduler.yml` are rendered by no
deployed template and no captured synth output on this machine, so there is no recorded oracle for
them. This produces a live one: real Jinja2 over the same template files, the real `ArnBuilder`
loaded from the sdk source by path, and `Utils.to_yaml` / `Utils.from_yaml` as the sdk defines them
(`yaml.dump(json_round_trip(payload), sort_keys=False, width=140)` and `yaml.safe_load`). Only the
config object is a stand-in, transcribed from `SocaConfig`/`ClusterConfig`; it decodes the settings
table dump independently of the port so a decoding bug shows up as a diff instead of cancelling out.

stdin:  {"scan", "modules", "overrides", "policies_dir", "arn_builder", "cluster_name",
         "module_id", "module_set", "vars", "templates"}
stdout: {"<template>": {"text": ..., "parsed": ...}} or {"<template>": {"error": "<type>: <msg>"}}
Needs Jinja2 and PyYAML on the path; exits 2 with a message when Jinja2 is missing.
"""

import importlib.util
import json
import sys
import types

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError as exc:  # the caller turns this into a required-service failure
    print(f'jinja2 not importable: {exc}', file=sys.stderr)
    raise SystemExit(2)

import yaml

DEFAULT_MODULE_SET = 'default'


# --- settings table ---------------------------------------------------------------------------


def decode(attribute):
    """One DynamoDB attribute value to a plain Python value."""
    (kind, value), = attribute.items()
    if kind == 'S':
        return value
    if kind == 'N':
        return int(value) if '.' not in value else float(value)
    if kind == 'BOOL':
        return value
    if kind == 'NULL':
        return None
    if kind == 'L':
        return [decode(item) for item in value]
    if kind == 'M':
        return {key: decode(item) for key, item in value.items()}
    if kind in ('SS', 'NS'):
        return list(value)
    raise ValueError(f'unhandled attribute type: {kind}')


def put_key(tree, key, value):
    node = tree
    parts = key.split('.')
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def build_tree(scan_file, overrides):
    items = json.load(open(scan_file))['Items']
    entries = {item['key']['S']: decode(item['value']) for item in items if 'value' in item}
    entries.update(overrides)
    tree = {}
    for key in sorted(entries):  # the db layer sorts by key before the tree is built
        put_key(tree, key, entries[key])
    return tree


# --- ideadatamodel/ideasdk stand-ins ------------------------------------------------------------


def is_empty(value):
    """`Utils.is_empty` (model_utils.py:37)."""
    if value is None:
        return True
    if isinstance(value, str):
        return len(value.strip()) == 0
    if isinstance(value, (list, tuple, set, dict, bytearray, bytes)):
        return len(value) == 0
    return False


def is_null_value(value):
    """`soca_config.is_null_value`: an empty list is a real value, every other empty one is null."""
    return is_empty(value) and not isinstance(value, list)


class ConfigStub:
    """`ClusterConfig` over `SocaConfig`, without pyhocon, boto3 or the settings-table client."""

    def __init__(self, tree, module_set=DEFAULT_MODULE_SET):
        self.tree = tree
        self.module_set = module_set
        self.module_info = None  # render_policy is called on a config with no current module

    # pyhocon layer, on already-rewritten keys
    def _raw(self, key, required=False):
        node = self.tree
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                if required:
                    raise KeyError(f"'{part}', key: {key}")
                return None
            node = node[part]
        return node

    def _raw_string(self, key, required=False):
        value = self._raw(key, required)
        if value is None:
            return None
        text = str(value).lower() if isinstance(value, bool) else str(value)
        return None if is_empty(text) else text

    # ClusterConfig.get_real_key (cluster_config.py:95-117)
    def get_real_key(self, key, module_id=None):
        module_name = key.split('.')[0]
        if module_name == 'global-settings':
            return key
        if is_empty(module_id):
            if self.module_info is not None and self.module_info['name'] == module_name:
                module_id = self.module_info['module_id']
            else:
                module_id = self._raw_string(
                    f'global-settings.module_sets.{self.module_set}.{module_name}.module_id'
                )
        if is_empty(module_id):
            module_id = module_name
        return f'{module_id}.{".".join(key.split(".")[1:])}'

    # SocaConfig getters, on module-name keys
    def get(self, key, default=None, required=False, module_id=None):
        value = self._raw(self.get_real_key(key, module_id), required)
        return default if is_null_value(value) else value

    def get_string(self, key, default=None, required=False, module_id=None):
        value = self._raw_string(self.get_real_key(key, module_id), required)
        return default if is_empty(value) else value

    def get_bool(self, key, default=None, required=False, module_id=None):
        value = self.get(key, None, required, module_id)
        if is_empty(value):
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ('true', 'yes', 'on')
        return bool(value)

    def get_int(self, key, default=None, required=False, module_id=None):
        value = self.get(key, None, required, module_id)
        return default if is_empty(value) else int(value)

    def get_float(self, key, default=None, required=False, module_id=None):
        value = self.get(key, None, required, module_id)
        return default if is_empty(value) else float(value)

    def get_list(self, key, default=None, required=False, module_id=None):
        value = self.get(key, None, required, module_id)
        return default if is_null_value(value) else value

    def get_config(self, key, default=None, required=False, module_id=None):
        value = self.get(key, None, required, module_id)
        return default if is_null_value(value) else value

    def get_module_id(self, module_name):
        return self.get_string(
            f'global-settings.module_sets.{self.module_set}.{module_name}.module_id',
            required=True,
        )

    def is_module_enabled(self, module_name):
        return not is_empty(
            self.get_string(f'global-settings.module_sets.{self.module_set}.{module_name}.module_id')
        )


class Utils:
    """The two `ideasdk.utils.Utils` members the policy templates reach (utils.py:232-242)."""

    @staticmethod
    def to_yaml(payload, sort_keys=False, width=140):
        return yaml.dump(json.loads(json.dumps(payload)), sort_keys=sort_keys, width=width)

    @staticmethod
    def from_yaml(data):
        return yaml.safe_load(data)


class Vars:
    """`SocaAnyPayload`: a missing attribute is Undefined in the template, not an error."""

    def __init__(self, values):
        for name, value in values.items():
            setattr(self, name, value)


def load_arn_builder(path):
    """The real `arn_builder.py`, with its two imports stubbed so no sdk install is needed."""
    cluster_config = types.ModuleType('ideasdk.config.cluster_config')
    cluster_config.ClusterConfig = ConfigStub
    constants = types.ModuleType('ideadatamodel.constants')
    constants.MODULE_CLUSTER_MANAGER = 'cluster-manager'
    constants.MODULE_DIRECTORYSERVICE = 'directoryservice'
    datamodel = types.ModuleType('ideadatamodel')
    datamodel.constants = constants
    sys.modules.setdefault('ideasdk', types.ModuleType('ideasdk'))
    sys.modules.setdefault('ideasdk.config', types.ModuleType('ideasdk.config'))
    sys.modules['ideasdk.config.cluster_config'] = cluster_config
    sys.modules['ideadatamodel'] = datamodel
    sys.modules['ideadatamodel.constants'] = constants
    spec = importlib.util.spec_from_file_location('arn_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ArnBuilder


def main():
    job = json.load(sys.stdin)
    config = ConfigStub(
        build_tree(job['scan'], job.get('overrides', {})),
        job.get('module_set', DEFAULT_MODULE_SET),
    )
    arn_builder = load_arn_builder(job['arn_builder'])
    env = Environment(loader=FileSystemLoader(searchpath=job['policies_dir'], followlinks=False), autoescape=False)
    context = {
        'cluster_name': config.get_string('cluster.cluster_name'),
        'module_id': job.get('module_id'),
        'aws_region': config.get_string('cluster.aws.region', required=True),
        'aws_dns_suffix': config.get_string('cluster.aws.dns_suffix', required=True),
        'aws_partition': config.get_string('cluster.aws.partition', required=True),
        'aws_account_id': config.get_string('cluster.aws.account_id', required=True),
        'config': config,
        'arns': arn_builder(config=config),
        'vars': Vars(job.get('vars', {})),
        'utils': Utils,
    }
    out = {}
    for name in job['templates']:
        try:
            text = env.get_template(name).render(context=context)
            out[name] = {'text': text, 'parsed': Utils.from_yaml(text)}
        except Exception as exc:  # noqa: BLE001 - the port has to fail on the same templates
            out[name] = {'error': f'{type(exc).__name__}: {exc}'}
    json.dump(out, sys.stdout)


if __name__ == '__main__':
    main()
