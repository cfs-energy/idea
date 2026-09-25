import assert from "node:assert/strict";
import test from "node:test";

import { compareUpgradeDrift, renderUpgradeDrift } from "../../src/config/upgrade-drift.ts";

const REPOSITORY = "public.ecr.aws/example/idea-control-plane";

test("a Phase 3 write to a stack-owned key is reported as the Phase 3 target, not the stack's unknown value", () => {
  const report = compareUpgradeDrift({
    current: [
      { key: "ecs.image", value: `${REPOSITORY}:26.09.3`, source: "stack", version: 45 },
      { key: "ecs.image_repositories.aws", value: REPOSITORY, version: 3 },
    ],
    generated: [{ key: "ecs.image", value: `${REPOSITORY}:26.09.4` }],
    phase3: [{ key: "ecs.image", value: `${REPOSITORY}:26.09.4` }],
    stacks: [{ moduleId: "ecs", selected: true, previous: { image: `${REPOSITORY}:26.09.3` } }],
  });
  const image = report.findings.find((finding) => finding.key === "ecs.image");
  assert.equal(image?.action, "PHASE3_OVERWRITE");
  assert.equal(image?.effect, "CHANGE");
  assert.equal(image?.routineUpdate, "release image");
  assert.deepEqual(report.changedRowsDifferingFromGenerated, []);
  assert.equal(report.findings.filter((finding) => finding.key === "ecs.image").length, 1);
});

test("a stack-owned key without a Phase 3 write keeps the stack classification", () => {
  const report = compareUpgradeDrift({
    current: [{ key: "ecs.cluster_name", value: "cluster-ecs", source: "stack", version: 27 }],
    generated: [],
    stacks: [{ moduleId: "ecs", selected: true, previous: { cluster_name: "cluster-ecs" } }],
  });
  assert.equal(report.findings.find((finding) => finding.key === "ecs.cluster_name")?.action, "STACK_OVERWRITE");
});


test("only template-marked global rewrites are exempt; add-only settings remain preserved", () => {
  for (const source of ["template", "api", "cli", "stack", "unknown", undefined]) {
    const report = compareUpgradeDrift({
      current: [
        { key: "global-settings.default", value: ["old"], source },
        { key: "global-settings.type", value: "old", source },
        { key: "scheduler.instance_type", value: "old", source },
      ],
      generated: [
        { key: "global-settings.default", value: ["new"] },
        { key: "global-settings.type", value: ["new"] },
        { key: "scheduler.instance_type", value: "new" },
      ],
    });
    assert.deepEqual(report.changedRowsDifferingFromGenerated,
      source === "template" ? [] : ["global-settings.default", "global-settings.type"]);
    assert.equal(report.findings.find((row) => row.key === "scheduler.instance_type")?.effect, "PRESERVE");
    const rendered = renderUpgradeDrift(report);
    assert.equal(rendered.includes("Template defaults updated:"), source === "template");
    assert.ok(rendered.includes(`source=${source ?? "-"}`));
  }
});

test("template marker does not exempt a Phase 3 or stack overwrite", () => {
  const report = compareUpgradeDrift({
    current: [
      { key: "scheduler.instance_ami", value: "old", source: "template" },
      { key: "global-settings.stack_owned", value: "old", source: "template" },
    ],
    generated: [
      { key: "scheduler.instance_ami", value: "new" },
      { key: "global-settings.stack_owned", value: "new" },
    ],
    phase3: [{ key: "scheduler.instance_ami", value: "new" }],
    stacks: [{ moduleId: "global-settings", selected: true, previous: { stack_owned: "old" }, target: { stack_owned: "new" } }],
  });
  assert.deepEqual(report.changedRowsDifferingFromGenerated, ["scheduler.instance_ami", "global-settings.stack_owned"]);
});
