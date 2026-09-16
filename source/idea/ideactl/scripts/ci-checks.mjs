#!/usr/bin/env node
/**
 * Runs the package checks CI can run without cloud credentials.
 */

import { spawnSync } from "node:child_process";
import {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} from "node:fs";
import { createRequire } from "node:module";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const DEFAULT_PACKAGE_ROOT = resolve(SCRIPT_DIRECTORY, "..");
const DEFAULT_WORKFLOW_ROOT = resolve(DEFAULT_PACKAGE_ROOT, "../../..", ".github/workflows");
const require = createRequire(import.meta.url);
const EXACT_VERSION =
  /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/u;
const TEST_FILE = /\.test\.ts$/u;
const SKIP_DECLARATION =
  /\bskip\s*:|(?:\btest|\bit|\bdescribe|\bsuite)\.skip\s*\(/u;
const TEXT_EXTENSIONS = new Set([
  "",
  ".cjs",
  ".css",
  ".html",
  ".ini",
  ".js",
  ".jinja2",
  ".json",
  ".md",
  ".mjs",
  ".py",
  ".sh",
  ".sql",
  ".ts",
  ".txt",
  ".yaml",
  ".yml",
]);
const EXCLUDED_DIRECTORIES = new Set([
  ".git",
  "cdk.out",
  "dist",
  "node_modules",
]);
const EXCLUDED_RELATIVE_DIRECTORIES = [
  "docs/port",
  "tools/e2e/reference",
  "tools/parity/fixtures",
  "tools/parity/live",
];
const ALLOWED_TICKET_LIKE_PREFIXES = new Set([
  "BSD",
  "FS",
  "GPL",
  "SHA",
  "UTF",
]);
/**
 * Escapes a literal for use inside a regular expression.
 *
 * @param {string} value literal text
 * @returns {string}
 */
function escapeRegExp(value) {
  return value.replaceAll(/[.*+?^${}()|[\]\\]/gu, "\\$&");
}

const PROHIBITED_NAMES = [
  "Q2xhdWRl",
  "Q3Vyc29y",
  "R1BU",
  "aWRlYS1jb20=",
  "aWRlYS1jb2xsYWI=",
  "cG9ueXRhaWw=",
].map((value) => Buffer.from(value, "base64").toString("utf8").toLowerCase());
/**
 * Returns true for a JSON object with string keys.
 *
 * @param {unknown} value parsed value
 * @returns {value is Record<string, unknown>}
 */
function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Reads and validates a JSON object.
 *
 * @param {string} file file to read
 * @returns {Record<string, unknown>}
 */
function readJsonRecord(file) {
  const value = JSON.parse(readFileSync(file, "utf8"));
  if (!isRecord(value)) {
    throw new Error(`${file} must contain a JSON object`);
  }
  return value;
}

/**
 * Reads an object member and validates that every value is a string.
 *
 * @param {Record<string, unknown>} record containing object
 * @param {string} key member name
 * @returns {Record<string, string>}
 */
function readStringMap(record, key) {
  const value = record[key];
  if (!isRecord(value)) {
    throw new Error(`${key} must be an object`);
  }
  const result = {};
  for (const [name, version] of Object.entries(value)) {
    if (typeof version !== "string") {
      throw new Error(`${key}.${name} must be a string`);
    }
    result[name] = version;
  }
  return result;
}

/**
 * Audits direct dependencies against exact lockfile resolutions.
 *
 * @param {string} packageRoot package directory
 */
export function checkDependencyPins(packageRoot) {
  const manifest = readJsonRecord(join(packageRoot, "package.json"));
  const lockfile = readJsonRecord(join(packageRoot, "package-lock.json"));
  const packages = lockfile.packages;
  if (!isRecord(packages)) {
    throw new Error("package-lock.json packages must be an object");
  }
  const lockRoot = packages[""];
  if (!isRecord(lockRoot)) {
    throw new Error("package-lock.json must describe the root package");
  }

  let checked = 0;
  for (const section of ["dependencies", "devDependencies"]) {
    const declared = readStringMap(manifest, section);
    const locked = readStringMap(lockRoot, section);
    for (const [name, version] of Object.entries(declared)) {
      if (!EXACT_VERSION.test(version)) {
        throw new Error(`${section}.${name} must be an exact version, found ${version}`);
      }
      if (locked[name] !== version) {
        throw new Error(`${section}.${name} differs between package.json and package-lock.json`);
      }
      const installed = packages[`node_modules/${name}`];
      if (!isRecord(installed) || installed.version !== version) {
        throw new Error(`${section}.${name} does not match its resolved lockfile version`);
      }
      checked += 1;
    }
  }
  if (checked === 0) {
    throw new Error("package.json must declare at least one dependency");
  }
  // The workflow refuses a package whose version differs from the release file; catch it here
  // first, since a release bump is easy to make everywhere but the manifest.
  const releaseFile = join(packageRoot, "..", "..", "..", "IDEA_VERSION.txt");
  if (existsSync(releaseFile)) {
    const release = readFileSync(releaseFile, "utf8").trim();
    if (manifest.version !== release) {
      throw new Error(`package.json version ${String(manifest.version)} must equal IDEA_VERSION.txt ${release}`);
    }
    if (lockRoot.version !== release) {
      throw new Error(`package-lock.json version ${String(lockRoot.version)} must equal IDEA_VERSION.txt ${release}`);
    }
  }
  console.log(`PASS dependency pins (${checked} direct packages, release ${String(manifest.version)})`);
}

/**
 * Returns true when a relative path is intentionally private or generated.
 *
 * @param {string} relativePath slash-separated path
 * @returns {boolean}
 */
function isExcludedPath(relativePath) {
  const parts = relativePath.split("/");
  if (parts.some((part) => EXCLUDED_DIRECTORIES.has(part))) {
    return true;
  }
  return EXCLUDED_RELATIVE_DIRECTORIES.some(
    (directory) =>
      relativePath === directory || relativePath.startsWith(`${directory}/`),
  );
}

/**
 * Walks text files without following symbolic links.
 *
 * @param {string} root directory to walk
 * @returns {string[]}
 */
function listTextFiles(root) {
  if (!existsSync(root)) {
    return [];
  }
  const files = [];
  const pending = [root];
  while (pending.length > 0) {
    const directory = pending.pop();
    if (directory === undefined) {
      continue;
    }
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const absolute = join(directory, entry.name);
      const relativePath = relative(root, absolute).split("\\").join("/");
      if (isExcludedPath(relativePath) || entry.isSymbolicLink()) {
        continue;
      }
      if (entry.isDirectory()) {
        pending.push(absolute);
        continue;
      }
      if (
        entry.isFile() &&
        statSync(absolute).size <= 1_000_000 &&
        (entry.name === "Dockerfile" || TEXT_EXTENSIONS.has(extname(entry.name)))
      ) {
        files.push(absolute);
      }
    }
  }
  return files.sort();
}

