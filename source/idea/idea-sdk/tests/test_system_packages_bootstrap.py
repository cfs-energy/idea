"""
Package list reads in the bootstrap templates, against a NULL config row.

`required=True` does not guard a key whose stored value is NULL: SocaConfig.get_list()
returns None for it and only raises for a key absent from the tree. The optional package
lists read with default=[] instead, or bootstrap fails on `' '.join(None)` at boot.
"""

import os

import pytest

from ideadatamodel import errorcodes, exceptions
from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
SYSTEM_PACKAGES_TEMPLATE = '_templates/linux/system_packages.jinja2'

PKG_PREFIX = 'global-settings.package_config.linux_packages'

# the lists that are legitimately empty on some OS families, and so are the ones that
# reach a module as a NULL row
OPTIONAL_PACKAGE_KEYS = (
    'system_7',
    'system_8',
    'system_9',
    'system_10',
    'application_7',
    'application_8',
    'sssd_7',
)


def render_system_packages(config, base_os: str = 'amazonlinux2023') -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    template = env.get_template(SYSTEM_PACKAGES_TEMPLATE)
    return template.render(
        context=BootstrapContext(
            config=config,
            module_name='vdc',
            module_id='vdc',
            module_set='default',
            base_os=base_os,
            instance_type='m6a.large',
        )
    )


def test_required_true_does_not_guard_a_null_row(context):
    """
    a NULL row reads back as None even with required=True, which only raises for a key
    absent from the tree. this is why the optional lists use default=[].
    """
    config = context.config()
    key = f'{PKG_PREFIX}.sssd_7'
    config.put(key, None)

    assert config.get_list(key, required=True) is None
    assert config.get_list(key, default=[]) == []

    with pytest.raises(exceptions.SocaException) as exc_info:
        config.get_list(f'{PKG_PREFIX}.no_such_list', required=True)
    assert exc_info.value.error_code == errorcodes.CONFIG_KEY_NOT_FOUND


def test_null_optional_package_lists_still_render(context):
    config = context.config()
    for key in OPTIONAL_PACKAGE_KEYS:
        config.put(f'{PKG_PREFIX}.{key}', None)

    rendered = render_system_packages(config)

    assert 'SYSTEM_PKGS_7=()' in rendered
    assert 'APPLICATION_PKGS_7=()' in rendered
    assert 'SSSD_PKGS_7=()' in rendered


def test_populated_optional_package_lists_still_render(context):
    config = context.config()
    config.put(f'{PKG_PREFIX}.sssd_7', ['python-sssdconfig'])

    rendered = render_system_packages(config)

    assert 'SSSD_PKGS_7=(python-sssdconfig)' in rendered
