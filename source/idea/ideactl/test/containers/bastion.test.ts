import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { STSClient } from '@aws-sdk/client-sts';
import { SQSClient } from '@aws-sdk/client-sqs';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { EC2Client } from '@aws-sdk/client-ec2';
import { configureAdminAccount, configureDirectory, requestAdJoin, restoreHostKeys } from '../../src/container/bastion.ts';

async function temporary(action: (root: string) => Promise<void>): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'bastion-test-'));
  try { await action(root); } finally { rmSync(root, { force: true, recursive: true }); }
}

test('replacements and concurrent first starters serve the persisted SSH identity', async () => {
  await temporary(async (root) => {
    let value = '{"schema":1}';
    let writes = 0;
    const store = {
      get: async () => value,
      put: async (_token: string, next: string) => { writes++; value = next; },
    };
    await restoreHostKeys(store, 'test-secret', join(root, 'first'));
    await restoreHostKeys(store, 'test-secret', join(root, 'second'));
    assert.equal(writes, 1);
    let version: string | undefined;
    let reads = 0;
    await restoreHostKeys({
      get: async (token) => { reads++; if (!token) return '{"schema":1}'; assert.equal(token, version); return value; },
      put: async (token) => { version = token; throw new Error('version exists'); },
    }, 'test-secret', join(root, 'third'));
    assert.equal(reads, 2);
    for (const name of Object.keys(JSON.parse(value).keys)) {
      for (const copy of ['second', 'third']) assert.equal(readFileSync(join(root, copy, name), 'utf8'), readFileSync(join(root, 'first', name), 'utf8'));
      assert.equal(statSync(join(root, 'first', name)).mode & 0o777, name.endsWith('.pub') ? 0o644 : 0o600);
    }
  });
});

test('invalid secrets and read failures never generate a new identity', async () => {
  const put = async () => { assert.fail('must not write'); };
  await assert.rejects(restoreHostKeys({ get: async () => '{"schema":1,"keys":{"../../escape":"bad"}}', put }, 'test-secret'), /Invalid bastion host key/);
  await assert.rejects(restoreHostKeys({ get: async () => { throw new Error('access denied'); }, put }, 'test-secret'), /access denied/);
});

test('OpenLDAP requires TLS and keeps shared homes and private configuration', async () => {
  await temporary(async (root) => {
    mkdirSync(join(root, 'etc/sssd'), { recursive: true });
    const calls: string[][] = [];
    await configureDirectory({ IDEA_DATA_DIR: '/data', IDEA_DIRECTORY_PROVIDER: 'openldap', IDEA_LDAP_HOST: 'directory.example.invalid', IDEA_LDAP_BASE: 'dc=example,dc=invalid', IDEA_LDAP_CERTIFICATE: 'certificate' }, root, undefined,
      (command, args) => { calls.push([command, ...args]); return ''; });
    const file = join(root, 'etc/sssd/sssd.conf');
    const config = readFileSync(file, 'utf8');
    for (const line of ['ldap_tls_reqcert = demand', 'fallback_homedir = /data/home/%u', 'sudo_provider = ldap']) assert.ok(config.includes(line));
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(calls, [['authselect', 'select', 'sssd', 'with-sudo', '--force']]);
  });
});

test('AD sends the one-time password only on stdin and uses file credential caches', async () => {
  await temporary(async (root) => {
    for (const path of ['etc/sssd', 'etc/sudoers.d']) mkdirSync(join(root, path), { recursive: true });
    const calls: Array<{ command: string; args: string[]; input?: string }> = [];
    await configureDirectory({ IDEA_DATA_DIR: '/data', IDEA_DIRECTORY_PROVIDER: 'activedirectory', IDEA_AD_DOMAIN: 'example.invalid', IDEA_AD_ID_MAPPING: 'false', IDEA_AD_SUDOERS_GROUP: 'Directory Admins' }, root,
      async () => ({ hostname: 'IDEA-TEST', domain_controller: 'dc.example.invalid', otp: 'one-time-password' }),
      (command, args, input) => { calls.push({ command, args, input }); return ''; });
    assert.equal(calls[0].input, 'one-time-password');
    assert.ok(!calls[0].args.join(' ').includes('one-time-password'));
    const config = readFileSync(join(root, 'etc/sssd/sssd.conf'), 'utf8');
    for (const line of ['krb5_ccname_template = FILE:/tmp/krb5cc_%U_XXXXXX', 'access_provider = ad', 'ldap_id_mapping = false', 'fallback_homedir = /data/home/%u']) assert.ok(config.includes(line));
    assert.equal(readFileSync(join(root, 'etc/krb5.conf.d/idea-ccache.conf'), 'utf8'), '[libdefaults]\ndefault_ccache_name = FILE:/tmp/krb5cc_%{uid}\n');
    assert.equal(readFileSync(join(root, 'etc/sudoers.d/idea-directory'), 'utf8'), '%Directory\\ Admins ALL=(ALL:ALL) ALL\n');
  });
});