/**
 * Reports the one-based line containing an offset.
 *
 * @param {string} text complete file text
 * @param {number} offset character offset
 * @returns {number}
 */
function lineAt(text, offset) {
  return text.slice(0, offset).split("\n").length;
}

/**
 * Adds one hygiene error with a stable path and line.
 *
 * @param {string[]} errors destination
 * @param {string} root scan root
 * @param {string} file matching file
 * @param {string} text complete file text
 * @param {number} offset character offset
 * @param {string} reason failure reason
 */
function addHygieneError(errors, root, file, text, offset, reason) {
  const path = relative(root, file).split("\\").join("/");
  errors.push(`${path}:${lineAt(text, offset)} ${reason}`);
}

/**
 * Checks one file for public repository hygiene violations.
 *
 * @param {string[]} errors destination
 * @param {string} root scan root
 * @param {string} file file to inspect
 */
function checkHygieneFile(errors, root, file) {
  const bytes = readFileSync(file);
  if (bytes.includes(0)) {
    return;
  }
  const text = bytes.toString("utf8");
  const relativePath = relative(root, file).split("\\").join("/");
  const inTest = relativePath.startsWith("test/");
  // The resources tree is carried from upstream: its files keep their publisher's licence headers
  // and licence links, its templates name the products they configure, its samples use the
  // documentation domain, and the load balancer account ids in it are the published service
  // accounts. Secrets, real account ids and real addresses are still refused there.
  const upstream = relativePath.startsWith("resources/");
  // The one upstream file whose twelve-digit numbers are real: the published per-region load
  // balancer service accounts that an access-log bucket policy has to name.
  const publishedServiceAccounts = relativePath === "resources/config/region_elb_account_id.yml";
  const uuidRanges = [
    ...text.matchAll(
      /\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b/gu,
    ),
  ].map((match) => [
    match.index,
    match.index + match[0].length,
  ]);

  for (const match of text.matchAll(
    /(?<![0-9A-Fa-f])\d{12}(?![0-9A-Fa-f])/gu,
  )) {
    if (
      uuidRanges.some(
        ([start, end]) => match.index >= start && match.index < end,
      )
    ) {
      continue;
    }
    const value = match[0];
    const synthetic =
      value === ["123456", "789012"].join("") || /^(\d)\1{11}$/u.test(value);
    if (!publishedServiceAccounts && (!(inTest || upstream) || !synthetic)) {
      addHygieneError(
        errors,
        root,
        file,
        text,
        match.index,
        "contains a prohibited account identifier",
      );
    }
  }

  for (const prohibited of upstream ? [] : PROHIBITED_NAMES) {
    // A trailing letter or digit means a longer word, not the forbidden name: a compute-node
    // resource name starts with the same letters as one of the cluster names. A trailing
    // hyphen is still the name, because every resource derived from it carries one.
    const match = new RegExp(`${escapeRegExp(prohibited)}(?![a-z0-9])`, "i").exec(text);
    if (match !== null) {
      addHygieneError(
        errors,
        root,
        file,
        text,
        match.index,
        "contains a prohibited private or product name",
      );
    }
  }

  const emDash = text.indexOf(String.fromCodePoint(0x2014));
  if (emDash >= 0) {
    addHygieneError(
      errors,
      root,
      file,
      text,
      emDash,
      "contains an em dash",
    );
  }

  for (const match of text.matchAll(/\b([A-Z]{2,10})-\d+\b/gu)) {
    const prefix = match[1];
    if (!upstream && prefix !== undefined && !ALLOWED_TICKET_LIKE_PREFIXES.has(prefix)) {
      addHygieneError(
        errors,
        root,
        file,
        text,
        match.index,
        "contains a ticket-like identifier",
      );
    }
  }

  const secretPatterns = [
    /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/gu,
    /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/gu,
    /\bgh[pousr]_[A-Za-z0-9_]{20,}\b/gu,
  ];
  for (const pattern of secretPatterns) {
    const match = pattern.exec(text);
    if (match !== null) {
      addHygieneError(
        errors,
        root,
        file,
        text,
        match.index,
        "contains secret material",
      );
    }
  }

  // A batch-system principal is `user@host`, which is not an email address and has no example
  // domain to use. The batch server matches a connection against the name it reverse-resolves the
  // caller's address to, so the host part must be a provider-shaped private DNS name or the grant
  // silently matches nothing. Addresses in the documentation range are the correct example to write
  // there, so accept a private DNS name built from one and keep everything else failing.
  const documentationHost = /^ip-(192-0-2|198-51-100|203-0-113)-\d{1,3}\.[a-z0-9-]+\.compute\.internal$/u;
  for (const match of text.matchAll(
    /[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})/gu,
  )) {
    const host = match[1] ?? "";
    if (documentationHost.test(host)) continue;
    if (upstream && host.toLowerCase() === "example.com") continue;
    if (host.toLowerCase() !== "example.invalid") {
      addHygieneError(
        errors,
        root,
        file,
        text,
        match.index,
        "contains a non-example email address",
      );
    }
  }

  const copyrightPattern = new RegExp(
    [
      "copy",
      "right.{0,40}amazon|amazon.{0,40}copy",
      "right",
    ].join(""),
    "iu",
  );
  const copyright = upstream ? null : copyrightPattern.exec(text);
  if (copyright !== null) {
    addHygieneError(
      errors,
      root,
      file,
      text,
      copyright.index,
      "contains a prohibited copyright header",
    );
  }
}

