/**
 * The identity-provider stack, synthesized through `app.ts` with the captured replay fixtures.
 *
 * The user pool, its domain and its two groups are stateful: a changed logical id, a changed
 * domain prefix or a lost `DeletionPolicy` is a replaced pool. The assertions below cover what a
 * template diff cannot: the stack-level manifest the CDK CLI deploys from, every resource's
 * deletion policies, that the domain prefix is stable rather than minted, and that a synth with
 * no replay of `cognito-idp:DescribeUserPool` fails instead of rewriting the invitation email.
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored, and this whole file requires them.
 */

import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { App } from 'aws-cdk-lib';

import { buildApp, type StackBuildProps } from '../../src/cdk/app.ts';
import { makeContext } from '../../src/cdk/constructs/base.ts';
import { replaySynthReads, userPoolKey } from '../../src/cdk/synth-reads.ts';
import { IdentityProviderStack, buildStack } from '../../src/cdk/stacks/identity-provider.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { ideaVersion } from '../../src/version.ts';
import { requireCapture } from '../support/fixtures.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { withRetirements } from '../support/retirements.ts';
import { NODE_NAG_SUPPRESSION, withNodeHandlers } from '../support/node-handlers.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.identity-provider', 'manifest.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-identity-provider.json');

const CLUSTER = 'idea-dev27';
const MODULE = 'identity-provider';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';
const STACK_NAME = `${CLUSTER}-${MODULE}`;

requireCapture(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE, PYTHON_MANIFEST],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];
const workdir = (prefix: string): string => {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  workdirs.push(dir);
  return dir;
};

after(() => {
  for (const dir of workdirs) rmSync(dir, { recursive: true, force: true });
});

