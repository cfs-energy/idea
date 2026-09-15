// Stacks whose recorded reference template predates a gated change that this branch already has.
//
// The reference template stays the oracle. It is compared with no tolerance at all, against a
// synthesis driven by the settings the reference template was generated from: `deployedInput`
// names the rows to put back and why. That comparison proves the port.
//
// The change the branch adds is then proved separately, as a delta rather than as an allowance.
// The gate requires that a synthesis from the settings as captured differs from the reference
// template in exactly the itemised `differences` and in nothing else, and that undoing every
// `cause` turns it back into the deployed-input synthesis property for property. So a ninth
// difference fails, one of the itemised differences changing shape fails, and an entry that has
// stopped applying fails instead of rotting in place.
//
// Nothing here is an ignore list. There is no code path in which a difference is accepted
// without being named, valued and reverted.

import { readFileSync, writeFileSync } from 'node:fs';

import { isStatefulType } from '../../src/cdk/stateful.ts';
import { compareTemplates, formatValue, isParity, type Json, type JsonObject } from './parity.ts';
import { NODE_HANDLERS } from './node-handlers.ts';

/** Reads one string setting out of the captured table dump. */
export type SettingsLookup = (key: string) => string | undefined;

export interface IntendedDifference {
  /** The path the plain comparison reports for this difference. */
  path: string;
  /** Which recorded cause produces it. */
  cause: string;
  /** Why this difference exists. */
  reason: string;
  /** The condition under which this entry is removed. */
  removeWhen: string;
}

interface DriftCause {
  id: string;
  /** What the branch adds, in words. */
  change: string;
  /**
   * True when the branch produces this difference from any settings, so both syntheses carry it.
   * An unconditional cause is undone on the deployed-input synthesis as well, before that
   * synthesis is held against the recorded template with no tolerance. A cause without this flag
   * is gated on a settings row and appears only in the as-captured synthesis.
   */
  unconditional?: boolean;
  /**
   * Undoes this cause in the as-captured template, in place. Returns a failure for every way
   * the template does not carry the recorded shape, so a changed value cannot pass by being
   * reverted to whatever it happens to be.
   */
  revert(asCaptured: JsonObject, settings: SettingsLookup): string[];
}

export interface IntendedDrift {
  cluster: string;
  stack: string;
  /** One line naming the branch change the reference template predates. */
  summary: string;
  /** Settings rows put back to the values the reference template was generated from. */
  deployedInput: Record<string, boolean | string>;
  /** Why those rows differ from the capture. */
  deployedInputReason: string;
  /** What has to happen for this whole entry to be deleted. */
  endsWhen: string;
  differences: IntendedDifference[];
  causes: DriftCause[];
}

function isObject(value: Json | undefined): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** The stack's single settings custom resource, whose properties carry the emitted rows. */
function clusterSettings(template: JsonObject): { id: string; settings: JsonObject } | string {
  const resources = isObject(template.Resources) ? template.Resources : {};
  const found = Object.entries(resources).filter(
    ([, resource]) => isObject(resource) && resource.Type === 'Custom::ClusterSettings',
  );
  if (found.length !== 1) return `expected one Custom::ClusterSettings resource, found ${found.length}`;
  const [id, resource] = found[0] as [string, JsonObject];
  const properties = isObject(resource.Properties) ? resource.Properties : undefined;
  const settings = properties !== undefined && isObject(properties.settings) ? properties.settings : undefined;
  if (settings === undefined) return `${id} has no settings properties`;
  return { id, settings };
}

/** Every inline policy document in the template, by logical id. */
function policyStatements(template: JsonObject): Array<{ id: string; statements: Json[] }> {
  const resources = isObject(template.Resources) ? template.Resources : {};
  const out: Array<{ id: string; statements: Json[] }> = [];
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource)) continue;
    const properties = isObject(resource.Properties) ? resource.Properties : undefined;
    const document = properties !== undefined && isObject(properties.PolicyDocument) ? properties.PolicyDocument : undefined;
    if (document === undefined || !Array.isArray(document.Statement)) continue;
    out.push({ id, statements: document.Statement });
  }
  return out;
}

/**
 * The instance attribute the settings row carried before the stable name.
 * Declared rather than read from the other side: reading it would accept any value.
 */
const INSTANCE_PRIVATE_DNS: Json = { 'Fn::GetAtt': ['schedulerinstance', 'PrivateDnsName'] };

const DNS_RECORD_SID = 'SchedulerDnsRecord';
/** The position the policy source puts the statement at, before the directory service includes. */
const DNS_RECORD_INDEX = 26;

const stableNameSetting: DriftCause = {
  id: 'stable-name-setting',
  change: 'the emitted private_dns_name row is the configured scheduler hostname rather than the instance attribute',
  revert(asCaptured, settings) {
    const found = clusterSettings(asCaptured);
    if (typeof found === 'string') return [found];
    const hostname = settings('scheduler.hostname');
    if (hostname === undefined) return ['the captured settings have no scheduler.hostname row'];
    const actual = found.settings.private_dns_name;
    const failures =
      actual === hostname
        ? []
        : [
            `${found.id} settings.private_dns_name is ${formatValue(actual)}, expected the scheduler.hostname value ${formatValue(hostname)}`,
          ];
    found.settings.private_dns_name = structuredClone(INSTANCE_PRIVATE_DNS);
    return failures;
  },
};

const dnsRecordStatement: DriftCause = {
  id: 'dns-record-statement',
  change: `one ${DNS_RECORD_SID} statement is inserted into the scheduler policy at index ${DNS_RECORD_INDEX}`,
  revert(asCaptured, settings) {
    const partition = settings('cluster.aws.partition');
    if (partition === undefined) return ['the captured settings have no cluster.aws.partition row'];
    const expected: Json = {
      Action: 'route53:ChangeResourceRecordSets',
      Effect: 'Allow',
      Resource: `arn:${partition}:route53:::hostedzone/*`,
      Sid: DNS_RECORD_SID,
    };
    const carriers = policyStatements(asCaptured)
      .map(({ id, statements }) => ({
        id,
        statements,
        indexes: statements.flatMap((statement, index) => (isObject(statement) && statement.Sid === DNS_RECORD_SID ? [index] : [])),
      }))
      .filter((carrier) => carrier.indexes.length > 0);
    if (carriers.length !== 1) return [`expected one policy carrying a ${DNS_RECORD_SID} statement, found ${carriers.length}`];
    const carrier = carriers[0]!;
    if (carrier.indexes.length !== 1) {
      return [`${carrier.id} carries ${carrier.indexes.length} ${DNS_RECORD_SID} statements, expected one`];
    }
    const index = carrier.indexes[0]!;
    const failures: string[] = [];
    if (index !== DNS_RECORD_INDEX) {
      failures.push(`${carrier.id} has ${DNS_RECORD_SID} at index ${index}, expected ${DNS_RECORD_INDEX}`);
    }
    const statement = carrier.statements[index];
    if (JSON.stringify(statement) !== JSON.stringify(expected)) {
      failures.push(`${carrier.id} statement ${DNS_RECORD_SID} is ${formatValue(statement)}, expected ${formatValue(expected)}`);
    }
    carrier.statements.splice(index, 1);
    return failures;
  },
};

