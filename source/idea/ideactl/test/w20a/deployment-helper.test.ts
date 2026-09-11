/**
 * Deployment ordering, `--optimize-deployment` grouping, the 10 s stagger and the
 * post-group status re-check (`deployment_helper.py`).
 */

import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import {
  deploymentOrder,
  DeploymentHelper,
  OPTIMIZED_DEPLOYMENT_STAGGER_MS,
  optimizedDeploymentOrder,
} from '../../src/cli/deployment-helper.ts';
import { resolveRequestedModules } from '../../src/cli/commands/deploy.ts';
import { fakeDeps, moduleRow, withTempIdeaHome } from './harness.ts';

const CLUSTER = 'sample-cluster';
const REGION = 'us-east-2';

/** Deliberately out of priority order, so a passing test proves the sort and not the input. */
const MODULES = [
  moduleRow('bastion-host', 'bastion-host', 'stack'),
  moduleRow('vdc', 'virtual-desktop-controller', 'app'),
  moduleRow('global-settings', 'global-settings', 'config'),
  moduleRow('cluster-manager', 'cluster-manager', 'app'),
  moduleRow('analytics', 'analytics', 'stack'),
  moduleRow('cluster', 'cluster', 'stack'),
  moduleRow('scheduler', 'scheduler', 'app'),
  moduleRow('identity-provider', 'identity-provider', 'stack'),
  moduleRow('shared-storage', 'shared-storage', 'stack'),
  moduleRow('metrics', 'metrics', 'stack'),
  moduleRow('directoryservice', 'directoryservice', 'stack'),
];

const ALL_IDS = MODULES.map((module) => module['module_id'] as string);

const home = withTempIdeaHome();
const previousCdkBin = process.env.IDEA_CDK_BIN;
before(() => {
  process.env.IDEA_CDK_BIN = '/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk';
});
after(() => {
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  home.restore();
});

describe('deploymentOrder', () => {
  it('sorts by module deployment priority and drops config modules', () => {
    assert.deepEqual(deploymentOrder(MODULES as never, ALL_IDS, false), [
      'cluster',
      'analytics',
      'identity-provider',
      'metrics',
      'directoryservice',
      'shared-storage',
      'cluster-manager',
      'vdc',
      'scheduler',
      'bastion-host',
    ]);
  });

  it('skips modules already deployed unless --upgrade', () => {
    const modules = MODULES.map((module) =>
      module['module_id'] === 'cluster' ? { ...module, status: 'deployed' } : module,
    );
    assert.ok(!deploymentOrder(modules as never, ALL_IDS, false).includes('cluster'));
    assert.ok(deploymentOrder(modules as never, ALL_IDS, true).includes('cluster'));
  });

  it('ignores a module id the modules table does not hold', () => {
    assert.deepEqual(deploymentOrder(MODULES as never, ['nosuchmodule'], false), []);
  });

  it('ecs deploys after shared-storage and before cluster-manager', () => {
    const modules = [
      ...MODULES,
      moduleRow('ecs', 'ecs', 'stack'),
    ];
    const ids = modules.map((module) => module['module_id'] as string);
    const order = deploymentOrder(modules as never, ids, false);
    const ecs = order.indexOf('ecs');
    const storage = order.indexOf('shared-storage');
    const manager = order.indexOf('cluster-manager');
    assert.ok(ecs > storage);
    assert.ok(ecs < manager);
  });
});

describe('optimizedDeploymentOrder', () => {
  it('groups equal priorities and keeps the groups in priority order', () => {
    assert.deepEqual(optimizedDeploymentOrder(MODULES as never, ALL_IDS, false), [
      ['cluster'],
      ['analytics', 'identity-provider', 'metrics', 'directoryservice'],
      ['shared-storage'],
      ['cluster-manager'],
      ['vdc', 'scheduler'],
      ['bastion-host'],
    ]);
  });
});

describe('resolveRequestedModules', () => {
  it('dedupes while keeping the first occurrence', () => {
    assert.deepEqual(resolveRequestedModules(['cluster', 'analytics', 'cluster']), {
      allModules: false,
      moduleIds: ['cluster', 'analytics'],
    });
  });

  it('`all` on its own selects every module', () => {
    assert.deepEqual(resolveRequestedModules(['all']), { allModules: true, moduleIds: undefined });
  });

  it('`all` mixed with a module id is a hard error', () => {
    assert.throws(() => resolveRequestedModules(['all', 'cluster']), /"all" deployment must be the only requested module/);
  });
});