/**
 * Audits public package files and workflow definitions.
 *
 * @param {string} packageRoot package directory
 * @param {string | undefined} workflowRoot workflow directory
 */
export function checkHygiene(packageRoot, workflowRoot) {
  const roots = [packageRoot];
  if (workflowRoot !== undefined && existsSync(workflowRoot)) {
    roots.push(workflowRoot);
  }
  const errors = [];
  let checked = 0;
  for (const root of roots) {
    for (const file of listTextFiles(root)) {
      checkHygieneFile(errors, root, file);
      checked += 1;
    }
  }
  if (errors.length > 0) {
    throw new Error(`repository hygiene failed:\n${errors.join("\n")}`);
  }
  console.log(`PASS repository hygiene (${checked} public files)`);
}

/**
 * Loads the explicit file-level skip allowances.
 *
 * @param {string} packageRoot package directory
 * @returns {Record<string, string>}
 */
function readSkipAllowances(packageRoot) {
  const file = join(packageRoot, "scripts/ci-skip-allowances.json");
  if (!existsSync(file)) {
    return {};
  }
  const value = readJsonRecord(file);
  const result = {};
  for (const [path, reason] of Object.entries(value)) {
    if (typeof reason !== "string" || reason.trim() === "") {
      throw new Error(`skip allowance ${path} must have a non-empty reason`);
    }
    result[path] = reason;
  }
  return result;
}

