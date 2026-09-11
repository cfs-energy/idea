/**
 * The argv the CDK CLI is spawned with, token for token against `cdk_invoker.py`.
 *
 * Python built a shell string; the shell then split on whitespace. Each Python line below is the
 * string it produced, and the assertion is the token list a shell would have made of it.
 */

import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { after, before, describe, it } from 'node:test';

import { CdkInvoker, DEPLOYABLE_MODULE_NAMES, type CdkInvokerOptions, type Deps } from '../../src/cli/cdk-invoker.ts';
import { fakeDeps, moduleRow, withTempIdeaHome } from './harness.ts';

const CDK = '/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk';
const CLUSTER = 'sample-cluster';
const REGION = 'us-east-2';
const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

const home = withTempIdeaHome();
const previousCdkBin = process.env.IDEA_CDK_BIN;
before(() => {
  process.env.IDEA_CDK_BIN = CDK;
});
after(() => {
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  home.restore();
});

function invoker(overrides: Partial<CdkInvokerOptions> = {}, deps?: Deps): CdkInvoker {
  return new CdkInvoker({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleId: 'cluster',
    moduleName: 'cluster',
    moduleSet: 'default',
    deploymentId: DEPLOYMENT_ID,
    deps: deps ?? fakeDeps(),
    ...overrides,
  });
}

