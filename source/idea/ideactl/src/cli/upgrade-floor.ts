/**
 * Direct upgrades require the supported ECS configuration baseline.
 * Older releases must reach it with their published administrator first.
 */

import { ideaVersion } from "../version.ts";

// ECS deployments from this release onward share the supported configuration baseline.
// Lower versions need their published upgrade instructions first.
export const UPGRADE_FLOOR_VERSION = "26.10.0";

// The 26.10.9 administrator upgrades clusters at this release or newer in one run.
export const RECOVERY_FLOOR_VERSION = "25.11.0";

export const RECOVERY_COMMAND = "IDEA_REVISION=v26.10.9 ./idea-admin.sh upgrade-cluster --cluster-name <name> --aws-region <region>";
export const HOST_CONTROL_PLANE_REFUSAL =
  "Host control-plane deployments and upgrades were removed in 26.11. Complete the ECS migration with the 26.10.9 administrator first.\n" +
  `Run from the checkout with your cluster name and region: ${RECOVERY_COMMAND}`;

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
    `Upgrade this cluster to 26.10.9 using the 26.10.9 administrator, then re-run this command.`,
    `Run from the checkout with your cluster name and region: ${RECOVERY_COMMAND}`,
    "",
    `Clusters on ${RECOVERY_FLOOR_VERSION} or newer upgrade to 26.10.9 directly. Only a cluster older than ${RECOVERY_FLOOR_VERSION} first upgrades one published release at a time with that release's administrator, following that release's Upgrade Instructions.`,
  );
  return lines.join("\n");
}

/** The refusal text for a cluster below the floor, or undefined when every deployed module clears it. */
export function upgradeFloorRefusal(cluster: string, modules: readonly FloorModule[]): string | undefined {
  const below = modulesBelowFloor(modules, UPGRADE_FLOOR_VERSION);
  if (below.length === 0) return undefined;
  return upgradeFloorMessage(cluster, ideaVersion(), UPGRADE_FLOOR_VERSION, below);
}
