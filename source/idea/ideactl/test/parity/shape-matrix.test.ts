/**
 * Alternate-shape matrix tests use synthetic fixture names and a deterministic synthesis
 * subprocess. The subprocess exercises matrix discovery, status mapping, failure propagation,
 * and the regression-gate exit status without embedding private fixture data in this file.
 */

import { match, strictEqual } from "node:assert";
import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

const PKG = resolve(import.meta.dirname, "../..");
const MATRIX = join(PKG, "tools/parity/shapes.ts");
const REAL_FIXTURES = join(PKG, "tools/parity/fixtures");
const LIVE_TEMPLATES = join(PKG, "tools/parity/live");

/**
 * Writes the minimum reference assembly needed for one discovered shape.
 *
 * A reconstructed shape carries the marker that declares its inputs derived from the reference
 * templates, which is what keeps its cells out of the gate's evidence.
 */
function writeShape(root: string, shape: string, stacks: string[], reconstructed: boolean): void {
  const fixture = join(root, shape);
  if (reconstructed) {
    writeFileSync(join(fixture, "DERIVED.md"), "Synthetic reconstructed fixture.\n", {
      flag: "w",
      flush: true,
    });
  }
  for (const stack of stacks) {
    const out = join(fixture, "python/_cdk", `cdk.out.${stack}`);
    mkdirSync(out, { recursive: true });
    writeFileSync(join(out, `${shape}-${stack}.template.json`), "{\"Resources\":{}}\n");
  }
}

/**
 * Writes a synthesis stand-in that emits each supported matrix result.
 */
function writeSynthProgram(root: string): string {
  const program = join(root, "synth.mjs");
  writeFileSync(
    program,
    [
      "const value = (name) => process.argv[process.argv.indexOf(name) + 1];",
      "const cluster = value(\"--cluster\");",
      "const stack = value(\"--stack\");",
      "const mismatch = () => {",
      "  console.log(\"DIFF     Resources.marker.Properties.Name\\n  live:  \\\"before\\\"\\n  synth: \\\"after\\\"\");",
      "  console.log(\"MISMATCH  1 live resources, 0 missing, 0 extra, 1 property diffs, 0 soft\");",
      "  process.exit(1);",
      "};",
      "if (process.env.BREAK_MATCH === \"1\" && cluster.startsWith(\"idea-test1\") && stack === \"metrics\") mismatch();",
      "if (process.env.MATRIX_MODE === \"mixed\") {",
      "  if (cluster.startsWith(\"idea-test1\") && stack === \"metrics\") {",
      "    console.log(\"PARITY  1 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft\");",
      "    process.exit(0);",
      "  }",
      "  if (cluster.startsWith(\"sample-cluster\") && stack === \"cluster\") mismatch();",
      "  console.error(\"ConfigKeyNotFound [Error]: 'roles', key: cluster.iam.roles\");",
      "  process.exit(2);",
      "}",
      "console.log(\"PARITY  1 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft\");",
    ].join("\n"),
  );
  return program;
}

/**
 * Runs the matrix against an isolated synthetic fixture root.
 */
function runMatrix(
  fixtures: string,
  synthProgram: string,
  environment: Record<string, string> = {},
): SpawnSyncReturns<string> {
  return spawnSync(
    process.execPath,
    [MATRIX, "--fixtures", fixtures, "--synth", synthProgram],
    {
      encoding: "utf8",
      env: { ...process.env, ...environment },
    },
  );
}

