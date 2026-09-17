/**
 * The directoryservice stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * The template-for-template comparison is the parity harness's job. What lives here is what a
 * template diff cannot see: the CloudFormation assembly manifest (stack tags, termination
 * protection, the bootstrap qualifier the synthesizer derives from the cluster name), the
 * deletion/update-replace policy of every stateful resource, and the three provider branches the
 * dev27 capture does not cover.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored, and this whole file requires them.
 */

import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { buildApp } from '../../src/cdk/app.ts';
import { buildStack, ec2BlockDeviceName } from '../../src/cdk/stacks/directoryservice.ts';
import { requireCapture } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-directoryservice.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.directoryservice', 'manifest.json');

const CLUSTER = 'idea-dev27';
const MODULE_ID = 'directoryservice';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

requireCapture(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE, PYTHON_MANIFEST],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

/**
 * Copies the dev27 settings scan, replacing (or adding) each named key with the given typed
 * DynamoDB attribute value, and deleting the ones mapped to `null`.
 */
function configWith(overrides: Record<string, Json | null>): string {
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  const items: Json[] = [];
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    if (key === undefined || !remaining.delete(key)) {
      items.push(item);
      continue;
    }
    const value = overrides[key];
    if (value !== null && value !== undefined) items.push({ ...item, value });
  }
  for (const key of remaining) {
    const value = overrides[key];
    if (value !== null && value !== undefined) {
      items.push({ key: { S: key }, value, version: { N: '1' } });
    }
  }
  scan.Items = items;
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-ds-config-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

interface Synthesized {
  template: Json;
  manifest: Json;
}

async function synth(configFile: string = CONFIG_FILE, terminationProtection = true): Promise<Synthesized> {
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-ds-'));
  workdirs.push(workdir);
  cpSync(CONTEXT_FILE, join(workdir, 'cdk.context.json'));
  cpSync(join(PKG, 'cdk.json'), join(workdir, 'cdk.json'));
  const outdir = join(workdir, `cdk.out.${MODULE_ID}`);

  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  process.chdir(workdir);
  process.env.CDK_OUTDIR = outdir;
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: MODULE_ID,
        moduleName: MODULE_ID,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection,
        configFile,
        synthReadsFile: SYNTH_READS,
      },
      { [MODULE_ID]: async () => buildStack },
    );
    app.synth();
    return {
      template: readJson(join(outdir, `${CLUSTER}-${MODULE_ID}.template.json`)),
      manifest: readJson(join(outdir, 'manifest.json')),
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

/**
 * The branches that throw during stack construction. `CDK_OUTDIR` stays unset here so the `App`
 * registers no exit-time autosynth: the stack never finished, and a deferred synth would fire
 * after the temp directories are gone.
 */
async function synthRejects(configFile: string): Promise<void> {
  const previousOutdir = process.env.CDK_OUTDIR;
  delete process.env.CDK_OUTDIR;
  try {
    await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: MODULE_ID,
        moduleName: MODULE_ID,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile,
        synthReadsFile: SYNTH_READS,
      },
      { [MODULE_ID]: async () => buildStack },
    );
  } finally {
    if (previousOutdir !== undefined) process.env.CDK_OUTDIR = previousOutdir;
  }
}

/** `{logicalId: {Type, Path}}`, `AWS::CDK::Metadata` dropped. */
function inventory(template: Json): Record<string, { Type: string; Path: string | undefined }> {
  return Object.fromEntries(
    Object.entries((template.Resources ?? {}) as Json)
      .filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata')
      .map(([id, r]) => [
        id,
        { Type: (r as Json).Type as string, Path: (r as Json).Metadata?.['aws:cdk:path'] as string | undefined },
      ]),
  );
}

/** `{logicalId: [DeletionPolicy, UpdateReplacePolicy]}` for every resource, undefined included. */
function policies(template: Json): Record<string, [unknown, unknown]> {
  return Object.fromEntries(
    Object.entries((template.Resources ?? {}) as Json)
      .filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata')
      .map(([id, r]) => [id, [(r as Json).DeletionPolicy, (r as Json).UpdateReplacePolicy] as [unknown, unknown]]),
  );
}

