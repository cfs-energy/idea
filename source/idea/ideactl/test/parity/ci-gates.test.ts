/**
 * Exercises each automation gate through the same command-line entry point used
 * by the workflow, including one controlled failing input per gate.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
  readFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = resolve(TEST_DIRECTORY, "../..");
const GATE_PROGRAM = join(PACKAGE_ROOT, "scripts/ci-gates.mjs");
const PARITY_PROGRAM = join(PACKAGE_ROOT, "tools/parity/parity.ts");
const TEMPORARY_DIRECTORIES: string[] = [];

after(() => {
  for (const directory of TEMPORARY_DIRECTORIES) {
    rmSync(directory, { force: true, recursive: true });
  }
});

/**
 * Creates one isolated package-like directory.
 */
function temporaryRoot(label: string): string {
  const root = mkdtempSync(join(tmpdir(), `ideactl-ci-${label}-`));
  TEMPORARY_DIRECTORIES.push(root);
  return root;
}

/**
 * Writes JSON with stable formatting and creates its parent directory.
 */
function writeJson(file: string, value: unknown): void {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, `${JSON.stringify(value, undefined, 2)}\n`);
}

/**
 * Runs one gate through the automation entry point.
 */
function runGate(command: string, ...args: string[]) {
  return spawnSync(process.execPath, [GATE_PROGRAM, command, ...args], {
    encoding: "utf8",
  });
}

/**
 * Verifies a controlled failure and prints its decisive diagnostic.
 */
function assertDeliberateFailure(
  gate: string,
  result: ReturnType<typeof runGate>,
  expected: RegExp,
): void {
  const output = `${result.stdout}${result.stderr}`;
  assert.equal(result.status, 1, output);
  assert.match(output, expected);
  const diagnostic =
    output
      .split(/\r?\n/u)
      .find((line) => line.startsWith("FAIL ") || line.includes("MISMATCH")) ??
    output.trim().split(/\r?\n/u)[0] ??
    "failed";
  console.log(`DELIBERATE FAILURE ${gate}: ${diagnostic}`);
}

/**
 * Writes a one-package manifest and lockfile.
 */
function writeDependencyFixture(root: string, declaration: string): void {
  writeJson(join(root, "package.json"), {
    dependencies: { "sample-package": declaration },
    devDependencies: {},
  });
  writeJson(join(root, "package-lock.json"), {
    packages: {
      "": {
        dependencies: { "sample-package": declaration },
        devDependencies: {},
      },
      "node_modules/sample-package": { version: "1.2.3" },
    },
  });
}

/**
 * Writes one minimal template accepted by the parity comparator.
 */
function template(displayName: string): Record<string, unknown> {
  return {
    Resources: {
      marker: {
        Metadata: { "aws:cdk:path": "sample-cluster-metrics/marker" },
        Properties: { DisplayName: displayName },
        Type: "AWS::SNS::Topic",
      },
    },
  };
}

/**
 * Writes a test that requires the parity comparator to exit successfully.
 */
function writeParityTest(
  root: string,
  expected: string,
  actual: string,
): void {
  const file = join(root, "test/synthetic-parity.test.ts");
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(
    file,
    [
      'import assert from "node:assert/strict";',
      'import { spawnSync } from "node:child_process";',
      'import { test } from "node:test";',
      'test("templates match", () => {',
      `  const result = spawnSync(process.execPath, [${JSON.stringify(PARITY_PROGRAM)}, "diff", ${JSON.stringify(expected)}, ${JSON.stringify(actual)}], { encoding: "utf8" });`,
      "  assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);",
      "});",
      "",
    ].join("\n"),
  );
}

test("dependency gate rejects a version range", () => {
  const root = temporaryRoot("dependencies");
  writeDependencyFixture(root, "^1.2.3");
  assertDeliberateFailure(
    "dependencies",
    runGate("dependencies", "--root", root),
    /must be an exact version/u,
  );
});

// The workflow refuses a manifest whose version differs from IDEA_VERSION.txt; a release bump on
// 2026-09-15 reached the pull request before that was caught locally.
test("dependency gate rejects a package version that differs from the release file", () => {
  // The release file sits three levels above the package, as in the repository.
  const repository = temporaryRoot("dependencies-release");
  const root = join(repository, "source", "idea", "ideactl");
  mkdirSync(root, { recursive: true });
  writeDependencyFixture(root, "1.2.3");
  const manifestFile = join(root, "package.json");
  const manifest = JSON.parse(readFileSync(manifestFile, "utf8")) as Record<string, unknown>;
  writeJson(manifestFile, { ...manifest, version: "26.09.0" });
  writeFileSync(join(repository, "IDEA_VERSION.txt"), "26.10.0\n");
  assertDeliberateFailure(
    "dependencies",
    runGate("dependencies", "--root", root),
    /package\.json version 26\.09\.0 must equal IDEA_VERSION\.txt 26\.10\.0/u,
  );
});