test("one command prints matched, differed, and missing-input verdicts for every cell", () => {
  const root = mkdtempSync(join(tmpdir(), "shape-matrix-"));
  try {
    const first = "idea-test1-new-network";
    const second = "sample-cluster-existing-network";
    mkdirSync(join(root, first), { recursive: true });
    mkdirSync(join(root, second), { recursive: true });
    writeShape(root, first, ["cluster", "metrics"], false);
    writeShape(root, second, ["cluster", "metrics"], true);
    const result = runMatrix(root, writeSynthProgram(root), { MATRIX_MODE: "mixed" });
    const output = `${result.stdout}${result.stderr}`;

    strictEqual(result.status, 1, output);
    match(output, /^STACK \| idea-test1-new-network \| sample-cluster-existing-network$/m);
    match(
      output,
      /^cluster \| could not run: fixture is missing required setting cluster\.iam\.roles \| differed: MISMATCH /m,
    );
    match(
      output,
      /^metrics \| matched \| could not run: reconstructed fixture is missing required setting cluster\.iam\.roles$/m,
    );
    match(output, /^\[sample-cluster-existing-network \/ cluster\]$/m);
    match(output, /^DIFF     Resources\.marker\.Properties\.Name$/m);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("an equal template on a reconstructed fixture is not a match", () => {
  const root = mkdtempSync(join(tmpdir(), "shape-reconstructed-"));
  try {
    const captured = "idea-test1-new-network";
    const derived = "sample-cluster-existing-network";
    mkdirSync(join(root, captured), { recursive: true });
    mkdirSync(join(root, derived), { recursive: true });
    writeShape(root, captured, ["metrics"], false);
    writeShape(root, derived, ["metrics"], true);

    // The stand-in reports parity for every cell, so only the marker separates the two columns.
    const result = runMatrix(root, writeSynthProgram(root));
    const output = `${result.stdout}${result.stderr}`;

    match(
      output,
      /^metrics \| matched \| matched on reconstructed inputs, not evidence$/m,
    );
    strictEqual(result.status, 2, output);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("a formerly matching cell makes the matrix gate fail", () => {
  const root = mkdtempSync(join(tmpdir(), "shape-regression-"));
  try {
    const first = "idea-test1-new-network";
    const second = "sample-cluster-existing-network";
    mkdirSync(join(root, first), { recursive: true });
    mkdirSync(join(root, second), { recursive: true });
    writeShape(root, first, ["metrics"], false);
    writeShape(root, second, ["metrics"], false);
    const synthProgram = writeSynthProgram(root);

    const matching = runMatrix(root, synthProgram);
    strictEqual(matching.status, 0, `${matching.stdout}${matching.stderr}`);
    match(matching.stdout, /^metrics \| matched \| matched$/m);

    const regressed = runMatrix(root, synthProgram, { BREAK_MATCH: "1" });
    strictEqual(regressed.status, 1, `${regressed.stdout}${regressed.stderr}`);
    match(regressed.stdout, /^metrics \| differed: MISMATCH .* \| matched$/m);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

/**
 * Returns alternate fixtures, with their marker, when both local reference assemblies exist.
 */
function localShapeFixtures(): Array<{ name: string; reconstructed: boolean }> {
  if (!existsSync(REAL_FIXTURES)) return [];
  return readdirSync(REAL_FIXTURES, { withFileTypes: true })
    .filter((entry) => {
      if (!entry.isDirectory()) return false;
      const fixture = join(REAL_FIXTURES, entry.name);
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
      reconstructed: existsSync(join(REAL_FIXTURES, entry.name, "DERIVED.md")),
    }))
    .sort((left, right) => left.name.localeCompare(right.name));
}

const localShapes = localShapeFixtures();
const CAPTURED_CELL = /^(matched|differed:|could not run: .*missing)/;
const RECONSTRUCTED_CELL =
  /^(matched on reconstructed inputs, not evidence|differed:|could not run: .*missing)/;

test(
  "the current matching stack remains equal in the captured alternate shape",
  {
    skip: localShapes.length === 2 ? false : "local alternate-shape references are absent",
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

    let capturedClusterCells = 0;
    for (const row of rows) {
      const cells = row.split(" | ").slice(1);
      strictEqual(cells.length, localShapes.length, row);
      for (const [index, cell] of cells.entries()) {
        const shape = localShapes[index];
        if (shape === undefined) throw new Error(`no shape for column ${index}`);
        // A reconstructed column may never read as a plain match: its inputs came from the
        // reference templates this row compares against.
        match(cell, shape.reconstructed ? RECONSTRUCTED_CELL : CAPTURED_CELL, row);
        if (row.startsWith("cluster | ") && !shape.reconstructed) {
          strictEqual(cell, "matched", row);
          capturedClusterCells += 1;
        }
      }
    }
    strictEqual(capturedClusterCells >= 1, true, output);
  },
);
