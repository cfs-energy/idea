"""
Linux host bootstrap: secrets stay out of traces and argument lists, values from settings and
job data are shell data, logs on shared storage are root only, shared file systems mount
nodev, the netplan step runs isolated, and downloads are verified before use.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR, Config
from test_image_bake_bootstrap import render

COMMON = os.path.join(IDEA_BOOTSTRAP_DIR, 'common', 'bootstrap_common.sh')
LOG_STUBS = """
log_info() { echo "info $*"; }
log_warning() { echo "warning $*"; }
log_error() { echo "error $*"; }
"""


def bash(script, **kwargs):
    return subprocess.run(
        [BASH, '-c', script], capture_output=True, text=True, **kwargs
    )


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize(
    'component,variable',
    [
        ('compute-node/setup.sh.jinja2', 'IDEA_COMPUTE_NODE_LOGS_DIR'),
        (
            'compute-node-ami-builder/setup.sh.jinja2',
            'IDEA_COMPUTE_NODE_AMI_BUILDER_LOGS_DIR',
        ),
        ('dcv-host-ami-builder/setup.sh.jinja2', 'IDEA_DCV_HOST_AMI_BUILDER_LOGS_DIR'),
    ],
)
def test_bootstrap_logs_on_shared_storage_are_root_only(tmp_path, component, variable):
    from test_bootstrap_shell_syntax import job

    script = render(
        component,
        'rhel9',
        variables={
            'job': job(),
            'job_directory': '/apps/idea-mock/jobs/1',
            'image_kind': 'compute',
        },
    )
    (line,) = [
        s for s in script.splitlines() if s.startswith(f'mkdir -p "${{{variable}}}"')
    ]
    logs = tmp_path / 'logs' / 'host'
    result = bash(f'umask 022\n{variable}={logs}\n{line}\n')
    assert result.returncode == 0, result.stderr
    assert oct(mode(logs)) == oct(0o700)


def ad_join(tmp_path):
    config = Config()
    config.put('directoryservice.name', 'corp.example.invalid')
    config.put(
        'directoryservice.ad_automation.sqs_queue_url',
        'https://sqs.example.invalid/queue',
    )
    config.put('directoryservice.sudoers.group_name', 'Idea Admins')
    block = render(
        '_templates/linux/join_activedirectory.jinja2', 'rhel9', config=config
    )
    return block.replace('/root/', f'{tmp_path}/').replace('/etc/', f'{tmp_path}/etc/')


@pytest.mark.skipif(
    BASH is None or shutil.which('jq') is None, reason='needs bash and jq'
)
def test_directory_join_keeps_nonce_and_one_time_password_out_of_the_trace(tmp_path):
    (tmp_path / 'etc' / 'sssd').mkdir(parents=True)
    (tmp_path / 'etc' / 'sudoers').write_text('')
    (tmp_path / 'etc' / 'nsswitch.conf').write_text('')
    calls = tmp_path / 'calls'
    stubs = f"""
IDEA_CLUSTER_NAME=idea-mock
AWS_REGION=us-east-1
instance_id() {{ echo i-0123456789abcdef0; }}
aws() {{
  echo "aws $*" >> {calls}
  if [[ "$*" == *get-item* ]]; then
    echo '{{"Item": {{"status": {{"S": "success"}}, "otp": {{"S": "MARKER-OTP-VALUE"}}, "hostname": {{"S": "IDEA-HOST"}}, "domain_controller": {{"S": "dc.example.invalid"}}}}}}'
  fi
}}
realm() {{
  echo "realm $*" >> {calls}
  case "$1" in
    join) touch {tmp_path}/joined ;;
    list) [[ -f {tmp_path}/joined ]] && echo corp.example.invalid ;;
    *) return 1 ;;
  esac
}}
systemctl() {{ :; }}
disable_sssd_responder_sockets() {{ :; }}
sleep() {{ :; }}
export -f aws realm
set -x
"""
    common = tmp_path / 'common.sh'
    common.write_text(open(COMMON).read().replace('/root/', f'{tmp_path}/'))
    result = bash(
        f'source {common}\n'
        + LOG_STUBS
        + stubs
        + ad_join(tmp_path).replace('$(command -v realm)', 'realm')
    )
    recorded = calls.read_text()
    # the join really ran with the value it received
    assert '--one-time-password=MARKER-OTP-VALUE' in recorded
    nonce = recorded.split('"nonce":"', 1)[1].split('"', 1)[0]
    assert len(nonce) == 32
    trace = result.stderr
    assert 'MARKER-OTP-VALUE' not in trace and nonce not in trace
    # tracing resumes for the rest of the block
    assert 'sssd.conf' in trace


@pytest.mark.skipif(BASH is None, reason='bash not installed')
def test_openldap_root_password_stays_out_of_the_trace_and_arguments(tmp_path):
    config = Config()
    config.put('directoryservice.hostname', 'ldap.example.invalid')
    config.put('directoryservice.ldap_base', 'dc=example,dc=invalid')
    config.put('directoryservice.provider', 'openldap')
    config.put('directoryservice.name', 'example.invalid')
    block = render(
        'openldap-server/_templates/install_openldap.jinja2',
        'amazonlinux2023',
        config=config,
    )
    certs = tmp_path / 'etc' / 'openldap' / 'certs'
    certs.mkdir(parents=True)
    block = block.replace('/etc/', f'{tmp_path}/etc/')
    calls = tmp_path / 'calls'
    stubs = f"""
