/** Compare rendered policy documents with captured templates. */

import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import { Fn, Stack } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { load } from 'js-yaml';

import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { renderPolicy, resourcesDir } from '../../src/cdk/policy.ts';
import { requireCapture } from '../support/fixtures.ts';

const RAW = fileURLToPath(new URL('../../tools/parity/fixtures/idea-dev27/raw/', import.meta.url));
const LIVE = fileURLToPath(new URL('../../tools/parity/live/', import.meta.url));
const SCAN = `${RAW}cluster-settings.scan.json`;
const MODULES = `${RAW}modules.scan.json`;
const CLUSTERS = join(homedir(), '.idea', 'clusters');
const STACKS = ['analytics', 'bastion-host', 'bootstrap', 'cluster', 'cluster-manager', 'directoryservice', 'identity-provider', 'metrics', 'scheduler', 'shared-storage', 'vdc'];
const missingDev27Templates = STACKS.filter((stack) => !existsSync(`${LIVE}idea-dev27-${stack}.json`));
requireCapture(
  [
    SCAN,
    MODULES,
    ...STACKS.map((stack) => `${LIVE}idea-dev27-${stack}.json`),
    CLUSTERS,
  ],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

// --- attribution ------------------------------------------------------------------------------

/** Classify captured policy resources by their template or generated source. */
interface Attribution {
  stack: string;
  /** Construct path after the stack segment; bootstrap resources do not carry metadata. */
  path?: string;
  /** Dynamic construct path for resources whose generated name changes between synths. */
  pathPattern?: RegExp;
  /** Bootstrap resource logical ID, used only when construct metadata is absent. */
  resourceId?: string;
  /** Logical id in the captured dev27 templates; absent iff dev27 does not have the feature on. */
  dev27Id?: string;
  /** Policy Jinja template. Exactly one of `template` / `generatedBy` is set. */
  template?: string;
  /** The CDK L2 grant that writes this document instead of a policy template. */
  generatedBy?: string;
  moduleOf?: string;
  vars?: 'scheduler-roles' | 'component-role';
  /** Absent from a cluster that does not turn the feature on. */
  conditional?: boolean;
  /**
   * The captured templates still carry this policy and this branch no longer creates it. The entry
   * stays so the captured resource is still classified; there is no template left to render it
   * from, so the rendering comparison skips it.
   */
  retired?: string;
}

const ATTRIBUTED: Attribution[] = [
  // analytics_stack.py:208, :438
  { stack: 'analytics', path: 'analytics-sink-lambda-policy/Resource', dev27Id: 'analyticssinklambdapolicyD33AD925', template: 'analytics-sink-lambda.yml' },
  { stack: 'analytics', path: 'opensearch-private-ips-lambda-policy/Resource', dev27Id: 'opensearchprivateipslambdapolicy2C1A5D98', template: 'custom-resource-opensearch-private-ips.yml' },
  {
    stack: 'analytics',
    pathPattern: /^analytics\/ESLogGroupPolicy[0-9a-f]+\/CustomResourcePolicy\/Resource$/,
    dev27Id: 'analyticsESLogGroupPolicyc8803cad36e6f020d595fd57136b9a57c0a2206865CustomResourcePolicy7D66AE30',
    generatedBy: 'opensearch.Domain logging options (analytics_stack.py:358) -> LogGroupResourcePolicy AwsCustomResource',
  },
  {
    stack: 'analytics',
    path: 'analytics/AccessPolicy/CustomResourcePolicy/Resource',
    dev27Id: 'analyticsAccessPolicyCustomResourcePolicy9ECE5CA0',
    generatedBy: 'opensearch.Domain access_policies (constructs/analytics.py:115-123) -> OpenSearchAccessPolicy AwsCustomResource',
  },
  {
    stack: 'analytics',
    path: 'analytics-sink-lambda-role/DefaultPolicy/Resource',
    dev27Id: 'analyticssinklambdaroleDefaultPolicy74409E1C',
    generatedBy: 'lambda.add_event_source(KinesisEventSource) (analytics_stack.py:271) grants read on the stream',
  },
  // bastion_host_stack.py:96
  { stack: 'bastion-host', path: 'bastion-host-policy/Resource', dev27Id: 'bastionhostpolicy211766EC', template: 'bastion-host.yml' },
  // cluster_manager_stack.py:225, :245, :261, :306, :331 (the last four only when bedrock is on)
  { stack: 'cluster-manager', path: 'cluster-manager-policy/Resource', dev27Id: 'clustermanagerpolicyEF9BA73D', template: 'cluster-manager.yml', moduleOf: 'cluster-manager' },
  { stack: 'cluster-manager', path: 'project-role-boundary/Resource', dev27Id: 'projectroleboundaryD13C6967', template: 'project-role-boundary.yml', moduleOf: 'cluster-manager', conditional: true },
  { stack: 'cluster-manager', path: 'detach-project-boundaries-lambda-policy/Resource', dev27Id: 'detachprojectboundarieslambdapolicy941B0FC6', template: 'custom-resource-detach-project-boundaries.yml', conditional: true },
  { stack: 'cluster-manager', path: 'ensure-bedrock-log-group-lambda-policy/Resource', dev27Id: 'ensurebedrockloggrouplambdapolicy4449C6A0', template: 'custom-resource-ensure-log-group.yml', conditional: true },
  { stack: 'cluster-manager', path: 'bedrock-invocation-logging-policy/Resource', dev27Id: 'bedrockinvocationloggingpolicyD7BA525E', template: 'bedrock-invocation-logging.yml', moduleOf: 'cluster-manager', conditional: true },
  // cluster_stack.py:241, :250, :263, :272 (only when cluster.backups.enabled; dev27 has it off)
  { stack: 'cluster', path: 'backup-create-policy/Resource', template: 'backup-create.yml', conditional: true },
  { stack: 'cluster', path: 'backup-s3-create-policy/Resource', template: 'backup-s3-create.yml', conditional: true },
  { stack: 'cluster', path: 'backup-restore-policy/Resource', template: 'backup-restore.yml', conditional: true },
  { stack: 'cluster', path: 'backup-s3-restore-policy/Resource', template: 'backup-s3-restore.yml', conditional: true },
  // cluster_stack.py:344, :353, :378, :445, :645, :693, :736, :919, :974
  { stack: 'cluster', path: 'amazon-ssm-managed-instance-core/Resource', dev27Id: 'amazonssmmanagedinstancecore26547C9F', template: 'amazon-ssm-managed-instance-core.yml' },
  { stack: 'cluster', path: 'cloud-watch-agent-server-policy/Resource', dev27Id: 'cloudwatchagentserverpolicy953A2E64', template: 'cloud-watch-agent-server-policy.yml' },
  { stack: 'cluster', path: 'LogRetention/Resource', dev27Id: 'LogRetentionDD0A1FA1', template: 'log-retention.yml' },
  { stack: 'cluster', path: 'update-cluster-prefix-list-policy/Resource', dev27Id: 'updateclusterprefixlistpolicy57E07488', template: 'custom-resource-update-cluster-prefix-list.yml', conditional: true, retired: 'the deploy tool merges the client addresses after the stack deploys' },
  { stack: 'cluster', path: 'cluster-settings-policy/Resource', dev27Id: 'clustersettingspolicyCB373F35', template: 'custom-resource-update-cluster-settings.yml' },
  { stack: 'cluster', path: 'solution-metrics-policy/Resource', dev27Id: 'solutionmetricspolicyCF61BD01', template: 'solution-metrics-lambda-function.yml' },
  { stack: 'cluster', path: 'self-signed-certificate-policy/Resource', dev27Id: 'selfsignedcertificatepolicyADC88D10', template: 'custom-resource-self-signed-certificate.yml' },
  { stack: 'cluster', path: 'cluster-ec2state-event-transformer-policy/Resource', dev27Id: 'clusterec2stateeventtransformerpolicy0DD48CD6', template: 'ec2state-event-transformer.yml' },
  { stack: 'cluster', path: 'cluster-endpoints-policy/Resource', dev27Id: 'clusterendpointspolicy8641A368', template: 'custom-resource-cluster-endpoints.yml' },
  {
    stack: 'cluster',
    path: 'vpc-flow-logs-role/DefaultPolicy/Resource',
    dev27Id: 'vpcflowlogsroleDefaultPolicyAAD1B3D4',
    generatedBy: 'ec2.FlowLogDestination.to_cloud_watch_logs (constructs/network.py:110) grants the role write on the log group',
  },
  // constructs/directory_service.py:298
  { stack: 'directoryservice', path: 'get-ad-security-group-id-lambda-policy/Resource', dev27Id: 'getadsecuritygroupidlambdapolicyC8292AF8', template: 'custom-resource-get-ad-security-group.yml' },
  // identity_provider_stack.py:177, :242
  { stack: 'identity-provider', path: 'id-token-claim-policy/Resource', dev27Id: 'idtokenclaimpolicy3D23F897', template: 'custom_resource_sso_claim_modifier.yml' },
  { stack: 'identity-provider', path: 'oauth-credentials-policy/Resource', dev27Id: 'oauthcredentialspolicy92D4C6F2', template: 'custom-resource-get-user-pool-client-secret.yml', retired: 'a user pool client returns its own generated secret' },
  // scheduler_stack.py:238, :248, :258
  { stack: 'scheduler', path: 'scheduler-policy/Resource', dev27Id: 'schedulerpolicyFF65A604', template: 'scheduler.yml', moduleOf: 'scheduler', vars: 'scheduler-roles' },
  { stack: 'scheduler', path: 'compute-node-policy/Resource', dev27Id: 'computenodepolicyBA9B1B50', template: 'compute-node.yml', moduleOf: 'scheduler', vars: 'scheduler-roles' },
  { stack: 'scheduler', path: 'spot-fleet-policy/Resource', dev27Id: 'spotfleetpolicyC2C65FA8', template: 'spot-fleet-request.yml', moduleOf: 'scheduler', vars: 'scheduler-roles' },
  // virtual_desktop_controller_stack.py:308, :365, :500, :754
  { stack: 'vdc', path: '{cluster}-vdc-ssm-commands-sns-topic-role-policy/Resource', dev27Id: 'ideadev27vdcssmcommandssnstopicrolepolicyFB3F8CC2', template: 'controller-ssm-command-pass-role.yml' },
  { stack: 'vdc', path: 'vdc-scheduled-event-transformer-policy/Resource', dev27Id: 'vdcscheduledeventtransformerpolicyAA1F68E7', template: 'controller-scheduled-event-transformer-lambda.yml' },
  { stack: 'vdc', path: 'vdc-host-policy/Resource', dev27Id: 'vdchostpolicy897A655B', template: 'virtual-desktop-dcv-host.yml' },
  { stack: 'vdc', path: '{cluster}-vdc-controller-policy/Resource', dev27Id: 'ideadev27vdccontrollerpolicy798DCB04', template: 'virtual-desktop-controller.yml', vars: 'component-role' },
  { stack: 'vdc', path: '{cluster}-vdc-broker-policy/Resource', dev27Id: 'ideadev27vdcbrokerpolicy7BA03365', template: 'virtual-desktop-dcv-broker.yml', vars: 'component-role' },
  { stack: 'vdc', path: '{cluster}-vdc-gateway-policy/Resource', dev27Id: 'ideadev27vdcgatewaypolicy32C2AAA6', template: 'virtual-desktop-dcv-connection-gateway.yml', vars: 'component-role' },
  {
    stack: 'vdc',
    path: 'vdc-controller-role/DefaultPolicy/Resource',
    dev27Id: 'vdccontrollerroleDefaultPolicy5F94E7B2',
    generatedBy: 'two Role.grant_pass_role calls (virtual_desktop_controller_stack.py:311, :507) on the controller role',
  },
  // Bootstrap resources have no construct metadata or policy template.
  { stack: 'bootstrap', resourceId: 'FilePublishingRoleDefaultPolicy', dev27Id: 'FilePublishingRoleDefaultPolicy', generatedBy: 'CDK bootstrap template: staging bucket + KMS key grants on the file-publishing role' },
  { stack: 'bootstrap', resourceId: 'ImagePublishingRoleDefaultPolicy', dev27Id: 'ImagePublishingRoleDefaultPolicy', generatedBy: 'CDK bootstrap template: ECR grants on the image-publishing role' },
  { stack: 'bootstrap', resourceId: 'CdkBootstrapPermissionsBoundaryPolicy', dev27Id: 'CdkBootstrapPermissionsBoundaryPolicy', generatedBy: 'CDK bootstrap template: the example permissions-boundary managed policy' },
];

// --- template access --------------------------------------------------------------------------

interface Resource {
  Type: string;
  Properties: { PolicyDocument: object };
  Metadata?: { 'aws:cdk:path'?: string };
}
interface Template {
  Resources: Record<string, Resource>;
}

/** Construct path with the stack segment dropped and the cluster name replaced by `{cluster}`. */
function constructPath(resource: Resource, cluster: string): string | undefined {
  const path = resource.Metadata?.['aws:cdk:path'];
  if (path === undefined) return undefined;
  return path.split('/').slice(1).join('/').replaceAll(cluster, '{cluster}');
}

function findByPath(template: Template, cluster: string, path: string): [string, Resource] | undefined {
  return Object.entries(template.Resources).find(([, r]) => constructPath(r, cluster) === path);
}

function iamPolicies(template: Template): [string, Resource][] {
  return Object.entries(template.Resources)
    .filter(([, resource]) => resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy');
}

function attributionFor(stack: string, id: string, resource: Resource, cluster: string): Attribution[] {
  const path = constructPath(resource, cluster);
  return ATTRIBUTED.filter(
    (entry) =>
      entry.stack === stack &&
      ((entry.path !== undefined && entry.path === path) ||
        (entry.pathPattern !== undefined && path !== undefined && entry.pathPattern.test(path)) ||
        (entry.resourceId !== undefined && entry.resourceId === id)),
  );
}

function policyKey(stack: string, id: string, resource: Resource, cluster: string): string {
  return `${stack}/${constructPath(resource, cluster) ?? `#${id}`}`;
}

/**
 * What `iam.PolicyDocument.from_json(rendered)` ends up as in a template: `toJSON()` normalises
 * each statement (a one-element `Action`/`Resource` list collapses to a string, duplicates inside
 * a list drop out), and resolving it through a stack runs `PostProcessPolicyDocument`, which drops
 * statements that are duplicates of an earlier one and turns CDK tokens into `Fn::GetAtt`. Both
 * halves are CDK's own code; `Stack.resolve` is what synth calls, so this is the shape the live
 * templates hold. Statement sorting is disabled by the `minimizePolicies` feature flag.
 */
function policyDocumentForComparison(document: object): object {
  return new Stack().resolve(iam.PolicyDocument.fromJson(document)) as object;
}

/**
 * The construct-path shapes CDK gives a document it wrote itself: a role's `DefaultPolicy`, an
 * `AwsCustomResource` provider's `CustomResourcePolicy`, and the bootstrap template, whose
 * resources carry no construct metadata at all. Nothing outside these shapes may be classified
 * `generatedBy`, and nothing inside them may claim a policy template - so a template-rendered
 * policy cannot be parked in the generated class to get it out of the comparison.
 */
function isCdkGenerated(stack: string, path: string | undefined): boolean {
  if (stack === 'bootstrap') return true;
  return path !== undefined && (path.endsWith('/DefaultPolicy/Resource') || path.endsWith('/CustomResourcePolicy/Resource'));
}

interface Classification {
  /** Live logical id -> the entry that claims it. */
  byLogicalId: Map<string, Attribution>;
  rendered: string[];
  generated: string[];
}

/** Classifies every IAM policy in `stacks`: exactly one entry each, in the class its path allows. */
function classifyAllPolicies(cluster: Cluster, stacks: string[]): Classification {
  const unmatched: string[] = [];
  const ambiguous: string[] = [];
  const misclassified: string[] = [];
  const byLogicalId = new Map<string, Attribution>();
  const rendered: string[] = [];
  const generated: string[] = [];
  for (const stack of stacks) {
    const template = cluster.templateFor(stack);
    if (template === undefined) continue;
    for (const [id, resource] of iamPolicies(template)) {
      const key = policyKey(stack, id, resource, cluster.name);
      const matches = attributionFor(stack, id, resource, cluster.name);
      if (matches.length === 0) {
        unmatched.push(key);
        continue;
      }
      if (matches.length > 1) {
        ambiguous.push(key);
        continue;
      }
      const entry = matches[0] as Attribution;
      byLogicalId.set(id, entry);
      const generatedByCdk = isCdkGenerated(stack, constructPath(resource, cluster.name));
      if ((entry.generatedBy !== undefined) !== generatedByCdk) {
        misclassified.push(`${key}: classified ${entry.generatedBy === undefined ? 'template-rendered' : 'L2-generated'}, construct path says ${generatedByCdk ? 'L2-generated' : 'template-rendered'}`);
      }
      (entry.template === undefined ? generated : rendered).push(key);
    }
  }
  assert.deepStrictEqual(unmatched, [], `unclassified IAM policies: ${unmatched.join(', ')}`);
  assert.deepStrictEqual(ambiguous, [], `ambiguously classified IAM policies: ${ambiguous.join(', ')}`);
  assert.deepStrictEqual(misclassified, [], `misclassified IAM policies: ${misclassified.join('; ')}`);
  return { byLogicalId, rendered, generated };
}

// --- the comparison ---------------------------------------------------------------------------

interface Cluster {
  name: string;
  configFor: () => ClusterConfig;
  templateFor: (stack: string) => Template | undefined;
}

function varsFor(entry: Attribution, template: Template, cluster: string): Record<string, unknown> | undefined {
  if (entry.vars === 'scheduler-roles') {
    // Python passes `role.role_arn`, a CDK token that resolves to the role's `Fn::GetAtt`.
    const roleArn = (path: string): string => {
      const found = findByPath(template, cluster, path);
      assert.ok(found !== undefined, `role not found in ${cluster} scheduler: ${path}`);
      return Fn.getAtt(found[0], 'Arn').toString();
    };
    return {
      scheduler_role_arn: roleArn('scheduler-role/Resource'),
      compute_node_role_arn: roleArn('scheduler-compute-node-role/Resource'),
      spot_fleet_request_role_arn: roleArn('scheduler-spot-fleet-request-role/Resource'),
    };
  }
  // `_build_iam_role` passes vars.role_arn; no policy template reads it.
  if (entry.vars === 'component-role') return { role_arn: '<<unused>>' };
  return undefined;
}

function compareAll(cluster: Cluster): { matched: string[]; failed: string[]; absent: string[] } {
  const config = cluster.configFor();
  const matched: string[] = [];
  const failed: string[] = [];
  const absent: string[] = [];
  for (const entry of ATTRIBUTED) {
    // L2-generated documents have no template to render; `classifyAllPolicies` accounts for them.
    if (entry.template === undefined) continue;
    // A retired policy is in the captured template and in no source tree.
    if (entry.retired !== undefined) continue;
    const template = cluster.templateFor(entry.stack);
    const found = template === undefined || entry.path === undefined ? undefined : findByPath(template, cluster.name, entry.path);
    const label = `${entry.stack}/${entry.path ?? `#${entry.resourceId ?? 'unknown'}`}`;
    if (found === undefined) {
      assert.ok(entry.conditional === true, `${label} is missing from ${cluster.name} but is not conditional`);
      absent.push(label);
      continue;
    }
    const rendered = renderPolicy(entry.template, {
      config,
      moduleId: entry.moduleOf === undefined ? undefined : config.moduleId(entry.moduleOf),
      vars: varsFor(entry, template as Template, cluster.name),
    });
    try {
      assert.deepStrictEqual(policyDocumentForComparison(rendered), found[1].Properties.PolicyDocument);
      matched.push(label);
    } catch (error) {
      failed.push(`${label} (${entry.template}): ${String(error).split('\n').slice(0, 20).join('\n')}`);
    }
  }
  return { matched, failed, absent };
}

describe('the classification itself', () => {
  it('puts every entry in exactly one class, with a reason', () => {
    const broken = ATTRIBUTED.filter(
      (entry) => (entry.template === undefined) === (entry.generatedBy === undefined) || entry.generatedBy?.trim() === '',
    ).map((entry) => `${entry.stack}/${entry.path ?? entry.pathPattern ?? entry.resourceId}`);
    assert.deepStrictEqual(broken, [], `entries need exactly one of template / generatedBy: ${broken.join(', ')}`);
  });

  it('names a policy template that exists for every template-rendered entry', () => {
    const dir = join(resourcesDir(), 'policies');
    const missing = ATTRIBUTED.filter(
      (entry) => entry.template !== undefined && entry.retired === undefined && !existsSync(join(dir, entry.template)),
    ).map((entry) => entry.template);
    assert.deepStrictEqual(missing, []);
    // A retired entry names a template that is gone, which is what makes it retired.
    const stillPresent = ATTRIBUTED.filter(
      (entry) => entry.retired !== undefined && entry.template !== undefined && existsSync(join(dir, entry.template)),
    ).map((entry) => entry.template);
    assert.deepStrictEqual(stillPresent, [], 'a retired entry still has its policy template; drop the retired flag');
  });
});

describe('renderPolicy on a template that does not decode', () => {
  /** The four `cluster.aws.*` values and the cluster name `policyContext` reads with required=true. */
  function minimalConfig(): ClusterConfig {
    const rows: Record<string, string> = {
      'cluster.cluster_name': 'sample-cluster',
      'cluster.aws.region': 'us-east-2',
      'cluster.aws.dns_suffix': 'amazonaws.com',
      'cluster.aws.partition': 'aws',
      'cluster.aws.account_id': '123456789012',
    };
    const items = Object.entries(rows).map(([key, value]) => ({ key: { S: key }, value: { S: value } }));
    return ClusterConfig.fromFile(JSON.stringify({ Items: items }), JSON.stringify({ Items: [] }));
  }

  it('raises the decode error itself, naming the template', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ideactl-policy-'));
    writeFileSync(join(dir, 'broken.yml'), "Version: '2012-10-17'\nStatement:\n  - Action: [s3:GetObject\n");
    assert.throws(
      () => renderPolicy('broken.yml', { config: minimalConfig(), policiesDir: dir }),
      (error: Error) => error.name === 'YAMLException' && error.message.includes('broken.yml'),
    );
  });
});

describe('PolicyDocument comparison boundary', () => {
  it('drops a duplicated statement, the way synth does', () => {
    // virtual-desktop-dcv-broker.yml carries the same s3 statement twice; the live template has one.
    const statement = { Action: ['s3:GetObject', 's3:PutObject'], Effect: 'Allow', Resource: '*' };
    const document = policyDocumentForComparison({
      Version: '2012-10-17',
      Statement: [statement, statement],
    }) as { Statement: object[] };
    assert.equal(document.Statement.length, 1);
  });

  it('collapses a one-element Action list and resolves a token Resource', () => {
    const document = policyDocumentForComparison({
      Version: '2012-10-17',
      Statement: [{ Action: ['sts:AssumeRole'], Effect: 'Allow', Resource: [Fn.getAtt('someRole1234ABCD', 'Arn').toString()] }],
    }) as { Statement: { Action: unknown; Resource: unknown }[] };
    assert.equal(document.Statement[0].Action, 'sts:AssumeRole');
    assert.deepStrictEqual(document.Statement[0].Resource, { 'Fn::GetAtt': ['someRole1234ABCD', 'Arn'] });
  });

  it('leaves statement order and action order alone', () => {
    const document = policyDocumentForComparison({
      Version: '2012-10-17',
      Statement: [
        { Action: ['s3:PutObject', 's3:GetObject'], Effect: 'Allow', Resource: '*' },
        { Action: 'sqs:SendMessage', Effect: 'Allow', Resource: '*' },
      ],
    }) as { Statement: { Action: unknown }[] };
    assert.deepStrictEqual(document.Statement[0].Action, ['s3:PutObject', 's3:GetObject']);
    assert.equal(document.Statement[1].Action, 'sqs:SendMessage');
  });
});

// --- dev27: the deployed templates --------------------------------------------------------------

/** Rebuilds a `ClusterConfig` from a dynamodb scan with rows replaced. */
function configFromScan(scanJson: string, modulesJson: string, overrides: Record<string, unknown>): ClusterConfig {
  const scan = JSON.parse(scanJson) as { Items: { key: { S: string }; value: unknown }[] };
  for (const [key, value] of Object.entries(overrides)) {
    const attribute = typeof value === 'boolean' ? { BOOL: value } : { S: String(value) };
    const item = scan.Items.find((row) => row.key.S === key);
    if (item === undefined) scan.Items.push({ key: { S: key }, value: attribute });
    else item.value = attribute;
  }
  return ClusterConfig.fromFile(JSON.stringify(scan), modulesJson);
}

/** Overrides that match the captured scheduler policy. */
const DEV27_STALE_ROWS = { 'scheduler.use_stable_server_name': false };

describe('renderPolicy vs the deployed dev27 templates', () => {
  const templates = new Map<string, Template>();
  const dev27: Cluster = {
    name: 'idea-dev27',
    configFor: () => configFromScan(readFileSync(SCAN, 'utf-8'), readFileSync(MODULES, 'utf-8'), DEV27_STALE_ROWS),
    templateFor: (stack) => {
      if (!templates.has(stack)) {
        const file = `${LIVE}idea-dev27-${stack}.json`;
        if (!existsSync(file)) throw new Error(`dev27 fixture is missing captured template: ${file}`);
        templates.set(stack, JSON.parse(readFileSync(file, 'utf-8')) as Template);
      }
      return templates.get(stack);
    },
  };

  it('requires all captured stack templates before comparing policies', () => {
    assert.deepStrictEqual(missingDev27Templates, []);
  });

  it('classifies every IAM policy in the ten stacks plus bootstrap, by logical id', () => {
    const { byLogicalId, rendered, generated } = classifyAllPolicies(dev27, STACKS);
    // The committed list and the captured templates hold the same set of logical ids.
    const live = [...byLogicalId.keys()].sort();
    const committed = ATTRIBUTED.filter((entry) => entry.dev27Id !== undefined).map((entry) => entry.dev27Id as string).sort();
    assert.deepStrictEqual(live, committed);
    // Each live resource is claimed by the entry that pins that logical id.
    const mispinned = [...byLogicalId.entries()].filter(([id, entry]) => entry.dev27Id !== id).map(([id]) => id);
    assert.deepStrictEqual(mispinned, []);
    console.log(`dev27 IAM policies: ${rendered.length + generated.length}; template-rendered ${rendered.length}; L2-generated ${generated.length}; unclassified 0`);
  });

  it('renders every template-rendered policy identically', () => {
    const { matched, failed, absent } = compareAll(dev27);
    console.log(`dev27 matched ${matched.length}/${matched.length + failed.length}; absent (feature off): ${absent.join(', ')}`);
    assert.deepStrictEqual(failed, []);
    assert.equal(
      matched.length + absent.length,
      ATTRIBUTED.filter((entry) => entry.template !== undefined && entry.retired === undefined).length,
    );
  });
});

// --- local Python synth output ---------------------------------------------------------------

/** Marks a cluster directory as a captured reference synth rather than a working directory. */
const ORACLE_MARKER = 'REFERENCE-ORACLE';

/**
 * Every `<cluster>/<region>` under `~/.idea/clusters` that is declared to hold a reference synth
 * and has a config directory. Real cluster names never reach this file: they come off the filesystem.
 *
 * The declaration is required rather than inferred, and that is the whole point. This used to accept
 * any directory with synth output in it, on the stated assumption that only the reference
 * implementation ever wrote there. That stopped being true the moment this tool could install a
 * cluster: an install leaves a partial synth of its own under the same path, which was then compared
 * against as though it were an oracle, and the suite went red for everyone until somebody moved the
 * directory out of the way. It happened twice in one day. A marker file cannot be produced by
 * accident, so a live cluster's working directory is now ignored by construction instead of by
 * everyone remembering.
 */
function discoverSynthClusters(): { name: string; root: string }[] {
  const found: { name: string; root: string }[] = [];
  for (const name of readdirSync(CLUSTERS)) {
    for (const region of readdirSync(join(CLUSTERS, name)).filter((r) => existsSync(join(CLUSTERS, name, r, '_cdk')))) {
      const root = join(CLUSTERS, name, region);
      if (!existsSync(join(root, ORACLE_MARKER))) continue;
      const stacks = readdirSync(join(root, '_cdk')).filter((d) => d.startsWith('cdk.out.'));
      if (stacks.length > 0 && configDirOf(root) !== undefined) found.push({ name, root });
    }
  }
  return found;
}

/** Select the fixture configuration directory. */
function configDirOf(root: string): string | undefined {
  const golden = readdirSync(root).filter((name) => name.startsWith('config.golden.')).sort();
  const candidates = [...golden.reverse().map((name) => join(root, name)), join(root, 'config')];
  return candidates.find((dir) => existsSync(join(dir, 'idea.yml')));
}

/** `read_config_from_files` + `traverse_config` over `<cluster>/config/<module_id>/*.yml`. */
function flattenConfigDir(configDir: string): Record<string, unknown> {
  const idea = load(readFileSync(join(configDir, 'idea.yml'), 'utf-8')) as { modules: { id: string; config_files: string[] }[] };
  const flat: Record<string, unknown> = {};
  const walk = (prefix: string, value: unknown): void => {
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const entries = Object.entries(value as Record<string, unknown>);
      if (entries.length === 0) {
        flat[prefix] = null;
        return;
      }
      for (const [key, child] of entries) walk(`${prefix}.${key}`, child);
      return;
    }
    flat[prefix] = typeof value === 'string' && value.trim() === '' ? null : value;
  };
  for (const module of idea.modules) {
    for (const file of module.config_files) {
      const path = join(configDir, module.id, file);
      if (!existsSync(path)) continue;
      const content = (load(readFileSync(path, 'utf-8')) ?? {}) as Record<string, unknown>;
      for (const [key, value] of Object.entries(content)) walk(`${module.id}.${key}`, value);
    }
  }
  return flat;
}

