import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { rehearseDayZero } from '../../tools/day-zero/rehearse.ts';
import { buildApp } from '../../src/cdk/app.ts';
import { buildStack } from '../../src/cdk/stacks/cluster-manager.ts';
import { renderPolicy } from '../../src/cdk/policy.ts';
import { IdeaCodeAsset } from '../../src/cdk/code-asset.ts';
import * as iam from 'aws-cdk-lib/aws-iam';

type Json = Record<string, any>;

// Bound unresolved resource references conservatively: an IAM ARN with a 512-character path
// and a 64-character name is longer than the queue, secret, stream and key ARNs used here.
const RESOURCE_ARN = `arn:aws-us-gov:iam::123456789012:role/${'p'.repeat(512)}${'n'.repeat(64)}`;
const pseudo: Record<string, string> = {
  'AWS::Partition': 'aws-us-gov', 'AWS::Region': 'us-gov-west-1',
  'AWS::AccountId': '123456789012', 'AWS::URLSuffix': 'amazonaws.com',
};
function resolve(value: any): any {
  if (Array.isArray(value)) return value.map(resolve);
  if (typeof value === 'string') return value.replaceAll('arn:aws:', 'arn:aws-us-gov:').replaceAll('us-east-2', 'us-gov-west-1');
  if (!value || typeof value !== 'object') return value;
  if (value.Ref) return pseudo[value.Ref] ?? RESOURCE_ARN;
  if (value['Fn::Join']) return resolve(value['Fn::Join'][1]).join(value['Fn::Join'][0]);
  if (value['Fn::GetAtt']) return RESOURCE_ARN;
  if (value['Fn::Sub']) {
    const arg = value['Fn::Sub'];
    const [text, vars] = Array.isArray(arg) ? arg : [arg, {}];
    return text.replace(/\$\{([^}]+)\}/g, (_: string, key: string) => resolve(vars[key] ?? pseudo[key] ?? RESOURCE_ARN));
  }
  assert.ok(!Object.keys(value).some((key) => key.startsWith('Fn::')), `unresolved intrinsic: ${JSON.stringify(value)}`);
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, resolve(item)]));
}
const size = (doc: unknown): number => Buffer.byteLength(JSON.stringify(resolve(doc)));

test('GovCloud Bedrock task policies fit IAM limits and preserve all template statements', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'cm-gov-policy-'));
  process.setMaxListeners(0);
  process.on('exit', () => rmSync(dir, { recursive: true, force: true }));
  const values = join(dir, 'values.yml');
  writeFileSync(values, readFileSync(join(import.meta.dirname, 'day-zero-new-cluster.values.yml'), 'utf8')
    .replace('aws_region: us-east-2', 'aws_region: us-gov-west-1'));
  const inputs = join(dir, 'inputs');
  const report = await rehearseDayZero({ valuesFile: values, workDir: dir, emitInputs: inputs, completeReads: true });
  assert.equal(report.stacks.find((stack) => stack.moduleId === 'cluster-manager')?.status, 'SYNTHESIZED', JSON.stringify(report.problems));
  const originalAssetPath = IdeaCodeAsset.prototype.assetPath;
  const previous = { cwd: process.cwd(), out: process.env.CDK_OUTDIR, nag: process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN };
  let expected: Json[] = [];
  try {
    IdeaCodeAsset.prototype.assetPath = () => join(dir, 'empty-lambda-asset');
    process.chdir(dir);
    process.env.CDK_OUTDIR = join(dir, 'gov-bedrock');
    process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = 'false';
    const app = await buildApp({
      clusterName: 'idea-test1', awsRegion: 'us-gov-west-1', moduleId: 'cluster-manager',
      moduleName: 'cluster-manager', deploymentId: 'test', terminationProtection: true,
      configFile: join(inputs, 'cluster-manager/cluster-settings.json'),
      synthReadsFile: join(inputs, 'cluster-manager/synth-reads.json'),
    }, { 'cluster-manager': async () => (props) => {
      props.ctx.config.setEntry('cluster.aws.partition', 'aws-us-gov');
      props.ctx.config.setEntry('cluster-manager.bedrock.enabled', true);
      props.ctx.config.setEntry('cluster-manager.metrics.cost.enabled', true);
      props.ctx.config.setEntry('ecs.datadog.api_key_secret_arn', 'arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:test-key-abcdef');
      expected = (renderPolicy('cluster-manager.yml', { config: props.ctx.config, moduleId: 'cluster-manager' }) as Json).Statement;
      return buildStack(props);
    } });
    const template = app.synth().getStackByName('idea-test1-cluster-manager').template;
    const resources = template.Resources as Record<string, Json>;
    const [roleId, role] = Object.entries(resources).find(([, resource]) =>
      resource.Type === 'AWS::IAM::Role' && resource.Properties.RoleName.includes('-cluster-manager-task-role-'))!;
    assert.ok(roleId);
    let inlineBytes = (role.Properties.Policies ?? []).reduce((n: number, policy: Json) => n + size(policy.PolicyDocument), 0);
    const managed: Json[] = [];
    for (const [id, resource] of Object.entries(resources)) {
      const props = resource.Properties;
      if (resource.Type === 'AWS::IAM::Policy' && props.Roles?.some((ref: Json) => ref.Ref === roleId)) inlineBytes += size(props.PolicyDocument);
      if (resource.Type === 'AWS::IAM::ManagedPolicy') {
        assert.ok(size(props.PolicyDocument) < 6144, `${id}: ${size(props.PolicyDocument)} bytes`);
        if (role.Properties.ManagedPolicyArns.some((ref: Json) => ref.Ref === id) || props.Roles?.some((ref: Json) => ref.Ref === roleId)) managed.push(props.PolicyDocument);
      }
    }
    assert.ok(inlineBytes < 10240, `inline total: ${inlineBytes}`);
    assert.equal(managed.length, 3);
    assert.ok(managed.some((doc) => JSON.stringify(doc).includes('bedrock:CreateInferenceProfile')));
    const normalize = (statements: Json[]) => statements.map((statement) => JSON.stringify(resolve(iam.PolicyStatement.fromJson(statement).toStatementJson()))).sort();
    assert.deepEqual(normalize(managed.flatMap((doc) => doc.Statement)), normalize(expected));
    console.log(`GovCloud task inline: ${inlineBytes}; managed: ${managed.map(size).join(', ')} bytes`);
  } finally {
    IdeaCodeAsset.prototype.assetPath = originalAssetPath;
    process.chdir(previous.cwd);
    if (previous.out === undefined) delete process.env.CDK_OUTDIR; else process.env.CDK_OUTDIR = previous.out;
    if (previous.nag === undefined) delete process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN; else process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = previous.nag;
  }
});