/** One `S` value from the gitignored dev27 settings scan, so no live identifier is committed. */
function fixtureSetting(key: string): string {
  const item = (readJson(CONFIG_FILE).Items as Json[]).find((i) => i.key?.S === key);
  assert.ok(item, `${key} missing from the settings fixture`);
  return (item as Json).value.S as string;
}

/** `{rule_id: reason}` in template order for one resource's cdk_nag suppressions. */
function nagSuppressions(template: Json, logicalId: string): string[] {
  const resource = (template.Resources as Json)[logicalId] as Json | undefined;
  assert.ok(resource, `${logicalId} not in the template`);
  const rules = ((resource as Json).Metadata?.cdk_nag?.rules_to_suppress ?? []) as Json[];
  return rules.map((r) => r.id as string);
}

function settingsOf(template: Json): Json {
  const [, resource] =
    Object.entries((template.Resources ?? {}) as Json).find(([, r]) => (r as Json).Type === 'Custom::ClusterSettings') ??
    [];
  assert.ok(resource, 'no Custom::ClusterSettings resource');
  return (resource as Json).Properties.settings as Json;
}

interface ResourceTag {
  id: string;
  key: string;
  value: unknown;
}

/**
 * Every Key/Value pair from a resource `Tags` or `tags` property, list or map form.
 */
function resourceTagEntries(template: Json): ResourceTag[] {
  const entries: ResourceTag[] = [];
  for (const [id, raw] of Object.entries((template.Resources ?? {}) as Json)) {
    const resource = raw as Json;
    if (resource.Type === 'AWS::CDK::Metadata') continue;
    const props = (resource.Properties ?? {}) as Json;
    const tags = props.Tags ?? props.tags;
    if (tags === undefined || tags === null) continue;
    if (Array.isArray(tags)) {
      for (const tag of tags as Json[]) {
        const key = (tag.Key ?? tag.key) as string | undefined;
        if (key === undefined) continue;
        entries.push({ id, key, value: tag.Value ?? tag.value });
      }
      continue;
    }
    if (typeof tags === 'object') {
      for (const [key, value] of Object.entries(tags as Json)) {
        entries.push({ id, key, value });
      }
    }
  }
  return entries;
}

function stackArtifactProperties(manifest: Json): Json {
  return (manifest.artifacts as Json)[`${CLUSTER}-${MODULE_ID}`].properties as Json;
}

const TLS_CERTIFICATE_ARN = 'arn:aws:secretsmanager:us-east-2:123456789012:secret:idea-dev27-directoryservice-certificate-AAAAAA';
const TLS_PRIVATE_KEY_ARN = 'arn:aws:secretsmanager:us-east-2:123456789012:secret:idea-dev27-directoryservice-private-key-BBBBBB';

const OPENLDAP_CONFIG: Record<string, Json | null> = {
  'directoryservice.provider': { S: 'openldap' },
  'directoryservice.hostname': { S: 'openldap.idea.local' },
  'directoryservice.instance_type': { S: 'm7i-flex.large' },
  'directoryservice.ec2.metadata_http_tokens': { S: 'required' },
  // Published by the deploy tool before this stack synthesizes; a real openldap cluster has them.
  'directoryservice.tls_certificate_secret_arn': { S: TLS_CERTIFICATE_ARN },
  'directoryservice.tls_private_key_secret_arn': { S: TLS_PRIVATE_KEY_ARN },
};

// --- AWS Managed Active Directory (the branch dev27 deployed) ----------------------------------

