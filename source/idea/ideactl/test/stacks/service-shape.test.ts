/**
 * Two shapes the container service API refuses that synthesis accepts.
 *
 * Both cost a full deployment to discover: the template is valid, every stack before the failing
 * one is created, and the refusal arrives from the service API while the stack rolls back.
 *
 * 1. A container port may appear in only one port mapping. The desktop gateway serves both stream
 *    protocols on 8443, and naming it twice is refused by name.
 * 2. A service may not name a target group that has no load balancer. Each module stack creates
 *    its target groups beside the listener or the endpoint that serves them, so the pairing is
 *    local; what this checks is that no service reaches outside its own stack for one.
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

const STACKS = ["clusterManager", "scheduler", "vdc"] as const;

describe("what the container service API accepts", () => {
  let templates: ContainerTemplates | undefined;
  const stacks = async (): Promise<ContainerTemplates> => {
    templates ??= await synthContainerStacks();
    return templates;
  };

  it("maps every container port at most once", async () => {
    const all = await stacks();
    const offenders: string[] = [];
    for (const stack of STACKS) {
      for (const [name, definition] of containers(all[stack] as Json)) {
        const mappings = Array.isArray(definition.PortMappings) ? definition.PortMappings : [];
        const ports = mappings.flatMap((mapping) =>
          isRecord(mapping) && typeof mapping.ContainerPort === "number" ? [mapping.ContainerPort] : [],
        );
        const duplicates = ports.filter((port, index) => ports.indexOf(port) !== index);
        if (duplicates.length > 0) offenders.push(`${stack}.${name}: ${[...new Set(duplicates)].join(",")}`);
      }
    }
    assert.deepEqual(offenders, [], `container ports mapped more than once: ${offenders.join("; ")}`);
  });

  it("names in each service only target groups its own stack created", async () => {
    // A service may not name a target group with no load balancer. A stack can see its own
    // listeners, so the check that matters offline is that no service names a group from outside
    // the stack, which is what an imported ARN would be.
    const all = await stacks();
    for (const stack of STACKS) {
      const template = all[stack] as Json;
      const created = new Set(
        resourcesOfType(template, "AWS::ElasticLoadBalancingV2::TargetGroup").map(([id]) => id),
      );
      for (const [id, service] of resourcesOfType(template, "AWS::ECS::Service")) {
        const properties = isRecord(service.Properties) ? service.Properties : {};
        const balancers = Array.isArray(properties.LoadBalancers) ? properties.LoadBalancers : [];
        assert.ok(balancers.length > 0, `${stack}.${id} registers with a target group`);
        for (const balancer of balancers) {
          const arn = isRecord(balancer) ? balancer.TargetGroupArn : undefined;
          const referenced = isRecord(arn) && typeof arn.Ref === "string" ? arn.Ref : undefined;
          assert.ok(
            referenced !== undefined && created.has(referenced),
            `${stack}.${id} names a target group this stack did not create: ${JSON.stringify(arn)}`,
          );
        }
      }
    }
  });
});
