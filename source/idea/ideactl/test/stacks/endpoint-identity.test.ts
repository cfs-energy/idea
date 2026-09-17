/**
 * The endpoint custom resources keep their identity across the container flag.
 *
 * `endpoint_name` is the physical resource id the endpoint handler returns, so
 * changing it makes CloudFormation create the new endpoint and then delete the
 * old one. For an endpoint with `default_action: true` the Delete branch rewrites
 * the listener's default action to a fixed response, which takes the portal, the
 * broker listeners and the desktop gateway offline. The logical id and the
 * resource type are the other two ways to provoke the same replacement, so all
 * three are pinned, with the flag off and with the flag on. With the flag on the
 * endpoints forward to target groups the same stack creates, which is the only
 * change: the identity is what this file holds still.
 */

import assert from "node:assert/strict";
import { after, test } from "node:test";

import type { StackBuilder } from "../../src/cdk/app.ts";
import { buildStack as buildClusterManagerStack } from "../../src/cdk/stacks/cluster-manager.ts";
import { buildStack as buildSchedulerStack } from "../../src/cdk/stacks/scheduler.ts";
import { buildStack as buildVdcStack } from "../../src/cdk/stacks/vdc.ts";
import { requireCapture } from "../support/fixtures.ts";
import {
  CONFIG_FILE,
  CONTEXT_FILE,
  ECS_SHARED_CAPACITY,
  SYNTH_READS_FILE,
  cleanupWorkdirs,
  resourcesOf,
  synthModuleStack,
  type Json,
} from "../support/ecs-harness.ts";

requireCapture(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

after(cleanupWorkdirs);

/** Logical id to the resource identity of every endpoint custom resource. */
function endpointIdentities(template: Json): Record<string, { type: unknown; endpointName: unknown }> {
  const identities: Record<string, { type: unknown; endpointName: unknown }> = {};
  for (const [id, resource] of Object.entries(resourcesOf(template))) {
    const endpointName = (resource["Properties"] as Json | undefined)?.["endpoint_name"];
    if (endpointName === undefined) continue;
    identities[id] = { type: resource["Type"], endpointName };
  }
  return identities;
}

function identity(type: string, endpointName: string): { type: unknown; endpointName: unknown } {
  return { type, endpointName };
}

const CASES: Array<{
  moduleId: string;
  moduleName: string;
  stackBuilder: StackBuilder;
  flagOn: Record<string, { S: string } | { N: string } | { BOOL: boolean } | { L: Array<{ S: string }> }>;
  expected: Record<string, { type: unknown; endpointName: unknown }>;
}> = [
  {
    moduleId: "cluster-manager",
    moduleName: "cluster-manager",
    stackBuilder: buildClusterManagerStack,
    flagOn: ECS_SHARED_CAPACITY,
    expected: {
      webportalendpoint: identity("Custom::WebPortalEndpoint", "cluster-manager-web-portal-endpoint"),
      externalendpoint: identity("Custom::ClusterManagerEndpointExternal", "cluster-manager-external-endpoint"),
      internalendpoint: identity("Custom::ClusterManagerEndpointInternal", "cluster-manager-internal-endpoint"),
    },
  },
  {
    moduleId: "scheduler",
    moduleName: "scheduler",
    stackBuilder: buildSchedulerStack,
    flagOn: ECS_SHARED_CAPACITY,
    expected: {
      externalendpoint: identity("Custom::SchedulerEndpointExternal", "scheduler-external-endpoint"),
      internalendpoint: identity("Custom::SchedulerEndpointInternal", "scheduler-internal-endpoint"),
    },
  },
  {
    moduleId: "vdc",
    moduleName: "virtual-desktop-controller",
    stackBuilder: buildVdcStack,
    flagOn: ECS_SHARED_CAPACITY,
    expected: {
      controllerendpointext: identity("Custom::ControllerEndpointExternal", "vdc-controller-endpoint-ext"),
      controllerendpointint: identity("Custom::ControllerEndpointInternal", "vdc-controller-endpoint-int"),
      dcvbrokerclientendpoint: identity("Custom::DcvBrokerClientEndpointInternal", "broker-client-endpoint"),
      dcvbrokeragentendpoint: identity("Custom::DcvBrokerAgentEndpointInternal", "broker-client-endpoint"),
      dcvbrokergatewayendpoint: identity("Custom::DcvBrokerGatewayEndpointInternal", "broker-gateway-endpoint"),
    },
  },
];

for (const testCase of CASES) {
  test(`${testCase.moduleId} endpoint identities survive the container flag`, async () => {
    const off = endpointIdentities(
      await synthModuleStack({
        moduleId: testCase.moduleId,
        moduleName: testCase.moduleName,
        stackBuilder: testCase.stackBuilder,
      }),
    );
    assert.deepEqual(off, testCase.expected, "the deployed endpoint identities");

    const on = endpointIdentities(
      await synthModuleStack({
        moduleId: testCase.moduleId,
        moduleName: testCase.moduleName,
        stackBuilder: testCase.stackBuilder,
        overrides: testCase.flagOn,
      }),
    );
    assert.deepEqual(on, testCase.expected, "the flag repoints the endpoints without replacing them");
  });
}
