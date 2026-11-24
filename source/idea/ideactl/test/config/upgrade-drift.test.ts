import assert from "node:assert/strict";
import { test } from "node:test";

import { compareUpgradeDrift } from "../../src/config/upgrade-drift.ts";

test("the preview lists an operator's global row the template does not generate as preserved, as the upgrade keeps it", () => {
  const report = compareUpgradeDrift({
    current: [
      { key: "global-settings.package_config.efs_utils.commit", value: "0123", source: "cli" },
      { key: "global-settings.retired_template_row", value: "old", source: "template" },
    ],
    generated: [],
    replaceGlobalSettings: true,
  });
  const byKey = new Map(report.findings.map((row: { key: string; effect: string }) => [row.key, row] as const));
  assert.equal(byKey.get("global-settings.package_config.efs_utils.commit")?.effect, "PRESERVE");
  assert.equal(byKey.get("global-settings.retired_template_row")?.effect, "DELETE");
});

test("the preview keeps another module set's rows for live modules and deletes those of a module the release dropped", () => {
  const report = compareUpgradeDrift({
    current: [
      { key: "global-settings.module_sets.default.scheduler.module_id", value: "scheduler", source: "template" },
      { key: "global-settings.module_sets.secondary.scheduler.module_id", value: "scheduler-b", source: "template" },
      { key: "global-settings.module_sets.secondary.retired-module.module_id", value: "retired-b" },
      { key: "global-settings.module_sets.default.retired-module.module_id", value: "retired" },
    ],
    generated: [{ key: "global-settings.module_sets.default.scheduler.module_id", value: "scheduler" }],
    replaceGlobalSettings: true,
  });
  const byKey = new Map(report.findings.map((row: { key: string; effect: string }) => [row.key, row.effect] as const));
  assert.equal(byKey.get("global-settings.module_sets.secondary.scheduler.module_id"), "PRESERVE");
  assert.equal(byKey.get("global-settings.module_sets.secondary.retired-module.module_id"), "DELETE");
  assert.equal(byKey.get("global-settings.module_sets.default.retired-module.module_id"), "DELETE");
});