const POLICY = 'Resources.schedulerpolicyFF65A604.Properties.PolicyDocument.Statement';
const SHIFTED_BY_INSERTION =
  'the statement it names keeps its content and moves down one index, because the inserted statement sits before the directory service includes in the policy source';
const REMOVE_ON_DEPLOY = 'the scheduler stack is deployed to this cluster, which makes the recorded template current again';

const SCHEDULER_DRIFT: IntendedDrift = {
  cluster: 'idea-dev27',
  stack: 'scheduler',
  summary: 'scheduler.use_stable_server_name is on in the captured settings and the recorded template predates it',
  deployedInput: { 'scheduler.use_stable_server_name': false },
  deployedInputReason:
    'the row was pre-staged in this cluster settings table but the scheduler stack was never redeployed, so the recorded template is a generation behind the table it was captured with',
  endsWhen: REMOVE_ON_DEPLOY,
  differences: [
    {
      path: 'Resources.ideadev27schedulersettings.Properties.settings.private_dns_name',
      cause: stableNameSetting.id,
      reason:
        'with the flag on the stack writes the configured scheduler hostname instead of the instance private DNS attribute, so execution hosts keep one server name across a scheduler replacement',
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[26].Action`,
      cause: dnsRecordStatement.id,
      reason: 'the inserted statement grants the record change the container scheduler path performs when a replacement task starts',
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[26].Resource`,
      cause: dnsRecordStatement.id,
      reason: 'the inserted statement is scoped to hosted zone record changes in this partition',
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[26].Sid`,
      cause: dnsRecordStatement.id,
      reason: `the inserted statement is identified as ${DNS_RECORD_SID}`,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[27].Action`,
      cause: dnsRecordStatement.id,
      reason: SHIFTED_BY_INSERTION,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[27].Resource`,
      cause: dnsRecordStatement.id,
      reason: SHIFTED_BY_INSERTION,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[27].Sid`,
      cause: dnsRecordStatement.id,
      reason: SHIFTED_BY_INSERTION,
      removeWhen: REMOVE_ON_DEPLOY,
    },
    {
      path: `${POLICY}[28]`,
      cause: dnsRecordStatement.id,
      reason: `${SHIFTED_BY_INSERTION}, and this is the last statement, so the recorded template has no index 28 at all`,
      removeWhen: REMOVE_ON_DEPLOY,
    },
  ],
  causes: [stableNameSetting, dnsRecordStatement],
};

const DRIFT: IntendedDrift[] = [SCHEDULER_DRIFT];

const RETAIN_CAUSE = 'retain-stateful-on-update-replace';

/**
 * Every resource in the recorded template that this branch now marks `UpdateReplacePolicy: Retain`,
 * with the value the recorded template carries at that path.
 *
 * Read from the recorded template, which is the oracle, and never from the synthesis. A resource
 * the synthesis marks and this list does not know about shows up as an unexpected difference, and
 * one this list expects and the synthesis does not mark shows up as a difference no longer
 * produced. Both fail.
 */
function retainedInThisBranch(deployed: JsonObject): Array<[string, Json | undefined]> {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const out: Array<[string, Json | undefined]> = [];
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource) || typeof resource.Type !== 'string') continue;
    if (!isStatefulType(resource.Type)) continue;
    if (resource.UpdateReplacePolicy === 'Retain') continue;
    out.push([id, resource.UpdateReplacePolicy]);
  }
  return out;
}

/** The resource type the recorded template gives a logical id, for the per-difference reason. */
function recordedType(deployed: JsonObject, id: string): string {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const resource = resources[id];
  return isObject(resource) && typeof resource.Type === 'string' ? resource.Type : '<unknown type>';
}

/**
 * The drift every stack carrying a stateful resource has, because this branch sets
 * `UpdateReplacePolicy: Retain` on all of them and no recorded template predates that.
 *
 * Unlike the settings-gated entries above there is no row to put back: the change is
 * unconditional, so it is undone on both syntheses and the recorded template is still held
 * against a synthesis with the change removed, with no tolerance.
 */
function retainStatefulDrift(cluster: string, stack: string, deployed: JsonObject): IntendedDrift | undefined {
  const retained = retainedInThisBranch(deployed);
  if (retained.length === 0) return undefined;
  const endsWhen = `the ${stack} stack is deployed to ${cluster} from this branch, which puts Retain in the recorded template`;
  const cause: DriftCause = {
    id: RETAIN_CAUSE,
    change: `UpdateReplacePolicy is Retain on the ${retained.length} stateful resource(s) this stack creates`,
    unconditional: true,
    revert(template) {
      const resources = isObject(template.Resources) ? template.Resources : {};
      const failures: string[] = [];
      for (const [id, recorded] of retained) {
        const resource = resources[id];
        if (!isObject(resource)) {
          failures.push(`${id} is in the recorded template and not in the synthesized one`);
          continue;
        }
        if (resource.UpdateReplacePolicy !== 'Retain') {
          failures.push(`${id} UpdateReplacePolicy is ${formatValue(resource.UpdateReplacePolicy)}, expected Retain`);
        }
        if (recorded === undefined) delete resource.UpdateReplacePolicy;
        else resource.UpdateReplacePolicy = structuredClone(recorded);
      }
      return failures;
    },
  };
  return {
    cluster,
    stack,
    summary: `this branch sets UpdateReplacePolicy Retain on every stateful resource; the recorded template predates it`,
    deployedInput: {},
    deployedInputReason: 'no settings row gates this change, so the deployed-input synthesis is the as-captured one',
    endsWhen,
    differences: retained.map(([id, recorded]) => ({
      path: `Resources.${id}.UpdateReplacePolicy`,
      cause: RETAIN_CAUSE,
      reason:
        `${recordedType(deployed, id)} holds state that cannot be rebuilt from the template, and the recorded ` +
        `value ${formatValue(recorded)} lets an update that forces a replacement delete it`,
      removeWhen: endsWhen,
    })),
    causes: [cause],
  };
}

