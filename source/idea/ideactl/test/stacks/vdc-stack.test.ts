/**
 * The virtual desktop controller (`vdc`) stack, synthesized through `app.ts` with the captured
 * replay fixtures.
 *
 * The parity harness compares the template. These tests cover what a template diff cannot see:
 * the cloud-assembly manifest the CDK CLI deploys from (termination protection, stack tags,
 * environment, the bootstrap qualifier), the removal policy on every stateful resource, and the
 * config branches dev27 does not exercise (QUIC, a provided gateway certificate, GovCloud).
 *
 * Fixtures under tools/parity/{fixtures,live} are gitignored; every test that needs them skips
 * when they are absent so the suite still runs without them.
 */

import assert from 'node:assert/strict';
import { cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, describe, test } from 'node:test';

import { buildApp } from '../../src/cdk/app.ts';
import { buildStack } from '../../src/cdk/stacks/vdc.ts';
import {
  ECS_CAPACITY_PROVIDER,
  ECS_CPU_ARCHITECTURE,
  ECS_GATEWAY_TASK_DESIRED,
  ECS_SHARED_CAPACITY,
  TARGET_GROUP_HASH,
} from '../support/ecs-harness.ts';
import { optionalFixtures, requireCapture } from '../support/fixtures.ts';
import { withCertificateRows, withRetainedCertificates } from '../support/retained-certificates.ts';
import { withRetainedStateful } from '../support/retain-stateful.ts';
import { withRetirements } from '../support/retirements.ts';
import { withNodeHandlers } from '../support/node-handlers.ts';
import { settingsLookup } from '../../tools/parity/intended-drift.ts';
import { cacheSetup } from '../support/setup-cache.ts';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');
const LIVE_TEMPLATE = join(PKG, 'tools', 'parity', 'live', 'idea-dev27-vdc.json');
const PYTHON_MANIFEST = join(FIXTURES, 'python', '_cdk', 'cdk.out.vdc', 'manifest.json');

const CLUSTER = 'idea-dev27';
const REGION = 'us-east-2';
const MODULE_ID = 'vdc';
const MODULE_NAME = 'virtual-desktop-controller';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';
const STACK_NAME = `${CLUSTER}-${MODULE_ID}`;

/**
 * Every captured Python vdc template other than this cluster's, discovered rather than named: the
 * fixture directories are gitignored and their cluster names do not belong in a public file. Used
 * read-only, to cross-check the QUIC-on shape against templates Python actually produced.
 */
function pythonVdcOracles(root: string): { cluster: string; template: string }[] {
  return readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name !== CLUSTER)
    .map((entry) => ({
      cluster: entry.name,
      template: join(root, entry.name, 'python', '_cdk', 'cdk.out.vdc', `${entry.name}-vdc.template.json`),
    }))
    .filter((oracle) => existsSync(oracle.template));
}

requireCapture(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE, LIVE_TEMPLATE, PYTHON_MANIFEST],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);
const PYTHON_ORACLE_ROOT = join(PKG, 'tools', 'parity', 'fixtures');
const runPythonOracleCoverage = optionalFixtures(
  [PYTHON_ORACLE_ROOT],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

type Json = Record<string, any>;

const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
});

function scratch(prefix: string): string {
  const workdir = mkdtempSync(join(tmpdir(), prefix));
  workdirs.push(workdir);
  return workdir;
}

/** Copies the dev27 settings scan, replacing the typed value of each named key (adding it if absent). */
function configWith(overrides: Record<string, Json>): string {
  const scan = readJson(CONFIG_FILE);
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) item.value = overrides[key];
  }
  for (const key of remaining) {
    (scan.Items as Json[]).push({ key: { S: key }, value: overrides[key], version: { N: '1' } });
  }
  const file = join(scratch('ideactl-vdc-config-'), 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

interface SynthOptions {
  configFile?: string;
  terminationProtection?: boolean;
  /** Extra CDK context, merged over the captured `cdk.context.json`. */
  context?: Record<string, unknown>;
  /**
   * Leaves the working directory behind. A build that throws leaves an un-synthesized `App`, whose
   * exit-time autosynth would otherwise write into a directory this suite has already removed.
   */
  keepWorkdir?: boolean;
}

/** Synthesizes the stack and returns both the template and the manifest artifact for it. */
async function synthVdcUncached(options: SynthOptions = {}): Promise<{ template: Json; artifact: Json }> {
  const workdir =
    options.keepWorkdir === true
      ? mkdtempSync(join(tmpdir(), 'ideactl-vdc-kept-'))
      : scratch('ideactl-vdc-');
  const context = { ...readJson(CONTEXT_FILE), ...(options.context ?? {}) };
  writeFileSync(join(workdir, 'cdk.context.json'), JSON.stringify(context));
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
        moduleName: MODULE_NAME,
        deploymentId: DEPLOYMENT_ID,
        terminationProtection: options.terminationProtection ?? true,
        configFile: options.configFile ?? CONFIG_FILE,
        synthReadsFile: SYNTH_READS,
      },
      { [MODULE_NAME]: async () => buildStack },
    );
    app.synth();
    return {
      template: readJson(join(outdir, `${STACK_NAME}.template.json`)),
      artifact: (readJson(join(outdir, 'manifest.json')).artifacts as Json)[STACK_NAME] as Json,
    };
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
  }
}

const synthDefaultVdc = cacheSetup(async () => synthVdcUncached());

/**
 * Reuses the immutable default-fixture synthesis and clones its plain JSON
 * result. Every configuration or manifest branch still synthesizes fresh.
 */
