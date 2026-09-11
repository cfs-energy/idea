/**
 * Two shapes the container service API refuses that synthesis accepts.
 *
 * Both cost a full deployment to discover: the template is valid, every stack before the container
 * module is created, and the refusal arrives from the service API while the stack rolls back.
 *
 * 1. A container port may appear in only one port mapping. The desktop gateway serves both stream
 *    protocols on 8443, and naming it twice is refused by name.
 * 2. A service may not name a target group that has no load balancer. The gateway's two target
 *    groups were both created and both attached, while the desktop stack attaches only the one
 *    matching the session protocol setting, so the other could never have one.
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

/** Every container definition in the template, by container name. */
function containers(template: Json): Map<string, Json> {
  const found = new Map<string, Json>();
  for (const [, resource] of resourcesOfType(template, "AWS::ECS::TaskDefinition")) {
    const properties = isRecord(resource.Properties) ? resource.Properties : {};
    const definitions = Array.isArray(properties.ContainerDefinitions) ? properties.ContainerDefinitions : [];
    for (const definition of definitions) {
      if (isRecord(definition) && typeof definition.Name === "string") found.set(definition.Name, definition);
    }
  }
  return found;
}

describe("what the container service API accepts", () => {
  it("maps every container port at most once", () => {
    const offenders: string[] = [];
    for (const [name, definition] of containers(synthEcs())) {
      const mappings = Array.isArray(definition.PortMappings) ? definition.PortMappings : [];
      const ports = mappings.flatMap((mapping) =>
        isRecord(mapping) && typeof mapping.ContainerPort === "number" ? [mapping.ContainerPort] : [],
      );
      const duplicates = ports.filter((port, index) => ports.indexOf(port) !== index);
      if (duplicates.length > 0) offenders.push(`${name}: ${[...new Set(duplicates)].join(",")}`);
    }
    assert.deepEqual(offenders, [], `container ports mapped more than once: ${offenders.join("; ")}`);
  });

  it("creates one gateway target group, matching the session protocol setting", () => {
    // Both were created and both attached. The desktop stack attaches one, so the other was a
    // target group with no load balancer, which a service may not name.
    const names = resourcesOfType(synthEcs(), "AWS::ElasticLoadBalancingV2::TargetGroup")
      .map(([, resource]) => (isRecord(resource.Properties) ? resource.Properties.Name : undefined))
      .filter((name): name is string => typeof name === "string")
      .filter((name) => name.includes("gw-ecs"));
    assert.equal(names.length, 1, names.join(","));
  });

  it("names in each service only target groups this stack created", () => {
    // The weaker half of the rule: a service may not name a target group with
    // no load balancer. This stack cannot see a listener, so what it can check is that every
    // target group a service names is one of its own, which keeps the set to audit small.
    const template = synthEcs();
    const created = new Set(
      resourcesOfType(template, "AWS::ElasticLoadBalancingV2::TargetGroup").map(([id]) => id),
    );
    for (const [id, service] of resourcesOfType(template, "AWS::ECS::Service")) {
      const properties = isRecord(service.Properties) ? service.Properties : {};
      const balancers = Array.isArray(properties.LoadBalancers) ? properties.LoadBalancers : [];
      for (const balancer of balancers) {
        const arn = isRecord(balancer) ? balancer.TargetGroupArn : undefined;
        const referenced = isRecord(arn) && typeof arn.Ref === "string" ? arn.Ref : undefined;
        assert.ok(
          referenced !== undefined && created.has(referenced),
          `${id} names a target group this stack did not create: ${JSON.stringify(arn)}`,
        );
      }
    }
  });
});
