/**
 * Offline upgrade-rehearsal checks.
 *
 * Pure tests pin the classifiers. The captured-fixture test runs only when the
 * gitignored development snapshot is available.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { marshall } from "@aws-sdk/util-dynamodb";
import { loadRegionAmiConfig, resolveRegionAmi } from "../../src/config/region-ami.ts";
import { join, resolve } from "node:path";
import test from "node:test";

import {
  findImmutableNameReplacements,
  findLaterWriterReads,
  planConfigurationSync,
} from "../../tools/upgrade-rehearsal/rehearse.ts";

const PKG = resolve(import.meta.dirname, "../..");
const REHEARSAL = join(PKG, "tools/upgrade-rehearsal/rehearse.ts");
const FIXTURE = join(PKG, "tools/parity/fixtures/idea-dev27");
const LIVE = join(PKG, "tools/parity/live");

/** The physical prefix rewrite and add-only pass are classified separately. */
test("plans the exact global rewrite and non-global add-only synchronization", () => {
  const plan = planConfigurationSync(
    [
      { key: "global-settings.same", value: true },
      { key: "global-settings.changed", value: "old" },
      { key: "global-settings.removed", value: 1 },
      { key: "cluster.preserved", value: "operator" },
    ],
    [
      { key: "global-settings.same", value: true },
      { key: "global-settings.changed", value: "new" },
      { key: "global-settings.added", value: [] },
      { key: "cluster.preserved", value: "generated" },
      { key: "cluster.added", value: false },
    ],
  );

  assert.deepEqual(plan.globalDeletes, [
    "global-settings.changed",
    "global-settings.removed",
    "global-settings.same",
  ]);
  assert.deepEqual(plan.globalWrites, [
    "global-settings.added",
    "global-settings.changed",
    "global-settings.same",
  ]);
  assert.deepEqual(plan.addOnlyWrites, ["cluster.added"]);
  assert.deepEqual(plan.changedGlobalRows, ["global-settings.changed"]);
  assert.deepEqual(plan.removedGlobalRows, ["global-settings.removed"]);
  assert.deepEqual(plan.preservedDrift, ["cluster.preserved"]);
});

/** A target writer after its consumer is reported even when an old row masks it. */
test("finds a setting read whose target writer is later in deployment order", () => {
  const findings = findLaterWriterReads(
    ["container", "application"],
    [
      {
        moduleId: "container",
        reads: ["application.role_arn"],
        publishedSettings: ["container.target_group_arns"],
      },
      {
        moduleId: "application",
        reads: ["container.target_group_arns"],
        publishedSettings: ["application.role_arn"],
      },
    ],
  );

  assert.deepEqual(findings, [
    {
      consumer: "container",
      key: "application.role_arn",
      writer: "application",
    },
  ]);
});

/** An explicit target-group Name change is replacement-only. */
test("finds a target group that is replaced rather than updated", () => {
  const before = {
    Resources: {
      DashboardTarget: {
        Type: "AWS::ElasticLoadBalancingV2::TargetGroup",
        Properties: { Name: "idea-test1-before" },
      },
    },
  };
  const after = {
    Resources: {
      DashboardTarget: {
        Type: "AWS::ElasticLoadBalancingV2::TargetGroup",
        Properties: { Name: "idea-test1-after" },
      },
    },
  };

  assert.deepEqual(findImmutableNameReplacements(before, after), [
    {
      logicalId: "DashboardTarget",
      resourceType: "AWS::ElasticLoadBalancingV2::TargetGroup",
      property: "Name",
    },
  ]);
});

/** Return true only when every private input needed by the command is present. */
function capturedFixtureAvailable(): boolean {
  return [
    join(FIXTURE, "raw/cluster-settings.scan.json"),
    join(FIXTURE, "raw/modules.scan.json"),
    join(FIXTURE, "python/values.yml"),
    join(FIXTURE, "synth-reads.json"),
    join(FIXTURE, "cdk.context.json"),
    join(LIVE, "idea-dev27-cluster.json"),
  ].every(existsSync);
}

test(
  "one command reports every stage and the upgrade-only findings from the captured cluster",
  {
    skip: capturedFixtureAvailable() ? false : "captured development fixture is absent",
    timeout: 180_000,
  },
  () => {
    const result = spawnSync(process.execPath, [REHEARSAL], {
      cwd: PKG,
      encoding: "utf8",
      timeout: 170_000,
    });
    if (result.error !== undefined) throw result.error;
    const output = `${result.stdout}${result.stderr}`;

    // The rehearsal must reach the end and report no blocking findings; a blocking finding
    // returning takes this red.
    assert.equal(result.status, 0, output);
    assert.equal((result.stdout.match(/^STAGE /gm) ?? []).length, 22, output);
    assert.equal((result.stdout.match(/^STACK .* SYNTHESIZED /gm) ?? []).length, 11, output);
    assert.match(result.stdout, /^SYNC global_delete=\d+ global_write=\d+ add_only_write=\d+ /m);
    assert.match(result.stdout, /FINDING OBSERVED SEMANTIC_CHANGE /);
    assert.match(result.stdout, /FINDING OBSERVED REPLACEMENT /);
    // The one later-writer read was the deploy-order cycle: the container stack read the module
    // security groups and roles, and the module stacks read the target groups it published. Each
    // module stack owns its own service now, so no stack reads a row a later stack writes. The
    // detector itself is still exercised by the unit test above, which plants one.
    assert.doesNotMatch(result.stdout, /FINDING OBSERVED LATER_WRITER_READ /);
    assert.match(result.stdout, /FINDING RISK TRANSIENT_ABSENCE /);
    // The routed step keeps the hosts and a migration step sets the input that selects it, so
    // neither the missing shape nor the undriven shape is reported any more.
    assert.doesNotMatch(result.stdout, /FINDING BLOCKING ROUTE_STAGE_UNDRIVEN /);
    assert.doesNotMatch(result.stdout, /FINDING BLOCKING ROUTE_STAGE_MISSING /);
    assert.match(result.stdout, /^REAL_CLUSTER_ONLY stages=/m);
    assert.match(result.stdout, /^RESULT blocking_findings=0\b/m);
    // Observed findings are not blocking and are expected to stay. Asserting there are some keeps
    // this from passing on a rehearsal that silently stopped analysing anything.
    assert.match(result.stdout, /^RESULT blocking_findings=0 observed_findings=[1-9]\d*/m);
  },
);

