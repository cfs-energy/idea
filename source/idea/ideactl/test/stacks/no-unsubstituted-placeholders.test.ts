/**
 * CloudFormation substitutes `${AWS::Partition}`, `${LogicalId}` and the synthesizer's
 * `${Qualifier}` only inside Fn::Sub. Anywhere else the text reaches AWS as written, so an ARN
 * built from one names no principal. The terminal CA resource policy shipped with the
 * synthesizer's CloudFormation execution role as such a literal: Secrets Manager refused the
 * policy as a lockout and the cluster-manager stack rolled back. Synthesis cannot see it; the
 * template is valid.
 *
 * Every stack ideactl deploys is synthesized from the day-zero fixture in the commercial and
 * GovCloud partitions, plus the rendered bootstrap template. Shell `${name[@]}` and IAM policy
 * variables such as `${aws:userid}` are not CloudFormation placeholders and are left alone.
 */

import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { before, describe, it } from 'node:test';

import { load } from 'js-yaml';

import { elbAccountIdForRegion, renderBootstrapStack } from '../../src/cdk/stacks/bootstrap.ts';
import { shake256Hex } from '../../src/util/shake256.ts';
import { rehearseDayZero } from '../../tools/day-zero/rehearse.ts';
import { byType, resourcesOf } from '../support/ecs-harness.ts';

type Json = Record<string, unknown>;

const PARTITIONS = [
  { partition: 'aws', region: 'us-east-2' },
  { partition: 'aws-us-gov', region: 'us-gov-west-1' },
] as const;
const ACCOUNT = '123456789012';
const CLUSTER = 'idea-test1';

/** `${AWS::Name}`, `${LogicalId}`, `${LogicalId.Attr}` and an unresolved `${Token[...]}`. */
const PLACEHOLDER = /\$\{(?:AWS::[A-Za-z]+|[A-Za-z0-9]+(?:\.[A-Za-z0-9.]+)?|Token\[[^\]]*\])\}/u;

/** JSON paths of strings outside Fn::Sub that still carry a CloudFormation placeholder. */
export function unsubstituted(value: unknown, path = ''): string[] {
  if (typeof value === 'string') return PLACEHOLDER.test(value) ? [`${path}: ${value}`] : [];
  if (Array.isArray(value)) return value.flatMap((item, index) => unsubstituted(item, `${path}[${index}]`));
  if (value === null || typeof value !== 'object') return [];
  return Object.entries(value as Json).flatMap(([key, item]) => (key === 'Fn::Sub' ? [] : unsubstituted(item, `${path}/${key}`)));
}

/** The value an ARN expression takes in the given partition, region and account. */
function resolveArn(value: unknown, pseudo: Record<string, string>): string {
  if (typeof value === 'string') return value;
  const expression = value as Json;
  if (typeof expression['Ref'] === 'string') return pseudo[expression['Ref']] ?? assert.fail(`unexpected Ref ${expression['Ref']}`);
  if (typeof expression['Fn::Sub'] === 'string') {
    return expression['Fn::Sub'].replaceAll(/\$\{([^}]+)\}/gu, (_, name: string) => pseudo[name] ?? assert.fail(`unexpected \${${name}}`));
  }
  if (Array.isArray(expression['Fn::Join'])) {
    const [separator, parts] = expression['Fn::Join'] as [string, unknown[]];
    return parts.map((part) => resolveArn(part, pseudo)).join(separator);
  }
  return assert.fail(`not an ARN expression: ${JSON.stringify(value)}`);
}

const templates = new Map<string, Record<string, Json>>();

