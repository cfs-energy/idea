/**
 * The packaged launcher's runtime cache: the real launcher source from build-sea.mjs runs against a
 * small archive, with the single-executable asset API stood in for. It extracts into a private
 * per-user directory, refuses a directory it does not own outright, and loads nothing whose
 * extracted bytes differ from the digest built into it.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve, sep } from "node:path";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = readFileSync(join(packageRoot, "scripts", "build-sea.mjs"), "utf8");
const start = source.indexOf("function tarEntries(root)");
const end = source.indexOf("/**\n * Writes the release archive");
assert.ok(start > 0 && end > start, "build-sea.mjs layout changed");
const build = new Function(
  "createHash", "lstatSync", "readdirSync", "readFileSync", "join", "relative", "sep", "gzipSync", "TAR_BLOCK_SIZE", "VERSION", "INTERNAL_CDK_ARGUMENT",
  `${source.slice(start, end)}\nreturn { createTarGz, runtimeTreeDigest, seaLauncher };`,
)(createHash, lstatSync, readdirSync, readFileSync, join, relative, sep, gzipSync, 512, "0.0.0-test", "__internal__") as {
  createTarGz(root: string): Buffer;
  runtimeTreeDigest(root: string): string;
  seaLauncher(runtimeHash: string, treeHash: string): string;
};

const work = realpathSync(mkdtempSync(join(tmpdir(), "ideactl-sea-cache-test-")));
after(() => rmSync(work, { recursive: true, force: true }));

const runtime = join(work, "runtime");
mkdirSync(runtime);
writeFileSync(join(runtime, "launcher.cjs"), `"use strict";\nconsole.log("runtime-marker " + __dirname);\nmodule.exports = 0;\n`);
const archive = build.createTarGz(runtime);
const archivePath = join(work, "runtime.tar.gz");
writeFileSync(archivePath, archive);
const launcher = join(work, "launcher.cjs");
writeFileSync(launcher, build.seaLauncher(createHash("sha256").update(archive).digest("hex"), build.runtimeTreeDigest(runtime)));
const stub = join(work, "sea-stub.cjs");
writeFileSync(stub, `const Module = require("node:module");
const { readFileSync } = require("node:fs");
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === "node:sea") return { getAsset: () => readFileSync(${JSON.stringify(archivePath)}) };
  return load.call(this, request, ...rest);
};
`);

const cache = join(work, "cache");
mkdirSync(cache, { mode: 0o700 });
const launch = () => spawnSync(process.execPath, ["--require", stub, launcher], { encoding: "utf8", env: { ...process.env, XDG_CACHE_HOME: cache } });

const posix = typeof process.getuid === "function" && process.getuid() !== 0;

test("extracts into a private directory, and re-extracts content that no longer matches the built digest", { skip: !posix }, () => {
  const first = launch();
  assert.equal(first.status, 0, first.stderr);
  const loadedFrom = /runtime-marker (\S+)/.exec(first.stdout)?.[1];
  assert.ok(loadedFrom, first.stdout);
  assert.equal(dirname(loadedFrom), join(cache, "ideactl"));
  const parent = lstatSync(dirname(loadedFrom));
  assert.equal(parent.uid, process.getuid!());
  assert.equal(parent.mode & 0o777, 0o700);

  // a changed file in the cache is not loaded: the tree is replaced from the embedded archive
  writeFileSync(join(loadedFrom, "launcher.cjs"), `"use strict";\nconsole.log("changed-marker");\nmodule.exports = 0;\n`);
  const second = launch();
  assert.equal(second.status, 0, second.stderr);
  assert.doesNotMatch(second.stdout, /changed-marker/);
  assert.match(second.stdout, /runtime-marker/);
});

test("refuses a cache directory whose mode is not 0700 rather than adopting it", { skip: !posix }, () => {
  const first = launch();
  const loadedFrom = /runtime-marker (\S+)/.exec(first.stdout)?.[1];
  assert.ok(loadedFrom, first.stdout + first.stderr);
  const parent = dirname(loadedFrom);
  chmodSync(parent, 0o777);
  try {
    const refused = launch();
    assert.notEqual(refused.status, 0);
    assert.match(refused.stderr, /refusing runtime directory/);
    assert.equal(lstatSync(parent).mode & 0o777, 0o777, "the launcher left the mode alone");
  } finally {
    chmodSync(parent, 0o700);
  }
});

test("falls back to a private temp directory when the cache bases it owns cannot be written", { skip: !posix }, () => {
  // XDG_CACHE_HOME and the account's home are both owned but read-only; the account database is
  // stood in for so the test never writes under the real home.
  const readOnly = join(work, "read-only");
  const home = join(work, "read-only-home");
  const temp = join(work, "temp");
  for (const directory of [readOnly, home, temp]) mkdirSync(directory, { mode: 0o700 });
  chmodSync(readOnly, 0o500);
  chmodSync(home, 0o500);
  const homeStub = join(work, "home-stub.cjs");
  writeFileSync(homeStub, `const os = require("node:os");
os.userInfo = () => ({ homedir: ${JSON.stringify(home)} });
`);
  try {
    const result = spawnSync(process.execPath, ["--require", stub, "--require", homeStub, launcher], {
      encoding: "utf8", env: { ...process.env, XDG_CACHE_HOME: readOnly, TMPDIR: temp },
    });
    assert.equal(result.status, 0, result.stderr);
    const loadedFrom = /runtime-marker (\S+)/.exec(result.stdout)?.[1];
    assert.equal(dirname(loadedFrom!), join(temp, `ideactl-${process.getuid!()}`));
  } finally {
    chmodSync(readOnly, 0o700);
    chmodSync(home, 0o700);
  }
});

test("falls back to the temp directory when an existing cache can no longer be written", { skip: !posix }, () => {
  const cacheBase = join(work, "stale-cache");
  const temp = join(work, "temp-stale");
  mkdirSync(join(cacheBase, "ideactl"), { recursive: true, mode: 0o700 });
  chmodSync(join(cacheBase, "ideactl"), 0o700);
  mkdirSync(temp, { mode: 0o700 });
  // an earlier, unverified runtime the launcher cannot remove: writing in the cache fails (EACCES)
  const stale = join(cacheBase, "ideactl", `0.0.0-test-${createHash("sha256").update(archive).digest("hex").slice(0, 16)}`, "locked");
  mkdirSync(stale, { recursive: true });
  writeFileSync(join(stale, "file"), "x");
  chmodSync(stale, 0o500);
  try {
    const result = spawnSync(process.execPath, ["--require", stub, launcher], {
      encoding: "utf8", env: { ...process.env, XDG_CACHE_HOME: cacheBase, TMPDIR: temp },
    });
    assert.equal(result.status, 0, result.stderr);
    const loadedFrom = /runtime-marker (\S+)/.exec(result.stdout)?.[1];
    assert.equal(dirname(loadedFrom!), join(temp, `ideactl-${process.getuid!()}`));
  } finally {
    chmodSync(stale, 0o700);
  }
});
