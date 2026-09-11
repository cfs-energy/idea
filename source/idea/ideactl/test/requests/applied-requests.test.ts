/**
 * One case per cross-item change, each written so that reverting the change fails it.
 */

import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import { ClusterConfig, ConfigKeyNotFound } from '../../src/config/cluster-config.ts';
import { buildContext, loadValuesFile } from '../../src/config/values.ts';
import { jinjaEnv, renderTemplate } from '../../src/config/jinja.ts';
import { pythonJsonString } from '../../src/lambda/commons/cfn-response.ts';

const ECS_SETTINGS_TEMPLATE = fileURLToPath(
  new URL('../../resources-ecs/config/templates/ecs/settings.yml', import.meta.url),
);

/** A settings table with one module subtree, so a whole-module read has something to return. */
function config(): ClusterConfig {
  return ClusterConfig.fromFile(
    JSON.stringify({
      Items: [
        {
          key: { S: 'global-settings.module_sets.default.shared-storage.module_id' },
          value: { S: 'shared-storage' },
        },
        { key: { S: 'shared-storage.apps.provider' }, value: { S: 'efs' } },
        { key: { S: 'shared-storage.data.provider' }, value: { S: 'efs' } },
      ],
    }),
    JSON.stringify({
      Items: [
        { module_id: { S: 'shared-storage' }, name: { S: 'shared-storage' }, type: { S: 'stack' } },
      ],
    }),
  );
}

describe('a whole-module key resolves to the module subtree', () => {
  it('reads the subtree pyhocon would return for a single-segment key', () => {
    // get_real_key builds 'shared-storage.', which pyhocon's key parse reads as one element.
    assert.equal(config().getRealKey('shared-storage'), 'shared-storage.');
    assert.deepEqual(config().getConfig('shared-storage', undefined, { required: true }), {
      apps: { provider: 'efs' },
      data: { provider: 'efs' },
    });
  });

  it('still throws for a module that has no rows at all', () => {
    assert.throws(
      () => config().getConfig('analytics', undefined, { required: true }),
      ConfigKeyNotFound,
    );
  });

  it('collapses a doubled separator the way pyhocon does', () => {
    assert.equal(config().getString('shared-storage..apps..provider'), 'efs');
  });
});

describe('every typed getter takes required with no default', () => {
  const c = config();

  it('resolves the three-argument form for each getter', () => {
    // These five calls are the point of the overload: they only compile with the
    // (key, defaultValue?, options?) signature that getString already had.
    assert.throws(() => c.getInt('shared-storage.missing', undefined, { required: true }), ConfigKeyNotFound);
    assert.throws(() => c.getBool('shared-storage.missing', undefined, { required: true }), ConfigKeyNotFound);
    assert.throws(() => c.getFloat('shared-storage.missing', undefined, { required: true }), ConfigKeyNotFound);
    assert.throws(() => c.getList('shared-storage.missing', undefined, { required: true }), ConfigKeyNotFound);
    assert.throws(() => c.getString('shared-storage.missing', undefined, { required: true }), ConfigKeyNotFound);
  });

  it('leaves the one-argument and defaulted forms unchanged', () => {
    assert.equal(c.getInt('shared-storage.missing'), undefined);
    assert.equal(c.getInt('shared-storage.missing', 7), 7);
    assert.equal(c.getBool('shared-storage.missing', true), true);
    assert.deepEqual(c.getList('shared-storage.missing', ['a']), ['a']);
  });
});

describe('enable_ecs reaches the render context as a Boolean', () => {
  function contextFor(overlay: string): Record<string, unknown> {
    const dir = mkdtempSync(join(tmpdir(), 'ideactl-requests-values-'));
    const file = join(dir, 'values.yml');
    writeFileSync(
      file,
      [
        'cluster_name: idea-test1',
        'administrator_email: admin@example.invalid',
        'aws_account_id: "123456789012"',
        'aws_dns_suffix: amazonaws.com',
        'aws_partition: aws',
        'aws_region: us-east-2',
        'ssh_key_pair_name: idea-test1-key',
        'vpc_cidr_block: 203.0.113.0/24',
        'base_os: amazonlinux2023',
        overlay,
        '',
      ].join('\n'),
    );
    return buildContext(loadValuesFile(file));
  }

  it('defaults to false when the values file is silent', () => {
    assert.equal(contextFor('# no ecs key').enable_ecs, false);
  });

  it('reads a YAML 1.1 Boolean, not a string', () => {
    assert.equal(contextFor('enable_ecs: true').enable_ecs, true);
    assert.equal(contextFor('enable_ecs: on').enable_ecs, true);
    assert.equal(contextFor('enable_ecs: false').enable_ecs, false);
  });

  it('renders the shipped enabled line from it, both ways', () => {
    const template = readFileSync(ECS_SETTINGS_TEMPLATE, 'utf-8');
    const enabledLine = template
      .split('\n')
      .find((line) => line.startsWith('enabled:'));
    assert.ok(enabledLine, 'the ecs settings template has no `enabled:` line');
    const dir = mkdtempSync(join(tmpdir(), 'ideactl-requests-templates-'));
    mkdirSync(join(dir, 'ecs'), { recursive: true });
    writeFileSync(join(dir, 'ecs', 'settings.yml'), `${enabledLine}\n`);
    const env = jinjaEnv(dir);
    assert.equal(renderTemplate(env, 'ecs/settings.yml', contextFor('enable_ecs: true')), 'enabled: true');
    assert.equal(renderTemplate(env, 'ecs/settings.yml', contextFor('enable_ecs: false')), 'enabled: false');
  });

  it('the shipped template writes no account-wide trunking key', () => {
    // The account setting is a pre-flight refusal, never a rendered setting.
    assert.doesNotMatch(readFileSync(ECS_SETTINGS_TEMPLATE, 'utf-8'), /awsvpc_trunking/);
  });
});

describe('the ASCII quoter is shared, not copied', () => {
  it('quotes exactly like json.dumps(..., ensure_ascii=True)', () => {
    assert.equal(pythonJsonString('plain'), '"plain"');
    assert.equal(pythonJsonString('say "hi"'), '"say \\"hi\\""');
    assert.equal(pythonJsonString('back\\slash'), '"back\\\\slash"');
    assert.equal(pythonJsonString('tab\there'), '"tab\\there"');
    assert.equal(pythonJsonString('line\nbreak'), '"line\\nbreak"');
    assert.equal(pythonJsonString('\u0001'), '"\\u0001"');
    assert.equal(pythonJsonString('café'), '"caf\\u00e9"');
    assert.equal(pythonJsonString('☃'), '"\\u2603"');
  });

  it('is the only definition left in the two handlers that asked for it', () => {
    for (const handler of [
      '../../src/lambda/idea_solution_metrics/index.ts',
      '../../src/lambda/idea_ec2_state_event_transformation_lambda/index.ts',
    ]) {
      const text = readFileSync(fileURLToPath(new URL(handler, import.meta.url)), 'utf-8');
      assert.doesNotMatch(text, /function pythonJsonString/, `${handler} still carries a local copy`);
      assert.match(text, /pythonJsonString/, `${handler} does not use the shared quoter`);
    }
  });
});
