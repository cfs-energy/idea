/**
 * Runs the local alternate-shape gate and accepts only full matches or the known
 * missing inputs documented by a reconstructed fixture.
 */

import { match, strictEqual } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { test } from "node:test";

const PKG = resolve(import.meta.dirname, "../..");
const FIXTURES = join(PKG, "tools/parity/fixtures");
const LIVE_TEMPLATES = join(PKG, "tools/parity/live");
const MATRIX = join(PKG, "tools/parity/shapes.ts");
// The reconstructed fixture's tables are real captures now; only its synth-time reads are still
// derived, so the one blocker a reconstructed shape may report is a missing read. The entry goes
// when those reads are captured.
const KNOWN_RECONSTRUCTION_BLOCKER =
  /^could not run: fixture is missing synth-time read (?:sts:GetCallerIdentity|elbv2:DescribeListeners|cognito-idp:DescribeUserPool|iam:ListRoles|opensearch:DescribeDomain):.*$/;
const RECONSTRUCTED_MATCH = "matched on reconstructed inputs, not evidence";

type Shape = {
  name: string;
  reconstructed: boolean;
};

/**
 * Discovers local alternate shapes without embedding private cluster names.
 */
function localShapes(): Shape[] {
  if (!existsSync(FIXTURES)) return [];
  return readdirSync(FIXTURES, { withFileTypes: true })
    .filter((entry) => {
      if (!entry.isDirectory()) return false;
      const fixture = join(FIXTURES, entry.name);
      return (
        !existsSync(join(LIVE_TEMPLATES, `${entry.name}-cluster.json`)) &&
        existsSync(
          join(
            fixture,
            "python/_cdk/cdk.out.cluster",
            `${entry.name}-cluster.template.json`,
          ),
        )
      );
    })
    .map((entry) => ({
      name: entry.name,
      reconstructed: existsSync(join(FIXTURES, entry.name, "DERIVED.md")),
    }))
    .sort((left, right) => left.name.localeCompare(right.name));
}

const SHAPES = localShapes();

test(
  "alternate shapes have no template differences",
  {
    skip: SHAPES.length === 2 ? false : "local alternate-shape references are absent",
    timeout: 420_000,
  },
  () => {
    const result = spawnSync(process.execPath, [MATRIX], {
      encoding: "utf8",
      timeout: 410_000,
    });
    const output = `${result.stdout}${result.stderr}`;

    if (result.error !== undefined) throw result.error;
    strictEqual(result.status === 0 || result.status === 2, true, output);

    const rows = result.stdout
      .split(/\r?\n/)
      .filter((line) => line.includes(" | "))
      .slice(2);
    strictEqual(rows.length, 10, output);

    let matchedCells = 0;
    for (const row of rows) {
      const cells = row.split(" | ").slice(1);
      strictEqual(cells.length, SHAPES.length, row);
      for (const [index, cell] of cells.entries()) {
        const reconstructed = SHAPES[index]?.reconstructed === true;
        // Only a captured fixture can contribute a match. A reconstructed fixture's inputs were
        // derived from the reference templates, so its equal cells carry the marker wording and
        // are not counted here.
        if (cell === "matched") {
          strictEqual(reconstructed, false, row);
          matchedCells += 1;
          continue;
        }
        if (cell === RECONSTRUCTED_MATCH) {
          strictEqual(reconstructed, true, row);
          continue;
        }
        match(cell, KNOWN_RECONSTRUCTION_BLOCKER, row);
        strictEqual(reconstructed, true, row);
      }
    }

    strictEqual(matchedCells >= 2, true, output);
  },
);
