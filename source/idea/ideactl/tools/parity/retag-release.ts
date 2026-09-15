// Moves the local parity reference to a new release string.
//
//   node tools/parity/retag-release.ts <from> <to>      e.g. 26.09.0 26.10.0
//
// The reference under tools/parity/live and tools/parity/fixtures/<cluster> is what the Python
// administrator last deployed, and that administrator is gone, so the reference can never be
// recaptured at a newer release. The release string is the one intended difference between it and
// this tree (stack descriptions, module version tags, the settings resource's version, release
// archive names in host user data), so a release bump rewrites it in place. Everything else in
// the reference stays as deployed. Both directories are ignored by git.

import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [from, to] = process.argv.slice(2);
if (from === undefined || to === undefined || !/^\d+\.\d+\.\d+$/.test(from) || !/^\d+\.\d+\.\d+$/.test(to)) {
  console.error("usage: retag-release.ts <from> <to>   (release strings, e.g. 26.09.0 26.10.0)");
  process.exit(2);
}

const ROOTS = [join(import.meta.dirname, "live"), join(import.meta.dirname, "fixtures")];
const TEXT = /\.(json|yml|yaml|txt)$/;
let files = 0;
let replacements = 0;

function walk(directory: string): void {
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      if (entry === "raw") continue; // captured AWS output stays as captured
      walk(path);
      continue;
    }
    if (!TEXT.test(entry)) continue;
    const before = readFileSync(path, "utf8");
    const count = before.split(from).length - 1;
    if (count === 0) continue;
    writeFileSync(path, before.replaceAll(from, to));
    files += 1;
    replacements += count;
  }
}

for (const root of ROOTS) {
  try { walk(root); } catch { /* a missing root is a tree without a local reference */ }
}
console.log(`retagged ${replacements} occurrence(s) of ${from} as ${to} in ${files} file(s)`);
