/** ECS parity after sunset, with reviewed release and browser-terminal additions. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { load } from 'js-yaml';
import { elbAccountIdForRegion, renderBootstrapStack } from '../../src/cdk/stacks/bootstrap.ts';
import { CostCollectorStack } from '../../src/cdk/stacks/cost-collector.ts';
import { rehearseDayZero } from '../../tools/day-zero/rehearse.ts';

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === 'object') return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]));
  return value;
}

export function templateDigest(template: any): string {
  // These three fields already contain fresh UUIDs on every baseline synth. No other field is omitted.
  const resources = template.Resources;
  if (resources.opensearchprivateips) {
    assert.match(resources.opensearchprivateips.Properties.UpdateToken, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    resources.opensearchprivateips.Properties.UpdateToken = '<random-uuid>';
  }
  if (resources.ideatest1dashboardtargetgroup) {
    assert.match(resources.ideatest1dashboardtargetgroup.Properties.Name, /^idea-test1-dashboard-[0-9a-f-]+$/);
    resources.ideatest1dashboardtargetgroup.Properties.Name = 'idea-test1-dashboard-<random-suffix>';
  }
  if (resources.ideatest1userpooldomainEB5B2B9B) {
    assert.match(resources.ideatest1userpooldomainEB5B2B9B.Properties.Domain, /^idea-test1-[0-9a-f-]+$/);
    resources.ideatest1userpooldomainEB5B2B9B.Properties.Domain = 'idea-test1-<random-uuid>';
  }
  return createHash('sha256').update(JSON.stringify(canonical(template))).digest('hex');
}

// Compared against the original v26.10.9 synth: only release versions/images and the
// terminal CA, SSH ingress, IAM grants and service dependency changed in 26.11.1.
// Cluster-manager also always grants the three read-only Cost Explorer actions.
// S-24 removes the unused scheduler, cluster-manager and VDC host roles and their settings.
// Keep hashing every field except the three validated random values above.
test('all eleven ECS module templates match the reviewed v26.11.1 baseline', async () => {
  const baseline = JSON.parse(readFileSync(new URL('./ecs-sunset-digests.json', import.meta.url), 'utf8'));
  const root = mkdtempSync(join(tmpdir(), 'ecs-sunset-parity-'));
  try {
    const result = await rehearseDayZero({ valuesFile: join(import.meta.dirname, 'day-zero-new-cluster.values.yml'), workDir: root, completeReads: true });
    assert.deepEqual(result.problems, []);
    const directories = readdirSync(root).filter(name => name.startsWith('cdk.out.')).sort();
    assert.deepEqual(directories.map(name => name.slice(8)), Object.keys(baseline).sort());
    for (const directory of directories) {
      const files = readdirSync(join(root, directory)).filter(name => name.endsWith('.template.json'));
      assert.equal(files.length, 1);
      const template = JSON.parse(readFileSync(join(root, directory, files[0]), 'utf8'));
      assert.equal(templateDigest(template), baseline[directory.slice(8)], directory);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('bootstrap and the standalone cost collector match the pre-sunset v26.10.9 baseline', () => {
  const bootstrap = load(renderBootstrapStack({
    clusterName: 'idea-test1', awsDnsSuffix: 'amazonaws.com',
    awsElbAccountId: elbAccountIdForRegion('us-east-2'), inputPermissionsBoundary: '',
  }));
  assert.equal(templateDigest(bootstrap), 'ac702e4f32c3df9c5a12f9e5ead68cb73dd078ae4822ff4bf1514e7961ec3e02');
  const collector = new CostCollectorStack(new App(), 'cost-collector', {
    env: { account: '123456789012', region: 'us-east-1' }, clusterName: 'gov-cluster',
    controlPlaneImage: '123456789012.dkr.ecr.us-east-1.amazonaws.com/control:release',
    agentImage: `123456789012.dkr.ecr.us-east-1.amazonaws.com/agent@sha256:${'a'.repeat(64)}`,
    datadogApiKeySecretArn: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:datadog-abcdef',
    vpcId: 'vpc-0123456789abcdef0', subnetIds: ['subnet-0123456789abcdef0'], publicSubnets: true,
  });
  assert.equal(templateDigest(Template.fromStack(collector).toJSON()), '31d184e7cdb65422e032a336b91f69e3a077159bbff98f086abb49129c20819c');
});
