"""
Run the rendered steps behind the last verified bootstrap findings in a sandbox with the system
tools stubbed. The desktop refresh templates also run under dash, the shell that runs the
refresh on Ubuntu.
"""

import json
import os
import shutil
import subprocess

import pytest

from test_bootstrap_downloads_executed import FAILURE
from test_bootstrap_secrets_and_downloads import COMMON, LOG_STUBS, bash
from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR, Config, job
from test_image_bake_bootstrap import render

pytestmark = pytest.mark.skipif(BASH is None, reason='bash not installed')
DASH = shutil.which('dash')


def compute_post_reboot(**params):
    return render(
        'compute-node/compute_node_post_reboot.sh.jinja2',
        'rhel9',
        variables={'job': job(**params), 'job_directory': '/apps/jobs/1'},
    )


# BC-10: an existing FSx (named by the job or its queue) must mount, or the node fails.
def existing_fsx_check(tmp_path, mounted_after):
    from ideadatamodel import SocaFSxLustreConfig

    text = compute_post_reboot(
        fsx_lustre=SocaFSxLustreConfig(enabled=True, existing_fsx='fs-0123')
    )
    start = text.index('# Begin: Existing FSx Check')
    block = text[start : text.index('# End: Existing FSx Check', start)]
    stubs = f"""
findmnt() {{ [[ -f {tmp_path}/mounted ]]; }}
mount() {{
  # the mount runs in a command substitution: count in a file
  echo "mount $*" >> {tmp_path}/calls
  local count=$(grep -c '^mount' {tmp_path}/calls)
  if [[ {mounted_after} -gt 0 && $count -ge {mounted_after} ]]; then touch {tmp_path}/mounted; return 0; fi
  echo "mount.lustre: mount fs-0123@tcp:/fsx at /fsx failed: No route to host"; return 32
}}
sleep() {{ echo "sleep $*" >> {tmp_path}/calls; }}
"""
    return bash(LOG_STUBS + FAILURE + stubs + block + 'echo SCHEDULER_STARTED\n')


def test_existing_fsx_that_mounts_after_retries_lets_the_node_continue(tmp_path):
    result = existing_fsx_check(tmp_path, mounted_after=3)
    assert result.returncode == 0 and 'SCHEDULER_STARTED' in result.stdout, (
        result.stdout
    )
    assert (tmp_path / 'calls').read_text().count('mount /fsx') == 3


def test_existing_fsx_that_never_mounts_fails_the_node(tmp_path):
    result = existing_fsx_check(tmp_path, mounted_after=0)
    assert result.returncode == 1 and 'SCHEDULER_STARTED' not in result.stdout
    assert (
        'FAILED existing-fsx-unavailable: FSx for Lustre fs-0123 did not mount at /fsx after 6 attempts'
        in result.stdout
    )
    assert 'No route to host' in result.stdout
    calls = (tmp_path / 'calls').read_text()
    assert calls.count('mount /fsx') == 6 and calls.count('sleep 30') == 5


def test_a_job_without_an_existing_fsx_has_no_mount_check():
    assert '# Begin: Existing FSx Check' not in compute_post_reboot()


# BC-13: the scheduler client is enabled, so a baked node rejoins after a reboot.
def test_scheduler_start_enables_pbs_before_starting_it(tmp_path):
    config = Config()
    config.put('scheduler.provider', 'openpbs')
    text = render(
        'compute-node/_templates/scheduler_start.jinja2', 'rhel9', config=config
    )
    stub = f'systemctl() {{ echo "$*" >> {tmp_path}/calls; }}\n'
    assert bash(stub + text).returncode == 0
    assert (tmp_path / 'calls').read_text() == 'enable pbs\nstart pbs\n'


# BC-01 and BC-12: the compute bake cleanup and its continuation after a reboot.
def builder_post_reboot():
    return render(
        'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
        'rocky9',
    )


def test_bake_cleanup_never_enters_a_mounted_share(tmp_path):
    from test_image_bake_bootstrap import bake_cleanup_segment

    text = builder_post_reboot()
    roots = {
        name: str(tmp_path / name.strip('/').replace('/', '-'))
        for name in ('/tmp', '/var/tmp', '/var/crash')
    }
    for path in roots.values():
        os.makedirs(path)
    tmp = tmp_path / 'tmp'
    (tmp / 'share').mkdir(parents=True)
    (tmp / 'share' / 'sentinel').write_text('shared data')
    (tmp / 'scratch-file').write_text('temporary')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'mountpoint').write_text(f'#!/bin/sh\n[ "$2" = "{tmp}/share" ]\n')
    (bin_dir / 'mountpoint').chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
    assert (
        subprocess.run(
            [BASH, '-c', bake_cleanup_segment(text, roots)], env=env
        ).returncode
        == 0
    )
    assert (tmp / 'share' / 'sentinel').exists()
    assert not (tmp / 'scratch-file').exists()


def test_bake_registers_its_continuation_before_a_driver_reboot(tmp_path):
    text = builder_post_reboot()
    start = text.index('if [[ "${REBOOT_REQUIRED}" == "yes" ]]; then')
    block = text[start : text.index('\nelse\n', start)] + '\nfi\n'
    stubs = f"""
REBOOT_REQUIRED=yes
SCRIPT_DIR=/root/bootstrap/latest/compute-node-ami-builder
IDEA_COMPUTE_NODE_AMI_BUILDER_LOGS_DIR=/apps/logs
crontab() {{ if [[ "$1" == -l ]]; then cat {tmp_path}/cron 2>/dev/null; else cat > {tmp_path}/cron; fi; }}
reboot_after_cloud_init() {{ echo REBOOT; }}
"""
    for _ in range(2):  # registering twice leaves one entry
        bash(stubs + block)
    cron = (tmp_path / 'cron').read_text()
    assert (
        cron.count(
            '@reboot /bin/bash /root/bootstrap/latest/compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh'
        )
        == 1
    )