async function synthVdc(options: SynthOptions = {}): Promise<{ template: Json; artifact: Json }> {
  if (Object.keys(options).length === 0) {
    return synthDefaultVdc();
  }
  return synthVdcUncached(options);
}

/** Everything but `AWS::CDK::Metadata`, whose Analytics blob differs between the two runtimes. */
function deployedResources(template: Json): Json {
  return Object.fromEntries(
    Object.entries(template.Resources as Json).filter(([, r]) => (r as Json).Type !== 'AWS::CDK::Metadata'),
  );
}

function resourcesOfType(template: Json, type: string): Json {
  return Object.fromEntries(
    Object.entries(template.Resources as Json).filter(([, r]) => (r as Json).Type === type),
  );
}

/**
 * The removal policy on every resource that survives a stack update, spelled out rather than read
 * back from the capture. The deployed template has no `Retain` anywhere: a logical-id change on
 * any of these *deletes* the resource, which for the Cognito client secret and the two Secrets
 * Manager secrets means every running vdc process loses its credentials.
 */
const DELETE_ON_REPLACE = [
  'controllerendpointext',
  'controllerendpointint',
  'dcvbrokeragentendpoint',
  'dcvbrokerclientendpoint',
  'dcvbrokergatewayendpoint',
  'ideadev27vdcsettings',
  'vdcclientid',
  'vdcclientsecret',
  'virtualdesktopcontrollereventsqueue3DC7EB29',
  'virtualdesktopcontrollereventsqueuedlqB772F1E2',
  'virtualdesktopcontrollerqueue704023D5',
  'virtualdesktopcontrollerqueuedlq328258C4',
];

/** The defused certificate custom resource, the one thing in this stack retained on delete. */
const RETAINED_CERTIFICATE = 'ideadev27vdcexternalcertgateway';

/** Stateful, but on the CloudFormation default: no policy is rendered at all. */
const NO_REMOVAL_POLICY = [
  'ideadev27userpoolresourceserver7B2B7736',
  'ideadev27userpooldcvsessionmanagerresourceserver7E658EA5',
  'ideadev27userpoolvdcclientC7AFCDDD',
  'vdcadministratorsgroup',
  'vdcusersgroup',
];

