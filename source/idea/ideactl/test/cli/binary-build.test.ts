/**
 * Guards the native releases and same-architecture macOS cross-build.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const repositoryRoot = resolve(packageRoot, "..", "..", "..");
const buildScript = join(packageRoot, "scripts", "build-sea.mjs");

test("the release command builds the same-architecture pair", () => {
  const packageJson = JSON.parse(
    readFileSync(join(packageRoot, "package.json"), "utf8"),
  ) as { scripts?: Record<string, string> };

  assert.equal(packageJson.scripts?.["build:dist"], "node scripts/build-sea.mjs");
  const script = readFileSync(buildScript, "utf8");
  assert.match(script, /function releaseTargets\(\)/);
  assert.doesNotMatch(script, /linux-sea-runtime/);
  assert.equal(
    existsSync(join(packageRoot, "scripts", "ideactl-linux.Dockerfile")),
    true,
  );
});

test("the release automation covers all five native targets", () => {
  const workflow = readFileSync(
    join(repositoryRoot, ".github", "workflows", "build_push.yaml"),
    "utf8",
  );
  const start = workflow.indexOf("  build_ideactl_artifacts:");
  const end = workflow.indexOf("  build_push_ideactl:", start);

  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const releaseJobs = workflow.slice(start, end);
  const targets = new Set(
    [...releaseJobs.matchAll(/\b(?:darwin|linux|windows)-(?:amd64|arm64)\b/g)].map(
      (match) => match[0],
    ),
  );

  assert.deepEqual([...targets].sort(), ["darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64", "windows-amd64"]);
  assert.match(releaseJobs, /windows-2025/);
  assert.match(releaseJobs, /macos-15-intel/);
  assert.match(workflow, /release\/\*\.zip\.sha256/);
  assert.match(workflow, /release\/\*\.tar\.gz\.sha256/);
  assert.match(workflow, /ideactl config generate/);
});

test("the single-target builder rejects another processor architecture", () => {
  const otherArchitecture = process.arch === "arm64" ? "amd64" : "arm64";
  const result = spawnSync(
    process.execPath,
    [buildScript, "--target", `darwin-${otherArchitecture}`],
    {
      cwd: packageRoot,
      encoding: "utf8",
    },
  );

  assert.notEqual(result.status, 0);
  assert.match(
    `${result.stdout}${result.stderr}`,
    /unsupported release target for/,
  );
});
