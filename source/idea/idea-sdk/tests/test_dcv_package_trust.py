"""
desktop DCV packages are trusted on the NICE key carried in the bootstrap bundle, never on a key
or checksum fetched from the package host: RPMs and Ubuntu 22.04 .debs must carry a signature
from the pinned key before they are installed, and the unsigned Ubuntu 24.04/26.04 packages must
match a SHA-256 pinned in settings.
"""

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from test_bootstrap_shell_syntax import Config
from test_dcv_host_ami_builder_bootstrap import IDEA_BOOTSTRAP_DIR, render

NICE_FINGERPRINT = '5B9EEBC864449701F6CE566A11B5C70A170C6114'
BUNDLED_KEY = os.path.join(IDEA_BOOTSTRAP_DIR, '_templates', 'linux', 'NICE-GPG-KEY')
IMAGE_KEY = os.path.join(
    IDEA_BOOTSTRAP_DIR,
    '..',
    '..',
    '..',
    'deployment',
    'ecr',
    'idea-control-plane',
    'NICE-GPG-KEY',
)
RPM_OS = ['amazonlinux2023', 'rhel8', 'rhel9', 'rhel10', 'rocky8', 'rocky9', 'rocky10']
PINNED_OS = ['ubuntu2404', 'ubuntu2604']
LINUX_OS = RPM_OS + ['ubuntu2204'] + PINNED_OS


def primary_fingerprint(armored: str) -> str:
    body = armored.split('\n\n', 1)[1].split('-----END', 1)[0]
    data = base64.b64decode(
        ''.join(line for line in body.splitlines() if not line.startswith('='))
    )
    assert data[0] & 0x3F == 0x06 or (data[0] >> 2) & 0x0F == 6, (
        'first packet is not a public key'
    )
    if data[0] & 0x40:
        length, start = (data[1] << 8 | data[2]) - (192 << 8) + 192, 3
        if data[1] < 192:
            length, start = data[1], 2
        elif data[1] == 255:
            length, start = int.from_bytes(data[2:6], 'big'), 6
    else:
        size = {0: 1, 1: 2, 2: 4}[data[0] & 0x03]
        length, start = int.from_bytes(data[1 : 1 + size], 'big'), 1 + size
    packet = data[start : start + length]
    return (
        hashlib.sha1(b'\x99' + len(packet).to_bytes(2, 'big') + packet)
        .hexdigest()
        .upper()
    )


def test_bundled_key_is_the_pinned_nice_key():
    with open(BUNDLED_KEY) as f:
        bundled = f.read()
    with open(IMAGE_KEY) as f:
        assert f.read() == bundled
    assert bundled.count('BEGIN PGP PUBLIC KEY BLOCK') == 1
    assert primary_fingerprint(bundled) == NICE_FINGERPRINT


@pytest.mark.parametrize('template', ['dcv_server', 'dcv_session_manager_agent'])
@pytest.mark.parametrize('base_os', LINUX_OS)
def test_no_key_is_fetched_and_the_bundled_key_is_used(template, base_os):
    text = render(f'_templates/linux/{template}.jinja2', base_os)
    with open(BUNDLED_KEY) as f:
        assert f.read().strip() in text
    assert f'DCV_NICE_FINGERPRINT={NICE_FINGERPRINT}' in text
    assert 'gpg_key' not in text and 'NICE-GPG-KEY' not in text.replace(
        'deployment/ecr/idea-control-plane/NICE-GPG-KEY', ''
    )
    if base_os in RPM_OS:
        assert 'rpm --import "${DCV_NICE_KEY}"' in text
    else:
        assert 'gpg --dearmor < "${DCV_NICE_KEY}"' in text


def before(text: str, check: str, install: str):
    assert f'{check} || dcv_untrusted ' in text, check
    assert text.index(check) < text.index(install), (check, install)


