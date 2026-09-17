/**
 * A Lambda asset root has to satisfy the handler string the same object produces.
 *
 * Every handler is a Node handler, so `lambdaHandler` is `index.handler` and the asset root has to
 * hold `index.mjs` at its top level. An asset root that does not produces a zip that deploys and
 * then fails at invoke with `Runtime.ImportModuleError`.
 *
 * CloudFormation does not surface that: the custom resource simply never answers, so the stack
 * sits in CREATE_IN_PROGRESS until it times out. Asserting only that `assetPath()` returns a path,
 * or does not throw, cannot catch it.
 */

import assert from 'node:assert/strict';
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';

import { IdeaCodeAsset, nodeLambdaPackages, distResourcesDir } from '../../src/cdk/code-asset.ts';

test('a Node asset root holds the bundle index.handler names', () => {
  const packages = nodeLambdaPackages();
  assert.ok(packages.length > 0, 'no handler packages found under src/lambda');

  for (const packageName of packages) {
    const asset = new IdeaCodeAsset(packageName);
    assert.equal(asset.lambdaHandler, 'index.handler');
    assert.equal(asset.runtime.name, 'nodejs22.x');

    const root = asset.assetPath();
    assert.equal(
      existsSync(join(root, 'index.mjs')),
      true,
      `${packageName}: asset root ${root} has no index.mjs, so the runtime cannot import index.handler`,
    );
    assert.equal(
      existsSync(join(root, packageName)),
      false,
      `${packageName}: asset root ${root} still holds a Python package directory`,
    );
  }
});

test('a package with no handler source is an error', () => {
  assert.throws(
    () => new IdeaCodeAsset('idea_no_such_handler_package'),
    /lambda package not found: idea_no_such_handler_package/,
  );
});


test('a validated prebuilt bundle works without handler sources', () => {
  const name = 'release_test_handler';
  const root = join(distResourcesDir(), 'lambda_assets', name);
  mkdirSync(root, { recursive: true });
  try {
    assert.throws(() => new IdeaCodeAsset(name), /invalid prebuilt lambda bundle/);
    writeFileSync(join(root, 'index.mjs'), '');
    assert.throws(() => new IdeaCodeAsset(name), /invalid prebuilt lambda bundle/);
    rmSync(join(root, 'index.mjs'));
    mkdirSync(join(root, 'index.mjs'));
    assert.throws(() => new IdeaCodeAsset(name), /invalid prebuilt lambda bundle/);
    rmSync(join(root, 'index.mjs'), { recursive: true });
    writeFileSync(join(root, 'index.mjs'), 'export const handler = async () => {};');
    const asset = new IdeaCodeAsset(name);
    assert.equal(asset.assetPath(), root);
    rmSync(join(root, 'index.mjs'));
    assert.throws(() => asset.assetPath(), /invalid prebuilt lambda bundle/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('package names cannot escape the asset root', () => {
  for (const name of ['', '..', '../other', '/tmp/handler', 'a/b']) {
    assert.throws(() => new IdeaCodeAsset(name), /invalid lambda package name/);
  }
});
