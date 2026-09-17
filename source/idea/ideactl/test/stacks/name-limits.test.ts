/**
 * The 64-character switch in `constructs/common.ts`.
 *
 * `Role` and `LambdaFunction` fall back to `build_trimmed_resource_name` once
 * `<cluster>-<name>[-<region>]` passes 64 characters. No captured dev27 resource is long enough to
 * exercise it (the longest live role name is 60), so the branch is pinned here against the Python
 * formula rather than against a template. A stack with a long module id or component name reaches
 * it, and a construct that silently emitted the untrimmed name would fail CloudFormation at
 * deploy time rather than at synth.
 */

import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { describe, test } from 'node:test';

import * as lambda from 'aws-cdk-lib/aws-lambda';

import { IdeaCodeAsset } from '../../src/cdk/code-asset.ts';
import { LambdaFunction, Role } from '../../src/cdk/constructs/common.ts';
import { harness } from '../support/construct-harness.ts';
import type { Json } from '../support/construct-harness.ts';

/** Long enough that `idea-dev27-<name>-us-east-2` passes 64 characters. */
const LONG = 'virtual-desktop-controller-scheduled-event-transformer-role';
const SHORT = 'vdc-scheduled-event-transformer-role';
const LONGER = `${LONG}-and-then-some-more-characters-to-cross-the-limit`;

/** `<cluster>-<name>-<region>` for SHORT. Under 64, so Role keeps this form. */
const SHORT_ROLE_NAME = 'idea-dev27-vdc-scheduled-event-transformer-role-us-east-2';
/** Python trim of LONG with the region suffix: `<cluster>-<region>-<name[:10]>-<shake256>`. */
const LONG_ROLE_NAME = 'idea-dev27-us-east-2-virtual-de-c8430fbb95f06866f7a0adac84bdf4b0';
/** `<cluster>-<name>` for SHORT. Under 64, so LambdaFunction keeps this form. */
const SHORT_FUNCTION_NAME = 'idea-dev27-vdc-scheduled-event-transformer-role';
/** Python trim of LONGER without a region suffix. */
const LONG_FUNCTION_NAME = 'idea-dev27-virtual-de-c824121e830522961abdbcccc6d28a0b696785c7af';

describe('Role name', () => {
  const roleName = (name: string): string => {
    const h = harness({ moduleId: 'vdc', moduleName: 'virtual-desktop-controller' });
    new Role(h.ctx, name, h.base.stack, { description: 'sample', assumedBy: ['lambda'] });
    const resources = h.template().Resources as Json;
    const role = Object.values(resources).find((r) => (r as Json).Type === 'AWS::IAM::Role') as Json;
    return role.Properties.RoleName as string;
  };

  test('stays untrimmed at 64 characters or fewer', () => {
    const name = roleName(SHORT);
    assert.equal(name, SHORT_ROLE_NAME);
    assert.ok(name.length <= 64, name);
  });

  test('switches to the trimmed form past 64 characters, with the region suffix', () => {
    assert.ok('idea-dev27-virtual-desktop-controller-scheduled-event-transformer-role-us-east-2'.length > 64);
    const name = roleName(LONG);
    assert.equal(name, LONG_ROLE_NAME);
    assert.equal(name.length, 64);
    assert.ok(name.startsWith('idea-dev27-us-east-2-'), name);
  });
});

describe('Lambda function name', () => {
  const functionName = (name: string): string => {
    const h = harness({ moduleId: 'vdc', moduleName: 'virtual-desktop-controller' });
    new LambdaFunction(h.ctx, name, h.base.stack, {
      code: lambda.Code.fromInline('def handler(event, context): pass'),
      handler: 'index.handler',
    });
    const resources = h.template().Resources as Json;
    const fn = Object.values(resources).find((r) => (r as Json).Type === 'AWS::Lambda::Function') as Json;
    return fn.Properties.FunctionName as string;
  };

  test('stays untrimmed at 64 characters or fewer, with no region suffix', () => {
    const name = functionName(SHORT);
    assert.equal(name, SHORT_FUNCTION_NAME);
    assert.ok(name.length <= 64, name);
  });

  test('switches to the trimmed form past 64 characters, without a region suffix', () => {
    assert.ok(`idea-dev27-${LONGER}`.length > 64);
    const name = functionName(LONGER);
    assert.equal(name, LONG_FUNCTION_NAME);
    assert.equal(name.length, 64);
    assert.ok(name.startsWith('idea-dev27-'), name);
  });
});

describe('IdeaCodeAsset packages the constructs reference', () => {
  // Every package name reached through a construct rather than through a stack. A missing package
  // is a synth-time throw, so a stack author gets a clear failure instead of a broken template.
  for (const packageName of ['idea_custom_resource_get_ad_security_group']) {
    test(packageName, () => {
      // The asset root is what `lambda.Code.fromAsset` zips, and both handlers are the Node port,
      // so the bundle has to be at the root of it for `index.handler` to resolve.
      const asset = new IdeaCodeAsset(packageName);
      assert.equal(asset.lambdaHandler, 'index.handler');
      const root = asset.assetPath();
      assert.ok(existsSync(join(root, 'index.mjs')), `${root} has no index.mjs`);
    });
  }
});
