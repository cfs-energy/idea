/**
 * The container host group has to be deletable by the platform, unaided.
 *
 * Managed termination protection keeps a scale-in from killing a task mid-flight, and enabling it
 * requires scale-in protection on the group. Nothing removed that protection when the group was
 * meant to go, so the group sat at desired zero with every instance still in service and
 * CloudFormation waited out its own timeout.
 *
 * This is a defect in the recovery from every path rather than in one path: during a rollback none
 * of our code runs, so the release has to be in the stack and it has to be ordered to execute
 * while the group still exists.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { synthEcs } from "../support/ecs-harness.ts";

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

describe("the container host group", () => {
  const template = synthEcs();

  it("still protects running tasks from a scale-in", () => {
    // The release exists so the protection can stay. If this stops being true the release is
    // solving a problem nobody has, and a scale-in can kill the scheduler.
    const [, group] = resourcesOfType(template, "AWS::AutoScaling::AutoScalingGroup")[0] ?? [];
    assert.ok(group !== undefined, "a host auto scaling group");
    const properties = isRecord(group.Properties) ? group.Properties : {};
    assert.equal(properties.NewInstancesProtectedFromScaleIn, true);
    const [, capacityProvider] = resourcesOfType(template, "AWS::ECS::CapacityProvider")[0] ?? [];
    assert.ok(capacityProvider !== undefined, "a capacity provider");
    const managed = isRecord(capacityProvider.Properties)
      ? (capacityProvider.Properties.AutoScalingGroupProvider as Json | undefined)
      : undefined;
    assert.equal(isRecord(managed) ? managed.ManagedTerminationProtection : undefined, "ENABLED");
  });

  it("carries something that releases that protection", () => {
    const releases = resourcesOfType(template, "Custom::ReleaseScaleInProtection");
    assert.equal(releases.length, 1, "one release resource");
  });

  it("orders the release to run while the group still exists", () => {
    // CloudFormation deletes in reverse dependency order, so depending on the group is what puts
    // the release before it on the way out. Without the dependency the two are unordered and the
    // release can run after the group has already failed to drain.
    const [releaseId, release] = resourcesOfType(template, "Custom::ReleaseScaleInProtection")[0] ?? [];
    assert.ok(release !== undefined, "a release resource");
    const [groupId] = resourcesOfType(template, "AWS::AutoScaling::AutoScalingGroup")[0] ?? [];
    assert.ok(groupId !== undefined, "a host auto scaling group");
    const dependsOn = release.DependsOn;
    const declared = Array.isArray(dependsOn) ? dependsOn : dependsOn === undefined ? [] : [dependsOn];
    assert.ok(
      declared.includes(groupId),
      `${String(releaseId)} must depend on ${String(groupId)}, declared: ${declared.join(",")}`,
    );
  });

  it("exists before anything that can fail", () => {
    // Depending on the group alone is not enough. The release and the daemon service are created
    // in parallel, and a failing service cancels the release's own policy, leaving the rollback
    // with nothing to clear the protection. The daemon is the only service left in this stack:
    // each application service is created by the module stack that publishes the rows it reads.
    const withDaemon = synthEcs(true);
    const [releaseId] = resourcesOfType(withDaemon, "Custom::ReleaseScaleInProtection")[0] ?? [];
    assert.ok(releaseId !== undefined, "a release resource");
    const services = resourcesOfType(withDaemon, "AWS::ECS::Service");
    assert.equal(services.length, 1, "the observability daemon is the only service to order against");
    for (const [id, service] of services) {
      const dependsOn = service.DependsOn;
      const declared = Array.isArray(dependsOn) ? dependsOn : dependsOn === undefined ? [] : [dependsOn];
      assert.ok(
        declared.includes(releaseId),
        `${id} must depend on ${String(releaseId)}, declared: ${declared.join(",")}`,
      );
    }
  });

  it("names the group it releases", () => {
    const [, release] = resourcesOfType(template, "Custom::ReleaseScaleInProtection")[0] ?? [];
    const properties = isRecord(release?.Properties) ? release.Properties : {};
    assert.notEqual(properties.AutoScalingGroupName, undefined);
  });
});
