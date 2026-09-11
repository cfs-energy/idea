/**
 * Contract for the multi-release upgrade floor. Nothing under src implements it yet, so the
 * comparison and the operator message live here until a caller exists.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkgRoot = join(here, "..", "..");
const repoRoot = join(pkgRoot, "..", "..", "..");

/** Previous published feature release named by the 26.09.0 changelog Upgrade Instructions. */
const UPGRADE_FLOOR_VERSION = "26.08.0";

interface DeployedModule {
  moduleId: string;
  name: string;
  type: string;
  status: string;
  version: string | null;
}

interface ParsedRelease {
  major: number;
  minor: number;
  patch: number;
}

/**
 * Parses an IDEA release string. Calendar versions and the older three-part scheme
 * share the same N.N.N shape, so integer comparison ranks 3.1.10 below 25.06.1.
 */
function parseIdeaRelease(version: string): ParsedRelease | undefined {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(version.trim());
  if (match === null || match[1] === undefined || match[2] === undefined || match[3] === undefined) {
    return undefined;
  }
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
  };
}

/** Negative when left is older, zero when equal, positive when left is newer. */
function compareIdeaRelease(left: string, right: string): number | undefined {
  const parsedLeft = parseIdeaRelease(left);
  const parsedRight = parseIdeaRelease(right);
  if (parsedLeft === undefined || parsedRight === undefined) {
    return undefined;
  }
  if (parsedLeft.major !== parsedRight.major) return parsedLeft.major - parsedRight.major;
  if (parsedLeft.minor !== parsedRight.minor) return parsedLeft.minor - parsedRight.minor;
  return parsedLeft.patch - parsedRight.patch;
}

/**
 * Deployed app and stack modules whose stored release is missing, unparseable,
 * or strictly below the floor. Config rows and undeployed rows are ignored.
 */
function modulesBelowFloor(modules: readonly DeployedModule[], floor: string): DeployedModule[] {
  return modules.filter((module) => {
    if (module.type === "config") return false;
    if (module.status !== "deployed") return false;
    if (module.version === null || module.version.trim() === "") return true;
    const compared = compareIdeaRelease(module.version, floor);
    return compared === undefined || compared < 0;
  });
}

/** Operator-facing refusal for upgrade-cluster. */
function upgradeFloorMessage(
  cluster: string,
  administratorVersion: string,
  floor: string,
  below: readonly DeployedModule[],
): string {
  const lines = [
    `Cluster ${cluster} is below the supported upgrade floor.`,
    "",
    `This administrator is ${administratorVersion}. upgrade-cluster supports clusters whose every deployed module is already at ${floor} or newer.`,
    "",
    `Deployed modules below ${floor}:`,
  ];
  for (const module of below) {
    const version = module.version === null || module.version.trim() === "" ? "(missing)" : module.version;
    lines.push(`  ${module.moduleId}   ${version}`);
  }
  lines.push(
    "",
    `A jump across more than one release is not supported. Upgrade this cluster to ${floor} using the ${floor} administrator, then re-run this command.`,
    "",
    `If the cluster is older than ${floor}, upgrade one published release at a time with that release's administrator, following that release's Upgrade Instructions.`,
  );
  return lines.join("\n");
}

test("calendar and legacy three-part releases compare numerically", () => {
  const ordered = ["3.1.10", "25.06.1", "25.12.0", "26.08.0", "26.09.0"];
  for (let index = 0; index < ordered.length - 1; index += 1) {
    const older = ordered[index];
    const newer = ordered[index + 1];
    if (older === undefined || newer === undefined) continue;
    const compared = compareIdeaRelease(older, newer);
    assert.notEqual(compared, undefined);
    assert.ok((compared ?? 0) < 0, `${older} must rank below ${newer}`);
  }
  assert.equal(compareIdeaRelease("26.08.0", "26.08.0"), 0);
  assert.ok((compareIdeaRelease("26.09.0", "26.08.0") ?? 0) > 0);
  assert.equal(compareIdeaRelease("26.09.0-dev", UPGRADE_FLOOR_VERSION), undefined);
});

test("the 26.09.0 upgrade floor ignores config rows and undeployed rows", () => {
  const modules: DeployedModule[] = [
    { moduleId: "global-settings", name: "global-settings", type: "config", status: "deployed", version: null },
    { moduleId: "cluster", name: "cluster", type: "stack", status: "deployed", version: "26.08.0" },
    { moduleId: "metrics", name: "metrics", type: "stack", status: "not-deployed", version: null },
  ];
  assert.deepEqual(modulesBelowFloor(modules, UPGRADE_FLOOR_VERSION), []);
});

test("deployed modules older than 26.08.0, missing, or unparseable fail the floor", () => {
  const modules: DeployedModule[] = [
    { moduleId: "cluster", name: "cluster", type: "stack", status: "deployed", version: "25.12.0" },
    { moduleId: "scheduler", name: "scheduler", type: "app", status: "deployed", version: "26.08.0" },
    { moduleId: "vdc", name: "virtual-desktop-controller", type: "app", status: "deployed", version: null },
    { moduleId: "analytics", name: "analytics", type: "stack", status: "deployed", version: "not-a-version" },
  ];
  const below = modulesBelowFloor(modules, UPGRADE_FLOOR_VERSION);
  assert.deepEqual(
    below.map((module) => module.moduleId),
    ["cluster", "vdc", "analytics"],
  );
});

test("the upgrade-cluster refusal names the floor, the below-floor modules, and the operator path", () => {
  const below: DeployedModule[] = [
    { moduleId: "cluster", name: "cluster", type: "stack", status: "deployed", version: "25.12.0" },
    { moduleId: "scheduler", name: "scheduler", type: "app", status: "deployed", version: "25.12.0" },
  ];
  const message = upgradeFloorMessage("sample-cluster", "26.09.0", UPGRADE_FLOOR_VERSION, below);
  assert.match(message, /Cluster sample-cluster is below the supported upgrade floor/);
  assert.match(message, /already at 26\.08\.0 or newer/);
  assert.match(message, /cluster {3}25\.12\.0/);
  assert.match(message, /scheduler {3}25\.12\.0/);
  assert.match(message, /Upgrade this cluster to 26\.08\.0 using the 26\.08\.0 administrator/);
  assert.doesNotMatch(message, /--force/);
});

test("Python upgrade-cluster does not compare module release versions", () => {
  const source = readFileSync(
    join(repoRoot, "source", "idea", "idea-administrator", "src", "ideaadministrator", "app_main.py"),
    "utf8",
  );
  const start = source.indexOf("def upgrade_cluster(");
  const end = source.indexOf("\ndef _update_values_base_os(");
  assert.ok(start >= 0 && end > start, "upgrade_cluster source range");
  const body = source.slice(start, end);
  assert.equal(body.includes("current_release_version"), false);
  assert.doesNotMatch(body, /module\[[\"']version[\"']\]/);
  assert.doesNotMatch(body, /supported.?floor|min(?:imum)?_version|below the supported/i);
});

test("the ported upgrade-cluster command has no release-floor option or comparison", () => {
  const source = readFileSync(join(pkgRoot, "src", "cli", "commands", "upgrade.ts"), "utf8");
  assert.doesNotMatch(source, /UPGRADE_FLOOR_VERSION|supported-release-floor|minVersion/);
  assert.doesNotMatch(source, /below the supported upgrade floor/);
  const command = source.slice(source.indexOf('.command("upgrade-cluster")'));
  assert.doesNotMatch(command, /--min-version|--supported-floor/);
});
