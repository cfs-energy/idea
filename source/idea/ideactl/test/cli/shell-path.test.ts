import assert from "node:assert/strict";
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
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { smokeRelease } from "../support/release-smoke.ts";
import { ideaVersion } from "../../src/version.ts";

interface ShellManifest {
  runtime: {
    packageManagerRequired: boolean;
    compilerRequired: boolean;
    nativeModules: string[];
    externalImports: string[];
  };
  contentBytes: {
    applicationBundle: number;
    deploymentCli: number;
    resources: number;
  };
  bundledPackages: string[];
}

const PACKAGE_ROOT = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const TEST_ROOT = mkdtempSync(join(tmpdir(), "ideactl-shell-path-"));
const BUILD_OUTPUT = join(TEST_ROOT, "build", "ideactl-shell");
const ARCHIVE = join(TEST_ROOT, "ideactl-shell.tar.gz");
const RELEASE_ROOT = join(TEST_ROOT, "release");
const ARTIFACT = join(RELEASE_ROOT, "ideactl-shell");
const CLEAN_HOME = join(TEST_ROOT, "clean-home");
const CLEAN_WORK = join(TEST_ROOT, "clean-work");
const RUNTIME_BIN = join(TEST_ROOT, "runtime-bin");
const VALUES_FILE = join(CLEAN_WORK, "values.yml");

/**
 * Runs one executable and reports complete output when it fails.
 *
 * @param command executable path
 * @param args executable arguments
 * @param env constrained process environment
 */
function run(
  command: string,
  args: string[],
  env: NodeJS.ProcessEnv = process.env,
): SpawnSyncReturns<string> {
  const result = spawnSync(command, args, {
    cwd: CLEAN_WORK,
    env,
    encoding: "utf8",
  });
  assert.equal(
    result.status,
    0,
    [
      `${command} ${args.join(" ")} failed`,
      `stdout:\n${result.stdout}`,
      `stderr:\n${result.stderr}`,
      result.error?.stack ?? "",
    ].join("\n"),
  );
  return result;
}

/**
 * Returns every regular file under a directory.
 *
 * @param root directory to walk
 */
function filesUnder(root: string): string[] {
  const files: string[] = [];
  const visit = (path: string): void => {
    const stats = statSync(path);
    if (stats.isDirectory()) {
      for (const name of readdirSync(path)) visit(join(path, name));
    } else if (stats.isFile()) {
      files.push(path);
    }
  };
  visit(root);
  return files;
}

before(() => {
  mkdirSync(CLEAN_HOME, { recursive: true });
  mkdirSync(CLEAN_WORK, { recursive: true });
  mkdirSync(RUNTIME_BIN, { recursive: true });
  symlinkSync(process.execPath, join(RUNTIME_BIN, "node"));
  cpSync(join(PACKAGE_ROOT, "test", "cli", "shell-path-values.yml"), VALUES_FILE);

  const build = spawnSync(
    process.execPath,
    [
      join(PACKAGE_ROOT, "scripts", "build-shell-bundle.mjs"),
      "--output",
      BUILD_OUTPUT,
      "--archive",
      ARCHIVE,
    ],
    {
      cwd: PACKAGE_ROOT,
      encoding: "utf8",
    },
  );
  assert.equal(
    build.status,
    0,
    [`artifact build failed`, build.stdout, build.stderr, build.error?.stack ?? ""].join("\n"),
  );
  assert.equal(existsSync(ARCHIVE), true);
  mkdirSync(RELEASE_ROOT, { recursive: true });
  const extract = spawnSync("tar", ["-xzf", ARCHIVE, "-C", RELEASE_ROOT], {
    cwd: TEST_ROOT,
    encoding: "utf8",
  });
  assert.equal(
    extract.status,
    0,
    [`artifact extraction failed`, extract.stdout, extract.stderr, extract.error?.stack ?? ""].join("\n"),
  );
  rmSync(dirname(BUILD_OUTPUT), { recursive: true, force: true });
});

after(() => {
  rmSync(TEST_ROOT, { recursive: true, force: true });
});

test("bundle has no run-time package or native-module resolution", () => {
  const manifest: ShellManifest = JSON.parse(
    readFileSync(join(ARTIFACT, "manifest.json"), "utf8"),
  );
  assert.equal(manifest.runtime.packageManagerRequired, false);
  assert.equal(manifest.runtime.compilerRequired, false);
  assert.deepEqual(manifest.runtime.nativeModules, []);
  assert.deepEqual(manifest.runtime.externalImports, []);
  assert.ok(manifest.contentBytes.applicationBundle > 0);
  assert.ok(manifest.contentBytes.deploymentCli > 0);
  assert.ok(manifest.contentBytes.resources > 0);
  assert.ok(manifest.bundledPackages.includes("commander"));
  assert.deepEqual(readdirSync(join(ARTIFACT, "dist", "node_modules")), ["aws-cdk"]);
  assert.deepEqual(
    filesUnder(ARTIFACT).filter((file) => file.endsWith(".node")),
    [],
  );
});

test("clean runtime prints its version and renders configuration", () => {
  const cleanTmp = join(TEST_ROOT, "tmp");
  const environment: NodeJS.ProcessEnv = {
    HOME: CLEAN_HOME,
    IDEA_USER_HOME: join(CLEAN_HOME, ".idea"),
    LANG: "en_US.UTF-8",
    NODE_PATH: "",
    PATH: RUNTIME_BIN,
    TMPDIR: cleanTmp,
  };
  mkdirSync(cleanTmp, { recursive: true });

  const about = run(join(ARTIFACT, "bin", "ideactl"), ["about"], environment);
  const render = run(
    join(ARTIFACT, "bin", "ideactl"),
    [
      "config",
      "generate",
      "--values-file",
      VALUES_FILE,
      "--config-dir",
      CLEAN_WORK,
      "--force",
    ],
    environment,
  );
  const deploymentCli = run(
    join(RUNTIME_BIN, "node"),
    [join(ARTIFACT, "dist", "node_modules", "aws-cdk", "bin", "cdk"), "--version"],
    environment,
  );

  assert.match(about.stdout, new RegExp(`^ideactl ${ideaVersion().replaceAll('.', '\\.')}$`, 'm'));
  assert.match(render.stdout, /generating config from templates/);
  assert.equal(existsSync(join(CLEAN_WORK, "config", "idea.yml")), true);
  assert.equal(existsSync(join(CLEAN_WORK, "config", "metrics", "settings.yml")), true);
  // The CDK CLI the package pins, not a literal, so a dependency bump cannot fail this on its own.
  const cdkCliVersion = (JSON.parse(readFileSync(new URL("../../node_modules/aws-cdk/package.json", import.meta.url), "utf8")) as { version: string }).version;
  assert.match(deploymentCli.stdout, new RegExp(`^${cdkCliVersion.replace(/\./g, "\\.")}`, "m"));
  assert.equal(existsSync(join(CLEAN_HOME, ".npm")), false);
  assert.deepEqual(readdirSync(RUNTIME_BIN), ["node"]);

  console.log(
    [
      "$ ideactl about",
      about.stdout.trim(),
      "$ ideactl config generate --values-file values.yml --config-dir . --force",
      render.stdout.trim(),
      "$ node dist/node_modules/aws-cdk/bin/cdk --version",
      deploymentCli.stdout.trim(),
    ].join("\n"),
  );
});

test("extracted shell artifact synthesizes a Lambda stack without handler sources", () => {
  assert.equal(existsSync(join(ARTIFACT, "dist", "src", "lambda")), false);
  smokeRelease(join(ARTIFACT, "bin", "ideactl"), RUNTIME_BIN);
});
