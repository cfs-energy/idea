"""
Any directory user could SSH to the node running another user's job, or to another user's Linux
desktop. The sshd PAM account stack now admits the host's owner, cluster administrators (whoever
sudo admits) and local accounts, and logs everyone else it refuses. sssd access is unchanged.
"""

import subprocess

import pytest

from ideadatamodel import SocaJobProvisioningOptions
from test_bootstrap_shell_syntax import BASH, COMPUTE_BASE_OS, DESKTOP_BASE_OS, job
from test_image_bake_bootstrap import render

RULE = 'account    requisite    pam_exec.so quiet /usr/local/libexec/idea-ssh-access'
# /etc/pam.d/sshd as each family ships it, cut to the lines around the account stack
PAM_SSHD = {
    'rpm': (
        '#%PAM-1.0\nauth       substack     password-auth\nauth       include      postlogin\n'
        'account    required     pam_sepermit.so\naccount    required     pam_nologin.so\n'
        'account    include      password-auth\npassword   include      password-auth\n'
        'session    include      password-auth\n'
    ),
    'deb': (
        '@include common-auth\naccount    required     pam_nologin.so\n'
        '@include common-account\nsession [success=ok ignore=ignore module_unknown=ignore default=bad] pam_selinux.so close\n'
        '@include common-session\n@include common-password\n'
    ),
}
DESKTOP = [b for b in DESKTOP_BASE_OS if not b.startswith('windows')]
COMPUTE = [b for b in COMPUTE_BASE_OS if not b.startswith('windows')]
ADMIN = 'hpc-admin'


def desktop(base_os, owner='sample-user'):
    return render(
        'virtual-desktop-host-linux/setup.sh.jinja2',
        base_os,
        variables={'session_owner': owner, 'idea_session_id': 'session-id'},
    )


def compute(base_os, owner='sample-user', **provisioning):
    node_job = job()
    node_job.owner = owner
    node_job.provisioning_options = SocaJobProvisioningOptions(
        stack_uuid='stack-uuid', **provisioning
    )
    return render(
        'compute-node/compute_node.sh.jinja2',
        base_os,
        variables={'job': node_job, 'job_directory': '/apps/example/jobs/1'},
    )


class Host:
    """A root with the family's /etc/pam.d/sshd and stubs for the lookups the check makes."""

    def __init__(self, tmp_path, base_os):
        self.root = tmp_path / 'root'
        (self.root / 'etc/pam.d').mkdir(parents=True)
        self.pam = self.root / 'etc/pam.d/sshd'
        self.pam.write_text(PAM_SSHD['deb' if base_os.startswith('ubuntu') else 'rpm'])
        self.bin = tmp_path / 'bin'
        self.bin.mkdir()
        self.log = tmp_path / 'syslog'
        stubs = {
            # local accounts are the ones /etc/passwd holds
            'getent': '[ "$1 $2 $3" = "-s files passwd" ] && [ "$4" = ec2-user ]',
            # sudo -n -l -U <user> true: only the administrator may run it
            'sudo': f'[ "$1 $2 $3" = "-n -l -U" ] && [ "$4" = {ADMIN} ] && [ "$5" = true ]',
            'logger': f'echo "$*" >> {self.log}',
        }
        for name, body in stubs.items():
            (self.bin / name).write_text(f'#!/bin/sh\n{body}\n')
            (self.bin / name).chmod(0o755)

    def localise(self, text):
        for path in ('/etc/idea', '/etc/pam.d', '/usr/local/libexec'):
            text = text.replace(path, f'{self.root}{path}')
        return text.replace('PATH=/usr/sbin', f'PATH={self.bin}:/usr/sbin')

    def bootstrap(self, script):
        start = script.index('# Begin: SSH access')
        block = script[start : script.index('# End: SSH access', start)]
        result = subprocess.run(
            [
                BASH,
                '-euc',
                'log_info() { :; }\nlog_warning() { echo "$*"; exit 9; }\n'
                + self.localise(block),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def ssh(self, user):
        """What pam_exec runs for a login, certificate or key: 0 admits, anything else refuses."""
        check = self.root / 'usr/local/libexec/idea-ssh-access'
        return subprocess.run(
            [str(check)],
            env={'PAM_USER': user, 'PAM_RHOST': '10.0.0.9', 'PAM_TYPE': 'account'},
            capture_output=True,
        ).returncode


def account_stack(pam):
    return [
        line
        for line in pam.read_text().splitlines()
        if line.startswith(('account', '-account', '@include common-account'))
    ]


@pytest.mark.parametrize(
    ('host', 'base_os'),
    [('desktop', b) for b in DESKTOP] + [('compute', b) for b in COMPUTE],
)
def test_owner_admins_and_local_accounts_get_in_and_others_are_refused(
    tmp_path, host, base_os
):
    target = Host(tmp_path, base_os)
    original = account_stack(target.pam)
    render_host = desktop if host == 'desktop' else compute
    target.bootstrap(render_host(base_os, owner='alice'))
    # first in the account stack, and every check the family had still follows it
    assert account_stack(target.pam) == [target.localise(RULE)] + original
    assert 'AllowUsers' not in render_host(
        base_os
    ) and 'AllowGroups' not in render_host(base_os)
    # the browser terminal logs in as the owner with a certificate; sshd then runs this same check
    assert target.ssh('alice') == 0
    assert target.ssh(ADMIN) == 0
    assert target.ssh('ec2-user') == 0
    assert not target.log.exists()
    assert target.ssh('bob') != 0
    assert target.log.read_text() == (
        '-p authpriv.notice -t idea-ssh-access refused SSH for bob from 10.0.0.9: '
        "not this host's owner or a cluster administrator\n"
    )
    assert target.ssh('') != 0


@pytest.mark.parametrize('base_os', ['rocky9', 'ubuntu2404'])
def test_a_host_from_another_hosts_image_admits_only_its_own_owner(tmp_path, base_os):
    target = Host(tmp_path, base_os)
    target.bootstrap(desktop(base_os, owner='alice'))
    target.bootstrap(desktop(base_os, owner='carol'))
    assert account_stack(target.pam).count(target.localise(RULE)) == 1
    assert target.ssh('carol') == 0
    assert target.ssh('alice') != 0


@pytest.mark.parametrize(
    'provisioning', [{'keep_forever': True}, {'terminate_when_idle': 10}]
)
def test_shared_capacity_nodes_keep_ssh_as_before(tmp_path, provisioning):
    target = Host(tmp_path, 'rocky9')
    original = target.pam.read_text()
    target.bootstrap(compute('rocky9'))
    assert target.localise(RULE) in target.pam.read_text()
    # an image made from a single-job node must not keep that job's owner on a shared node
    target.bootstrap(compute('rocky9', **provisioning))
    assert target.pam.read_text() == original
    assert not (target.root / 'etc/idea/ssh-allowed-users').exists()


def test_the_rule_is_in_the_sshd_stack_only():
    for script in (desktop('rocky9'), compute('rocky9')):
        block = script[
            script.index('# Begin: SSH access') : script.index('# End: SSH access')
        ]
        assert 'sssd' not in block and 'simple_allow' not in block
        assert {line for line in block.splitlines() if '/etc/pam.d/' in line} <= {
            line for line in block.splitlines() if '/etc/pam.d/sshd' in line
        }