describe('directoryservice stack, aws_managed_activedirectory', () => {
  test('the stateful resources carry exactly the live deletion policies', async () => {
    const { template } = await synth();
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    assert.deepEqual(policies(template), policies(withRetainedStateful(readJson(LIVE_TEMPLATE))));
  });

  test('both admin secrets and the MicrosoftAD have no deletion policy, and survive a replacement', async () => {
    // Changing directoryservice.name, .ad_short_name or the service account secret is what forces
    // the directory to be replaced, and a replacement severs every joined machine. The teardown
    // behaviour is unchanged.
    const { template } = await synth();
    for (const id of [
      'awsmanagedactivedirectoryadminusername',
      'awsmanagedactivedirectoryadminpassword',
      'activedirectory',
    ]) {
      const resource = (template.Resources as Json)[id] as Json;
      assert.ok(resource, `${id} missing`);
      assert.equal(resource.DeletionPolicy, undefined, `${id} DeletionPolicy`);
      assert.equal(resource.UpdateReplacePolicy, 'Retain', `${id} UpdateReplacePolicy`);
    }
  });

  test('the queue pair deletes with the stack, and both custom resources are Delete/Delete', async () => {
    const { template } = await synth();
    const pair = (id: string): [unknown, unknown] => [
      ((template.Resources as Json)[id] as Json).DeletionPolicy,
      ((template.Resources as Json)[id] as Json).UpdateReplacePolicy,
    ];
    for (const id of ['adautomationsqsqueue6E223C51', 'adautomationsqsqueuedlqC8003D97']) {
      assert.deepEqual(pair(id), ['Delete', 'Retain'], id);
    }
    for (const id of ['ideadev27activedirectory', 'ideadev27directoryservicesettings']) {
      assert.deepEqual(pair(id), ['Delete', 'Delete'], id);
    }
  });

  test('the logical ids and construct paths match the live template', async () => {
    const { template } = await synth();
    assert.deepEqual(inventory(template), inventory(readJson(LIVE_TEMPLATE)));
  });

  test('both queues end up tagged with the stack resource name, not the construct id', async () => {
    const { template } = await synth();
    for (const id of ['adautomationsqsqueue6E223C51', 'adautomationsqsqueuedlqC8003D97']) {
      const tags = ((template.Resources as Json)[id] as Json).Properties.Tags as Json[];
      const name = tags.find((tag) => tag.Key === 'Name');
      assert.deepEqual(name, { Key: 'Name', Value: `${CLUSTER}-${MODULE_ID}` }, id);
    }
  });

  test('the settings payload carries the directory id and both secret refs', async () => {
    const { template } = await synth();
    const settings = settingsOf(template);
    assert.equal(settings.deployment_id, DEPLOYMENT_ID);
    assert.deepEqual(settings.directory_id, { Ref: 'activedirectory' });
    assert.deepEqual(settings.root_username_secret_arn, { Ref: 'awsmanagedactivedirectoryadminusername' });
    assert.deepEqual(settings.root_password_secret_arn, { Ref: 'awsmanagedactivedirectoryadminpassword' });
    assert.deepEqual(settings['ad_automation.sqs_queue_url'], { Ref: 'adautomationsqsqueue6E223C51' });
    assert.deepEqual(settings['ad_automation.sqs_queue_arn'], {
      'Fn::GetAtt': ['adautomationsqsqueue6E223C51', 'Arn'],
    });
  });
});

// --- assembly manifest ------------------------------------------------------------------------

