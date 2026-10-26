import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ClusterConfig} from '../../src/config/cluster-config.ts';
import {renderPolicy} from '../../src/cdk/policy.ts';

test('the project boundary covers every desktop and attached managed-policy action', () => {
    const rows: Record<string, string> = {
        'global-settings.module_sets.default.virtual-desktop-controller.module_id': 'vdc',
        'global-settings.module_sets.default.cluster-manager.module_id': 'cluster-manager',
        'cluster.cluster_name': 'example', 'cluster.aws.region': 'us-gov-west-1',
        'cluster.aws.partition': 'aws-us-gov', 'cluster.aws.account_id': '123456789012',
        'cluster.aws.dns_suffix': 'amazonaws.com', 'cluster.s3.bucket': 'example',
        'cluster.kms.key_type': 'customer-managed',
        'cluster.dynamodb.kms_key_id': '00000000-0000-0000-0000-000000000000',
        'cluster.kms.key_id': '00000000-0000-0000-0000-000000000000',
    };
    const config = new ClusterConfig(Object.entries(rows).map(([key, value]) => ({key, value})));
    const actions = (name: string) => {
        const document = renderPolicy(name, {config}) as {Statement: {Effect: string; Action: string | string[]}[]};
        return document.Statement.filter(s => s.Effect === 'Allow').flatMap(s => s.Action);
    };
    const ceiling = actions('project-role-boundary.yml');
    const host = ['virtual-desktop-dcv-host.yml', 'amazon-ssm-managed-instance-core.yml', 'cloud-watch-agent-server-policy.yml', 'amazon-prometheus-remote-write-access.yml'].flatMap(actions);
    const uncovered = host.filter(action => !ceiling.some(pattern => new RegExp('^' + pattern.replaceAll('*', '.*') + '$', 'i').test(action)));
    assert.deepEqual([...new Set(uncovered)], []);
    assert.ok(host.includes('dynamodb:PutItem'));
});