@pytest.mark.parametrize('base_os', LINUX_OS)
def test_dcv_server_packages_are_verified_before_install(base_os):
    text = render('_templates/linux/dcv_server.jinja2', base_os)
    if base_os in ('rhel10', 'rocky10'):
        before(
            text,
            'dcv_rpms_signed nice-dcv-server-*.${machine}.rpm nice-dcv-web-viewer-*.${machine}.rpm',
            'dnf install -y ./nice-dcv-server-',
        )
    elif base_os in RPM_OS:
        before(
            text,
            'dcv_rpms_signed nice-xdcv-*.${machine}.rpm nice-dcv-server-*.${machine}.rpm nice-dcv-web-viewer-*.${machine}.rpm',
            'install_rpm_with_retry "nice-xdcv-',
        )
    elif base_os == 'ubuntu2204':
        before(
            text,
            'dcv_debs_signed nice-xdcv_*.deb nice-dcv-server_*.deb nice-dcv-web-viewer_*.deb',
            'dpkg -i nice-xdcv_*.deb',
        )
    else:
        before(
            text,
            'dcv_sha256_pinned "${DCV_SERVER_TGZ}" "${DCV_SERVER_SHA256_HASH}"',
            'tar zxvf',
        )
        assert 'wget ${DCV_SERVER_SHA256_HASH}' not in text


@pytest.mark.parametrize('base_os', LINUX_OS)
def test_session_manager_agent_is_verified_before_install(base_os):
    text = render('_templates/linux/dcv_session_manager_agent.jinja2', base_os)
    if base_os in RPM_OS:
        before(
            text, 'dcv_rpms_signed "${PACKAGE_NAME}"', 'yum install -y ${PACKAGE_NAME}'
        )
    elif base_os == 'ubuntu2204':
        before(text, 'dcv_debs_signed "${PACKAGE_NAME}"', 'dpkg -i ${PACKAGE_NAME}')
    else:
        before(
            text,
            'dcv_sha256_pinned "${PACKAGE_NAME}" "${AGENT_SHA256_HASH}"',
            'dpkg -i ${PACKAGE_NAME}',
        )
        assert 'wget ${AGENT_SHA256_HASH}' not in text


@pytest.mark.parametrize(
    'path',
    [
        'x86_64.debian.{os}',
        'aarch64.linux.{os}',
        'agent.x86_64.ubuntu.{os}',
        'agent.aarch64.linux.{os}',
    ],
)
@pytest.mark.parametrize('base_os', PINNED_OS)
def test_unsigned_ubuntu_packages_are_pinned_to_a_release(path, base_os):
    entry = Config().get_config('global-settings')['package_config']['dcv']
    for part in path.format(os=base_os).split('.'):
        entry = entry[part]
    assert re.fullmatch(
        r'https://d1uj6qtbmh3dt5\.cloudfront\.net/2\d{3}\.\d+/[^/]+/[^/]+', entry['url']
    )
    assert re.fullmatch(r'[0-9a-f]{64}', entry['sha256sum'])


