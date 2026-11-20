/** `config update` refuses a tree rendered for another account than the credentials it runs with. */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { configUpdate } from "../../src/cli/commands/config.ts";
import { fakeDeps } from "../support/deploy-harness.ts";

function tree(account: string): string {
  const root = mkdtempSync(join(tmpdir(), "ideactl-account-"));
  const configDir = join(root, "config");
  mkdirSync(join(configDir, "cluster"), { recursive: true });
  mkdirSync(join(configDir, "global-settings"), { recursive: true });
  writeFileSync(join(configDir, "idea.yml"), "modules:\n  - name: global-settings\n    id: global-settings\n    type: config\n    config_files: [settings.yml]\n"
    + "  - name: cluster\n    id: cluster\n    type: stack\n    config_files: [settings.yml]\n");
  writeFileSync(join(configDir, "global-settings", "settings.yml"), "module_sets:\n  default:\n    cluster:\n      module_id: cluster\n");
  writeFileSync(join(configDir, "cluster", "settings.yml"), `cluster_name: sample-cluster\naws:\n  account_id: "${account}"\n  region: us-east-2\n`);
  return root;
}

for (const [account, refused] of [["111111111111", true], ["222222222222", false]] as const) {
  test(`config update ${refused ? "refuses" : "accepts"} a tree rendered for account ${account} with credentials for 222222222222`, async () => {
    const root = tree(account);
    const deps = Object.assign(fakeDeps(), { callerIdentity: async () => ({ account: "222222222222", arn: "arn" }) });
    try {
      const run = configUpdate(deps, { clusterName: "sample-cluster", awsRegion: "us-east-2", moduleSet: "default", configDir: root, force: true });
      if (refused) {
        await assert.rejects(run, /rendered for account 111111111111, but these credentials are for account 222222222222/);
        assert.deepEqual(deps.writes, []);
      } else {
        await run;
        assert.ok(deps.writes.length > 0);
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
}
