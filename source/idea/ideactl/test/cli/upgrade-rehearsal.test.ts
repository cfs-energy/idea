/** Offline checks of the supported upgrade dry run, using public synthetic fixtures. */

import { marshall } from "@aws-sdk/util-dynamodb";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { loadRegionAmiConfig, resolveRegionAmi } from "../../src/config/region-ami.ts";
import { generateConfigFromTemplates, flattenConfigDir } from "../../src/config/generator.ts";
import { loadValuesFile } from "../../src/config/values.ts";

const PKG = resolve(import.meta.dirname, "../..");

test("P-60: supported ECS generation works with the public fixture", () => {
  const root = mkdtempSync(join(tmpdir(), "upgrade-generation-"));
  try {
    const values = loadValuesFile(join(PKG, "test/stacks/ecs-values.yml"));
    const modules = generateConfigFromTemplates(values, root);
    assert.ok(modules.some((module) => module.id === "ecs"));
    assert.equal(flattenConfigDir(root)["ecs.enabled"], true);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

for (const missingInventory of [false, true]) {
  test(`captured 25.11 shape is refused before writes, missing inventory=${missingInventory}`, () => {
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
        "--out", join(root, "report.txt"),
      ...(missingInventory ? [] : ["--inventory", join(root, "inventory.json")]),
      ], { cwd: PKG, encoding: "utf8", timeout: 30_000 });
      // The rehearsal exits explicitly; a large piped stdout can lose its tail.
      const output = readFileSync(join(root, "report.txt"), "utf8") + result.stderr;
      assert.equal(result.status, 1, output);
      assert.match(output, /below the supported upgrade floor/);
      assert.match(output, /26\.10\.0/);
      assert.match(output, /writes=0 deploys=0 stack-updates=0/);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
}
