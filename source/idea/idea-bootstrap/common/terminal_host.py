"""Install public terminal trust and publish this host's SSH public key, never private keys."""

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


# sysexits EX_CONFIG: retrying cannot fix this host, so the boot retry loop stops on it.
EXIT_SITE_INTEGRATION = 78
# Site hooks that edit sshd_config hold this lock, so neither side loses the other's edit.
LOCK = 'run/idea-terminal-trust.lock'
LOCK_WAIT_SECONDS = 120


class SiteIntegrationRequired(ValueError):
    pass


PRINCIPALS_COMMAND = """#!/usr/bin/python3
import subprocess
import sys
from pathlib import Path


def main(username, ca_key):
    trusted = Path('/etc/ssh/idea_terminal_ca.pub').read_text().split()[1]
    if ca_key != trusted:
        # Preserve the implicit username principal for the site's existing CAs.
        print(username)
        return
    if not Path('/run/idea-terminal-trust-ready').exists():
        return
    local = subprocess.run(['/usr/bin/getent', '-s', 'files', 'passwd', username], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5)
    if local.returncode != 2:
        return
    directory = subprocess.run(['/usr/bin/getent', '-s', 'sss', 'passwd', username], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5)
    if directory.returncode == 0 and directory.stdout.split(':', 1)[0] == username:
        print('idea:' + username)


if __name__ == '__main__':
    main(*sys.argv[1:])
"""


