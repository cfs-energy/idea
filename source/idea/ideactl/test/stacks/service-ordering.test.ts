/**
 * Every container service starts only after the identities it runs with carry their policies, and
 * after its own module's settings resource, because the application reads `client_id` and the rest
 * of that module's rows from the table at boot.
 *
 * The settings dependency is why the published `asg_name` and `asg_arn` are literals rather than
 * references: a settings resource that referenced the service would have to be created after it,
 * while the service has to be created after the settings. Both directions are pinned here, because
 * reintroducing the reference is how the cycle comes back.
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
  type Json,
} from "../support/ecs-harness.ts";

requireFixtures(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

after(cleanupWorkdirs);

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function resourcesOfType(template: Json, type: string): Array<[string, Json]> {
  const resources = isRecord(template.Resources) ? template.Resources : {};
  return Object.entries(resources).filter(
    (entry): entry is [string, Json] => isRecord(entry[1]) && entry[1].Type === type,
  );
}

function dependsOn(resource: Json): string[] {
  const declared = isRecord(resource) ? resource.DependsOn : undefined;
  return Array.isArray(declared) ? declared.map(String) : declared === undefined ? [] : [String(declared)];
}

/** Every logical id a value refers to, by Ref or Fn::GetAtt. */
function referencedIds(value: unknown, found: Set<string>): void {
  if (Array.isArray(value)) {
    for (const item of value) referencedIds(item, found);
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, item] of Object.entries(value)) {
    if (key === "Ref" && typeof item === "string") {
      found.add(item);
      continue;
    }
    if (key === "Fn::GetAtt") {
      const target = Array.isArray(item) ? item[0] : String(item).split(".")[0];
      if (typeof target === "string") found.add(target);
      continue;
    }
    referencedIds(item, found);
  }
}

/** Services per stack, so a stack that stops creating one fails rather than passing vacuously. */
const SERVICE_COUNT: Record<keyof ContainerTemplates, number> = {
  clusterManager: 1,
  scheduler: 1,
  vdc: 3,
};

const STACKS = ["clusterManager", "scheduler", "vdc"] as const;

describe("container service ordering", () => {
  let templates: ContainerTemplates | undefined;
  const stacks = async (): Promise<ContainerTemplates> => {
    templates ??= await synthContainerStacks();
    return templates;
  };

  it("creates the services the module stacks own", async () => {
    const all = await stacks();
    for (const stack of STACKS) {
      assert.equal(resourcesOfType(all[stack], "AWS::ECS::Service").length, SERVICE_COUNT[stack], stack);
    }
  });

  it("starts each service after every policy attached to its task role", async () => {
    const all = await stacks();
    for (const stack of STACKS) {
      const template = all[stack];
      const policies = resourcesOfType(template, "AWS::IAM::Policy");
      for (const [serviceId, service] of resourcesOfType(template, "AWS::ECS::Service")) {
        const properties = isRecord(service.Properties) ? service.Properties : {};
        const taskDefinitionRef = isRecord(properties.TaskDefinition) ? properties.TaskDefinition.Ref : undefined;
        const [, taskDefinition] =
          resourcesOfType(template, "AWS::ECS::TaskDefinition").find(([id]) => id === taskDefinitionRef) ?? [];
        const taskRoleRef =
          isRecord(taskDefinition?.Properties) && isRecord(taskDefinition.Properties.TaskRoleArn)
            ? (taskDefinition.Properties.TaskRoleArn["Fn::GetAtt"] as string[] | undefined)?.[0]
            : undefined;
        assert.notEqual(taskRoleRef, undefined, `${stack}.${serviceId} names a task role`);
        const attached = policies
          .filter(([, policy]) => {
            const roles = isRecord(policy.Properties) && Array.isArray(policy.Properties.Roles) ? policy.Properties.Roles : [];
            return roles.some((role) => isRecord(role) && role.Ref === taskRoleRef);
          })
          .map(([id]) => id);
        assert.ok(
          attached.length >= 2,
          `${stack}.${serviceId}: inline and default policies on ${String(taskRoleRef)}, found ${attached.join(",")}`,
        );
        const declared = dependsOn(service);
        for (const policyId of attached) {
          assert.ok(
            declared.includes(policyId),
            `${stack}.${serviceId} must depend on ${policyId}, declared: ${declared.join(",")}`,
          );
        }
      }
    }
  });

  it("starts each service after its own module's settings resource", async () => {
    const all = await stacks();
    for (const stack of STACKS) {
      const template = all[stack];
      const [settingsId] = resourcesOfType(template, "Custom::ClusterSettings")[0] ?? [];
      assert.notEqual(settingsId, undefined, `${stack} publishes settings`);
      for (const [serviceId, service] of resourcesOfType(template, "AWS::ECS::Service")) {
        assert.ok(
          dependsOn(service).includes(String(settingsId)),
          `${stack}.${serviceId} must depend on ${String(settingsId)}`,
        );
      }
    }
  });

  it("publishes the service identity without referencing the service", async () => {
    const all = await stacks();
    for (const stack of STACKS) {
      const template = all[stack];
      const serviceIds = new Set(resourcesOfType(template, "AWS::ECS::Service").map(([id]) => id));
      const [settingsId, settings] = resourcesOfType(template, "Custom::ClusterSettings")[0] ?? [];
      assert.notEqual(settings, undefined, `${stack} publishes settings`);
      const referenced = new Set<string>();
      referencedIds(isRecord(settings) ? settings.Properties : undefined, referenced);
      const offenders = [...referenced].filter((id) => serviceIds.has(id));
      assert.deepEqual(
        offenders,
        [],
        `${String(settingsId)} references ${offenders.join(",")}, which is the cycle the literals avoid`,
      );
      assert.deepEqual(dependsOn(isRecord(settings) ? settings : {}), [], `${String(settingsId)} waits for nothing`);
    }
  });

  it("names every service explicitly, which is what the published identity spells out", async () => {
    const all = await stacks();
    const names: string[] = [];
    for (const stack of STACKS) {
      for (const [serviceId, service] of resourcesOfType(all[stack], "AWS::ECS::Service")) {
        const properties = isRecord(service.Properties) ? service.Properties : {};
        assert.equal(typeof properties.ServiceName, "string", `${stack}.${serviceId} carries a service name`);
        names.push(properties.ServiceName as string);
      }
    }
    assert.deepEqual(names.sort(), [
      "idea-dev27-cluster-manager",
      "idea-dev27-scheduler",
      "idea-dev27-vdc-broker",
      "idea-dev27-vdc-controller",
      "idea-dev27-vdc-gateway",
    ]);
  });
});