const NODE_HANDLER_CAUSE = 'node-handler-runtime';
const NODE_RUNTIME = 'nodejs22.x';
const NODE_LAMBDA_HANDLER = 'index.handler';
const NAG_RULE = 'AwsSolutions-L1';
const NODE_NAG_REASON = 'Node 22 is the runtime the deploy tool is built and tested with.';
/** The one suppression a Node function carries, which is what its recorded pair is compared with. */
const NODE_NAG_RULES: Json[] = [{ reason: NODE_NAG_REASON, id: NAG_RULE }];

/** The `cdk_nag` suppressions one resource of a template carries, when it has any. */
function nagRules(resource: JsonObject): Json[] | undefined {
  const metadata = isObject(resource.Metadata) ? resource.Metadata : undefined;
  const nag = metadata !== undefined && isObject(metadata.cdk_nag) ? metadata.cdk_nag : undefined;
  return nag !== undefined && Array.isArray(nag.rules_to_suppress) ? nag.rules_to_suppress : undefined;
}

/** One ported function as the recorded template has it: the values the revert puts back. */
interface RecordedPythonHandler {
  logicalId: string;
  packageName: string;
  runtime: Json | undefined;
  handler: Json | undefined;
  nag: Json[] | undefined;
}

/**
 * Every ported handler the recorded template actually carries, read from the recorded template
 * because that is the oracle. A logical id the recorded template does not have is not expected of
 * the synthesis, and one the synthesis stops producing fails as a missing resource.
 */
function recordedPythonHandlers(deployed: JsonObject): RecordedPythonHandler[] {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const out: RecordedPythonHandler[] = [];
  for (const handler of NODE_HANDLERS) {
    const resource = resources[handler.logicalId];
    if (!isObject(resource) || resource.Type !== 'AWS::Lambda::Function') continue;
    const properties = isObject(resource.Properties) ? resource.Properties : {};
    out.push({
      logicalId: handler.logicalId,
      packageName: handler.packageName,
      runtime: properties.Runtime,
      handler: properties.Handler,
      nag: nagRules(resource),
    });
  }
  return out;
}

/**
 * The drift every stack carrying a ported handler has: the function is the same function, updated
 * in place, on the runtime its implementation is now written for. The logical id, the function
 * name and the package type are unchanged, which is what makes it an update rather than a
 * replacement; `test/handler-inplace` proves that separately and from the same table.
 *
 * Like the retain entry this is unconditional: no settings row gates it, so it is undone on both
 * syntheses and the recorded template is still held against a synthesis with the change removed.
 */
function nodeHandlerDrift(cluster: string, stack: string, deployed: JsonObject): IntendedDrift | undefined {
  const recorded = recordedPythonHandlers(deployed);
  if (recorded.length === 0) return undefined;
  const endsWhen = `the ${stack} stack is deployed to ${cluster} from this branch, which puts the Node runtime in the recorded template`;

  const cause: DriftCause = {
    id: NODE_HANDLER_CAUSE,
    change: `${recorded.length} deployed function(s) run the TypeScript port on ${NODE_RUNTIME} instead of the Python handler`,
    unconditional: true,
    revert(template) {
      const resources = isObject(template.Resources) ? template.Resources : {};
      const failures: string[] = [];
      for (const entry of recorded) {
        const resource = resources[entry.logicalId];
        if (!isObject(resource)) {
          failures.push(`${entry.logicalId} is in the recorded template and not in the synthesized one`);
          continue;
        }
        const properties = isObject(resource.Properties) ? resource.Properties : undefined;
        if (properties === undefined) {
          failures.push(`${entry.logicalId} has no Properties`);
          continue;
        }
        if (properties.Runtime !== NODE_RUNTIME) {
          failures.push(`${entry.logicalId} Runtime is ${formatValue(properties.Runtime)}, expected ${NODE_RUNTIME}`);
        }
        if (properties.Handler !== NODE_LAMBDA_HANDLER) {
          failures.push(`${entry.logicalId} Handler is ${formatValue(properties.Handler)}, expected ${NODE_LAMBDA_HANDLER}`);
        }
        properties.Runtime = structuredClone(entry.runtime) as Json;
        properties.Handler = structuredClone(entry.handler) as Json;
        if (entry.nag === undefined) continue;
        const rules = nagRules(resource);
        if (JSON.stringify(rules) !== JSON.stringify(NODE_NAG_RULES)) {
          failures.push(
            `${entry.logicalId} cdk_nag suppressions are ${formatValue(rules as Json)}, expected ${formatValue(NODE_NAG_RULES)}`,
          );
          continue;
        }
        const metadata = resource.Metadata as JsonObject;
        (metadata.cdk_nag as JsonObject).rules_to_suppress = structuredClone(entry.nag);
      }
      return failures;
    },
  };

  const differences: IntendedDifference[] = [];
  for (const entry of recorded) {
    const base = `Resources.${entry.logicalId}`;
    differences.push({
      path: `${base}.Properties.Runtime`,
      cause: NODE_HANDLER_CAUSE,
      reason: `${entry.packageName} is now a TypeScript handler, so the function runs on ${NODE_RUNTIME}`,
      removeWhen: endsWhen,
    });
    differences.push({
      path: `${base}.Properties.Handler`,
      cause: NODE_HANDLER_CAUSE,
      reason: `the bundled handler is one ${NODE_LAMBDA_HANDLER} module rather than ${formatValue(entry.handler)}`,
      removeWhen: endsWhen,
    });
    const first = entry.nag?.[0];
    if (isObject(first)) {
      for (const key of Object.keys(first)) {
        if (JSON.stringify(first[key]) === JSON.stringify((NODE_NAG_RULES[0] as JsonObject)[key])) continue;
        differences.push({
          path: `${base}.Metadata.cdk_nag.rules_to_suppress[0].${key}`,
          cause: NODE_HANDLER_CAUSE,
          reason: `the ${NAG_RULE} suppression names the runtime the function actually uses`,
          removeWhen: endsWhen,
        });
      }
    }
    for (let index = 1; index < (entry.nag?.length ?? 0); index += 1) {
      differences.push({
        path: `${base}.Metadata.cdk_nag.rules_to_suppress[${index}]`,
        cause: NODE_HANDLER_CAUSE,
        reason: 'the second suppression explains the Python pin, and there is no Python pin left to explain',
        removeWhen: endsWhen,
      });
    }
  }

  return {
    cluster,
    stack,
    summary: 'the handler implementations this branch deploys are the TypeScript ports; the recorded template predates them',
    deployedInput: {},
    deployedInputReason: 'no settings row gates this change, so the deployed-input synthesis is the as-captured one',
    endsWhen,
    differences,
    causes: [cause],
  };
}

