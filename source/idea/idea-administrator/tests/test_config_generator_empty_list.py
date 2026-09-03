"""
Test Cases for config entry flattening of empty lists

traverse_config() flattens the rendered yaml into the key/value entries that get synced
into the cluster settings table. it used to route every value through
Utils.get_any_value(), which reports an empty list as absent, so `system_7: []` was
written as None and stored as NULL.
"""

from ideaadministrator.app.config_generator import ConfigGenerator


def flatten(config: dict) -> dict:
    entries = []
    ConfigGenerator({}).traverse_config(entries, '', config)
    return {entry['key']: entry['value'] for entry in entries}


def test_empty_list_survives_flattening():
    values = flatten({'global-settings': {'package_config': {'system_7': []}}})
    assert values['global-settings.package_config.system_7'] == []


def test_null_and_blank_still_flatten_to_none():
    values = flatten({'m': {'not_set': None, 'blank': '   '}})
    assert values['m.not_set'] is None
    assert values['m.blank'] is None


def test_other_values_unchanged():
    values = flatten({'m': {'ids': ['a', 'b'], 'count': 0, 'flag': False, 'name': 'x'}})
    assert values['m.ids'] == ['a', 'b']
    assert values['m.count'] == 0
    assert values['m.flag'] is False
    assert values['m.name'] == 'x'
