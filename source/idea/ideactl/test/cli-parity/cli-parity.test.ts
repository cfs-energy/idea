import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import type { Command, Option as CommandOption } from "commander";

import { buildProgram, liveDeps } from "../../src/cli/main.ts";
import {
  compareSurfaces,
  evaluateDifferences,
  extractNewSurface,
  extractOriginalSurface,
  formatReport,
  parityExitCode,
  parseIntentions,
  runChecker,
} from "../../tools/cli-parity/check.ts";

const originalSource = readFileSync(
  new URL(
    "../../../idea-administrator/src/ideaadministrator/app_main.py",
    import.meta.url,
  ),
  "utf8",
);
const mainSource = readFileSync(
  new URL("../../src/cli/main.ts", import.meta.url),
  "utf8",
);

/** Find one child command and fail with a useful message when the surface changes unexpectedly. */
function child(parent: Command, name: string): Command {
  const command = parent.commands.find((candidate) => candidate.name() === name);
  assert.ok(command, `command not found: ${name}`);
  return command;
}

test("extracts original declarations without executing the original program", () => {
  const surface = extractOriginalSurface(originalSource);
  assert.equal(surface.commands.length, 50);
  assert.deepEqual(surface.exitCodes, [0, 1, 2]);

  const integrationTests = surface.commands.find(
    (command) => command.path === "run-integration-tests",
  );
  assert.ok(integrationTests);
  assert.deepEqual(
    integrationTests.options.find((option) => option.key === "--param")?.spellings,
    ["--param", "-p"],
  );

  const status = surface.commands.find(
    (command) => command.path === "check-cluster-status",
  );
  assert.equal(
    status?.options.find((option) => option.key === "--wait-timeout")?.defaultValue,
    "900",
  );
});

/**
 * Lines under one report heading, stopping at the next heading or at `(none)`.
 * Used so this test watches classified members, not the headings the printer always emits.
 */
function reportSectionMembers(report: string, heading: string): string[] {
  const lines = report.split("\n");
  const start = lines.indexOf(heading);
  assert.notEqual(start, -1, `report missing ${heading}`);
  const members: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (line === "(none)" || line.startsWith("SUMMARY ") || /^[A-Z][A-Z_]+$/.test(line)) {
      break;
    }
    if (line.startsWith("- ")) {
      members.push(line);
    }
  }
  return members;
}

test("prints all three difference classes", () => {
  const original = extractOriginalSurface(originalSource);
  const current = extractNewSurface(buildProgram(liveDeps()), mainSource);
  assert.ok(original.commands.some((command) => command.path === "build-bootstrap-package"));
  assert.equal(
    current.commands.some((command) => command.path === "build-bootstrap-package"),
    false,
  );
  assert.ok(current.commands.some((command) => command.path === "migrate"));
  assert.equal(original.commands.some((command) => command.path === "migrate"), false);
  assert.notDeepEqual(original.exitCodes, current.exitCodes);

  const result = runChecker();
  assert.match(result.report, /^CLI_PARITY_REPORT$/mu);
  assert.match(result.report, /^PRESENT_IN_ORIGINAL_AND_MISSING_HERE$/mu);
  assert.match(result.report, /^PRESENT_HERE_AND_NOT_IN_ORIGINAL$/mu);
  assert.match(result.report, /^PRESENT_IN_BOTH_BUT_DIFFERING$/mu);
  assert.match(result.report, /^SUMMARY /mu);

  const missingHere = reportSectionMembers(result.report, "PRESENT_IN_ORIGINAL_AND_MISSING_HERE");
  const extraHere = reportSectionMembers(result.report, "PRESENT_HERE_AND_NOT_IN_ORIGINAL");
  const differing = reportSectionMembers(result.report, "PRESENT_IN_BOTH_BUT_DIFFERING");
  assert.ok(
    missingHere.some((line) => line.startsWith("- command build-bootstrap-package ")),
    `original-only section lost build-bootstrap-package: ${missingHere.join("\n")}`,
  );
  assert.ok(
    extraHere.some((line) => line.startsWith("- command migrate ")),
    `new-only section lost migrate: ${extraHere.join("\n")}`,
  );
  assert.ok(
    differing.some((line) => line.startsWith("- exit codes ")),
    `differing section lost exit codes: ${differing.join("\n")}`,
  );

  const summary = /^SUMMARY total=(\d+) accepted=(\d+) unexplained=(\d+) stale=(\d+)$/mu.exec(
    result.report,
  );
  assert.ok(summary, `missing SUMMARY line in:\n${result.report}`);
  const total = Number.parseInt(summary[1] ?? "", 10);
  const unexplained = Number.parseInt(summary[3] ?? "", 10);
  const stale = Number.parseInt(summary[4] ?? "", 10);
  assert.ok(total > 0, `classifier reported no differences:\n${result.report}`);
  assert.equal(result.exitCode, unexplained > 0 || stale > 0 ? 1 : 0);
});

test("fails when an option is removed from the new command tree", () => {
  const baseline = extractNewSurface(buildProgram(liveDeps()), mainSource);
  const changedProgram = buildProgram(liveDeps());
  const setCommand = child(child(changedProgram, "config"), "set");
  const optionIndex = setCommand.options.findIndex(
    (option) => option.long === "--aws-region",
  );
  assert.notEqual(optionIndex, -1);
  (setCommand.options as CommandOption[]).splice(optionIndex, 1);

  const changed = extractNewSurface(changedProgram, mainSource);
  const evaluation = evaluateDifferences(
    compareSurfaces(baseline, changed),
    {},
  );
  assert.equal(parityExitCode(evaluation), 1);
  assert.match(
    formatReport(evaluation),
    /option config set --aws-region .* \[UNEXPLAINED\]/u,
  );
});

test("requires a non-empty reason for every accepted difference", () => {
  assert.throws(
    () => parseIntentions("{\"new-only:option:test:--flag\":\"\"}"),
    /non-empty reason/u,
  );
  assert.deepEqual(parseIntentions("{}"), {});
});