const RETIRED_RESOURCE_CAUSE = 'retired-resources';
const RETIRED_SETTING_CAUSE = 'retired-settings-row';
const CLIENT_SECRET_CAUSE = 'client-secret-from-the-client';
const CLIENT_SECRET_ATTRIBUTE = 'ClientSecret';
const OAUTH_CREDENTIALS_TYPE = 'Custom::GetOAuthCredentials';
const USER_POOL_CLIENT_TYPE = 'AWS::Cognito::UserPoolClient';

const PREFIX_LIST_REASON =
  'the deploy tool merges the configured client addresses into the prefix list once the stack has published its id, so the stack no longer carries a function to do it';
const OAUTH_REASON =
  'a user pool client returns its own generated secret, so nothing reads it back through a custom resource';

/**
 * What this branch stops creating, per stack. A retirement is recognised in the recorded template
 * by resource type or by the construct path under the stack, never by logical id: both are the
 * same in every cluster, which is what makes one declaration cover all of them.
 */
interface Retirement {
  stack: string;
  reason: string;
  /** `Custom::*` types this branch no longer emits. */
  types?: readonly string[];
  /** Construct paths under the stack, matched whole. */
  paths?: readonly string[];
}

const RETIRED_RESOURCES: readonly Retirement[] = [
  {
    stack: 'cluster',
    reason: PREFIX_LIST_REASON,
    types: ['Custom::ClusterPrefixList'],
    paths: [
      'update-cluster-prefix-list/Resource',
      'update-cluster-prefix-list-role/Resource',
      'update-cluster-prefix-list-policy/Resource',
    ],
  },
  {
    stack: 'identity-provider',
    reason: OAUTH_REASON,
    paths: ['oauth-credentials/Resource', 'oauth-credentials-role/Resource', 'oauth-credentials-policy/Resource'],
  },
  { stack: 'cluster-manager', reason: OAUTH_REASON, types: [OAUTH_CREDENTIALS_TYPE] },
  { stack: 'scheduler', reason: OAUTH_REASON, types: [OAUTH_CREDENTIALS_TYPE] },
  { stack: 'vdc', reason: OAUTH_REASON, types: [OAUTH_CREDENTIALS_TYPE] },
];

/** The settings rows a stack stops emitting because the resource they named is retired. */
const RETIRED_SETTINGS: Record<string, { key: string; reason: string }> = {
  'identity-provider': {
    key: 'cognito.oauth_credentials_lambda_arn',
    reason: 'the function the row named is gone, and no module reads the row',
  },
};

/** The construct path a recorded resource carries, with the stack segment dropped. */
function constructPath(resource: JsonObject): string | undefined {
  const metadata = isObject(resource.Metadata) ? resource.Metadata : undefined;
  const path = metadata?.['aws:cdk:path'];
  if (typeof path !== 'string') return undefined;
  const slash = path.indexOf('/');
  return slash < 0 ? '' : path.slice(slash + 1);
}

interface RetiredResource {
  id: string;
  type: string;
  reason: string;
  /** The resource exactly as the recorded template carries it: what the revert puts back. */
  recorded: JsonObject;
}

/**
 * Every resource of the recorded template this branch no longer creates. Read from the recorded
 * template, which is the oracle: a resource the synthesis still produces fails the revert, and one
 * the recorded template does not carry is simply not expected of anything.
 */
function retiredResources(stack: string, deployed: JsonObject): RetiredResource[] {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const out: RetiredResource[] = [];
  for (const retirement of RETIRED_RESOURCES.filter((entry) => entry.stack === stack)) {
    for (const [id, resource] of Object.entries(resources)) {
      if (!isObject(resource) || typeof resource.Type !== 'string') continue;
      const path = constructPath(resource);
      const matched =
        (retirement.types ?? []).includes(resource.Type) || (path !== undefined && (retirement.paths ?? []).includes(path));
      if (!matched) continue;
      out.push({ id, type: resource.Type, reason: retirement.reason, recorded: resource });
    }
  }
  return out;
}

/** The one settings row this stack stops emitting, with the value the recorded template holds. */
function retiredSetting(
  stack: string,
  deployed: JsonObject,
): { id: string; key: string; reason: string; recorded: Json } | undefined {
  const retirement = RETIRED_SETTINGS[stack];
  if (retirement === undefined) return undefined;
  const found = clusterSettings(deployed);
  if (typeof found === 'string' || !(retirement.key in found.settings)) return undefined;
  return { id: found.id, key: retirement.key, reason: retirement.reason, recorded: found.settings[retirement.key] as Json };
}

/**
 * The deployed secret whose value is the retired custom resource's `ClientSecret` attribute. The
 * same secret now reads the attribute off the user pool client, which is where the value came from
 * in the first place, so the secret updates in place with the value it already holds.
 */
function recordedClientSecret(deployed: JsonObject): { id: string; source: string } | undefined {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const credentialResources = new Set(
    Object.entries(resources)
      .filter(([, resource]) => isObject(resource) && resource.Type === OAUTH_CREDENTIALS_TYPE)
      .map(([id]) => id),
  );
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource) || resource.Type !== 'AWS::SecretsManager::Secret') continue;
    const properties = isObject(resource.Properties) ? resource.Properties : undefined;
    const secretString = properties !== undefined && isObject(properties.SecretString) ? properties.SecretString : undefined;
    const getAtt = secretString !== undefined && Array.isArray(secretString['Fn::GetAtt']) ? secretString['Fn::GetAtt'] : undefined;
    if (getAtt === undefined || getAtt[1] !== CLIENT_SECRET_ATTRIBUTE) continue;
    const source = getAtt[0];
    if (typeof source !== 'string' || !credentialResources.has(source)) continue;
    return { id, source };
  }
  return undefined;
}

