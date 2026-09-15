/**
 * Replacing a stateful component is a deliberate act with its own verb.
 *
 * Two things are asserted here rather than assumed. That an upgrade, which passes no allowance at
 * all, is stopped by the change-set guard when a stateful resource would be replaced. And that the
 * replace verb shows the operator what they lose before anything can happen, and will not proceed
 * on a flag they would be passing for another reason.
 *
 * The retain policy is the backstop under both, not a second gate: it never stops a deploy, it
 * decides what survives one. It covers the resources an upgrade has never replaced; the ones it
 * replaces routinely are outside the set entirely, which is asserted here rather than assumed.
 */

import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { ExitWithCode, evaluateChangeSet, type Deps } from '../../src/cli/cdk-invoker.ts';
import { REPLACEABLE_COMPONENTS, componentByName, runReplace, warningFor } from '../../src/cli/commands/replace.ts';

const change = (action: string, logicalId: string, resourceType: string, replacement?: string) => ({
  ResourceChange: { Action: action, LogicalResourceId: logicalId, ResourceType: resourceType, Replacement: replacement },
});

const changeSet = (...changes: ReturnType<typeof change>[]) => ({ Status: 'CREATE_COMPLETE', Changes: changes });

/** The upgrade path: no logical id allowed, no component named. */
const asUpgrade = (description: ReturnType<typeof changeSet>) => evaluateChangeSet(description);

describe('an upgrade never replaces a stateful resource', () => {
  test('a certain replacement is refused', () => {
    const verdict = asUpgrade(changeSet(change('Modify', 'userpool', 'AWS::Cognito::UserPool', 'True')));
    assert.equal(verdict.refusals.length, 1);
    assert.equal(verdict.refusals[0]?.refusal, 'replacement');
    assert.equal(verdict.allowed.length, 0);
  });

  test('a conditional replacement of a stateful resource is refused too', () => {
    // CloudFormation says Conditional when it will only know at execution time. On a user pool
    // that is a coin toss with every account on one side of it.
    const verdict = asUpgrade(changeSet(change('Modify', 'userpool', 'AWS::Cognito::UserPool', 'Conditional')));
    assert.equal(verdict.refusals.length, 1, 'a conditional replacement of a stateful resource must not pass');
    assert.match(verdict.refusals[0]?.reason ?? '', /Conditional/);
  });

  test('an instance is outside the protected set and its guard behaviour is unchanged', () => {
    // An upgrade replaces the scheduler and jump-host instances routinely, so they are not in the
    // protected set and nothing here treats them as stateful. What they keep is the refusal every
    // resource type has had all along: a certain replacement stops, a conditional one does not,
    // and a removal is not a stateful removal.
    assert.equal(asUpgrade(changeSet(change('Modify', 'schedulerinstance', 'AWS::EC2::Instance', 'True'))).refusals.length, 1);
    assert.deepEqual(asUpgrade(changeSet(change('Modify', 'schedulerinstance', 'AWS::EC2::Instance', 'Conditional'))).refusals, []);
    assert.deepEqual(asUpgrade(changeSet(change('Remove', 'schedulerinstance', 'AWS::EC2::Instance'))).refusals, []);
  });

  test('a conditional replacement of a stateless resource is not refused', () => {
    // Conditional is common and mostly harmless. Refusing it everywhere would teach operators to
    // pass the override without reading it.
    const verdict = asUpgrade(changeSet(change('Modify', 'launchtemplate', 'AWS::EC2::LaunchTemplate', 'Conditional')));
    assert.deepEqual(verdict.refusals, []);
  });

  test('every stateful type in the captures is refused when it would be replaced', () => {
    const types = [
      'AWS::Backup::BackupVault',
      'AWS::Cognito::UserPool',
      'AWS::DirectoryService::MicrosoftAD',
      'AWS::EFS::FileSystem',
      'AWS::Kinesis::Stream',
      'AWS::Logs::LogGroup',
      'AWS::OpenSearchService::Domain',
      'AWS::SQS::Queue',
      'AWS::SecretsManager::Secret',
    ];
    const passed = types.filter((type) => asUpgrade(changeSet(change('Modify', 'r', type, 'True'))).refusals.length === 0);
    assert.deepEqual(passed, [], `these would be replaced without a refusal: ${passed.join(', ')}`);
  });
});

describe('the replace verb allowance', () => {
  const named = new Map([['AWS::OpenSearchService::Domain', 'search-domain']]);

  test('permits the replacement of the one type the operator named', () => {
    const verdict = evaluateChangeSet(
      changeSet(change('Modify', 'analytics367A4110', 'AWS::OpenSearchService::Domain', 'True')),
      [],
      new Map(),
      named,
    );
    assert.deepEqual(verdict.refusals, []);
    assert.equal(verdict.allowed[0]?.allowedBy, 'replace search-domain');
  });

  test('does not permit a different type in the same change set', () => {
    const verdict = evaluateChangeSet(
      changeSet(
        change('Modify', 'analytics367A4110', 'AWS::OpenSearchService::Domain', 'True'),
        change('Modify', 'userpool', 'AWS::Cognito::UserPool', 'True'),
      ),
      [],
      new Map(),
      named,
    );
    assert.equal(verdict.refusals.length, 1);
    assert.equal(verdict.refusals[0]?.resourceType, 'AWS::Cognito::UserPool');
  });

  test('does not permit a removal, which is a different intent', () => {
    const verdict = evaluateChangeSet(
      changeSet(change('Remove', 'analytics367A4110', 'AWS::OpenSearchService::Domain')),
      [],
      new Map(),
      named,
    );
    assert.equal(verdict.refusals.length, 1);
    assert.equal(verdict.refusals[0]?.refusal, 'stateful-remove');
  });
});

