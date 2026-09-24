import assert from "node:assert/strict";
import test from "node:test";

import { compareUpgradeDrift } from "../../src/config/upgrade-drift.ts";

const REPOSITORY = "public.ecr.aws/example/idea-control-plane";

test("a Phase 3 write to a stack-owned key is reported as the Phase 3 target, not the stack's unknown value", () => {
  const report = compareUpgradeDrift({
    current: [
      { key: "ecs.image", value: `${REPOSITORY}:26.09.3`, source: "stack", version: 45 },
      { key: "ecs.image_repositories.aws", value: REPOSITORY, version: 3 },
    ],
    generated: [],
    phase3: [{ key: "ecs.image", value: `${REPOSITORY}:26.09.4` }],
    stacks: [{ moduleId: "ecs", selected: true, previous: { image: `${REPOSITORY}:26.09.3` } }],
  });
  const image = report.findings.find((finding) => finding.key === "ecs.image");
  assert.equal(image?.action, "PHASE3_OVERWRITE");
  assert.equal(image?.effect, "CHANGE");
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
