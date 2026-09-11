/**
 * Guards the two-target standalone release surface.
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

test("the release automation has two arm64 targets and no matrix", () => {
  const workflow = readFileSync(
    join(repositoryRoot, ".github", "workflows", "build_push.yaml"),
    "utf8",
  );
  const start = workflow.indexOf("  build_ideactl_artifacts:");
  const end = workflow.indexOf("  build_push:", start);

  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const releaseJobs = workflow.slice(start, end);
  const targets = new Set(
    [...releaseJobs.matchAll(/\b(?:darwin|linux)-(?:amd64|arm64)\b/g)].map(
      (match) => match[0],
    ),
  );

  assert.deepEqual([...targets].sort(), ["darwin-arm64", "linux-arm64"]);
  assert.doesNotMatch(releaseJobs, /\bmatrix\b/);
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
