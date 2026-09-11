/**
 * The deploy-time change-set guard.
 *
 * One fixture per refusal class, plus a benign in-place update that must be allowed through.
 * Each refusal test asserts both halves: the deploy throws, AND `executeChangeSet` was never
 * called. Deleting any one guard clause fails at least one of these.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, describe, it } from 'node:test';

import {
  builtInAllowedReplacements,
  CdkInvoker,
  CDK_DEPLOY_CHANGE_SET_NAME,
  ChangeSetRefused,
  evaluateChangeSet,
  isEmptyChangeSet,
  isStatefulType,
  STATEFUL_TYPE_PREFIXES,
} from '../../src/cli/cdk-invoker.ts';
import { change, fakeDeps, withTempIdeaHome, type FakeDepsOptions } from './harness.ts';

const CLUSTER = 'sample-cluster';

// -------------------------------------------------------------------------------------------
// the four fixtures
// -------------------------------------------------------------------------------------------

/** Refusal class 1: any change that replaces the resource. */
export const REPLACEMENT_CHANGE_SET = {
  Status: 'CREATE_COMPLETE',
  Changes: [change('Modify', 'clusteropenldapinstance', 'AWS::EC2::Instance', 'True')],
};

/** Refusal class 2: removing a custom resource, which runs its Delete handler. */
export const CUSTOM_RESOURCE_REMOVE_CHANGE_SET = {
  Status: 'CREATE_COMPLETE',
  Changes: [change('Remove', 'analyticsclustersettings', 'Custom::ClusterSettings')],
};

/** Refusal class 3: removing a resource whose state cannot be rebuilt from the template. */
export const STATEFUL_REMOVE_CHANGE_SET = {
  Status: 'CREATE_COMPLETE',
  Changes: [change('Remove', 'useridentityuserpool', 'AWS::Cognito::UserPool')],
};

/** The control: an in-place property update on a stateless resource. */
export const BENIGN_CHANGE_SET = {
  Status: 'CREATE_COMPLETE',
  Changes: [
    change('Modify', 'clustermanagerasg', 'AWS::AutoScaling::AutoScalingGroup', 'False'),
    change('Add', 'clustermanagerlogs', 'AWS::Logs::LogGroup'),
    change('Modify', 'schedulerlaunchtemplate', 'AWS::EC2::LaunchTemplate', 'Conditional'),
  ],
};

// -------------------------------------------------------------------------------------------
// the classifier
// -------------------------------------------------------------------------------------------

