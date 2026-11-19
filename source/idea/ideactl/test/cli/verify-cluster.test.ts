// The verification reads through injected adapters only; every fake here throws on a write.

import assert from "node:assert/strict";
import test from "node:test";

import { CloudTrailClient } from "@aws-sdk/client-cloudtrail";
import { Command } from "commander";

import { imageRepository, liveClusterHealth, verifyCluster, registerVerifyClusterCommand, type ClusterHealthApi, type VerifyClusterDeps } from "../../src/cli/commands/verify-cluster.ts";

const clusterName = "idea-test1";
const awsRegion = "us-east-2";
const image = "public.ecr.aws/sample/idea:26.11.2";
const oldImage = "public.ecr.aws/sample/idea:26.11.1";
// the scheduler task as ECS reports it: the log sidecars have no health check
const schedulerTask = [
  { task: "task-1", container: "scheduler", health: "HEALTHY" },
  { task: "task-1", container: "scheduler-application-logs", health: "UNKNOWN" },
  { task: "task-1", container: "scheduler-openpbs-logs", health: "UNKNOWN" },
];

interface Fixture {
  deps: VerifyClusterDeps;
  lines: string[];
  health: ClusterHealthApi;
  stacks: Map<string, string>;
  /** Every sleep the check asked for; a sleep moves the injected clock instead of waiting. */
  sleeps: number[];
}

function fixture(): Fixture {
  const lines: string[] = [];
  const sleeps: number[] = [];
  let clock = Date.UTC(2026, 10, 19, 15, 0, 0);
  const stacks = new Map([
    [`${clusterName}-cluster`, "UPDATE_COMPLETE"],
    [`${clusterName}-scheduler`, "UPDATE_COMPLETE"],
    [`${clusterName}-bastion-host`, "UPDATE_COMPLETE"],
    [`${clusterName}-cluster-manager`, "UPDATE_COMPLETE"],
  ]);
  const tables: Record<string, Array<Record<string, unknown>>> = {
    [`${clusterName}.cluster-settings`]: [{ key: "ecs.cluster_name", value: `${clusterName}-ecs` }, { key: "ecs.image", value: image }],
    [`${clusterName}.modules`]: [
      { module_id: "global-settings", name: "global-settings", type: "config", status: "deployed" },
      { module_id: "cluster", name: "cluster", type: "stack", status: "deployed" },
      { module_id: "scheduler", name: "scheduler", type: "app", status: "deployed" },
      { module_id: "bastion-host", name: "bastion-host", type: "stack", status: "deployed" },
      { module_id: "cluster-manager", name: "cluster-manager", type: "app", status: "deployed" },
      { module_id: "analytics", name: "analytics", type: "stack", status: "not-deployed" },
    ],
  };
  const health: ClusterHealthApi = {
    async services() {
      return [
        { name: `${clusterName}-scheduler`, running: 1, desired: 1, rollout: "COMPLETED", images: [image, image, image] },
        { name: `${clusterName}-cluster-manager`, running: 2, desired: 2, rollout: "COMPLETED", images: [image] },
        { name: `${clusterName}-datadog`, running: 3, desired: 3, rollout: "COMPLETED", images: ["public.ecr.aws/datadog/agent:7"] },
      ];
    },
    async containerHealth(input) {
      return input.service.endsWith("scheduler") ? schedulerTask : [{ task: "task-1", container: "bastion", health: "HEALTHY" }];
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
    now() { return clock; },
    async sleep(ms) { sleeps.push(ms); clock += ms; },
    clusterHealth: health,
  };
  return { deps, lines, health, stacks, sleeps };
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
    /\| container health +\| idea-test1-bastion-host +\| bastion UNHEALTHY, no HEALTHY container in 1 task\(s\) +\| FAIL +\|/,
    /\| target group +\| idea-test1-controller +\| healthy 1, unhealthy 1 +\| FAIL +\|/,
  ]) assert.match(rows, row);
  assert.equal(lines.at(-1), "Cluster verification: 6 line(s) FAIL");
});

test("a scheduler task with one HEALTHY container and two log sidecars without a health check passes", async () => {
  const { deps, lines } = fixture();
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), true);
  assert.match(table(lines), /\| container health +\| idea-test1-scheduler +\| 1 container\(s\) HEALTHY, unchecked \(no health check\): scheduler-application-logs scheduler-openpbs-logs +\| ok +\|/);
});