# BL-03: a desktop from a baked image mounts its instance store now and hands it to the owner.
def test_desktop_scratch_is_mounted_and_given_to_the_owner_after_the_join():
    text = render(
        'virtual-desktop-host-linux/setup.sh.jinja2',
        'rhel9',
        instance_type='m6id.large',
        variables={'session_owner': 'sample-user', 'project': 'sample-project'},
    )
    join = text.index('# Begin: Join')
    restore = text.index('/opt/idea/.services/restore-scratch-ownership.sh\nfi')
    assert join < restore < text.index('configure_dcv_host.sh')
    storage = text[text.index('function setup_instance_storage') :]
    assert 'mount "${SCRATCH_MOUNTPOINT}" || true' in storage


def test_desktop_scratch_mount_runs_right_after_its_fstab_line(tmp_path):
    common = tmp_path / 'common.sh'
    fstab = tmp_path / 'fstab'
    fstab.write_text('')
    common.write_text(open(COMMON).read().replace('/etc/fstab', str(fstab)))
    text = render('_templates/linux/instance_storage.jinja2', 'rhel9')
    start = text.index('function setup_instance_storage() {')
    body = text[start : text.index('\n}\n', start) + 3].replace(
        '/etc/fstab', str(fstab)
    )
    body = body.replace(
        "SCRATCH_MOUNTPOINT='/scratch'", f"SCRATCH_MOUNTPOINT='{tmp_path}/scratch'"
    )
    body = body.replace('/opt/idea/.services', f'{tmp_path}/services').replace(
        '/etc/systemd/system', f'{tmp_path}/systemd'
    )
    stubs = f"""
free_instance_store_devices() {{ echo /dev/nvme1n1; }}
mkfs() {{ :; }}
mount() {{ echo "mount $*" >> {tmp_path}/calls; touch {tmp_path}/mounted; }}
findmnt() {{ [[ -f {tmp_path}/mounted ]] && echo /dev/nvme1n1; }}
chown() {{ :; }}
systemctl() {{ :; }}
IDEA_SESSION_OWNER=
"""
    result = bash(
        f'source {common}\n' + LOG_STUBS + stubs + body + 'setup_instance_storage\n'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'calls').read_text() == f'mount {tmp_path}/scratch\n'
    assert 'is not mounted' not in result.stdout


# BL-26: the owner rule is written before the join starts SSSD, and again after it. The rendered
# segment (rule, OpenLDAP join, rule) runs with system paths in a sandbox: SSSD starting records
# whether the rule is active, and the join's PAM tooling rewrites the sshd stack without it.
@pytest.mark.parametrize('rewrites_sshd', [False, True])
@pytest.mark.parametrize('host', ['desktop', 'compute'])
@pytest.mark.parametrize('base_os', ['rhel9', 'ubuntu2404'])
def test_ssh_owner_rule_is_active_when_sssd_starts_and_after_the_join(
    tmp_path, host, base_os, rewrites_sshd
):
    config = Config()
    config.put('directoryservice.provider', 'openldap')
    config.put('directoryservice.tls_certificate_secret_arn', 'arn:example')
    config.put('directoryservice.ldap_base', 'dc=example')
    config.put('directoryservice.hostname', 'ldap.example.invalid')
    config.put('directoryservice.name', 'example.invalid')
    if host == 'desktop':
        text = render(
            'virtual-desktop-host-linux/setup.sh.jinja2',
            base_os,
            config=config,
            variables={'session_owner': 'sample-user', 'project': 'sample-project'},
        )
    else:
        text = render(
            'compute-node/compute_node.sh.jinja2',
            base_os,
            config=config,
            variables={
                'job': job(),
                'job_directory': '/apps/jobs/1',
                'queue_profile': 'compute',
                'project': 'p',
                'motd_messages': [],
            },
        )
    start = text.index('# Begin: SSH access')
    second = text.index('# Begin: SSH access', text.index('# Begin: Join', start))
    segment = text[
        start : text.index('# End: SSH access', second) + len('# End: SSH access')
    ]
    root = tmp_path / 'root'
    for directory in (
        'etc/pam.d',
        'etc/sssd',
        'etc/openldap',
        'etc/idea',
        'usr/local/libexec',
    ):
        (root / directory).mkdir(parents=True)
    pam = root / 'etc/pam.d/sshd'
    pam.write_text('account include password-auth\n')
    (root / 'etc/nsswitch.conf').write_text('')
    segment = segment.replace('/etc/', f'{root}/etc/').replace(
        '/usr/local/libexec', f'{root}/usr/local/libexec'
    )
    # the rule names its installed path; restore that text after the path rewrite
    segment = segment.replace(
        f'{root}/usr/local/libexec/idea-ssh-access[[:space:]]',
        '/usr/local/libexec/idea-ssh-access[[:space:]]',
    )
    segment = segment.replace(
        f'pam_exec.so quiet {root}/usr/local/libexec/idea-ssh-access',
        'pam_exec.so quiet /usr/local/libexec/idea-ssh-access',
    )
    events = tmp_path / 'events'
    stubs = f"""
get_secret() {{ echo -n certificate; }}
exit_fail() {{ echo "Failed: $1"; exit 1; }}
sshd() {{ echo 'usepam yes'; }}
openssl() {{ :; }}
# the join's PAM tooling rewrites the sshd stack from its own template
# authselect and pam-auth-update rewrite the shared stacks, not sshd's own file; the second
# case is a site tool that does rewrite it
rewrite_pam() {{ [[ {str(rewrites_sshd).lower()} == false ]] || echo 'account include password-auth' > {pam}; }}
authconfig() {{ rewrite_pam; }}
authselect() {{ rewrite_pam; }}
pam-auth-update() {{ rewrite_pam; }}
systemctl() {{
  if [[ "$*" == *sssd* && "$1" != stop ]]; then
    if grep -q 'pam_exec.so quiet /usr/local/libexec/idea-ssh-access' {pam}; then echo active >> {events}; else echo missing >> {events}; fi
  fi
}}
"""
    result = bash(LOG_STUBS + stubs + segment)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = events.read_text().split()
    assert recorded, 'sssd never started'
    if not rewrites_sshd:
        # no directory user can log in before the rule: it is active at every sssd start
        assert set(recorded) == {'active'}, recorded
    # whatever the join did to the file, the rule is active once the block is done
    assert 'pam_exec.so quiet /usr/local/libexec/idea-ssh-access' in pam.read_text()