def run_trust(
    tmp_path, script: str, path_prefix: str = ''
) -> subprocess.CompletedProcess:
    trust = tmp_path / 'trust.sh'
    trust.write_text(render('_templates/linux/dcv_package_trust.jinja2', 'rocky9'))
    prelude = (
        'log_info() { echo "INFO $*"; }\nlog_error() { echo "ERROR $*"; }\n'
        f'source {trust}\n'
    )
    env = dict(os.environ)
    if path_prefix:
        env['PATH'] = f'{path_prefix}:{env["PATH"]}'
    return subprocess.run(
        ['bash', '-c', prelude + script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_rpm_check_requires_a_signature_from_a_scratch_keyring(tmp_path):
    stub = tmp_path / 'bin'
    stub.mkdir()
    # --checksig prints the file's content, so each file decides what rpmkeys reports
    (stub / 'rpmkeys').write_text(
        '#!/bin/bash\necho "$*" >> "${RPMKEYS_LOG}"\n'
        'if [[ "$3" == --checksig ]]; then echo "$4: $(cat "$4")"; fi\n'
    )
    (stub / 'rpmkeys').chmod(0o755)
    (tmp_path / 'signed.rpm').write_text('digests signatures OK')
    (tmp_path / 'unsigned.rpm').write_text('digests OK')
    (tmp_path / 'wrongkey.rpm').write_text('digests SIGNATURES NOT OK')
    log = tmp_path / 'rpmkeys.log'
    script = f'export RPMKEYS_LOG={log}\n'
    result = run_trust(tmp_path, script + 'dcv_rpms_signed signed.rpm', str(stub))
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert re.fullmatch(r'--dbpath \S+ --import \S+', calls[0])
    dbpath = calls[0].split()[1]
    assert (
        dbpath != '/var/lib/rpm'
        and calls[1] == f'--dbpath {dbpath} --checksig signed.rpm'
    )
    for bad in (
        'unsigned.rpm',
        'wrongkey.rpm',
        'signed.rpm unsigned.rpm',
        'missing.rpm',
    ):
        result = run_trust(tmp_path, script + f'dcv_rpms_signed {bad}', str(stub))
        assert result.returncode != 0, bad
        assert 'not signed by the pinned NICE key' in result.stdout


def test_sha256_pin_is_a_digest_not_a_url(tmp_path):
    (tmp_path / 'pkg.tgz').write_bytes(b'dcv')
    digest = hashlib.sha256(b'dcv').hexdigest()
    for value, ok in (
        (digest, True),
        (digest.upper(), True),
        (hashlib.sha256(b'other').hexdigest(), False),
        ('https://d1uj6qtbmh3dt5.cloudfront.net/pkg.tgz.sha256sum', False),
        ('', False),
    ):
        result = run_trust(tmp_path, f'dcv_sha256_pinned pkg.tgz "{value}"')
        assert (result.returncode == 0) is ok, (value, result.stdout)


DEB_TOOLS = ('ar', 'gpg', 'gpgconf', 'md5sum', 'sha1sum')


def write_ar(path, members):
    # common ar format, as a .deb uses; built here because macOS ar rewrites archives it creates
    out = [b'!<arch>\n']
    for name, data in members.items():
        out.append(
            f'{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n'.encode()
        )
        out.append(data + (b'\n' if len(data) % 2 else b''))
    path.write_bytes(b''.join(out))


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in DEB_TOOLS),
    reason='needs ar, gpg and coreutils',
)
def test_deb_check_follows_the_dpkg_sig_builder_signature(tmp_path):
    # short path: gpg-agent's socket must fit in sun_path
    home = tempfile.mkdtemp(prefix='gpg', dir='/tmp')
    try:
        signed_debs(tmp_path, ['gpg', '--batch', '--homedir', home])
    finally:
        subprocess.run(
            ['gpgconf', '--homedir', home, '--kill', 'gpg-agent'], capture_output=True
        )
        shutil.rmtree(home, ignore_errors=True)