def instance_identity():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = 'http://169.254.169.254/latest/'
    request = urllib.request.Request(
        base + 'api/token',
        method='PUT',
        headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'},
    )
    with opener.open(request, timeout=5) as response:
        token = response.read().decode()
    request = urllib.request.Request(
        base + 'dynamic/instance-identity/document',
        headers={'X-aws-ec2-metadata-token': token},
    )
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def dynamodb(operation, **payload):
    # The bootstrap already installs AWS CLI; system Python needs no third-party modules.
    env = {**os.environ, 'AWS_MAX_ATTEMPTS': '1', 'AWS_PAGER': ''}
    # The unit loads /etc/environment, where bootstrap writes the proxy as IDEA_* names.
    for name in ('HTTPS_PROXY', 'NO_PROXY'):
        if env.get('IDEA_' + name):
            env.setdefault(name, env['IDEA_' + name])
    try:
        result = subprocess.run(
            [
                'aws',
                'dynamodb',
                operation,
                '--cli-input-json',
                json.dumps(payload),
                '--output',
                'json',
                '--cli-connect-timeout',
                '5',
                '--cli-read-timeout',
                '5',
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
            env=env,
        )
    except subprocess.CalledProcessError as error:
        # Only public trust is exchanged; retain AWS/configuration errors for operators.
        raise RuntimeError(error.stderr.strip()) from error
    return json.loads(result.stdout or '{}')


def replace_public_keys(path, text):
    replace_validated(path, text, ['ssh-keygen', '-l', '-f'])


def replace_sshd_config(path, text):
    return replace_validated(path, text, ['sshd', '-t', '-f'])


def replace_validated(path, text, validator=None, mode=0o644):
    path = path.resolve()
    if path.exists() and path.read_text() == text:
        return False
    with tempfile.NamedTemporaryFile(
        mode='w', dir=path.parent, suffix='.tmp', delete=False
    ) as staged:
        temporary = Path(staged.name)
    try:
        temporary.write_text(text)
        if validator:
            subprocess.run(
                validator + [staged.name],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=5,
            )
        if path.exists():
            metadata = path.stat()
            os.chmod(staged.name, metadata.st_mode & 0o777)
            os.chown(staged.name, metadata.st_uid, metadata.st_gid)
        else:
            os.chmod(staged.name, mode)
        os.replace(staged.name, path)
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def reload_sshd():
    # Distributions name the unit sshd or ssh (Ubuntu aliases one to the other).
    units = set()
    for name in ('sshd', 'ssh'):
        shown = subprocess.run(
            ['systemctl', 'show', '-p', 'Id', '-p', 'LoadState', name],
            check=True,
            stdout=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
        ).stdout
        properties = dict(
            line.split('=', 1) for line in shown.splitlines() if '=' in line
        )
        if properties.get('LoadState') == 'loaded':
            units.add(properties['Id'])
    if not units:
        raise RuntimeError('No sshd unit to reload')
    # try-reload-or-restart is the same verb on systemd 229+; AL2 ships 219.
    subprocess.run(
        ['systemctl', 'reload-or-try-restart'] + sorted(units), check=True, timeout=30
    )


def configure(cluster, host_id, root=Path('/')):
    (root / 'run/sshd').mkdir(mode=0o755, parents=True, exist_ok=True)
    ready = root / 'run/idea-terminal-trust-ready'
    if ready.exists():
        ready.unlink()
    ca = (
        dynamodb(
            'get-item',
            TableName=f'{cluster}.cluster-settings',
            Key={'key': {'S': 'cluster-manager.web_terminal.ca_public_key'}},
            ConsistentRead=True,
        )
        .get('Item', {})
        .get('value', {})
        .get('S', '')
    )
    parts = ca.split()
    if len(parts) != 2 or parts[0] != 'ssh-ed25519':
        raise ValueError('Terminal CA is not ready')
    subprocess.run(
        ['ssh-keygen', '-l', '-f', '-'],
        input=ca + '\n',
        universal_newlines=True,
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=5,
    )
    with open(str(root / LOCK), 'a') as lock:
        # A long site hook fails this attempt; the retry loop tries again later.
        for _ in range(LOCK_WAIT_SECONDS):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(1)
        else:
            raise RuntimeError('sshd_config is locked by a site hook')
        install_trust(ca, host_id, root)
    public = (root / 'etc/ssh/ssh_host_ed25519_key.pub').read_text().strip()
    dynamodb(
        'put-item',
        TableName=f'{cluster}.cluster-manager.terminal',
        Item={'pk': {'S': 'host:' + host_id}, 'public_key': {'S': public}},
    )

    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.touch(mode=0o644)


def install_trust(ca, host_id, root):
    ssh = root / 'etc/ssh'
    path = ssh / 'idea_terminal_ca.pub'
    effective = subprocess.run(
        ['sshd', '-T'],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=5,
    ).stdout
    options = dict(
        line.split(maxsplit=1) for line in effective.splitlines() if ' ' in line
    )
    command = '/etc/ssh/idea_terminal_principals %u %K'
    if options.get('authorizedprincipalsfile', 'none') != 'none' or options.get(
        'authorizedprincipalscommand', 'none'
    ) not in {'none', command}:
        raise SiteIntegrationRequired(
            'Site certificate principal policy requires explicit Terminal integration'
        )
    previous = path.read_text().strip() if path.exists() else ''
    config = ssh / 'sshd_config'
    text = config.read_text()
    trusted = next(
        (
            line.split(maxsplit=1)[1]
            for line in effective.splitlines()
            if line.startswith('trustedusercakeys ')
        ),
        'none',
    )
    if trusted != 'none' and trusted != '/etc/ssh/idea_terminal_ca.pub':
        site = root / trusted.lstrip('/')
        keys = site.read_text().splitlines()
        # Replace only our previously installed key; preserve the site's trust on rotation.
        keys = [key for key in keys if key not in {previous, ca}]
        replace_public_keys(site, '\n'.join(keys + [ca]) + '\n')
    else:
        directive = 'TrustedUserCAKeys /etc/ssh/idea_terminal_ca.pub'
        # Prepend so the directive is global even when the existing config ends in Match.
        if directive not in text.splitlines():
            text = directive + '\n' + text
    # Advance tracking only after effective trust no longer contains the old CA.
    replace_public_keys(path, ca + '\n')
    principals = ssh / 'idea_terminal_principals'
    replace_validated(principals, PRINCIPALS_COMMAND, mode=0o755)
    directive = 'AuthorizedPrincipalsCommand ' + command
    if directive not in text.splitlines():
        text = directive + '\nAuthorizedPrincipalsCommandUser root\n' + text
    # The bastion entrypoint signals its own sshd. Elsewhere a failed reload stays
    # pending, so the next attempt reloads even though the file is now unchanged.
    pending = root / 'run/idea-terminal-sshd-reload'
    if host_id != 'bastion' and config.read_text() != text:
        pending.touch()
    replace_sshd_config(config, text)
    if pending.exists():
        reload_sshd()
        pending.unlink()


def main():
    if os.environ.get('IDEA_MODULE_NAME') == 'bastion-host':
        host_id = 'bastion'
    else:
        identity = instance_identity()
        region = identity['region']
        partition = (
            'aws-us-gov'
            if region.startswith('us-gov-')
            else 'aws-cn'
            if region.startswith('cn-')
            else 'aws'
        )
        host_id = f'arn:{partition}:ec2:{identity["region"]}:{identity["accountId"]}:instance/{identity["instanceId"]}'
    for attempt in range(5):
        try:
            configure(os.environ['IDEA_CLUSTER_NAME'], host_id)
            return
        except SiteIntegrationRequired as error:
            print(f'Terminal trust not installed: {error}', file=sys.stderr)
            sys.exit(EXIT_SITE_INTEGRATION)
        except Exception as error:
            print(
                f'Terminal trust attempt {attempt + 1} failed: {error}', file=sys.stderr
            )
            if attempt == 4:
                raise RuntimeError(
                    'Terminal SSH trust could not be installed'
                ) from error
            time.sleep(5)


if __name__ == '__main__':
    main()
