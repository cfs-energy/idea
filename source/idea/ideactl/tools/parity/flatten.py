#!/usr/bin/env python3
"""Flatten a generated IDEA config directory to sorted flat JSON.

The layer-A oracle for the config generator port. A line-for-line copy of
`ConfigGenerator.read_config_from_files` + `traverse_config`
(the deleted Python administrator's app/config_generator.py:646-714) and
`is_null_value` (idea-sdk/src/ideasdk/config/soca_config.py:19-24), deliberately
NOT importing ideaadministrator so the oracle does not depend on the code under
test or on its venv. PyYAML only, for the same YAML 1.1 parse Python does.

    python3 flatten.py <config-dir> [-o out.json] [--key-prefix P]

<config-dir> is the directory holding idea.yml. Writes {"<key>": <value>, ...}
with sorted keys, and prints the key count.
"""

import argparse
import json
import os
import sys

import yaml


def is_empty(value):
    if value is None:
        return True
    if isinstance(value, (str, list, tuple, set, dict, bytearray, bytes)):
        return len(value.strip()) == 0 if isinstance(value, str) else len(value) == 0
    return False


def is_null_value(value):
    # an empty list is a real config value; every other empty value counts as null.
    return is_empty(value) and not isinstance(value, list)


def read_config_from_files(config_dir):
    with open(os.path.join(config_dir, 'idea.yml')) as f:
        idea_config = yaml.safe_load(f.read())
    config = {}
    for module in idea_config['modules']:
        module_id = module['id']
        module_settings = {}
        for file in module['config_files']:
            with open(os.path.join(config_dir, module_id, file)) as f:
                settings = yaml.safe_load(f.read())
            module_settings = {**module_settings, **settings}
        config[module_id] = module_settings
    return config


def traverse_config(config_entries, prefix, config, filter_key_prefix=None):
    for key in config:
        if '.' in key or ':' in key:
            raise ValueError(
                f'Config key name: {key} under: {prefix} cannot contain a dot(.), colon(:) or comma(,)'
            )

        # Null normalization precedes dictionary traversal, so `key: {}` becomes NULL.
        value = config[key]
        if is_null_value(value):
            value = None

        path_prefix = f'{prefix}.{key}' if not is_empty(prefix) else key

        if isinstance(value, dict):
            traverse_config(config_entries, path_prefix, value, filter_key_prefix)
        else:
            if not is_empty(filter_key_prefix) and not path_prefix.startswith(
                filter_key_prefix
            ):
                continue
            config_entries.append({'key': path_prefix, 'value': value})


def flatten(config_dir, key_prefix=None):
    entries = []
    traverse_config(
        entries, '', read_config_from_files(config_dir), filter_key_prefix=key_prefix
    )
    flat = {}
    for entry in entries:
        if entry['key'] in flat and flat[entry['key']] != entry['value']:
            raise ValueError(f'duplicate key with differing values: {entry["key"]}')
        flat[entry['key']] = entry['value']
    return flat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config_dir')
    parser.add_argument('-o', '--out')
    parser.add_argument('--key-prefix')
    args = parser.parse_args()

    flat = flatten(args.config_dir, args.key_prefix)
    text = json.dumps(flat, indent=2, sort_keys=True) + '\n'
    if args.out:
        with open(args.out, 'w') as f:
            f.write(text)
        print(f'{args.out}: {len(flat)} keys', file=sys.stderr)
    else:
        sys.stdout.write(text)
        print(f'{len(flat)} keys', file=sys.stderr)


if __name__ == '__main__':
    main()