def signed_debs(tmp_path, gpg):
    subprocess.run(
        gpg
        + [
            '--passphrase',
            '',
            '--quick-gen-key',
            'Test <t@example.invalid>',
            'ed25519',
            'sign',
            'never',
        ],
        check=True,
        capture_output=True,
    )
    armored = subprocess.run(
        gpg + ['--armor', '--export'], check=True, capture_output=True
    ).stdout
    (tmp_path / 'test.asc').write_bytes(armored)
    fingerprint = primary_fingerprint(armored.decode())
    members = {
        'debian-binary': b'2.0\n',
        'control.tar.zst': os.urandom(64),
        'data.tar.zst': os.urandom(256),
    }
    lines = ''.join(
        f'\t{hashlib.md5(v).hexdigest()} {hashlib.sha1(v).hexdigest()} {len(v)} {k}\n'
        for k, v in members.items()
    )
    (tmp_path / 'list').write_text(
        f'Version: 4\nSigner: Test\nRole: builder\nFiles: \n{lines}'
    )
    subprocess.run(
        gpg
        + [
            '--clearsign',
            '--output',
            str(tmp_path / '_gpgbuilder'),
            str(tmp_path / 'list'),
        ],
        check=True,
    )
    signature = (tmp_path / '_gpgbuilder').read_bytes()
    tampered = dict(members, **{'data.tar.zst': members['data.tar.zst'] + b'x'})
    write_ar(tmp_path / 'signed.deb', {**members, '_gpgbuilder': signature})
    write_ar(tmp_path / 'stripped.deb', members)
    write_ar(tmp_path / 'tampered.deb', {**tampered, '_gpgbuilder': signature})
    write_ar(
        tmp_path / 'extra.deb', {**members, 'evil': b'evil', '_gpgbuilder': signature}
    )

    test_key = f'DCV_NICE_KEY={tmp_path}/test.asc DCV_NICE_FINGERPRINT={fingerprint}\n'
    assert run_trust(tmp_path, test_key + 'dcv_debs_signed signed.deb').returncode == 0
    for bad in ('stripped.deb', 'tampered.deb', 'extra.deb', 'signed.deb stripped.deb'):
        result = run_trust(tmp_path, test_key + f'dcv_debs_signed {bad}')
        assert result.returncode != 0, (bad, result.stdout)
    # the same package against the real pinned key: signed, but by the wrong key
    result = run_trust(tmp_path, 'dcv_debs_signed signed.deb')
    assert (
        result.returncode != 0 and 'not signed by the pinned NICE key' in result.stdout
    )


OLD_DEFAULT_NAMES = {
    'amazonlinux2023': (
        'nice-dcv-amzn2023',
        'nice-dcv-session-manager-agent-amzn2023.{arch}.rpm',
    ),
    'rocky8': ('nice-dcv-el8', 'nice-dcv-session-manager-agent-el8.{arch}.rpm'),
    'rhel9': ('nice-dcv-el9', 'nice-dcv-session-manager-agent-el9.{arch}.rpm'),
    'rocky10': ('nice-dcv-el10', 'nice-dcv-session-manager-agent-el10.{arch}.rpm'),
    'ubuntu2204': (
        'nice-dcv-ubuntu2204',
        'nice-dcv-session-manager-agent_{deb}.ubuntu2204.deb',
    ),
}


@pytest.mark.parametrize('base_os', OLD_DEFAULT_NAMES)
def test_signed_families_keep_the_stored_url_shaped_rows(base_os):
    # upgrades leave these rows as earlier releases stored them: a package URL and a .sha256sum URL
    server, agent = OLD_DEFAULT_NAMES[base_os]
    cdn = 'https://d1uj6qtbmh3dt5.cloudfront.net'
    server_text = render('_templates/linux/dcv_server.jinja2', base_os)
    agent_text = render('_templates/linux/dcv_session_manager_agent.jinja2', base_os)
    for arch, deb in (('x86_64', 'amd64'), ('aarch64', 'arm64')):
        assert f'"{cdn}/{server}-{arch}.tgz"' in server_text
        assert f'"{cdn}/{server}-{arch}.tgz.sha256sum"' in server_text
        name = agent.format(arch=arch, deb=deb)
        assert (
            f'"{cdn}/{name}"' in agent_text
            and f'"{cdn}/{name}.sha256sum"' in agent_text
        )
    assert (
        'wget ${DCV_SERVER_SHA256_HASH}' in server_text
        and 'dcv_sha256_pinned' not in server_text.split('# End: DCV package trust')[1]
    )
    assert (
        'wget ${AGENT_SHA256_HASH}' in agent_text
        and 'dcv_sha256_pinned' not in agent_text.split('# End: DCV package trust')[1]
    )