/** The `Fn::GetAtt` array of one secret's `SecretString`, when it has that shape. */
function secretGetAtt(template: JsonObject, id: string): Json[] | string {
  const resources = isObject(template.Resources) ? template.Resources : {};
  const resource = resources[id];
  if (!isObject(resource)) return `${id} is in the recorded template and not in the synthesized one`;
  const properties = isObject(resource.Properties) ? resource.Properties : undefined;
  const secretString = properties !== undefined && isObject(properties.SecretString) ? properties.SecretString : undefined;
  const getAtt = secretString !== undefined && Array.isArray(secretString['Fn::GetAtt']) ? secretString['Fn::GetAtt'] : undefined;
  if (getAtt === undefined) return `${id} SecretString is not an Fn::GetAtt`;
  return getAtt;
}

/** The logical ids of the recorded template this branch no longer creates. */
export function retiredLogicalIds(stack: string, deployed: JsonObject): string[] {
  return retiredResources(stack, deployed).map((entry) => entry.id);
}

/** The certificate custom resources this branch keeps, defused and retained, for one more release. */
export function certificateLogicalIds(deployed: JsonObject): string[] {
  return recordedCertificates(deployed).map((entry) => entry.id);
}

/** The settings row this stack stops emitting, when it has one. */
export function retiredSettingKey(stack: string): string | undefined {
  return RETIRED_SETTINGS[stack]?.key;
}

/**
 * The drift a stack has because this wave retires a Lambda-backed resource it used to carry.
 *
 * Three changes, one story, all read from the recorded template so the expectation comes from the
 * oracle: the resources the branch no longer creates, the settings row that named one of them, and
 * the secret that now takes its value from the user pool client instead of from a custom resource.
 *
 * Unconditional, like the retain and node-handler entries: no settings row gates any of it, so
 * every cause is undone on both syntheses and the recorded template is still held against a
 * synthesis with the changes removed, with no tolerance.
 */
function retirementDrift(cluster: string, stack: string, deployed: JsonObject): IntendedDrift | undefined {
  const retired = retiredResources(stack, deployed);
  const setting = retiredSetting(stack, deployed);
  const secret = retired.length === 0 ? undefined : recordedClientSecret(deployed);
  if (retired.length === 0 && setting === undefined) return undefined;
  const endsWhen = `the ${stack} stack is deployed to ${cluster} from this branch, which removes these resources from the recorded template`;

  const causes: DriftCause[] = [];
  const differences: IntendedDifference[] = [];

  if (retired.length > 0) {
    causes.push({
      id: RETIRED_RESOURCE_CAUSE,
      change: `${retired.length} resource(s) this stack used to create are retired`,
      unconditional: true,
      revert(template) {
        const resources = isObject(template.Resources) ? template.Resources : {};
        const failures: string[] = [];
        for (const entry of retired) {
          if (resources[entry.id] !== undefined) {
            failures.push(`${entry.id} is still synthesized, so this branch has not retired it`);
            continue;
          }
          resources[entry.id] = structuredClone(entry.recorded);
        }
        return failures;
      },
    });
    for (const entry of retired) {
      differences.push({
        path: `Resources.${entry.id}`,
        cause: RETIRED_RESOURCE_CAUSE,
        reason: `the ${entry.type} is removed from the deployed stack: ${entry.reason}`,
        removeWhen: endsWhen,
      });
    }
  }

  if (setting !== undefined) {
    causes.push({
      id: RETIRED_SETTING_CAUSE,
      change: `the ${setting.key} row is no longer emitted`,
      unconditional: true,
      revert(template) {
        const found = clusterSettings(template);
        if (typeof found === 'string') return [found];
        if (found.settings[setting.key] !== undefined) {
          return [`${found.id} still emits ${setting.key}, so this branch has not retired the row`];
        }
        found.settings[setting.key] = structuredClone(setting.recorded);
        return [];
      },
    });
    differences.push({
      path: `Resources.${setting.id}.Properties.settings.${setting.key}`,
      cause: RETIRED_SETTING_CAUSE,
      reason: setting.reason,
      removeWhen: endsWhen,
    });
  }

  if (secret !== undefined) {
    causes.push({
      id: CLIENT_SECRET_CAUSE,
      change: `${secret.id} takes its value from the user pool client instead of from ${secret.source}`,
      unconditional: true,
      revert(template) {
        const getAtt = secretGetAtt(template, secret.id);
        if (typeof getAtt === 'string') return [getAtt];
        const source = getAtt[0];
        const resources = isObject(template.Resources) ? template.Resources : {};
        const referenced = typeof source === 'string' ? resources[source] : undefined;
        // The revert is held to the shape the change produces: any other value fails rather than
        // being turned into the recorded one.
        if (!isObject(referenced) || referenced.Type !== USER_POOL_CLIENT_TYPE) {
          return [`${secret.id} SecretString reads ${formatValue(source)}, which is not a ${USER_POOL_CLIENT_TYPE}`];
        }
        if (getAtt[1] !== CLIENT_SECRET_ATTRIBUTE) {
          return [`${secret.id} SecretString reads ${formatValue(getAtt[1])}, expected ${CLIENT_SECRET_ATTRIBUTE}`];
        }
        getAtt[0] = secret.source;
        return [];
      },
    });
    differences.push({
      path: `Resources.${secret.id}.Properties.SecretString.Fn::GetAtt[0]`,
      cause: CLIENT_SECRET_CAUSE,
      reason: `the secret holds the same value, read from the client that generated it rather than from ${secret.source}`,
      removeWhen: endsWhen,
    });
  }

  return {
    cluster,
    stack,
    summary: 'this branch retires the Lambda-backed resources this stack carried; the recorded template predates that',
    deployedInput: {},
    deployedInputReason: 'no settings row gates this change, so the deployed-input synthesis is the as-captured one',
    endsWhen,
    differences,
    causes,
  };
}

const CERTIFICATE_RETAIN_CAUSE = 'self-signed-certificate-retain';
const CERTIFICATE_ARN_CAUSE = 'certificate-arn-from-settings';
const CERTIFICATE_TYPE_PREFIX = 'Custom::SelfSignedCertificate';