/** Wraps a flattened config as the dynamodb scan shape `ClusterConfig.fromFile` reads. */
function configFromFlat(flat: Record<string, unknown>, modules: { id: string; name: string; type: string }[]): ClusterConfig {
  const attribute = (value: unknown): unknown => {
    if (value === null || value === undefined) return { NULL: true };
    if (typeof value === 'boolean') return { BOOL: value };
    if (typeof value === 'number') return { N: String(value) };
    if (Array.isArray(value)) return { L: value.map(attribute) };
    if (typeof value === 'object') return { M: Object.fromEntries(Object.entries(value as object).map(([k, v]) => [k, attribute(v)])) };
    return { S: String(value) };
  };
  const items = Object.entries(flat).map(([key, value]) => ({ key: { S: key }, value: attribute(value) }));
  const moduleItems = modules.map((m) => ({ module_id: { S: m.id }, name: { S: m.name }, type: { S: m.type } }));
  return ClusterConfig.fromFile(JSON.stringify({ Items: items }), JSON.stringify({ Items: moduleItems }));
}

const synthClusters = discoverSynthClusters();

describe('local Python synth outputs', () => {
  it('found at least one cluster to compare against', () => {
    assert.ok(
      synthClusters.length > 0,
      `no cluster under ${CLUSTERS} is a reference oracle: each needs a _cdk/cdk.out.* synth, a config `
        + `directory, and a ${ORACLE_MARKER} file declaring the synth came from the reference `
        + `implementation rather than from a live install of this tool`,
    );
  });
});

