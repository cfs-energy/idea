/**
 * Watches a captured-template directory and verifies that its contents stay unchanged.
 *
 * A filesystem event catches short-lived writes that a final directory snapshot would miss.
 * The snapshots also detect changes when the host does not report an event.
 */

import { createHash } from "node:crypto";
import { readFileSync, readdirSync, watch, type FSWatcher } from "node:fs";
import { join } from "node:path";
import { setTimeout as wait } from "node:timers/promises";

export type OracleDirectoryGuard = Readonly<{
  verifyAndClose: () => Promise<void>;
  close: () => void;
  /** How many watcher events have arrived so far; a test waits on it before a transient write. */
  observedEvents: () => number;
}>;

/**
 * Records every directory entry and hashes every regular file.
 */
function snapshot(directory: string, relativeDirectory = ""): Map<string, string> {
  const state = new Map<string, string>();
  const current = join(directory, relativeDirectory);
  const entries = readdirSync(current, { withFileTypes: true }).sort((left, right) =>
    left.name.localeCompare(right.name),
  );

  for (const entry of entries) {
    const relativePath = join(relativeDirectory, entry.name);
    if (entry.isDirectory()) {
      state.set(relativePath, "directory");
      for (const [path, value] of snapshot(directory, relativePath)) state.set(path, value);
      continue;
    }
    if (entry.isFile()) {
      const digest = createHash("sha256").update(readFileSync(join(directory, relativePath))).digest("hex");
      state.set(relativePath, `file:${digest}`);
      continue;
    }
    state.set(relativePath, "other");
  }
  return state;
}

/**
 * Describes additions, removals, and content changes between two snapshots.
 */
function stateChanges(before: Map<string, string>, after: Map<string, string>): string[] {
  const changes: string[] = [];
  const paths = [...new Set([...before.keys(), ...after.keys()])].sort();
  for (const path of paths) {
    const previous = before.get(path);
    const current = after.get(path);
    if (previous === undefined) changes.push(`added ${path}`);
    else if (current === undefined) changes.push(`removed ${path}`);
    else if (previous !== current) changes.push(`modified ${path}`);
  }
  return changes;
}

/**
 * Starts a guard for one read-only oracle directory.
 */
export function guardOracleDirectory(directory: string): OracleDirectoryGuard {
  const initial = snapshot(directory);
  const events: string[] = [];
  let closed = false;
  const watcher: FSWatcher = watch(directory, (eventType, filename) => {
    events.push(`${eventType} ${filename?.toString() ?? "<unknown>"}`);
  });
  const armed = snapshot(directory);
  const startupChanges = stateChanges(initial, armed);

  const close = (): void => {
    if (closed) return;
    closed = true;
    watcher.close();
  };

  /** Resolves once no event has arrived for 200 milliseconds, or after two seconds. */
  const quiet = async (): Promise<void> => {
    const deadline = Date.now() + 2000;
    let seen = events.length;
    let quietSince = Date.now();
    while (Date.now() < deadline) {
      await wait(20);
      if (events.length !== seen) {
        seen = events.length;
        quietSince = Date.now();
      } else if (Date.now() - quietSince >= 200) {
        return;
      }
    }
  };

  const verifyAndClose = async (): Promise<void> => {
    let final: Map<string, string>;
    try {
      // Filesystem notifications can arrive after the write has already been removed, and under
      // load they arrive late, so wait until the watcher has been quiet for a while rather than
      // for a fixed interval.
      await quiet();
      final = snapshot(directory);
      await quiet();
    } finally {
      close();
    }

    const changes = [...new Set([...startupChanges, ...events, ...stateChanges(initial, final)])].sort();
    if (changes.length > 0) {
      throw new Error(
        [`Oracle directory changed during test run: ${directory}`, ...changes.map((change) => `  ${change}`)].join(
          "\n",
        ),
      );
    }
  };

  return { verifyAndClose, close, observedEvents: () => events.length };
}
