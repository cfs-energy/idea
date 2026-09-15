/**
 * Every target group a container service names is attached to a load balancer by the same stack,
 * before the service is created.
 *
 * A service may not name a target group that has no load balancer. Each module stack creates its
 * own target groups beside the endpoints that route to them, so the attachment is local; what this
 * pins is that the local endpoint really covers every group its service names, and that the
 * service is ordered after it. Both the service and the endpoint only reference the target group,
 * so without an explicit dependency they are siblings and CloudFormation creates them in parallel:
 * the service wins the race and is refused.
 *
 * Two further properties are asserted because they are what keeps this safe on a cluster that
 * already exists:
 *
 * 1. Each endpoint keeps the name its module stack has always used. The handler keys a rule by that
 *    name and adopts one already carrying it, so on an existing cluster the rule is modified where
 *    it stands rather than replaced.
 * 2. A routed endpoint carries a priority and conditions; a listener default action carries neither
 *    a rule nor a priority that could collide.
 */

import assert from "node:assert/strict";
import { after, describe, it } from "node:test";

import { requireFixtures } from "../support/fixtures.ts";
import {
  CONFIG_FILE,
  CONTEXT_FILE,
  SYNTH_READS_FILE,
  cleanupWorkdirs,
  synthContainerStacks,
  type ContainerTemplates,
} from "../support/ecs-harness.ts";

