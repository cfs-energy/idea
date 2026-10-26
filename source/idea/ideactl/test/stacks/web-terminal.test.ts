import assert from 'node:assert/strict';
import {mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {test} from 'node:test';
import {rehearseDayZero} from '../../tools/day-zero/rehearse.ts';
import {byType, resourcesOf} from '../support/ecs-harness.ts';

test('terminal deployment protects the CA, scopes host pins, and admits desktop SSH', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'terminal-infra-'));
    process.setMaxListeners(0);
    process.on('exit', () => rmSync(directory, {recursive: true, force: true}));
    const values = join(directory, 'values.yml');
    writeFileSync(values, readFileSync(join(import.meta.dirname, 'day-zero-new-cluster.values.yml'), 'utf8') + '\nkms_key_id: 00000000-0000-0000-0000-000000000000\n');
    const report = await rehearseDayZero({valuesFile: values, workDir: directory, completeReads: true});
    for (const module of ['cluster-manager', 'vdc', 'bastion-host']) assert.equal(report.stacks.find(s => s.moduleId === module)?.status, 'SYNTHESIZED', JSON.stringify(report.problems));
    const resources = (module: string) => {
        const output = join(directory, `cdk.out.${module}`);
        const name = readdirSync(output).find(file => file.endsWith('.template.json'))!;
        return resourcesOf(JSON.parse(readFileSync(join(output, name), 'utf8')));
    };
    const manager = resources('cluster-manager');
    const [, ca] = byType(manager, 'AWS::SecretsManager::Secret').find(([, r]) => r.Properties.Name?.endsWith('-terminal-ca'))!;
    assert.ok(ca.Properties.KmsKeyId);
    assert.equal(ca.Properties.SecretString, '{"schema":1}');
    const statement = byType(manager, 'AWS::SecretsManager::ResourcePolicy').flatMap(([, p]) => p.Properties.ResourcePolicy.Statement).find(s => s.Effect === 'Deny');
    assert.equal(statement.Principal, '*');
    // secret data and version stages: the task role only (CloudFormation manages the secret through the second statement)
    for (const action of ['GetSecretValue', 'PutSecretValue', 'UpdateSecretVersionStage', 'RestoreSecret', 'RotateSecret', 'CancelRotateSecret']) assert.ok(statement.Action.includes(`secretsmanager:${action}`), action);
    for (const action of ['UpdateSecret', 'DeleteSecret', 'PutResourcePolicy', 'DeleteResourcePolicy']) assert.ok(!statement.Action.includes(`secretsmanager:${action}`), `${action} must not be task-role-only`);
    assert.match(JSON.stringify(statement.Condition), /cluster-manager-task-role/);
    const statements = byType(manager, 'AWS::SecretsManager::ResourcePolicy').flatMap(([, p]) => p.Properties.ResourcePolicy.Statement);
    for (const action of ['UpdateSecret', 'DeleteSecret', 'PutResourcePolicy', 'DeleteResourcePolicy', 'RestoreSecret', 'RotateSecret', 'CancelRotateSecret']) {
        const deny = statements.find(s => s.Effect === 'Deny' && s.Action.includes(`secretsmanager:${action}`));
        assert.ok(deny, `P-43: deny ${action} to other principals`);
        assert.match(JSON.stringify(deny.Condition), /cluster-manager-task-role/);
        if (['UpdateSecret', 'DeleteSecret', 'PutResourcePolicy', 'DeleteResourcePolicy'].includes(action)) {
            assert.match(JSON.stringify(deny.Condition), /cfn-exec-role/);
        } else {
            assert.doesNotMatch(JSON.stringify(deny.Condition), /cfn-exec-role/);
        }
    }
    assert.doesNotMatch(JSON.stringify(statement.Condition), /cfn-exec-role/);

    const managed = byType(manager, 'AWS::IAM::ManagedPolicy');
    for (const [, policy] of managed) assert.ok(JSON.stringify(policy.Properties.PolicyDocument).length <= 6144);
    assert.match(JSON.stringify(managed), /cluster-manager\.terminal/);
    const batchPins = managed.flatMap(([, p]) => p.Properties.PolicyDocument.Statement).find(s => Array.isArray(s.Action) && s.Action.includes('dynamodb:BatchGetItem'));
    assert.match(JSON.stringify(batchPins.Resource), /cluster-manager\.terminal/);
    const ttl = managed.flatMap(([, p]) => p.Properties.PolicyDocument.Statement).find(s => [s.Action].flat().includes('dynamodb:DescribeTimeToLive'));
    assert.ok(ttl, 'R3-A-5: initialization must be able to inspect TTL');
    assert.match(JSON.stringify(ttl.Resource), /cluster-manager\.terminal/);
    assert.doesNotMatch(JSON.stringify(ttl.Resource), /cluster-settings|table\/\*/);
    const bastionPolicies = JSON.stringify(byType(resources('bastion-host'), 'AWS::IAM::Policy'));
    assert.match(bastionPolicies, /host:bastion/);
    for (const action of ['kms:Decrypt', 'kms:GenerateDataKey']) assert.ok(bastionPolicies.includes(action), action);
    assert.match(bastionPolicies, /key\/00000000-0000-0000-0000-000000000000/);
    assert.match(bastionPolicies, /dynamodb:LeadingKeys/);
    const vdc = resources('vdc');
    assert.ok(byType(vdc, 'AWS::EC2::SecurityGroupIngress').some(([, rule]) => rule.Properties.FromPort === 22 && rule.Properties.Description === 'Browser terminals from cluster-manager'));
    assert.ok(byType(vdc, 'AWS::IAM::ManagedPolicy').some(([, p]) => JSON.stringify(p).includes('host:${ec2:SourceInstanceARN}')));
});
