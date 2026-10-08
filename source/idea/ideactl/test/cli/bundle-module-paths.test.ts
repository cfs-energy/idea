/**
 * A bundled dependency still finds files beside its own modules once the release is extracted
 * elsewhere, and the build refuses a bundle where one would not.
 */

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { build, type Plugin } from 'esbuild';

// @ts-expect-error -- plain JavaScript build script without declarations
import { BUNDLE_BANNER, modulePathPlugin, shipPackage, verifyModulePaths } from '../../scripts/build-shell-bundle.mjs';

type Rewritten = Map<string, { packageName: string; file: string; code: string }>;

/** A package that reads a data file through `__dirname` and names itself through `__filename`. */
function fixture(): { root: string; nodeModules: string; out: string; bundle: string } {
  const root = mkdtempSync(join(tmpdir(), 'ideactl bundle paths-'));
  const nodeModules = join(root, 'node_modules');
  const packageRoot = join(nodeModules, 'fixture-pkg');
  mkdirSync(join(packageRoot, 'lib'), { recursive: true });
  mkdirSync(join(packageRoot, 'assets'), { recursive: true });
  writeFileSync(join(packageRoot, 'package.json'), JSON.stringify({ name: 'fixture-pkg', main: 'lib/index.js' }));
  writeFileSync(join(packageRoot, 'assets', 'data.txt'), 'fixture-data');
  writeFileSync(join(packageRoot, 'lib', 'index.js'), [
    '"use strict";',
    '// __dirname named in a comment does not need a rewrite on its own',
    'const fs = require("fs");',
    'const path = require("path");',
    'module.exports = () => fs.readFileSync(path.join(__dirname, "..", "assets", "data.txt"), "utf8") + ":" + path.parse(__filename).name;',
  ].join('\n'));
  writeFileSync(join(root, 'main.mjs'), 'import read from "fixture-pkg";\nconsole.log(read());\n');
  const out = join(root, 'out');
  return { root, nodeModules, out, bundle: join(out, 'dist', 'src', 'cli', 'main.js') };
}

async function bundle(paths: ReturnType<typeof fixture>, plugins: Plugin[]) {
  return build({
    entryPoints: [join(paths.root, 'main.mjs')],
    bundle: true, platform: 'node', format: 'esm', target: 'node22',
    banner: { js: BUNDLE_BANNER as string }, plugins, absWorkingDir: paths.root,
    metafile: true, outfile: paths.bundle, nodePaths: [paths.nodeModules], logLevel: 'silent',
  });
}

function runBundle(bundleFile: string, cwd: string) {
  return spawnSync(process.execPath, [bundleFile], { cwd, encoding: 'utf8' });
}

test('a rewritten dependency reads its own files from the shipped package', async () => {
  const paths = fixture();
  try {
    const rewritten: Rewritten = new Map();
    const result = await bundle(paths, [modulePathPlugin(rewritten, paths.nodeModules)]);
    const shipped = join(paths.out, 'dist', 'node_modules');
    shipPackage('fixture-pkg', shipped, paths.nodeModules);
    const verified = verifyModulePaths(result.metafile, rewritten, shipped, { workingDirectory: paths.root, nodeModules: paths.nodeModules });
    assert.equal(rewritten.size, 1);
    assert.equal(verified.checked, 1);

    // Run from an unrelated directory with the source package gone, as an extracted release does.
    rmSync(paths.nodeModules, { recursive: true, force: true });
    const run = runBundle(paths.bundle, tmpdir());
    assert.equal(run.status, 0, run.stderr);
    assert.equal(run.stdout.trim(), 'fixture-data:index');
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});

test('the build fails when a literal dependency path is not shipped', async () => {
  const paths = fixture();
  try {
    const rewritten: Rewritten = new Map();
    const result = await bundle(paths, [modulePathPlugin(rewritten, paths.nodeModules)]);
    const shipped = join(paths.out, 'dist', 'node_modules');
    shipPackage('fixture-pkg', shipped, paths.nodeModules);
    rmSync(join(shipped, 'fixture-pkg', 'assets', 'data.txt'));
    assert.throws(
      () => verifyModulePaths(result.metafile, rewritten, shipped, { workingDirectory: paths.root, nodeModules: paths.nodeModules }),
      /fixture-pkg\/lib\/index\.js: .*data\.txt.* which is not shipped/,
    );
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});

test('the build fails when a dependency uses __dirname without the rewrite', async () => {
  const paths = fixture();
  try {
    const rewritten: Rewritten = new Map();
    const result = await bundle(paths, []);
    assert.throws(
      () => verifyModulePaths(result.metafile, rewritten, join(paths.out, 'dist', 'node_modules'), { workingDirectory: paths.root, nodeModules: paths.nodeModules }),
      /fixture-pkg\/lib\/index\.js uses __dirname or __filename without a rewrite/,
    );
    // Without the rewrite the bundled dependency looks beside the bundle, which is the failure the
    // released 26.10.3 executable had.
    const run = runBundle(paths.bundle, tmpdir());
    assert.notEqual(run.status, 0);
    assert.match(run.stderr, /ENOENT/);
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});


test('release entry points execute through a symlinked checkout', () => {
  const root = mkdtempSync(join(tmpdir(), 'ideactl entry points-'));
  try {
    const checkout = join(root, 'checkout');
    symlinkSync(join(import.meta.dirname, '../..'), checkout, 'junction');
    for (const [script, args, message] of [
      ['scripts/build-shell-bundle.mjs', ['--invalid-argument'], /unknown argument: --invalid-argument/],
      ['test/support/release-smoke.ts', [], /release executable path is required/],
    ] as const) {
      const result = spawnSync(process.execPath, [join(checkout, script), ...args], { encoding: 'utf8' });
      assert.equal(result.status, 1, result.stderr);
      assert.match(result.stderr, message, 'the main-module guard must reach argument validation');
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

for (const declaration of [
  'var __dirname = "elsewhere";',
  'console.log(__dirname); var __dirname;',
  'const a = 1, __dirname = "elsewhere";',
  'function nested(__dirname) { return __dirname; }',
  'const nested = (__filename) => __filename;',
  'const { path: __dirname } = { path: "elsewhere" };',
  'try {} catch (__filename) {}',
  'function __dirname() {}',
  'class __filename {}',
  'const nested = function __dirname() {};',
  'const [__filename] = [];',
  'const nested = ({ path: __dirname = "elsewhere" }) => __dirname;',
  'import { path as __dirname } from "node:path";',
]) {
  test(`the rewrite rejects a module-owned path binding: ${declaration}`, async () => {
    const paths = fixture();
    try {
      writeFileSync(join(paths.nodeModules, 'fixture-pkg/lib/index.js'),
        `console.log(__filename); ${declaration} module.exports = () => __dirname;`);
      await assert.rejects(bundle(paths, [modulePathPlugin(new Map(), paths.nodeModules)]),
        /declares its own __dirname or __filename/);
    } finally {
      rmSync(paths.root, { recursive: true, force: true });
    }
  });
}

test('path-named properties and strings are not bindings', async () => {
  const paths = fixture();
  try {
    writeFileSync(join(paths.nodeModules, 'fixture-pkg/lib/index.js'),
      'const value = { __dirname: "__filename" }; module.exports = () => __dirname + value.__dirname;');
    const rewritten: Rewritten = new Map();
    await bundle(paths, [modulePathPlugin(rewritten, paths.nodeModules)]);
    assert.equal(rewritten.size, 1);
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});