describe('vdc stack, dev27', () => {
  test('matches the deployed template resource for resource', async () => {
    const { template } = await synthVdc();
    const synthesized = deployedResources(template);
    // The deployed side carries this branch's retain policy on its stateful resources, the one
    // change to these attributes, itemised in tools/parity/intended-drift.ts.
    const live = deployedResources(
      withCertificateRows(
        withRetainedCertificates(withNodeHandlers(withRetirements('vdc', withRetainedStateful(readJson(LIVE_TEMPLATE))))),
        CONFIG_FILE,
      ),
    );

    // The Lambda asset hash is computed from the package contents by each runtime's own
    // algorithm; the parity harness reports it as a soft difference. Everything else is compared.
    const lambdaId = 'vdcscheduledeventtransformer32EA7695';
    assert.match(
      (synthesized[lambdaId] as Json).Properties.Code.S3Key as string,
      /^cdk\/[0-9a-f]{64}\.zip$/,
    );
    (synthesized[lambdaId] as Json).Properties.Code.S3Key = (live[lambdaId] as Json).Properties.Code
      .S3Key;
    // `aws:asset:*` metadata is added by the CDK CLI, not by `app.synth()`, and no comparison
    // reads it: `parity.ts` keeps only `aws:cdk:path` out of `Metadata`.
    for (const side of [synthesized, live]) {
      const metadata = (side[lambdaId] as Json).Metadata as Json;
      for (const key of Object.keys(metadata)) {
        if (key.startsWith('aws:asset:')) delete metadata[key];
      }
    }

    assert.deepEqual(synthesized, live);
    assert.equal(Object.keys(synthesized).length, 78);
  });

  test('the deploy-time manifest matches the one Python wrote', async () => {
    const { artifact } = await synthVdc();
    const expected = readJson(PYTHON_MANIFEST).artifacts[STACK_NAME] as Json;

    assert.equal(artifact.type, expected.type);
    assert.equal(artifact.environment, expected.environment);
    assert.equal(artifact.displayName, expected.displayName);

    // The template asset hash covers the template body, which the resource comparison already
    // pins; every other property drives how the CLI deploys the stack.
    const volatile = new Set(['stackTemplateAssetObjectUrl']);
    const strip = (properties: Json): Json =>
      Object.fromEntries(Object.entries(properties).filter(([key]) => !volatile.has(key)));
    assert.deepEqual(strip(artifact.properties as Json), strip(expected.properties as Json));

    // and the parts of it worth naming, so a regression reads as itself
    const properties = artifact.properties as Json;
    assert.equal(properties.terminationProtection, true);
    assert.equal(properties.bootstrapStackVersionSsmParameter, '/cdk-bootstrap/6f3b37a775/version');
    assert.deepEqual(properties.tags, {
      'idea:ClusterName': CLUSTER,
      'idea:ModuleId': MODULE_ID,
      'idea:ModuleName': MODULE_NAME,
      'idea:ModuleVersion': (readJson(LIVE_TEMPLATE).Description as string).split('Version: ')[1],
    });
  });

  test('termination protection is a manifest property, invisible in the template', async () => {
    const protectedStack = await synthVdc();
    const unprotectedStack = await synthVdc({ terminationProtection: false });
    assert.equal((protectedStack.artifact.properties as Json).terminationProtection, true);
    assert.equal((unprotectedStack.artifact.properties as Json).terminationProtection, false);
    assert.deepEqual(
      deployedResources(protectedStack.template),
      deployedResources(unprotectedStack.template),
    );
  });

  test('every stateful resource keeps its deployed removal policy', async () => {
    const { template } = await synthVdc();
    const resources = template.Resources as Json;
    // The teardown side is asserted by value, because nothing here may become Retain on delete.
    // The update-replace side is held against the deployed template with the retain policy this
    // branch adds, so a stateful resource that lost it fails and a stateless one that gained it
    // fails too.
    const live = (withRetirements('vdc', withRetainedStateful(readJson(LIVE_TEMPLATE))) as Json).Resources as Json;

    // The gateway certificate resource is defused rather than deleted, so both policies retain:
    // the running gateway is serving the pair its secrets name.
    assert.equal((resources[RETAINED_CERTIFICATE] as Json).DeletionPolicy, 'Retain');
    assert.equal((resources[RETAINED_CERTIFICATE] as Json).UpdateReplacePolicy, 'Retain');

    for (const id of DELETE_ON_REPLACE) {
      const resource = resources[id] as Json | undefined;
      assert.ok(resource !== undefined, `${id} is missing from the synthesized template`);
      assert.equal(resource.DeletionPolicy, 'Delete', `${id}.DeletionPolicy`);
      assert.equal(resource.UpdateReplacePolicy, (live[id] as Json).UpdateReplacePolicy, `${id}.UpdateReplacePolicy`);
    }

    for (const id of NO_REMOVAL_POLICY) {
      const resource = resources[id] as Json | undefined;
      assert.ok(resource !== undefined, `${id} is missing from the synthesized template`);
      assert.equal(resource.DeletionPolicy, undefined, `${id}.DeletionPolicy`);
      assert.equal(resource.UpdateReplacePolicy, (live[id] as Json).UpdateReplacePolicy, `${id}.UpdateReplacePolicy`);
    }

    // the certificate resource is the only thing in this stack retained on delete, and nothing
    // else carries a policy at all
    const carriers = Object.entries(resources)
      .filter(([, r]) => (r as Json).DeletionPolicy !== undefined)
      .map(([id]) => id);
    assert.deepEqual(carriers.sort(), [...DELETE_ON_REPLACE, RETAINED_CERTIFICATE].sort());
    assert.deepEqual(
      carriers.filter((id) => (resources[id] as Json).DeletionPolicy !== 'Delete'),
      [RETAINED_CERTIFICATE],
    );
  });

  test('the broker agent endpoint keeps the client endpoint name, and the target group order holds', async () => {
    const { template } = await synthVdc();
    const resources = template.Resources as Json;

    assert.equal(
      (resources.dcvbrokeragentendpoint as Json).Properties.endpoint_name,
      'broker-client-endpoint',
    );
    assert.equal(
      (resources.dcvbrokerclientendpoint as Json).Properties.endpoint_name,
      'broker-client-endpoint',
    );
    assert.equal(
      (resources.dcvbrokergatewayendpoint as Json).Properties.endpoint_name,
      'broker-gateway-endpoint',
    );

    assert.deepEqual((resources.controllerasgASGDDF54A55 as Json).Properties.TargetGroupARNs, [
      { Ref: 'controllertargetgroupintE01106BF' },
      { Ref: 'controllertargetgroupext63F85C5A' },
    ]);
    assert.deepEqual((resources.brokerasgASG24CAB1DF as Json).Properties.TargetGroupARNs, [
      { Ref: 'brokeragenttargetgroup63081663' },
      { Ref: 'brokerclienttargetgroupE49CEB15' },
      { Ref: 'brokergatewaytargetgroup26708C7F' },
    ]);
    // the gateway ASG is attached through the target group's `targets`, not by the L1 override
    assert.deepEqual((resources.gatewayasgASG04E7FFF1 as Json).Properties.TargetGroupARNs, [
      { Ref: 'dcvconnectiongatewaytargetgroupnlbEAFF76CA' },
    ]);
  });

  test('the controller role default policy passes both roles, in call order', async () => {
    const { template } = await synthVdc();
    const policy = (template.Resources as Json).vdccontrollerroleDefaultPolicy5F94E7B2 as Json;
    assert.equal(policy.Type, 'AWS::IAM::Policy');
    // an IAM policy with no explicit name is named after its own logical id
    assert.equal(policy.Properties.PolicyName, 'vdccontrollerroleDefaultPolicy5F94E7B2');
    assert.deepEqual(policy.Properties.PolicyDocument.Statement, [
      {
        Action: 'iam:PassRole',
        Effect: 'Allow',
        Resource: { 'Fn::GetAtt': ['vdchostrole47D92D67', 'Arn'] },
      },
      {
        Action: 'iam:PassRole',
        Effect: 'Allow',
        Resource: { 'Fn::GetAtt': ['vdcssmcommandssnstopicrole8102386D', 'Arn'] },
      },
    ]);
  });

  test('the cluster settings hold every key the module reads back, in order', async () => {
    const { template } = await synthVdc();
    const settings = (template.Resources as Json).ideadev27vdcsettings as Json;
    assert.equal(settings.Properties.module_id, MODULE_ID);
    assert.equal(settings.Properties.settings.deployment_id, DEPLOYMENT_ID);
    assert.deepEqual(
      Object.keys(settings.Properties.settings as Json),
      Object.keys(
        ((readJson(LIVE_TEMPLATE).Resources as Json).ideadev27vdcsettings as Json).Properties
          .settings as Json,
      ),
    );
    // built by string concatenation, not GetAtt: the runtime parses this shape
    const instanceProfileArn = settings.Properties.settings.dcv_host_instance_profile_arn as Json;
    assert.equal(
      (instanceProfileArn['Fn::Join'] as Json[])[1][1].Ref,
      'vdchostinstanceprofile',
    );
  });
});

