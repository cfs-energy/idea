/**
 * The oldest deployed release `upgrade-cluster` will take a cluster from. A jump across more than
 * one published release is not supported: an older cluster is upgraded one release at a time with
 * that release's tool, following its upgrade instructions.
 */

import { ideaVersion } from "../version.ts";

/** Previous published feature release named by the current changelog's upgrade instructions. */
export const UPGRADE_FLOOR_VERSION = "26.09.0";

/** The module-table fields the floor reads; a `ModuleInfo` row satisfies it. */
export interface FloorModule {
  module_id: string;
  type: string;
  status?: unknown;
  version?: unknown;
}

interface ParsedRelease {
  major: number;
  minor: number;
  patch: number;
}

/**
 * Calendar versions and the older three-part scheme share the same N.N.N shape, so integer
 * comparison ranks 3.1.10 below 25.06.1.
 */
function parseIdeaRelease(version: string): ParsedRelease | undefined {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(version.trim());
  if (match === null || match[1] === undefined || match[2] === undefined || match[3] === undefined) {
    return undefined;
  }
  return { major: Number(match[1]), minor: Number(match[2]), patch: Number(match[3]) };
}

/** Negative when left is older, zero when equal, positive when left is newer; undefined when unparsable. */
export function compareIdeaRelease(left: string, right: string): number | undefined {
  const parsedLeft = parseIdeaRelease(left);
  const parsedRight = parseIdeaRelease(right);
  if (parsedLeft === undefined || parsedRight === undefined) return undefined;
  if (parsedLeft.major !== parsedRight.major) return parsedLeft.major - parsedRight.major;
  if (parsedLeft.minor !== parsedRight.minor) return parsedLeft.minor - parsedRight.minor;
  return parsedLeft.patch - parsedRight.patch;
}

/**
 * Deployed app and stack modules whose stored release is missing, unparsable, or strictly below
 * the floor. Config rows and undeployed rows are ignored.
 */
export function modulesBelowFloor<T extends FloorModule>(modules: readonly T[], floor: string): T[] {
  return modules.filter((module) => {
    if (module.type === "config") return false;
    if (module.status !== "deployed") return false;
    const version = typeof module.version === "string" ? module.version.trim() : "";
    if (version === "") return true;
    const compared = compareIdeaRelease(version, floor);
    return compared === undefined || compared < 0;
  });
}

/** Operator-facing refusal for `upgrade-cluster`. */
export function upgradeFloorMessage(
  cluster: string,
  administratorVersion: string,
  floor: string,
  below: readonly FloorModule[],
): string {
  const lines = [
    `Cluster ${cluster} is below the supported upgrade floor.`,
    "",
    `This administrator is ${administratorVersion}. upgrade-cluster supports clusters whose every deployed module is already at ${floor} or newer.`,
    "",
    `Deployed modules below ${floor}:`,
  ];
  for (const module of below) {
    const version = typeof module.version === "string" && module.version.trim() !== "" ? module.version : "(missing)";
    lines.push(`  ${module.module_id}   ${version}`);
  }
  lines.push(
    "",
    `A jump across more than one release is not supported. Upgrade this cluster to ${floor} using the ${floor} administrator, then re-run this command.`,
    "",
    `If the cluster is older than ${floor}, upgrade one published release at a time with that release's administrator, following that release's Upgrade Instructions.`,
  );
  return lines.join("\n");
}

/** The refusal text for a cluster below the floor, or undefined when every deployed module clears it. */
export function upgradeFloorRefusal(cluster: string, modules: readonly FloorModule[]): string | undefined {
  const below = modulesBelowFloor(modules, UPGRADE_FLOOR_VERSION);
  if (below.length === 0) return undefined;
  return upgradeFloorMessage(cluster, ideaVersion(), UPGRADE_FLOOR_VERSION, below);
}
