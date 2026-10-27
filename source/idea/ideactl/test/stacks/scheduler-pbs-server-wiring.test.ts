/**
 * The batch server names itself from the scheduler task's IDEA_SCHEDULER_DNS_NAME
 * (roles/scheduler.sh), and every client is pointed at the `scheduler.private_dns_name` row the
 * same stack publishes: desktops and compute nodes read it from settings, the bastion through
 * IDEA_PBS_SERVER. test_pbs_server_name.py proves the two derivations agree for one input; this
 * proves both stacks feed them that one input. Synthetic settings only, so it runs in a public
 * checkout, where scheduler-stack.test.ts does not.
 */

import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { SchedulerStack } from '../../src/cdk/stacks/scheduler.ts';
import { byType, cleanupWorkdirs, onlyOne, resourcesOf, synthBastion, type Json } from '../support/ecs-harness.ts';

after(cleanupWorkdirs);

const HOSTNAME = 'scheduler.idea-test1.us-east-2.local';
const LISTENER = 'arn:aws:elasticloadbalancing:us-east-2:123456789012:listener/app/sample/0123456789abcdef/0123456789abcdef';

/** The rows the scheduler stack needs beyond the shared synthetic container settings. */
const SCHEDULER_SETTINGS = {
  'scheduler.hostname': HOSTNAME,
  'idp.cognito.user_pool_id': 'us-east-2_sample',
  'cluster.cluster_endpoints_lambda_arn': 'arn:aws:lambda:us-east-2:123456789012:function:sample-endpoints',
  'cluster.load_balancers.external_alb.https_listener_arn': LISTENER,
  'cluster.load_balancers.internal_alb.https_listener_arn': LISTENER,
  'scheduler.endpoints.external.priority': 100,
  'scheduler.endpoints.external.path_patterns': [],
  'scheduler.endpoints.internal.priority': 100,
  'scheduler.endpoints.internal.path_patterns': [],
  'ecs.host_security_group_id': 'sg-0123456789abcdef9',
};

function environmentOf(resources: Record<string, Json>, container: string, name: string): unknown {
  const [, task] = onlyOne(byType(resources, 'AWS::ECS::TaskDefinition'), 'AWS::ECS::TaskDefinition');
  const definition = task.Properties.ContainerDefinitions.find((entry: Json) => entry.Name === container);
  assert.ok(definition, `no ${container} container`);
  return definition.Environment.find((entry: Json) => entry.Name === name)?.Value;
}

test('the scheduler names itself and the bastion PBS_SERVER from one value', () => {
  const scheduler = resourcesOf(synthBastion(SCHEDULER_SETTINGS, SchedulerStack, 'scheduler'));
  const serverSource = environmentOf(scheduler, 'scheduler-container', 'IDEA_SCHEDULER_DNS_NAME');
  const [, row] = onlyOne(byType(scheduler, 'Custom::ClusterSettings'), 'Custom::ClusterSettings');
  // the row lands as <module_id>.private_dns_name, which is the key the clients read
  assert.equal(row.Properties.module_id, 'scheduler');
  const published = row.Properties.settings.private_dns_name;
  assert.equal(serverSource, HOSTNAME);
  assert.equal(published, serverSource);

  const bastion = resourcesOf(synthBastion({ 'scheduler.private_dns_name': published }));
  assert.equal(environmentOf(bastion, 'bastion', 'IDEA_PBS_SERVER'), serverSource);
});
