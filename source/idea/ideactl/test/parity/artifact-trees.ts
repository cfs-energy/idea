/**
 * Builds the runtime resource tree each assembler produces, then compares the
 * files. The comparison is of those trees, not of the assembler source.
 */

import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import type { PolicyVars } from "../../src/cdk/policy.ts";

const HERE = dirname(fileURLToPath(import.meta.url));

/** Package root (`source/idea/ideactl`). */
export const PACKAGE_ROOT = resolve(HERE, "..", "..");

/** Repository root that the copy scripts walk to for the bootstrap tree. */
export const REPOSITORY_ROOT = resolve(PACKAGE_ROOT, "..", "..", "..");

/** Directories the copy scripts take from the package resource tree. */
export const RESOURCE_GROUPS = [
  "cdk",
  "config",
  "input_params",
  "integration_tests",
  "policies",
] as const;

/** Values file that asks for the container control-plane shape. */
export const CONTAINER_VALUES = join(PACKAGE_ROOT, "test", "cli", "shell-path-values.yml");

/** Control-plane image recipe. Its COPY list is the image assembler's input set. */
export const CONTROL_PLANE_DOCKERFILE = join(
  REPOSITORY_ROOT,
  "deployment",
  "ecr",
  "idea-control-plane",
  "Dockerfile",
);

export interface TreeDiff {
  onlyLeft: string[];
  onlyRight: string[];
  mismatched: string[];
}

export interface GenerateResult {
  status: number;
  stdout: string;
  stderr: string;
}

/**
 * Lists regular files under `root` as `/`-separated paths relative to it.
 *
 * @param root directory to walk
 * @returns sorted relative paths
 */
export function listFiles(root: string): string[] {
  const files: string[] = [];
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(path);
        continue;
      }
      if (entry.isFile()) {
        files.push(relative(root, path).split("\\").join("/"));
      }
    }
  };
  if (existsSync(root)) visit(root);
  return files.sort();
}

/**
 * Drops prebuilt Lambda asset paths. Only the zip-building assemblers write them.
 *
 * @param paths relative resource paths
 * @returns paths outside `lambda_assets/`
 */
export function withoutLambdaAssets(paths: readonly string[]): string[] {
  return paths.filter((path) => !path.startsWith("lambda_assets/"));
}

/**
 * Compares two resource trees by relative path and file bytes.
 *
 * @param left first tree
 * @param right second tree
 * @returns paths only on one side, plus shared paths whose bytes differ
 */
export function diffTrees(left: string, right: string): TreeDiff {
  const leftFiles = new Set(withoutLambdaAssets(listFiles(left)));
  const rightFiles = new Set(withoutLambdaAssets(listFiles(right)));
  const onlyLeft = [...leftFiles].filter((path) => !rightFiles.has(path)).sort();
  const onlyRight = [...rightFiles].filter((path) => !leftFiles.has(path)).sort();
  const mismatched: string[] = [];
  for (const path of leftFiles) {
    if (!rightFiles.has(path)) continue;
    const leftBytes = readFileSync(join(left, path));
    const rightBytes = readFileSync(join(right, path));
    if (!leftBytes.equals(rightBytes)) mismatched.push(path);
  }
  return { onlyLeft, onlyRight, mismatched };
}

/**
 * True when both trees have the same shared-group files and those files match.
 *
 * @param left first tree
 * @param right second tree
 */
export function treesAgree(left: string, right: string): boolean {
  const diff = diffTrees(left, right);
  return diff.onlyLeft.length === 0 && diff.onlyRight.length === 0 && diff.mismatched.length === 0;
}

/**
 * The checkout files the assemblers are supposed to ship: the six package
 * resource groups and the bootstrap tree.
 *
 * @returns relative paths in assembled-tree layout
 */
export function checkoutUnion(): string[] {
  const resources = join(PACKAGE_ROOT, "resources");
  const bootstrap = join(REPOSITORY_ROOT, "source", "idea", "idea-bootstrap");
  const paths = new Set<string>();
  for (const group of RESOURCE_GROUPS) {
    for (const file of listFiles(join(resources, group))) {
      paths.add(`${group}/${file}`);
    }
  }
  for (const file of listFiles(bootstrap)) {
    paths.add(`bootstrap/${file}`);
  }
  return [...paths].sort();
}

/**
 * Paths present in the checkout union and missing from an assembled tree.
 *
 * @param assembled assembled resources directory
 */
export function missingFromCheckout(assembled: string): string[] {
  const have = new Set(withoutLambdaAssets(listFiles(assembled)));
  return checkoutUnion().filter((path) => !have.has(path));
}

/**
 * Runs `scripts/copy-resources.mjs` into `destination`.
 *
 * @param destination output resources directory
 */
export function assembleCopyResources(destination: string): void {
  mkdirSync(dirname(destination), { recursive: true });
  const result = spawnSync(process.execPath, [join(PACKAGE_ROOT, "scripts", "copy-resources.mjs")], {
    cwd: PACKAGE_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      IDEACTL_RESOURCE_OUTPUT_DIR: destination,
    },
  });
  if (result.status !== 0) {
    throw new Error(
      [`copy-resources failed`, result.stdout, result.stderr, result.error?.message ?? ""].join("\n"),
    );
  }
}