/** The certificate custom resources the recorded template carries, with the policies it records. */
function recordedCertificates(deployed: JsonObject): Array<{ id: string; type: string; policies: Record<string, Json | undefined> }> {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const out: Array<{ id: string; type: string; policies: Record<string, Json | undefined> }> = [];
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource) || typeof resource.Type !== 'string') continue;
    if (!resource.Type.startsWith(CERTIFICATE_TYPE_PREFIX)) continue;
    out.push({
      id,
      type: resource.Type,
      policies: { DeletionPolicy: resource.DeletionPolicy, UpdateReplacePolicy: resource.UpdateReplacePolicy },
    });
  }
  return out;
}

/** The `module_id` the settings resource writes its rows under, which prefixes every row key. */
function settingsModuleId(template: JsonObject): string | undefined {
  const resources = isObject(template.Resources) ? template.Resources : {};
  for (const resource of Object.values(resources)) {
    if (!isObject(resource) || resource.Type !== 'Custom::ClusterSettings') continue;
    const properties = isObject(resource.Properties) ? resource.Properties : undefined;
    const moduleId = properties?.module_id;
    return typeof moduleId === 'string' ? moduleId : undefined;
  }
  return undefined;
}

/**
 * The settings row each certificate attribute is published as, read off the recorded template's
 * own settings resource. That is what makes the expected literal derivable: the row the stack
 * emitted the attribute to is the row the synthesis now reads it from.
 */
function certificateRowKeys(deployed: JsonObject, certificates: ReadonlySet<string>): Map<string, string> {
  const rows = new Map<string, string>();
  const found = clusterSettings(deployed);
  const moduleId = settingsModuleId(deployed);
  if (typeof found === 'string' || moduleId === undefined) return rows;
  for (const [key, value] of Object.entries(found.settings)) {
    if (!isObject(value) || !Array.isArray(value['Fn::GetAtt'])) continue;
    const [id, attribute] = value['Fn::GetAtt'];
    if (typeof id !== 'string' || !certificates.has(id)) continue;
    rows.set(`${id}|${String(attribute)}`, `${moduleId}.${key}`);
  }
  return rows;
}

interface CertificateReference {
  path: string;
  certificateId: string;
  attribute: string;
  /** The synthesized value at the same place, when a synthesized template was walked. */
  actual: Json | undefined;
  /** Writes the recorded `Fn::GetAtt` back into the synthesized template. */
  put(value: Json): void;
}

/**
 * Every place the recorded template reads an attribute off a certificate resource, walked from the
 * recorded template because that is the oracle, with the synthesized template alongside so one
 * walk both itemises the differences and reverts them.
 */
function eachCertificateReference(
  recorded: Json | undefined,
  actual: Json | undefined,
  put: (value: Json) => void,
  path: string,
  certificates: ReadonlySet<string>,
  visit: (reference: CertificateReference) => void,
): void {
  if (Array.isArray(recorded)) {
    for (const [index, item] of recorded.entries()) {
      const element = Array.isArray(actual) ? actual[index] : undefined;
      eachCertificateReference(
        item,
        element,
        (value) => {
          if (Array.isArray(actual)) actual[index] = value;
        },
        `${path}[${index}]`,
        certificates,
        visit,
      );
    }
    return;
  }
  if (!isObject(recorded)) return;
  const getAtt = recorded['Fn::GetAtt'];
  if (Array.isArray(getAtt) && typeof getAtt[0] === 'string' && certificates.has(getAtt[0])) {
    visit({ path, certificateId: getAtt[0], attribute: String(getAtt[1]), actual, put });
    return;
  }
  for (const [key, value] of Object.entries(recorded)) {
    const parent = isObject(actual) ? actual : undefined;
    eachCertificateReference(
      value,
      parent?.[key],
      (next) => {
        if (parent !== undefined) parent[key] = next;
      },
      `${path}.${key}`,
      certificates,
      visit,
    );
  }
}

/** Walks the properties of every resource and the outputs, which is what the comparison covers. */
function walkCertificateReferences(
  deployed: JsonObject,
  actual: JsonObject | undefined,
  certificates: ReadonlySet<string>,
  visit: (reference: CertificateReference) => void,
): void {
  const resources = isObject(deployed.Resources) ? deployed.Resources : {};
  const actualResources = actual !== undefined && isObject(actual.Resources) ? actual.Resources : undefined;
  for (const [id, resource] of Object.entries(resources)) {
    if (!isObject(resource)) continue;
    const actualResource = actualResources !== undefined && isObject(actualResources[id]) ? (actualResources[id] as JsonObject) : undefined;
    eachCertificateReference(
      resource.Properties,
      actualResource?.Properties,
      (value) => {
        if (actualResource !== undefined) actualResource.Properties = value;
      },
      `Resources.${id}.Properties`,
      certificates,
      visit,
    );
  }
  eachCertificateReference(
    deployed.Outputs,
    actual?.Outputs,
    (value) => {
      if (actual !== undefined) actual.Outputs = value;
    },
    'Outputs',
    certificates,
    visit,
  );
}

/**
 * The drift a stack has because the deploy tool generates the self-signed certificates now.
 *
 * Two changes. The certificate custom resources stay for one release, defused: a Node handler that
 * only finds and returns, and `Retain` so nothing can destroy a certificate in service. And every
 * value that was an attribute of one of those resources is the settings row the tool published
 * instead, which is the same string the deployed stack resolved the attribute to.
 *
 * Both are read from the recorded template, and the expected literal for each reference comes from
 * the captured settings row the recorded template itself named. Unconditional, like the retain and
 * node-handler entries.
 */