describe('the warnings', () => {
  /**
   * The specific loss each warning has to name, written here rather than read from the source.
   * A warning that is a template with the noun swapped will not contain these.
   */
  const NAMES_ITS_OWN_LOSS: Record<string, readonly string[]> = {
    'jump-host': ['host key', 'host identity'],
    'search-domain': ['indexed', 'starts empty'],
    directory: ['domain membership', 'rejoined'],
    'user-pool': ['multi-factor', 'sign in'],
    'scheduler-host': ['batch server', 'in flight'],
    'shared-file-system': ['only copy'],
    'backup-vault': ['recovery point'],
  };

  test('every component names its own loss, not a generic one', () => {
    const generic: string[] = [];
    for (const component of REPLACEABLE_COMPONENTS) {
      const phrases = NAMES_ITS_OWN_LOSS[component.component];
      assert.ok(phrases !== undefined, `no expectation written for ${component.component}`);
      const text = component.consequence.toLowerCase();
      const missing = phrases.filter((phrase) => !text.includes(phrase));
      if (missing.length > 0) generic.push(`${component.component} does not name ${missing.join(', ')}`);
    }
    assert.deepEqual(generic, []);
  });

  test('no two components share a consequence', () => {
    const texts = REPLACEABLE_COMPONENTS.map((component) => component.consequence);
    assert.equal(new Set(texts).size, texts.length);
  });

  test('every component says which property change forces the replacement', () => {
    const silent = REPLACEABLE_COMPONENTS.filter((component) => component.forcedBy.trim() === '');
    assert.deepEqual(silent, []);
  });

  test('the warning carries the consequence, not only the resource type', () => {
    const component = componentByName('search-domain');
    assert.ok(component);
    assert.match(warningFor(component), /indexed history is destroyed/);
  });
});

/** A deps object whose first real action throws, so reaching the deploy is observable. */
function depsReachingDeploy(): { deps: Deps; out: string[]; err: string[]; reached: () => boolean } {
  const out: string[] = [];
  const err: string[] = [];
  let reached = false;
  const deps = {
    out: (line: string) => out.push(line),
    err: (line: string) => err.push(line),
    uuid: () => '00000000-0000-0000-0000-000000000000',
    scan: () => {
      reached = true;
      throw new Error('reached the deploy');
    },
  } as unknown as Deps;
  return { deps, out, err, reached: () => reached };
}

describe('the replace verb', () => {
  const options = { clusterName: 'sample-cluster', awsRegion: 'us-east-2', moduleSet: 'default' };

  test('shows the consequence and stops when the operator has not confirmed', async () => {
    const { deps, out, err, reached } = depsReachingDeploy();
    await assert.rejects(() => runReplace(deps, 'search-domain', options), ExitWithCode);
    assert.match(out.join('\n'), /indexed history is destroyed/);
    assert.match(err.join('\n'), /--confirm search-domain/);
    assert.equal(reached(), false, 'nothing may reach the deploy without a confirmation');
  });

  test('a confirmation for a different component does not proceed', async () => {
    const { deps, reached } = depsReachingDeploy();
    await assert.rejects(() => runReplace(deps, 'search-domain', { ...options, confirm: 'directory' }), ExitWithCode);
    assert.equal(reached(), false);
  });

  test('a truthy flag is not a confirmation', async () => {
    const { deps, reached } = depsReachingDeploy();
    await assert.rejects(() => runReplace(deps, 'search-domain', { ...options, confirm: 'yes' }), ExitWithCode);
    assert.equal(reached(), false);
  });

  test('the matching component name proceeds', async () => {
    const { deps, reached } = depsReachingDeploy();
    await assert.rejects(
      () => runReplace(deps, 'search-domain', { ...options, confirm: 'search-domain' }),
      /reached the deploy/,
    );
    assert.equal(reached(), true);
  });

  test('a refused component does not proceed even when confirmed', async () => {
    for (const name of ['shared-file-system', 'backup-vault']) {
      const { deps, out, err, reached } = depsReachingDeploy();
      await assert.rejects(() => runReplace(deps, name, { ...options, confirm: name }), ExitWithCode);
      assert.match(out.join('\n'), /only copy|recovery point/);
      assert.match(err.join('\n'), /cannot be replaced by this command/);
      assert.equal(reached(), false, `${name} must never reach a deploy`);
    }
  });

  test('an unknown component lists the known ones', async () => {
    const { deps, err, reached } = depsReachingDeploy();
    await assert.rejects(() => runReplace(deps, 'the-database', options), ExitWithCode);
    assert.match(err.join('\n'), /jump-host/);
    assert.equal(reached(), false);
  });
});
