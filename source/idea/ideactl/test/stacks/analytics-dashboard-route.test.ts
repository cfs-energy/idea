/**
 * The OpenSearch Dashboards route on the external load balancer is opt-in. The external ALB is
 * internet-facing and the route forwards without authentication, so a cluster that does not ask
 * for it gets no listener rule, no target group and no private-IP lookup. Turning the setting off
 * on an existing cluster removes the custom resource, and its delete removes the rule. Synthetic
 * settings only, so it runs in a public checkout.
 */

import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import type { StackBuildProps } from '../../src/cdk/app.ts';
import { AnalyticsStack } from '../../src/cdk/stacks/analytics.ts';
import { byType, cleanupWorkdirs, resourcesOf, synthBastion, type Json } from '../support/ecs-harness.ts';

after(cleanupWorkdirs);

const LISTENER = 'arn:aws:elasticloadbalancing:us-east-2:123456789012:listener/app/sample/0123456789abcdef/0123456789abcdef';

const ANALYTICS_SETTINGS: Record<string, unknown> = {
  'cluster.cluster_endpoints_lambda_arn': 'arn:aws:lambda:us-east-2:123456789012:function:sample-endpoints',
  'cluster.load_balancers.external_alb.https_listener_arn': LISTENER,
  'analytics.opensearch.use_existing': false,
  'analytics.opensearch.data_node_instance_type': 'm7g.large.search',
  'analytics.opensearch.data_nodes': 2,
  'analytics.opensearch.ebs_volume_size': 100,
  'analytics.opensearch.removal_policy': 'DESTROY',
  'analytics.opensearch.node_to_node_encryption': true,
  'analytics.opensearch.logging.app_log_enabled': true,
  'analytics.opensearch.logging.slow_index_log_enabled': true,
  'analytics.opensearch.logging.slow_search_log_enabled': true,
  'analytics.opensearch.endpoints.external.priority': 16,
  'analytics.opensearch.endpoints.external.path_patterns': ['/_dashboards*'],
  'analytics.kinesis.shard_count': 2,
  'analytics.kinesis.stream_mode': 'PROVISIONED',
  'analytics.kinesis.removal_policy': 'DESTROY',
};

class SyntheticAnalyticsStack extends AnalyticsStack {
  constructor(props: StackBuildProps) {
    super(props, true);
  }
}

function synthAnalytics(overrides: Record<string, unknown> = {}): Record<string, Json> {
  return resourcesOf(synthBastion({ ...ANALYTICS_SETTINGS, ...overrides }, SyntheticAnalyticsStack, 'analytics'));
}

const DASHBOARD_TYPES = [
  'Custom::DashboardEndpointExternal',
  'AWS::ElasticLoadBalancingV2::TargetGroup',
  'Custom::OpenSearchPrivateIPAddresses',
];

test('the default synth publishes no dashboard route on the external load balancer', () => {
  const resources = synthAnalytics();
  assert.ok(byType(resources, 'AWS::OpenSearchService::Domain').length === 1, 'the domain is still built');
  for (const type of DASHBOARD_TYPES) {
    assert.deepEqual(byType(resources, type).map(([id]) => id), [], `unexpected ${type}`);
  }
});

test('an explicit false matches the default', () => {
  const resources = synthAnalytics({ 'analytics.opensearch.endpoints.external.enabled': false });
  for (const type of DASHBOARD_TYPES) assert.equal(byType(resources, type).length, 0, type);
});

test('opting in restores the route on the external listener', () => {
  const resources = synthAnalytics({ 'analytics.opensearch.endpoints.external.enabled': true });
  const endpoints = byType(resources, 'Custom::DashboardEndpointExternal');
  assert.equal(endpoints.length, 1);
  const [, endpoint] = endpoints[0];
  assert.equal(endpoint.Properties.listener_arn, LISTENER);
  assert.equal(endpoint.Properties.priority, 16);
  assert.deepEqual(endpoint.Properties.conditions, [{ Field: 'path-pattern', Values: ['/_dashboards*'] }]);
  assert.equal(byType(resources, 'AWS::ElasticLoadBalancingV2::TargetGroup').length, 1);
});