test("an UNHEALTHY container fails its task even beside a HEALTHY one", async () => {
  const { deps, lines, health } = fixture();
  health.containerHealth = async () => [...schedulerTask.slice(0, 2), { task: "task-1", container: "scheduler-openpbs-logs", health: "UNHEALTHY" }];
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), false);
  assert.match(table(lines), /\| container health +\| idea-test1-scheduler +\| scheduler-openpbs-logs UNHEALTHY, unchecked \(no health check\): scheduler-application-logs +\| FAIL +\|/);
});

test("a task with no HEALTHY container fails, also beside a healthy task", async () => {
  const { deps, lines, health } = fixture();
  const starting = schedulerTask.map((container) => ({ ...container, task: "task-2", health: "UNKNOWN" }));
  health.containerHealth = async (input) => input.service.endsWith("scheduler") ? starting : [{ task: "task-1", container: "bastion", health: "HEALTHY" }, { task: "task-2", container: "bastion", health: "UNKNOWN" }];
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), false);
  const rows = table(lines);
  assert.match(rows, /\| container health +\| idea-test1-scheduler +\| scheduler UNKNOWN, scheduler-application-logs UNKNOWN, scheduler-openpbs-logs UNKNOWN, no HEALTHY container in 1 task\(s\) +\| FAIL +\|/);
  assert.match(rows, /\| container health +\| idea-test1-bastion-host +\| bastion UNKNOWN, no HEALTHY container in 1 task\(s\) +\| FAIL +\|/);
});

test("a two-page CloudTrail lookup sends the same window on both pages", async (t) => {
  const { deps, lines, health } = fixture();
  const windows: Array<[number | undefined, number | undefined, string | undefined]> = [];
  t.mock.method(CloudTrailClient.prototype, "send", async (command: { input: { StartTime?: Date; EndTime?: Date; NextToken?: string } }) => {
    windows.push([command.input.StartTime?.getTime(), command.input.EndTime?.getTime(), command.input.NextToken]);
    // the first page takes a moment, as a real one does, so a clock read per page would move
    if (windows.length === 1) { await new Promise((resolve) => setTimeout(resolve, 15)); return { Events: [{ CloudTrailEvent: "{}" }], NextToken: "page-2" }; }
    return { Events: [{ CloudTrailEvent: "{}" }] };
  });
  health.accessDenied = liveClusterHealth().accessDenied;
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), true);
  assert.equal(windows.length, 2);
  assert.deepEqual(windows.map(([start, end]) => [start, end]), [windows[0].slice(0, 2), windows[0].slice(0, 2)]);
  assert.deepEqual(windows.map(([, , token]) => token), [undefined, "page-2"]);
  assert.match(table(lines), /none in 2 event\(s\) since/);
});

test("a scoped upgrade holds only the selected modules' services to the release image", async () => {
  const { deps, lines, health } = fixture();
  const services = health.services;
  health.services = async (input) => (await services(input)).map((service) => service.name.endsWith("cluster-manager") ? { ...service, images: [oldImage] } : service);
  // cluster is selected, cluster-manager is not: a shared name prefix does not make it selected
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion, modules: ["scheduler", "cluster"] }), true);
  assert.match(table(lines), /\| ecs service +\| idea-test1-cluster-manager +\| 2\/2 running, rollout COMPLETED, image public\.ecr\.aws\/sample\/idea:26\.11\.1, not in this upgrade +\| ok +\|/);
  lines.length = 0;
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion, modules: ["cluster-manager"] }), false);
  assert.match(table(lines), /\| ecs service +\| idea-test1-cluster-manager +\| .*image public\.ecr\.aws\/sample\/idea:26\.11\.1 +\| FAIL +\|/);
  lines.length = 0;
  // the standalone check has no module list and holds every service to the release image
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion }), false);
});

// the scheduler and a controller drain their old tasks after the stacks complete, as on dev
function rollouts(health: ClusterHealthApi, rolloutFor: (read: number, name: string) => string): { reads: () => number; order: string[] } {
  const services = health.services;
  const containerHealth = health.containerHealth;
  let reads = 0;
  const order: string[] = [];
  health.services = async (input) => {
    reads += 1;
    order.push("services");
    return (await services(input)).map((service) => service.name.endsWith("-scheduler") || service.name.endsWith("-cluster-manager")
      ? { ...service, rollout: rolloutFor(reads, service.name) } : service);
  };
  health.containerHealth = async (input) => { order.push("containers"); return containerHealth(input); };
  return { reads: () => reads, order };
}