BOOTSTRAP_DIR={tmp_path}
cat() {{ [[ "$1" == */infra.cfg ]] && return 0; command cat "$@"; }}
source() {{ :; }}
get_secret() {{ case "$1" in *PASSWORD*) echo -n MARKER-ROOT-PASSWORD ;; *KEY*) echo -n MARKER-TLS-KEY ;; *) echo -n value ;; esac; }}
LDAP_ROOT_PASSWORD_SECRET_ARN=PASSWORD LDAP_TLS_PRIVATE_KEY_SECRET_ARN=KEY
package_transaction() {{ :; }}
systemctl() {{ :; }}
chown() {{ :; }}
authconfig() {{ :; }}
record() {{ echo "$*" >> {calls}; command cat > /dev/null; }}
slappasswd_stub() {{ echo "slappasswd $* stdin=$(command cat)" >> {calls}; echo '{{CRYPT}}hash'; }}
set -x
"""
    block = (
        block.replace('/sbin/slappasswd', 'slappasswd_stub')
        .replace('/bin/ldapmodify', 'record ldapmodify')
        .replace('/bin/ldapadd', 'record ldapadd')
    )
    result = bash(LOG_STUBS + stubs + block)
    recorded = calls.read_text()
    assert (
        'slappasswd -T /dev/stdin' in recorded
        and 'stdin=MARKER-ROOT-PASSWORD' in recorded
    )
    assert 'MARKER-ROOT-PASSWORD' not in recorded.replace(
        'stdin=MARKER-ROOT-PASSWORD', ''
    )
    assert 'MARKER-ROOT-PASSWORD' not in result.stderr
    assert 'MARKER-TLS-KEY' not in result.stderr
    assert oct(mode(certs / 'idea-openldap.key')) == oct(0o600)


@pytest.mark.skipif(BASH is None, reason='bash not installed')
@pytest.mark.parametrize(
    'name', ['tag_ebs_volumes.jinja2', 'tag_network_interface.jinja2']
)
def test_node_tags_reach_the_cli_as_data(tmp_path, name):
    tags = [
        {'Key': 'idea:JobOwnerEmail', 'Value': "o'brien@example.invalid"},
        {'Key': 'Name', 'Value': 'a "b" $(printf MARKER) `printf MARKER` $HOME'},
    ]
    variable = 'ebs_volume_tags' if 'ebs' in name else 'network_interface_tags'
    from jinja2 import Environment, FileSystemLoader
    from ideasdk.context import BootstrapContext

    context = BootstrapContext(
        config=Config(),
        module_name='scheduler',
        module_id='scheduler',
        module_set='default',
        base_os='rhel9',
        instance_type='m7i.large',
    )
    block = (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template(f'_templates/linux/{name}')
        .render(context=context, **{variable: tags})
    )
    out = tmp_path / 'tags.json'
    stubs = f"""
