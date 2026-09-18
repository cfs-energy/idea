"""Prepare the bastion's persistent identity and directory client before listening."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import time
import urllib.request
import uuid

import boto3


KEY_TYPES = ('rsa', 'ecdsa', 'ed25519')


def restore_host_keys(client, secret_arn, directory=Path('/etc/ssh')):
    value = json.loads(client.get_secret_value(SecretId=secret_arn)['SecretString'])
    if value == {'schema': 1}:
        with tempfile.TemporaryDirectory() as temporary:
            keys = {}
            for kind in KEY_TYPES:
                path = Path(temporary) / f'ssh_host_{kind}_key'
                subprocess.run(
                    ['ssh-keygen', '-q', '-t', kind, '-N', '', '-f', str(path)],
                    check=True,
                )
                keys[path.name] = path.read_text()
                keys[path.name + '.pub'] = Path(str(path) + '.pub').read_text()
            # Every first starter attempts the same immutable version. Only one value wins;
            # losers read it back before listening, including after an ambiguous API timeout.
            token = hashlib.sha256(
                f'{secret_arn}:ssh-host-keys-v1'.encode()
            ).hexdigest()
            try:
                client.put_secret_value(
                    SecretId=secret_arn,
                    ClientRequestToken=token,
                    SecretString=json.dumps({'schema': 1, 'keys': keys}),
                )
            except Exception:
                # A failed read propagates: never serve with unpersisted local keys.
                pass
            value = json.loads(
                client.get_secret_value(SecretId=secret_arn, VersionId=token)[
                    'SecretString'
                ]
            )
    keys = value.get('keys', {})
    names = {
        f'ssh_host_{kind}_key{suffix}' for kind in KEY_TYPES for suffix in ('', '.pub')
    }
    if value.get('schema') != 1 or set(keys) != names:
        raise ValueError(
            'Invalid bastion host key secret; refusing to change SSH identity'
        )
    directory.mkdir(parents=True, exist_ok=True)
    for name in sorted(names):
        path = directory / name
        path.write_text(keys[name])
        path.chmod(0o644 if name.endswith('.pub') else 0o600)


def request_ad_join(session, env):
    identity = session.client('sts').get_caller_identity()['UserId']
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(
        env['ECS_CONTAINER_METADATA_URI_V4'] + '/task', timeout=5
    ) as response:
        task_arn = json.load(response)['TaskARN']
    nonce = uuid.uuid4().hex
    session.client('sqs').send_message(
        QueueUrl=env['IDEA_AD_QUEUE_URL'],
        MessageBody=json.dumps(
            {
                'header': {'namespace': 'ADAutomation.PresetComputer'},
                'payload': {'task_arn': task_arn, 'nonce': nonce},
            }
        ),
        MessageGroupId=identity,
        MessageDeduplicationId=nonce,
    )
    client = session.client('dynamodb')
    for _ in range(60):
        item = client.get_item(
            TableName=env['IDEA_AD_TABLE'],
            ConsistentRead=True,
            Key={'instance_id': {'S': identity}, 'nonce': {'S': nonce}},
        ).get('Item')
        if item:
            value = {
                key: attribute['S']
                for key, attribute in item.items()
                if 'S' in attribute
            }
            if value.get('status') != 'success':
                raise RuntimeError('Directory authorization failed')
            return value
        time.sleep(2)
    raise TimeoutError('Directory authorization did not complete')


def configure_directory(session, env, root=Path('/')):
    data = env['IDEA_DATA_DIR']
    if env['IDEA_DIRECTORY_PROVIDER'] == 'openldap':
        certificate = root / 'etc/openldap/cacerts/openldap-server.pem'
        certificate.parent.mkdir(parents=True, exist_ok=True)
        certificate.write_text(env.pop('IDEA_LDAP_CERTIFICATE'))
        config = f"""[sssd]