test("hygiene gate rejects a non-synthetic account identifier", () => {
  const root = temporaryRoot("hygiene");
  const file = join(root, "src/example.ts");
  mkdirSync(dirname(file), { recursive: true });
  const prohibitedIdentifier = ["123456", "789013"].join("");
  writeFileSync(file, `export const account = "${prohibitedIdentifier}";\n`);
  assertDeliberateFailure(
    "hygiene",
    runGate("hygiene", "--root", root),
    /prohibited account identifier/u,
  );
});

test("skip gate rejects a test file without an allowance", () => {
  const root = temporaryRoot("skips");
  const file = join(root, "test/skipped.test.ts");
  mkdirSync(dirname(file), { recursive: true });
  const skippedOptions = ["{ sk", "ip: true }"].join("");
  writeFileSync(
    file,
    [
      'import { test } from "node:test";',
      `test("not run", ${skippedOptions}, () => {});`,
      "",
    ].join("\n"),
  );
  assertDeliberateFailure(
    "skips",
    runGate("skips", "--root", root),
    /declares a skip without an allowance/u,
  );
});

test("skip gate accepts a test file with a reasoned allowance", () => {
  const root = temporaryRoot("allowed-skip");
  const file = join(root, "test/skipped.test.ts");
  mkdirSync(dirname(file), { recursive: true });
  const skippedOptions = ["{ sk", "ip: true }"].join("");
  writeFileSync(
    file,
    [
      'import { test } from "node:test";',
      `test("not run", ${skippedOptions}, () => {});`,
      "",
    ].join("\n"),
  );
  writeJson(join(root, "scripts/ci-skip-allowances.json"), {
    "test/skipped.test.ts": "An external fixture is optional.",
  });
  const result = runGate("skips", "--root", root);
  assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
  assert.match(result.stdout, /PASS test skip allowances/u);
});

test("workflow gate rejects malformed YAML", () => {
  const root = temporaryRoot("workflows");
  const workflowRoot = join(root, "workflows");
  mkdirSync(workflowRoot, { recursive: true });
  writeFileSync(join(workflowRoot, "broken.yaml"), "name: Broken\non: [\n");
  assertDeliberateFailure(
    "workflows",
    runGate("workflows", "--root", root, "--workflows", workflowRoot),
    // js-yaml 4 and 5 word the truncated-flow-sequence error differently.
    /unexpected end of the stream|unexpected end of stream|deficient indentation/u,
  );
});

test("type-check gate rejects a strict type error", () => {
  const root = temporaryRoot("typecheck");
  writeJson(join(root, "tsconfig.json"), {
    compilerOptions: {
      noEmit: true,
      strict: true,
      target: "es2022",
    },
    include: ["broken.ts"],
  });
  writeFileSync(join(root, "broken.ts"), 'const value: string = 1;\n');
  assertDeliberateFailure(
    "typecheck",
    runGate("typecheck", "--root", root),
    /TS2322/u,
  );
});

test("synthetic parity gate rejects a template difference", () => {
  const root = temporaryRoot("parity");
  const expected = join(root, "expected.json");
  const actual = join(root, "actual.json");
  writeJson(expected, template("expected"));
  writeJson(actual, template("different"));
  writeParityTest(root, expected, actual);
  assertDeliberateFailure(
    "parity",
    runGate(
      "parity",
      "--root",
      root,
      "--parity-test",
      "test/synthetic-parity.test.ts",
    ),
    /MISMATCH|synthetic template parity failed/u,
  );
});

test("full-suite gate propagates a test failure", () => {
  const root = temporaryRoot("tests");
  const file = join(root, "test/failing.test.ts");
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(
    file,
    [
      'import assert from "node:assert/strict";',
      'import { test } from "node:test";',
      'test("controlled failure", () => assert.fail("controlled failure"));',
      "",
    ].join("\n"),
  );
  assertDeliberateFailure(
    "tests",
    runGate("tests", "--root", root),
    /controlled failure|full test suite failed/u,
  );
});

test("human-only gate list names every credential-backed proof", () => {
  const result = runGate("human");
  assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
  assert.match(result.stdout, /HUMAN-ONLY CREDENTIAL GATES/u);
  assert.match(result.stdout, /real cluster settings/u);
  assert.match(result.stdout, /empty live infrastructure diff/u);
  assert.match(result.stdout, /fresh development install/u);
  assert.match(result.stdout, /restricted-partition fixture/u);
});