instance_id() {{ echo i-0123456789abcdef0; }}
aws() {{
  case "$*" in
    *create-tags*) local f="${{@: -1}}"; command cat "${{f#file://}}" > {out}; mode=$(stat -c %a "${{f#file://}}" 2>/dev/null || stat -f %Lp "${{f#file://}}"); echo "$mode" > {out}.mode ;;
    *) echo id-1 ;;
  esac
}}
"""
    result = bash(LOG_STUBS + stubs + block)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text())[: len(tags)] == tags
    assert (tmp_path / 'tags.json.mode').read_text().strip() == '600'


@pytest.mark.skipif(BASH is None, reason='bash not installed')
def test_shared_storage_settings_render_as_shell_data(tmp_path):
    config = Config()
    value = 'nfs4 rw,"x" $(printf MARKER) `printf MARKER` \'q\' 0 0'
    config.put('shared-storage.apps.mount_options', value)
    config.put('shared-storage.apps.mount_dir', "/apps dir'$(printf MARKER)")
    block = render(
        '_templates/linux/mount_shared_storage.jinja2',
        'rhel9',
        lustre=False,
        config=config,
    )
    start = block.index('  function mount_shared_storage () {')
    body = block[start : block.index('\n    local FS_MOUNT_ATTEMPT', start)] + '\n}\n'
    calls = tmp_path / 'calls'
    stubs = f"""
mkdir() {{ printf '%s\\n' "mkdir $*" >> {calls}; }}
add_efs_to_fstab() {{ printf '%s|' "$@" >> {calls}; echo >> {calls}; }}
"""
    result = bash(stubs + body + 'mount_shared_storage\n')
    assert result.returncode == 0, result.stderr
    recorded = calls.read_text()
    assert f"|/apps dir'$(printf MARKER)|{value}|" in recorded
    assert 'MARKER\n' not in recorded.replace('$(printf MARKER)', '')


@pytest.mark.skipif(BASH is None, reason='bash not installed')
@pytest.mark.parametrize(
    'given,expected',
    [
        (
            'nfs4 nfsvers=4.1,hard 0 0',
            'nfs4 nfsvers=4.1,hard,nodev 0 0',
        ),
        (
            'lustre defaults,noatime,flock,_netdev 0 0',
            'lustre defaults,noatime,flock,_netdev,nodev 0 0',
        ),
        # suid and nosuid pass through as given; an administrator who names dev keeps it
        ('efs _netdev,tls,iam,suid 0 0', 'efs _netdev,tls,iam,suid,nodev 0 0'),
        ('nfs4 rw,nosuid 0 0', 'nfs4 rw,nosuid,nodev 0 0'),
        ('nfs4 rw,dev 0 0', 'nfs4 rw,dev 0 0'),
    ],
)
def test_shared_file_systems_mount_nodev(tmp_path, given, expected):
    fstab = tmp_path / 'fstab'
    fstab.write_text('')
    script = (
        f'source {COMMON}\n'
        + LOG_STUBS
        + f'add_efs_to_fstab fs.example.invalid /apps "{given}"\n'
    ).replace('/etc/fstab', str(fstab))
    common = open(COMMON).read().replace('/etc/fstab', str(fstab))
    path = tmp_path / 'common.sh'
    path.write_text(common)
    result = bash(script.replace(COMMON, str(path)))
    assert result.returncode == 0, result.stderr
    assert fstab.read_text() == f'fs.example.invalid:/ /apps/ {expected}\n'


@pytest.mark.skipif(BASH is None, reason='bash not installed')
def test_ubuntu_netplan_update_imports_nothing_from_the_working_directory(tmp_path):
    pytest.importorskip('yaml')
    script = render(
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2', 'ubuntu2204'
    )
    assert '/tmp/update_netplan.py' not in script
    start = script.index("/usr/bin/python3 -I - << 'PYEND'")
    step = script[start : script.index('\nPYEND\n', start) + 7]
    netplan = tmp_path / '50-cloud-init.yaml'
    netplan.write_text('network:\n  version: 2\n')
    work = tmp_path / 'work'
    work.mkdir()
    # a module of that name in the working directory must not be imported
    (work / 'yaml.py').write_text(
        f'open({str(tmp_path / "imported")!r}, "w").close()\n'
    )
    step = step.replace('/etc/netplan/50-cloud-init.yaml', str(netplan)).replace(
        '/usr/bin/python3', sys.executable
    )
    result = bash(step, cwd=work)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'imported').exists()
    assert 'renderer: NetworkManager' in netplan.read_text()


@pytest.mark.skipif(BASH is None, reason='bash not installed')
def test_pinned_sha256_check(tmp_path):
    archive = tmp_path / 'archive.tar.gz'
    archive.write_bytes(b'sample')
    wrong = '0' * 64
    actual = subprocess.run(
        ['sha256sum', str(archive)], capture_output=True, text=True
    ).stdout.split()[0]
    prefix = f'source {COMMON}\n' + LOG_STUBS
    assert bash(prefix + f'sha256_matches {archive} {actual}').returncode == 0
    assert bash(prefix + f'sha256_matches {archive} {wrong}').returncode == 1
    assert bash(prefix + f'sha256_matches {archive} ""').returncode == 1


@pytest.mark.skipif(
    BASH is None or shutil.which('gpg') is None, reason='needs bash and gpg'
)
def test_detached_signature_must_come_from_the_pinned_key(tmp_path):
    homes = []

    def key(name):
        # a short home: the agent socket path has a length limit
        home = tempfile.mkdtemp(prefix='g')
        homes.append(home)
        gpg = ['gpg', '--batch', '--homedir', str(home)]
        subprocess.run(
            gpg
            + [
                '--pinentry-mode',
                'loopback',
                '--passphrase',
                '',
                '--quick-gen-key',
                f'{name} <{name}@example.invalid>',
                'ed25519',
                'sign',
                '1d',
            ],
            check=True,
            capture_output=True,
        )
        listing = subprocess.run(
            gpg + ['--with-colons', '--list-keys'], capture_output=True, text=True
        ).stdout.splitlines()
        # the record after the primary key line carries its fingerprint in field 10
        fingerprint = next(
            listing[n + 1].split(':')[9]
            for n, row in enumerate(listing)
            if row.startswith('pub:')
        )
        exported = tmp_path / f'{name}.asc'
        exported.write_bytes(
            subprocess.run(
                gpg + ['--armor', '--export', fingerprint], capture_output=True
            ).stdout
        )
        return gpg, fingerprint, exported

    pinned_gpg, pinned_fingerprint, pinned_key = key('pinned')
    other_gpg, _, _ = key('other')
    payload = tmp_path / 'package.rpm'
    payload.write_bytes(b'sample package')
    prefix = f'source {COMMON}\n' + LOG_STUBS
    for gpg, expected in ((pinned_gpg, 0), (other_gpg, 1)):
        # each signer's signature in its own file: the last check below uses the pinned one
        signature = tmp_path / f'package.rpm.{expected}.sig'
        signature.unlink(missing_ok=True)
        subprocess.run(
            gpg + ['--detach-sign', '-o', str(signature), str(payload)],
            check=True,
            capture_output=True,
        )
        result = bash(
            prefix
            + f'verify_detached_signature {pinned_key} {pinned_fingerprint} {signature} {payload}'
        )
        assert result.returncode == expected, result.stdout + result.stderr
    # a valid signature by the pinned key over different content fails too
    payload.write_bytes(b'changed package')
    result = bash(
        prefix
        + f'verify_detached_signature {pinned_key} {pinned_fingerprint} {tmp_path}/package.rpm.0.sig {payload}'
    )
    assert result.returncode == 1
    for home in homes:
        subprocess.run(
            ['gpgconf', '--homedir', home, '--kill', 'all'], capture_output=True
        )
        shutil.rmtree(home, ignore_errors=True)


@pytest.mark.skipif(BASH is None, reason='bash not installed')
def test_openpbs_builds_only_the_pinned_commit(tmp_path):
    script = render('_templates/linux/openpbs.jinja2', 'amazonlinux2023')
    # the binary modes are run and checked in test_bootstrap_downloads_executed.py
    start = script.index('function install_openpbs_git() {')
    body = script[start : script.index('\n}\n', start) + 3]
    calls = tmp_path / 'calls'
    stubs = f"""