for (const { name, root } of synthClusters) {
  describe(`renderPolicy vs the Python synth output for ${name}`, () => {
    const templates = new Map<string, Template>();
    const cluster: Cluster = {
      name,
      configFor: () => {
        const configDir = configDirOf(root) as string;
        const idea = load(readFileSync(join(configDir, 'idea.yml'), 'utf-8')) as { modules: { id: string; name: string; type: string }[] };
        return configFromFlat(flattenConfigDir(configDir), idea.modules);
      },
      templateFor: (stack) => {
        if (!templates.has(stack)) {
          const file = join(root, '_cdk', `cdk.out.${stack}`, `${name}-${stack}.template.json`);
          if (!existsSync(file)) return undefined;
          templates.set(stack, JSON.parse(readFileSync(file, 'utf-8')) as Template);
        }
        return templates.get(stack);
      },
    };
    const synthStacks = readdirSync(join(root, '_cdk')).filter((d) => d.startsWith('cdk.out.')).map((d) => d.slice('cdk.out.'.length));

    it('has a template for every synthesized stack', () => {
      const missing = synthStacks.filter((stack) => !existsSync(join(root, '_cdk', `cdk.out.${stack}`, `${name}-${stack}.template.json`)));
      assert.deepStrictEqual(missing, []);
    });

    it('classifies every IAM policy in the synth output', () => {
      const { rendered, generated } = classifyAllPolicies(cluster, synthStacks);
      console.log(`${name}: IAM policies: ${rendered.length + generated.length}; template-rendered ${rendered.length}; L2-generated ${generated.length}; unclassified 0`);
    });

    it('renders every template-rendered policy identically', () => {
      const { matched, failed, absent } = compareAll(cluster);
      console.log(`${name}: matched ${matched.length}/${matched.length + failed.length}; absent (feature off): ${absent.join(', ') || 'none'}`);
      assert.deepStrictEqual(failed, []);
      assert.equal(
      matched.length + absent.length,
      ATTRIBUTED.filter((entry) => entry.template !== undefined && entry.retired === undefined).length,
    );
    });
  });
}