function certificateDrift(cluster: string, stack: string, deployed: JsonObject): IntendedDrift | undefined {
  const recorded = recordedCertificates(deployed);
  if (recorded.length === 0) return undefined;
  const certificates = new Set(recorded.map((entry) => entry.id));
  const rowKeys = certificateRowKeys(deployed, certificates);
  const endsWhen = `the ${stack} stack is deployed to ${cluster} from this branch, which puts these values in the recorded template`;

  const causes: DriftCause[] = [];
  const differences: IntendedDifference[] = [];

  causes.push({
    id: CERTIFICATE_RETAIN_CAUSE,
    change: `the ${recorded.length} certificate custom resource(s) carry DeletionPolicy and UpdateReplacePolicy Retain`,
    unconditional: true,
    revert(template) {
      const resources = isObject(template.Resources) ? template.Resources : {};
      const failures: string[] = [];
      for (const entry of recorded) {
        const resource = resources[entry.id];
        if (!isObject(resource)) {
          failures.push(`${entry.id} is in the recorded template and not in the synthesized one`);
          continue;
        }
        for (const [policy, value] of Object.entries(entry.policies)) {
          if (resource[policy] !== 'Retain') {
            failures.push(`${entry.id} ${policy} is ${formatValue(resource[policy])}, expected Retain`);
          }
          if (value === undefined) delete resource[policy];
          else resource[policy] = structuredClone(value);
        }
      }
      return failures;
    },
  });
  for (const entry of recorded) {
    for (const policy of Object.keys(entry.policies)) {
      differences.push({
        path: `Resources.${entry.id}.${policy}`,
        cause: CERTIFICATE_RETAIN_CAUSE,
        reason:
          `the ${entry.type} names secrets a load balancer or a running host is serving, and the recorded ` +
          `value ${formatValue(entry.policies[policy])} runs its Delete handler`,
        removeWhen: endsWhen,
      });
    }
  }

  const references: CertificateReference[] = [];
  walkCertificateReferences(deployed, undefined, certificates, (reference) => references.push(reference));
  if (references.length > 0) {
    causes.push({
      id: CERTIFICATE_ARN_CAUSE,
      change: `${references.length} value(s) read a settings row the deploy tool published instead of a certificate resource attribute`,
      unconditional: true,
      revert(template, settings) {
        const failures: string[] = [];
        walkCertificateReferences(deployed, template, certificates, (reference) => {
          const rowKey = rowKeys.get(`${reference.certificateId}|${reference.attribute}`);
          if (rowKey === undefined) {
            failures.push(
              `${reference.path} reads ${reference.certificateId}.${reference.attribute}, which the recorded settings do not publish`,
            );
            return;
          }
          const expected = settings(rowKey);
          if (expected === undefined) {
            failures.push(`${reference.path}: the captured settings have no ${rowKey} row`);
            return;
          }
          if (reference.actual !== expected) {
            failures.push(`${reference.path} is ${formatValue(reference.actual)}, expected the ${rowKey} row ${formatValue(expected)}`);
            return;
          }
          reference.put({ 'Fn::GetAtt': [reference.certificateId, reference.attribute] });
        });
        return failures;
      },
    });
    for (const reference of references) {
      const rowKey = rowKeys.get(`${reference.certificateId}|${reference.attribute}`);
      differences.push({
        path: reference.path,
        cause: CERTIFICATE_ARN_CAUSE,
        reason:
          `the deploy tool generates or adopts the certificate and publishes ${String(rowKey)}, so the stack reads the ` +
          `same value from the configuration rather than from ${reference.certificateId}.${reference.attribute}`,
        removeWhen: endsWhen,
      });
    }
  }

  return {
    cluster,
    stack,
    summary: 'this branch generates the self-signed certificates in the deploy tool and retains the defused custom resources; the recorded template predates that',
    deployedInput: {},
    deployedInputReason: 'no settings row gates this change, so the deployed-input synthesis is the as-captured one',
    endsWhen,
    differences,
    causes,
  };
}

/**
 * The recorded template with every certificate attribute replaced by the settings row it was
 * published as: what a synthesis from this branch produces, derived from the oracle rather than
 * copied from the synthesis being checked. The stack tests hold their synthesis against this.
 */
export function withCertificateSettings(deployed: JsonObject, settings: SettingsLookup): JsonObject {
  const certificates = new Set(recordedCertificates(deployed).map((entry) => entry.id));
  const rows = certificateRowKeys(deployed, certificates);
  const copy = structuredClone(deployed);
  walkCertificateReferences(deployed, copy, certificates, (reference) => {
    const rowKey = rows.get(`${reference.certificateId}|${reference.attribute}`);
    const value = rowKey === undefined ? undefined : settings(rowKey);
    if (value !== undefined) reference.put(value);
  });
  return copy;
}

/** Both entries for one stack, as one. */
function mergeDrift(first: IntendedDrift, second: IntendedDrift): IntendedDrift {
  return {
    cluster: first.cluster,
    stack: first.stack,
    summary: `${first.summary}; ${second.summary}`,
    deployedInput: { ...first.deployedInput, ...second.deployedInput },
    deployedInputReason: first.deployedInputReason,
    endsWhen: `${first.endsWhen}; ${second.endsWhen}`,
    differences: [...first.differences, ...second.differences],
    causes: [...first.causes, ...second.causes],
  };
}

/**
 * The entry for one comparison, or undefined when the stack has no drift at all.
 *
 * `deployed` is the recorded reference template: the unconditional retain entry is derived from
 * it, so the expectation comes from the oracle rather than from the code that produces the change.
 * Omitting it leaves only the recorded settings-gated entries, which is what a comparison against
 * a substitute app wants: a substitute app is not this app, so this app's unconditional changes
 * are not expected of it.
 */
export function intendedDriftFor(cluster: string, stack: string, deployed?: JsonObject): IntendedDrift | undefined {
  const entries = [
    DRIFT.find((entry) => entry.cluster === cluster && entry.stack === stack),
    deployed === undefined ? undefined : retainStatefulDrift(cluster, stack, deployed),
    deployed === undefined ? undefined : nodeHandlerDrift(cluster, stack, deployed),
    deployed === undefined ? undefined : retirementDrift(cluster, stack, deployed),
    deployed === undefined ? undefined : certificateDrift(cluster, stack, deployed),
  ].filter((entry): entry is IntendedDrift => entry !== undefined);
  return entries.length === 0 ? undefined : entries.reduce(mergeDrift);
}

/** A captured table dump and its rows. */
function scanRows(scanFile: string): { scan: JsonObject; items: Json[] } {
  const parsed: Json = JSON.parse(readFileSync(scanFile, 'utf8'));
  if (!isObject(parsed)) throw new Error(`${scanFile}: expected a table scan object`);
  return { scan: parsed, items: Array.isArray(parsed.Items) ? parsed.Items : [] };
}

/** The `key` attribute of one row, when it is a string. */
function rowKey(item: Json): string | undefined {
  return isObject(item) && isObject(item.key) && typeof item.key.S === 'string' ? item.key.S : undefined;
}

/** Every string-valued row of a captured table dump. */
export function settingsLookup(scanFile: string): SettingsLookup {
  const values = new Map<string, string>();
  for (const item of scanRows(scanFile).items) {
    const key = rowKey(item);
    const value = isObject(item) && isObject(item.value) && typeof item.value.S === 'string' ? item.value.S : undefined;
    if (key !== undefined && value !== undefined) values.set(key, value);
  }
  return (key) => values.get(key);
}