describe('vdc stack, config branches dev27 does not deploy', () => {
  test('QUIC support renames the gateway target group and opens UDP egress', async () => {
    const { template } = await synthVdc({
      configFile: configWith({ 'vdc.dcv_session.quic_support': { BOOL: true } }),
    });
    const resources = template.Resources as Json;

    const targetGroup = resources.dcvconnectiongatewaytargetgroupnlbEAFF76CA as Json;
    assert.equal(targetGroup.Properties.Protocol, 'TCP_UDP');
    // TUN, not TN: the name changes, which replaces the target group
    assert.equal(targetGroup.Properties.Name, `${CLUSTER}-gateway-TUN-e8356b3f`);
    assert.equal(
      (resources.ideadev27vdcexternalnlbdcvconnectiongatewaynlblistenerBF44B455 as Json).Properties
        .Protocol,
      'TCP_UDP',
    );

    // the two QUIC rules land last on each group, after the TCP (and, on the host, the AD) rules
    const trailingEgress = (id: string): Json[] =>
      ((resources[id] as Json).Properties.SecurityGroupEgress as Json[]).slice(-2);

    const expected = (component: string): Json[] => [
      {
        CidrIp: '0.0.0.0/0',
        Description: `Allow all egress for UDP for QUIC Support on ${component}`,
        FromPort: 0,
        IpProtocol: 'udp',
        ToPort: 65535,
      },
      {
        CidrIpv6: '::/0',
        Description: `Allow all egress for UDP for QUIC Support on ${component}`,
        FromPort: 0,
        IpProtocol: 'udp',
        ToPort: 65535,
      },
    ];

    assert.deepEqual(trailingEgress('vdcdcvhostsecuritygroup96258805'), expected('DCV Host'));
    assert.deepEqual(
      trailingEgress('vdcgatewaysecuritygroup4695D876'),
      expected('DCV Connection Gateway'),
    );

    // the captured Python templates that deploy with QUIC on say the same thing
    const oracles = runPythonOracleCoverage ? pythonVdcOracles(PYTHON_ORACLE_ROOT) : [];
    if (runPythonOracleCoverage) {
      assert.ok(oracles.length > 0, `Required optional oracle coverage has no VDC templates: ${PYTHON_ORACLE_ROOT}`);
    }
    for (const oracle of oracles) {
      const python = readJson(oracle.template).Resources as Json;
      const gatewayTargetGroup = python.dcvconnectiongatewaytargetgroupnlbEAFF76CA as Json;
      if (gatewayTargetGroup.Properties.Protocol !== 'TCP_UDP') continue;
      assert.equal(
        (python.dcvconnectiongatewaytargetgroupnlbEAFF76CA as Json).Properties.Protocol,
        'TCP_UDP',
        `${oracle.cluster}: gateway target group protocol`,
      );
      assert.match(
        (python.dcvconnectiongatewaytargetgroupnlbEAFF76CA as Json).Properties.Name as string,
        /-gateway-TUN-[0-9a-f]{8}$/,
        `${oracle.cluster}: gateway target group name`,
      );
      for (const [id, component] of [
        ['vdcdcvhostsecuritygroup96258805', 'DCV Host'],
        ['vdcgatewaysecuritygroup4695D876', 'DCV Connection Gateway'],
      ] as const) {
        const egress = (python[id] as Json).Properties.SecurityGroupEgress as Json[];
        assert.deepEqual(
          egress.slice(-2).map((rule) => rule.Description),
          [
            `Allow all egress for UDP for QUIC Support on ${component}`,
            `Allow all egress for UDP for QUIC Support on ${component}`,
          ],
          `${oracle.cluster}: ${id} QUIC egress`,
        );
      }
    }
  });

  test('a provided gateway certificate drops the self-signed resource', async () => {
    const certificateSecretArn = 'arn:aws:secretsmanager:us-east-2:123456789012:secret:cert-AbCdEf';
    const privateKeySecretArn = 'arn:aws:secretsmanager:us-east-2:123456789012:secret:key-AbCdEf';
    const { template } = await synthVdc({
      configFile: configWith({
        'vdc.dcv_connection_gateway.certificate.provided': { BOOL: true },
        'vdc.dcv_connection_gateway.certificate.certificate_secret_arn': { S: certificateSecretArn },
        'vdc.dcv_connection_gateway.certificate.private_key_secret_arn': { S: privateKeySecretArn },
        'vdc.dcv_connection_gateway.certificate.custom_dns_name': { S: 'vdc.example.invalid' },
      }),
    });
    const resources = template.Resources as Json;

    assert.equal(
      Object.keys(resourcesOfType(template, 'Custom::SelfSignedCertificateConnectionGateway')).length,
      0,
    );

    const settings = (resources.ideadev27vdcsettings as Json).Properties.settings as Json;
    // pyhocon's `ConfigTree.get_string` lowercases a boolean, so the setting is written as text
    assert.equal(settings['dcv_connection_gateway.certificate.provided'], 'true');
    assert.equal(
      settings['dcv_connection_gateway.certificate.certificate_secret_arn'],
      certificateSecretArn,
    );
    assert.equal(
      settings['dcv_connection_gateway.certificate.private_key_secret_arn'],
      privateKeySecretArn,
    );
    assert.equal(
      settings['dcv_connection_gateway.certificate.custom_dns_name'],
      'vdc.example.invalid',
    );

    // the gateway user data substitutes the configured ARNs instead of the custom resource's
    const userData = (resources.gatewaylt3BC4A74E as Json).Properties.LaunchTemplateData.UserData;
    assert.deepEqual(userData['Fn::Base64']['Fn::Sub'][1], {
      __CERTIFICATE_SECRET_ARN__: certificateSecretArn,
      __PRIVATE_KEY_SECRET_ARN__: privateKeySecretArn,
    });
  });

  test('GovCloud drops the tags CloudFormation will not accept on an events rule', async () => {
    const { template } = await synthVdc({
      configFile: configWith({ 'cluster.aws.partition': { S: 'aws-us-gov' } }),
    });
    const rule = (template.Resources as Json).ideadev27vdcscheduleruleBCD27E01 as Json;
    assert.equal(rule.Type, 'AWS::Events::Rule');
    assert.equal(rule.Properties.Tags, undefined);
    assert.equal(rule.Properties.Name, `${CLUSTER}-${MODULE_ID}-schedule-rule`);

    // and the commercial partition keeps them
    const commercial = await synthVdc();
    assert.deepEqual(
      ((commercial.template.Resources as Json).ideadev27vdcscheduleruleBCD27E01 as Json).Properties
        .Tags,
      [
        { Key: 'idea:ClusterName', Value: CLUSTER },
        { Key: 'Name', Value: `${CLUSTER}-${MODULE_ID}` },
      ],
    );
  });

  // A literal placeholder renders into host user data, and the host then tries to download a
  // package by that name. Each component derives its location from the deployment identifier by
  // the same naming rule the uploader uses.
  test('a bootstrap package uri absent from the context is derived, not left as a placeholder', async () => {
    const { template } = await synthVdc({
      context: {
        controller_bootstrap_package_uri: '',
        dcv_broker_bootstrap_package_uri: '',
        dcv_connection_gateway_package_uri: '',
      },
    });
    const resources = template.Resources as Json;
    const userDataOf = (id: string): string => {
      const sub = (resources[id] as Json).Properties.LaunchTemplateData.UserData['Fn::Base64'][
        'Fn::Sub'
      ];
      return (typeof sub === 'string' ? sub : sub[0]) as string;
    };
    const expected: Record<string, string> = {
      controllerltF1BF0FE6: 'controller',
      brokerlt82670F31: 'dcv-broker',
      gatewaylt3BC4A74E: 'dcv-connection-gateway',
    };
    for (const [id, component] of Object.entries(expected)) {
      const userData = userDataOf(id);
      // Asserting the exact component suffix, not merely that a location is present: a helper that
      // handed every component the same name would otherwise pass.
      assert.match(
        userData,
        new RegExp(
          `download_bootstrap\\.sh "s3://[^"]*/idea/bootstrap/bootstrap-${MODULE_ID}-${component}-[^"]+\\.tar\\.gz"`,
        ),
        id,
      );
      assert.doesNotMatch(userData, /not-provided/, id);
    }
  });

  test('detailed monitoring is rejected by the CDK launch-template guard', async () => {
    await assert.rejects(
      synthVdc({
        configFile: configWith({
          'vdc.controller.autoscaling.enable_detailed_monitoring': { BOOL: true },
        }),
        keepWorkdir: true,
      }),
      /instanceMonitoring/,
    );
  });

  /**
   * The gateway task and the network resources it hangs off, under the container flag. They are
   * built here rather than on the container stack because a service may not name a target group
   * that has no load balancer, and the load balancer is this stack's.
   */
  const GATEWAY_TARGET_GROUP = "dcvconnectiongatewayecstargetgroupnlbB89E8640";
  const GATEWAY_SERVICE = "dcvconnectiongatewayserviceServiceF6172F36";
  const GATEWAY_TASK_DEFINITION = "dcvconnectiongatewaytaskdefinitionCA8DCE2B";
  const GATEWAY_TASK_ROLE = "dcvconnectiongatewaytaskrole083CEE07";
  const GATEWAY_TASK_EXECUTION_ROLE = "dcvconnectiongatewaytaskexecutionroleD6FA88E6";
  /** The host gateway group, which the task ENI joins so it holds the position the host holds. */
  const GATEWAY_SECURITY_GROUP = "vdcgatewaysecuritygroup4695D876";
  const GATEWAY_NLB_LISTENER = "ideadev27vdcexternalnlbdcvconnectiongatewaynlblistenerBF44B455";
  /** The self-signed certificate resource this stack has always created for the gateway. */
  // The two rows the deploy tool publishes, which the task reads instead of the resource's
  // attributes. Read from the captured settings so the test cannot pass on a value it invented.
  const GATEWAY_CERTIFICATE_SECRET_ARN = settingsLookup(CONFIG_FILE)(
    "vdc.dcv_connection_gateway.certificate.certificate_secret_arn",
  ) as string;
  const GATEWAY_PRIVATE_KEY_SECRET_ARN = settingsLookup(CONFIG_FILE)(
    "vdc.dcv_connection_gateway.certificate.private_key_secret_arn",
  ) as string;

  /** Settings that put this stack on the container flag: the shared capacity and nothing else. */
  const ECS_ENABLED: Record<string, Json> = { ...ECS_SHARED_CAPACITY };

  /** The name of the target group an endpoint's forward action points at. */
  function forwardedTargetGroupName(resources: Json, endpointId: string): unknown {
    const actions = (resources[endpointId] as Json).Properties.actions as Json[];
    const arn = actions[0]?.TargetGroupArn as Json;
    const referenced = arn?.Ref as string | undefined;
    if (referenced === undefined) return arn;
    return (resources[referenced] as Json | undefined)?.Properties?.Name;
  }

  /** The target group names this stack creates under the container flag. */
  const CONTAINER_TARGET_GROUPS = {
    controllerExternal: `${CLUSTER}-vdc-ecs-e-${TARGET_GROUP_HASH["vdc"]}`,
    controllerInternal: `${CLUSTER}-vdc-ecs-i-${TARGET_GROUP_HASH["vdc"]}`,
    brokerClient: `${CLUSTER}-brk-ecs-c-${TARGET_GROUP_HASH["vdc"]}`,
    brokerAgent: `${CLUSTER}-brk-ecs-a-${TARGET_GROUP_HASH["vdc"]}`,
    brokerGateway: `${CLUSTER}-brk-ecs-g-${TARGET_GROUP_HASH["vdc"]}`,
  } as const;

  test("ecs.retain_existing_hosts routes the endpoints and keeps all three host groups", async () => {
    // The intermediate state a cutover needs: the container target groups serve the endpoints while
    // all three host groups are still there to go back to. They register with nothing, because the
    // container target groups take IP targets.
    const { template } = await synthVdc({
      configFile: configWith({ ...ECS_ENABLED, "ecs.retain_existing_hosts": { BOOL: true } }),
    });
    const resources = template.Resources as Json;

    for (const id of [
      "controllerltProfile36DB920D",
      "controllerltF1BF0FE6",
      "controllerasgASGDDF54A55",
      "brokerltProfile7E9C2A12",
      "brokerlt82670F31",
      "brokerasgASG24CAB1DF",
      "gatewayltProfile9C2C4E20",
      "gatewaylt3BC4A74E",
      "gatewayasgASG04E7FFF1",
      "vdccontrollersecuritygroupfromideadev27vdcbastionhostsecuritygroup3ED16E1C22122FAA1D",
      "vdcbrokersecuritygroupfromideadev27vdcbastionhostsecuritygroup3ED16E1C2286AD88A1",
      "vdcgatewaysecuritygroupfromideadev27vdcbastionhostsecuritygroup3ED16E1C2241CA63C7",
    ]) {
      assert.ok(resources[id] !== undefined, id);
    }
    for (const id of ["controllerasgASGDDF54A55", "brokerasgASG24CAB1DF", "gatewayasgASG04E7FFF1"]) {
      assert.equal(resources[id].Properties.TargetGroupArns, undefined, id);
    }
    // The stack's own target groups are gone in both routed and removed states: the endpoints point
    // at the container groups either way.
    for (const id of [
      "controllertargetgroupext63F85C5A",
      "controllertargetgroupintE01106BF",
      "brokerclienttargetgroupE49CEB15",
      "brokeragenttargetgroup63081663",
      "brokergatewaytargetgroup26708C7F",
      "dcvconnectiongatewaytargetgroupnlbEAFF76CA",
    ]) {
      assert.equal(resources[id], undefined, id);
    }

    assert.equal(forwardedTargetGroupName(resources, "controllerendpointext"), CONTAINER_TARGET_GROUPS.controllerExternal);
    assert.equal(forwardedTargetGroupName(resources, "controllerendpointint"), CONTAINER_TARGET_GROUPS.controllerInternal);
    assert.equal(forwardedTargetGroupName(resources, "dcvbrokerclientendpoint"), CONTAINER_TARGET_GROUPS.brokerClient);
    // The gateway target group is created here, beside the listener that gives it a load balancer,
    // so the listener forwards to it by reference rather than to an arn a setting carried.
    assert.deepEqual(
      resources.ideadev27vdcexternalnlbdcvconnectiongatewaynlblistenerBF44B455.Properties.DefaultActions,
      [{ TargetGroupArn: { Ref: GATEWAY_TARGET_GROUP }, Type: "forward" }],
    );
  });

  test("ecs.enabled publishes the task roles as the identities the controller trusts", async () => {
    // The controller drops any queue message whose sender is not the role id it reads from these
    // rows, including its own session-validation events and the broker's. On the first migrated
    // cluster every desktop stayed INITIALIZING because the rows still named the host roles.
    const { template } = await synthVdc({ configFile: configWith(ECS_ENABLED) });
    const settings = (template.Resources as Json).ideadev27vdcsettings.Properties.settings as Json;
    assert.deepEqual(settings.controller_iam_role_id, { "Fn::GetAtt": ["controllertaskroleF8EB1618", "RoleId"] });
    assert.deepEqual(settings.dcv_broker_role_id, { "Fn::GetAtt": ["dcvbrokertaskrole9460019F", "RoleId"] });
    // The DCV hosts stay instances, so their role does not move.
    assert.deepEqual(settings.dcv_host_role_id, { "Fn::GetAtt": ["vdchostrole47D92D67", "RoleId"] });
  });

  test("ecs.enabled removes control-plane hosts and preserves the VDI host shape", async () => {
    const { template } = await synthVdc({ configFile: configWith(ECS_ENABLED) });
    const resources = template.Resources as Json;

    for (const id of [
      "controllerltProfile36DB920D",
      "controllerltF1BF0FE6",
      "controllerasgASGDDF54A55",
      "controllerasgScalingPolicycpuutilizationscalingpolicy173438B3",
      "controllertargetgroupext63F85C5A",
      "controllertargetgroupintE01106BF",
      "brokerltProfile7E9C2A12",
      "brokerlt82670F31",
      "brokerasgASG24CAB1DF",
      "brokerasgScalingPolicycpuutilizationscalingpolicyBBAF87BD",
      "brokerclienttargetgroupE49CEB15",
      "brokeragenttargetgroup63081663",
      "brokergatewaytargetgroup26708C7F",
      "gatewayltProfile9C2C4E20",
      "gatewaylt3BC4A74E",
      "gatewayasgASG04E7FFF1",
      "gatewayasgScalingPolicycpuutilizationscalingpolicy5CE7AF58",
      "dcvconnectiongatewaytargetgroupnlbEAFF76CA",
      "vdccontrollersecuritygroupfromideadev27vdcbastionhostsecuritygroup3ED16E1C22122FAA1D",
      "vdcbrokersecuritygroupfromideadev27vdcbastionhostsecuritygroup3ED16E1C2286AD88A1",
      "vdcgatewaysecuritygroupfromideadev27vdcbastionhostsecuritygroup3ED16E1C2241CA63C7",
    ]) {
      assert.equal(resources[id], undefined, id);
    }

    assert.equal(forwardedTargetGroupName(resources, "controllerendpointext"), CONTAINER_TARGET_GROUPS.controllerExternal);
    assert.equal(forwardedTargetGroupName(resources, "controllerendpointint"), CONTAINER_TARGET_GROUPS.controllerInternal);
    assert.equal(forwardedTargetGroupName(resources, "dcvbrokerclientendpoint"), CONTAINER_TARGET_GROUPS.brokerClient);
    assert.equal(forwardedTargetGroupName(resources, "dcvbrokeragentendpoint"), CONTAINER_TARGET_GROUPS.brokerAgent);
    assert.equal(forwardedTargetGroupName(resources, "dcvbrokergatewayendpoint"), CONTAINER_TARGET_GROUPS.brokerGateway);
    assert.deepEqual(
      resources.ideadev27vdcexternalnlbdcvconnectiongatewaynlblistenerBF44B455.Properties.DefaultActions,
      [{ TargetGroupArn: { Ref: GATEWAY_TARGET_GROUP }, Type: "forward" }],
    );

    // Each task runs as an identity this stack creates, so the host roles never trust the task
    // service. Two sets of roles coexist through the transition.
    for (const id of ["vdccontrollerroleB8A27FF1", "vdcbrokerrole21DFD357", "vdcgatewayroleDD58223F"]) {
      assert.doesNotMatch(
        JSON.stringify(resources[id].Properties.AssumeRolePolicyDocument),
        /ecs-tasks\./,
        id,
      );
    }

    // The three identities are the service names spelled out, not references to the services: each
    // service has to start after this resource, because its application reads these rows at boot.
    const settings = resources.ideadev27vdcsettings.Properties.settings as Json;
    for (const [key, component] of [
      ["controller", "controller"],
      ["dcv_broker", "broker"],
      ["dcv_connection_gateway", "gateway"],
    ] as const) {
      assert.equal(settings[`${key}.asg_name`], `${CLUSTER}-${MODULE_ID}-${component}`);
      assert.match(
        JSON.stringify(settings[`${key}.asg_arn`]),
        new RegExp(`service/${CLUSTER}-ecs/${CLUSTER}-${MODULE_ID}-${component}`),
      );
    }
    assert.equal(JSON.stringify(settings).includes(GATEWAY_SERVICE), false, "the settings name no service resource");

    for (const id of [
      "vdcdcvhostsecuritygroup96258805",
      "vdcdcvhostsecuritygroupfromideadev27vdcbastionhostsecuritygroup3ED16E1C2261FFA718",
      "vdchostrole47D92D67",
      "vdchostinstanceprofile",
      "ideadev27vdcexternalcertgateway",
      "ideadev27vdcexternalnlbA82E094E",
      "controllerendpointext",
      "controllerendpointint",
      "dcvbrokerclientendpoint",
      "dcvbrokeragentendpoint",
      "dcvbrokergatewayendpoint",
      "ideadev27vdcsettings",
    ]) {
      assert.ok(resources[id] !== undefined, id);
    }

    // Three tasks, three services, and the five application target groups plus the gateway's.
    const ofType = (type: string): string[] =>
      Object.entries(resources).filter(([, resource]) => (resource as Json).Type === type).map(([id]) => id);
    assert.equal(ofType("AWS::ECS::TaskDefinition").length, 3, "controller, broker and gateway tasks");
    assert.equal(ofType("AWS::ECS::Service").length, 3, "controller, broker and gateway services");
    assert.equal(ofType("AWS::ElasticLoadBalancingV2::TargetGroup").length, 6, "five application groups and the gateway's");
  });

  test("ecs.enabled runs the gateway as a task in front of this stack's load balancer", async () => {
    const { template } = await synthVdc({ configFile: configWith(ECS_ENABLED) });
    const resources = template.Resources as Json;

    // One target group, created here so the listener can give it a load balancer. It takes IP
    // targets, which is why it cannot be the host group's, and the listener forwards to this
    // resource rather than to an arn a setting carried.
    const gatewayTargetGroups = Object.entries(resources).filter(
      ([, resource]) =>
        (resource as Json).Type === "AWS::ElasticLoadBalancingV2::TargetGroup" &&
        String((resource as Json).Properties.Name).includes("gw-ecs"),
    );
    assert.deepEqual(
      gatewayTargetGroups.map(([id]) => id),
      [GATEWAY_TARGET_GROUP],
      "one gateway target group",
    );
    const targetGroup = (resources[GATEWAY_TARGET_GROUP] as Json).Properties;
    assert.match(targetGroup.Name as string, new RegExp(`^${CLUSTER}-gw-ecs-TN-[0-9a-f]{8}$`));
    assert.equal(targetGroup.TargetType, "ip");
    assert.equal(targetGroup.Port, 8443);
    assert.equal(targetGroup.Protocol, "TCP");
    assert.equal(targetGroup.HealthCheckPort, "8989");
    const listener = (resources[GATEWAY_NLB_LISTENER] as Json).Properties;
    assert.deepEqual(listener.DefaultActions, [
      { TargetGroupArn: { Ref: GATEWAY_TARGET_GROUP }, Type: "forward" },
    ]);
    assert.equal(listener.Protocol, "TCP");

    // The listener is what gives the target group its load balancer, so the service has to wait
    // for it. Attaching to the target group is what carries that ordering today, and the stack
    // states it as well; either way the deployed template must show the wait.
    const service = resources[GATEWAY_SERVICE] as Json;
    const dependsOn = Array.isArray(service.DependsOn) ? (service.DependsOn as string[]) : [];
    assert.ok(dependsOn.includes(GATEWAY_NLB_LISTENER), "the service waits for the listener");
    const serviceProperties = service.Properties as Json;
    assert.equal(serviceProperties.EnableExecuteCommand, true);
    assert.equal(serviceProperties.HealthCheckGracePeriodSeconds, 270);
    assert.deepEqual(serviceProperties.DeploymentConfiguration.DeploymentCircuitBreaker, {
      Enable: true,
      Rollback: true,
    });
    assert.equal(serviceProperties.DesiredCount, ECS_GATEWAY_TASK_DESIRED);
    assert.equal(
      serviceProperties.CapacityProviderStrategy[0].CapacityProvider,
      ECS_CAPACITY_PROVIDER,
      "the task runs on the container stack's capacity",
    );
    assert.deepEqual(
      serviceProperties.NetworkConfiguration.AwsvpcConfiguration.SecurityGroups,
      [{ "Fn::GetAtt": [GATEWAY_SECURITY_GROUP, "GroupId"] }],
      "the task ENI holds the network position the gateway host holds",
    );
    assert.deepEqual(serviceProperties.LoadBalancers[0].TargetGroupArn, { Ref: GATEWAY_TARGET_GROUP });

    const taskDefinition = (resources[GATEWAY_TASK_DEFINITION] as Json).Properties;
    assert.deepEqual(taskDefinition.RuntimePlatform, {
      CpuArchitecture: ECS_CPU_ARCHITECTURE,
      OperatingSystemFamily: "LINUX",
    });
    assert.equal(taskDefinition.NetworkMode, "awsvpc");
    // The gateway is a proxy. It mounts no shared storage, so the only volume is the log scratch
    // the sidecar reads.
    assert.deepEqual(
      (taskDefinition.Volumes as Json[]).filter((volume) => volume.EFSVolumeConfiguration !== undefined),
      [],
      "no shared storage is mounted on the gateway task",
    );
    const containers = taskDefinition.ContainerDefinitions as Json[];
    const gateway = containers.find((container) => container.Name === "dcv-connection-gateway-container");
    assert.ok(gateway !== undefined, "the gateway container");
    assert.deepEqual(gateway.Secrets, [
      {
        Name: "DCV_GATEWAY_CERT_PEM",
        ValueFrom: GATEWAY_CERTIFICATE_SECRET_ARN,
      },
      {
        Name: "DCV_GATEWAY_KEY_PEM",
        ValueFrom: GATEWAY_PRIVATE_KEY_SECRET_ARN,
      },
    ]);
    // The awslogs driver carries stdout only, so the agent-era file logs keep arriving through a
    // sidecar, in the group and stream family the hosts wrote to.
    const logGroup = `/${CLUSTER}/${MODULE_ID}/dcv-connection-gateway`;
    assert.deepEqual(
      containers.map((container) => [
        container.Name,
        container.LogConfiguration.Options["awslogs-group"],
        container.LogConfiguration.Options["awslogs-stream-prefix"],
      ]),
      [
        ["dcv-connection-gateway-container", logGroup, "dcv-connection-gateway"],
        ["dcv-connection-gateway-file-logs", logGroup, "dcv-connection-gateway"],
      ],
    );

    // The task trust differs from the host trust, which is why the task has its own role. The host
    // roles keep theirs: both sets exist through the transition.
    for (const id of [GATEWAY_TASK_ROLE, GATEWAY_TASK_EXECUTION_ROLE]) {
      const statements = (resources[id] as Json).Properties.AssumeRolePolicyDocument
        .Statement as Json[];
      assert.equal(statements.length, 1, id);
      const statement = statements[0] as Json;
      assert.equal(statement.Action, "sts:AssumeRole", id);
      assert.equal(statement.Effect, "Allow", id);
      assert.deepEqual(statement.Principal, { Service: "ecs-tasks.amazonaws.com" }, id);
      // The account condition is the confused-deputy boundary: without it any account's task
      // service can ask for this role.
      assert.match(
        String(statement.Condition?.StringEquals?.["aws:SourceAccount"]),
        /^\d{12}$/,
        `${id} trusts the task service only from this account`,
      );
    }
    for (const id of ["vdccontrollerroleB8A27FF1", "vdcbrokerrole21DFD357", "vdcgatewayroleDD58223F"]) {
      assert.doesNotMatch(
        JSON.stringify((resources[id] as Json).Properties.AssumeRolePolicyDocument),
        /ecs-tasks\./,
        id,
      );
    }
  });

  test("ecs.enabled with QUIC names the gateway target group for both transports", async () => {
    const { template } = await synthVdc({
      configFile: configWith({ ...ECS_ENABLED, "vdc.dcv_session.quic_support": { BOOL: true } }),
    });
    const resources = template.Resources as Json;

    const targetGroup = (resources[GATEWAY_TARGET_GROUP] as Json).Properties;
    // TUN, not TN: the name changes, which replaces the target group.
    assert.match(targetGroup.Name as string, new RegExp(`^${CLUSTER}-gw-ecs-TUN-[0-9a-f]{8}$`));
    assert.equal(targetGroup.Protocol, "TCP_UDP");
    assert.equal(
      (resources[GATEWAY_NLB_LISTENER] as Json).Properties.Protocol,
      "TCP_UDP",
    );
  });
});
