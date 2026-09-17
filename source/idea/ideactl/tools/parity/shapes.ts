/**
 * Runs the parity comparison for every stack in the two alternate cluster fixtures.
 *
 * Fixture directories are discovered from reference cloud assemblies that have no deployed
 * template set. This keeps private cluster identifiers out of the source file while still
 * printing a concrete stack-by-shape table for the local gate.
 *
 * A fixture that carries a `DERIVED.md` marker declares that its settings and synth-time reads
 * were derived from the reference templates this gate compares against, so an equal template
 * there is partly self-confirming. Those cells are reported under their own wording and never
 * make the gate exit 0, because only a captured fixture can carry evidence about a real cluster.
 */

import { spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";

const PKG = resolve(import.meta.dirname, "../..");
const DEFAULT_FIXTURES = join(PKG, "tools/parity/fixtures");
const DEFAULT_SYNTH = join(PKG, "tools/parity/synth.ts");
const LIVE_TEMPLATES = join(PKG, "tools/parity/live");

type CellStatus = "matched" | "matched-reconstructed" | "differed" | "could-not-run";

/** What a zero exit on a fixture whose inputs were derived, not captured, is worth. */
const RECONSTRUCTED_MATCH = "matched on reconstructed inputs, not evidence";

type Shape = {
  name: string;
  fixtureDir: string;
  stacks: Set<string>;
  reconstructed: boolean;
};

type Cell = {
  shape: string;
  stack: string;
  status: CellStatus;
  summary: string;
  output: string;
};

/**
 * Returns the value following a named command-line flag.
 */
function flag(argv: string[], name: string): string | undefined {
  const index = argv.indexOf(name);
  return index >= 0 ? argv[index + 1] : undefined;
}

/**
 * Finds stack names from the reference cloud assembly stored with one fixture.
 */
function discoverStacks(fixtureDir: string, shapeName: string): Set<string> {
  const cdkDir = join(fixtureDir, "python/_cdk");
  if (!existsSync(cdkDir)) return new Set<string>();

  const stacks = new Set<string>();
  for (const entry of readdirSync(cdkDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.startsWith("cdk.out.")) continue;
    const stack = entry.name.slice("cdk.out.".length);
    const reference = join(cdkDir, entry.name, `${shapeName}-${stack}.template.json`);
    if (existsSync(reference)) stacks.add(stack);
  }
  return stacks;
}

/**
 * Discovers the two alternate-shape fixture directories.
 */
function discoverShapes(fixturesRoot: string): Shape[] {
  if (!existsSync(fixturesRoot)) {
    throw new Error(`missing fixtures directory: ${fixturesRoot}`);
  }

  const shapes: Shape[] = [];
  for (const entry of readdirSync(fixturesRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const fixtureDir = join(fixturesRoot, entry.name);
    const deployedClusterTemplate = join(LIVE_TEMPLATES, `${entry.name}-cluster.json`);
    if (existsSync(deployedClusterTemplate)) continue;
    const reconstructed = existsSync(join(fixtureDir, "DERIVED.md"));
    const stacks = discoverStacks(fixtureDir, entry.name);
    if (stacks.size > 0) {
      shapes.push({ name: entry.name, fixtureDir, stacks, reconstructed });
    }
  }

  shapes.sort((left, right) => left.name.localeCompare(right.name));
  if (shapes.length !== 2) {
    throw new Error(
      `expected 2 alternate-shape fixture directories with reference templates under ${fixturesRoot}, found ${shapes.length}`,
    );
  }
  return shapes;
}

/**
 * Extracts a stable mismatch summary for the matrix cell.
 */
function mismatchSummary(output: string): string {
  const line = output.split(/\r?\n/).find((value) => value.startsWith("MISMATCH"));
  return line ?? "comparison reported differences";
}

/**
 * Converts a synthesis failure into the missing input or concrete failure reason.
 */
function failureReason(output: string, reconstructed: boolean): string {
  const configKey = /ConfigKeyNotFound(?: \[Error\])?:[^\n]*key: ([^\s,]+)/.exec(output)?.[1];
  if (configKey !== undefined) {
    const source = reconstructed ? "reconstructed fixture" : "fixture";
    return `${source} is missing required setting ${configKey}`;
  }

  const synthRead = /SynthReadMiss: ([^\n]+)/.exec(output)?.[1]?.trim().replace(/^SynthReadMiss:\s*/, "");
  if (synthRead !== undefined) return `fixture is missing synth-time read ${synthRead}`;

  const knownMissing = output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(
      (line) =>
        line.startsWith("missing replay fixtures:") ||
        line.startsWith("no cdk.context.json") ||
        line.startsWith("no reference template:"),
    );
  if (knownMissing !== undefined) return knownMissing;

  const errorLine = output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line.startsWith("Error:"));
  return errorLine ?? "synthesis failed without a diagnostic";
}

/**
 * Runs one stack against one shape through the existing synth and comparison path.
 */
function runCell(shape: Shape, stack: string, fixturesRoot: string, synthProgram: string): Cell {
  const reference = join(shape.fixtureDir, "python/_cdk", `cdk.out.${stack}`, `${shape.name}-${stack}.template.json`);
  if (!existsSync(reference)) {
    return {
      shape: shape.name,
      stack,
      status: "could-not-run",
      summary: `missing reference template ${relative(PKG, reference)}`,
      output: "",
    };
  }

  const result = spawnSync(
    process.execPath,
    [
      synthProgram,
      "--cluster",
      shape.name,
      "--stack",
      stack,
      "--against",
      "synth",
      "--fixtures",
      fixturesRoot,
    ],
    { encoding: "utf8" },
  );
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`.trim();

  if (result.status === 0) {
    return shape.reconstructed
      ? { shape: shape.name, stack, status: "matched-reconstructed", summary: RECONSTRUCTED_MATCH, output }
      : { shape: shape.name, stack, status: "matched", summary: "matched", output };
  }
  if (result.status === 1) {
    return {
      shape: shape.name,
      stack,
      status: "differed",
      summary: `differed: ${mismatchSummary(output)}`,
      output,
    };
  }
  return {
    shape: shape.name,
    stack,
    status: "could-not-run",
    summary: `could not run: ${failureReason(output, shape.reconstructed)}`,
    output,
  };
}

/**
 * Escapes one value for the Markdown-compatible table printed by the gate.
 */
function tableValue(value: string): string {
  return value.replaceAll("|", "\\|").replaceAll(/\r?\n/g, " ");
}

/**
 * Prints the matrix and the full comparison output for cells that differ.
 */
function printResults(shapes: Shape[], stacks: string[], cells: Cell[]): void {
  console.log(["STACK", ...shapes.map((shape) => shape.name)].map(tableValue).join(" | "));
  console.log(["---", ...shapes.map(() => "---")].join(" | "));
  for (const stack of stacks) {
    const row = [
      stack,
      ...shapes.map((shape) => {
        const cell = cells.find((value) => value.stack === stack && value.shape === shape.name);
        return cell?.summary ?? "could not run: matrix result missing";
      }),
    ];
    console.log(row.map(tableValue).join(" | "));
  }

  const differences = cells.filter((cell) => cell.status === "differed");
  if (differences.length === 0) return;
  console.log("\nDIFFERENCES");
  for (const cell of differences) {
    console.log(`\n[${cell.shape} / ${cell.stack}]`);
    console.log(cell.output);
  }
}

/**
 * Executes the full matrix and returns the parity-compatible exit status.
 */
function main(argv: string[]): number {
  parseArgs({
    args: argv,
    options: {
      fixtures: { type: "string" },
      synth: { type: "string" },
    },
    strict: true,
    allowPositionals: false,
  });

  const fixturesRoot = resolve(flag(argv, "--fixtures") ?? DEFAULT_FIXTURES);
  const synthProgram = resolve(flag(argv, "--synth") ?? DEFAULT_SYNTH);
  if (!existsSync(synthProgram)) {
    console.error(`missing synthesis program: ${synthProgram}`);
    return 2;
  }

  const shapes = discoverShapes(fixturesRoot);
  const stacks = [...new Set(shapes.flatMap((shape) => [...shape.stacks]))].sort();
  const cells = stacks.flatMap((stack) =>
    shapes.map((shape) => runCell(shape, stack, fixturesRoot, synthProgram)),
  );
  printResults(shapes, stacks, cells);

  if (cells.some((cell) => cell.status === "differed")) return 1;
  // A reconstructed match counts with the cells that could not run: neither is proof.
  if (cells.some((cell) => cell.status !== "matched")) return 2;
  return 0;
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    process.exit(main(process.argv.slice(2)));
  } catch (error) {
    console.error(`${error}`);
    process.exit(2);
  }
}