check_openpbs_git_installed() {{ return 1; }}
install_openpbs_dependencies() {{ :; }}
git() {{
  case "$1" in
    clone) command mkdir -p openpbs ;;
    checkout) return 1 ;;
    rev-parse) echo 0000 ;;
  esac
}}
compile_and_install_openpbs() {{ echo compiled >> {calls}; }}
fail_bootstrap() {{ echo "failed $1" >> {calls}; exit 1; }}
pushd() {{ builtin cd "$1"; }}
popd() {{ :; }}
"""
    body = body.replace('/root/bootstrap', str(tmp_path))
    result = bash(LOG_STUBS + stubs + body + 'install_openpbs_git\n')
    assert result.returncode == 1
    assert calls.read_text() == 'failed download-unverified\n'


def test_downloads_run_as_root_are_pinned_or_signed():
    root = os.path.join(IDEA_BOOTSTRAP_DIR, '_templates', 'linux')
    text = {
        name: open(os.path.join(root, name)).read()
        for name in (
            'efs_mount_helper.jinja2',
            'cjson_devel.jinja2',
            'fsx_lustre_client.jinja2',
            'cloudwatch_agent.jinja2',
            'gpu_drivers.jinja2',
            'prometheus.jinja2',
            'prometheus_node_exporter.jinja2',
        )
    }
    assert 'sh.rustup.rs' not in text['efs_mount_helper.jinja2']
    assert 'git checkout v1.' not in text['cjson_devel.jinja2']
    assert 'fsx-lustre-client-repo-public-keys' not in text['fsx_lustre_client.jinja2']
    assert 'verify_detached_signature' in text['cloudwatch_agent.jinja2']
    gpu = text['gpu_drivers.jinja2']
    assert gpu.count('amd_rpms_signed $(ls') == 2
    for name in ('prometheus.jinja2', 'prometheus_node_exporter.jinja2'):
        assert text[name].index('sha256_matches') < text[name].index('tar -xf')


def test_job_scratch_entries_mount_nodev_and_keep_set_id():
    from test_bootstrap_shell_syntax import JOB_PARAMS, job

    for name in ('efa, ht, scratch', 'defaults'):
        script = render(
            'compute-node/_templates/scratch_storage.jinja2',
            'rhel9',
            variables={'job': job(**JOB_PARAMS[name])},
        )
        entries = [line for line in script.splitlines() if '>> /etc/fstab' in line]
        assert entries and all(
            ' ext4 defaults,' in line and ',nodev 0 0' in line for line in entries
        )
        assert 'nosuid' not in script


@pytest.mark.skipif(
    BASH is None or shutil.which('jq') is None, reason='needs bash and jq'
)
@pytest.mark.parametrize(
    'case,member_before,leave_ok,join_rc,join_makes_member,stops',
    [
        # an image's stale membership that cannot be left, and a join that fails: stops
        ('leave_fails_join_fails', True, False, 1, False, True),
        # the join makes the membership and then reports a failure: the membership decides
        ('join_nonzero_member_verified', False, True, 1, True, False),
        ('join_ok', False, True, 0, True, False),
    ],
)
def test_directory_join_outcome_follows_leave_and_membership(
    tmp_path, case, member_before, leave_ok, join_rc, join_makes_member, stops
):
    (tmp_path / 'etc' / 'sssd').mkdir(parents=True)
    (tmp_path / 'etc' / 'sudoers').write_text('')
    (tmp_path / 'etc' / 'nsswitch.conf').write_text('')
    member = tmp_path / 'member'
    if member_before:
        member.write_text('')
    stubs = """