/**
 * Writes a copy of the captured table dump with the deployed-input rows put back, so the
 * comparison against the recorded template is driven by the inputs that produced it.
 */
export function writeDeployedInputSettings(scanFile: string, overrides: Record<string, boolean | string>, outFile: string): void {
  const attribute = (value: boolean | string): Json => (typeof value === 'boolean' ? { BOOL: value } : { S: value });
  const { scan, items } = scanRows(scanFile);
  const remaining = new Set(Object.keys(overrides));
  for (const item of items) {
    const key = rowKey(item);
    if (key === undefined || !remaining.delete(key) || !isObject(item)) continue;
    item.value = attribute(overrides[key]!);
  }
  for (const key of remaining) {
    items.push({ key: { S: key }, value: attribute(overrides[key]!), version: { N: '1' } });
  }
  scan.Items = items;
  writeFileSync(outFile, JSON.stringify(scan));
}

export interface IntendedDriftInput {
  drift: IntendedDrift;
  /** The recorded reference template, still the oracle. */
  deployed: JsonObject;
  /** Synthesized from the settings as captured. */
  asCaptured: JsonObject;
  /** Synthesized from the settings the recorded template was generated from. */
  deployedInputSynth: JsonObject;
  settings: SettingsLookup;
  ignoreVersion?: boolean;
}

/**
 * The deployed-input synthesis with every unconditional cause undone: what the recorded template
 * is the oracle for, and therefore what the plain comparison runs against.
 *
 * Every reverted value is checked against its recorded shape on the way, so this cannot turn a
 * changed value into a passing one; it appends to `failures` when it does not find what the
 * recorded template says is there.
 */
export function unconditionalBaseline(
  drift: IntendedDrift,
  deployedInputSynth: JsonObject,
  settings: SettingsLookup,
  failures: string[] = [],
): JsonObject {
  const baseline = structuredClone(deployedInputSynth);
  for (const cause of drift.causes) {
    if (cause.unconditional !== true) continue;
    failures.push(...cause.revert(baseline, settings).map((failure) => `${cause.id} on the deployed-input synthesis: ${failure}`));
  }
  return baseline;
}

function failuresFrom(prefix: string, report: ReturnType<typeof compareTemplates>): string[] {
  return [
    ...report.missing.map((id) => `${prefix}: missing resource ${id}`),
    ...report.extra.map((id) => `${prefix}: extra resource ${id}`),
    ...report.hard.map((d) => `${prefix}: ${d.path} ${formatValue(d.live)} -> ${formatValue(d.synth)}`),
  ];
}

/**
 * Runs all three checks. An empty result is the only pass.
 *
 * 1. The deployed-input synthesis, with every unconditional cause undone, matches the recorded
 *    template with no tolerance.
 * 2. The as-captured synthesis differs from the recorded template in exactly the itemised set.
 * 3. Undoing every cause turns the as-captured synthesis back into that same baseline, with every
 *    reverted value checked against its recorded shape.
 *
 * An unconditional cause is in both syntheses, so it is undone in both. It is still named, valued
 * and reverted exactly like a settings-gated one; the only difference is which templates carry it.
 */
export function checkIntendedDrift(input: IntendedDriftInput): string[] {
  const { drift, deployed, asCaptured, deployedInputSynth, settings, ignoreVersion = false } = input;
  const failures: string[] = [];

  const baseline = unconditionalBaseline(drift, deployedInputSynth, settings, failures);

  const port = compareTemplates(deployed, baseline, ignoreVersion);
  if (!isParity(port)) failures.push(...failuresFrom('deployed-input synthesis differs from the recorded template', port));

  const observed = compareTemplates(deployed, asCaptured, ignoreVersion);
  const declared = new Set(drift.differences.map((difference) => difference.path));
  // A retirement removes a whole resource, so a difference may name one: `Resources.<id>`. It is
  // itemised, valued and reverted exactly like a property difference, and a resource the entry
  // does not name still fails.
  const seen = new Set([
    ...observed.hard.map((difference) => difference.path),
    ...observed.missing.map((id) => `Resources.${id}`),
    ...observed.extra.map((id) => `Resources.${id}`),
  ]);
  for (const id of observed.missing) {
    if (declared.has(`Resources.${id}`)) continue;
    failures.push(`as-captured synthesis is missing resource ${id}`);
  }
  for (const id of observed.extra) {
    if (declared.has(`Resources.${id}`)) continue;
    failures.push(`as-captured synthesis has extra resource ${id}`);
  }
  for (const difference of observed.hard) {
    if (declared.has(difference.path)) continue;
    failures.push(
      `unexpected difference, not in the itemised set: ${difference.path} ${formatValue(difference.live)} -> ${formatValue(difference.synth)}`,
    );
  }
  for (const path of declared) {
    if (!seen.has(path)) failures.push(`itemised difference no longer produced, remove the entry: ${path}`);
  }

  const reverted = structuredClone(asCaptured);
  for (const cause of drift.causes) {
    failures.push(...cause.revert(reverted, settings).map((failure) => `${cause.id}: ${failure}`));
  }
  const residue = compareTemplates(baseline, reverted, ignoreVersion);
  failures.push(...failuresFrom('after undoing every cause the templates still differ', residue));
  failures.push(
    ...residue.soft.map((d) => `after undoing every cause a volatile value still differs: ${d.path} ${formatValue(d.live)} -> ${formatValue(d.synth)}`),
  );

  return failures;
}

/** The itemised expectation, printed so the gate names what it accepted instead of hiding it. */
export function formatIntendedDrift(drift: IntendedDrift): string {
  const lines = [
    `INTENDED DRIFT  ${drift.cluster} ${drift.stack}: ${drift.differences.length} differences from the recorded template`,
    `  ${drift.summary}`,
    `  deployed input: ${Object.entries(drift.deployedInput)
      .map(([key, value]) => `${key}=${String(value)}`)
      .join(', ')}`,
    `    ${drift.deployedInputReason}`,
    `  ends when: ${drift.endsWhen}`,
  ];
  for (const cause of drift.causes) lines.push(`  cause ${cause.id}: ${cause.change}`);
  for (const difference of drift.differences) lines.push(`  ${difference.path}  (${difference.cause})`);
  lines.push('  each difference carries its reason and its removal condition in tools/parity/intended-drift.ts');
  return lines.join('\n');
}
