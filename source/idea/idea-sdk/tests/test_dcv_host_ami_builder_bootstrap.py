"""
the dcv host build bootstrap must render without any per-session state: no session id,
no broker registration, no host-ready notification, and it must both honor and write
the first-boot skip markers the session bootstrap checks.
"""

import os

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils
from ideadatamodel import SocaAnyPayload
from ideatestutils import MockConfig
from ideasdk.config.soca_config import SocaConfig

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)


def render(
    template_path: str,
    base_os: str = 'amazonlinux2023',
    instance_type: str = 'c5.large',
) -> str:
    config = SocaConfig(config=MockConfig().get_config())
    context = BootstrapContext(
        config=config,
        module_name='virtual-desktop-controller',
        module_id='vdc',
        module_set='default',
        base_os=base_os,
        instance_type=instance_type,
    )
    context.vars.ami_dir = '/apps/idea-mock/vdc/ami_builder/idea-dcv-host/1'
    context.vars.ami_name = 'idea-dcv-host-amazonlinux2023-v01011970-000000'
    context.vars.session = SocaAnyPayload(type='console')
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    return env.get_template(template_path).render(context=context)


def test_build_setup_has_no_session_state():
    rendered = render('dcv-host-ami-builder/setup.sh.jinja2')
    assert 'dcv_host_ami_builder.sh' in rendered
    assert 'IDEA_SESSION_ID' not in rendered
    assert 'IDEA_SESSION_OWNER' not in rendered
    assert 'sqs send-message' not in rendered


def test_build_writes_the_first_boot_skip_markers():
    rendered = render('dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2')
    assert 'idea_preinstalled_packages.log' in rendered
    assert 'idea_system_upgraded.log' in rendered
    # both markers are written on success so the session bootstrap skips the work
    assert 'Package installation completed' in rendered
    assert 'System upgrade completed' in rendered


def test_build_post_reboot_installs_dcv_but_never_registers():
    rendered = render('dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2')
    assert 'AmiBuilderStatus,Value=complete' in rendered
    assert 'systemctl disable dcvserver' in rendered
    assert 'systemctl disable dcv-session-manager-agent' in rendered
    # the session-only actions must not be in the built image path
    assert 'sqs send-message' not in rendered
    assert 'dcv_host_ready_message' not in rendered
    # the markers survive the image clean-up
    assert 'rm -rf /root/bootstrap/logs' in rendered
    assert 'rm -rf /root/bootstrap\n' not in rendered


def test_build_post_reboot_scrubs_the_builder_identity():
    rendered = render('dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2')
    for line in (
        'rm -rf /var/lib/amazon/ssm/*',
        ': > /etc/machine-id',
        'rm -f /var/lib/dbus/machine-id',
        'rm -rf /var/lib/cloud/instances/* /var/lib/cloud/instance /var/lib/cloud/data/*',
        'find /var/log -type f -exec truncate -s 0 {} +',
        'rm -f /etc/dcv/dcv.key /etc/dcv/dcv.pem',
    ):
        assert line in rendered, line
    # the scrub runs before the ready tag, never after it
    assert rendered.index('rm -rf /var/lib/amazon/ssm/*') < rendered.index(
        'AmiBuilderStatus,Value=complete'
    )
