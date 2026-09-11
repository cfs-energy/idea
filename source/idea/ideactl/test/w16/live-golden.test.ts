// Golden test against gitignored live fixtures. The test requires them explicitly.
// Cluster settings and the uploader naming functions rebuild the bootstrap
// package location. The live script is the expected output, not an input.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { bootstrapPackagePlans, bootstrapPackageUri } from '../../src/cli/bootstrap-package.ts';
import { buildBootstrapUserData } from '../../src/cdk/userdata.ts';
import { requireFixtures } from '../support/fixtures.ts';

const pkg = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const liveDir = join(pkg, 'tools', 'parity', 'live');
const settingsFile = join(
  pkg, 'tools', 'parity', 'fixtures', 'idea-dev27', 'raw', 'cluster-settings.scan.json',
);

// [stack, logical id, base_os settings key, install command, infra config]
const CASES: Array<
  [string, string, string, string, Record<string, string> | undefined]
> = [
  ['cluster-manager', 'clustermanagerlt5189880B', 'cluster-manager.ec2.autoscaling.base_os',
    '/bin/bash cluster-manager/setup.sh', undefined],
  ['scheduler', 'schedulerltC82E59C0', 'scheduler.base_os',
    '/bin/bash scheduler/setup.sh', undefined],
  ['scheduler', 'schedulerinstance', 'scheduler.base_os',
    '/bin/bash scheduler/setup.sh', undefined],
  ['vdc', 'controllerltF1BF0FE6', 'vdc.controller.autoscaling.base_os',
    '/bin/bash virtual-desktop-controller/setup.sh', undefined],
  ['vdc', 'brokerlt82670F31', 'vdc.dcv_broker.autoscaling.base_os',
    '/bin/bash dcv-broker/setup.sh', {
      BROKER_CLIENT_TARGET_GROUP_ARN: '${__BROKER_CLIENT_TARGET_GROUP_ARN__}',
      CONTROLLER_EVENTS_QUEUE_URL: '${__CONTROLLER_EVENTS_QUEUE_URL__}',
    }],
  ['vdc', 'gatewaylt3BC4A74E', 'vdc.dcv_connection_gateway.autoscaling.base_os',
    '/bin/bash dcv-connection-gateway/setup.sh', {
      CERTIFICATE_SECRET_ARN: '${__CERTIFICATE_SECRET_ARN__}',
      PRIVATE_KEY_SECRET_ARN: '${__PRIVATE_KEY_SECRET_ARN__}',
    }],
  ['bastion-host', 'bastionhostltFD0D5EC2', 'bastion-host.base_os',
    '/bin/bash bastion-host/setup.sh', undefined],
  ['bastion-host', 'bastionhostinstance', 'bastion-host.base_os',
    '/bin/bash bastion-host/setup.sh', undefined],
];

/** Component the uploader puts in this resource's bootstrap archive. */
const BOOTSTRAP_COMPONENT_BY_LOGICAL_ID: Record<string, string> = {
  clustermanagerlt5189880B: 'cluster-manager',
  schedulerltC82E59C0: 'scheduler',
  schedulerinstance: 'scheduler',
  controllerltF1BF0FE6: 'virtual-desktop-controller',
  brokerlt82670F31: 'dcv-broker',
  gatewaylt3BC4A74E: 'dcv-connection-gateway',
  bastionhostltFD0D5EC2: 'bastion-host',
  bastionhostinstance: 'bastion-host',
};

function moduleForStack(stack: string): { moduleName: string; moduleId: string } {
  if (stack === 'vdc') {
    return { moduleName: 'virtual-desktop-controller', moduleId: 'vdc' };
  }
  return { moduleName: stack, moduleId: stack };
}

/**
 * Rebuilds the download location from cluster settings and the uploader's
 * naming functions. The live script is not an input.
 */
