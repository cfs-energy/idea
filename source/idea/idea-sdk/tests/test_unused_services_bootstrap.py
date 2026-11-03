"""
Daemons the Ubuntu desktop image carries that no IDEA service uses. First bootstrap and the image
bake remove them; a running desktop gets them stopped and masked by the refresh, never a package
transaction. global-settings.host_hardening.disable_unused_services off puts them back. Every run
must be safe to repeat.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ideasdk.context import BootstrapContext
from ideasdk.utils import Jinja2Utils

IDEA_BOOTSTRAP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'idea-bootstrap')
)
TEMPLATE = '_templates/linux/unused_services.jinja2'
FLAG_KEY = 'global-settings.host_hardening.disable_unused_services'
UBUNTU = ('ubuntu2204', 'ubuntu2404', 'ubuntu2604')


def render(config, base_os: str, **variables) -> str:
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    return env.get_template(TEMPLATE).render(
        context=BootstrapContext(
            config=config,
            module_name='vdc',
            module_id='vdc',
            module_set='default',
            base_os=base_os,
            instance_type='m6a.large',
        ),
        **variables,
    )


def test_the_desktop_image_path_includes_it():
    dcv_server = Path(
        IDEA_BOOTSTRAP_DIR, '_templates/linux/dcv_server.jinja2'
    ).read_text()
    assert f"{{% include '{TEMPLATE}' %}}" in dcv_server


@pytest.mark.parametrize(
    'base_os', ('amazonlinux2023', 'rhel9', 'rocky10', 'windows2022')
)
def test_other_systems_render_nothing(context, base_os):
    assert 'cups-browsed' not in render(context.config(), base_os)


class Host:
    """the rendered block run with stub dpkg-query, apt-get and systemctl over a state dir"""

    def __init__(
        self,
        tmp_path: Path,
        shell: str,
        installed=('cups', 'cups-browsed'),
        enabled=('cups-browsed.service',),
    ):
        self.root = tmp_path / 'root'
        self.bin = tmp_path / 'bin'
        self.calls = tmp_path / 'calls'
        self.state = tmp_path / 'state'
        for path in (self.root, self.bin, self.state):
            path.mkdir()
        for package in installed:
            (self.state / f'pkg-{package}').touch()
        for unit in enabled:
            (self.state / f'enabled-{unit}').touch()
        self.calls.write_text('')
        self.shell = shell
        for name in 'cat grep rm touch basename ls wc mkdir'.split():
            (self.bin / name).symlink_to(shutil.which(name))
        log = f'echo "$(basename "$0") $*" >> {self.calls}\n'
        self.stub(
            'dpkg-query',
            f'for p; do :; done\n[ -f {self.state}/pkg-$p ] && echo "install ok installed"',
        )
        self.stub(
            'apt-get',
            log + f'[ "$1" != purge ] || for p; do rm -f {self.state}/pkg-$p; done',
        )
        state = self.state
        self.stub(
            'systemctl',
            log + 'for u; do :; done\n'
            'case "$1" in\n'
            f'  is-enabled) if [ -f {state}/masked-$u ]; then echo masked; exit 1; '
            f'elif [ -f {state}/enabled-$u ]; then echo enabled; else echo disabled; exit 1; fi ;;\n'
            f'  mask) touch {state}/masked-$u ;;\n'
            f'  unmask) rm -f {state}/masked-$u ;;\n'
            f'  disable) rm -f {state}/enabled-$u ;;\n'
            f'  enable) touch {state}/enabled-$u ;;\n'
            'esac',
        )

    def stub(self, name, body):
        (self.bin / name).write_text('#!/bin/sh\n' + body + '\n')
        (self.bin / name).chmod(0o755)

    def installed(self, package):
        return (self.state / f'pkg-{package}').exists()

    def enabled(self, unit):
        return (self.state / f'enabled-{unit}').exists()

    def masked(self, unit='cups-browsed.service'):
        return (self.state / f'masked-{unit}').exists()

    def run(self, block: str):
        before = len(self.calls.read_text().splitlines())
        result = subprocess.run(
            [self.shell],
            input='log_warning() { echo "$@"; }\n'
            + block.replace('/var/lib/', f'{self.root}/var/lib/'),
            text=True,
            capture_output=True,
            env={'PATH': str(self.bin)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        self.output = result.stdout
        return self.calls.read_text().splitlines()[before:]


SHELLS = [s for s in ('bash', 'dash') if shutil.which(s)]


@pytest.mark.parametrize('shell', SHELLS)
@pytest.mark.parametrize('base_os', UBUNTU)
def test_bootstrap_removes_cups_browsed_keeps_cups_and_a_rerun_changes_nothing(
    context, tmp_path, shell, base_os
):
    host = Host(tmp_path, shutil.which(shell))
    block = render(context.config(), base_os)
    first = host.run(block)
    assert not host.installed('cups-browsed')
    assert host.installed('cups')
    assert host.masked()
    assert first[0].startswith('apt-get purge -y')
    assert 'systemctl mask cups-browsed.service' in first
    assert [
        c for c in host.run(block) if not c.startswith('systemctl is-enabled')
    ] == []


@pytest.mark.parametrize('shell', SHELLS)
def test_a_running_desktop_is_masked_without_a_package_transaction(
    context, tmp_path, shell
):
    host = Host(tmp_path, shutil.which(shell))
    block = render(context.config(), 'ubuntu2404', bootstrap_refresh=True)
    calls = host.run(block)
    assert not [c for c in calls if c.startswith('apt-get')]
    assert 'systemctl disable --now cups-browsed.service' in calls
    assert host.masked()
    assert host.installed('cups-browsed')


@pytest.mark.parametrize('shell', SHELLS)
def test_turning_the_flag_off_unmasks_and_starts_it_again(context, tmp_path, shell):
    host = Host(tmp_path, shutil.which(shell))
    host.run(render(context.config(), 'ubuntu2404', bootstrap_refresh=True))
    calls = host.run(
        render(
            context.config(),
            'ubuntu2404',
            bootstrap_refresh=True,
            disable_unused_services=False,
        )
    )
    assert not host.masked()
    assert 'systemctl enable --now cups-browsed.service' in calls


def test_the_flag_is_read_from_the_cluster_config(context):
    config = context.config()
    config.put(FLAG_KEY, False)
    try:
        rendered = render(config, 'ubuntu2404')
    finally:
        config.put(FLAG_KEY, True)
    assert 'apt-get purge' not in rendered
    assert 'systemctl unmask cups-browsed.service' in rendered


SERVER_PACKAGES = ('cups', 'postgresql', 'sendmail-bin')
SERVER_UNITS = ('postgresql.service', 'sendmail.service')


def pg_cluster(host, databases):
    base = host.root / 'var/lib/postgresql/16/main/base'
    base.mkdir(parents=True)
    for oid in databases:
        (base / str(oid)).mkdir()


@pytest.mark.parametrize('shell', SHELLS)
@pytest.mark.parametrize('running', (False, True))
def test_postgresql_and_sendmail_are_stopped_and_their_packages_stay(
    context, tmp_path, shell, running
):
    host = Host(tmp_path, shutil.which(shell), SERVER_PACKAGES, SERVER_UNITS)
    pg_cluster(host, (1, 4, 5))
    variables = {'bootstrap_refresh': True} if running else {}
    block = render(context.config(), 'ubuntu2404', **variables)
    calls = host.run(block)
    assert 'systemctl disable --now postgresql.service' in calls
    assert 'systemctl disable --now sendmail.service' in calls
    assert not host.enabled('postgresql.service') and not host.enabled(
        'sendmail.service'
    )
    assert all(host.installed(p) for p in SERVER_PACKAGES)
    assert not [c for c in calls if c.startswith('apt-get')]
    assert [
        c for c in host.run(block) if not c.startswith('systemctl is-enabled')
    ] == []


@pytest.mark.parametrize('shell', SHELLS)
def test_a_postgresql_holding_a_users_database_keeps_running(context, tmp_path, shell):
    host = Host(tmp_path, shutil.which(shell), SERVER_PACKAGES, SERVER_UNITS)
    pg_cluster(host, (1, 4, 5, 16384))
    calls = host.run(render(context.config(), 'ubuntu2404', bootstrap_refresh=True))
    assert host.enabled('postgresql.service')
    assert 'postgresql holds databases beyond its defaults; left running' in host.output
    assert 'systemctl disable --now sendmail.service' in calls


@pytest.mark.parametrize('shell', SHELLS)
def test_turning_the_flag_off_starts_only_what_was_stopped(context, tmp_path, shell):
    host = Host(tmp_path, shutil.which(shell), SERVER_PACKAGES, ('postgresql.service',))
    pg_cluster(host, (1, 4, 5))
    host.run(render(context.config(), 'ubuntu2404', bootstrap_refresh=True))
    calls = host.run(
        render(
            context.config(),
            'ubuntu2404',
            bootstrap_refresh=True,
            disable_unused_services=False,
        )
    )
    assert host.enabled('postgresql.service')
    # sendmail was off before IDEA touched it, so it stays off
    assert not host.enabled('sendmail.service')
    assert 'systemctl enable --now sendmail.service' not in calls


def test_every_ubuntu_package_install_runs_it():
    packages = Path(
        IDEA_BOOTSTRAP_DIR, '_templates/linux/system_packages.jinja2'
    ).read_text()
    deb = packages[packages.index('apt install -yq $DEFAULT_PACKAGES_FOR_DEB') :]
    assert f"{{% include '{TEMPLATE}' %}}" in deb.split('{% endif %}')[0]


@pytest.mark.parametrize('shell', SHELLS)
@pytest.mark.parametrize('base_os', UBUNTU)
def test_new_hosts_lose_the_telnet_client_and_running_desktops_keep_it(
    context, tmp_path, shell, base_os
):
    packages = ('cups', 'telnet', 'inetutils-telnet', 'openssh-client')
    host = Host(tmp_path, shutil.which(shell), packages, ())
    calls = host.run(render(context.config(), base_os))
    assert not host.installed('telnet') and not host.installed('inetutils-telnet')
    assert host.installed('openssh-client')
    assert [c for c in calls if c.startswith('apt-get purge')] == [
        'apt-get purge -y -o DPkg::Lock::Timeout=300 telnet',
        'apt-get purge -y -o DPkg::Lock::Timeout=300 inetutils-telnet',
    ]
    (tmp_path / 'running').mkdir()
    running = Host(tmp_path / 'running', shutil.which(shell), packages, ())
    running.run(render(context.config(), base_os, bootstrap_refresh=True))
    assert running.installed('telnet')