describe('getCdkAppCmd', () => {
  // Two branches, and the test used to pin only the first while running in the second. When the
  // tool is installed it re-enters by name, matching what the reference implementation builds. When
  // it is not on the path, as in a checkout or a test run, it re-enters through the running
  // interpreter and script, because assuming the name works is what broke a real deploy.
  const reEntryArguments =
    `cdk cdk-app --cluster-name ${CLUSTER} --aws-region ${REGION} --module-id cluster ` +
    `--module-name cluster --deployment-id ${DEPLOYMENT_ID} --termination-protection true`;

  it('re-enters by name when the tool is on the path, matching the reference form', () => {
    const binDirectory = mkdtempSync(join(tmpdir(), 'ideactl-path-'));
    const stub = join(binDirectory, 'ideactl');
    writeFileSync(stub, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
    const originalPath = process.env.PATH;
    process.env.PATH = `${binDirectory}:${originalPath ?? ''}`;
    try {
      assert.equal(invoker().getCdkAppCmd(), `ideactl ${reEntryArguments}`);
    } finally {
      if (originalPath === undefined) delete process.env.PATH;
      else process.env.PATH = originalPath;
      rmSync(binDirectory, { recursive: true, force: true });
    }
  });

  it('re-enters through the interpreter and script when the tool is not on the path', () => {
    const originalPath = process.env.PATH;
    process.env.PATH = '/nonexistent-for-this-test';
    try {
      const command = invoker().getCdkAppCmd();
      assert.ok(command.endsWith(reEntryArguments), command);
      assert.ok(command.startsWith(`${JSON.stringify(process.execPath)} `), command);
      assert.ok(!command.startsWith('ideactl '), command);
    } finally {
      if (originalPath === undefined) delete process.env.PATH;
      else process.env.PATH = originalPath;
    }
  });

  it('appends --aws-profile only when one is given', () => {
    assert.ok(invoker({ awsProfile: 'admin' }).getCdkAppCmd().endsWith('--termination-protection true --aws-profile admin'));
    assert.ok(!invoker({ awsProfile: '' }).getCdkAppCmd().includes('--aws-profile'));
  });

  it('carries --termination-protection false through', () => {
    assert.ok(invoker({ terminationProtection: false }).getCdkAppCmd().includes('--termination-protection false'));
  });
});

describe('getCdkCommand', () => {
  it('synth: `cdk synth --app <cmd> --output cdk.out.<module_id>`', () => {
    const cdk = invoker({ moduleId: 'metrics', moduleName: 'metrics' });
    assert.deepEqual(cdk.getCdkCommand('synth', ['--app', cdk.getCdkAppCmd()]), [
      CDK,
      'synth',
      '--app',
      cdk.getCdkAppCmd(),
      '--output',
      'cdk.out.metrics',
    ]);
  });

  it('diff takes no --rollback', () => {
    const cdk = invoker();
    assert.ok(!cdk.getCdkCommand('diff', ['--app', cdk.getCdkAppCmd()]).includes('--rollback'));
  });

  it('deploy appends --rollback <bool>, then --profile, then -c pairs, then --output last', () => {
    const cdk = invoker({ moduleId: 'vdc', moduleName: 'virtual-desktop-controller', awsProfile: 'admin', rollback: false });
    assert.deepEqual(
      cdk.getCdkCommand('deploy', ['--app', 'APP', '--require-approval', 'never'], {
        controller_bootstrap_package_uri: 's3://bucket/idea/bootstrap/a.tar.gz',
        dcv_broker_bootstrap_package_uri: 's3://bucket/idea/bootstrap/b.tar.gz',
      }),
      [
        CDK,
        'deploy',
        '--app',
        'APP',
        '--require-approval',
        'never',
        '--rollback',
        'false',
        '--profile',
        'admin',
        '-c',
        'controller_bootstrap_package_uri=s3://bucket/idea/bootstrap/a.tar.gz',
        '-c',
        'dcv_broker_bootstrap_package_uri=s3://bucket/idea/bootstrap/b.tar.gz',
        '--output',
        'cdk.out.vdc',
      ],
    );
  });

  it('scopes cdk.out per module id so parallel deploys do not collide', () => {
    for (const moduleId of ['cluster', 'analytics', 'vdc', 'bastion-host']) {
      const argv = invoker({ moduleId, moduleName: moduleId }).getCdkCommand('synth');
      assert.deepEqual(argv.slice(-2), ['--output', `cdk.out.${moduleId}`]);
    }
  });
});

describe('getDeployArgv', () => {
  it('is the reference deploy argv plus the flag that prepares a change set without executing it', () => {
    const cdk = invoker();
    const argv = cdk.getDeployArgv();
    assert.deepEqual(argv, [
      CDK,
      'deploy',
      '--app',
      cdk.getCdkAppCmd(),
      '--outputs-file',
      cdk.outputsFile(),
      '--require-approval',
      'never',
      '--method=prepare-change-set',
      '--rollback',
      'true',
      '--output',
      'cdk.out.cluster',
    ]);
  });

  it('writes <module-name>-outputs.json under deployments/<deployment-id>', () => {
    const cdk = invoker({ moduleId: 'vdc', moduleName: 'virtual-desktop-controller' });
    assert.equal(basename(cdk.outputsFile()), 'virtual-desktop-controller-outputs.json');
    assert.ok(cdk.outputsFile().includes(join('deployments', DEPLOYMENT_ID)));
  });

  it('names the stack <cluster>-<module_id>', () => {
    assert.equal(invoker({ moduleId: 'vdc', moduleName: 'virtual-desktop-controller' }).stackName, `${CLUSTER}-vdc`);
  });
});

describe('CdkInvoker.invoke', () => {
  const tables = {
    [`${CLUSTER}.modules`]: [
      moduleRow('cluster', 'cluster', 'stack', 'deployed'),
      moduleRow('analytics', 'analytics', 'stack'),
      moduleRow('scheduler', 'scheduler', 'app', 'not-deployed'),
      moduleRow('bastion-host', 'bastion-host', 'stack'),
      moduleRow('directoryservice', 'directoryservice', 'stack'),
    ],
    [`${CLUSTER}.cluster-settings`]: [
      { key: 'cluster.cluster_s3_bucket', value: `${CLUSTER}-cluster-${REGION}-123456789012` },
      { key: 'directoryservice.provider', value: 'aws_managed_activedirectory' },
      { key: 'global-settings.module_sets.default.cluster.module_id', value: 'cluster' },
    ],
  };

  it('deploys a module with no host through the change-set guard', async () => {
    const deps = fakeDeps({ tables });
    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'analytics',
      moduleSet: 'default',
      deploymentId: DEPLOYMENT_ID,
      deps,
    });
    await cdk.invoke();
    assert.equal(deps.spawns.length, 1);
    assert.ok(deps.spawns[0]?.includes('--method=prepare-change-set'));
  });

  it('refuses to deploy directoryservice while cluster is not deployed', async () => {
    const deps = fakeDeps({
      tables: {
        ...tables,
        [`${CLUSTER}.modules`]: [
          moduleRow('cluster', 'cluster', 'stack', 'not-deployed'),
          moduleRow('directoryservice', 'directoryservice', 'stack'),
        ],
      },
    });
    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'directoryservice',
      moduleSet: 'default',
      deps,
    });
    await assert.rejects(() => cdk.invoke(), /module: cluster is not yet deployed/);
    assert.deepEqual(deps.spawns, []);
  });

  it('refuses to deploy bastion-host while scheduler is not deployed', async () => {
    const deps = fakeDeps({ tables });
    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'bastion-host',
      moduleSet: 'default',
      deps,
    });
    await assert.rejects(() => cdk.invoke(), /module: scheduler is not yet deployed/);
  });

  it('is a no-op for a module name outside the invoke mapping', async () => {
    const deps = fakeDeps({
      tables: { ...tables, [`${CLUSTER}.modules`]: [moduleRow('global-settings', 'global-settings', 'config')] },
    });
    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'global-settings',
      moduleSet: 'default',
      deps,
    });
    await cdk.invoke();
    assert.deepEqual(deps.spawns, []);
    assert.ok(deps.stdout.some((line) => line.includes('module name not found: global-settings')));
  });

  it('stops before the deploy when the release archive is missing', async () => {
    const deps = fakeDeps({ tables });
    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'scheduler',
      moduleSet: 'default',
      deps,
    });
    await assert.rejects(() => cdk.invoke(), /package not found: .*idea-scheduler-/);
    assert.deepEqual(deps.spawns, []);
  });

  it('stops rather than pointing a host at a bootstrap package it never uploaded', async () => {
    const deps = fakeDeps({
      tables: {
        ...tables,
        [`${CLUSTER}.cluster-settings`]: [
          { key: 'cluster.cluster_s3_bucket', value: `${CLUSTER}-cluster-${REGION}-123456789012` },
          { key: 'directoryservice.provider', value: 'openldap' },
        ],
      },
    });
    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'directoryservice',
      moduleSet: 'default',
      deps,
    });
    await assert.rejects(() => cdk.invoke(), /Cannot build the bootstrap package for module directoryservice on this cluster/);
    assert.deepEqual(deps.spawns, []);
  });

  it('renders and uploads the bootstrap package, and passes its uri as a context param', async () => {
    // A two-component stand-in for `idea-bootstrap`: the real tree needs a full BootstrapContext,
    // which is not ported. What is under test here is the plumbing, not the templates.
    const sourceDir = mkdtempSync(join(tmpdir(), 'ideactl-bootstrap-'));
    for (const component of ['common', 'openldap-server']) {
      mkdirSync(join(sourceDir, component), { recursive: true });
      writeFileSync(join(sourceDir, component, 'setup.sh.jinja2'), 'cluster={{ context.cluster_name }}\n');
    }

    const deps = fakeDeps({
      tables: {
        ...tables,
        [`${CLUSTER}.cluster-settings`]: [
          { key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' },
          { key: 'directoryservice.provider', value: 'openldap' },
          { key: 'directoryservice.base_os', value: 'amazonlinux2023' },
          { key: 'directoryservice.instance_type', value: 'm7i.large' },
        ],
      },
      bootstrapContext: () => ({ cluster_name: CLUSTER }),
    });
    deps.bootstrapSourceDir = sourceDir;

    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'directoryservice',
      moduleSet: 'default',
      deploymentId: DEPLOYMENT_ID,
      deps,
    });
    await cdk.invoke();

    const key = `idea/bootstrap/bootstrap-directoryservice-${DEPLOYMENT_ID}.tar.gz`;
    assert.ok(deps.puts.has(`sample-bucket/${key}`), 'the bootstrap archive was not uploaded');
    assert.ok(
      deps.spawns[0]?.includes(`bootstrap_package_uri=s3://sample-bucket/${key}`),
      'the deploy argv is missing the bootstrap package uri context param',
    );
    rmSync(sourceDir, { recursive: true, force: true });
  });

  it('covers every module name the Python invoke mapping holds, plus only intended additions', () => {
    // The ten names the reference implementation maps. This list is the parity half and does not
    // grow: a name appearing here that the reference does not map is a divergence.
    const referenceModules = [
      'analytics',
      'bastion-host',
      'cluster',
      'cluster-manager',
      'directoryservice',
      'identity-provider',
      'metrics',
      'scheduler',
      'shared-storage',
      'virtual-desktop-controller',
    ];
    // Deliberate additions, each with the reason it has no reference
    // counterpart. The container control plane does not exist in the reference implementation at
    // all, so there is nothing for it to match. Asserting the exact union rather than a superset
    // means an eleventh module added without a line here still fails this test.
    const intendedAdditions = ['ecs'];
    assert.deepEqual(
      [...DEPLOYABLE_MODULE_NAMES].sort(),
      [...referenceModules, ...intendedAdditions].sort(),
    );
    for (const name of intendedAdditions) {
      assert.ok(!referenceModules.includes(name), `${name} is not an addition`);
    }
  });

  it('invoke deploys the ecs module instead of treating it as unknown', async () => {
    const deps = fakeDeps({
      tables: {
        [`${CLUSTER}.modules`]: [moduleRow('ecs', 'ecs', 'stack')],
        [`${CLUSTER}.cluster-settings`]: [
          { key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' },
        ],
      },
    });
    const cdk = await CdkInvoker.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: 'ecs',
      moduleSet: 'default',
      deps,
    });
    await cdk.invoke();
    assert.equal(deps.spawns.length, 1);
    assert.ok(deps.spawns[0]?.includes('cdk.out.ecs'));
    assert.ok(!deps.stdout.some((line) => line.includes('module name not found: ecs')));
  });

  it('DEPLOYABLE_MODULE_NAMES includes ecs', () => {
    assert.ok(DEPLOYABLE_MODULE_NAMES.has('ecs'));
  });
});