/**
 * Runs the shell assembler's `copyRuntimeResources` without bundling application code.
 *
 * The function is not exported, so this writes a short caller next to a copy of
 * the script and pins the package root to this checkout.
 *
 * @param destination output resources directory
 */
export function assembleShellResources(destination: string): void {
  const source = readFileSync(join(PACKAGE_ROOT, "scripts", "build-shell-bundle.mjs"), "utf8");
  if (!source.trimEnd().endsWith("main();")) {
    throw new Error("shell assembler no longer ends with main(); cannot extract the copy step");
  }
  const pinned = source
    .replace(
      "const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), \"..\");",
      `const PACKAGE_ROOT = ${JSON.stringify(PACKAGE_ROOT)};`,
    )
    .replace(
      /main\(\);\s*$/u,
      [
        "const destination = process.argv[2];",
        "const lambdaAssets = process.argv[3];",
        "copyRuntimeResources(destination, lambdaAssets);",
      ].join("\n"),
    );
  const work = mkdtempSync(join(tmpdir(), "ideactl-artifact-parity-shell-"));
  const harness = join(work, "run-shell-copy.mjs");
  const lambdaAssets = join(work, "lambda_assets");
  mkdirSync(lambdaAssets, { recursive: true });
  writeFileSync(harness, pinned);
  try {
    const result = spawnSync(process.execPath, [harness, destination, lambdaAssets], {
      cwd: PACKAGE_ROOT,
      encoding: "utf8",
    });
    if (result.status !== 0) {
      throw new Error(
        [`shell resource copy failed`, result.stdout, result.stderr, result.error?.message ?? ""].join("\n"),
      );
    }
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

/**
 * Host paths the control-plane Dockerfile copies into `/idea-build/`.
 *
 * @param dockerfileText Dockerfile contents
 */
export function dockerfileBuildContextSources(dockerfileText: string): string[] {
  const sources: string[] = [];
  for (const rawLine of dockerfileText.split("\n")) {
    const line = rawLine.trim();
    if (!line.startsWith("COPY ")) continue;
    if (line.includes("--from=")) continue;
    const parts = line.slice("COPY ".length).trim().split(/\s+/u);
    if (parts.length < 2) continue;
    const destination = parts[parts.length - 1];
    if (!destination.startsWith("/idea-build/")) continue;
    for (const source of parts.slice(0, -1)) {
      if (source.startsWith("-")) continue;
      sources.push(source);
    }
  }
  return sources;
}

/**
 * Recreates the Dockerfile `/idea-build` tree from this checkout, then runs the
 * copied `copy-resources.mjs`. A forgotten `COPY` of an overlay directory makes
 * that run miss the overlay or throw.
 *
 * @param destination output resources directory
 */
export function assembleDockerfileResources(destination: string): void {
  const sources = dockerfileBuildContextSources(readFileSync(CONTROL_PLANE_DOCKERFILE, "utf8"));
  if (sources.length === 0) {
    throw new Error("control-plane Dockerfile has no COPY lines into /idea-build/");
  }
  const context = mkdtempSync(join(tmpdir(), "ideactl-artifact-parity-image-"));
  try {
    for (const source of sources) {
      const from = join(REPOSITORY_ROOT, source);
      const to = join(context, source);
      if (!existsSync(from)) {
        throw new Error(`Dockerfile COPY source missing from checkout: ${source}`);
      }
      mkdirSync(dirname(to), { recursive: true });
      cpSync(from, to, { recursive: true });
    }
    const script = join(context, "source", "idea", "ideactl", "scripts", "copy-resources.mjs");
    if (!existsSync(script)) {
      throw new Error("Dockerfile context is missing scripts/copy-resources.mjs");
    }
    const result = spawnSync(process.execPath, [script], {
      cwd: dirname(script),
      encoding: "utf8",
      env: {
        ...process.env,
        IDEACTL_RESOURCE_OUTPUT_DIR: destination,
      },
    });
    if (result.status !== 0) {
      throw new Error(
        [`dockerfile copy-resources failed`, result.stdout, result.stderr, result.error?.message ?? ""].join(
          "\n",
        ),
      );
    }
  } finally {
    rmSync(context, { recursive: true, force: true });
  }
}

/**
 * Writes a copy of `copy-resources.mjs` that skips the container module's template
 * directory, then runs it. Used to prove the comparison goes red when one assembler
 * ships less of the resource tree than the others.
 *
 * @param destination output resources directory
 */
export function assembleSabotagedCopyResources(destination: string): void {
  const original = readFileSync(join(PACKAGE_ROOT, "scripts", "copy-resources.mjs"), "utf8");
  const groupCopy = [
    "  cpSync(",
    "    join(packageResources, directory),",
    "    join(outputResources, directory),",
    "    { recursive: true },",
    "  );",
  ].join("\n");
  if (!original.includes(groupCopy)) {
    throw new Error("copy-resources group copy not found; sabotage cannot narrow it");
  }
  const sabotaged = original
    .replace(
      groupCopy,
      groupCopy.replace(
        "{ recursive: true }",
        "{ recursive: true, filter: (from) => !from.split(\"\\\\\").join(\"/\").endsWith(\"/config/templates/ecs\") }",
      ),
    )
    .replace(
      "const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), \"..\");",
      `const packageRoot = ${JSON.stringify(PACKAGE_ROOT)};`,
    );
  const work = mkdtempSync(join(tmpdir(), "ideactl-artifact-parity-sabotage-"));
  const script = join(work, "copy-resources.mjs");
  writeFileSync(script, sabotaged);
  try {
    const result = spawnSync(process.execPath, [script], {
      cwd: PACKAGE_ROOT,
      encoding: "utf8",
      env: {
        ...process.env,
        IDEACTL_RESOURCE_OUTPUT_DIR: destination,
      },
    });
    if (result.status !== 0) {
      throw new Error(
        [`sabotaged copy-resources failed`, result.stdout, result.stderr, result.error?.message ?? ""].join(
          "\n",
        ),
      );
    }
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

/**
 * Stages a copy of `src/` next to an assembled resource tree so runtime path
 * lookup reads that tree and not the development checkout overlay.
 *
 * @param resources assembled resources directory
 * @returns stage root the caller must remove
 */
export function stageTool(resources: string): string {
  const stage = mkdtempSync(join(tmpdir(), "ideactl-artifact-parity-stage-"));
  cpSync(join(PACKAGE_ROOT, "src"), join(stage, "src"), { recursive: true });
  cpSync(resources, join(stage, "resources"), { recursive: true });
  symlinkSync(join(PACKAGE_ROOT, "node_modules"), join(stage, "node_modules"), "dir");
  return stage;
}

/**
 * Runs `config generate` through the staged tool against the container values file.
 *
 * @param stage directory from `stageTool`
 * @param configDir output cluster directory
 */
export async function generateConfig(stage: string, configDir: string): Promise<GenerateResult> {
  mkdirSync(configDir, { recursive: true });
  const main = pathToFileURL(join(stage, "src", "cli", "main.ts")).href;
  const { run } = (await import(main)) as { run: (argv?: string[]) => Promise<number> };
  const home = join(stage, "home");
  mkdirSync(home, { recursive: true });
  const previousHome = process.env.HOME;
  const previousIdeaHome = process.env.IDEA_USER_HOME;
  process.env.HOME = home;
  process.env.IDEA_USER_HOME = join(home, ".idea");
  const stdout: string[] = [];
  const stderr: string[] = [];
  const originalLog = console.log;
  const originalError = console.error;
  console.log = (...args: unknown[]) => {
    stdout.push(args.map(String).join(" "));
  };
  console.error = (...args: unknown[]) => {
    stderr.push(args.map(String).join(" "));
  };
  try {
    const status = await run([
      "config",
      "generate",
      "--values-file",
      CONTAINER_VALUES,
      "--config-dir",
      configDir,
      "--force",
    ]);
    return { status, stdout: stdout.join("\n"), stderr: stderr.join("\n") };
  } finally {
    console.log = originalLog;
    console.error = originalError;
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    if (previousIdeaHome === undefined) delete process.env.IDEA_USER_HOME;
    else process.env.IDEA_USER_HOME = previousIdeaHome;
  }
}

/**
 * Renders one policy document from the staged resource tree.
 *
 * @param stage directory from `stageTool`
 */
export async function renderSamplePolicy(stage: string): Promise<object> {
  const policyModule = pathToFileURL(join(stage, "src", "cdk", "policy.ts")).href;
  const configModule = pathToFileURL(join(stage, "src", "config", "cluster-config.ts")).href;
  const { renderPolicy } = (await import(policyModule)) as {
    renderPolicy: (name: string, vars: PolicyVars) => object;
  };
  const { ClusterConfig: StagedConfig } = (await import(configModule)) as {
    ClusterConfig: typeof ClusterConfig;
  };
  const items = [
    ["cluster.cluster_name", "sample-cluster"],
    ["cluster.aws.region", "us-east-2"],
    ["cluster.aws.dns_suffix", "amazonaws.com"],
    ["cluster.aws.partition", "aws"],
    ["cluster.aws.account_id", "123456789012"],
  ].map(([key, value]) => ({ key: { S: key }, value: { S: value } }));
  const config = StagedConfig.fromFile(JSON.stringify({ Items: items }), JSON.stringify({ Items: [] }));
  return renderPolicy("amazon-ssm-managed-instance-core.yml", { config });
}

/**
 * Creates a scratch directory under the process temp root.
 *
 * @param label suffix for the directory name
 */
export function scratch(label: string): string {
  return mkdtempSync(join(tmpdir(), `ideactl-artifact-parity-${label}-`));
}

/**
 * True when `path` is a directory.
 *
 * @param path filesystem path
 */
export function isDirectory(path: string): boolean {
  return existsSync(path) && statSync(path).isDirectory();
}