describe('directoryservice stack manifest', () => {
  test('stack properties equal the ones the python app wrote', async () => {
    const { manifest } = await synth();
    const ours = (manifest.artifacts as Json)[`${CLUSTER}-${MODULE_ID}`].properties as Json;
    const theirs = (readJson(PYTHON_MANIFEST).artifacts as Json)[`${CLUSTER}-${MODULE_ID}`].properties as Json;

    assert.equal(ours.terminationProtection, true);
    assert.equal(ours.terminationProtection, theirs.terminationProtection);
    assert.deepEqual(ours.tags, theirs.tags);
    assert.equal(ours.validateOnSynth, theirs.validateOnSynth);
    assert.equal(ours.assumeRoleArn, theirs.assumeRoleArn);
    assert.equal(ours.cloudFormationExecutionRoleArn, theirs.cloudFormationExecutionRoleArn);
    assert.equal(ours.requiresBootstrapStackVersion, theirs.requiresBootstrapStackVersion);
    assert.equal(ours.bootstrapStackVersionSsmParameter, theirs.bootstrapStackVersionSsmParameter);
    assert.deepEqual(ours.lookupRole, theirs.lookupRole);
    // The template asset digest moves with the template body; the bucket and prefix must not.
    const bucketPrefix = (url: string): string => url.slice(0, url.lastIndexOf('/') + 1);
    assert.equal(
      bucketPrefix(ours.stackTemplateAssetObjectUrl as string),
      bucketPrefix(theirs.stackTemplateAssetObjectUrl as string),
    );
  });

  test('the stack tags are stack level, so they appear in no resource', async () => {
    const { template, manifest } = await synth();
    const tags = stackArtifactProperties(manifest).tags as Json;
    assert.deepEqual(Object.keys(tags).sort(), [
      'idea:ClusterName',
      'idea:ModuleId',
      'idea:ModuleName',
      'idea:ModuleVersion',
    ]);
    // ModuleId and ModuleVersion stay off every resource. ClusterName and ModuleName may
    // appear as resource tags, and when they do the value is the stack tag's value.
    const body = JSON.stringify(template.Resources);
    assert.ok(!body.includes('idea:ModuleVersion'), 'idea:ModuleVersion leaked into the template');
    assert.ok(!body.includes('idea:ModuleId'), 'idea:ModuleId leaked into the template');
    for (const { id, key, value } of resourceTagEntries(template)) {
      if (Object.hasOwn(tags, key)) {
        assert.equal(value, tags[key], `${id} ${key}`);
      }
    }
  });

  test('termination protection follows the flag', async () => {
    const off = await synth(CONFIG_FILE, false);
    const on = await synth(CONFIG_FILE, true);
    assert.equal(stackArtifactProperties(off.manifest).terminationProtection, false);
    assert.equal(stackArtifactProperties(on.manifest).terminationProtection, true);
  });
  test('root_credentials_provided skips both secrets and takes the arns from config', async () => {
    // Unlike openldap, this branch goes through `get_username_secret_arn()`
    // (directoryservice_stack.py:545-550), which reads the config keys when the credentials are
    // provided. The expected values come out of the gitignored fixture, never a literal.
    const { template } = await synth(configWith({ 'directoryservice.root_credentials_provided': { BOOL: true } }));
    assert.deepEqual(
      Object.values(inventory(template)).filter((r) => r.Type === 'AWS::SecretsManager::Secret'),
      [],
    );
    const settings = settingsOf(template);
    assert.equal(settings.root_username_secret_arn, fixtureSetting('directoryservice.root_username_secret_arn'));
    assert.equal(settings.root_password_secret_arn, fixtureSetting('directoryservice.root_password_secret_arn'));
  });
});

// --- use_existing: no directory, no secrets, no resolver --------------------------------------

describe('directoryservice stack, use_existing managed AD', () => {
  test('only the queue pair, their policies and the settings are built', async () => {
    const { template } = await synth(
      configWith({
        'directoryservice.use_existing': { BOOL: true },
        'directoryservice.directory_id': { S: 'd-1234567890' },
      }),
    );
    assert.deepEqual(
      Object.values(inventory(template))
        .map((r) => r.Type)
        .sort(),
      ['AWS::SQS::Queue', 'AWS::SQS::Queue', 'AWS::SQS::QueuePolicy', 'AWS::SQS::QueuePolicy', 'Custom::ClusterSettings'],
    );
  });

  test('the settings drop directory_id and both secret arns', async () => {
    const { template } = await synth(
      configWith({
        'directoryservice.use_existing': { BOOL: true },
        'directoryservice.directory_id': { S: 'd-1234567890' },
      }),
    );
    assert.deepEqual(Object.keys(settingsOf(template)).sort(), [
      'ad_automation.sqs_queue_arn',
      'ad_automation.sqs_queue_url',
      'deployment_id',
    ]);
  });

  test('an empty directory_id fails the synth', async () => {
    await assert.rejects(
      synthRejects(configWith({ 'directoryservice.use_existing': { BOOL: true }, 'directoryservice.directory_id': null })),
      /directoryservice\.directory_id is required/,
    );
  });
});

// --- self managed activedirectory --------------------------------------------------------------