# BL-31: an absent owner address adds no tag.
def test_compute_tags_carry_no_null_email(tmp_path):
    text = render(
        'compute-node/compute_node.sh.jinja2',
        'rhel9',
        variables={
            'job': job().copy(update={'owner_email': None}),
            'job_directory': '/apps/jobs/1',
            'queue_profile': 'compute',
            'project': 'p',
            'motd_messages': [],
        },
    )
    start = text.index('cat > "${TAGS}" <<\'IDEA_EBS_VOLUME_TAGS\'\n') + len(
        'cat > "${TAGS}" <<\'IDEA_EBS_VOLUME_TAGS\'\n'
    )
    tags = json.loads(text[start : text.index('\nIDEA_EBS_VOLUME_TAGS', start)])
    assert all(tag['Value'] is not None for tag in tags)
    assert 'idea:JobOwnerEmail' not in [tag['Key'] for tag in tags]


# BC-28 and U03: job values reach /etc/environment as data, read back unchanged when sourced.
def test_compute_setup_writes_job_values_as_data(tmp_path):
    odd = 'sample \'job\' "$(touch MARKER)" `x` $HOME'
    text = render(
        'compute-node/setup.sh.jinja2',
        'rhel9',
        variables={
            'job': job().copy(update={'name': odd, 'job_group': "grp'$(touch MARKER)"}),
            'job_directory': '/apps/jobs/1',
        },
    )
    start = text.index("printf 'IDEA_JOB_UID=%q")
    block = text[
        start : text.index('>> /etc/environment', start) + len('>> /etc/environment')
    ]
    environment = tmp_path / 'environment'
    block = block.replace('/etc/environment', str(environment))
    result = bash(
        block
        + f'\nsource {environment}\nprintf "%s|%s" "$IDEA_JOB_NAME" "$IDEA_JOB_GROUP"',
        cwd=tmp_path,
    )
    assert result.stdout == f"{odd}|grp'$(touch MARKER)"
    assert not (tmp_path / 'MARKER').exists()


# BL-39: an unreadable settings file is left alone.
def test_claude_code_settings_leaves_an_unparsable_file_alone(tmp_path):
    text = render(
        '_templates/linux/claude_code_settings.jinja2',
        'rhel9',
        variables={
            'bedrock_env': {'AWS_REGION': 'us-east-1'},
            'claude_code_output_style': 'default',
            'claude_code_permission_mode': 'default',
            'auto_mode_environment': [],
        },
    )
    start = text.index("<<'IDEA_CLAUDE_SEED'\n") + len("<<'IDEA_CLAUDE_SEED'\n")
    program = text[start : text.index('\nIDEA_CLAUDE_SEED\n', start)]
    defaults = tmp_path / 'defaults.json'
    defaults.write_text(json.dumps({'outputStyle': 'concise'}))
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    settings = home / '.claude' / 'settings.json'
    settings.write_text('{ not json')
    script = tmp_path / 'seed.py'
    script.write_text(
        program.replace('/etc/idea/claude-code-defaults.json', str(defaults))
    )
    subprocess.run(
        ['python3', '-I', str(script)],
        env={**os.environ, 'HOME': str(home)},
        check=True,
    )
    assert settings.read_text() == '{ not json'


# BL-38: a stopped service's marker stays until it starts again.
@pytest.mark.parametrize('starts', [True, False])
def test_stopped_service_marker_survives_a_failed_start(tmp_path, starts):
    config = Config()
    # the re-enable path: hardening switched off after an earlier bootstrap stopped the services
    config.put('global-settings.host_hardening.disable_unused_services', False)
    text = render(
        '_templates/linux/unused_services.jinja2', 'ubuntu2404', config=config
    )
    start = text.index('for idea_unit in postgresql sendmail; do')
    block = text[start : text.index('done\n', start) + 5].replace(
        '/var/lib/idea', str(tmp_path)
    )
    (tmp_path / 'stopped-postgresql').write_text('')
    stub = f'systemctl() {{ {"return 0" if starts else "return 1"}; }}\n'
    assert bash(LOG_STUBS + stub + block).returncode == 0
    assert (tmp_path / 'stopped-postgresql').exists() == (not starts)


