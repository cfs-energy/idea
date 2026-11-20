"""
Run the rendered Linux bootstrap steps the review panel named, in a sandbox with the system
tools stubbed, and check the outcome each fix promises.
"""

import hashlib
import os

import pytest

from test_bootstrap_downloads_executed import FAILURE, efs_config, function_body
from test_bootstrap_secrets_and_downloads import COMMON, LOG_STUBS, bash
from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR, Config
from test_image_bake_bootstrap import render

pytestmark = pytest.mark.skipif(BASH is None, reason='bash not installed')


def common_with_fstab(tmp_path, fstab_text=''):
    fstab = tmp_path / 'fstab'
    fstab.write_text(fstab_text)
    path = tmp_path / 'common.sh'
    path.write_text(open(COMMON).read().replace('/etc/fstab', str(fstab)))
    return path, fstab


# N-1: the efs-utils build happens only where an EFS share is in scope, and stops the host only
# where a share mounts with the helper.
def efs_helper_run(tmp_path, config):
    text = render('_templates/linux/efs_mount_helper.jinja2', 'rocky9', config=config)
    body = text[text.index('EFS_UTILS_NEEDED=no') :]
    calls = tmp_path / 'calls'
    stubs = f"""
command() {{ [[ "$*" == '-v amazon-efs-mount-watchdog' ]] && return 1; builtin command "$@"; }}
package_transaction() {{ echo "package $*" >> {calls}; }}
curl() {{ echo "curl $*" >> {calls}; return 7; }}
git() {{ echo "git $*" >> {calls}; return 128; }}
uname() {{ echo x86_64; }}
"""
    result = bash(
        f'source {COMMON}\n' + LOG_STUBS + FAILURE + stubs + body, cwd=tmp_path
    )
    return result, (calls.read_text() if calls.exists() else '')