/**
 * Rejects test files that can skip without a file-level allowance.
 *
 * @param {string} packageRoot package directory
 */
export function checkSkipAllowances(packageRoot) {
  const testRoot = join(packageRoot, "test");
  const allowances = readSkipAllowances(packageRoot);
  const declaredSkips = [];
  for (const file of listTextFiles(testRoot)) {
    if (!TEST_FILE.test(file)) {
      continue;
    }
    const source = readFileSync(file, "utf8");
    if (SKIP_DECLARATION.test(source)) {
      declaredSkips.push(relative(packageRoot, file).split("\\").join("/"));
    }
  }
  const missing = declaredSkips.filter((file) => allowances[file] === undefined);
  const stale = Object.keys(allowances).filter((file) => !declaredSkips.includes(file));
  if (missing.length > 0 || stale.length > 0) {
    const failures = [
      ...missing.map((file) => `${file} declares a skip without an allowance`),
      ...stale.map((file) => `${file} has a stale skip allowance`),
    ];
    throw new Error(`test skip policy failed:\n${failures.join("\n")}`);
  }
  console.log(`PASS test skip allowances (${declaredSkips.length} allowed files)`);
}

/**
 * Parses every workflow as a YAML object.
 *
 * @param {string} workflowRoot workflow directory
 */
export function checkWorkflows(workflowRoot) {
  const yamlModule = require("js-yaml");
  if (
    !isRecord(yamlModule) ||
    typeof yamlModule.load !== "function"
  ) {
    throw new Error("YAML parser is unavailable");
  }
  const workflows = readdirSync(workflowRoot, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isFile() &&
        (entry.name.endsWith(".yaml") || entry.name.endsWith(".yml")),
    )
    .map((entry) => join(workflowRoot, entry.name))
    .sort();
  if (workflows.length === 0) {
    throw new Error(`no workflow files found under ${workflowRoot}`);
  }
  for (const workflow of workflows) {
    const parsed = yamlModule.load(readFileSync(workflow, "utf8"));
    if (!isRecord(parsed)) {
      throw new Error(`${workflow} must contain a YAML object`);
    }
  }
  console.log(`PASS workflow parsing (${workflows.length} workflows)`);
}

/**
 * Runs a subprocess and forwards all output.
 *
 * @param {string} label check label
 * @param {string} command executable
 * @param {string[]} args arguments
 * @param {string} cwd working directory
 */
function runChecked(label, command, args, cwd) {
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    env,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.stdout !== "") {
    process.stdout.write(result.stdout);
  }
  if (result.stderr !== "") {
    process.stderr.write(result.stderr);
  }
  if (result.error !== undefined) {
    throw new Error(`${label} could not start: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${label} failed with exit ${result.status ?? "signal"}`);
  }
  console.log(`PASS ${label}`);
}

/**
 * Runs the package type check.
 *
 * @param {string} packageRoot package directory
 */
export function runTypeCheck(packageRoot) {
  const compiler = join(DEFAULT_PACKAGE_ROOT, "node_modules/typescript/bin/tsc");
  runChecked(
    "type check",
    process.execPath,
    [compiler, "--noEmit", "-p", join(packageRoot, "tsconfig.json")],
    packageRoot,
  );
}

/**
 * Runs the complete package test suite with native type stripping.
 *
 * @param {string} packageRoot package directory
 */