describe('DeploymentHelper.invoke', () => {
  const tablesWith = (modules: Array<Record<string, unknown>>): Record<string, Array<Record<string, unknown>>> => ({
    [`${CLUSTER}.modules`]: modules,
    [`${CLUSTER}.cluster-settings`]: [
      { key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' },
      { key: 'global-settings.module_sets.default.cluster.module_id', value: 'cluster' },
    ],
  });

  const openHelper = async (
    modules: Array<Record<string, unknown>>,
    options: { optimizeDeployment?: boolean; moduleIds?: string[]; allModules?: boolean } = {},
  ) => {
    const deps = fakeDeps({ tables: tablesWith(modules) });
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      deploymentId: 'deployment-1',
      deps,
      ...options,
    });
    return { deps, helper };
  };

  it('deploys sequentially in priority order', async () => {
    const { deps, helper } = await openHelper(
      [moduleRow('cluster', 'cluster', 'stack'), moduleRow('metrics', 'metrics', 'stack')],
      { allModules: true },
    );
    await helper.invoke();
    assert.deepEqual(
      deps.spawns.map((argv) => argv[argv.length - 1]),
      ['cdk.out.cluster', 'cdk.out.metrics'],
    );
    assert.deepEqual(deps.sleeps, []);
  });

  it('staggers each module of a group by 10 seconds', async () => {
    const modules = [
      moduleRow('analytics', 'analytics', 'stack'),
      moduleRow('metrics', 'metrics', 'stack'),
      moduleRow('identity-provider', 'identity-provider', 'stack'),
    ].map((module) => ({ ...module, status: 'not-deployed' }));
    const { deps, helper } = await openHelper(modules, { optimizeDeployment: true, allModules: true });
    // Every module reads `deployed` on the re-check, so the group passes.
    deps.scan = async (input) => ({
      Items:
        input.TableName === `${CLUSTER}.modules`
          ? modules.map((module) => ({ ...module, status: 'deployed' }))
          : tablesWith(modules)[input.TableName],
    });
    await helper.invoke();
    assert.deepEqual(deps.sleeps, [
      OPTIMIZED_DEPLOYMENT_STAGGER_MS,
      OPTIMIZED_DEPLOYMENT_STAGGER_MS,
      OPTIMIZED_DEPLOYMENT_STAGGER_MS,
    ]);
    assert.equal(deps.spawns.length, 3);
  });

  it('fails the group when one module fails, naming it', async () => {
    const modules = [moduleRow('analytics', 'analytics', 'stack'), moduleRow('metrics', 'metrics', 'stack')];
    const deps = fakeDeps({ tables: tablesWith(modules), spawnExitCodes: [0, 1] });
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      optimizeDeployment: true,
      allModules: true,
      deps,
    });
    await assert.rejects(() => helper.invoke(), /deployment failed\. could not deploy module\(s\): metrics/);
  });

  it('fails when a module in the group did not reach status deployed', async () => {
    const modules = [moduleRow('analytics', 'analytics', 'stack'), moduleRow('metrics', 'metrics', 'stack')];
    const { helper } = await openHelper(modules, { optimizeDeployment: true, allModules: true });
    await assert.rejects(
      () => helper.invoke(),
      /Module analytics on sample-cluster is not deployed after its stack run/,
    );
  });

  it('says so instead of deploying when everything is already deployed', async () => {
    const { deps, helper } = await openHelper(
      [moduleRow('cluster', 'cluster', 'stack', 'deployed')],
      { allModules: true },
    );
    await helper.invoke();
    assert.deepEqual(deps.spawns, []);
    assert.ok(deps.stdout.some((line) => line.includes('is already deployed. use the --upgrade flag')));
  });

  it('re-deploys a deployed module with --upgrade', async () => {
    const deps = fakeDeps({ tables: tablesWith([moduleRow('cluster', 'cluster', 'stack', 'deployed')]) });
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      allModules: true,
      upgrade: true,
      deps,
    });
    await helper.invoke();
    assert.equal(deps.spawns.length, 1);
  });

  it('refreshModules retries once on ExpiredTokenException', async () => {
    const modules = [moduleRow('analytics', 'analytics', 'stack'), moduleRow('metrics', 'metrics', 'stack')]
      .map((module) => ({ ...module, status: 'not-deployed' }));
    const { deps, helper } = await openHelper(modules, { optimizeDeployment: true, allModules: true });
    let scans = 0;
    let refreshAttempts = 0;
    deps.scan = async (input) => {
      if (input.TableName === `${CLUSTER}.modules`) {
        scans += 1;
        if (deps.spawns.length >= 2) {
          refreshAttempts += 1;
          if (refreshAttempts === 1) {
            const error = new Error('expired');
            error.name = 'ExpiredTokenException';
            throw error;
          }
          return { Items: modules.map((module) => ({ ...module, status: 'deployed' })) };
        }
        return { Items: modules };
      }
      return { Items: tablesWith(modules)[input.TableName] ?? [] };
    };
    await helper.invoke();
    assert.ok(scans >= 2);
    assert.equal(refreshAttempts, 2);
  });
});
