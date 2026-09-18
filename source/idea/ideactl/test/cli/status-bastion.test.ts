import assert from 'node:assert/strict';
import test from 'node:test';
import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { connectionInfo } from '../../src/cli/commands/status.ts';

for (const isPublic of [true, false]) {
  test(`container connection info preserves the administrator login and needs no instance: public=${isPublic}`, () => {
    const config = new ClusterConfig([
      { key: 'global-settings.module_sets.default.cluster.module_id', value: 'cluster' },
      { key: 'cluster.load_balancers.external_alb.load_balancer_dns_name', value: 'portal.example.invalid' },
      { key: 'cluster.network.ssh_key_pair', value: 'sample-key' },
      { key: 'bastion.service_name', value: 'sample-bastion' },
      { key: 'bastion.private_dns_name', value: 'internal.example.invalid' },
      ...(isPublic ? [{ key: 'bastion.public_ip', value: '192.0.2.1' }] : []),
    ], [{ module_id: 'bastion', name: 'bastion-host', type: 'stack', status: 'deployed' }]);
    const entries = connectionInfo(config, 'us-east-2');
    assert.equal(entries.length, 1);
    assert.equal(entries[0]?.value, `ssh -i ~/.ssh/sample-key.pem ec2-user@${isPublic ? '192.0.2.1' : 'internal.example.invalid'}`);
  });
}
