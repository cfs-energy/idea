/**
 * `quick-setup`: the order of the steps is the contract, because each one leaves state
 * the next reads. This drives the `--skip-config` path, which starts at step 4.
 */

import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, it } from 'node:test';

import { quickSetup } from '../../src/cli/main.ts';
import { fakeDeps, moduleRow, withTempIdeaHome } from './harness.ts';

const CLUSTER = 'sample-cluster';
const REGION = 'us-east-2';

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

const TABLES = {
  [`${CLUSTER}.modules`]: [
    moduleRow('cluster', 'cluster', 'stack'),
    moduleRow('metrics', 'metrics', 'stack'),
    moduleRow('shared-storage', 'shared-storage', 'stack'),
  ],
  [`${CLUSTER}.cluster-settings`]: [
    { key: 'global-settings.module_sets.default.cluster.module_id', value: 'cluster' },
    { key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' },
    { key: 'cluster.aws.dns_suffix', value: 'amazonaws.com' },
    { key: 'cluster.load_balancers.external_alb.load_balancer_dns_name', value: 'alb.example.invalid' },
  ],
};

function valuesFile(): string {
  const dir = mkdtempSync(join(tmpdir(), 'ideactl-quicksetup-'));
  const file = join(dir, 'values.yml');
  writeFileSync(file, `cluster_name: ${CLUSTER}\naws_region: ${REGION}\n`);
  return file;
}

describe('quick-setup --skip-config', () => {
  it('bootstraps, then deploys in priority order, then checks the endpoints', async () => {
    const file = valuesFile();
    const deps = fakeDeps({ tables: TABLES });
    await quickSetup(deps, { valuesFile: file, skipConfig: true, force: true, moduleSet: 'default' });

    const commands = deps.spawns.map((argv) => argv[1]);
    assert.deepEqual(commands, ['bootstrap', 'deploy', 'deploy', 'deploy']);
    assert.deepEqual(
      deps.spawns.slice(1).map((argv) => argv[argv.length - 1]),
      ['cdk.out.cluster', 'cdk.out.metrics', 'cdk.out.shared-storage'],
    );
    // Each change set passed the guard, so each one was executed.
    assert.deepEqual(
      deps.executed.map((call) => call.StackName),
      [`${CLUSTER}-cluster`, `${CLUSTER}-metrics`, `${CLUSTER}-shared-storage`],
    );
    assert.ok(deps.stdout.some((line) => line.includes('Web Portal') || line.includes('Cluster Connection Info')));
    rmSync(join(file, '..'), { recursive: true, force: true });
  });

  it('refuses --skip-config without --values-file', async () => {
    const deps = fakeDeps({ tables: TABLES });
    await assert.rejects(
      () => quickSetup(deps, { skipConfig: true, force: true, moduleSet: 'default' }),
      (error: Error) => error.name === 'ExitWithCode',
    );
    assert.ok(deps.stderr.some((line) => line.includes('--values-file is required')));
  });

  it('stops at the confirmation prompt without bootstrapping', async () => {
    const file = valuesFile();
    const deps = fakeDeps({ tables: TABLES, answers: [false] });
    await assert.rejects(
      () => quickSetup(deps, { valuesFile: file, skipConfig: true, moduleSet: 'default' }),
      (error: Error) => error.name === 'ExitWithCode',
    );
    assert.deepEqual(deps.spawns, []);
    assert.ok(deps.stdout.includes('Deployment aborted!'));
    rmSync(join(file, '..'), { recursive: true, force: true });
  });
});

describe('bootstrap', () => {
  it('renders the toolkit template and runs `cdk bootstrap` with the tags from cluster settings', async () => {
    const { runBootstrap } = await import('../../src/cli/commands/deploy.ts');
    const deps = fakeDeps({
      tables: {
        ...TABLES,
        [`${CLUSTER}.cluster-settings`]: [
          ...TABLES[`${CLUSTER}.cluster-settings`],
          { key: 'global-settings.custom_tags', value: ['Key=CostCenter,Value=42'] },
        ],
      },
    });
    await runBootstrap(deps, { clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default' });

    const argv = deps.spawns[0] ?? [];
    assert.equal(argv[1], 'bootstrap');
    assert.deepEqual(argv.slice(argv.indexOf('--bootstrap-bucket-name'), argv.indexOf('--qualifier')), [
      '--bootstrap-bucket-name',
      'sample-bucket',
      '--toolkit-stack-name',
      `${CLUSTER}-bootstrap`,
      '--termination-protection',
      'true',
    ]);
    // Custom tags first, the cluster tag last so it wins a key collision.
    const tags = argv.reduce<string[]>((all, token, index) => (argv[index - 1] === '--tags' ? [...all, token] : all), []);
    assert.deepEqual(tags, ['CostCenter=42', `idea:ClusterName=${CLUSTER}`]);
    assert.ok(argv.includes('--public-access-block-configuration'));
    assert.ok(deps.stdout.some((line) => line.startsWith('rendered cdk toolkit stack template for cluster:')));
  });
});