for (const missingInventory of [false, true]) {
  test(`captured 25.11 shape exercises historical replay, missing inventory=${missingInventory}`, () => {
    const root = mkdtempSync(join(tmpdir(), "upgrade-capture-"));
    try {
      const cluster = "idea-test1";
      const region = "us-east-2";
      const stock = resolveRegionAmi(loadRegionAmiConfig(), region, "amazonlinux2023");
      const settings = [
        { key: "cluster.cluster_name", value: cluster },
        { key: "cluster.aws.region", value: region },
        { key: "cluster.cluster_s3_bucket", value: "sample-bucket" },
        { key: "global-settings.module_sets.default.cluster-manager.module_id", value: "cluster-manager" },
        { key: "scheduler.base_os", value: "amazonlinux2023" },
        { key: "scheduler.compute_node_os", value: "amazonlinux2023" },
        { key: "scheduler.compute_node_ami", value: "ami-built" },
        { key: "scheduler.job_provisioning.job_periodic_check_interval_seconds", value: 23 },
        { key: "vdc.dcv_host_role_name", value: "sample-host" },
        { key: "vdc.instance_types.allow", value: ["custom.large"] },
      ];
      const modules = ["cluster", "global-settings", "cluster-manager", "scheduler", "vdc"].map((id) => ({
        module_id: id, name: id === "vdc" ? "virtual-desktop-controller" : id,
        type: id === "global-settings" ? "config" : "app", status: "deployed", version: "25.11.0",
      }));
      const write = (name: string, value: unknown): void => writeFileSync(join(root, name), JSON.stringify(value));
      write("cluster-settings.json", { Items: settings.map((row) => marshall(row)) });
      write("modules.json", { Items: modules.map((row) => marshall(row)) });
      writeFileSync(join(root, "values.yml"), readFileSync(join(PKG, "test/stacks/ecs-values.yml"), "utf8").replace(/^enable_ecs:.*$/m, "enable_ecs: false") + "\nenabled_modules: [scheduler, virtual-desktop-controller]\n");
      mkdirSync(join(root, "templates"));
      for (const module of modules) write(`templates/${cluster}-${module.module_id}.json`, { Resources: {} });
      write("inventory.json", {
        tables: {
          [`${cluster}.scheduler.queue-profiles`]: [],
          [`${cluster}.vdc.controller.software-stacks`]: [{ stack_id: "unused", base_os: "amazonlinux2" }],
          [`${cluster}.vdc.controller.user-sessions`]: [],
        },
        images: [
          { ImageId: "ami-built", Name: "idea-compute-node-sample", CreationDate: "2026-09-01" },
          { ImageId: stock, Name: "stock", CreationDate: "2026-01-01" },
        ],
        stacks: Object.fromEntries(modules.map((module) => [`${cluster}-${module.module_id}`, module.module_id === "scheduler" ? ["i-sample"] : []])),
        protection: { "i-sample": true }, protectionTags: [],
        iam: { "sample-host": { attached: [], inline: ["old"], collision: false, available: 10 } },
        deployedIam: { "sample-host": { attached: ["host-policy"], inline: [], collision: false, available: 10 } },
        deploymentSettings: [{ key: "vdc.dcv_host_policy_arn", value: "arn:aws:iam::sample:policy/host-policy" }],
        instanceTypes: [], openSearchTypes: [], jobs: {}, trunking: false,
      });
      const result = spawnSync(process.execPath, [join(PKG, "tools/parity/upgrade-dry-run.ts"),
        "--capture", root, "--values", join(root, "values.yml"), "--templates", join(root, "templates"),
        ...(missingInventory ? [] : ["--inventory", join(root, "inventory.json")]),
      ], { cwd: PKG, encoding: "utf8", timeout: 30_000 });
      const output = result.stdout + result.stderr;
      assert.equal(result.status, missingInventory ? 1 : 0, output);
      if (missingInventory) assert.match(output, /writes=0 deploys=0 stack-updates=0/);
      else {
        assert.match(output, /Historical migration:/);
        assert.match(output, /keeping built compute image/);
        assert.match(output, /EOL software stack .* deleted/);
        assert.match(output, /EC2 termination protection restored on i-sample/);
        assert.match(output, /Completion verified:/);
        assert.match(output, /RESULT completed/);
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
}
