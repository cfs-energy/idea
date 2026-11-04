/** Prepare the bastion's persistent SSH identity and directory client before listening. */
import { execFileSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { chmodSync, chownSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';
import { EC2Client, DescribeKeyPairsCommand } from '@aws-sdk/client-ec2';
import { SecretsManagerClient, GetSecretValueCommand, PutSecretValueCommand } from '@aws-sdk/client-secrets-manager';
import { SQSClient, SendMessageCommand } from '@aws-sdk/client-sqs';
import { STSClient, GetCallerIdentityCommand } from '@aws-sdk/client-sts';
import { Agent, fetch } from 'undici';

const KEY_TYPES = ['rsa', 'ecdsa', 'ed25519'];
type Env = Record<string, string | undefined>;
const required = (env: Env, name: string): string => {
  const value = env[name];
  if (value === undefined) throw new Error(`Missing ${name}`);
  return value;
};
export interface SecretStore {
  get(version?: string): Promise<string>;
  put(token: string, value: string): Promise<void>;
}

export async function restoreHostKeys(store: SecretStore, secretArn: string, directory = '/etc/ssh'): Promise<void> {
  let value = JSON.parse(await store.get());
  if (value.schema === 1 && Object.keys(value).length === 1) {
    const temporary = mkdtempSync(join(tmpdir(), 'bastion-keys-'));
    try {
      const keys: Record<string, string> = {};
      for (const kind of KEY_TYPES) {
        const name = `ssh_host_${kind}_key`;
        const path = join(temporary, name);
        execFileSync('ssh-keygen', ['-q', '-t', kind, '-N', '', '-f', path]);
        keys[name] = readFileSync(path, 'utf8');
        keys[`${name}.pub`] = readFileSync(`${path}.pub`, 'utf8');
      }
      const token = createHash('sha256').update(`${secretArn}:ssh-host-keys-v1`).digest('hex');
      try { await store.put(token, JSON.stringify({ schema: 1, keys })); } catch {
        // The immutable version resolves concurrent starters and ambiguous write timeouts.
      }
      value = JSON.parse(await store.get(token));
    } finally { rmSync(temporary, { recursive: true, force: true }); }
  }
  const names = KEY_TYPES.flatMap((kind) => [`ssh_host_${kind}_key`, `ssh_host_${kind}_key.pub`]).sort();
  const keys = value.keys ?? {};
  if (value.schema !== 1 || JSON.stringify(Object.keys(keys).sort()) !== JSON.stringify(names)
    || names.some((name) => typeof keys[name] !== 'string')) {
    throw new Error('Invalid bastion host key secret; refusing to change SSH identity');
  }
  mkdirSync(directory, { recursive: true });
  for (const name of names) {
    const path = join(directory, name);
    writeFileSync(path, keys[name], { mode: 0o600 });
    chmodSync(path, name.endsWith('.pub') ? 0o644 : 0o600);
  }
}

export async function requestAdJoin(env: Env): Promise<Record<string, string>> {
  const identity = (await new STSClient({ region: env.AWS_DEFAULT_REGION ?? env.AWS_REGION }).send(new GetCallerIdentityCommand({}))).UserId;
  if (!identity) throw new Error('Missing caller identity');
  const dispatcher = new Agent();
  let taskArn: string;
  try {
    const response = await fetch(`${required(env, 'ECS_CONTAINER_METADATA_URI_V4')}/task`, {
      dispatcher, signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw new Error(`Task metadata returned ${response.status}`);
    taskArn = (await response.json() as { TaskARN: string }).TaskARN;
    if (!taskArn) throw new Error('Task metadata has no TaskARN');
  } finally { await dispatcher.close(); }
  const nonce = randomUUID().replaceAll('-', '');
  await new SQSClient({ region: env.AWS_DEFAULT_REGION ?? env.AWS_REGION }).send(new SendMessageCommand({
    QueueUrl: required(env, 'IDEA_AD_QUEUE_URL'),
    MessageBody: JSON.stringify({ header: { namespace: 'ADAutomation.PresetComputer' }, payload: { task_arn: taskArn, nonce } }),
    MessageGroupId: identity, MessageDeduplicationId: nonce,
  }));
  const client = new DynamoDBClient({ region: env.AWS_DEFAULT_REGION ?? env.AWS_REGION });
  for (let attempt = 0; attempt < 60; attempt++) {
    const { Item } = await client.send(new GetItemCommand({
      TableName: required(env, 'IDEA_AD_TABLE'), ConsistentRead: true,
      Key: { instance_id: { S: identity }, nonce: { S: nonce } },
    }));
    if (Item) {
      const value = Object.fromEntries(Object.entries(Item).flatMap(([key, attribute]) => attribute.S === undefined ? [] : [[key, attribute.S]]));
      if (value.status !== 'success') throw new Error('Directory authorization failed');
      return value;
    }
    await sleep(2000);
  }
  throw new Error('Directory authorization did not complete');
}

type Run = (command: string, args: string[], input?: string) => string;
const run: Run = (command, args, input) => execFileSync(command, args, { input, encoding: 'utf8' });
const privateFile = (path: string, text: string, mode = 0o600): void => {
  writeFileSync(path, text, { mode });
  chmodSync(path, mode);
};

export async function configureDirectory(env: Env, root = '/', authorize = requestAdJoin, execute: Run = run): Promise<void> {
  const data = required(env, 'IDEA_DATA_DIR');
  let config: string;
  if (required(env, 'IDEA_DIRECTORY_PROVIDER') === 'openldap') {
    const certificate = join(root, 'etc/openldap/cacerts/openldap-server.pem');
    mkdirSync(join(root, 'etc/openldap/cacerts'), { recursive: true });
    privateFile(certificate, required(env, 'IDEA_LDAP_CERTIFICATE'));
    delete env.IDEA_LDAP_CERTIFICATE;
    config = `[sssd]
services = nss, pam, sudo
config_file_version = 2
domains = default
[domain/default]
id_provider = ldap
auth_provider = ldap
chpass_provider = ldap
sudo_provider = ldap
ldap_uri = ldap://${required(env, 'IDEA_LDAP_HOST')}
ldap_search_base = ${required(env, 'IDEA_LDAP_BASE')}
ldap_sudo_search_base = ou=Sudoers,${required(env, 'IDEA_LDAP_BASE')}
ldap_id_use_start_tls = true
ldap_tls_reqcert = demand
ldap_tls_cacert = ${certificate}
cache_credentials = true
use_fully_qualified_names = false
fallback_homedir = ${data}/home/%u
[nss]
homedir_substring = ${data}/home
[pam]
[sudo]
`;
  } else {
    const authorization = await authorize(env);
    const domain = required(env, 'IDEA_AD_DOMAIN');
    const hostname = authorization.hostname;
    execute('adcli', ['join', `--domain=${domain}`, `--domain-controller=${authorization.domain_controller}`,
      `--computer-name=${hostname}`, `--host-fqdn=${hostname.toLowerCase()}.${domain}`, '--login-type=computer', '--stdin-password'], authorization.otp);
    config = `[sssd]
services = nss, pam
config_file_version = 2
domains = ${domain}
[domain/${domain}]
ad_domain = ${domain}
ad_hostname = ${hostname.toLowerCase()}.${domain}
krb5_realm = ${domain.toUpperCase()}
id_provider = ad
access_provider = ad
auth_provider = ad
chpass_provider = ad
cache_credentials = true
krb5_store_password_if_offline = true
krb5_ccname_template = FILE:/tmp/krb5cc_%U_XXXXXX
default_shell = /bin/bash
ldap_id_mapping = ${required(env, 'IDEA_AD_ID_MAPPING')}
use_fully_qualified_names = false
fallback_homedir = ${data}/home/%u
ldap_sasl_authid = ${hostname}$
sudo_provider = none
[nss]
homedir_substring = ${data}/home
[pam]
`;
    const group = required(env, 'IDEA_AD_SUDOERS_GROUP').replaceAll('\\', '\\\\').replaceAll(' ', '\\ ');
    if (/[\r\n]/.test(group)) throw new Error('Invalid directory sudoers group');
    privateFile(join(root, 'etc/sudoers.d/idea-directory'), `%${group} ALL=(ALL:ALL) ALL\n`, 0o440);
    mkdirSync(join(root, 'etc/krb5.conf.d'), { recursive: true });
    privateFile(join(root, 'etc/krb5.conf.d/idea-ccache.conf'), '[libdefaults]\ndefault_ccache_name = FILE:/tmp/krb5cc_%{uid}\n');
  }
  privateFile(join(root, 'etc/sssd/sssd.conf'), config);
  execute('authselect', ['select', 'sssd', 'with-sudo', '--force']);
}

export async function configureAdminAccount(env: Env, root = "/", execute: Run = run, own = chownSync): Promise<void> {
  const response = await new EC2Client({ region: env.AWS_DEFAULT_REGION ?? env.AWS_REGION }).send(new DescribeKeyPairsCommand({ KeyNames: [required(env, 'IDEA_SSH_KEY_PAIR')], IncludePublicKey: true }));
  const publicKey = response.KeyPairs?.[0]?.PublicKey?.trim();
  if (!publicKey || !/^(ssh-rsa |ssh-ed25519 )/.test(publicKey) || publicKey.includes('\n')) throw new Error('Invalid EC2 key pair public key');
  let account: string;
  try { account = execute('getent', ['passwd', 'ec2-user']); } catch {
    execute('useradd', ['--create-home', '--shell', '/bin/bash', 'ec2-user']);
    account = execute('getent', ['passwd', 'ec2-user']);
  }
  const [, , uid, gid] = account.trim().split(':');
  if (!/^\d+$/.test(uid) || !/^\d+$/.test(gid)) throw new Error('Invalid ec2-user account');
  const directory = join(root, 'home/ec2-user/.ssh');
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);
  privateFile(join(directory, 'authorized_keys'), `${publicKey}\n`);
  for (const path of [join(root, 'home/ec2-user'), directory, join(directory, 'authorized_keys')]) own(path, Number(uid), Number(gid));
  privateFile(join(root, 'etc/sudoers.d/idea-ec2-user'), 'ec2-user ALL=(ALL) NOPASSWD: ALL\n', 0o440);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  process.umask(0o077);
  const secretArn = required(process.env, 'IDEA_HOST_KEYS_SECRET_ARN');
  const client = new SecretsManagerClient({ region: process.env.AWS_DEFAULT_REGION ?? process.env.AWS_REGION });
  await restoreHostKeys({
    get: async (version) => (await client.send(new GetSecretValueCommand({ SecretId: secretArn, VersionId: version }))).SecretString!,
    put: async (token, value) => { await client.send(new PutSecretValueCommand({ SecretId: secretArn, ClientRequestToken: token, SecretString: value })); },
  }, secretArn);
  await configureDirectory(process.env);
  await configureAdminAccount(process.env);
}