services = nss, pam, sudo
config_file_version = 2
domains = default
[domain/default]
id_provider = ldap
auth_provider = ldap
chpass_provider = ldap
sudo_provider = ldap
ldap_uri = ldap://{env['IDEA_LDAP_HOST']}
ldap_search_base = {env['IDEA_LDAP_BASE']}
ldap_sudo_search_base = ou=Sudoers,{env['IDEA_LDAP_BASE']}
ldap_id_use_start_tls = true
ldap_tls_reqcert = demand
ldap_tls_cacert = {certificate}
cache_credentials = true
use_fully_qualified_names = false
fallback_homedir = {data}/home/%u
[nss]
homedir_substring = {data}/home
[pam]
[sudo]
"""
    else:
        authorization = request_ad_join(session, env)
        domain = env['IDEA_AD_DOMAIN']
        hostname = authorization['hostname']
        subprocess.run(
            [
                'adcli',
                'join',
                f'--domain={domain}',
                f'--domain-controller={authorization["domain_controller"]}',
                f'--computer-name={hostname}',
                f'--host-fqdn={hostname.lower()}.{domain}',
                '--login-type=computer',
                '--stdin-password',
            ],
            input=authorization['otp'],
            text=True,
            check=True,
        )
        config = f"""[sssd]
services = nss, pam
config_file_version = 2
domains = {domain}
[domain/{domain}]
ad_domain = {domain}
ad_hostname = {hostname.lower()}.{domain}
krb5_realm = {domain.upper()}
id_provider = ad
access_provider = ad
auth_provider = ad
chpass_provider = ad
cache_credentials = true
krb5_store_password_if_offline = true
default_shell = /bin/bash
ldap_id_mapping = {env['IDEA_AD_ID_MAPPING']}
use_fully_qualified_names = false
fallback_homedir = {data}/home/%u
ldap_sasl_authid = {hostname}$
sudo_provider = none
[nss]
homedir_substring = {data}/home
[pam]
"""
        group = env['IDEA_AD_SUDOERS_GROUP'].replace('\\', '\\\\').replace(' ', '\\ ')
        if any(character in group for character in '\n\r'):
            raise ValueError('Invalid directory sudoers group')
        sudoers = root / 'etc/sudoers.d/idea-directory'
        sudoers.write_text(f'%{group} ALL=(ALL:ALL) ALL\n')
        sudoers.chmod(0o440)
    path = root / 'etc/sssd/sssd.conf'
    path.write_text(config)
    path.chmod(0o600)
    subprocess.run(['authselect', 'select', 'sssd', 'with-sudo', '--force'], check=True)


def configure_admin_account(session, env, root=Path('/')):
    response = session.client('ec2').describe_key_pairs(
        KeyNames=[env['IDEA_SSH_KEY_PAIR']], IncludePublicKey=True
    )
    public_key = response['KeyPairs'][0]['PublicKey'].strip()
    if not public_key.startswith(('ssh-rsa ', 'ssh-ed25519 ')) or '\n' in public_key:
        raise ValueError('Invalid EC2 key pair public key')
    try:
        account = pwd.getpwnam('ec2-user')
    except KeyError:
        subprocess.run(
            ['useradd', '--create-home', '--shell', '/bin/bash', 'ec2-user'], check=True
        )
        account = pwd.getpwnam('ec2-user')
    directory = root / 'home/ec2-user/.ssh'
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    keys = directory / 'authorized_keys'
    keys.write_text(public_key + '\n')
    keys.chmod(0o600)
    for path in (directory.parent, directory, keys):
        os.chown(path, account.pw_uid, account.pw_gid)
    sudoers = root / 'etc/sudoers.d/idea-ec2-user'
    sudoers.write_text('ec2-user ALL=(ALL) NOPASSWD: ALL\n')
    sudoers.chmod(0o440)


def main():
    os.umask(0o077)
    session = boto3.Session()
    restore_host_keys(
        session.client('secretsmanager'), os.environ['IDEA_HOST_KEYS_SECRET_ARN']
    )
    configure_directory(session, os.environ)
    configure_admin_account(session, os.environ)


if __name__ == '__main__':
    main()
