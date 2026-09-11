/**
 * Every target group the container stack hands to a service is attached to a listener by the same
 * stack.
 *
 * A service may not name a target group that has no load balancer, and this module deploys before
 * the modules whose stacks create those listener rules today, so without this four of the five
 * services are refused and the stack rolls back.
 *
 * Two properties are asserted here rather than assumed, because both are what keeps this safe on a
 * cluster that already exists:
 *
 * 1. Each endpoint carries the name its module stack uses. The handler keys a rule by that name
 *    and adopts one already carrying it, so the module stack's later resource converges on the
 *    same rule instead of making a second, and on an existing cluster the rule is modified where
 *    it stands rather than replaced. A name that drifts from the module stack's silently turns
 *    adoption back into a collision.
 * 2. A routed endpoint carries a priority and conditions; a listener default action carries
 *    neither a rule nor a priority that could collide.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { synthEcs } from "../o-constraints/harness.ts";

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function resourcesOfType(template: Json, type: string): Array<[string, Json]> {
  const resources = isRecord(template.Resources) ? template.Resources : {};
  return Object.entries(resources).filter(
    (entry): entry is [string, Json] => isRecord(entry[1]) && entry[1].Type === type,
  );
}

/** Logical ids of target groups a resource's properties forward to. */
function forwardedTargetGroups(properties: Json): string[] {
  const actions = Array.isArray(properties.actions) ? properties.actions : [];
  return actions.flatMap((action) => {
    if (!isRecord(action)) return [];
    const arn = action.TargetGroupArn;
    if (!isRecord(arn)) return [];
    // A target group's Ref is its ARN, which is how both the service and the endpoint name it.
    if (typeof arn.Ref === "string") return [arn.Ref];
    const attribute = arn["Fn::GetAtt"];
    return Array.isArray(attribute) && typeof attribute[0] === "string" ? [attribute[0]] : [];
  });
}

const template = synthEcs();
const endpoints = [
  ...resourcesOfType(template, "Custom::EcsEndpoint"),
  ...resourcesOfType(template, "Custom::EcsDefaultEndpoint"),
];

describe("the container stack's endpoints", () => {
  it("attaches every target group a service names", () => {
    const attached = new Set(
      endpoints.flatMap(([, endpoint]) =>
        forwardedTargetGroups(isRecord(endpoint.Properties) ? endpoint.Properties : {}),
      ),
    );
    const named = new Set<string>();
    for (const [, service] of resourcesOfType(template, "AWS::ECS::Service")) {
      const properties = isRecord(service.Properties) ? service.Properties : {};
      for (const balancer of Array.isArray(properties.LoadBalancers) ? properties.LoadBalancers : []) {
        const arn = isRecord(balancer) ? balancer.TargetGroupArn : undefined;
        if (isRecord(arn) && typeof arn.Ref === "string") named.add(arn.Ref);
      }
    }
    // The gateway's target group is the exception by design: its load balancer belongs to the
    // desktop module, which deploys later, and that is why the gateway is being moved there.
    const unattached = [...named].filter((id) => !attached.has(id) && !id.startsWith("gwecs"));
    assert.deepEqual(unattached, [], `target groups a service names with no listener: ${unattached.join(", ")}`);
  });

  it("uses the endpoint names the module stacks use", () => {
    // Read from the module stack sources rather than restated, so a rename there fails here.
    const names = endpoints
      .map(([, endpoint]) => (isRecord(endpoint.Properties) ? endpoint.Properties.endpoint_name : undefined))
      .filter((name): name is string => typeof name === "string")
      .sort();
    assert.deepEqual(names, [
      "broker-client-endpoint",
      "broker-client-endpoint",
      "broker-gateway-endpoint",
      "cm-external-endpoint",
      "cm-internal-endpoint",
      "cm-web-portal-endpoint",
      "scheduler-external-endpoint",
      "scheduler-internal-endpoint",
      "vdc-controller-endpoint-ext",
      "vdc-controller-endpoint-int",
    ]);
  });

  it("gives every routed endpoint a priority and conditions", () => {
    const routed = resourcesOfType(template, "Custom::EcsEndpoint");
    assert.equal(routed.length, 6);
    for (const [id, endpoint] of routed) {
      const properties = isRecord(endpoint.Properties) ? endpoint.Properties : {};
      assert.equal(typeof properties.priority, "number", `${id} priority`);
      assert.ok(Array.isArray(properties.conditions) && properties.conditions.length > 0, `${id} conditions`);
      assert.notEqual(properties.default_action, true, `${id} is a rule, not a default action`);
    }
  });

  it("gives every default action no rule to collide on", () => {
    const defaults = resourcesOfType(template, "Custom::EcsDefaultEndpoint");
    assert.equal(defaults.length, 4);
    for (const [id, endpoint] of defaults) {
      const properties = isRecord(endpoint.Properties) ? endpoint.Properties : {};
      assert.equal(properties.default_action, true, `${id} default action`);
      assert.equal(properties.conditions, undefined, `${id} has no rule conditions`);
    }
  });

  it("creates each service after the endpoints that attach its target groups", () => {
    // Both the service and its endpoint only reference the target group, so without an explicit
    // dependency they are siblings and CloudFormation creates them in parallel. The service wins
    // and is refused for a target group with no load balancer, which is the failure this whole
    // method exists to prevent.
    const endpointIds = new Map<string, string[]>();
    for (const [id, endpoint] of endpoints) {
      for (const group of forwardedTargetGroups(isRecord(endpoint.Properties) ? endpoint.Properties : {})) {
        endpointIds.set(group, [...(endpointIds.get(group) ?? []), id]);
      }
    }
    for (const [serviceId, service] of resourcesOfType(template, "AWS::ECS::Service")) {
      const properties = isRecord(service.Properties) ? service.Properties : {};
      const dependsOn = service.DependsOn;
      const declared = Array.isArray(dependsOn) ? dependsOn : dependsOn === undefined ? [] : [dependsOn];
      for (const balancer of Array.isArray(properties.LoadBalancers) ? properties.LoadBalancers : []) {
        const arn = isRecord(balancer) ? balancer.TargetGroupArn : undefined;
        const group = isRecord(arn) && typeof arn.Ref === "string" ? arn.Ref : undefined;
        if (group === undefined || group.startsWith("gwecs")) continue;
        const required = endpointIds.get(group) ?? [];
        assert.ok(required.length > 0, `${group} has no endpoint`);
        for (const endpointId of required) {
          assert.ok(
            declared.includes(endpointId),
            `${serviceId} must depend on ${endpointId}, declared: ${declared.join(",")}`,
          );
        }
      }
    }
  });

  it("routes each endpoint through the shared handler, so a delete of a gone rule succeeds", () => {
    // Every one of the ten is the same custom resource. The second resource describing an endpoint
    // always deletes nothing, and the handler treats that as success; asserting they all use the
    // one service token is what makes that cover all ten rather than the one under unit test.
    const tokens = new Set(
      endpoints.map(([, endpoint]) =>
        JSON.stringify(isRecord(endpoint.Properties) ? endpoint.Properties.ServiceToken : undefined),
      ),
    );
    assert.equal(tokens.size, 1, `endpoints use ${tokens.size} different handlers`);
  });
});