requireFixtures(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

after(cleanupWorkdirs);

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function resources(template: Json): Array<[string, Json]> {
  const all = isRecord(template.Resources) ? template.Resources : {};
  return Object.entries(all).filter((entry): entry is [string, Json] => isRecord(entry[1]));
}

function resourcesOfType(template: Json, type: string): Array<[string, Json]> {
  return resources(template).filter(([, resource]) => resource.Type === type);
}

/** Every endpoint custom resource in a module stack: the ones carrying an `endpoint_name`. */
function endpointsOf(template: Json): Array<[string, Json]> {
  return resources(template).filter(
    ([, resource]) => isRecord(resource.Properties) && typeof resource.Properties.endpoint_name === "string",
  );
}

/** Logical ids of target groups a resource's properties forward to. */
function forwardedTargetGroups(properties: Json): string[] {
  const actions = Array.isArray(properties.actions)
    ? properties.actions
    : Array.isArray(properties.DefaultActions)
      ? properties.DefaultActions
      : [];
  return actions.flatMap((action) => {
    if (!isRecord(action)) return [];
    const arn = action.TargetGroupArn;
    if (!isRecord(arn)) return [];
    // A target group's Ref is its ARN, which is how the service, the endpoint and the listener all
    // name it.
    if (typeof arn.Ref === "string") return [arn.Ref];
    const attribute = arn["Fn::GetAtt"];
    return Array.isArray(attribute) && typeof attribute[0] === "string" ? [attribute[0]] : [];
  });
}

/** Everything in one stack that gives a target group a load balancer, by logical id. */
function attachments(template: Json): Map<string, string[]> {
  const byGroup = new Map<string, string[]>();
  const attachers = [...endpointsOf(template), ...resourcesOfType(template, "AWS::ElasticLoadBalancingV2::Listener")];
  for (const [id, resource] of attachers) {
    const properties = isRecord(resource.Properties) ? resource.Properties : {};
    for (const group of forwardedTargetGroups(properties)) {
      byGroup.set(group, [...(byGroup.get(group) ?? []), id]);
    }
  }
  return byGroup;
}

function dependsOn(resource: Json): string[] {
  const declared = resource.DependsOn;
  return Array.isArray(declared) ? declared.map(String) : declared === undefined ? [] : [String(declared)];
}

/** The endpoint names each module stack registers, which are its physical resource identities. */
const ENDPOINT_NAMES: Record<keyof ContainerTemplates, string[]> = {
  clusterManager: [
    "cluster-manager-external-endpoint",
    "cluster-manager-internal-endpoint",
    "cluster-manager-web-portal-endpoint",
  ],
  scheduler: ["scheduler-external-endpoint", "scheduler-internal-endpoint"],
  vdc: [
    // The broker agent endpoint registers under the client endpoint's name, which is how it is
    // deployed today: the name is the custom resource's physical identity.
    "broker-client-endpoint",
    "broker-client-endpoint",
    "broker-gateway-endpoint",
    "vdc-controller-endpoint-ext",
    "vdc-controller-endpoint-int",
  ],
};

const STACKS = ["clusterManager", "scheduler", "vdc"] as const;

describe("the container services' target groups", () => {
  let templates: ContainerTemplates | undefined;
  const stacks = async (): Promise<ContainerTemplates> => {
    templates ??= await synthContainerStacks();
    return templates;
  };

  it("attaches every target group a service names, in the same stack", async () => {
    const all = await stacks();
    for (const stack of STACKS) {
      const template = all[stack];
      const attached = attachments(template);
      for (const [serviceId, service] of resourcesOfType(template, "AWS::ECS::Service")) {
        const properties = isRecord(service.Properties) ? service.Properties : {};
        const balancers = Array.isArray(properties.LoadBalancers) ? properties.LoadBalancers : [];
        assert.ok(balancers.length > 0, `${stack}.${serviceId} registers with a target group`);
        for (const balancer of balancers) {
          const arn = isRecord(balancer) ? balancer.TargetGroupArn : undefined;
          const group = isRecord(arn) && typeof arn.Ref === "string" ? arn.Ref : undefined;
          assert.ok(group !== undefined, `${stack}.${serviceId} names a target group of this stack`);
          assert.ok(
            (attached.get(group) ?? []).length > 0,
            `${stack}.${serviceId} names ${group}, which nothing in this stack gives a load balancer`,
          );
        }
      }
    }
  });

  it("creates each service after the endpoints that attach its target groups", async () => {
    const all = await stacks();
    for (const stack of STACKS) {
      const template = all[stack];
      const attached = attachments(template);
      for (const [serviceId, service] of resourcesOfType(template, "AWS::ECS::Service")) {
        const properties = isRecord(service.Properties) ? service.Properties : {};
        const declared = dependsOn(service);
        for (const balancer of Array.isArray(properties.LoadBalancers) ? properties.LoadBalancers : []) {
          const arn = isRecord(balancer) ? balancer.TargetGroupArn : undefined;
          const group = isRecord(arn) && typeof arn.Ref === "string" ? arn.Ref : undefined;
          if (group === undefined) continue;
          for (const attacherId of attached.get(group) ?? []) {
            assert.ok(
              declared.includes(attacherId),
              `${stack}.${serviceId} must depend on ${attacherId}, declared: ${declared.join(",")}`,
            );
          }
        }
      }
    }
  });

  it("keeps the endpoint names the module stacks have always registered", async () => {
    const all = await stacks();
    for (const stack of STACKS) {
      const names = endpointsOf(all[stack])
        .map(([, endpoint]) => (isRecord(endpoint.Properties) ? endpoint.Properties.endpoint_name : undefined))
        .filter((name): name is string => typeof name === "string")
        .sort();
      assert.deepEqual(names, ENDPOINT_NAMES[stack], `${stack} endpoint names`);
    }
  });

  it("gives every routed endpoint a priority and conditions, and every default action neither", async () => {
    const all = await stacks();
    let routed = 0;
    let defaults = 0;
    for (const stack of STACKS) {
      for (const [id, endpoint] of endpointsOf(all[stack])) {
        const properties = isRecord(endpoint.Properties) ? endpoint.Properties : {};
        if (properties.default_action === true) {
          defaults += 1;
          assert.equal(properties.conditions, undefined, `${stack}.${id} has no rule conditions`);
          assert.equal(properties.priority, 0, `${stack}.${id} has no priority to collide on`);
          continue;
        }
        routed += 1;
        assert.equal(typeof properties.priority, "number", `${stack}.${id} priority`);
        assert.ok(Array.isArray(properties.conditions) && properties.conditions.length > 0, `${stack}.${id} conditions`);
      }
    }
    assert.equal(routed, 6, "cluster-manager external and internal, scheduler both, controller both");
    assert.equal(defaults, 4, "the web portal and the three broker listeners");
  });

  it("routes each endpoint through the shared handler, so a delete of a gone rule succeeds", async () => {
    // The second resource describing an endpoint always deletes nothing, and the handler treats
    // that as success; asserting they all use one service token per stack is what makes that
    // cover every endpoint rather than the one under unit test.
    const all = await stacks();
    for (const stack of STACKS) {
      const tokens = new Set(
        endpointsOf(all[stack]).map(([, endpoint]) =>
          JSON.stringify(isRecord(endpoint.Properties) ? endpoint.Properties.ServiceToken : undefined),
        ),
      );
      assert.equal(tokens.size, 1, `${stack} endpoints use ${tokens.size} different handlers`);
    }
  });
});