describe('directoryservice stack, self managed activedirectory', () => {
  const selfManaged: Record<string, Json | null> = {
    'directoryservice.provider': { S: 'activedirectory' },
    'directoryservice.root_credentials_provided': { BOOL: true },
    'directoryservice.clusteradmin.clusteradmin_username_secret_arn': { S: 'arn:aws:secretsmanager:us-east-2:123456789012:secret:clusteradmin-username' },
    'directoryservice.clusteradmin.clusteradmin_password_secret_arn': { S: 'arn:aws:secretsmanager:us-east-2:123456789012:secret:clusteradmin-password' },
  };

  test('builds only the queue pair and the settings', async () => {
    const { template } = await synth(configWith(selfManaged));
    assert.deepEqual(
      Object.values(inventory(template))
        .map((r) => r.Type)
        .sort(),
      ['AWS::SQS::Queue', 'AWS::SQS::Queue', 'AWS::SQS::QueuePolicy', 'AWS::SQS::QueuePolicy', 'Custom::ClusterSettings'],
    );
    assert.deepEqual(Object.keys(settingsOf(template)).sort(), [
      'ad_automation.sqs_queue_arn',
      'ad_automation.sqs_queue_url',
      'deployment_id',
    ]);
  });

  test('root_credentials_provided false fails the synth', async () => {
    await assert.rejects(
      synthRejects(configWith({ ...selfManaged, 'directoryservice.root_credentials_provided': { BOOL: false } })),
      /root_credentials_provided must be true/,
    );
  });

  test('a missing clusteradmin secret arn fails the synth', async () => {
    await assert.rejects(
      synthRejects(configWith({ ...selfManaged, 'directoryservice.clusteradmin.clusteradmin_password_secret_arn': null })),
      /clusteradmin_password_secret_arn is required/,
    );
  });
});

// --- openldap ----------------------------------------------------------------------------------

