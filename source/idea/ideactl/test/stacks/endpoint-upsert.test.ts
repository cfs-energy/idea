/**
 * Creating an endpoint adopts a rule that already carries its name.
 *
 * Two resources can legitimately describe one endpoint. The container stack owns the target group
 * and has to create the rule so its services have a load balancer to name; the module stack
 * creates the same endpoint later in the same run. A plain create collides on the priority, and on
 * a cluster that already has the rule it collides with the rule in service.
 *
 * Adopting converges both: one rule, created once, and on an existing cluster modified where it
 * stands so it keeps its own identity and its priority. That last property is the cutover's, and
 * it is why this is an adoption rather than a delete and recreate.
 *
 * The failure paths get the same attention as the success path, because this is a custom resource:
 * one that cannot answer costs an hour of CloudFormation timeout and then fails the delete too.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type {
  ClusterEndpointsElbv2,
  ClusterEndpointsEvent,
} from "../../src/lambda/idea_custom_resource_cluster_endpoints/index.ts";
import { createHandler } from "../../src/lambda/idea_custom_resource_cluster_endpoints/index.ts";
import type { CfnResponse, CfnResponseSender } from "../../src/lambda/commons/cfn-response.ts";

const CONTEXT = { logStreamName: "synthetic-log-stream" };
const ENDPOINT = "sample-module-endpoint";
const LISTENER = "synthetic-listener";
const EXISTING_RULE = "arn:aws:elasticloadbalancing:us-east-2:123456789012:listener-rule/app/x/y/z";

function event(requestType: ClusterEndpointsEvent["RequestType"]): ClusterEndpointsEvent {
  return {
    RequestType: requestType,
    ResponseURL: "https://example.invalid/response",
    StackId: "synthetic-stack",
    RequestId: "synthetic-request",
    LogicalResourceId: "SyntheticEndpoint",
    ResourceProperties: {
      endpoint_name: ENDPOINT,
      listener_arn: LISTENER,
      default_action: false,
      priority: 12,
      conditions: [{ Field: "path-pattern", Values: ["/sample/*"] }],
      actions: [{ Type: "forward", TargetGroupArn: "synthetic-target-group" }],
    },
  } as ClusterEndpointsEvent;
}

interface Calls {
  created: number;
  modified: string[];
  deleted: string[];
}

/** An ELBv2 stub whose listener either already carries the endpoint's rule or does not. */
function elbv2(present: boolean, calls: Calls, overrides: Partial<ClusterEndpointsElbv2> = {}): ClusterEndpointsElbv2 {
  return {
    describeRules: async () => ({ $metadata: {}, Rules: present ? [{ RuleArn: EXISTING_RULE }] : [] }),
    describeTags: async () => ({
      $metadata: {},
      TagDescriptions: present
        ? [{ ResourceArn: EXISTING_RULE, Tags: [{ Key: "idea:EndpointName", Value: ENDPOINT }] }]
        : [],
    }),
    modifyListener: async () => ({ $metadata: {} }),
    createRule: async () => {
      calls.created += 1;
      return { $metadata: {}, Rules: [{ RuleArn: "synthetic-created-rule" }] };
    },
    modifyRule: async (input) => {
      calls.modified.push(String(input.RuleArn));
      return { $metadata: {} };
    },
    deleteRule: async (input) => {
      calls.deleted.push(String(input.RuleArn));
      return { $metadata: {} };
    },
    ...overrides,
  };
}

async function run(
  requestType: ClusterEndpointsEvent["RequestType"],
  present: boolean,
  overrides: Partial<ClusterEndpointsElbv2> = {},
): Promise<{ responses: CfnResponse[]; calls: Calls }> {
  const responses: CfnResponse[] = [];
  const sender: CfnResponseSender = async (response) => {
    responses.push(response);
  };
  const calls: Calls = { created: 0, modified: [], deleted: [] };
  const handle = createHandler({
    elbv2: () => elbv2(present, calls, overrides),
    responseSender: sender,
    sleep: async () => {},
  });
  await handle(event(requestType), CONTEXT);
  return { responses, calls };
}

describe("creating a cluster endpoint", () => {
  it("creates the rule when the listener has none by that name", async () => {
    const { responses, calls } = await run("Create", false);
    assert.equal(calls.created, 1);
    assert.deepEqual(calls.modified, []);
    assert.equal(responses[0]?.status, "SUCCESS");
  });

  it("adopts the rule already carrying its name, keeping that rule's identity", async () => {
    const { responses, calls } = await run("Create", true);
    assert.equal(calls.created, 0, "no second rule");
    assert.deepEqual(calls.modified, [EXISTING_RULE], "the existing rule is modified in place");
    assert.equal(responses[0]?.status, "SUCCESS");
  });

  it("answers even when the listener cannot be read", async () => {
    // A custom resource that throws without answering is an hour of CloudFormation timeout and a
    // delete that fails afterwards, so the refusal has to arrive as a response.
    const { responses } = await run("Create", false, {
      describeRules: async () => {
        throw new Error("synthetic describe failure");
      },
    });
    assert.equal(responses.length, 1);
    assert.equal(responses[0]?.status, "FAILED");
  });

  it("answers when the rule cannot be created", async () => {
    const { responses } = await run("Create", false, {
      createRule: async () => {
        throw new Error("PriorityInUse");
      },
    });
    assert.equal(responses.length, 1);
    assert.equal(responses[0]?.status, "FAILED");
  });
});

describe("deleting a cluster endpoint", () => {
  it("removes the rule it finds by name", async () => {
    const { responses, calls } = await run("Delete", true);
    assert.deepEqual(calls.deleted, [EXISTING_RULE]);
    assert.equal(responses[0]?.status, "SUCCESS");
  });

  it("succeeds when the rule is already gone", async () => {
    // The second of two resources describing one endpoint deletes nothing, and a teardown that
    // failed there would leave the stack undeletable.
    const { responses, calls } = await run("Delete", false);
    assert.deepEqual(calls.deleted, []);
    assert.equal(responses[0]?.status, "SUCCESS");
  });
});