function reconstructedBootstrapPackageUri(
  stack: string,
  logicalId: string,
  settings: Map<string, string | undefined>,
): string {
  const bucket = settings.get('cluster.cluster_s3_bucket');
  assert.ok(bucket, 'cluster.cluster_s3_bucket missing from settings');
  const { moduleName, moduleId } = moduleForStack(stack);
  const deploymentId = settings.get(`${moduleId}.deployment_id`);
  assert.ok(deploymentId, `${moduleId}.deployment_id missing from settings`);
  const component = BOOTSTRAP_COMPONENT_BY_LOGICAL_ID[logicalId];
  assert.ok(component, `${logicalId}: no bootstrap component mapping`);
  const plan = bootstrapPackagePlans(moduleName, moduleId, deploymentId).find((entry) =>
    entry.components.includes(component),
  );
  assert.ok(plan, `${logicalId}: no bootstrap plan for ${component}`);
  return bootstrapPackageUri(bucket, `${plan.basename}.tar.gz`);
}

function userDataTemplate(resource: { Type: string; Properties: Record<string, any> }): string | undefined {
  const props = resource.Properties;
  const userData =
    resource.Type === 'AWS::EC2::Instance'
      ? props?.UserData
      : props?.LaunchTemplateData?.UserData;
  const sub = userData?.['Fn::Base64']?.['Fn::Sub'];
  if (typeof sub === 'string') return sub;
  if (Array.isArray(sub) && typeof sub[0] === 'string') return sub[0];
  return undefined;
}

requireFixtures(
  [
    settingsFile,
    ...CASES.map(([stack]) => join(liveDir, `idea-dev27-${stack}.json`)),
  ],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

test('user data matches every live dev27 EC2 resource', () => {
  // Cluster settings are raw DynamoDB scan output with typed attribute values.
  const rows = JSON.parse(readFileSync(settingsFile, 'utf8')).Items as Array<
    Record<string, Record<string, string>>
  >;
  const settings = new Map<string, string | undefined>(
    rows.map((r) => [r.key.S, r.value?.S]),
  );
  const region = settings.get('cluster.aws.region')!;
  const httpsProxy = settings.get('cluster.network.https_proxy') ?? '';
  const proxyConfig: Record<string, string> = httpsProxy
    ? {
        http_proxy: httpsProxy,
        https_proxy: httpsProxy,
        no_proxy: settings.get('cluster.network.no_proxy') ?? '',
      }
    : {};

  const templates = new Map<string, any>();
  const liveUserDataIds: string[] = [];
  for (const stack of new Set(CASES.map(([name]) => name))) {
    const template = JSON.parse(readFileSync(join(liveDir, `idea-dev27-${stack}.json`), 'utf8'));
    templates.set(stack, template);
    for (const [logicalId, resource] of Object.entries(template.Resources as Record<string, any>)) {
      if (userDataTemplate(resource as { Type: string; Properties: Record<string, any> }) !== undefined) {
        liveUserDataIds.push(`${stack}:${logicalId}`);
      }
    }
  }
  assert.deepEqual(
    liveUserDataIds.sort(),
    CASES.map(([stack, logicalId]) => `${stack}:${logicalId}`).sort(),
    'CASES must list every live EC2 resource that carries user data',
  );

  for (const [stack, logicalId, baseOsKey, installCommand, infraConfig] of CASES) {
    const resource = templates.get(stack).Resources[logicalId];
    assert.ok(resource, `${logicalId}: missing from live ${stack} template`);
    const live = userDataTemplate(resource);
    assert.ok(live, `${logicalId}: no Fn::Sub user data in live template`);

    const expectedUri = reconstructedBootstrapPackageUri(stack, logicalId, settings);
    const uri = /^bash \/root\/bootstrap\/download_bootstrap\.sh "(.*)"$/m.exec(live);
    assert.ok(uri, `${logicalId}: no download_bootstrap.sh invocation in live user data`);
    assert.equal(uri[1], expectedUri, `${logicalId}: live bootstrap package location`);

    const built = buildBootstrapUserData({
      baseOs: settings.get(baseOsKey)!,
      awsRegion: region,
      bootstrapPackageUri: expectedUri,
      installCommands: [installCommand],
      infraConfig,
      proxyConfig,
    });
    assert.equal(built, live, `${logicalId} user data differs`);
  }
});
