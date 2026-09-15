import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { App } from 'aws-cdk-lib';

import { SchedulerStack } from '../../src/cdk/stacks/scheduler.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';

for (const hostname of [undefined, null, '', '   ']) {
  test(`rejects missing hostname ${JSON.stringify(hostname)} for the selected module`, () => {
    const outdir = mkdtempSync(join(tmpdir(), 'scheduler-hostname-'));
    const config = new ClusterConfig([
      { key: 'cluster.cluster_s3_bucket', value: 'example-bucket' },
      { key: 'scheduler.hostname', value: 'wrong-module.example.local' },
      ...(hostname === undefined ? [] : [{ key: 'batch.hostname', value: hostname }]),
    ]);
    try {
      assert.throws(() => new SchedulerStack({
        app: new App({ outdir }),
        ctx: {
          config,
          clusterName: 'example',
          awsRegion: 'us-east-2',
          awsProfile: undefined,
          moduleId: 'batch',
          releaseVersion: 'test',
          synthReads: {
            callerIdentity: async () => ({ account: '123456789012', arn: 'unused' }),
            describeDomain: async () => { throw new Error('unexpected domain lookup'); },
            describeListener: async () => ({}),
            describeUserPool: async () => ({}),
            listServiceLinkedRoles: async () => [],
          },
        },
        moduleName: 'scheduler',
        deploymentId: 'test',
        terminationProtection: false,
        env: { account: '123456789012', region: 'us-east-2' },
      }), /Missing required setting: batch\.hostname/);
    } finally {
      rmSync(outdir, { recursive: true, force: true });
    }
  });
}