test("at the end of an upgrade, rollouts still draining are waited for and then pass", async () => {
  const { deps, lines, health, sleeps } = fixture();
  const seen = rollouts(health, (read, name) => read <= 2 || (read === 3 && name.endsWith("-cluster-manager")) ? "IN_PROGRESS" : "COMPLETED");
  let settledAt = -1;
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion, settleMs: 600_000, onSettled: () => { settledAt = seen.reads(); } }), true);
  assert.equal(seen.reads(), 4);
  assert.deepEqual(sleeps, [5000, 5000, 5000]);
  assert.equal(settledAt, 4);
  // container health is read once, after the services settled
  assert.deepEqual(seen.order, ["services", "services", "services", "services", "containers", "containers"]);
  assert.deepEqual(lines.filter((line) => line.startsWith("Waiting")), [
    "Waiting for ECS rollouts to finish (old tasks draining), 0s of 600s: idea-test1-scheduler, idea-test1-cluster-manager",
    "Waiting for ECS rollouts to finish (old tasks draining), 10s of 600s: idea-test1-cluster-manager",
  ]);
  assert.equal(lines.filter((line) => line.startsWith("Cluster verification for")).length, 1);
  assert.match(table(lines), /\| ecs service +\| idea-test1-scheduler +\| 1\/1 running, rollout COMPLETED, release image +\| ok +\|/);
  assert.equal(lines.at(-1), "Cluster verification: every checked line is ok");
});

test("a rollout still in progress at the limit fails with the reason", async () => {
  const { deps, lines, health, sleeps } = fixture();
  rollouts(health, (_read, name) => name.endsWith("-scheduler") ? "IN_PROGRESS" : "COMPLETED");
  assert.equal(await verifyCluster(deps, { clusterName, awsRegion, settleMs: 60_000 }), false);
  assert.equal(sleeps.length, 12);
  // one line when the wait starts and one every 30 s while nothing changes
  assert.deepEqual(lines.filter((line) => line.startsWith("Waiting")).map((line) => line.split(",")[1]), [" 0s of 60s: idea-test1-scheduler", " 30s of 60s: idea-test1-scheduler"]);
  assert.match(table(lines), /\| ecs service +\| idea-test1-scheduler +\| 1\/1 running, rollout IN_PROGRESS, release image, rollout still in progress after 60s; run verify-cluster again +\| FAIL +\|/);
  assert.equal(lines.at(-1), "Cluster verification: 1 line(s) FAIL");
});

test("a FAILED rollout, a short service or an old image fails as read and is not waited for", async () => {
  for (const service of [
    { rollout: "FAILED", running: 1, images: [image] },
    { rollout: "IN_PROGRESS", running: 0, images: [image] },
    { rollout: "IN_PROGRESS", running: 1, images: [oldImage] },
  ]) {
    const { deps, lines, health, sleeps } = fixture();
    let reads = 0;
    // beside it, a draining service that settles on the second read
    health.services = async () => [
      { name: `${clusterName}-scheduler`, desired: 1, ...service },
      { name: `${clusterName}-cluster-manager`, running: 2, desired: 2, rollout: (reads += 1) === 1 ? "IN_PROGRESS" : "COMPLETED", images: [image] },
    ];
    assert.equal(await verifyCluster(deps, { clusterName, awsRegion, settleMs: 600_000 }), false);
    assert.deepEqual(sleeps, [5000]);
    const rows = table(lines);
    assert.match(rows, /\| ecs service +\| idea-test1-scheduler +\| [^|]* +\| FAIL +\|/);
    assert.doesNotMatch(rows, /still in progress after/);
    assert.match(rows, /\| ecs service +\| idea-test1-cluster-manager +\| 2\/2 running, rollout COMPLETED, release image +\| ok +\|/);
  }
});

test("the standalone check reads once and reports a draining rollout as wait", async () => {
  const { deps, lines, health, sleeps } = fixture();
  const seen = rollouts(health, () => "IN_PROGRESS");
  const program = new Command("ideactl").exitOverride();
  registerVerifyClusterCommand(program, deps);
  await assert.rejects(program.parseAsync(["verify-cluster", "--cluster-name", clusterName, "--aws-region", awsRegion], { from: "user" }),
    (error: Error) => error.name === "ExitWithCode" && (error as Error & { code: number }).code === 1);
  assert.equal(seen.reads(), 1);
  assert.deepEqual(sleeps, []);
  assert.match(table(lines), /\| ecs service +\| idea-test1-scheduler +\| 1\/1 running, rollout IN_PROGRESS, release image, settling +\| wait +\|/);
  assert.equal(lines.at(-1), "Cluster verification: 2 service(s) still rolling out; run again in a minute");
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