# BL-34: rendered configuration is data.
def test_prometheus_and_cloudwatch_configuration_is_written_as_data(tmp_path):
    cloudwatch = open(
        os.path.join(IDEA_BOOTSTRAP_DIR, '_templates/linux/cloudwatch_agent.jinja2')
    ).read()
    assert "<<'IDEA_CLOUDWATCH_AGENT_CONFIG'" in cloudwatch
    assert "echo '{{ context.utils.to_json(cloudwatch_agent_config" not in cloudwatch
    prom = open(
        os.path.join(IDEA_BOOTSTRAP_DIR, '_templates/linux/prometheus.jinja2')
    ).read()
    assert "<<'IDEA_PROMETHEUS_CONFIG'" in prom
    payload = "label: o'brien $(true)"
    script = f"cat > {tmp_path}/out <<'IDEA_PROMETHEUS_CONFIG'\n{payload}\nIDEA_PROMETHEUS_CONFIG\n"
    assert bash(script).returncode == 0
    assert (tmp_path / 'out').read_text() == payload + '\n'


# VDI-25: a retried tag call that then succeeds leaves no failure for an inherited ERR trap.
def test_tag_retry_status_does_not_trip_an_err_trap(tmp_path):
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
        .get_template('_templates/linux/tag_ebs_volumes.jinja2')
        .render(context=context, ebs_volume_tags=[])
    )
    stubs = f"""
trap 'echo "ERR at $LINENO" >> {tmp_path}/errors' ERR
set -o errtrace
instance_id() {{ echo i-1; }}
count=0
aws() {{ case "$*" in *create-tags*) count=$((count + 1)); [[ $count -ge 2 ]] ;; *) echo vol-1 ;; esac; }}
sleep() {{ :; }}
"""
    assert bash(LOG_STUBS + stubs + block).returncode == 0
    assert not (tmp_path / 'errors').exists(), (tmp_path / 'errors').read_text()


# VDI-26: the builder fixture's ready message does not leak into the rendered builder.
def test_ready_message_fixture_is_not_rendered_into_the_builder():
    text = render(
        'dcv-host-ami-builder/dcv_host_ami_builder.sh.jinja2',
        'rhel9',
        variables={'dcv_host_ready_message': 'MARKER-READY-MESSAGE'},
    )
    assert 'MARKER-READY-MESSAGE' not in text