describe('evaluateChangeSet', () => {
  it('refuses a Replacement: True change', () => {
    const verdict = evaluateChangeSet(REPLACEMENT_CHANGE_SET);
    assert.equal(verdict.refusals.length, 1);
    assert.equal(verdict.refusals[0]?.refusal, 'replacement');
    assert.equal(verdict.refusals[0]?.logicalId, 'clusteropenldapinstance');
  });

  it('refuses Remove on a Custom:: resource', () => {
    const verdict = evaluateChangeSet(CUSTOM_RESOURCE_REMOVE_CHANGE_SET);
    assert.equal(verdict.refusals.length, 1);
    assert.equal(verdict.refusals[0]?.refusal, 'custom-resource-remove');
  });

  it('refuses Remove on every stateful type in the list', () => {
    const samples = [
      'AWS::Cognito::UserPool',
      'AWS::DirectoryService::MicrosoftAD',
      'AWS::EFS::FileSystem',
      'AWS::FSx::FileSystem',
      'AWS::OpenSearchService::Domain',
      'AWS::Elasticsearch::Domain',
      'AWS::Kinesis::Stream',
      'AWS::KinesisFirehose::DeliveryStream',
      'AWS::SecretsManager::Secret',
      'AWS::Route53::RecordSet',
      'AWS::SQS::Queue',
      'AWS::SNS::Topic',
      'AWS::Logs::LogGroup',
    ];
    for (const resourceType of samples) {
      const verdict = evaluateChangeSet({ Changes: [change('Remove', 'someresource', resourceType)] });
      assert.equal(verdict.refusals.length, 1, `${resourceType} was not refused`);
      assert.equal(verdict.refusals[0]?.refusal, 'stateful-remove');
    }
  });

  it('every stateful service has a prefix in the list', () => {
    for (const service of [
      'Cognito',
      'DirectoryService',
      'EFS',
      'FSx',
      'OpenSearch',
      'Kinesis',
      'SecretsManager',
      'Route53',
      'SQS',
      'SNS',
      'Logs',
    ]) {
      assert.ok(
        STATEFUL_TYPE_PREFIXES.some((prefix) => prefix.includes(service)),
        `no stateful prefix covers ${service}`,
      );
    }
  });

  it('lets a benign in-place update through', () => {
    const verdict = evaluateChangeSet(BENIGN_CHANGE_SET);
    assert.deepEqual(verdict.refusals, []);
    assert.deepEqual(verdict.allowed, []);
    assert.equal(verdict.empty, false);
  });

  it('does not refuse an Add of a stateful type, only a Remove', () => {
    const verdict = evaluateChangeSet({ Changes: [change('Add', 'newpool', 'AWS::Cognito::UserPool')] });
    assert.deepEqual(verdict.refusals, []);
  });

  it('an explicit --allow-replacement moves a refusal to the printed allow list', () => {
    const verdict = evaluateChangeSet(REPLACEMENT_CHANGE_SET, ['clusteropenldapinstance']);
    assert.deepEqual(verdict.refusals, []);
    assert.equal(verdict.allowed.length, 1);
    assert.equal(verdict.allowed[0]?.allowedBy, '--allow-replacement');
  });

  it('an override for a different logical ID does not help', () => {
    const verdict = evaluateChangeSet(REPLACEMENT_CHANGE_SET, ['someotherresource']);
    assert.equal(verdict.refusals.length, 1);
  });

  it('reports each refusal class a single change trips', () => {
    const verdict = evaluateChangeSet({
      Changes: [change('Remove', 'analyticssink', 'Custom::AnalyticsSink', 'True')],
    });
    assert.deepEqual(
      verdict.refusals.map((refusal) => refusal.refusal).sort(),
      ['custom-resource-remove', 'replacement'],
    );
  });

  it('reads a change set that spans pages only through the invoker, never truncated here', () => {
    const verdict = evaluateChangeSet({
      Changes: [
        change('Modify', 'a', 'AWS::EC2::Instance', 'False'),
        change('Remove', 'b', 'AWS::EFS::FileSystem'),
      ],
    });
    assert.equal(verdict.refusals.length, 1);
    assert.equal(verdict.refusals[0]?.logicalId, 'b');
  });
});

describe('the built-in allow list', () => {
  it('covers the analytics dashboard target group, whose Name changes on every synth', () => {
    const allowed = builtInAllowedReplacements('idea-dev27');
    assert.equal(allowed.get('ideadev27dashboardtargetgroup'), 'AWS::ElasticLoadBalancingV2::TargetGroup');
  });

  it('is scoped to that resource type, so a different resource with the same id is still refused', () => {
    const builtIn = builtInAllowedReplacements(CLUSTER);
    const verdict = evaluateChangeSet(
      { Changes: [change('Modify', 'sampleclusterdashboardtargetgroup', 'AWS::Cognito::UserPool', 'True')] },
      [],
      builtIn,
    );
    assert.equal(verdict.refusals.length, 1);
  });

  it('holds exactly one entry, so it cannot quietly grow into a bypass', () => {
    assert.equal(builtInAllowedReplacements(CLUSTER).size, 1);
  });
});

describe('isEmptyChangeSet', () => {
  it('recognises the no-changes failure CloudFormation reports', () => {
    assert.equal(
      isEmptyChangeSet({
        Status: 'FAILED',
        StatusReason: "The submitted information didn't contain changes. Submit different information to create a change set.",
      }),
      true,
    );
  });

  it('does not treat a real failure as empty', () => {
    assert.equal(isEmptyChangeSet({ Status: 'FAILED', StatusReason: 'Insufficient permissions' }), false);
  });
});

