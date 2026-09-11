/**
 * Enforces machine-local test prerequisites.
 *
 * Captured parity data is intentionally absent from public checkouts. Required
 * checks fail by default. Optional coverage may skip only when the caller
 * explicitly declares that checkout mode.
 */

import { existsSync } from "node:fs";

export const PUBLIC_CHECKOUT_ENV = "IDEACTL_PUBLIC_CHECKOUT";

function missingPaths(paths: readonly string[]): string[] {
  return paths.filter((path) => !existsSync(path));
}

function fixtureFailure(paths: readonly string[], regenerate: string): Error {
  return new Error(
    [
      "Required fixture is missing:",
      ...paths.map((path) => `  ${path}`),
      `Regenerate it with: ${regenerate}`,
    ].join("\n"),
  );
}

/**
 * Require every captured input or oracle used by a test.
 *
 * The error names each missing path and gives the capture command so release
 * checks cannot accidentally pass after their oracle disappears.
 */
export function requireFixtures(paths: readonly string[], regenerate: string): void {
  const missing = missingPaths(paths);
  if (missing.length > 0) throw fixtureFailure(missing, regenerate);
}

/**
 * Require at least one equivalent fixture location.
 */
export function requireAnyFixture(paths: readonly string[], regenerate: string): void {
  if (paths.some((path) => existsSync(path))) return;
  throw fixtureFailure(paths, regenerate);
}

/**
 * Return whether optional capture-based coverage may run.
 *
 * Optional coverage is still required by default. It skips only when
 * `IDEACTL_PUBLIC_CHECKOUT=1` explicitly identifies a checkout with no private
 * captures.
 */
export function optionalFixtures(paths: readonly string[], regenerate: string): boolean {
  const missing = missingPaths(paths);
  if (missing.length === 0) return true;
  if (process.env[PUBLIC_CHECKOUT_ENV] === "1") return false;
  throw fixtureFailure(missing, regenerate);
}

/**
 * Return whether optional service-backed coverage may run.
 */
export function optionalService(available: boolean, name: string, setup: string): boolean {
  if (available) return true;
  if (process.env[PUBLIC_CHECKOUT_ENV] === "1") return false;
  return requiredService(name, setup);
}

/**
 * Fail a test setup when its required local service cannot be reached.
 */
export function requiredService(name: string, setup: string): never {
  throw new Error(`Required service is unavailable: ${name}\nStart it with: ${setup}`);
}
