import assert from "node:assert/strict";
import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";

import * as yaml from "js-yaml";
import { smokeRelease } from "../support/release-smoke.ts";
import { ideaVersion } from "../../src/version.ts";
import { optionalService } from "../support/fixtures.ts";

interface DistributionMetadata {
  selfContained: boolean;
  runTimeRequirements: string[];
  target: string;
  sizes: {
    executableBytes: number;
    releaseArchiveBytes: number;
    embeddedSupportArchiveBytes: number;
    extractedSupportBytes: number;
  };
  sha256: string;
}

const PACKAGE_ROOT = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const REPOSITORY_ROOT = dirname(dirname(dirname(PACKAGE_ROOT)));
const TEST_ROOT = mkdtempSync(join(tmpdir(), "ideactl-dist-test-"));
const BUILD_ROOT = join(TEST_ROOT, "build");
const CLEAN_ROOT = join(TEST_ROOT, "clean");
const CLEAN_HOME = join(TEST_ROOT, "home");
const CLEAN_TMP = join(TEST_ROOT, "tmp");
const EMPTY_PATH = join(TEST_ROOT, "empty-path");
const EXECUTABLE = join(CLEAN_ROOT, "ideactl");
const VALUES_FILE = join(CLEAN_ROOT, "values.yml");

let metadata: DistributionMetadata;
let archiveName = "";
let archiveBytes = Buffer.alloc(0);
let checksumText = "";
let buildTranscript = "";

/**
 * Runs one process and returns a complete diagnostic on failure.
 *
 * @param command executable path or command name
 * @param args command arguments
 * @param cwd working directory
 * @param env constrained environment
 */