# VDC-29: the refresh templates run under dash.
@pytest.mark.skipif(DASH is None, reason='dash not installed')
def test_openpbs_client_server_runs_under_dash(tmp_path):
    from jinja2 import Environment, FileSystemLoader

    text = (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template('_templates/linux/openpbs_client_server.jinja2')
        .render(
            context={'base_os': 'ubuntu2404'},
            pbs_server_name='scheduler-new',
            private_zone='cluster.example.invalid',
            bootstrap_refresh=True,
        )
    )
    pbs_conf = tmp_path / 'pbs.conf'
    pbs_conf.write_text('PBS_SERVER=scheduler-old\n')
    resolv = tmp_path / 'resolv.conf'
    resolv.write_text('search ec2.internal\n')
    text = (
        text.replace('/etc/pbs.conf', str(pbs_conf))
        .replace('/etc/resolv.conf', str(resolv))
        .replace('/var/spool/pbs', str(tmp_path / 'spool'))
    )
    stubs = f"""
log_info() {{ echo "info $*"; }}
log_warning() {{ echo "warning $*"; }}
log_error() {{ echo "error $*"; }}
systemctl() {{ echo "systemctl $*" >> {tmp_path}/calls; [ "$1" != is-active ] || [ -f {tmp_path}/running ]; }}
lsattr() {{ :; }}
"""
    # pbs restarts and runs: the name is rewritten and the refresh succeeds
    (tmp_path / 'running').write_text('')
    result = subprocess.run([DASH, '-c', stubs + text], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'not found' not in result.stderr
    assert pbs_conf.read_text() == 'PBS_SERVER=scheduler-new\n'
    assert 'search cluster.example.invalid ec2.internal' in resolv.read_text()
    # pbs does not come up: the refresh fails, so the desktop is retried
    (tmp_path / 'running').unlink()
    result = subprocess.run([DASH, '-c', stubs + text], capture_output=True, text=True)
    assert result.returncode == 1 and 'not found' not in result.stderr


@pytest.mark.skipif(DASH is None, reason='dash not installed')
def test_ssh_access_runs_under_dash(tmp_path):
    from jinja2 import Environment, FileSystemLoader

    text = (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template('_templates/linux/ssh_access.jinja2')
        .render(ssh_allowed_users=['sample-user'])
    )
    pam = tmp_path / 'sshd'
    pam.write_text('account include password-auth\n')
    text = (
        text.replace('/etc/pam.d/sshd', str(pam))
        .replace(
            'mkdir -p /etc/idea /usr/local/libexec',
            f'mkdir -p {tmp_path}/idea {tmp_path}/libexec',
        )
        .replace('cat > /etc/idea/', f'cat > {tmp_path}/idea/')
        .replace('cat > /usr/local/libexec/', f'cat > {tmp_path}/libexec/')
        .replace('chmod 0755 /usr/local/libexec/', f'chmod 0755 {tmp_path}/libexec/')
    )
    stubs = 'log_info() { echo "info $*"; }\nlog_warning() { echo "warning $*"; }\nsshd() { echo "usepam no"; }\n'
    result = subprocess.run(
        [DASH, '-c', stubs + text + '\necho "FAILED=${IDEA_SSH_ACCESS_FAILED:-}"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and 'not found' not in result.stderr, result.stderr
    assert 'FAILED=1' in result.stdout and 'UsePAM no' in result.stdout


# Site hooks: enforced only when the checksum setting is configured.
SITE_HOOKS = [
    (
        'compute-node/compute_node_post_reboot.sh.jinja2',
        'scheduler.compute_node_site_hook_sha256',
        'compute_node/userdata_customizations.sh',
    ),
    (
        'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
        'scheduler.ami_builder_site_hook_sha256',
        'ami_builder/userdata_customizations.sh',
    ),
    (
        'dcv-host-ami-builder/dcv_host_ami_builder_post_reboot.sh.jinja2',
        'virtual-desktop-controller.ami_builder_site_hook_sha256',
        'ami_builder/userdata_customizations.sh',
    ),
    (
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
        'virtual-desktop-controller.dcv_host_site_hook_sha256',
        'dcv_host/userdata_customizations.sh',
    ),
]
HOOK = '#!/bin/bash\necho "ran as $(basename "$0")" >> "$RECORD"\n'


def message_key(setting, module_id='vdc'):
    """The key the messages name: the stored row is keyed by the module id, not its name."""
    return f'{module_id}.{setting.split(".", 1)[1]}'


def site_hook_run(
    tmp_path,
    template,
    setting,
    path,
    configured,
    present=True,
    swap=None,
    uncopied=False,
):
    """swap: content written over the shared hook as soon as a digest has been taken;
    uncopied: the hook cannot be copied (root reads any mode, so the failure is stood in)"""
    import hashlib
    import re
    import shlex

    config = Config()
    if configured is not None:
        config.put(setting, configured)
    variables = {
        'job': job(),
        'job_directory': '/apps/jobs/1',
        'session_owner': 'sample-user',
        'project': 'p',
    }
    text = render(template, 'rhel9', config=config, variables=variables)
    if 'if prepare_site_hook ' in text:
        start = text.index('if prepare_site_hook ')
        end = (
            text.index(
                '\n  fi\n' if text[start - 2 : start] == '  ' else '\nfi\n', start
            )
            + 5
        )
        block = text[start:end]
    else:
        # the desktop checks before DCV starts and runs the checked copy at the end
        check_at = text.index('SITE_HOOK_RUN=""\nprepare_site_hook')
        hook_at = text.index('if [[ -n "${SITE_HOOK_RUN}" ]]; then', check_at)
        block = (
            text[check_at : text.index('|| true\n', check_at) + 8]
            + text[hook_at : text.index('\nfi\n', hook_at) + 4]
        )
    home = tmp_path / 'apps'
    hook = home / 'mod' / path
    if present:
        hook.parent.mkdir(parents=True)
        hook.write_text(HOOK)
    block = block.replace(
        '${IDEA_CLUSTER_HOME}/${IDEA_MODULE_ID}/', f'{home}/mod/'
    ).replace('${IDEA_CLUSTER_HOME}/', f'{home}/mod/')
    block = re.sub(
        r'>> \$\{[A-Z_]+\}/[^ ]*userdata_customizations\.log',
        f'>> {tmp_path}/hook.log',
        block,
    )
    block = block.replace(
        '>> ${BOOTSTRAP_DIR}/logs/userdata_customizations.log',
        f'>> {tmp_path}/hook.log',
    )
    common = open(COMMON).read()
    swapper = ''
    if swap is not None:
        # someone who can write the shared directory replaces the hook between reads
        swapper = (
            'sha256sum() { command sha256sum "$@"; printf %s '
            f'{shlex.quote(swap)} > {shlex.quote(str(hook))}; }}\n'
        )
    if uncopied:
        swapper += 'cat() { [[ "${*: -1}" != */userdata_customizations.sh ]] && command cat "$@"; }\n'
    result = bash(
        f"source /dev/stdin <<'COMMON'\n{common}\nCOMMON\n"
        + LOG_STUBS
        + FAILURE
        + swapper
        + f'export RECORD={tmp_path}/ran\n'
        + 'flock() { :; }\n'
        + block
        + '\necho CONTINUED\n'
    )
    digest = hashlib.sha256(HOOK.encode()).hexdigest()
    ran = (tmp_path / 'ran').read_text() if (tmp_path / 'ran').exists() else ''
    return result, digest, ran


@pytest.mark.parametrize('template,setting,path', SITE_HOOKS)
def test_site_hook_runs_its_checked_copy_when_the_digest_matches(
    tmp_path, template, setting, path
):
    import hashlib

    result, digest, ran = site_hook_run(
        tmp_path,
        template,
        setting,
        path,
        hashlib.sha256(HOOK.encode()).hexdigest().upper(),
    )
    assert result.returncode == 0 and 'CONTINUED' in result.stdout, (
        result.stdout + result.stderr
    )
    # what ran is the root-only copy, not the shared file
    assert ran == 'ran as userdata_customizations.sh\n'
    assert f'matches {message_key(setting)}' in result.stdout
    assert 'warning running site hook' not in result.stdout


@pytest.mark.parametrize('template,setting,path', SITE_HOOKS)
def test_site_hook_with_a_different_digest_stops_the_bootstrap(
    tmp_path, template, setting, path
):
    result, digest, ran = site_hook_run(tmp_path, template, setting, path, '0' * 64)
    assert result.returncode == 1 and ran == '' and 'CONTINUED' not in result.stdout
    assert 'FAILED site-hook-mismatch: site hook' in result.stdout
    assert (
        f'has SHA-256 {digest}, not the {message_key(setting)} value' in result.stdout
    )


@pytest.mark.parametrize('template,setting,path', SITE_HOOKS)
def test_site_hook_without_a_digest_runs_as_before_and_names_the_setting(
    tmp_path, template, setting, path
):
    result, digest, ran = site_hook_run(tmp_path, template, setting, path, None)
    assert result.returncode == 0 and 'CONTINUED' in result.stdout, (
        result.stdout + result.stderr
    )
    assert ran == 'ran as userdata_customizations.sh\n'
    assert 'warning running site hook' in result.stdout
    assert f'set {message_key(setting)} to {digest}' in result.stdout


@pytest.mark.parametrize('template,setting,path', SITE_HOOKS)
@pytest.mark.parametrize('pinned', [False, True])
def test_what_runs_is_the_content_whose_digest_was_logged(
    tmp_path, template, setting, path, pinned
):
    import hashlib

    digest = hashlib.sha256(HOOK.encode()).hexdigest()
    result, _, ran = site_hook_run(
        tmp_path,
        template,
        setting,
        path,
        digest if pinned else None,
        swap='#!/bin/bash\necho swapped >> "$RECORD"\n',
    )
    assert result.returncode == 0 and 'CONTINUED' in result.stdout, (
        result.stdout + result.stderr
    )
    assert ran == 'ran as userdata_customizations.sh\n'
    assert digest in result.stdout


@pytest.mark.parametrize('template,setting,path', SITE_HOOKS)
def test_unpinned_site_hook_that_cannot_be_copied_runs_from_its_path_as_before(
    tmp_path, template, setting, path
):
    result, _, ran = site_hook_run(
        tmp_path, template, setting, path, None, uncopied=True
    )
    assert result.returncode == 0 and 'CONTINUED' in result.stdout, (
        result.stdout + result.stderr
    )
    assert f'set {message_key(setting)} to unreadable' in result.stdout
    assert ran == 'ran as userdata_customizations.sh\n'


@pytest.mark.parametrize('template,setting,path', SITE_HOOKS)
def test_absent_site_hook_runs_nothing(tmp_path, template, setting, path):
    result, _, ran = site_hook_run(
        tmp_path, template, setting, path, '0' * 64, present=False
    )
    assert result.returncode == 0 and 'CONTINUED' in result.stdout and ran == ''
    assert 'no userdata customizations' in result.stdout


def test_site_hook_setting_that_is_not_a_digest_stops_the_bootstrap(tmp_path):
    template, setting, path = SITE_HOOKS[0]
    result, _, ran = site_hook_run(
        tmp_path, template, setting, path, "abc'$(touch MARKER)"
    )
    assert result.returncode == 1 and ran == ''
    assert f'{message_key(setting)} is not a SHA-256' in result.stdout
    assert not (tmp_path / 'MARKER').exists()


# B1: the scratch branches read what the scheduler writes. The fixture is the scheduler's own
# output for each kind of job, captured and checked by idea-scheduler/tests/test_scratch_params_capture.py.
SCHEDULER_PARAMS = json.loads(
    open(
        os.path.join(
            os.path.dirname(__file__), 'fixtures', 'scheduler-scratch-params.json'
        )
    ).read()
)


def scheduler_job(case):
    from ideadatamodel import SocaFSxLustreConfig, SocaMemory, SocaMemoryUnit

    params = SCHEDULER_PARAMS[case]
    size = params['scratch_storage_size']
    fsx = dict(params['fsx_lustre'])
    if 'size' in fsx:
        fsx['size'] = SocaMemory(
            value=fsx['size']['value'], unit=SocaMemoryUnit(fsx['size']['unit'])
        )
    return job(
        enable_scratch=params['enable_scratch'],
        scratch_storage_size=SocaMemory(
            value=size['value'], unit=SocaMemoryUnit(size['unit'])
        ),
        fsx_lustre=SocaFSxLustreConfig(**fsx),
    )


@pytest.mark.parametrize(
    'case,branch',
    [
        ('default', 'setup_scratch_storage_instance_store'),
        # an explicit opt-out is the same value the scheduler writes for the default job
        ('explicit_opt_out', 'setup_scratch_storage_instance_store'),
        ('ebs', 'setup_scratch_storage_ebs'),
        ('new_fsx', 'setup_scratch_storage_fsx_for_lustre'),
        ('existing_fsx', 'setup_scratch_storage_fsx_for_lustre'),
    ],
)
def test_scratch_branch_follows_the_scheduler_written_parameters(case, branch):
    text = compute_post_reboot_for(scheduler_job(case))
    start = text.index('# Begin: Scratch Storage')
    scratch = text[start : text.index('# End: Scratch Storage', start)]
    assert f'function {branch} ()' in scratch and f'\n{branch}\n' in scratch
    others = {
        'setup_scratch_storage_instance_store',
        'setup_scratch_storage_ebs',
        'setup_scratch_storage_fsx_for_lustre',
    } - {branch}
    assert not [name for name in others if f'function {name} ()' in scratch]
    # the permissions step opens up the path the branch provisioned; an existing FSx is not the job's
    expected_mount = {'existing_fsx': "''", 'new_fsx': "'/fsx'"}.get(case, "'/scratch'")
    assert f'JOB_SCRATCH_MOUNT={expected_mount}\n' in text
    # only an existing FSx gets the mount check, and only it is added to fstab by name
    assert ('# Begin: Existing FSx Check' in text) == (case == 'existing_fsx')
    assert ("add_fsx_lustre_to_fstab 'fs-0123456789abcdef0" in scratch) == (
        case == 'existing_fsx'
    )


def test_default_job_gets_its_instance_store_as_scratch(tmp_path):
    text = compute_post_reboot_for(scheduler_job('default'))
    start = text.index('function setup_scratch_storage_instance_store () {')
    body = text[start : text.index('\n}\n', start) + 3]
    fstab = tmp_path / 'fstab'
    fstab.write_text('')
    common = tmp_path / 'common.sh'
    common.write_text(open(COMMON).read().replace('/etc/fstab', str(fstab)))
    body = body.replace('/etc/fstab', str(fstab)).replace(
        "SCRATCH_MOUNTPOINT='/scratch'", f"SCRATCH_MOUNTPOINT='{tmp_path}/scratch'"
    )
    stubs = f"""
BOOTSTRAP_DIR={tmp_path}
free_instance_store_devices() {{ echo /dev/nvme1n1; }}
findmnt() {{ return 1; }}
mkfs() {{ echo "mkfs $*" >> {tmp_path}/calls; }}
mkdir() {{ :; }}
"""
    result = bash(
        f'source {common}\n'
        + LOG_STUBS
        + stubs
        + body
        + 'setup_scratch_storage_instance_store\n'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'calls').read_text() == 'mkfs -t ext4 /dev/nvme1n1\n'
    assert f'/dev/nvme1n1 {tmp_path}/scratch ext4' in fstab.read_text()


def compute_post_reboot_for(job_value):
    return render(
        'compute-node/compute_node_post_reboot.sh.jinja2',
        'rhel9',
        variables={'job': job_value, 'job_directory': '/apps/jobs/1'},
    )


# S1: a node that must reboot for its Lustre client reboots before the existing-FSx check.
def test_existing_fsx_check_waits_for_a_pending_reboot(tmp_path):
    text = compute_post_reboot_for(scheduler_job('existing_fsx'))
    start = text.index('# Begin: Existing FSx Check')
    block = text[start : text.index('# End: Existing FSx Check', start)]
    reboot = text.index('REBOOT_REQUIRED=$(cat /root/bootstrap/reboot_required.txt)')
    reboot_block = text[reboot : text.index('exit 0', reboot) + 6] + '\nfi\n'
    (tmp_path / 'reboot_required.txt').write_text('yes')
    script = (block + reboot_block).replace(
        '/root/bootstrap/reboot_required.txt', str(tmp_path / 'reboot_required.txt')
    )
    stubs = f"""
findmnt() {{ return 1; }}
mount() {{ echo "mount $*" >> {tmp_path}/calls; return 32; }}
sleep() {{ :; }}
crontab() {{ :; }}
reboot_after_cloud_init() {{ echo REBOOTING; }}
SCRIPT_DIR=/root/bootstrap/latest/compute-node
"""
    result = bash(LOG_STUBS + FAILURE + stubs + script)
    assert result.returncode == 0 and 'REBOOTING' in result.stdout, result.stdout
    assert 'FAILED' not in result.stdout and not (tmp_path / 'calls').exists()


# S2: the wait for a new job FSx follows the scheduler's stack timeout.
@pytest.mark.parametrize('timeout,polls', [(1800, 30), (300, 10), (3600, 60)])
def test_new_fsx_wait_follows_the_stack_timeout(tmp_path, timeout, polls):
    config = Config()
    config.put('scheduler.job_provisioning.stack_provisioning_timeout_seconds', timeout)
    text = render(
        'compute-node/_templates/scratch_storage.jinja2',
        'rhel9',
        config=config,
        variables={'job': scheduler_job('new_fsx')},
    )
    assert f'local FSX_WAIT_POLLS={polls}\n' in text
    start = text.index('function setup_scratch_storage_fsx_for_lustre () {')
    body = text[start : text.index('\n}\n', start) + 3]
    stubs = f"""
aws() {{ case "$2" in describe-file-systems) echo CREATING ;; *) echo arn:aws:fsx:r:1:file-system/fs-1 ;; esac; }}
sleep() {{ echo slept >> {tmp_path}/sleeps; }}
"""
    result = bash(
        f'BOOTSTRAP_DIR={tmp_path}\n'
        + LOG_STUBS
        + FAILURE
        + stubs
        + body.replace("FSX_MOUNTPOINT='/fsx'", f"FSX_MOUNTPOINT='{tmp_path}/fsx'")
        + 'setup_scratch_storage_fsx_for_lustre\n'
    )
    assert (
        result.returncode == 1
        and f'not AVAILABLE after {polls} minutes' in result.stdout
    )
    assert (tmp_path / 'sleeps').read_text().count('slept') == polls


# S3: a bake carries efs-utils when the cluster has any EFS share, whatever its scope.
@pytest.mark.parametrize(
    'builder', ['compute-node-ami-builder', 'dcv-host-ami-builder']
)
def test_bake_builds_efs_utils_for_a_project_scoped_efs_share(builder):
    config = Config()
    for share in ('apps', 'data'):
        # the cluster's EFS shares are not in this bake's scope: only the project share is EFS here
        config.put(f'shared-storage.{share}.scope', ['module'])
        config.put(f'shared-storage.{share}.modules', ['bastion-host'])
    config.put(
        'shared-storage.projects-efs',
        {
            'provider': 'efs',
            'scope': ['project'],
            'projects': ['p'],
            'mount_dir': '/projects',
            'mount_options': 'nfs4 nfsvers=4.1 0 0',
            'efs': {'dns': 'fs-1.efs.example.invalid'},
        },
    )
    # rendered with the real context: the shared render helper stubs has_storage_provider
    from jinja2 import Environment, FileSystemLoader
    from ideasdk.context import BootstrapContext
    from ideadatamodel import SocaAnyPayload

    def real_render(name):
        context = BootstrapContext(
            config=config,
            module_name='scheduler',
            module_id='scheduler',
            module_set='default',
            base_os='rocky9',
            instance_type='m7i.large',
        )
        context.vars.ami_dir = '/apps/example/ami'
        context.vars.ami_name = 'sample-image'
        context.vars.session = SocaAnyPayload(type='console')
        context.vars.enabled_drivers = ()
        return (
            Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
            .get_template(name)
            .render(context=context)
        )

    text = real_render(f'{builder}/setup.sh.jinja2')
    start = text.index('EFS_UTILS_NEEDED=no')
    assert (
        'EFS_UTILS_NEEDED=yes'
        in text[start : text.index('if [[ "${EFS_UTILS_NEEDED}" == yes ]]', start)]
    )
    # a host of another project that is not a bake still skips it
    host = real_render('_templates/linux/efs_mount_helper.jinja2')
    start = host.index('EFS_UTILS_NEEDED=no')
    assert (
        'EFS_UTILS_NEEDED=yes'
        not in host[start : host.index('if [[ "${EFS_UTILS_NEEDED}" == yes ]]', start)]
    )


# Contract S5: a pinned value with surrounding whitespace or upper case still matches.
@pytest.mark.parametrize('template,setting,path', SITE_HOOKS)
def test_site_hook_digest_ignores_whitespace_and_case(
    tmp_path, template, setting, path
):
    import hashlib

    value = '  ' + hashlib.sha256(HOOK.encode()).hexdigest().upper() + ' \n'
    result, _, ran = site_hook_run(tmp_path, template, setting, path, value)
    assert result.returncode == 0 and 'CONTINUED' in result.stdout, (
        result.stdout + result.stderr
    )
    assert ran == 'ran as userdata_customizations.sh\n'


# Contract S3: a desktop checks its pinned hook before DCV starts, so a mismatch stops it before
# the owner can connect; a matching hook still runs at the end.
@pytest.mark.parametrize('matches', [True, False])
def test_desktop_site_hook_is_checked_before_dcv_starts(tmp_path, matches):
    import hashlib

    config = Config()
    config.put(
        'virtual-desktop-controller.dcv_host_site_hook_sha256',
        hashlib.sha256(HOOK.encode()).hexdigest() if matches else '0' * 64,
    )
    text = render(
        'virtual-desktop-host-linux/configure_dcv_host.sh.jinja2',
        'rhel9',
        config=config,
        variables={'session_owner': 'sample-user', 'project': 'p'},
    )
    # in the rendered script the check comes before the DCV start, the hook block after it
    check_at = text.index('SITE_HOOK_RUN=""\nprepare_site_hook')
    dcv_at = text.index('\nstart_and_configure_dcv_service\n', check_at)
    hook_at = text.index('if [[ -n "${SITE_HOOK_RUN}" ]]; then', dcv_at)
    check = text[check_at : text.index('|| true\n', check_at) + 8]
    hook_block = text[hook_at : text.index('\nfi\n', hook_at) + 4]
    # run those three parts in that order; the steps between them install software and are not run
    segment = (
        check
        + 'start_and_configure_dcv_service\nstart_and_configure_dcv_agent_service\n'
        + hook_block
    )
    hook = tmp_path / 'apps' / 'dcv_host' / 'userdata_customizations.sh'
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK)
    segment = segment.replace('${IDEA_CLUSTER_HOME}/', f'{tmp_path}/apps/').replace(
        '>> ${BOOTSTRAP_DIR}/logs/userdata_customizations.log',
        f'>> {tmp_path}/hook.log',
    )
    calls = tmp_path / 'calls'
    stubs = f"""
export RECORD={tmp_path}/ran
flock() {{ :; }}
start_and_configure_dcv_service() {{ echo dcv-started >> {calls}; }}
start_and_configure_dcv_agent_service() {{ echo agent-started >> {calls}; }}
"""
    common = open(COMMON).read()
    result = bash(
        f"source /dev/stdin <<'COMMON'\n{common}\nCOMMON\n"
        + LOG_STUBS
        + FAILURE
        + stubs
        + segment
    )
    recorded = calls.read_text() if calls.exists() else ''
    if matches:
        assert 'dcv-started' in recorded
        assert (tmp_path / 'ran').read_text() == 'ran as userdata_customizations.sh\n'
    else:
        assert result.returncode == 1 and 'FAILED site-hook-mismatch' in result.stdout
        assert 'dcv-started' not in recorded and 'agent-started' not in recorded
        assert not (tmp_path / 'ran').exists()


# rereview2 S2: the messages name the key under the module id, also where it differs from the name.
def test_site_hook_messages_name_the_module_id_key(tmp_path):
    from jinja2 import Environment, FileSystemLoader
    from ideasdk.context import BootstrapContext

    config = Config()
    context = BootstrapContext(
        config=config,
        module_name='scheduler',
        module_id='hpc',
        module_set='default',
        base_os='rhel9',
        instance_type='m7i.large',
    )
    context.vars.job = job()
    context.vars.job_directory = '/apps/jobs/1'
    text = (
        Environment(loader=FileSystemLoader(IDEA_BOOTSTRAP_DIR))
        .get_template('compute-node/compute_node_post_reboot.sh.jinja2')
        .render(context=context)
    )
    start = text.index('if prepare_site_hook ')
    block = text[start : text.index('\n  fi\n', start) + 5]
    hook = tmp_path / 'apps' / 'hpc' / 'compute_node' / 'userdata_customizations.sh'
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK)
    block = block.replace(
        '${IDEA_CLUSTER_HOME}/${IDEA_MODULE_ID}/', f'{tmp_path}/apps/hpc/'
    )
    block = block.replace(
        '${IDEA_COMPUTE_NODE_LOGS_DIR}/userdata_customizations.log',
        f'{tmp_path}/hook.log',
    )
    common = open(COMMON).read()
    result = bash(
        f"source /dev/stdin <<'COMMON'\n{common}\nCOMMON\n"
        + LOG_STUBS
        + FAILURE
        + f'export RECORD={tmp_path}/ran\n'
        + block
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'set hpc.compute_node_site_hook_sha256 to ' in result.stdout
    assert 'scheduler.compute_node_site_hook_sha256' not in result.stdout