export function runTests(packageRoot) {
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  const result = spawnSync(process.execPath, ["--test", "test/**/*.test.ts"], {
    cwd: packageRoot,
    encoding: "utf8",
    env,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.stdout !== "") process.stdout.write(result.stdout);
  if (result.stderr !== "") process.stderr.write(result.stderr);
  if (result.error !== undefined) {
    throw new Error(`full test suite could not start: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`full test suite failed with exit ${result.status ?? "signal"}`);
  }
  // A file that needs a private capture a public checkout cannot have announces itself and
  // registers no test; the runner lists it as passed, so the honest count is printed here.
  const notRun = (result.stdout.match(/^PRIVATE CAPTURE ABSENT, tests not run:/gm) ?? []).length;
  console.log(notRun === 0 ? "PASS full test suite" : `PASS full test suite (${notRun} file(s) not run: private captures absent in this checkout)`);
}

/**
 * Compares the committed synthetic template with the current synthesis.
 *
 * @param {string} packageRoot package directory
 * @param {string | undefined} testFile optional test override
 */
export function runSyntheticParity(packageRoot, testFile) {
  runChecked(
    "synthetic template parity",
    process.execPath,
    ["--test", testFile ?? "test/parity/synthetic-parity.test.ts"],
    packageRoot,
  );
}

/**
 * Parses the small command-line interface.
 *
 * @param {string[]} argv process arguments
 * @returns {{
 *   commands: string[];
 *   packageRoot: string;
 *   workflowRoot: string | undefined;
 *   parityTest: string | undefined;
 * }}
 */
function parseArguments(argv) {
  const commands = [];
  let packageRoot = DEFAULT_PACKAGE_ROOT;
  let workflowRoot;
  let parityTest;
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag !== undefined && !flag.startsWith("--")) {
      commands.push(flag);
      continue;
    }
    const value = argv[index + 1];
    if (
      !["--root", "--workflows", "--parity-test"].includes(flag ?? "") ||
      value === undefined ||
      value === ""
    ) {
      throw new Error(`invalid argument: ${flag ?? ""}`);
    }
    if (flag === "--root") {
      packageRoot = resolve(value);
    } else if (flag === "--workflows") {
      workflowRoot = resolve(value);
    } else {
      parityTest = value;
    }
    index += 1;
  }
  return {
    commands: commands.length === 0 ? ["all"] : commands,
    packageRoot,
    workflowRoot:
      workflowRoot ??
      (packageRoot === DEFAULT_PACKAGE_ROOT ? DEFAULT_WORKFLOW_ROOT : undefined),
    parityTest,
  };
}

/**
 * Runs one check or all of them.
 *
 * @param {string[]} argv process arguments
 */
export async function runCli(argv) {
  const flagIndex = argv.findIndex((arg) => arg.startsWith("--"));
  const commands = argv.slice(0, flagIndex < 0 ? argv.length : flagIndex);
  if (commands.length > 1) {
    const flags = flagIndex < 0 ? [] : argv.slice(flagIndex);
    for (const command of commands) await runCli([command, ...flags]);
    return;
  }
  const options = parseArguments(argv);
  const actions = {
    dependencies: () => checkDependencyPins(options.packageRoot),
    hygiene: () => checkHygiene(options.packageRoot, options.workflowRoot),
    parity: () => runSyntheticParity(options.packageRoot, options.parityTest),
    skips: () => checkSkipAllowances(options.packageRoot),
    tests: () => runTests(options.packageRoot),
    typecheck: () => runTypeCheck(options.packageRoot),
    workflows: () => {
      if (options.workflowRoot === undefined) {
        throw new Error("workflow parsing requires --workflows");
      }
      checkWorkflows(options.workflowRoot);
    },
  };

  if (options.commands.includes("all")) {
    if (options.commands.length !== 1) throw new Error("all cannot be combined with other checks");
    const failures = [];
    for (const check of [
      "dependencies",
      "hygiene",
      "skips",
      "workflows",
      "typecheck",
      "parity",
      "tests",
    ]) {
      try {
        actions[check]();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`FAIL ${message}`);
        failures.push(check);
      }
    }
    if (failures.length === 0) {
      console.log("PASS all checks");
    } else {
      console.error(`FAIL checks: ${failures.join(", ")}`);
      process.exitCode = 1;
    }
    return;
  }

  for (const command of options.commands) {
    const action = actions[command];
    if (action === undefined) throw new Error(`unknown check: ${command}`);
    action();
  }
}

const entry = process.argv[1];
if (
  entry !== undefined &&
  pathToFileURL(resolve(entry)).href === import.meta.url
) {
  try {
    await runCli(process.argv.slice(2));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`FAIL ${message}`);
    process.exitCode = 1;
  }
}
