// The verification reads through injected adapters only; every fake here throws on a write.

import assert from "node:assert/strict";
import test from "node:test";

import { Command } from "commander";

import { imageRepository, verifyCluster, registerVerifyClusterCommand, type ClusterHealthApi, type VerifyClusterDeps } from "../../src/cli/commands/verify-cluster.ts";

const clusterName = "idea-test1";
const awsRegion = "us-east-2";
const image = "public.ecr.aws/sample/idea:26.11.2";

interface Fixture {
  deps: VerifyClusterDeps;
  lines: string[];
  health: ClusterHealthApi;
  stacks: Map<string, string>;
}

function fixture(): Fixture {
  const lines: string[] = [];
  const stacks = new Map([
    [`${clusterName}-cluster`, "UPDATE_COMPLETE"],
    [`${clusterName}-scheduler`, "UPDATE_COMPLETE"],
    [`${clusterName}-bastion-host`, "UPDATE_COMPLETE"],
  ]);
  const tables: Record<string, Array<Record<string, unknown>>> = {
    [`${clusterName}.cluster-settings`]: [{ key: "ecs.cluster_name", value: `${clusterName}-ecs` }, { key: "ecs.image", value: image }],
    [`${clusterName}.modules`]: [
      { module_id: "global-settings", name: "global-settings", type: "config", status: "deployed" },
      { module_id: "cluster", name: "cluster", type: "stack", status: "deployed" },
      { module_id: "scheduler", name: "scheduler", type: "app", status: "deployed" },
      { module_id: "bastion-host", name: "bastion-host", type: "stack", status: "deployed" },
      { module_id: "analytics", name: "analytics", type: "stack", status: "not-deployed" },
    ],
  };
  const health: ClusterHealthApi = {
    async services() {
      return [
        { name: `${clusterName}-scheduler`, running: 1, desired: 1, rollout: "COMPLETED", images: [image] },
        { name: `${clusterName}-datadog`, running: 3, desired: 3, rollout: "COMPLETED", images: ["public.ecr.aws/datadog/agent:7"] },
      ];
    },
    async containerHealth(input) {
      return [{ task: "task-1", container: input.service.endsWith("scheduler") ? "scheduler" : "bastion", health: "HEALTHY" }];
    },
    async targetGroups() {
      return [{ name: "idea-test1-scheduler", states: ["healthy"] }, { name: "idea-test1-bastion", states: ["healthy", "draining"] }];
    },
    async accessDenied() {
      return { events: [], scanned: 12, complete: true };
    },
  };
  const deps: VerifyClusterDeps = {
    async scan(input) {
      return { Items: tables[input.TableName] ?? [] };
    },
    cfn: {
      async describeStack(name) {
        const status = stacks.get(name);
        if (status === undefined) throw new Error(`Stack with id ${name} does not exist`);
        return { StackStatus: status };
      },
      async describeChangeSet() { throw new Error("verification must not read change sets"); },
      async executeChangeSet() { throw new Error("verification must not change a stack"); },
    },
    out(line) { lines.push(line); },
    now() { return Date.UTC(2026, 10, 19, 15, 0, 0); },
    clusterHealth: health,
  };
  return { deps, lines, health, stacks };
}

const table = (lines: string[]): string => lines.find((line) => line.startsWith("Cluster verification for")) ?? "";

test("a settled cluster passes every line, and the not-deployed module is not read", async () => {
  const { deps, lines } = fixture();
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), true);
  const rows = table(lines);
  for (const row of [
    /\| stack +\| idea-test1-cluster +\| UPDATE_COMPLETE +\| ok +\|/,
    /\| ecs service +\| idea-test1-scheduler +\| 1\/1 running, rollout COMPLETED, release image +\| ok +\|/,
    // a sidecar from another repository is not held to the release image
    /\| ecs service +\| idea-test1-datadog +\| 3\/3 running, rollout COMPLETED, no release image +\| ok +\|/,
    /\| container health +\| idea-test1-bastion-host +\| 1 container\(s\) HEALTHY +\| ok +\|/,
    /\| target group +\| idea-test1-bastion +\| healthy 1, draining 1 +\| ok +\|/,
    /\| access denied +\| idea-test1-\* roles +\| none in 12 event\(s\) since 2026-11-19T14:00:00.000Z +\| ok +\|/,
  ]) assert.match(rows, row);
  assert.doesNotMatch(rows, /analytics|global-settings/);
  assert.equal(lines.at(-1), "Cluster verification: every checked line is ok");
});