/** Copies the dev27 settings scan, setting the `S` value of each named key (adding it if absent). */
function configWith(overrides: Record<string, string>, drop: string[] = []): string {
  const scan = readJson(CONFIG_FILE);
  const dropped = new Set(drop);
  const items = (scan.Items as Json[]).filter((item) => !dropped.has(item.key?.S as string));
  const remaining = new Set(Object.keys(overrides));
  for (const item of items) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item.value = { S: overrides[key] };
  }
  for (const key of remaining) {
    items.push({ key: { S: key }, value: { S: overrides[key] }, version: { N: '1' } });
  }
  scan.Items = items;
  const file = join(workdir('ideactl-idp-config-'), 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

/** Copies the dev27 synth reads, replacing the invitation template of the dev27 user pool. */
function synthReadsWith(invitation: { EmailSubject: string; EmailMessage: string } | null): string {
  const reads = readJson(SYNTH_READS);
  const key = userPoolKey(userPoolIdFromConfig());
  if (invitation === null) delete reads[key];
  else reads[key].AdminCreateUserConfig.InviteMessageTemplate = invitation;
  const file = join(workdir('ideactl-idp-reads-'), 'synth-reads.json');
  writeFileSync(file, JSON.stringify(reads));
  return file;
}

function configValue(key: string): string | undefined {
  const item = (readJson(CONFIG_FILE).Items as Json[]).find((row) => row.key?.S === key);
  return item?.value?.S as string | undefined;
}

const userPoolIdFromConfig = (): string => configValue('identity-provider.cognito.user_pool_id') as string;

interface SynthOptions {
  configFile?: string;
  synthReadsFile?: string;
  awsRegion?: string;
}

/**
 * Synthesizes the stack the way a deploy does: `idea-admin.sh:26` defaults
 * `IDEA_ADMIN_ENABLE_CDK_NAG_SCAN` to false, and the Python capture in the fixtures ran the same
 * way (no `aws:cdk:warning` / `aws:cdk:error` in any of its `*.metadata.json`). With the scan on,
 * cdk-nag 2.38.2's `AwsSolutions-COG8` fails the synth for both runtimes.
 */
async function synthIdentityProvider(options: SynthOptions = {}): Promise<{ template: Json; manifest: Json }> {
  const dir = workdir('ideactl-idp-');
  cpSync(CONTEXT_FILE, join(dir, 'cdk.context.json'));
  // The CDK CLI turns asset metadata on for us; in process it has to be asked for, or the two
  // lambdas lose their `aws:asset:*` metadata and the template stops matching what was deployed.
  const cdkJson = readJson(join(PKG, 'cdk.json'));
  cdkJson.context['aws:cdk:enable-asset-metadata'] = true;
  writeFileSync(join(dir, 'cdk.json'), JSON.stringify(cdkJson));
  const outdir = join(dir, `cdk.out.${MODULE}`);

  const previous = {
    cwd: process.cwd(),
    outdir: process.env.CDK_OUTDIR,
    nag: process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN,
  };
  const synthReadsFile = options.synthReadsFile ?? SYNTH_READS;
  process.chdir(dir);
  process.env.CDK_OUTDIR = outdir;
  process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = 'false';
  try {
    const app = await buildApp(
      {
        clusterName: CLUSTER,
        awsRegion: options.awsRegion ?? REGION,
        moduleId: MODULE,
        moduleName: MODULE,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: true,
        configFile: options.configFile ?? CONFIG_FILE,
        synthReadsFile,
      },
      { [MODULE]: async () => buildStack },
    );
    app.synth();
    return {
      template: readJson(join(outdir, `${STACK_NAME}.template.json`)),
      manifest: readJson(join(outdir, 'manifest.json')),
    };
  } finally {
    process.chdir(previous.cwd);
    for (const [name, value] of Object.entries({
      CDK_OUTDIR: previous.outdir,
      IDEA_ADMIN_ENABLE_CDK_NAG_SCAN: previous.nag,
    })) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

function buildProps(options: SynthOptions = {}): StackBuildProps {
  const config = ClusterConfig.fromFile(readFileSync(options.configFile ?? CONFIG_FILE, 'utf8'));
  return {
    // No CDK_OUTDIR here, so the app does not auto-synth at exit into a deleted directory.
    app: new App({ context: readJson(CONTEXT_FILE) }),
    ctx: makeContext({
      config,
      awsRegion: options.awsRegion ?? REGION,
      moduleId: MODULE,
      releaseVersion: ideaVersion(),
      synthReads: replaySynthReads(options.synthReadsFile ?? SYNTH_READS),
    }),
    moduleName: MODULE,
    deploymentId: DEPLOYMENT_ID,
    terminationProtection: true,
    env: { account: '123456789012', region: options.awsRegion ?? REGION },
  };
}

/**
 * Constructs the stack directly, without the `cognito-idp:DescribeUserPool` that `buildStack`
 * does, for the branches that throw before a template is written.
 */
function construct(options: SynthOptions = {}): IdentityProviderStack {
  return new IdentityProviderStack(buildProps(options));
}

/** Reads back and builds the stack the way `buildApp` does, without synthesizing. */
async function build(options: SynthOptions = {}): Promise<void> {
  await buildStack(buildProps(options));
}

/** Everything but `AWS::CDK::Metadata`, whose Analytics blob differs between the two runtimes. */
function deployedResources(template: Json): Json {
  return Object.fromEntries(
    Object.entries(template.Resources as Json).filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata'),
  );
}

/** `[rule id, reason]` per suppression, for every resource that carries one. */
function nagSuppressions(template: Json): Record<string, [string, string][]> {
  const carried = Object.entries(template.Resources as Json).flatMap(([id, resource]) => {
    const rules = (resource as Json).Metadata?.cdk_nag?.rules_to_suppress as Json[] | undefined;
    return rules === undefined ? [] : [[id, rules.map((rule) => [rule.id, rule.reason])] as const];
  });
  return Object.fromEntries(carried) as Record<string, [string, string][]>;
}

/** Lambda asset hashes are over the built package; they are a code update, not a replacement. */
function maskAssetHashes(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value).replace(/[0-9a-f]{64}/g, '<asset>')) as unknown;
}

/** `DeletionPolicy` / `UpdateReplacePolicy` per logical id, `null` where the attribute is absent. */
function deletionPolicies(template: Json): Record<string, [unknown, unknown]> {
  return Object.fromEntries(
    Object.entries(deployedResources(template)).map(([id, resource]) => [
      id,
      [(resource as Json).DeletionPolicy ?? null, (resource as Json).UpdateReplacePolicy ?? null],
    ]),
  );
}

describe('identity-provider stack, cognito-idp provider', () => {
  test('matches the deployed dev27 template, asset hashes aside', async () => {
    const { template } = await synthIdentityProvider();
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    assert.deepEqual(
      maskAssetHashes(deployedResources(template)),
      maskAssetHashes(
        deployedResources(withNodeHandlers(withRetirements('identity-provider', withRetainedStateful(readJson(LIVE_TEMPLATE))))),
      ),
    );
  });

  test('the cdk-nag suppressions are the deployed ones, resource for resource', async () => {
    // Every suppression is part of the deployed template, so a lost one and an invented one are
    // both a `cdk diff` on a stack that must not diff. Driven off the live template, so a
    // suppression removed from the stack fails here even if this list is never updated.
    const { template } = await synthIdentityProvider();
    const live = nagSuppressions(withNodeHandlers(withRetirements('identity-provider', readJson(LIVE_TEMPLATE))));
    assert.deepEqual(nagSuppressions(template), live);

    // Spelled out, so a change to both sides at once still fails.
    assert.deepEqual(live.ideadev27userpoolD5C370B5, [
      ['AwsSolutions-COG2', 'Suppress MFA warning. MFA provided by customer IdP/SSO methods.'],
      ['AwsSolutions-COG3', 'suppress advanced security rule 1/to save cost, 2/Not supported in GovCloud'],
    ]);
    // The one ported handler left in this stack carries the Node suppression in place of the
    // Python pair.
    assert.deepEqual(live.idtokenclaim18B64AB5, [NODE_NAG_SUPPRESSION]);

    // cdk-nag raises AwsSolutions-COG8 on the pool (no PLUS feature plan) and neither
    // administrator suppresses it: idea-admin.sh defaults IDEA_ADMIN_ENABLE_CDK_NAG_SCAN to
    // false, so no deploy has ever run the scan. A suppression here would add a rule to a
    // Metadata block the deployed pool does not have.
    for (const [id, reason] of live.ideadev27userpoolD5C370B5) {
      assert.notEqual(id, 'AwsSolutions-COG8', reason);
    }
  });

  test('every deletion policy is what the live stack has, including the ones it does not have', async () => {
    const { template } = await synthIdentityProvider();
    const live = withRetirements('identity-provider', withRetainedStateful(readJson(LIVE_TEMPLATE)));
    assert.deepEqual(deletionPolicies(template), deletionPolicies(live));

    // Spelled out, so a change to both sides at once still fails. The pool still deletes with the
    // stack, and is never lost to a replacement: it holds every account and every enrolment.
    const policies = deletionPolicies(template);
    assert.deepEqual(policies.ideadev27userpoolD5C370B5, ['Delete', 'Retain']);
    assert.deepEqual(policies.ideadev27identityprovidersettings, ['Delete', 'Delete']);
    for (const id of [
      'ideadev27userpooldomain237CD715',
      'ideadev27userpooladministratorsgroup',
      'ideadev27userpoolmanagersgroup',
    ]) {
      assert.deepEqual(policies[id], [null, 'Retain'], id);
    }
    assert.deepEqual(policies.idtokenclaimroleF1630968, [null, null]);
  });

  test('removal_policy RETAIN reaches both policies on the pool', async () => {
    const configFile = configWith({ 'identity-provider.cognito.removal_policy': 'RETAIN' });
    const { template } = await synthIdentityProvider({ configFile });
    const policies = deletionPolicies(template);
    assert.deepEqual(policies.ideadev27userpoolD5C370B5, ['Retain', 'Retain']);
    // Only the pool follows the config key: the settings resource keeps the CDK default.
    assert.deepEqual(policies.ideadev27identityprovidersettings, ['Delete', 'Delete']);
  });

  test('the stack manifest matches the one Python deployed from', async () => {
    const { manifest } = await synthIdentityProvider();
    const artifact = manifest.artifacts[STACK_NAME] as Json;
    const expected = readJson(PYTHON_MANIFEST).artifacts[STACK_NAME] as Json;

    assert.equal(artifact.type, 'aws:cloudformation:stack');
    assert.equal(artifact.environment, expected.environment);
    assert.equal(artifact.displayName, STACK_NAME);
    // None of these is visible in the template: the deploy role, the bootstrap version gate, the
    // stack tags CloudFormation stores, and the termination protection that guards the pool.
    for (const key of [
      'terminationProtection',
      'tags',
      'validateOnSynth',
      'assumeRoleArn',
      'cloudFormationExecutionRoleArn',
      'requiresBootstrapStackVersion',
      'bootstrapStackVersionSsmParameter',
      'lookupRole',
    ]) {
      assert.deepEqual(artifact.properties[key], expected.properties[key], key);
    }
    assert.equal(artifact.properties.terminationProtection, true);
    assert.deepEqual(artifact.properties.tags, {
      'idea:ClusterName': CLUSTER,
      'idea:ModuleId': MODULE,
      'idea:ModuleName': MODULE,
      'idea:ModuleVersion': ideaVersion(),
    });
  });

  test('the domain prefix comes from config and is identical on a second synth', async () => {
    const domainUrl = configValue('identity-provider.cognito.domain_url') as string;
    const prefix = domainUrl.replace('https://', '').split('.')[0] as string;

    const first = await synthIdentityProvider();
    const second = await synthIdentityProvider();
    const domainOf = (t: Json): string =>
      deployedResources(t).ideadev27userpooldomain237CD715.Properties.Domain as string;

    assert.equal(domainOf(first.template), prefix);
    assert.equal(domainOf(second.template), prefix);
    assert.equal(domainOf(first.template), domainOf(readJson(LIVE_TEMPLATE)));
    // A minted `<cluster>-<uuid4>` would differ between the two synths.
    assert.equal(domainOf(first.template), domainOf(second.template));
  });

  test('the invitation email is the one read back from the pool, not the generated one', async () => {
    const invitation = {
      EmailSubject: 'Welcome to the cluster',
      EmailMessage: '<p>An operator edited this in the console: {username} {####}</p>',
    };
    const { template } = await synthIdentityProvider({ synthReadsFile: synthReadsWith(invitation) });
    const pool = deployedResources(template).ideadev27userpoolD5C370B5;
    assert.deepEqual(pool.Properties.AdminCreateUserConfig.InviteMessageTemplate, invitation);

    // And the dev27 fixture's own values reach the template unchanged.
    const fromLive = readJson(LIVE_TEMPLATE).Resources.ideadev27userpoolD5C370B5.Properties
      .AdminCreateUserConfig.InviteMessageTemplate as Json;
    const { template: unchanged } = await synthIdentityProvider();
    assert.deepEqual(
      deployedResources(unchanged).ideadev27userpoolD5C370B5.Properties.AdminCreateUserConfig
        .InviteMessageTemplate,
      fromLive,
    );
  });

  test('the cluster settings carry the four keys downstream modules read', async () => {
    const { template } = await synthIdentityProvider();
    const settings = deployedResources(template).ideadev27identityprovidersettings;
    assert.equal(settings.Type, 'Custom::ClusterSettings');
    assert.deepEqual(Object.keys(settings.Properties.settings), [
      'deployment_id',
      'cognito.user_pool_id',
      'cognito.provider_url',
      'cognito.domain_url',
    ]);
    assert.deepEqual(settings.Properties.settings['cognito.user_pool_id'], {
      Ref: 'ideadev27userpoolD5C370B5',
    });
    assert.deepEqual(settings.Properties.settings['cognito.domain_url'], {
      'Fn::Join': [
        '',
        ['https://', { Ref: 'ideadev27userpooldomain237CD715' }, '.auth.us-east-2.amazoncognito.com'],
      ],
    });
    assert.deepEqual(settings.Properties.settings['cognito.provider_url'], {
      'Fn::GetAtt': ['ideadev27userpoolD5C370B5', 'ProviderURL'],
    });
  });
});

describe('identity-provider stack, synth-time read-back', () => {
  test('a stack built without the read fails instead of regenerating the invitation email', () => {
    assert.throws(
      () => construct(),
      /cognito-idp:DescribeUserPool for user pool .* is required at synth time and was not read/,
    );
  });

  test('a replay that does not carry the read fails loudly', async () => {
    await assert.rejects(() => build({ synthReadsFile: synthReadsWith(null) }), /SynthReadMiss/);
  });

  test('the read comes from the SynthReads the app was built with, not from argv or the environment', async () => {
    const wanted = { EmailSubject: 'From the app', EmailMessage: '<p>{username} {####}</p>' };
    const wantedFile = synthReadsWith(wanted);
    const decoy = synthReadsWith({ EmailSubject: 'From a stale flag', EmailMessage: '<p>no</p>' });

    const previousArgv = process.argv;
    const previousEnv = process.env.IDEA_SYNTH_READS_FILE;
    process.argv = [...previousArgv, '--synth-reads', decoy];
    process.env.IDEA_SYNTH_READS_FILE = decoy;
    try {
      const { template } = await synthIdentityProvider({ synthReadsFile: wantedFile });
      const pool = deployedResources(template).ideadev27userpoolD5C370B5;
      assert.deepEqual(pool.Properties.AdminCreateUserConfig.InviteMessageTemplate, wanted);
    } finally {
      process.argv = previousArgv;
      if (previousEnv === undefined) delete process.env.IDEA_SYNTH_READS_FILE;
      else process.env.IDEA_SYNTH_READS_FILE = previousEnv;
    }
  });
});

describe('identity-provider stack, other branches', () => {
  test('a cluster with no user pool yet generates the invitation and mints a domain', async () => {
    const configFile = configWith({}, [
      'identity-provider.cognito.user_pool_id',
      'identity-provider.cognito.domain_url',
    ]);
    const first = await synthIdentityProvider({ configFile });
    const second = await synthIdentityProvider({ configFile });
    const resources = deployedResources(first.template);
    const albDns = configValue('cluster.load_balancers.external_alb.load_balancer_dns_name') as string;
    const endpoint = `https://${albDns}`;
    const domainOf = (t: Json): string =>
      deployedResources(t).ideadev27userpooldomain237CD715.Properties.Domain as string;

    assert.deepEqual(
      resources.ideadev27userpoolD5C370B5.Properties.AdminCreateUserConfig.InviteMessageTemplate,
      {
        EmailSubject: `Invitation to Join IDEA Cluster: ${CLUSTER}`,
        EmailMessage: [
          '<p>Hello <b>{username},</b></p>',
          `<p>You have been invited to join the <b>${CLUSTER}</b> cluster.</p>`,
          '<p>Your temporary password is:</p>',
          '<h3>{####}</h3>',
          '<p>You can sign in to your account using the link below: <br/>',
          `<a href="${endpoint}">${endpoint}</a></p>`,
          '<p>---<br/>',
          '<b>IDEA Cluster Admin</b></p>',
        ].join('\n'),
      },
    );
    assert.match(
      domainOf(first.template),
      /^idea-dev27-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    assert.notEqual(domainOf(first.template), domainOf(second.template));
  });

  // Python has no guard for this state: `build_cognito_idp` never looks at `domain_url`, and
  // `directory_service.py:518-529` mints `f'{cluster_name}-{Utils.uuid()}'` whenever the key is
  // empty, whether or not a pool already exists. Replacing the deployed domain is a real hazard,
  // but refusing to synth is behaviour Python does not have, so the port does not add it.
  test('an existing pool with an empty domain url still mints a fresh prefix, as Python does', async () => {
    const configFile = configWith({}, ['identity-provider.cognito.domain_url']);
    const stored = (configValue('identity-provider.cognito.domain_url') as string)
      .replace('https://', '')
      .split('.')[0] as string;

    const first = await synthIdentityProvider({ configFile });
    const second = await synthIdentityProvider({ configFile });
    const domainOf = (t: Json): string =>
      deployedResources(t).ideadev27userpooldomain237CD715.Properties.Domain as string;

    assert.match(
      domainOf(first.template),
      /^idea-dev27-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    assert.notEqual(domainOf(first.template), domainOf(second.template));
    assert.notEqual(domainOf(first.template), stored);
    // The pool itself is untouched: the invitation email still comes from the replayed read.
    assert.equal(
      deployedResources(first.template).ideadev27userpoolD5C370B5.Properties.AdminCreateUserConfig
        .InviteMessageTemplate.EmailSubject,
      readJson(SYNTH_READS)[userPoolKey(userPoolIdFromConfig())].AdminCreateUserConfig
        .InviteMessageTemplate.EmailSubject,
    );
  });

  test('a FIPS region writes the fips domain url into the cluster settings', async () => {
    const { template } = await synthIdentityProvider({ awsRegion: 'us-gov-west-1' });
    const settings = deployedResources(template).ideadev27identityprovidersettings;
    assert.deepEqual(settings.Properties.settings['cognito.domain_url'], {
      'Fn::Join': [
        '',
        [
          'https://',
          { Ref: 'ideadev27userpooldomain237CD715' },
          '.auth-fips.us-gov-west-1.amazoncognito.com',
        ],
      ],
    });
  });

  test('keycloak is rejected as not supported yet', () => {
    const configFile = configWith({ 'identity-provider.provider': 'keycloak' });
    assert.throws(() => construct({ configFile }), /identity provider: keycloak not supported \(yet\)\./);
  });

  test('an unknown provider is rejected', () => {
    const configFile = configWith({ 'identity-provider.provider': 'okta' });
    assert.throws(() => construct({ configFile }), /identity provider: okta not supported$/m);
  });
});
