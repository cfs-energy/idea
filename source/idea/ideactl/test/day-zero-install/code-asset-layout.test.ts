/**
 * A Lambda asset root has to satisfy the handler string the same object produces.
 *
 * `lambdaHandler` is `<package>.handler.handler`, so the Python runtime imports
 * `<package>.handler` from the zip root. That only resolves when the asset root contains the
 * package as a directory, alongside the shared commons package every handler imports. Returning
 * the source directory of one package instead produces a zip whose root is `handler.py`, which
 * deploys and then fails at invoke with `Runtime.ImportModuleError`. CloudFormation does not
 * surface that: the custom resource simply never answers, so the stack sits in CREATE_IN_PROGRESS
 * until it times out.
 *
 * Asserting only that `assetPath()` returns a path, or does not throw, cannot catch that.
 */

import assert from 'node:assert/strict';
import { existsSync, statSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';

import { IdeaCodeAsset, distResourcesDir } from '../../src/cdk/code-asset.ts';

const COMMONS_PACKAGE = 'idea_lambda_commons';

/** Every handler package shipped under `lambda_functions`, the commons package aside. */
function handlerPackages(): string[] {
  return [
    'idea_custom_resource_cluster_endpoints',
    'idea_custom_resource_update_cluster_prefix_list',
    'idea_custom_resource_update_cluster_settings',
    'idea_solution_metrics',
    'idea_ec2_state_event_transformation_lambda',
  ];
}

test('an asset root holds the handler package as a directory', () => {
  for (const packageName of handlerPackages()) {
    const asset = new IdeaCodeAsset(packageName);
    const root = asset.assetPath();

    const [importedPackage, importedModule] = asset.lambdaHandler.split('.');
    assert.equal(importedPackage, packageName);

    const packageDir = join(root, packageName);
    assert.equal(
      existsSync(packageDir) && statSync(packageDir).isDirectory(),
      true,
      `${packageName}: asset root ${root} has no ${packageName}/ directory, so the runtime cannot import ${asset.lambdaHandler}`,
    );
    assert.equal(
      existsSync(join(packageDir, `${importedModule}.py`)),
      true,
      `${packageName}: ${packageDir} has no ${importedModule}.py`,
    );
  }
});

test('an asset root holds the commons package every handler imports', () => {
  for (const packageName of handlerPackages()) {
    const root = new IdeaCodeAsset(packageName).assetPath();
    const commons = join(root, COMMONS_PACKAGE);
    assert.equal(
      existsSync(commons) && statSync(commons).isDirectory(),
      true,
      `${packageName}: asset root ${root} has no ${COMMONS_PACKAGE}/, so "from ${COMMONS_PACKAGE} import ..." fails at invoke`,
    );
  }
});

test('a handler with third-party dependencies has them installed in the asset root', () => {
  const packageName = 'idea_custom_resource_self_signed_certificate';
  const source = join(distResourcesDir(), 'lambda_functions', packageName);
  if (!existsSync(join(source, 'requirements.txt'))) return;

  const root = new IdeaCodeAsset(packageName).assetPath();
  assert.equal(
    existsSync(join(root, 'cryptography')),
    true,
    `${packageName}: asset root ${root} has no installed cryptography package`,
  );
});
