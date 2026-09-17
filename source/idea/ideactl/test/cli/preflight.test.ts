/**
 * Contract tests for grouped, command-scoped pre-flight checks.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  PreflightRegistry,
  awsvpcTrunkingRemedy,
  createAwsvpcTrunkingCheck,
  createConfigurationDriftCheck,
  createTemplateComparisonCheck,
  renderPreflightReport,
  runPreflight,
  type PreflightContext,
} from "../../src/cli/preflight.ts";

const CONTEXT: Readonly<PreflightContext> = {
  command: "upgrade-cluster",
  account: "123456789012",
  region: "us-east-2",
  cluster: "sample-cluster",
  profile: "sample-profile",
};

/** Register the three checks required before a migration. */
function registryWithThreeFailures(calls: string[]): PreflightRegistry {
  return new PreflightRegistry()
    .register(createAwsvpcTrunkingCheck(async () => {
      calls.push("awsvpc-trunking");
      return false;
    }))
    .register(createTemplateComparisonCheck(async () => {
      calls.push("template-comparison");
      return {
        matches: false,
        remedyCommand: "compare-templates --cluster sample-cluster",
      };
    }))
    .register(createConfigurationDriftCheck(async () => {
      calls.push("configuration-drift");
      return {
        reportHash: "report-123",
        lossKeys: [
          "global-settings.scheduler.queue",
          "scheduler.compute_node_os",
        ],
      };
    }));
}

test("runs all relevant checks and reports every failure as one group", async () => {
  const calls: string[] = [];
  const report = await runPreflight(registryWithThreeFailures(calls), CONTEXT);
  const rendered = renderPreflightReport(report);

  assert.equal(report.passed, false);
  assert.deepEqual(calls.sort(), [
    "awsvpc-trunking",
    "configuration-drift",
    "template-comparison",
  ]);
  assert.equal(report.results.length, 3);
  assert.equal(report.results.every((result) => !result.passed), true);
  assert.match(rendered, /FAIL \[error\] awsvpc-trunking/);
  assert.match(rendered, /FAIL \[error\] template-comparison/);
  assert.match(rendered, /FAIL \[error\] configuration-drift/);
  assert.match(rendered, /123456789012/);
  assert.match(rendered, /us-east-2/);
  assert.match(rendered, /global-settings\.scheduler\.queue/);
  assert.match(rendered, /scheduler\.compute_node_os/);
  assert.match(rendered, /--accept-drift report-123/);
  assert.match(rendered, /FAIL: 0 passed, 3 failed/);
});

test("trunking failure gives the exact account and region remedy", async () => {
  const command = awsvpcTrunkingRemedy(CONTEXT);
  const report = await runPreflight(
    new PreflightRegistry().register(createAwsvpcTrunkingCheck(async () => false)),
    CONTEXT,
  );

  assert.equal(
    command,
    "aws ecs put-account-setting-default --name awsvpcTrunking --value enabled --region us-east-2 --profile sample-profile",
  );
  assert.equal(report.results[0]?.message?.includes("account 123456789012"), true);
  assert.equal(report.results[0]?.message?.includes("region us-east-2"), true);
  assert.equal(report.results[0]?.message?.includes(command), true);
});

test("passes acknowledged drift only when the report hash is unchanged", async () => {
  const registry = new PreflightRegistry()
    .register(createAwsvpcTrunkingCheck(async () => true))
    .register(createTemplateComparisonCheck(async () => ({
      matches: true,
      remedyCommand: "",
    })))
    .register(createConfigurationDriftCheck(async () => ({
      reportHash: "report-current",
      acceptedReportHash: "report-current",
      lossKeys: ["global-settings.scheduler.queue"],
    })));

  const report = await runPreflight(registry, CONTEXT);

  assert.equal(report.passed, true);
  assert.equal(report.results.every((result) => result.passed), true);
  assert.match(renderPreflightReport(report), /PASS: 3 passed, 0 failed/);
});

test("rejects an empty drift hash when losses need acknowledgment", async () => {
  const registry = new PreflightRegistry().register(
    createConfigurationDriftCheck(async () => ({
      reportHash: "",
      acceptedReportHash: "",
      lossKeys: ["scheduler.compute_node_os"],
    })),
  );

  const report = await runPreflight(registry, CONTEXT);

  assert.equal(report.passed, false);
  assert.match(report.results[0]?.message ?? "", /Configuration drift report hash must not be empty/);
});

test("runs only checks registered for the requested command", async () => {
  const calls: string[] = [];
  const registry = new PreflightRegistry()
    .register(createAwsvpcTrunkingCheck(async () => {
      calls.push("awsvpc-trunking");
      return true;
    }))
    .register(createTemplateComparisonCheck(async () => {
      calls.push("template-comparison");
      return { matches: true, remedyCommand: "" };
    }))
    .register(createConfigurationDriftCheck(async () => {
      calls.push("configuration-drift");
      return { reportHash: "", lossKeys: [] };
    }));

  const report = await runPreflight(registry, {
    ...CONTEXT,
    command: "deploy",
  });

  assert.equal(report.passed, true);
  assert.deepEqual(calls, ["awsvpc-trunking"]);
  assert.deepEqual(report.results.map((result) => result.name), ["awsvpc-trunking"]);
});

test("captures a failed probe without suppressing sibling checks", async () => {
  let siblingRan = false;
  const registry = new PreflightRegistry()
    .register(createAwsvpcTrunkingCheck(async () => {
      throw new Error("fixture read failed");
    }))
    .register(createTemplateComparisonCheck(async () => {
      siblingRan = true;
      return { matches: true, remedyCommand: "" };
    }));

  const report = await runPreflight(registry, CONTEXT);

  assert.equal(siblingRan, true);
  assert.equal(report.passed, false);
  assert.match(report.results[0]?.message ?? "", /fixture read failed/);
  assert.match(report.results[0]?.message ?? "", /account 123456789012 in region us-east-2/);
});

test("rejects duplicate check names", () => {
  const registry = new PreflightRegistry().register(
    createAwsvpcTrunkingCheck(async () => true),
  );

  assert.throws(
    () => registry.register(createAwsvpcTrunkingCheck(async () => true)),
    /Duplicate pre-flight check name: awsvpc-trunking/,
  );
});
