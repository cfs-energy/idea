"""
Three bootstrap defects seen on rc.1 nodes: socket-activated sshd has no /run/sshd until its first
connection, so sshd -T and -t refused to run and the key exchange drop-in was always rejected;
the compute bake deleted /var/tmp under kdump's initrd build; and sssd tried a DNS update no zone
serves.
"""

import subprocess
from pathlib import Path

from test_bootstrap_shell_syntax import BASH, IDEA_BOOTSTRAP_DIR
from test_image_bake_bootstrap import render


def test_sshd_can_check_its_config_before_the_service_has_ever_started(tmp_path):
    block = render('_templates/linux/ssh_post_quantum_kex.jinja2', 'ubuntu2404')
    run_dir = tmp_path / 'run-sshd'
    dropin = tmp_path / 'sshd_config.d'
    block = block.replace('/run/sshd', str(run_dir)).replace(
        '/etc/ssh/sshd_config.d', str(dropin)
    )
    calls = tmp_path / 'calls'
    stubs = f"""
ssh() {{ echo sntrup761x25519-sha512@openssh.com; }}
sshd() {{
  [[ -d {run_dir} ]] || {{ echo 'Missing privilege separation directory' >&2; return 255; }}
  [[ "$1" == -t ]] || echo 'kexalgorithms curve25519-sha256'
}}
systemctl() {{ echo "systemctl $*" >> {calls}; }}
log_warning() {{ echo "warning $*" >> {calls}; }}
"""
    result = subprocess.run([BASH, '-c', stubs + block], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (dropin / '10-idea-kex.conf').read_text() == (
        'KexAlgorithms sntrup761x25519-sha512@openssh.com,curve25519-sha256\n'
    )
    assert 'warning' not in calls.read_text()


def test_the_compute_bake_lets_kdump_finish_before_clearing_var_tmp():
    script = render(
        'compute-node-ami-builder/compute_node_ami_builder_post_reboot.sh.jinja2',
        'rhel8',
        variables={'image_kind': 'compute'},
    )
    assert script.index('systemctl start kdump.service') < script.index(
        'rm -rf /var/tmp/*'
    )


def test_sssd_does_not_attempt_dns_updates():
    template = Path(IDEA_BOOTSTRAP_DIR, '_templates/linux/join_activedirectory.jinja2')
    assert 'dyndns_update = false' in template.read_text()