describe('isStatefulType', () => {
  it('is false for an undefined type and for a stateless one', () => {
    assert.equal(isStatefulType(undefined), false);
    assert.equal(isStatefulType('AWS::AutoScaling::AutoScalingGroup'), false);
    // An instance is replaced by an ordinary upgrade and is deliberately not in the set.
    assert.equal(isStatefulType('AWS::EC2::Instance'), false);
  });
});

// -------------------------------------------------------------------------------------------
// the enforcement path
// -------------------------------------------------------------------------------------------

describe('CdkInvoker.deployThroughChangeSet', () => {
  const home = withTempIdeaHome();
  const previousCdkBin = process.env.IDEA_CDK_BIN;
  before(() => {
    process.env.IDEA_CDK_BIN = '/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk';
  });
  after(() => {
    if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
    else process.env.IDEA_CDK_BIN = previousCdkBin;
    home.restore();
  });

  const invokerFor = (options: FakeDepsOptions, allowReplacement: string[] = []) => {
    const deps = fakeDeps(options);
    const invoker = new CdkInvoker({
      clusterName: CLUSTER,
      awsRegion: 'us-east-2',
      moduleId: 'analytics',
      moduleName: 'analytics',
      moduleSet: 'default',
      deploymentId: 'deployment-1',
      allowReplacement,
      deps,
    });
    return { deps, invoker };
  };

  // The guard reads every page of the change set, and nothing exercised that loop. A break there
  // would inspect page one and execute, so a replacement announced on a later page would go
  // through silently, which is the single outcome this guard exists to prevent. Modelled as two
  // pages with the benign change first, so a loop that stops early sees nothing to refuse.
  it('refuses a replacement announced only on a later page of the change set', async () => {
    const { deps, invoker } = invokerFor({
      changeSetPages: [
        { Status: 'CREATE_COMPLETE', Changes: BENIGN_CHANGE_SET.Changes, NextToken: '1' },
        { Status: 'CREATE_COMPLETE', Changes: REPLACEMENT_CHANGE_SET.Changes },
      ],
    });
    await assert.rejects(() => invoker.deployThroughChangeSet(), /refused 1 change\(s\)/);
    // The offending resource is named in the printed detail, which is what an operator acts on.
    assert.match(deps.stdout.join('\n') + deps.stderr.join('\n'), /clusteropenldapinstance/);
    assert.deepEqual(deps.executed, [], 'nothing may execute after a refusal');
    // Both pages were actually fetched, so the refusal came from following the token rather than
    // from the first page happening to contain the offending change.
    assert.deepEqual(deps.describeChangeSetCalls, [undefined, '1']);
  });

  it('follows every page before executing a benign change set', async () => {
    const { deps, invoker } = invokerFor({
      changeSetPages: [
        { Status: 'CREATE_COMPLETE', Changes: BENIGN_CHANGE_SET.Changes, NextToken: '1' },
        { Status: 'CREATE_COMPLETE', Changes: BENIGN_CHANGE_SET.Changes, NextToken: '2' },
        { Status: 'CREATE_COMPLETE', Changes: BENIGN_CHANGE_SET.Changes },
      ],
    });
    await invoker.deployThroughChangeSet();
    assert.deepEqual(deps.describeChangeSetCalls, [undefined, '1', '2']);
    assert.equal(deps.executed.length, 1);
  });

  // An empty-string token is the shape a provider can hand back for "no more pages". Treating it
  // as a real token asks for a page that does not exist and hangs or throws.
  it('treats an empty continuation token as the end of the change set', async () => {
    const { deps, invoker } = invokerFor({
      changeSetPages: [{ Status: 'CREATE_COMPLETE', Changes: BENIGN_CHANGE_SET.Changes, NextToken: '' }],
    });
    await invoker.deployThroughChangeSet();
    assert.deepEqual(deps.describeChangeSetCalls, [undefined]);
    assert.equal(deps.executed.length, 1);
  });

  it('always creates the change set without executing it', async () => {
    const { deps, invoker } = invokerFor({ changeSet: BENIGN_CHANGE_SET });
    await invoker.deployThroughChangeSet();
    const argv = deps.spawns[0] ?? [];
    // The toolkit rejects the deprecated no-execute flag beside a method; this method is the one
    // that creates the change set and stops, which is what the guard inspects before executing.
    assert.ok(
      argv.includes('--method=prepare-change-set'),
      'deploy argv is missing --method=prepare-change-set',
    );
    assert.ok(!argv.includes('--no-execute'), 'deploy argv still carries the rejected no-execute flag');
  });

  for (const [name, changeSet] of [
    ['replacement', REPLACEMENT_CHANGE_SET],
    ['custom-resource remove', CUSTOM_RESOURCE_REMOVE_CHANGE_SET],
    ['stateful remove', STATEFUL_REMOVE_CHANGE_SET],
  ] as const) {
    it(`refuses to execute a ${name} change set`, async () => {
      const { deps, invoker } = invokerFor({ changeSet });
      await assert.rejects(() => invoker.deployThroughChangeSet(), ChangeSetRefused);
      assert.deepEqual(deps.executed, [], 'the change set was executed despite the refusal');
      assert.ok(
        deps.stderr.some((line) => line.includes('REFUSING to execute change set')),
        'the refusal was not printed',
      );
    });
  }

  it('executes a benign change set and writes the outputs file', async () => {
    const { deps, invoker } = invokerFor({
      changeSet: BENIGN_CHANGE_SET,
      stack: {
        StackStatus: 'UPDATE_COMPLETE',
        Outputs: [{ OutputKey: 'ClusterName', OutputValue: CLUSTER }],
      },
    });
    await invoker.deployThroughChangeSet();
    assert.deepEqual(deps.executed, [
      // Rollback stays on unless the caller asks for it off, so an interrupted update reverses.
      { StackName: `${CLUSTER}-analytics`, ChangeSetName: CDK_DEPLOY_CHANGE_SET_NAME, DisableRollback: false },
    ]);
    const outputs = JSON.parse(readFileSync(invoker.outputsFile(), 'utf-8')) as Record<string, unknown>;
    assert.deepEqual(outputs, { [`${CLUSTER}-analytics`]: { ClusterName: CLUSTER } });
  });

  it('prints every override it honours', async () => {
    const { deps, invoker } = invokerFor({ changeSet: REPLACEMENT_CHANGE_SET }, ['clusteropenldapinstance']);
    await invoker.deployThroughChangeSet();
    assert.ok(
      deps.stdout.some((line) => line.includes('ALLOWED by --allow-replacement') && line.includes('clusteropenldapinstance')),
      'the override was not printed',
    );
    assert.equal(deps.executed.length, 1);
  });

  it('does not execute an empty change set', async () => {
    const { deps, invoker } = invokerFor({
      changeSet: { Status: 'FAILED', StatusReason: "The submitted information didn't contain changes." },
    });
    await invoker.deployThroughChangeSet();
    assert.deepEqual(deps.executed, []);
    assert.ok(deps.stdout.some((line) => line.endsWith('no changes')));
  });

  it('fails without executing when the change set could not be created', async () => {
    const deps = fakeDeps({ spawnExitCodes: [1] });
    const invoker = new CdkInvoker({
      clusterName: CLUSTER,
      awsRegion: 'us-east-2',
      moduleId: 'analytics',
      moduleName: 'analytics',
      moduleSet: 'default',
      deploymentId: 'deployment-1',
      deps,
    });
    await assert.rejects(() => invoker.deployThroughChangeSet());
    assert.deepEqual(deps.executed, []);
  });

  it('fails when the executed change set leaves the stack in a rollback state', async () => {
    const { invoker } = invokerFor({
      changeSet: BENIGN_CHANGE_SET,
      stack: { StackStatus: 'UPDATE_ROLLBACK_COMPLETE', StackStatusReason: 'resource failed' },
    });
    await assert.rejects(() => invoker.deployThroughChangeSet(), /UPDATE_ROLLBACK_COMPLETE/);
  });
});