function run(
  command: string,
  args: string[],
  cwd: string,
  env: NodeJS.ProcessEnv = process.env,
): SpawnSyncReturns<string> {
  const result = spawnSync(command, args, { cwd, env, encoding: "utf8" });
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
 * Asserts the file taken from the release archive is a regular file of the
 * size the builder advertised for the executable.
 *
 * @param path extracted executable path
 * @param advertisedBytes size recorded in the builder metadata
 */
function assertExtractedExecutable(path: string, advertisedBytes: number): void {
  const extracted = lstatSync(path);
  assert.equal(extracted.isSymbolicLink(), false);
  assert.equal(extracted.isFile(), true);
  assert.equal(extracted.size, advertisedBytes);
}

/**
 * Validates the generated metadata before tests rely on its fields.
 *
 * @param value parsed metadata
 */
function requireMetadata(value: object): asserts value is DistributionMetadata {
  const record = value as Record<string, unknown>;
  const sizes = record.sizes;
  assert.equal(record.selfContained, true);
  assert.ok(Array.isArray(record.runTimeRequirements));
  assert.equal(typeof record.target, "string");
  assert.equal(typeof record.sha256, "string");
  assert.ok(typeof sizes === "object" && sizes !== null);
  const sizeRecord = sizes as Record<string, unknown>;
  for (const key of [
    "executableBytes",
    "releaseArchiveBytes",
    "embeddedSupportArchiveBytes",
    "extractedSupportBytes",
  ]) {
    assert.equal(typeof sizeRecord[key], "number");
  }
}

// The two-target release build runs only on macOS. A public checkout on another platform skips
// this file; anywhere else the missing build host is a loud failure.
const canBuild = optionalService(process.platform === "darwin", "macOS build host for the two-target release build", "run this file on macOS");

before(() => {
  if (!canBuild) return;
  mkdirSync(BUILD_ROOT, { recursive: true });
  mkdirSync(CLEAN_ROOT, { recursive: true });
  mkdirSync(CLEAN_HOME, { recursive: true });
  mkdirSync(CLEAN_TMP, { recursive: true });
  mkdirSync(EMPTY_PATH, { recursive: true });
  assert.deepEqual(readdirSync(CLEAN_ROOT), []);

  const build = run(
    process.execPath,
    [
      join(PACKAGE_ROOT, "scripts", "build-sea.mjs"),
      "--output-directory",
      BUILD_ROOT,
    ],
    PACKAGE_ROOT,
  );
  buildTranscript = build.stdout.trim();

  const metadataFile = readdirSync(BUILD_ROOT).find((name) => name.endsWith(".json"));
  const archiveFile = readdirSync(BUILD_ROOT).find((name) => name.endsWith(".tar.gz"));
  assert.notEqual(metadataFile, undefined);
  assert.notEqual(archiveFile, undefined);
  if (metadataFile === undefined || archiveFile === undefined) {
    throw new Error("distribution build did not produce metadata and an archive");
  }

  const parsed: object = JSON.parse(readFileSync(join(BUILD_ROOT, metadataFile), "utf8"));
  requireMetadata(parsed);
  metadata = parsed;
  archiveName = archiveFile;
  archiveBytes = readFileSync(join(BUILD_ROOT, archiveFile));
  checksumText = readFileSync(join(BUILD_ROOT, `${archiveFile}.sha256`), "utf8");

  writeFileSync(
    VALUES_FILE,
    [
      "cluster_name: idea-test1",
      "administrator_email: admin@example.invalid",
      'aws_account_id: "123456789012"',
      "aws_dns_suffix: amazonaws.com",
      "aws_partition: aws",
      "aws_region: us-east-2",
      "ssh_key_pair_name: idea-test1-key",
      "vpc_cidr_block: 203.0.113.0/24",
      "base_os: amazonlinux2023",
      "instance_type: m7i.large",
      "volume_size: 200",
      "volume_type: gp3",
      "storage_apps_provider: efs",
      "storage_data_provider: efs",
      "directory_service_provider: aws_managed_activedirectory",
      "metrics_provider: cloudwatch",
      "alb_public: true",
      "enable_aws_backup: false",
      // Every new cluster runs its control plane as container tasks; the installer writes this
      // key and `config generate` refuses a new cluster without it.
      "enable_ecs: true",
      "enabled_modules:",
      "  - metrics",
      "",
    ].join("\n"),
  );

  rmSync(BUILD_ROOT, { recursive: true, force: true });

  const archiveCopy = join(TEST_ROOT, archiveName);
  writeFileSync(archiveCopy, archiveBytes);
  run("tar", ["-xzf", archiveCopy, "-C", CLEAN_ROOT], TEST_ROOT);
});

after(() => {
  rmSync(TEST_ROOT, { recursive: true, force: true });
});

test("release archive contains one self-contained executable with a valid checksum", { skip: !canBuild }, () => {
  assert.equal(metadata.selfContained, true);
  assert.deepEqual(metadata.runTimeRequirements, []);
  assert.ok(metadata.sizes.executableBytes > metadata.sizes.embeddedSupportArchiveBytes);
  assert.ok(metadata.sizes.extractedSupportBytes > metadata.sizes.embeddedSupportArchiveBytes);
  assert.equal(metadata.sizes.releaseArchiveBytes, archiveBytes.length);

  const digest = createHash("sha256").update(archiveBytes).digest("hex");
  assert.equal(digest, metadata.sha256);
  assert.equal(checksumText, `${digest}  ${archiveName}\n`);

  const listing = run("tar", ["-tzf", join(TEST_ROOT, archiveName)], TEST_ROOT);
  assert.deepEqual(listing.stdout.trim().split("\n"), ["ideactl"]);
});

test("the copied executable runs and renders config without a runtime or package manager", { skip: !canBuild }, () => {
  assertExtractedExecutable(EXECUTABLE, metadata.sizes.executableBytes);
  assert.deepEqual(readdirSync(CLEAN_ROOT).sort(), ["ideactl", "values.yml"]);

  const environment: NodeJS.ProcessEnv = {
    HOME: CLEAN_HOME,
    IDEA_USER_HOME: join(CLEAN_HOME, ".idea"),
    LANG: "en_US.UTF-8",
    NODE_PATH: "",
    PATH: EMPTY_PATH,
    TMPDIR: CLEAN_TMP,
  };
  const about = run(EXECUTABLE, ["about"], CLEAN_ROOT, environment);
  const render = run(
    EXECUTABLE,
    [
      "config",
      "generate",
      "--values-file",
      VALUES_FILE,
      "--config-dir",
      CLEAN_ROOT,
      "--force",
    ],
    CLEAN_ROOT,
    environment,
  );

  assert.match(about.stdout, new RegExp(`^ideactl ${ideaVersion().replaceAll('.', '\\.')}$`, 'm'));
  assert.match(render.stdout, /generating config from templates/);
  assert.equal(existsSync(join(CLEAN_ROOT, "config", "idea.yml")), true);
  assert.equal(existsSync(join(CLEAN_ROOT, "config", "metrics", "settings.yml")), true);
  assert.equal(existsSync(join(CLEAN_HOME, ".npm")), false);

  console.log(
    [
      buildTranscript,
      "$ PATH=/empty ./ideactl about",
      about.stdout.trim(),
      "$ PATH=/empty ./ideactl config generate --values-file values.yml --config-dir . --force",
      render.stdout.trim(),
    ].join("\n"),
  );
});

test("release automation builds all operator targets and parses", { skip: !canBuild }, () => {
  const workflowDirectory = join(REPOSITORY_ROOT, ".github", "workflows");
  const workflowNames = [
    "build_push.yaml",
    "lint_build.yaml",
    "sync_docs_branch.yaml",
    "unit_tests.yaml",
  ];
  for (const name of workflowNames) {
    const source = readFileSync(join(workflowDirectory, name), "utf8");
    assert.doesNotThrow(() => yaml.load(source), `${name} must parse as YAML`);
  }

  const releaseWorkflow = readFileSync(join(workflowDirectory, "build_push.yaml"), "utf8");

  const targets = [...releaseWorkflow.matchAll(/target: ((?:darwin|linux)-\S+)|build:dist -- --target (windows-\S+)/g)]
    .map((match) => match[1] ?? match[2]);
  assert.deepEqual(targets.sort(), ["darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64", "windows-amd64"]);

  assert.match(releaseWorkflow, /sha256sum --check SHA256SUMS/);
  assert.match(releaseWorkflow, /release create/);
  assert.doesNotMatch(releaseWorkflow, /--clobber/);
  assert.match(
    releaseWorkflow,
    /needs:\s*\n\s+- build_ideactl_artifacts\s*\n\s+- build_ideactl_linux_artifact\s*\n\s+- build_ideactl_windows_artifact\s*\n\s+- build_push_ideactl/,
  );
  assert.match(releaseWorkflow, /secrets\.ECR_ROLE/);
});

test("extracted standalone executable synthesizes a Lambda stack", { skip: !canBuild }, () => {
  smokeRelease(EXECUTABLE);
});