def test_efs_helper_is_not_built_without_an_efs_share(tmp_path):
    config = Config()
    for share in ('apps', 'data'):
        config.put(f'shared-storage.{share}.provider', 'fsx_openzfs')
    result, calls = efs_helper_run(tmp_path, config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'No in-scope EFS share' in result.stdout
    assert calls == ''


def test_efs_helper_build_failure_only_warns_where_shares_mount_as_nfs4(tmp_path):
    result, calls = efs_helper_run(tmp_path, efs_config('nfs4'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'curl ' in calls and 'FAILED' not in result.stdout
    assert 'continuing without the EFS mount helper' in result.stdout


def test_efs_helper_build_failure_stops_a_host_that_mounts_with_it(tmp_path):
    result, _ = efs_helper_run(tmp_path, efs_config('efs'))
    assert result.returncode == 1
    assert (
        'FAILED download-failed: rustup-init 1.29.3 could not be downloaded'
        in result.stdout
    )


# LIN-06: every in-scope share must be mounted, also one that never got an fstab entry.
@pytest.mark.parametrize('deferring', [False, True])
def test_a_share_without_an_fstab_entry_fails_the_mount_check(tmp_path, deferring):
    common, _ = common_with_fstab(tmp_path, 'UUID=1 / xfs defaults 0 0\n')
    stubs = f"""
mount() {{ return 32; }}
mountpoint() {{ return 1; }}
IDEA_SHARED_MOUNT_DIRS=('{tmp_path}/lustre')
IDEA_SHARED_LUSTRE_DIRS=('{tmp_path}/lustre')
"""
    call = (
        'mount_shared_filesystems -t nolustre\n'
        if deferring
        else 'mount_shared_filesystems\n'
    )
    result = bash(f'source {common}\n' + LOG_STUBS + stubs + call)
    # a bake deferring Lustre until its kernel boots does not expect it yet
    assert (result.returncode == 0) == deferring, result.stdout
    if not deferring:
        assert f'Shared storage not mounted: {tmp_path}/lustre' in result.stdout


# LIN-07: every scope named must match.
@pytest.mark.parametrize(
    'scope,queue_profile,expected',
    [
        (['module', 'scheduler:queue-profile'], 'other', False),
        (['module', 'scheduler:queue-profile'], 'compute', True),
        (['module', 'project', 'scheduler:queue-profile'], 'other', False),
        (['module', 'project', 'scheduler:queue-profile'], 'compute', True),
        (['module'], 'other', True),
    ],
)
def test_shared_storage_scope_requires_every_named_scope(
    scope, queue_profile, expected
):
    from ideasdk.context import BootstrapContext

    context = BootstrapContext(
        config=Config(),
        module_name='scheduler',
        module_id='scheduler',
        module_set='default',
        base_os='rhel9',
        instance_type='m7i.large',
    )
    context.vars.project = 'sample-project'
    context.vars.queue_profile = queue_profile
    storage = {
        'scope': scope,
        'modules': ['scheduler'],
        'projects': ['sample-project'],
        'queue_profiles': ['compute'],
    }
    assert context.eval_shared_storage_scope(shared_storage=storage) == expected


# LIN-08: the mount path is a literal, never a pattern.
def test_fstab_entries_are_matched_by_literal_mount_path(tmp_path):
    common, fstab = common_with_fstab(tmp_path)
    script = (
        f'source {common}\n'
        + LOG_STUBS
        + 'add_efs_to_fstab fs-1.example.invalid /data-v1 "nfs4 hard 0 0"\n'
        + 'add_efs_to_fstab fs-2.example.invalid /data.v1 "nfs4 hard 0 0"\n'
        + 'add_efs_to_fstab fs-3.example.invalid /data.v1 "nfs4 soft 0 0"\n'
    )
    assert bash(script).returncode == 0
    assert fstab.read_text().splitlines() == [
        'fs-1.example.invalid:/ /data-v1/ nfs4 hard,nodev 0 0',
        'fs-3.example.invalid:/ /data.v1/ nfs4 soft,nodev 0 0',
    ]


# LIN-09 / ACC-43: stored paths are shell data at every site.
# quote and command substitution, and no space (fstab fields are space separated)
ODD = "/data'$(echo${IFS}MARKER)"


@pytest.mark.parametrize(
    'template',
    [
        '_templates/linux/join_activedirectory.jinja2',
        '_templates/linux/join_openldap.jinja2',
    ],
)
def test_directory_join_reads_the_data_path_as_data(template):
    config = Config()
    config.put('shared-storage.data.mount_dir', ODD)
    config.put('directoryservice.name', 'corp.example.invalid')
    config.put(
        'directoryservice.ad_automation.sqs_queue_url', 'https://sqs.example.invalid/q'
    )
    config.put('directoryservice.sudoers.group_name', 'Admins')
    config.put('directoryservice.tls_certificate_secret_arn', 'arn:example')
    config.put('directoryservice.ldap_base', 'dc=example')
    config.put('directoryservice.hostname', 'ldap.example.invalid')
    text = render(template, 'rhel9', config=config)
    line = next(
        row for row in text.splitlines() if row.startswith('IDEA_CLUSTER_DATA_DIR=')
    )
    assert bash(line + '\nprintf %s "$IDEA_CLUSTER_DATA_DIR"').stdout == ODD


def test_image_scrub_and_builder_cleanup_remove_only_the_literal_path(tmp_path):
    config = Config()
    config.put('shared-storage.data.mount_dir', ODD)
    fstab_text = (
        'UUID=1 / xfs defaults 0 0\n'
        f'fs-1:/ {ODD}/ nfs4 hard 0 0\n'
        'fs-2:/ /dataXv1/ nfs4 hard 0 0\n'
    )
    scrub = render(
        'dcv-host-ami-builder/image_scrub.sh.jinja2', 'rocky9', config=config
    )
    start = scrub.index("IDEA_SCRUB_DIR='/data")
    line = scrub[start : scrub.index('\n', scrub.index('\n', start) + 1)]
    fstab = tmp_path / 'fstab'
    fstab.write_text(fstab_text)
    assert bash(line.replace('/etc/fstab', str(fstab))).returncode == 0
    assert fstab.read_text().splitlines() == [
        'UUID=1 / xfs defaults 0 0',
        'fs-2:/ /dataXv1/ nfs4 hard 0 0',
    ]
    post = render(
        'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
        'rocky9',
        config=config,
    )
    line = next(
        row.strip()
        for row in post.splitlines()
        if 'remove_from_fstab' in row and 'data' in row
    )
    common, fstab = common_with_fstab(tmp_path, fstab_text)
    assert bash(f'source {common}\n{line}\n').returncode == 0
    assert "/data'" not in fstab.read_text() and '/dataXv1/' in fstab.read_text()


def test_desktop_scratch_service_script_keeps_the_path_literal():
    config = Config()
    config.put(
        'virtual-desktop-controller.instance_storage.mount_point',
        "/scratch'$(printf MARKER)",
    )
    text = render('_templates/linux/instance_storage.jinja2', 'rhel9', config=config)
    start = text.index("restore-scratch-ownership.sh << 'EOF'\n")
    service = text[text.index('\n', start) + 1 : text.index('\nEOF\n', start)]
    line = next(
        row for row in service.splitlines() if row.startswith('SCRATCH_MOUNTPOINT=')
    )
    assert (
        bash(line + '\nprintf %s "$SCRATCH_MOUNTPOINT"').stdout
        == "/scratch'$(printf MARKER)"
    )


# LIN-12 / R4-32: kernel selection writes the carried FSx key, it does not download one.
@pytest.mark.parametrize('base_os', ['ubuntu2204', 'ubuntu2404', 'ubuntu2604'])
def test_ubuntu_kernel_selection_uses_the_carried_fsx_key(base_os):
    text = render('_templates/linux/set_kernel.jinja2', base_os)
    assert 'fsx-lustre-client-repo-public-keys' not in text
    body = function_body(text, 'fsx_lustre_key')
    carried = open(
        os.path.join(IDEA_BOOTSTRAP_DIR, '_templates/linux/FSX-LUSTRE-UBUNTU-KEY')
    ).read()
    assert (
        bash(body + 'fsx_lustre_key fsx-ubuntu-public-key').stdout.strip()
        == carried.strip()
    )
    key_line = text.index(
        'fsx_lustre_key fsx-ubuntu-public-key | idea_gpg "" --dearmor'
    )
    assert text.index('function fsx_lustre_key') < key_line


# LIN-20, LIN-23: the GRID driver path.
def test_grid_install_stops_when_no_installer_was_copied(tmp_path):
    text = render(
        '_templates/linux/gpu_drivers.jinja2', 'rhel9', instance_type='g5.xlarge'
    )
    body = function_body(text, 'stop_root_x_server') + function_body(
        text, 'install_nvidia_grid_drivers'
    )
    body = body.replace('/root/bootstrap', str(tmp_path))
    record = tmp_path / 'ran'
    stubs = f"""
aws() {{ return 1; }}
curl() {{ :; }}
sh() {{ echo "installer $*" >> {record}; }}
"""
    result = bash(
        LOG_STUBS
        + stubs
        + body.replace('/bin/sh ', 'sh ')
        + 'install_nvidia_grid_drivers\n'
    )
    assert result.returncode == 1
    assert 'No NVIDIA GRID installer could be copied' in result.stdout
    assert not record.exists()


@pytest.mark.parametrize(
    'owner,comm,signalled',
    [(0, 'Xorg', True), (1000, 'Xorg', False), (0, 'bash', False)],
)
def test_only_a_root_owned_x_server_lock_is_signalled(tmp_path, owner, comm, signalled):
    text = render(
        '_templates/linux/gpu_drivers.jinja2', 'rhel9', instance_type='g5.xlarge'
    )
    body = function_body(text, 'stop_root_x_server')
    (tmp_path / 'proc/4242').mkdir(parents=True)
    (tmp_path / 'proc/4242/comm').write_text(comm + '\n')
    lock = tmp_path / 'X0-lock'
    lock.write_text('      4242\n')
    body = body.replace('/tmp/.X0-lock', str(lock)).replace(
        '/proc/', f'{tmp_path}/proc/'
    )
    stubs = f"""
stat() {{ echo {owner}; }}
kill() {{ echo "kill $*" >> {tmp_path}/killed; }}
"""
    assert bash(stubs + body + 'stop_root_x_server').returncode == 0
    assert (tmp_path / 'killed').exists() == signalled


# LIN-25 / ACC-42: a host that is not a domain member stops.
@pytest.mark.parametrize(
    'joined,member',
    [
        (True, True),
        (False, False),
        (False, True),
    ],  # last: an earlier membership the image kept
)
def test_a_failed_directory_join_stops_the_host(tmp_path, joined, member):
    config = Config()
    config.put('directoryservice.name', 'corp.example.invalid')
    config.put(
        'directoryservice.ad_automation.sqs_queue_url', 'https://sqs.example.invalid/q'
    )
    config.put('directoryservice.sudoers.group_name', 'Admins')
    text = render(
        '_templates/linux/join_activedirectory.jinja2', 'rhel9', config=config
    )
    start = text.index(
        'if [[ "${AD_LEAVE_FAILED}" == true && "${AD_JOIN_SUCCEEDED}" != true ]]'
    )
    block = text[start : text.index('\nfi\n', start) + 4]
    stubs = f"""
AD_DOMAIN_NAME=corp.example.invalid
AD_JOIN_SUCCEEDED={str(joined).lower()}
# the image's membership could not be left: only this run's join counts
AD_LEAVE_FAILED=true
realm() {{ [[ "$1" == list && {str(member).lower()} == true ]] && echo corp.example.invalid; }}
exit_fail() {{ echo "Failed: $1"; exit 1; }}
"""
    result = bash(
        stubs + block.replace('$(command -v realm)', 'realm') + 'echo CONTINUED\n'
    )
    assert ('CONTINUED' in result.stdout) == joined
    if not joined:
        assert 'not joined to corp.example.invalid' in result.stdout


# LIN-26: callers capture only the secret.
def test_get_secret_prints_only_the_secret_after_a_retry(tmp_path):
    count = tmp_path / 'count'
    stubs = f"""
which() {{ echo aws; }}
aws() {{ [[ -f {count} ]] || {{ touch {count}; return 255; }}; echo -n MARKER-SECRET; }}
sleep() {{ :; }}
AWS_DEFAULT_REGION=us-east-1
"""
    result = bash(
        f'source {COMMON}\n'
        + stubs
        + 'value=$(get_secret arn:example); printf %s "$value"'
    )
    assert result.stdout == 'MARKER-SECRET'
    assert 'not ready yet' in result.stderr


# LIN-01 / SCH-12 / R4-31 / LIN-02: only blank disks become scratch.
DISKS = {
    # name: (model, size, file system)
    'nvme0n1': ('Amazon Elastic Block Store', 8, ''),
    'nvme1n1': ('Amazon Elastic Block Store', 100, 'ext4'),
    'nvme2n1': ('Amazon EC2 NVMe Instance Storage', 100, ''),
    'nvme3n1': ('Amazon Elastic Block Store', 100, ''),
    # an EBS disk of the requested size that blkid cannot read: not blank
    'nvme4n1': ('Amazon Elastic Block Store', 100, ''),
}


def disk_stubs(tmp_path):
    calls = tmp_path / 'calls'
    cases_model = ' '.join(f'/dev/{n}) echo "{m}" ;;' for n, (m, _, _) in DISKS.items())
    cases_fs = ' '.join(f'/dev/{n}) echo "{f}" ;;' for n, (_, _, f) in DISKS.items())
    cases_size = ' '.join(
        f'/dev/{n}) echo {s * 1024**3} ;;' for n, (_, s, _) in DISKS.items()
    )
    signed = ' '.join(f'/dev/{n}' for n, (_, _, f) in DISKS.items() if f)
    inconclusive = '/dev/nvme4n1'
    return (
        calls,
        f"""
lsblk() {{
  local dev="${{@: -1}}"
  case "$*" in
    "-dn -o NAME,TYPE") printf '%s disk\\n' {' '.join(DISKS)} ;;
    *MODEL*) case "${{@: -1}}" in {cases_model} esac ;;
    *FSTYPE*) case "${{@: -1}}" in {cases_fs} esac ;;
    *SIZE*) case "$2" in {cases_size} esac ;;
    *NAME*) echo disk ;;
  esac
}}
blkid() {{
  [[ " {signed} " == *" ${{@: -1}} "* ]] && return 0
  [[ " {inconclusive} " == *" ${{@: -1}} "* ]] && return 4
  return 2
}}
findmnt() {{ return 1; }}
mkfs() {{ echo "mkfs ${{@: -1}}" >> {calls}; }}
dd() {{ echo "dd $*" >> {calls}; }}
mdadm() {{ echo "mdadm $*" >> {calls}; }}
mkdir() {{ :; }}
""",
    )


def test_free_instance_store_devices_takes_only_blank_instance_store(tmp_path):
    _, stubs = disk_stubs(tmp_path)
    body = function_body(open(COMMON).read(), 'block_device_blank') + function_body(
        open(COMMON).read(), 'free_instance_store_devices'
    )
    # the stubbed disks are not real block devices: the test answers -b for them
    body = body.replace('[[ -b "${dev}" ]]', '[[ "${dev}" == /dev/nvme[0-4]n1 ]]')
    result = bash(
        stubs
        + body.replace(
            '/dev/nvme[0-9]*n1 /dev/xvdc[a-z]',
            '/dev/nvme0n1 /dev/nvme1n1 /dev/nvme2n1 /dev/nvme3n1 /dev/nvme4n1',
        )
        + 'free_instance_store_devices'
    )
    assert result.stdout.split() == ['/dev/nvme2n1']


def test_ebs_scratch_formats_one_blank_disk_of_the_requested_size(tmp_path):
    from ideadatamodel import SocaMemory, SocaMemoryUnit
    from test_bootstrap_shell_syntax import job

    text = render(
        'compute-node/_templates/scratch_storage.jinja2',
        'rhel9',
        variables={
            'job': job(
                scratch_storage_size=SocaMemory(value=100, unit=SocaMemoryUnit.GB)
            )
        },
    )
    calls, stubs = disk_stubs(tmp_path)
    common, fstab = common_with_fstab(tmp_path)
    helpers = open(common).read()
    helpers = helpers.replace(
        '[[ -b "${dev}" ]] || return 1',
        '[[ "${dev}" == /dev/nvme[0-4]n1 ]] || return 1',
    )
    (tmp_path / 'helpers.sh').write_text(helpers)
    body = function_body(text, 'setup_scratch_storage_ebs').replace(
        '/etc/fstab', str(fstab)
    )
    script = (
        f'source {tmp_path}/helpers.sh\nBOOTSTRAP_DIR={tmp_path}\n'
        + LOG_STUBS
        + stubs
        + body
    )
    assert bash(script + 'setup_scratch_storage_ebs\n').returncode == 0
    # the image's 100 GB volume with a file system is left alone; the blank one is taken once
    # the instance store disk of the same size and the disk blkid could not read are left alone
    assert calls.read_text() == 'mkfs /dev/nvme3n1\n'
    assert fstab.read_text().count('/scratch') == 1


# LIN-15: a renamed archive still installs its binary, and one without it fails.
def test_metrics_archive_is_extracted_regardless_of_its_file_name(tmp_path):
    import tarfile

    payload_dir = tmp_path / 'src' / 'prometheus-2.53.5.linux-amd64'
    payload_dir.mkdir(parents=True)
    (payload_dir / 'prometheus').write_text('#!/bin/sh\n')
    archive = tmp_path / 'archive.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(payload_dir, arcname=payload_dir.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    prefix = 'global-settings.package_config.prometheus.installer.linux'
    config = Config()
    config.put(f'{prefix}.x86_64', 'https://mirror.example.invalid/m/renamed.tar.gz')
    config.put(
        f'{prefix}.aarch64', 'https://mirror.example.invalid/m/renamed-arm.tar.gz'
    )
    config.put(f'{prefix}.x86_64_sha256', digest)
    text = render(
        '_templates/linux/prometheus.jinja2',
        'rhel9',
        config=config,
        variables={'additional_scrape_configs': None},
    )
    body = text[
        text.index('PROMETHEUS_AMD64_URL=') : text.index(
            'function setup_prometheus_service'
        )
    ]
    work = tmp_path / 'work'
    work.mkdir()
    installed = tmp_path / 'bin'
    installed.mkdir()
    stubs = f"""
uname() {{ echo x86_64; }}
wget() {{ command cp {archive} "${{1##*/}}"; }}
pushd() {{ builtin cd {work}; }}
"""
    result = bash(
        f'source {COMMON}\n'
        + LOG_STUBS
        + FAILURE
        + stubs
        + body.replace('/usr/local/bin/', f'{installed}/').replace(
            'mkdir -p ${PROMETHEUS_DIR}', ':'
        )
        + 'install_prometheus\n'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (installed / 'prometheus').exists()


# LIN-17: the binary modes are set on the reuse path too.
@pytest.mark.parametrize(
    'base_os,function',
    [('rhel8', 'install_openpbs_release'), ('rhel9', 'install_openpbs_git')],
)
def test_openpbs_modes_are_set_when_the_image_already_has_it(
    tmp_path, base_os, function
):
    text = render('_templates/linux/openpbs.jinja2', base_os)
    body = function_body(
        text.replace(
            'function set_openpbs_modes() {', 'function set_openpbs_modes () {'
        ),
        'set_openpbs_modes',
    )
    start = text.index(f'function {function}() {{')
    body += text[start : text.index('\n}\n', start) + 3]
    stubs = f"""
check_openpbs_installed() {{ return 0; }}
check_openpbs_git_installed() {{ return 0; }}
chmod() {{ echo "chmod $*" >> {tmp_path}/modes; }}
"""
    assert bash(LOG_STUBS + stubs + body + f'{function}\n').returncode == 0
    assert (
        (tmp_path / 'modes').read_text()
        == 'chmod 4755 /opt/pbs/sbin/pbs_iff\nchmod 0755 /opt/pbs/sbin/pbs_rcp\n'
    )


# LIN-19: the GovCloud bucket region probe has a time limit and a fallback.
def test_govcloud_package_download_falls_back_to_the_instance_region(tmp_path):
    from ideasdk.bootstrap import BootstrapUserDataBuilder

    text = BootstrapUserDataBuilder(
        base_os='rhel9',
        aws_region='us-gov-east-1',
        bootstrap_package_uri='s3://bucket/bootstrap.tar.gz',
        install_commands=['true'],
        substitution_support=False,
    ).build()
    start = text.index('echo "#!/bin/bash')
    helper = text[start : text.index('> /root/bootstrap/download_bootstrap.sh', start)]
    script = bash(helper).stdout.replace('/root/bootstrap/', f'{tmp_path}/')
    stubs = f"""
curl() {{ [[ "$*" == *--max-time* ]] || echo "unbounded $*" >> {tmp_path}/calls; case "$*" in *api/token*) echo t ;; *placement/region*) echo us-gov-east-1 ;; esac; }}
aws() {{ echo "aws $*" >> {tmp_path}/calls; return 1; }}
"""
    (tmp_path / 'helper.sh').write_text(
        script.replace('#!/bin/bash\nset -e\n', '#!/bin/bash\nset -e\n' + stubs)
    )
    bash(f'bash {tmp_path}/helper.sh s3://bucket/bootstrap.tar.gz')
    calls = (tmp_path / 'calls').read_text()
    assert 'aws --region us-gov-east-1 s3 cp' in calls
    # the probe of the us-gov-west-1 endpoint has a time limit
    assert not [
        line
        for line in calls.splitlines()
        if line.startswith('unbounded') and 'us-gov-west-1' in line
    ]


# U11 / ACC-26 / LIN-29: the SSH rule counts only when it is active and sshd uses PAM.
@pytest.mark.parametrize(
    'pam_text,usepam,failed',
    [
        (
            '#account    requisite    pam_exec.so quiet /usr/local/libexec/idea-ssh-access\naccount include password-auth\n',
            'yes',
            False,
        ),
        ('account include password-auth\n', 'no', True),
    ],
)
def test_ssh_access_rule_is_active_and_consulted(tmp_path, pam_text, usepam, failed):
    from jinja2 import Environment, FileSystemLoader

    text = (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template('_templates/linux/ssh_access.jinja2')
        .render(ssh_allowed_users=['sample-user'])
    )
    pam = tmp_path / 'sshd'
    pam.write_text(pam_text)
    script = (
        text.replace('/etc/pam.d/sshd', str(pam))
        .replace('/etc/idea', str(tmp_path / 'idea'))
        .replace('/usr/local/libexec', str(tmp_path / 'libexec'))
    )
    # the rule names its installed path; the check looks for that exact text
    script = script.replace(
        f'{tmp_path}/libexec/idea-ssh-access[[:space:]]*$',
        '/usr/local/libexec/idea-ssh-access[[:space:]]*$',
    )
    script = script.replace(
        f'pam_exec.so quiet {tmp_path}/libexec/idea-ssh-access',
        'pam_exec.so quiet /usr/local/libexec/idea-ssh-access',
    )
    stubs = f'sshd() {{ echo "usepam {usepam}"; }}\n'
    result = bash(
        LOG_STUBS + stubs + script + '\necho "FAILED=${IDEA_SSH_ACCESS_FAILED:-}"'
    )
    assert result.returncode == 0, result.stderr
    assert ('FAILED=1' in result.stdout) == failed
    active = [
        row
        for row in pam.read_text().splitlines()
        if row.startswith(
            'account    requisite    pam_exec.so quiet /usr/local/libexec/idea-ssh-access'
        )
    ]
    assert len(active) == 1
