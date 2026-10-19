"""
RHEL 10, Rocky Linux 10 and Ubuntu 26.04 desktops. DCV 2026.0 runs virtual sessions there on
GDM's Wayland GNOME: no Xdcv, no DCV GL, no X server and no console sessions, so the desktop
bootstrap must leave Wayland on and install only the packages the Wayland tarballs ship.
"""

import configparser
import subprocess
from pathlib import Path

import pytest
from test_dcv_host_ami_builder_bootstrap import render, run_set_kernel

WAYLAND = ['rhel10', 'rocky10', 'ubuntu2604']
WALLPAPERS = {
    'rhel10': ('rhel10-brandrects-l.jpg', 'rhel10-brandrects-d.jpg'),
    'rocky10': (
        'rocky-default-10-gemstone-skies-day.png',
        'rocky-default-10-gemstone-skies-night.png',
    ),
    'ubuntu2604': ('warty-final-ubuntu.png', 'ubuntu-wallpaper-d.png'),
}

X11 = [
    'amazonlinux2023',
    'rhel8',
    'rhel9',
    'rocky8',
    'rocky9',
    'ubuntu2204',
    'ubuntu2404',
]


@pytest.mark.parametrize('base_os', WAYLAND + X11)
def test_only_el10_disables_gdm_local_displays(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert ('# Begin: DCV headless GDM' in text) == (base_os in WAYLAND[:2])
    if base_os in WAYLAND[:2]:
        assert text.index('Found window manager pre-installed') < text.index(
            '# Begin: DCV headless GDM'
        )
        assert 'WaylandEnable=false' not in text
        assert 'RemoteLoginEnable=false' not in text
        assert 'ShowLocalGreeter' not in text
        assert '[xdmcp]' not in text


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10'])
def test_headless_gdm_preserves_config_and_reapplies_seat_rules(tmp_path, base_os):
    conf = tmp_path / 'custom.conf'
    original = '[daemon]\nWaylandEnable=true\n[debug]\nEnable=true\n'
    conf.write_text(original)
    rules = tmp_path / 'rules.d'
    script = render('_templates/linux/dcv_gdm_headless.jinja2', base_os).replace(
        '/etc/udev/rules.d', str(rules)
    )
    for _ in range(2):
        result = subprocess.run(
            [
                'bash',
                '-c',
                f'GDM_CONFIG_RHEL_PATH={conf}\n'
                'udevadm() { echo "udevadm $*"; }\n'
                'systemctl() { echo "systemctl $*"; }\n' + script,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert conf.read_text() == original
        assert result.stdout.splitlines() == [
            'udevadm control --reload-rules',
            (
                'udevadm trigger --action=change --subsystem-match=pci '
                '--attr-match=vendor=0x1d0f --attr-match=device=0x1111'
            ),
            (
                'udevadm trigger --action=change --subsystem-match=drm '
                '--subsystem-match=graphics'
            ),
            'udevadm settle',
            'systemctl restart systemd-logind',
            'systemctl restart gdm',
        ]
        entries = (rules / '99-idea-dcv-headless.rules').read_text().splitlines()
        assert len(entries) == 2
        for rule, attribute in zip(entries, ['ATTR', 'ATTRS']):
            assert f'{attribute}{{vendor}}=="0x1d0f"' in rule
            assert f'{attribute}{{device}}=="0x1111"' in rule
            assert 'TAG-="seat", TAG-="master-of-seat"' in rule
        assert entries[0].startswith('SUBSYSTEM=="pci",')
        assert entries[1].startswith('SUBSYSTEM=="drm|graphics",')


@pytest.mark.parametrize('failed_command', ['control', 'trigger', 'settle'])
def test_headless_gdm_does_not_restart_after_udev_failure(tmp_path, failed_command):
    script = render('_templates/linux/dcv_gdm_headless.jinja2', 'rocky10').replace(
        '/etc/udev/rules.d', str(tmp_path / 'rules.d')
    )
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'udevadm() {{ [[ "$1" != "{failed_command}" ]]; }}\n'
            'systemctl() { echo "restart gdm"; }\n' + script,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert 'restart gdm' not in result.stdout


@pytest.mark.parametrize('base_os', WAYLAND)
@pytest.mark.parametrize('key', ['color-scheme', 'gtk-theme'])
def test_desktop_dark_defaults(base_os, key):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    value = (
        'prefer-dark'
        if key == 'color-scheme'
        else 'Yaru-dark'
        if base_os == 'ubuntu2604'
        else 'Adwaita-dark'
    )
    assert f"{key}='{value}'" in text


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10'])
def test_desktop_el10_legacy_dark_theme(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert '/usr/local/share/themes/Adwaita-dark/gtk-3.0' in text
    assert 'resource:///org/gtk/libgtk/theme/Adwaita/gtk-dark.css' in text


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10'])
@pytest.mark.parametrize(
    'key', ['allow-updates', 'download-updates', 'download-updates-notify']
)
def test_desktop_software_update_defaults(base_os, key):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert f'{key}=false' in text


@pytest.mark.parametrize(
    'setting',
    [
        'no-show-notifications=true',
        'hide-reboot-notification=true',
        'show-updates-status-icon=false',
        'update-notifier.desktop',
        'update-notifier-release.path',
        'update-notifier-release.service',
        'update-notifier-livepatch.path',
        'update-notifier-livepatch.service',
    ],
)
def test_desktop_ubuntu_update_defaults(setting):
    text = render('_templates/linux/dcv_server.jinja2', 'ubuntu2604')
    assert setting in text


@pytest.mark.parametrize('base_os', WAYLAND)
@pytest.mark.parametrize(
    'entrypoint',
    [
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
    ],
)
def test_desktop_polish_runs_for_bakes_and_preinstalled_desktops(base_os, entrypoint):
    from test_image_bake_bootstrap import render as render_host

    text = render_host(entrypoint, base_os)
    assert text.index('# Begin: GNOME Desktop Polish') > text.index(
        'Found window manager pre-installed'
    )
    assert 'gnome-initial-setup-first-login.service' in text
    assert 'gnome-initial-setup-upgrade-login.service' in text


@pytest.mark.parametrize('base_os', X11)
def test_desktop_polish_does_not_change_older_desktops(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert '# Begin: GNOME Desktop Polish' not in text


@pytest.mark.parametrize('base_os', WAYLAND)
@pytest.mark.parametrize('installed', [True, False])
@pytest.mark.parametrize('existing_profile', [True, False])
def test_desktop_polish_is_targeted_and_idempotent(
    tmp_path, base_os, installed, existing_profile
):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert '# Begin: GNOME Desktop Polish' in text
    block = text.split('# Begin: GNOME Desktop Polish')[1].split(
        '# End: GNOME Desktop Polish'
    )[0]
    etc = tmp_path / 'etc'
    units = tmp_path / 'units'
    units.mkdir()
    autostart = etc / 'xdg/autostart'
    autostart.mkdir(parents=True)
    welcome = autostart / 'gnome-initial-setup-first-login.desktop'
    pro = autostart / 'ubuntu-advantage-notification.desktop'
    updater = autostart / 'update-notifier.desktop'
    desktop = '[Desktop Entry]\nType=Application\nExec=original\nHidden=false\n'
    updater.write_text(desktop)
    unrelated = autostart / 'unrelated.desktop'
    unrelated.write_text(desktop)
    services = ['gnome-initial-setup-first-login.service']
    if base_os == 'ubuntu2604':
        services.extend(
            [
                'gnome-initial-setup-upgrade-login.service',
                'update-notifier-release.path',
                'update-notifier-release.service',
                'update-notifier-livepatch.path',
                'update-notifier-livepatch.service',
            ]
        )
    if installed:
        welcome.write_text(desktop)
        if base_os == 'ubuntu2604':
            pro.write_text(desktop)
        for unit in services:
            (units / unit).write_text('[Service]\nExecStart=original\n')
    profile = etc / 'dconf/profile/user'
    if existing_profile:
        profile.parent.mkdir(parents=True)
        profile.write_text('user-db:user\nsystem-db:site\n')
    # These are the schemas observed on rc.13; absent optional packages are supported.
    schemas = ''
    if installed:
        schemas = (
            'org.gnome.shell.extensions.ding\ncom.ubuntu.update-notifier'
            if base_os == 'ubuntu2604'
            else 'org.gnome.software'
        )
    protected = {}
    for unit in [
        'apt-daily.timer',
        'apt-daily-upgrade.timer',
        'unattended-upgrades.service',
        'packagekit.service',
        'packagekit-offline-update.service',
    ]:
        path = etc / 'systemd/system' / unit
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[Unit]\nDescription=preserve\n')
        protected[path] = path.read_bytes()
    crash = etc / 'systemd/user/update-notifier-crash.path'
    crash.parent.mkdir(parents=True, exist_ok=True)
    crash.write_text('[Path]\nPathExistsGlob=/var/crash/*\n')
    protected[crash] = crash.read_bytes()
    backgrounds = tmp_path / 'backgrounds'
    backgrounds.mkdir()
    for name in WALLPAPERS[base_os]:
        (backgrounds / name).touch()
    script = (
        f'gsettings() {{ printf "%s\\n" "{schemas}"; }}\n'
        'dconf() { echo "dconf $*"; }\n'
        + block.replace('/etc/', f'{etc}/')
        .replace('/usr/lib/systemd/user/', f'{units}/')
        .replace('/usr/local/share/', f'{tmp_path}/share/')
        .replace('/usr/share/backgrounds/', f'{backgrounds}/')
    )
    for _ in range(2):
        result = subprocess.run(
            ['bash', '-eu', '-c', script], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
    assert unrelated.read_text() == desktop
    assert all(path.read_bytes() == value for path, value in protected.items())
    if base_os == 'ubuntu2604':
        assert 'Hidden=true' in updater.read_text()
    else:
        assert updater.read_text() == desktop
    assert profile.read_text().count('system-db:local\n') == 1
    if existing_profile:
        assert 'system-db:site\n' in profile.read_text()
    defaults = configparser.ConfigParser()
    defaults.read(etc / 'dconf/db/local.d/00-idea-desktop')
    assert defaults['org/gnome/desktop/interface']['color-scheme'] == "'prefer-dark'"
    theme = 'Yaru-dark' if base_os == 'ubuntu2604' else 'Adwaita-dark'
    assert defaults['org/gnome/desktop/interface']['gtk-theme'] == f"'{theme}'"
    if base_os != 'ubuntu2604':
        assert (tmp_path / 'share/themes/Adwaita-dark/gtk-3.0/gtk.css').read_text() == (
            '@import url("resource:///org/gtk/libgtk/theme/Adwaita/gtk-dark.css");\n'
        )
    assert 'dconf update' in result.stdout
    if installed:
        assert 'Hidden=true' in welcome.read_text()
        assert 'Hidden=false' not in welcome.read_text()
        for unit in services:
            assert (etc / 'systemd/user' / unit).readlink() == Path('/dev/null')
        if base_os == 'ubuntu2604':
            assert 'Hidden=true' in pro.read_text()
            for key in ['show-home', 'show-trash']:
                assert defaults['org/gnome/shell/extensions/ding'][key] == 'false'
            notifier = defaults['com/ubuntu/update-notifier']
            for key in [
                'notify-ubuntu-advantage-available',
                'show-livepatch-status-icon',
                'show-updates-status-icon',
            ]:
                assert notifier[key] == 'false'
            for key in ['no-show-notifications', 'hide-reboot-notification']:
                assert notifier[key] == 'true'
        else:
            for key in ['allow-updates', 'download-updates', 'download-updates-notify']:
                assert defaults['org/gnome/software'][key] == 'false'
    else:
        assert not welcome.exists()
        assert not pro.exists()
        assert list((etc / 'systemd/user').iterdir()) == [crash]
    if not schemas:
        assert defaults.sections() == [
            'org/gnome/desktop/interface',
            'org/gnome/desktop/background',
        ]
    background = defaults['org/gnome/desktop/background']
    for key, name in zip(('picture-uri', 'picture-uri-dark'), WALLPAPERS[base_os]):
        assert background[key] == f"'{(backgrounds / name).as_uri()}'"
    assert background['picture-options'] == "'zoom'"
    assert (
        etc / 'dconf/db/local.d/locks/idea-background'
    ).read_text().splitlines() == [
        '/org/gnome/desktop/background/picture-uri',
        '/org/gnome/desktop/background/picture-uri-dark',
        '/org/gnome/desktop/background/picture-options',
    ]


@pytest.mark.parametrize(
    'base_os,light,dark',
    [
        ('rhel10', 'rhel10-brandrects-l.jpg', 'rhel10-brandrects-d.jpg'),
        (
            'rocky10',
            'rocky-default-10-gemstone-skies-day.png',
            'rocky-default-10-gemstone-skies-night.png',
        ),
        ('ubuntu2604', 'warty-final-ubuntu.png', 'ubuntu-wallpaper-d.png'),
    ],
)
def test_each_os_uses_its_own_stock_wallpaper(base_os, light, dark):
    # the shared home carries another OS's wallpaper setting; a locked system default wins,
    # and a missing file only costs a plain colour, never the bootstrap
    text = render('_templates/linux/gnome_desktop_polish.jinja2', base_os)
    assert f'desktop_light=file:///usr/share/backgrounds/{light}' in text
    assert f'desktop_dark=file:///usr/share/backgrounds/{dark}' in text
    assert 'desktop_wallpaper' not in text
    assert '/org/gnome/desktop/background/picture-uri-dark' in text


@pytest.mark.parametrize('base_os', WAYLAND + X11)
@pytest.mark.parametrize('launcher_installed', [True, False])
def test_host_preserves_the_2026_vendor_unit(tmp_path, base_os, launcher_installed):
    from test_image_bake_bootstrap import render as render_host

    text = render_host(
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2', base_os
    )
    function = (
        'function start_and_configure_dcv_service() {'
        + text.split('function start_and_configure_dcv_service() {')[1].split('\n}\n')[
            0
        ]
        + '\n}'
    )
    vendor = (
        Path(__file__).parent / 'fixtures/dcv-2026.0/dcvserver.service'
    ).read_text()
    unit = tmp_path / 'dcvserver.service'
    unit.write_text(vendor)
    result = subprocess.run(
        [
            'bash',
            '-c',
            'sudo() { "$@"; }\n'
            'systemctl() { if [[ "$1" == cat ]]; then return '
            + ('0' if launcher_installed else '1')
            + '; fi; }\n'
            + function.replace('/usr/lib/systemd/system/dcvserver.service', str(unit))
            + '\nstart_and_configure_dcv_service',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    if launcher_installed:
        assert unit.read_text() == vendor
    else:
        assert 'DefaultDependencies=no' in unit.read_text()
        assert 'RuntimeDirectory=' not in unit.read_text()


@pytest.mark.parametrize(
    'state,active,ok',
    [
        ('not-found', False, True),
        ('loaded', False, False),
        ('loaded', True, True),
        ('masked', False, False),
    ],
)
def test_bake_checks_the_launcher_when_installed(tmp_path, state, active, ok):
    text = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'rhel10')
    assert "check dcv-launcher '" in text
    function = (
        'dcv_launcher_ok() {'
        + text.split('dcv_launcher_ok() {')[1].split('\n}\n')[0]
        + '\n}'
    )
    calls = tmp_path / 'calls'
    result = subprocess.run(
        [
            'bash',
            '-c',
            f'systemctl() {{ echo "$*" >> "{calls}"; '
            f'if [[ "$1" == show ]]; then echo {state}; '
            f'else return {0 if active else 1}; fi; }}\n'
            + function
            + '\ndcv_launcher_ok',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is ok, result.stdout
    if state == 'not-found':
        assert 'start ' not in calls.read_text()
        assert 'is-active' not in calls.read_text()


@pytest.mark.parametrize('base_os', WAYLAND + X11)
@pytest.mark.parametrize('launcher_installed', [True, False])
def test_dcv_2026_orders_launcher_after_runtime_directory_creation(
    tmp_path, base_os, launcher_installed
):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    # This must also execute for a preinstalled server on a baked image.
    setup = text[text.index('# DCV 2026.0 service dependencies') :]
    assert 'if systemctl cat dcvsessionlauncher.service' in setup
    assert 'dcvserver.service.d/idea.conf' in setup
    assert 'Wants=dcvsessionlauncher.service' in setup
    assert 'After=network-online.target remote-fs.target' in setup
    assert 'dcvsessionlauncher.service.d/idea.conf' in setup
    assert 'After=dcvserver.service' in setup
    assert 'systemctl daemon-reload' in setup
    assert 'install -d -o dcv -g dcv -m 3777 /var/log/dcv' in setup
    assert 'ProtectSystem=' not in setup and 'ReadWritePaths=' not in setup
    units = tmp_path / 'units'
    result = subprocess.run(
        [
            'bash',
            '-c',
            'install() { echo "install $*"; }\n'
            'systemctl() { if [[ "$1" == cat ]]; then return '
            + ('0' if launcher_installed else '1')
            + '; else echo "$*"; fi; }\n'
            + setup.replace('/etc/systemd/system', str(units)).replace(
                '/etc/tmpfiles.d/idea-dcv.conf', str(tmp_path / 'tmpfiles.conf')
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    if launcher_installed:
        assert (
            tmp_path / 'tmpfiles.conf'
        ).read_text() == 'd /var/log/dcv 3777 dcv dcv -\n'
        assert (units / 'dcvserver.service.d/idea.conf').read_text() == (
            '[Unit]\nAfter=network-online.target remote-fs.target\n'
            'Wants=dcvsessionlauncher.service\n'
        )
        assert (units / 'dcvsessionlauncher.service.d/idea.conf').read_text() == (
            '[Unit]\nAfter=dcvserver.service\n'
        )
        assert 'daemon-reload' in result.stdout
    else:
        assert not units.exists()
        assert not (tmp_path / 'tmpfiles.conf').exists()
        assert result.stdout == ''


@pytest.mark.parametrize('base_os', WAYLAND + X11)
def test_only_ubuntu2604_pins_migration_to_system_python(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert ('user-session-migration.service.d/idea.conf' in text) == (
        base_os == 'ubuntu2604'
    )


def test_ubuntu2604_keeps_gi_and_system_python_for_migration(tmp_path):
    text = render('_templates/linux/dcv_wayland_login.jinja2', 'ubuntu2604')
    setup = text.split('install -d -m 755 /usr/local/libexec')[0]
    result = subprocess.run(
        [
            'bash',
            '-c',
            'apt() { echo "apt $*"; }\n' + setup.replace('/etc/', f'{tmp_path}/'),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert 'apt install -y python3-gi' in result.stdout
    settings = configparser.ConfigParser()
    settings.read(tmp_path / 'systemd/user/user-session-migration.service.d/idea.conf')
    assert settings['Service']['Environment'] == 'PATH=/usr/bin:/bin'


DCV_TARBALL = {
    'rhel10': 'nice-dcv-el10',
    'rocky10': 'nice-dcv-el10',
    'ubuntu2604': 'nice-dcv-ubuntu2604',
}
AGENT = {
    'rhel10': 'nice-dcv-session-manager-agent-el10',
    'rocky10': 'nice-dcv-session-manager-agent-el10',
    'ubuntu2604': 'nice-dcv-session-manager-agent_amd64.ubuntu2604',
}


@pytest.mark.parametrize('base_os', WAYLAND)
def test_dcv_server_installs_the_wayland_packages(base_os):
    # the image bake renders the console superset; the X dummy driver must still stay out
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    for arch in ('x86_64', 'aarch64'):
        assert (
            f'https://d1uj6qtbmh3dt5.cloudfront.net/{DCV_TARBALL[base_os]}-{arch}.tgz"'
            in text
        )
        assert f'{DCV_TARBALL[base_os]}-{arch}.tgz.sha256sum"' in text
    assert 'WaylandEnable=false' not in text
    assert '"nice-xdcv-*' not in text and 'nice-xdcv_*' not in text
    assert 'install_x_dummy_driver' not in text
    assert '/etc/X11/Xsession.d' not in text and 'im-config' not in text
    assert 'systemctl set-default graphical.target' in text
    if base_os == 'ubuntu2604':
        assert (
            'apt install -y ./nice-dcv-server_*.deb ./nice-dcv-web-viewer_*.deb' in text
        )
        assert 'apt install -y ubuntu-desktop-minimal\n' in text
        assert 'dpkg -l gnome-shell' in text
        assert 'systemctl restart gdm3' in text
    else:
        assert (
            'dnf install -y ./nice-dcv-server-*.${machine}.rpm ./nice-dcv-web-viewer-*.${machine}.rpm'
            in text
        )
        assert 'groupinstall "Server with GUI"' in text
        assert 'rpm -qa gnome-shell' in text
        assert '/etc/polkit-1/rules.d/49-idea-disable-package-refresh.rules' in text
        assert 'repos.pkla' not in text


@pytest.mark.parametrize('base_os', X11)
def test_x11_desktops_keep_xdcv_and_disable_wayland(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    assert '"nice-xdcv-*' in text or 'nice-xdcv_*' in text
    assert 'install_x_dummy_driver' in text
    if base_os != 'amazonlinux2023':
        assert 'WaylandEnable=false' in text


@pytest.mark.parametrize('base_os', WAYLAND)
def test_session_manager_agent_has_a_package(base_os):
    text = render('_templates/linux/dcv_session_manager_agent.jinja2', base_os)
    assert f'{AGENT[base_os]}' in text
    assert 'if [[ -z "$(' in text


@pytest.mark.parametrize('base_os', WAYLAND)
def test_host_configuration_skips_dcv_gl(base_os):
    from unittest.mock import MagicMock

    from ideasdk.utils import Jinja2Utils
    from test_dcv_host_ami_builder_bootstrap import IDEA_BOOTSTRAP_DIR

    # a MagicMock context is a GPU instance type, which is the dcvgladmin path on X11
    env = Jinja2Utils.env_using_file_system_loader(IDEA_BOOTSTRAP_DIR)
    template = env.get_template(
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2'
    )
    text = template.render(context=MagicMock(base_os=base_os))
    assert 'dcvgladmin' in template.render(context=MagicMock(base_os='rocky9'))
    gl = text.split('function configure_gl() {')[1].split('\n}\n')[0]
    assert 'no need for this configuration' in gl
    assert 'dcvgladmin' not in gl
    assert 'pulseaudio-utils' in text


@pytest.mark.parametrize('base_os', WAYLAND)
def test_image_checks_require_a_wayland_gdm(base_os):
    text = render('dcv-host-ami-builder/image_checks.sh.jinja2', base_os)
    assert "check wayland '" in text
    assert 'WaylandEnable' in text
    gdm = 'gdm3' if base_os == 'ubuntu2604' else 'gdm'
    assert f'unit_installed {gdm}.service' in text


@pytest.mark.parametrize('base_os', X11)
def test_image_checks_for_x11_desktops_have_no_wayland_check(base_os):
    assert 'check wayland' not in render(
        'dcv-host-ami-builder/image_checks.sh.jinja2', base_os
    )


def test_wayland_check_rejects_a_disabled_wayland(tmp_path):
    import os
    import subprocess

    text = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'rocky10')
    function = (
        'wayland_ok() {' + text.split('wayland_ok() {')[1].split('\n}\n')[0] + '\n}'
    )
    conf = tmp_path / 'custom.conf'
    for content, target, ok in (
        ('[daemon]\n#WaylandEnable=false\n', 'graphical.target', True),
        ('[daemon]\nWaylandEnable=false\n', 'graphical.target', False),
        ('[daemon]\n', 'multi-user.target', False),
    ):
        conf.write_text(content)
        result = subprocess.run(
            [
                'bash',
                '-c',
                'systemctl() { echo "$TARGET"; }\n'
                + function.replace('/etc/gdm/custom.conf', str(conf))
                + '\nwayland_ok',
            ],
            env={'PATH': os.environ['PATH'], 'TARGET': target},
            check=False,
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) is ok, (content, target, result.stdout)


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10'])
def test_el10_desktop_bake_is_supported_end_to_end(base_os):
    setup = render('dcv-host-ami-builder/setup.sh.jinja2', base_os)
    assert 'systemctl enable --now amazon-ssm-agent' in setup
    packages = render('dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2', base_os)
    assert '# Begin: Kernel Version Management' in packages
    assert 'TARGET_KERNEL_VERSION="$(uname -r | cut -d. -f1-3)*"' in packages
    post = render(
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2', base_os
    )
    assert 'nice-dcv-el10' in post
    assert 'package_transaction yum install -y pulseaudio-utils' in post


def test_ubuntu2604_bake_selects_a_kernel_the_fsx_repo_has_a_module_for():
    setup = render('dcv-host-ami-builder/setup.sh.jinja2', 'ubuntu2604')
    assert 'systemctl enable --now snap.amazon-ssm-agent' in setup
    kernel = setup.split('# Begin: Kernel Version Management')[1]
    assert 'apt-cache pkgnames lustre-client-modules-' in kernel
    assert (
        'apt install -y lustre-client-utils "lustre-client-modules-${TARGET_KERNEL_ABI}"'
        in setup
    )
    packages = render(
        'dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2', 'ubuntu2604'
    )
    assert '# Begin: Kernel Version Management' not in packages
    checks = render('dcv-host-ami-builder/image_checks.sh.jinja2', 'ubuntu2604')
    assert (
        'packages_installed lustre-client-utils "lustre-client-modules-$(uname -r)"'
        in checks
    )


def test_ubuntu2604_lustre_client_uses_the_resolute_suite():
    text = render('_templates/linux/fsx_lustre_client.jinja2', 'ubuntu2604')
    assert 'not available for Ubuntu 26.04' not in text
    assert 'fsxlustreclientrepo.list' in text
    assert 'lustre-client-modules-$(uname -r)' in text


@pytest.mark.parametrize('arch', ['amd64', 'arm64'])
@pytest.mark.parametrize('running', ['7.0.0-1010-aws', '7.0.0-1014-aws'])
def test_ubuntu2604_bake_pins_the_fsx_kernel(tmp_path, arch, running):
    # resolute carries one module ABI, 7.0.0-1014-aws: an image on another ABI moves to it
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2604',
        running,
        BAKE_STAGE='packages',
        KERNEL_ARCH=arch,
        REPO_ABIS='7.0.0-1014-aws',
        FSX_ABIS='7.0.0-1014-aws',
        IMAGE_ABIS='7.0.0-1010-aws 7.0.0-1012-aws 7.0.0-1013-aws 7.0.0-1014-aws',
    )
    assert code == 0, calls
    assert 'apt-mark hold linux-image-7.0.0-1014-aws' in calls
    assert 'Advanced options for Ubuntu>Ubuntu, with Linux 7.0.0-1014-aws' in calls
    if running == '7.0.0-1014-aws':
        assert 'apt install' not in calls
    else:
        assert 'apt install -y linux-image-7.0.0-1014-aws' in calls
        assert 'reboot_required Ubuntu kernel version change' in calls
    assert '\nreboot\n' not in calls


def test_ubuntu2604_bake_stops_when_no_kernel_has_a_module(tmp_path):
    code, calls = run_set_kernel(
        tmp_path,
        'ubuntu2604',
        '7.0.0-1010-aws',
        BAKE_STAGE='packages',
        KERNEL_ARCH='amd64',
        REPO_ABIS='7.0.0-1014-aws',
        FSX_ABIS='7.0.0-1014-aws',
        IMAGE_ABIS='7.0.0-1010-aws',
    )
    assert code == 1, calls
    assert 'Value=lustre-module-missing' in calls


@pytest.mark.parametrize('base_os', ['rhel10', 'rocky10'])
@pytest.mark.parametrize(
    'running', ['6.12.0-211.7.1.el10_2.x86_64', '6.12.0-124.8.1.el10_1.aarch64']
)
def test_el10_desktop_keeps_and_locks_the_kernel_its_image_boots(
    tmp_path, base_os, running
):
    code, calls = run_set_kernel(tmp_path, base_os, running)
    assert code == 0, calls
    assert 'reboot' not in calls
    assert 'dnf install -y kernel' not in calls
    assert f'dnf versionlock kernel-{running} kernel-core-{running}' in calls


@pytest.mark.parametrize('base_os', WAYLAND + X11)
def test_wait_online_fix_is_baked_and_applied_only_on_ubuntu2604(base_os):
    from test_image_bake_bootstrap import render as render_host

    for entrypoint in [
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
    ]:
        text = render_host(entrypoint, base_os)
        assert ('# Begin: Ubuntu Network Wait Online' in text) == (
            base_os == 'ubuntu2604'
        )


@pytest.mark.parametrize(
    'renderer, networkd, masked',
    [
        ('NetworkManager', False, True),
        ('NetworkManager', True, False),
        ('networkd', True, False),
        ('networkd', False, False),
    ],
)
def test_wait_online_preserves_the_configured_renderer(
    tmp_path, renderer, networkd, masked
):
    text = render('_templates/linux/ubuntu_network_wait_online.jinja2', 'ubuntu2604')
    if networkd:
        (tmp_path / '10-netplan-ens5.network').touch()
    text = text.replace('/run/systemd/network', str(tmp_path))
    calls = tmp_path / 'calls'
    script = (
        f"""netplan() {{
  if [[ "$1" == get ]]; then echo {renderer}; else echo generate >> {calls}; fi
}}
systemctl() {{ echo "$*" >> {calls}; }}
"""
        + text
    )
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    commands = calls.read_text().splitlines()
    assert commands[0] == 'generate'
    assert ('mask systemd-networkd-wait-online.service' in commands) == masked
    assert ('enable NetworkManager-wait-online.service' in commands) == masked