before(async () => {
  const base = readFileSync(join(import.meta.dirname, 'day-zero-new-cluster.values.yml'), 'utf8');
  process.setMaxListeners(0);
  for (const { partition, region } of PARTITIONS) {
    const directory = mkdtempSync(join(tmpdir(), `placeholders-${partition}-`));
    process.on('exit', () => rmSync(directory, { recursive: true, force: true }));
    const values = join(directory, 'values.yml');
    writeFileSync(values, base.replace('aws_partition: aws\n', `aws_partition: ${partition}\n`).replace('aws_region: us-east-2\n', `aws_region: ${region}\n`));
    const report = await rehearseDayZero({ valuesFile: values, workDir: directory, completeReads: true });
    const stacks: Record<string, Json> = {};
    for (const stack of report.stacks) {
      assert.equal(stack.status, 'SYNTHESIZED', `${partition} ${stack.moduleId}: ${JSON.stringify(report.problems)}`);
      const output = join(directory, `cdk.out.${stack.moduleId}`);
      for (const name of readdirSync(output).filter((file) => file.endsWith('.template.json'))) {
        stacks[name] = JSON.parse(readFileSync(join(output, name), 'utf8')) as Json;
      }
    }
    assert.ok(Object.keys(stacks).some((name) => name.includes('cluster-manager')), `${partition}: no cluster-manager template`);
    stacks['bootstrap'] = load(renderBootstrapStack({
      clusterName: CLUSTER,
      awsDnsSuffix: 'amazonaws.com',
      awsElbAccountId: elbAccountIdForRegion(region),
      inputPermissionsBoundary: '',
    })) as Json;
    templates.set(partition, stacks);
  }
});

describe('CloudFormation placeholders', () => {
  for (const { partition, region } of PARTITIONS) {
    it(`the terminal CA policy exempts the real CloudFormation execution role in ${partition}`, () => {
      const stacks = templates.get(partition)!;
      const manager = Object.entries(stacks).find(([name]) => name.includes('cluster-manager'))![1];
      const statement = byType(resourcesOf(manager), 'AWS::SecretsManager::ResourcePolicy')
        .flatMap(([, policy]) => ((policy['Properties'] as Json)['ResourcePolicy'] as Json)['Statement'] as Json[])
        .find((candidate) => (candidate['Action'] as string[]).includes('secretsmanager:PutResourcePolicy'));
      assert.ok(statement, 'no statement guards PutResourcePolicy');
      const principals = ((statement['Condition'] as Json)['ArnNotEquals'] as Json)['aws:PrincipalArn'] as unknown[];
      const pseudo = { 'AWS::Partition': partition, 'AWS::Region': region, 'AWS::AccountId': ACCOUNT };
      const qualifier = shake256Hex(CLUSTER, 5);
      const expected = `arn:${partition}:iam::${ACCOUNT}:role/cdk-${qualifier}-cfn-exec-role-${ACCOUNT}-${region}`;
      const cfnExec = principals.find((principal) => JSON.stringify(principal).includes('cfn-exec-role'));
      assert.ok(cfnExec, JSON.stringify(principals));
      assert.deepEqual(unsubstituted(cfnExec), [], 'the role ARN carries a literal placeholder');
      assert.equal(resolveArn(cfnExec, pseudo), expected);
    });

    it(`no ${partition} template carries a placeholder outside Fn::Sub`, () => {
      const hits = Object.entries(templates.get(partition)!).flatMap(([name, template]) => unsubstituted(template, name));
      assert.deepEqual(hits, []);
    });
  }

  it('the detector sees a literal placeholder and ignores Fn::Sub, shell and IAM variables', () => {
    assert.equal(unsubstituted({ a: ['arn:${AWS::Partition}:iam::1:role/x'] }).length, 1);
    assert.equal(unsubstituted({ a: 'cdk-${Qualifier}-role' }).length, 1);
    assert.deepEqual(unsubstituted({ 'Fn::Sub': 'arn:${AWS::Partition}:x' }), []);
    assert.deepEqual(unsubstituted({ a: 'for f in "${followed[@]:-}"', b: '${aws:userid}', c: 'host:${ec2:SourceInstanceARN}' }), []);
  });
});