IDEA_CLUSTER_NAME=idea-mock
AWS_REGION=us-east-1
instance_id() { echo i-0123456789abcdef0; }
aws() {
  if [[ "$*" == *get-item* ]]; then
    echo '{"Item": {"status": {"S": "success"}, "otp": {"S": "otp"}, "hostname": {"S": "IDEA-HOST"}, "domain_controller": {"S": "dc.example.invalid"}}}'
  fi
}
exit_fail() { echo "Failed: $1"; exit 1; }
systemctl() { :; }
disable_sssd_responder_sockets() { :; }
sleep() { :; }
export -f aws
"""
    # the leave step needs an executable realm: a stand-in on PATH keeps the state in a file
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    realm = bin_dir / 'realm'
    realm.write_text(
        f"""#!/bin/bash
case "$1" in
  list) [[ -f {member} ]] && echo corp.example.invalid; exit 0 ;;
  leave) [[ {str(leave_ok).lower()} == true ]] && rm -f {member} && exit 0; exit 1 ;;
  join) [[ {str(join_makes_member).lower()} == true ]] && touch {member}; exit {join_rc} ;;
esac
exit 1
"""
    )
    realm.chmod(0o755)
    common = tmp_path / 'common.sh'
    common.write_text(open(COMMON).read().replace('/root/', f'{tmp_path}/'))
    result = bash(
        f'source {common}\n' + LOG_STUBS + stubs + ad_join(tmp_path),
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
    )
    stopped = 'not joined to corp.example.invalid' in result.stdout
    assert stopped == stops, result.stdout[-2000:]
    # a host that proceeds goes on to write its sssd configuration
    assert (tmp_path / 'etc' / 'sssd' / 'sssd.conf').exists() == (not stops)
