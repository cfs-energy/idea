/**
 * `constructs/dns.ts`. The resolver endpoint/rule/association are covered end to end by
 * `directory-service.test.ts` (they only ever appear under `ActiveDirectory`); this file covers
 * the private hosted zone against the live dev27 `cluster` template, and the resolver defaults on
 * their own.
 */

import assert from 'node:assert/strict';
import { after, describe, test } from 'node:test';

import { DNSResolverEndpoint, DNSResolverRule, PrivateHostedZone } from '../../src/cdk/constructs/dns.ts';
import { ExistingSocaCluster } from '../../src/cdk/constructs/existing-resources.ts';
import { CLUSTER, cleanup, harness, liveResources, requireLiveFixture } from './harness.ts';
import type { Json } from './harness.ts';

after(cleanup);
requireLiveFixture('cluster');

describe('PrivateHostedZone', () => {
  test('matches the deployed zone, doubled cluster prefix in the Name tag included', () => {
    const h = harness({ moduleId: 'cluster', moduleName: 'cluster' });
    const cluster = new ExistingSocaCluster(h.ctx, h.base.stack);
    new PrivateHostedZone(h.ctx, h.base.stack, cluster.vpc);

    const zone = (h.template().Resources as Json).ideadev27privatehostedzone741B171D;
    const live = liveResources('cluster').ideadev27privatehostedzone741B171D as Json;

    assert.equal(zone.Metadata['aws:cdk:path'], live.Metadata['aws:cdk:path']);
    assert.deepEqual(zone.Properties.HostedZoneConfig, live.Properties.HostedZoneConfig);
    assert.deepEqual(zone.Properties.HostedZoneTags, live.Properties.HostedZoneTags);
    assert.equal(zone.Properties.Name, live.Properties.Name);
    // the live cluster stack creates its own VPC, so its VPCId is a Ref where the imported one is
    // a literal; the region and the arity are what this construct decides
    assert.equal((zone.Properties.VPCs as Json[]).length, 1);
    assert.equal(zone.Properties.VPCs[0].VPCRegion, live.Properties.VPCs[0].VPCRegion);
    assert.equal(
      zone.Properties.VPCs[0].VPCId,
      h.ctx.config.getString('cluster.network.vpc_id', undefined, { required: true }),
    );
  });
});

describe('DNS resolver defaults', () => {
  test('the endpoint defaults to OUTBOUND and the rule to FORWARD, with no port', () => {
    const h = harness({ moduleId: 'directoryservice', moduleName: 'directoryservice' });
    const cluster = new ExistingSocaCluster(h.ctx, h.base.stack);
    const endpoint = new DNSResolverEndpoint(h.ctx, CLUSTER, h.base.stack, {
      securityGroupIds: ['sg-0123456789abcdef0'],
      subnetIds: ['subnet-0123456789abcdef0'],
    });
    new DNSResolverRule(h.ctx, 'sample-rule', h.base.stack, {
      domainName: 'example.invalid',
      vpc: cluster.vpc,
      resolverEndpointId: endpoint.resolverEndpoint.attrResolverEndpointId,
      ipAddresses: ['192.0.2.10'],
    });

    const resources = h.template().Resources as Json;
    const endpointResource = resources.ideadev27dnsresolverendpoint;
    assert.equal(endpointResource.Properties.Direction, 'OUTBOUND');
    assert.equal(endpointResource.Properties.Name, CLUSTER);
    assert.deepEqual(endpointResource.Properties.IpAddresses, [{ SubnetId: 'subnet-0123456789abcdef0' }]);

    const rule = resources.samplerulednsresolverrule;
    assert.equal(rule.Properties.RuleType, 'FORWARD');
    assert.equal(rule.Properties.Name, 'sample-rule-dns-resolver-rule');
    assert.deepEqual(rule.Properties.TargetIps, [{ Ip: '192.0.2.10' }]);
    assert.deepEqual(rule.Properties.Tags, [
      { Key: 'idea:ClusterName', Value: CLUSTER },
      { Key: 'Name', Value: `${CLUSTER}-sample-rule` },
    ]);
  });
});