describe('directoryservice stack, openldap', () => {
  test('builds the resource set the spec lists, and no ad-automation queue', async () => {
    const { template } = await synth(configWith(OPENLDAP_CONFIG));
    const paths = Object.values(inventory(template))
      .map((r) => `${r.Type} ${r.Path}`)
      .sort();
    assert.deepEqual(paths, [
      `AWS::EC2::Instance ${CLUSTER}-${MODULE_ID}/${MODULE_ID}-instance`,
      `AWS::EC2::LaunchTemplate ${CLUSTER}-${MODULE_ID}/${MODULE_ID}-lt/Resource`,
      `AWS::EC2::SecurityGroup ${CLUSTER}-${MODULE_ID}/${MODULE_ID}-security-group/Resource`,
      // the bastion ingress rule is a separate resource: the peer is an imported security group
      `AWS::EC2::SecurityGroupIngress ${CLUSTER}-${MODULE_ID}/${MODULE_ID}-security-group/from ideadev27directoryservicebastionhostsecuritygroup2466957D:22`,
      `AWS::IAM::InstanceProfile ${CLUSTER}-${MODULE_ID}/${MODULE_ID}-openldap-instance-profile`,
      `AWS::IAM::Policy ${CLUSTER}-${MODULE_ID}/openldap-server-policy/Resource`,
      `AWS::IAM::Role ${CLUSTER}-${MODULE_ID}/${MODULE_ID}-openldap-role/Resource`,
      `AWS::Route53::RecordSet ${CLUSTER}-${MODULE_ID}/${MODULE_ID}-dns-record/Resource`,
      `AWS::SecretsManager::Secret ${CLUSTER}-${MODULE_ID}/openldap-admin-password`,
      `AWS::SecretsManager::Secret ${CLUSTER}-${MODULE_ID}/openldap-admin-username`,
      `Custom::ClusterSettings ${CLUSTER}-${MODULE_ID}/${CLUSTER}-${MODULE_ID}-settings/Default`,
      `Custom::SelfSignedCertificateOpenLDAPServer ${CLUSTER}-${MODULE_ID}/openldap-server-certs/Default`,
    ]);
  });

  test('the two secrets carry no deletion policy and the settings resource is Delete/Delete', async () => {
    const { template } = await synth(configWith(OPENLDAP_CONFIG));
    const byPath = Object.fromEntries(
      Object.entries((template.Resources ?? {}) as Json).map(([, r]) => [(r as Json).Metadata?.['aws:cdk:path'], r as Json]),
    );
    for (const id of ['openldap-admin-username', 'openldap-admin-password']) {
      const secret = byPath[`${CLUSTER}-${MODULE_ID}/${id}`] as Json;
      assert.equal(secret.DeletionPolicy, undefined, id);
      // A secret is replaced when its name changes, so the retained one never holds the name the
      // replacement wants, and the value is not lost.
      assert.equal(secret.UpdateReplacePolicy, 'Retain', id);
    }
    const settings = byPath[`${CLUSTER}-${MODULE_ID}/${CLUSTER}-${MODULE_ID}-settings/Default`] as Json;
    assert.deepEqual([settings.DeletionPolicy, settings.UpdateReplacePolicy], ['Delete', 'Delete']);
  });

  test('the certificate custom resource carries the spec properties and is retained', async () => {
    const { template } = await synth(configWith(OPENLDAP_CONFIG));
    const certs = (template.Resources as Json).openldapservercerts as Json;
    assert.ok(certs, 'openldap-server-certs missing');
    assert.equal(certs.Type, 'Custom::SelfSignedCertificateOpenLDAPServer');
    // The running server is serving this pair, and the resource stays only to update in place.
    assert.equal(certs.DeletionPolicy, 'Retain');
    assert.equal(certs.UpdateReplacePolicy, 'Retain');
    assert.equal(certs.Properties.domain_name, 'openldap.idea.local');
    assert.equal(certs.Properties.certificate_name, `${CLUSTER}-${MODULE_ID}`);
    assert.equal(certs.Properties.create_acm_certificate, false);
    assert.equal(certs.Properties.kms_key_id, undefined);
    assert.deepEqual(certs.Properties.tags, {
      Name: `${CLUSTER}-${MODULE_ID}`,
      'idea:ClusterName': CLUSTER,
      'idea:ModuleName': MODULE_ID,
    });
  });

  test('the instance is tagged infra, backed up, and boots from the substituted user data', async () => {
    const { template } = await synth(configWith(OPENLDAP_CONFIG));
    const instance = (template.Resources as Json)[
      Object.keys(template.Resources as Json).find(
        (id) => ((template.Resources as Json)[id] as Json).Type === 'AWS::EC2::Instance',
      ) as string
    ] as Json;
    const tags = instance.Properties.Tags as Json[];
    assert.deepEqual(tags.find((tag) => tag.Key === 'idea:NodeType'), { Key: 'idea:NodeType', Value: 'infra' });
    assert.deepEqual(tags.find((tag) => tag.Key === 'Name'), { Key: 'Name', Value: `${CLUSTER}-${MODULE_ID}` });
    assert.deepEqual(tags.find((tag) => tag.Key === 'idea:BackupPlan'), {
      Key: 'idea:BackupPlan',
      Value: `${CLUSTER}-cluster`,
    });
    // amazonlinux2023 in the dev27 fixture
    assert.equal((instance.Properties.BlockDeviceMappings as Json[])[0]?.DeviceName, '/dev/xvda');
    assert.equal(instance.Properties.Monitoring, false);
    assert.equal(instance.Properties.DisableApiTermination, false);
    const userData = instance.Properties.UserData as Json;
    assert.ok(userData['Fn::Base64'], 'user data is not Fn::Base64');
    const body = JSON.stringify(userData['Fn::Base64']['Fn::Sub']);
    for (const marker of [
      'LDAP_ROOT_USERNAME_SECRET_ARN',
      'LDAP_ROOT_PASSWORD_SECRET_ARN',
      'LDAP_TLS_CERTIFICATE_SECRET_ARN',
      'LDAP_TLS_PRIVATE_KEY_SECRET_ARN',
      'openldap-server/setup.sh',
    ]) {
      assert.ok(body.includes(marker), `user data missing ${marker}`);
    }
    // The two TLS ARNs are the rows the deploy tool published, not attributes of the resource.
    const substitutions = userData['Fn::Base64']['Fn::Sub'][1] as Json;
    assert.equal(substitutions.__LDAP_TLS_CERTIFICATE_SECRET_ARN__, TLS_CERTIFICATE_ARN);
    assert.equal(substitutions.__LDAP_TLS_PRIVATE_KEY_SECRET_ARN__, TLS_PRIVATE_KEY_ARN);
  });

  test('the settings payload is the openldap key set, without public_ip', async () => {
    const { template } = await synth(configWith(OPENLDAP_CONFIG));
    const settings = settingsOf(template) as Json;
    assert.equal(settings.tls_certificate_secret_arn, TLS_CERTIFICATE_ARN);
    assert.equal(settings.tls_private_key_secret_arn, TLS_PRIVATE_KEY_ARN);
    assert.deepEqual(Object.keys(settingsOf(template)).sort(), [
      'deployment_id',
      'iam_role_arn',
      'instance_id',
      'instance_profile_arn',
      'private_dns_name',
      'private_ip',
      'root_password_secret_arn',
      'root_username_secret_arn',
      'security_group_id',
      'tls_certificate_secret_arn',
      'tls_private_key_secret_arn',
    ]);
  });

  test('directoryservice.public adds public_ip and an associated public address', async () => {
    const { template } = await synth(configWith({ ...OPENLDAP_CONFIG, 'directoryservice.public': { BOOL: true } }));
    assert.ok('public_ip' in settingsOf(template));
    const instance = (template.Resources as Json)[
      Object.keys(template.Resources as Json).find(
        (id) => ((template.Resources as Json)[id] as Json).Type === 'AWS::EC2::Instance',
      ) as string
    ] as Json;
    assert.equal((instance.Properties.NetworkInterfaces as Json[])[0]?.AssociatePublicIpAddress, true);
  });

  test('root_credentials_provided cannot synth: the settings read the secrets Python never made', async () => {
    // `build_openldap_cluster_settings` dereferences `self.openldap_credentials.admin_username.ref`
    // (directoryservice_stack.py:521-522), and `DirectoryServiceCredentials.__init__` returns at
    // directory_service.py:67 without assigning it. Python raises AttributeError; the port raises
    // TypeError reading `.ref` of the missing secret resource.
    await assert.rejects(
      synthRejects(configWith({ ...OPENLDAP_CONFIG, 'directoryservice.root_credentials_provided': { BOOL: true } })),
      {
        name: 'TypeError',
        message: "Cannot read properties of undefined (reading 'ref')",
      },
    );
  });

  test('the cdk_nag suppressions match Python, in Python order', async () => {
    const { template } = await synth(configWith(OPENLDAP_CONFIG));
    const byType = (type: string): string[] =>
      Object.keys(inventory(template)).filter((id) => ((template.Resources as Json)[id] as Json).Type === type);
    const [instanceId] = byType('AWS::EC2::Instance');
    // add_nag_suppression is called three times in this order: EC26 always, EC28 when detailed
    // monitoring is off, EC29 when termination protection is off. Both default to false.
    assert.deepEqual(nagSuppressions(template, instanceId as string), [
      'AwsSolutions-EC26',
      'AwsSolutions-EC28',
      'AwsSolutions-EC29',
    ]);
    const secrets = byType('AWS::SecretsManager::Secret');
    assert.equal(secrets.length, 2);
    for (const id of secrets) assert.deepEqual(nagSuppressions(template, id), ['AwsSolutions-SMG4']);
  });

  test('detailed monitoring and termination protection each drop their suppression', async () => {
    const { template } = await synth(
      configWith({
        ...OPENLDAP_CONFIG,
        'directoryservice.ec2.enable_detailed_monitoring': { BOOL: true },
        'directoryservice.ec2.enable_termination_protection': { BOOL: true },
      }),
    );
    const [instanceId] = Object.keys(inventory(template)).filter(
      (id) => ((template.Resources as Json)[id] as Json).Type === 'AWS::EC2::Instance',
    );
    assert.deepEqual(nagSuppressions(template, instanceId as string), ['AwsSolutions-EC26']);
  });

  test('a missing hostname fails the synth', async () => {
    await assert.rejects(synthRejects(configWith({ ...OPENLDAP_CONFIG, 'directoryservice.hostname': null })), /hostname/);
  });
});

// --- an unknown provider builds nothing --------------------------------------------------------

describe('directoryservice stack, unknown provider', () => {
  test('builds no resources at all, not even cluster settings', async () => {
    const { template } = await synth(configWith({ 'directoryservice.provider': { S: 'not-a-provider' } }));
    assert.deepEqual(inventory(template), {});
  });
});

describe('block device names', () => {
  test('amazon linux gets xvda, everything else sda1', () => {
    assert.equal(ec2BlockDeviceName('amazonlinux2'), '/dev/xvda');
    assert.equal(ec2BlockDeviceName('amazonlinux2023'), '/dev/xvda');
    assert.equal(ec2BlockDeviceName('rhel9'), '/dev/sda1');
    assert.equal(ec2BlockDeviceName('ubuntu2204'), '/dev/sda1');
  });
});
