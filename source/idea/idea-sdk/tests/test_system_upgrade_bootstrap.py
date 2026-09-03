"""
The first-boot system upgrade template, both branches.

Ubuntu must upgrade noninteractively: a debconf prompt (package config change, kernel ABI
bump) must never be able to hang bootstrap in PROVISIONING. The dnf family
only upgrades when the opt-in flag is set.
"""

import os

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
SYSTEM_UPGRADE_TEMPLATE = '_templates/linux/system_upgrade.jinja2'

FLAG_KEY = 'virtual-desktop-controller.dcv_session.first_boot_dnf_update'


def render_system_upgrade(config, base_os: str) -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    template = env.get_template(SYSTEM_UPGRADE_TEMPLATE)
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


def test_ubuntu_upgrade_runs_noninteractively(context):
    rendered = render_system_upgrade(context.config(), base_os='ubuntu2204')
    assert 'DEBIAN_FRONTEND=noninteractive apt update' in rendered
    assert 'DEBIAN_FRONTEND=noninteractive apt upgrade -y' in rendered
    assert '--force-confdef' in rendered
    assert '--force-confold' in rendered


def test_non_ubuntu_renders_no_apt_block(context):
    rendered = render_system_upgrade(context.config(), base_os='amazonlinux2023')
    assert 'apt update' not in rendered
    assert 'apt upgrade' not in rendered


def test_dnf_update_rendered_when_flag_enabled(context):
    config = context.config()
    config.put(FLAG_KEY, True)
    try:
        rendered = render_system_upgrade(config, base_os='rocky9')
        assert 'dnf -y update' in rendered
    finally:
        config.put(FLAG_KEY, False)


def test_dnf_update_absent_by_default(context):
    config = context.config()
    config.put(FLAG_KEY, False)
    rendered = render_system_upgrade(config, base_os='rocky9')
    assert 'dnf -y update' not in rendered