test("each unsettled state fails its own line and the whole check", async () => {
  const { deps, lines, health, stacks } = fixture();
  stacks.set(`${clusterName}-scheduler`, "UPDATE_ROLLBACK_COMPLETE");
  health.services = async () => [
    { name: `${clusterName}-scheduler`, running: 0, desired: 1, rollout: "IN_PROGRESS", images: [image] },
    { name: `${clusterName}-vdc-controller`, running: 2, desired: 2, rollout: "COMPLETED", images: ["public.ecr.aws/sample/idea:26.11.1"] },
  ];
  health.containerHealth = async (input) => input.service.endsWith("scheduler") ? [] : [{ task: "t", container: "bastion", health: "UNHEALTHY" }];
  health.targetGroups = async () => [{ name: "idea-test1-controller", states: ["healthy", "unhealthy"] }];
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), false);
  const rows = table(lines);
  for (const row of [
    /\| stack +\| idea-test1-scheduler +\| UPDATE_ROLLBACK_COMPLETE +\| FAIL +\|/,
    /\| ecs service +\| idea-test1-scheduler +\| 0\/1 running, rollout IN_PROGRESS, release image +\| FAIL +\|/,
    /\| ecs service +\| idea-test1-vdc-controller +\| .*image public\.ecr\.aws\/sample\/idea:26\.11\.1 +\| FAIL +\|/,
    /\| container health +\| idea-test1-scheduler +\| no running task +\| FAIL +\|/,
    /\| container health +\| idea-test1-bastion-host +\| bastion UNHEALTHY +\| FAIL +\|/,
    /\| target group +\| idea-test1-controller +\| healthy 1, unhealthy 1 +\| FAIL +\|/,
  ]) assert.match(rows, row);
  assert.equal(lines.at(-1), "Cluster verification: 6 line(s) FAIL");
});

test("no permission for the CloudTrail lookup reads 'not checked' and never fails the check", async () => {
  const { deps, lines, health } = fixture();
  health.accessDenied = async () => { throw Object.assign(new Error("not authorized"), { name: "AccessDeniedException" }); };
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), true);
  assert.match(table(lines), /\| access denied +\| idea-test1-\* roles +\| no permission for cloudtrail:LookupEvents +\| not checked +\|/);
  health.accessDenied = async () => { throw new Error("socket hang up"); };
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), true);
});

test("an access denial from a cluster role since the upgrade started fails the check, and a partial window says so", async () => {
  const { deps, lines, health } = fixture();
  const seen: Array<{ since: Date; rolePrefix: string; maxEvents: number }> = [];
  health.accessDenied = async (input) => {
    seen.push(input);
    return { events: [{ time: "2026-11-19T14:30:00Z", eventName: "GetSecretValue", role: "idea-test1-scheduler-task-role-us-east-2", errorCode: "AccessDenied" }], scanned: 500, complete: false };
  };
  const since = Date.UTC(2026, 10, 19, 14, 21, 23);
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion, since }), false);
  assert.deepEqual(seen, [{ awsRegion, awsProfile: undefined, since: new Date(since), rolePrefix: "idea-test1-", maxEvents: 500 }]);
  assert.match(table(lines), /1 in 500 event\(s\) since 2026-11-19T14:21:23.000Z, the newest 500 only: idea-test1-scheduler-task-role-us-east-2 GetSecretValue AccessDenied at 2026-11-19T14:30:00Z +\| FAIL/);
});

test("without the AWS readers the service, target and access lines read 'not checked'", async () => {
  const { deps, lines } = fixture();
  delete deps.clusterHealth;
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), true);
  assert.equal((table(lines).match(/not checked/g) ?? []).length, 3);
});

test("verify-cluster exits 1 on a failed line and reads its window from --since-minutes", async () => {
  const { deps, health, stacks } = fixture();
  const windows: Date[] = [];
  health.accessDenied = async (input) => { windows.push(input.since); return { events: [], scanned: 0, complete: true }; };
  const run = async (...args: string[]) => {
    const program = new Command("ideactl").exitOverride();
    registerVerifyClusterCommand(program, deps);
    await program.parseAsync(["verify-cluster", "--cluster-name", clusterName, "--aws-region", awsRegion, ...args], { from: "user" });
  };
  await run("--since-minutes", "30");
  assert.deepEqual(windows, [new Date(Date.UTC(2026, 10, 19, 14, 30, 0))]);
  stacks.set(`${clusterName}-cluster`, "UPDATE_FAILED");
  await assert.rejects(run(), (error: Error) => error.name === "ExitWithCode" && (error as Error & { code: number }).code === 1);
  await assert.rejects(run("--since-minutes", "soon"), /--since-minutes must be a positive number/);
});

test("the image repository ignores the tag, the digest and a registry port", () => {
  assert.equal(imageRepository("public.ecr.aws/sample/idea:26.11.2"), "public.ecr.aws/sample/idea");
  assert.equal(imageRepository("public.ecr.aws/sample/idea:26.11.2@sha256:" + "a".repeat(64)), "public.ecr.aws/sample/idea");
  assert.equal(imageRepository("registry.example:5000/idea"), "registry.example:5000/idea");
  assert.equal(imageRepository("registry.example:5000/idea:26.11.2"), "registry.example:5000/idea");
});