test('the administrator account restores only the public EC2 key', async (t) => {
  t.mock.method(EC2Client.prototype, 'send', async function (this: EC2Client, command: { input: unknown }) {
    assert.equal(await this.config.region(), 'us-east-2');
    assert.deepEqual(command.input, { KeyNames: ['sample-key'], IncludePublicKey: true });
    return { KeyPairs: [{ PublicKey: 'ssh-ed25519 public-key' }] };
  });
  await temporary(async (root) => {
    mkdirSync(join(root, 'etc/sudoers.d'), { recursive: true });
    await configureAdminAccount({ IDEA_SSH_KEY_PAIR: 'sample-key', AWS_DEFAULT_REGION: 'us-east-2' }, root,
      () => 'ec2-user:x:1000:1000::/home/ec2-user:/bin/bash', () => {});
    const file = join(root, 'home/ec2-user/.ssh/authorized_keys');
    assert.equal(readFileSync(file, 'utf8'), 'ssh-ed25519 public-key\n');
    assert.equal(statSync(file).mode & 0o777, 0o600);
  });
});


test('AD authorization binds task metadata, caller identity, nonce, and consistent read', async (t) => {
  const server = createServer((request, response) => {
    assert.equal(request.url, '/task');
    response.end(JSON.stringify({ TaskARN: 'arn:aws:ecs:us-east-2:123456789012:task/sample/id' }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  let nonce: string;
  let status = 'success';
  t.mock.method(STSClient.prototype, 'send', async function (this: STSClient) {
    assert.equal(await this.config.region(), 'us-east-2');
    return { UserId: 'role:task-id' };
  });
  t.mock.method(SQSClient.prototype, 'send', async function (this: SQSClient, command: any) {
    assert.equal(await this.config.region(), 'us-east-2');
    const body = JSON.parse(command.input.MessageBody);
    nonce = body.payload.nonce;
    assert.match(nonce, /^[a-f0-9]{32}$/);
    assert.deepEqual(body, { header: { namespace: 'ADAutomation.PresetComputer' }, payload: { nonce, task_arn: 'arn:aws:ecs:us-east-2:123456789012:task/sample/id' } });
    assert.equal(command.input.MessageGroupId, 'role:task-id');
    assert.equal(command.input.MessageDeduplicationId, nonce);
    assert.equal(command.input.QueueUrl, 'sample-queue');
    return {};
  });
  t.mock.method(DynamoDBClient.prototype, 'send', async function (this: DynamoDBClient, command: any) {
    assert.equal(await this.config.region(), 'us-east-2');
    assert.deepEqual(command.input, { TableName: 'sample-table', ConsistentRead: true, Key: { instance_id: { S: 'role:task-id' }, nonce: { S: nonce } } });
    return { Item: { status: { S: status }, hostname: { S: 'IDEA-SAMPLE' }, otp: { S: 'one-time-password' } } };
  });
  const env = { AWS_DEFAULT_REGION: 'us-east-2', ECS_CONTAINER_METADATA_URI_V4: `http://127.0.0.1:${address.port}`, IDEA_AD_QUEUE_URL: 'sample-queue', IDEA_AD_TABLE: 'sample-table' };
  try {
    assert.deepEqual(await requestAdJoin(env), { status: 'success', hostname: 'IDEA-SAMPLE', otp: 'one-time-password' });
    status = 'denied';
    await assert.rejects(requestAdJoin(env), /Directory authorization failed/);
  } finally { server.close(); await once(server, 'close'); }
});
