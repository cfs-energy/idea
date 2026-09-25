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
  retainedResources,
  CdkInvoker,
  CDK_DEPLOY_CHANGE_SET_NAME,
  ChangeSetRefused,
  evaluateChangeSet,
  isEmptyChangeSet,
  isStatefulType,
  STATEFUL_TYPE_PREFIXES,
} from '../../src/cli/cdk-invoker.ts';
import { change, fakeDeps, withTempIdeaHome, type FakeDepsOptions } from '../support/deploy-harness.ts';

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
      'AWS::EC2::VPC',
      'AWS::EC2::Subnet',
      'AWS::EC2::NatGateway',
      'AWS::EC2::RouteTable',
      'AWS::EC2::EIP',
      'AWS::EC2::InternetGateway',
      'AWS::EC2::VPCGatewayAttachment',
      'AWS::EC2::Route',
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

  it('allows Remove of a retired custom resource type, whose Delete handler is a no-op', () => {
    // The prefix-list and OAuth-credential custom resources leave every stack this release. Their
    // deployed Python handlers answer Delete with SUCCESS and change nothing, so the removal must
    // not stop an upgrade on every cluster.
    for (const [logicalId, resourceType] of [
      ['sampleclusterclusterprefixlist', 'Custom::ClusterPrefixList'],
      ['clustermanagercreds', 'Custom::GetOAuthCredentials'],
    ] as const) {
      const verdict = evaluateChangeSet({ Changes: [change('Remove', logicalId, resourceType)] });
      assert.equal(verdict.refusals.length, 0, resourceType);
      assert.equal(verdict.allowed[0]?.allowedBy, 'retired custom resource; its Delete handler is a no-op');
    }
    // Only the remove class: a replacement of one is still a replacement.
    const replaced = evaluateChangeSet({ Changes: [change('Modify', 'clustermanagercreds', 'Custom::GetOAuthCredentials', 'True')] });
    assert.equal(replaced.refusals.length, 1);
  });

  it('allows a Conditional replacement that hangs only on attributes of unreplaced resources', () => {
    // The desktop stack's SNS subscriptions take Endpoint from !GetAtt queue.Arn. Adding a Retain
    // policy modifies the subscription, and CloudFormation calls that Conditional because it cannot
    // know at plan time whether the queue's ARN changes. The same change set shows the queue is
    // modified without replacement, so the ARN, and the subscription, stay put.
    const endpoint = { Target: { Name: 'Endpoint', Attribute: 'Properties', RequiresRecreation: 'Always' }, ChangeSource: 'ResourceAttribute', CausingEntity: 'controllerqueue.Arn' };
    const policy = { Target: { Attribute: 'UpdateReplacePolicy', RequiresRecreation: 'Never' }, ChangeSource: 'DirectModification' };
    const subscription = (details: typeof endpoint[] | Array<typeof endpoint | typeof policy>) =>
      change('Modify', 'queuesubscription', 'AWS::SNS::Subscription', 'Conditional', details);

    const kept = evaluateChangeSet({ Changes: [subscription([endpoint, policy]), change('Modify', 'controllerqueue', 'AWS::SQS::Queue', 'False')] });
    assert.equal(kept.refusals.length, 0);
    assert.equal(kept.allowed[0]?.allowedBy, 'attribute of an unreplaced resource');

    const absent = evaluateChangeSet({ Changes: [subscription([endpoint, policy])] });
    assert.equal(absent.refusals.length, 0, 'a resource absent from the change set is unchanged');

    const replaced = evaluateChangeSet({ Changes: [subscription([endpoint]), change('Modify', 'controllerqueue', 'AWS::SQS::Queue', 'True')] });
    assert.ok(replaced.refusals.some((refusal) => refusal.logicalId === 'queuesubscription'), 'the causing resource is replaced');

    const added = evaluateChangeSet({ Changes: [subscription([endpoint]), change('Add', 'controllerqueue', 'AWS::SQS::Queue')] });
    assert.equal(added.refusals.length, 1, 'the causing resource is new, so its attribute is new');

    const direct = evaluateChangeSet({
      Changes: [subscription([{ Target: { Name: 'Endpoint', Attribute: 'Properties', RequiresRecreation: 'Always' }, ChangeSource: 'DirectModification' }])],
    });
    assert.equal(direct.refusals.length, 1, 'a direct edit of a recreating property');

    const bare = evaluateChangeSet({ Changes: [change('Modify', 'queuesubscription', 'AWS::SNS::Subscription', 'Conditional')] });
    assert.equal(bare.refusals.length, 1, 'no property detail means no evidence');
  });

  it('allows retained record set removal for the scheduler handover', () => {
    const changes = [change('Remove', 'schedulerdnsrecord', 'AWS::Route53::RecordSet')];
    const verdict = evaluateChangeSet({ Changes: changes }, [], new Map(), new Map(), new Set(['schedulerdnsrecord']));
    assert.equal(verdict.refusals.length, 0);
    assert.equal(verdict.allowed.length, 1);
    assert.equal(evaluateChangeSet({ Changes: changes }).refusals.length, 1);
  });

  for (const type of ['AWS::EFS::FileSystem', 'Custom::Something']) {
    it(`refuses retained ${type} removal without an explicit override`, () => {
      const changes = [change('Remove', 'old', type), change('Add', 'renamed', type)];
      const retained = new Set(['old']);
      const verdict = evaluateChangeSet({ Changes: changes }, [], new Map(), new Map(), retained);
      assert.equal(verdict.refusals.length, 1);
      assert.equal(verdict.refusals[0]?.logicalId, 'old');
      const override = evaluateChangeSet({ Changes: changes }, ['old'], new Map(), new Map(), retained);
      assert.equal(override.refusals.length, 0);
      assert.equal(override.allowed[0]?.allowedBy, '--allow-replacement');
    });
  }

  it('reads retained logical IDs from a deployed template and nothing from an unparsable one', () => {
    const template = JSON.stringify({ Resources: { a: { Type: 'X', DeletionPolicy: 'Retain' }, b: { Type: 'X', DeletionPolicy: 'Delete' }, c: { Type: 'X' } } });
    assert.deepEqual([...retainedResources(template)], ['a']);
    assert.deepEqual([...retainedResources('not json')], []);
    assert.deepEqual([...retainedResources(undefined)], []);
  });

  it('allows replacing a task definition, which is how an image upgrade rolls', () => {
    // A new image is a new task definition revision; CloudFormation calls that a replacement, the
    // service rolls to it and the previous revision stays ACTIVE under Retain.
    const verdict = evaluateChangeSet({ Changes: [change('Modify', 'schedulertaskdefinition', 'AWS::ECS::TaskDefinition', 'True')] });
    assert.equal(verdict.refusals.length, 0);
    assert.equal(verdict.allowed[0]?.allowedBy, 'a new revision; the previous one is retained');
    const other = evaluateChangeSet({ Changes: [change('Modify', 'schedulerservice', 'AWS::ECS::Service', 'True')] });
    assert.equal(other.refusals.length, 1, 'only the revisioned type');
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
    for (const resourceType of ['AWS::EC2::Instance', 'AWS::EC2::NetworkInterface', 'AWS::EC2::SecurityGroup']) {
      assert.equal(isStatefulType(resourceType), false, resourceType);
    }
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

  const invokerFor = (options: FakeDepsOptions, allowReplacement: string[] = [], pollIntervalMs?: number) => {
    const deps = fakeDeps(options);
    const invoker = new CdkInvoker({
      clusterName: CLUSTER,
      awsRegion: 'us-east-2',
      moduleId: 'analytics',
      moduleName: 'analytics',
      moduleSet: 'default',
      deploymentId: 'deployment-1',
      allowReplacement,
      pollIntervalMs,
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

  it('executes a change set that only updates cluster settings: that resource stamps the module version', async () => {
    const { deps, invoker } = invokerFor({
      changeSet: {
        Status: 'CREATE_COMPLETE',
        Changes: [change('Modify', 'analyticsclustersettings', 'Custom::ClusterSettings', 'False')],
      },
    });
    const verdict = await invoker.deployThroughChangeSet();
    assert.equal(verdict.empty, false);
    assert.equal(deps.executed.length, 1);
  });

  it('executes a change set that updates cluster settings and another resource', async () => {
    const { deps, invoker } = invokerFor({
      changeSet: {
        Status: 'CREATE_COMPLETE',
        Changes: [
          change('Modify', 'analyticsclustersettings', 'Custom::ClusterSettings', 'False'),
          change('Modify', 'analyticsfunction', 'AWS::Lambda::Function', 'False'),
        ],
      },
    });
    await invoker.deployThroughChangeSet();
    assert.equal(deps.executed.length, 1);
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

  // A replaced instance's old copy is deleted in the update's cleanup, and a termination-protected
  // one fails that delete while the stack still reports UPDATE_COMPLETE: the instance runs on,
  // unreferenced and billing. dev27's bastion did exactly this on 2026-09-15.
  it('clears termination protection on a protected instance an accepted replacement deletes, before executing', async () => {
    const replaced = change('Modify', 'clusteropenldapinstance', 'AWS::EC2::Instance', 'True');
    const { deps, invoker } = invokerFor(
      { changeSet: { Status: 'CREATE_COMPLETE', Changes: [{ ResourceChange: { ...replaced.ResourceChange, PhysicalResourceId: 'i-old' } }] } },
      ['clusteropenldapinstance'],
    );
    const calls: string[] = [];
    deps.instanceProtection = {
      async isProtected(input) { return input.instanceId === 'i-old'; },
      async setProtected(input) { calls.push(`${input.instanceId}:${input.protected}:executed=${deps.executed.length}`); },
    };
    await invoker.deployThroughChangeSet();
    assert.deepEqual(calls, ['i-old:false:executed=0']);
    assert.equal(deps.executed.length, 1);
    assert.match(deps.stdout.join('\n'), /cleared instance termination protection on i-old \(clusteropenldapinstance\)/);
  });

  it('leaves an unprotected replaced instance alone and warns when it cannot check', async () => {
    const replaced = change('Modify', 'clusteropenldapinstance', 'AWS::EC2::Instance', 'True');
    const changeSet = { Status: 'CREATE_COMPLETE', Changes: [{ ResourceChange: { ...replaced.ResourceChange, PhysicalResourceId: 'i-old' } }] };
    const unprotected = invokerFor({ changeSet }, ['clusteropenldapinstance']);
    const calls: string[] = [];
    unprotected.deps.instanceProtection = {
      async isProtected() { return false; },
      async setProtected(input) { calls.push(input.instanceId); },
    };
    await unprotected.invoker.deployThroughChangeSet();
    assert.deepEqual(calls, []);
    const blind = invokerFor({ changeSet }, ['clusteropenldapinstance']);
    delete blind.deps.instanceProtection;
    await blind.invoker.deployThroughChangeSet();
    assert.match(blind.deps.stdout.join('\n'), /warning: i-old \(clusteropenldapinstance\) is being replaced; if it is termination-protected/);
    assert.equal(blind.deps.executed.length, 1);
  });

  it('fails when the executed change set leaves the stack in a rollback state', async () => {
    const { invoker } = invokerFor({
      changeSet: BENIGN_CHANGE_SET,
      stack: { StackStatus: 'UPDATE_ROLLBACK_COMPLETE', StackStatusReason: 'resource failed' },
    });
    await assert.rejects(() => invoker.deployThroughChangeSet(), /UPDATE_ROLLBACK_COMPLETE/);
  });

  it('waits through update cleanup until the stack completes', async () => {
    const { deps, invoker } = invokerFor({ changeSet: BENIGN_CHANGE_SET });
    const statuses = ['UPDATE_COMPLETE_CLEANUP_IN_PROGRESS', 'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS', 'UPDATE_COMPLETE'];
    deps.cfn.describeStack = async () => ({ StackStatus: statuses.shift() });
    await invoker.deployThroughChangeSet();
    assert.deepEqual(deps.sleeps, [15_000, 15_000]);
  });

  it('stops waiting for an in-progress stack after four hours', async () => {
    const { deps, invoker } = invokerFor(
      { changeSet: BENIGN_CHANGE_SET, stack: { StackStatus: 'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS' } },
      [],
      60 * 60_000,
    );
    await assert.rejects(
      () => invoker.deployThroughChangeSet(),
      /still UPDATE_COMPLETE_CLEANUP_IN_PROGRESS after 240 minutes/,
    );
    assert.deepEqual(deps.sleeps, [60 * 60_000, 60 * 60_000, 60 * 60_000, 60 * 60_000]);
  });
});
