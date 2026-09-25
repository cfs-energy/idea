/**
 * Port of `app/cdk/cdk_invoker.py`: the argv the CDK CLI is spawned with for one module, plus the
 * deploy-time change-set guard.
 *
 * Two things are worth knowing before changing anything here.
 *
 * 1. The argv is a contract. Python built a shell string and let the shell split it; this builds
 *    the tokens directly and spawns without a shell. Every flag, its order, and the per-module
 *    `--output cdk.out.<module_id>` isolation are reproduced token for token, because a missing
 *    `-c bootstrap_package_uri` produces a stack that synthesizes and then boots hosts which
 *    cannot find their bootstrap package.
 *
 * 2. `deploy` never executes a change set it has not read. Every deploy runs
 *    `cdk deploy --method=prepare-change-set`, reads the change set with `DescribeChangeSet`,
 *    and refuses to execute when a change would replace or remove something that carries state.
 *    `--allow-replacement <logicalId>` is the only override and every override is printed.
 */

import { spawn as spawnProcess, spawnSync } from 'node:child_process';
import { retryDelayMs } from './aws-client-options.ts';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { STATEFUL_TYPE_PREFIXES, isStatefulType } from '../cdk/stateful.ts';
import { ClusterConfig, GeneralException, type ModuleInfo, type TableScanner } from '../config/cluster-config.ts';
import type { ConfigEntry, ModuleSpec } from '../config/cluster-config-db.ts';
import type { CertificateDeps } from './certificates.ts';
import type { PrefixListApi } from './commands/utils.ts';
import {
  bootstrapPackagePlans,
  bootstrapPackageUri,
  buildAndUploadBootstrapPackage,
  releasePackageNames,
  releasePackageUri,
  type BootstrapPackagePlan,
} from './bootstrap-package.ts';
import { ideaVersion } from '../version.ts';

/** Modules the container stack runs as tasks, which therefore need no host packages. */
const CONTAINER_SERVED_MODULES = ['cluster-manager', 'scheduler', 'virtual-desktop-controller', 'bastion-host'];

// ---------------------------------------------------------------------------------------------
// host filesystem layout (`app_props.py`)
// ---------------------------------------------------------------------------------------------

/** `~/.idea`, or `IDEA_USER_HOME`. */
export function ideaUserHome(): string {
  return process.env.IDEA_USER_HOME ?? join(homedir(), '.idea');
}

function ensureDir(path: string): string {
  mkdirSync(path, { recursive: true });
  return path;
}

export function clusterRegionDir(clusterName: string, awsRegion: string, create = true): string {
  const dir = join(ideaUserHome(), 'clusters', clusterName, awsRegion);
  return create ? ensureDir(dir) : dir;
}

export function clusterConfigDir(clusterName: string, awsRegion: string, create = true): string {
  const dir = join(clusterRegionDir(clusterName, awsRegion, create), 'config');
  return create ? ensureDir(dir) : dir;
}

export function clusterCdkDir(clusterName: string, awsRegion: string): string {
  return ensureDir(join(clusterRegionDir(clusterName, awsRegion), '_cdk'));
}

export function clusterDeploymentsDir(clusterName: string, awsRegion: string): string {
  return ensureDir(join(clusterRegionDir(clusterName, awsRegion), 'deployments'));
}

export function valuesFilePath(clusterName: string, awsRegion: string): string {
  return join(clusterRegionDir(clusterName, awsRegion), 'values.yml');
}

/** `ValuesDiff.get_values_file_s3_key()`. */
export const VALUES_FILE_S3_KEY = 'values/values.yml';

/** `~/.idea/downloads`: where the image leaves the release archives. */
export function downloadsDir(): string {
  return ensureDir(join(ideaUserHome(), 'downloads'));
}

/**
 * The bundled CDK CLI. `IDEA_CDK_BIN` wins, then the CLI's own `node_modules`, then the image's
 * `~/.idea/lib/idea-cdk` layout.
 */
export function cdkBin(): string {
  // An explicit override is taken as given: the image and the tests both point at a path this
  // process has no business second-guessing.
  const override = process.env.IDEA_CDK_BIN;
  if (override !== undefined && override !== '') return override;
  const candidates = [
    fileURLToPath(new URL('../../node_modules/aws-cdk/bin/cdk', import.meta.url)),
    fileURLToPath(new URL('../../../node_modules/aws-cdk/bin/cdk', import.meta.url)),
    join(ideaUserHome(), 'lib', 'idea-cdk', 'node_modules', 'aws-cdk', 'bin', 'cdk'),
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (found === undefined) {
    throw new GeneralException(
      `Unable to find cdk binary at: ${candidates.join(', ')}. Please ensure ideactl is installed correctly.`,
    );
  }
  return found;
}

/** `cdk.json` is copied into the cluster's `_cdk` directory the first time it is needed. */
export function setupClusterCdkDir(clusterName: string, awsRegion: string): string {
  const cdkHome = clusterCdkDir(clusterName, awsRegion);
  const cdkJson = join(cdkHome, 'cdk.json');
  if (!existsSync(cdkJson)) {
    const template = [
      fileURLToPath(new URL('../../cdk.json', import.meta.url)),
      fileURLToPath(new URL('../../../cdk.json', import.meta.url)),
    ].find((candidate) => existsSync(candidate));
    if (template !== undefined) {
      writeFileSync(cdkJson, readFileSync(template));
    }
  }
  return cdkHome;
}

// ---------------------------------------------------------------------------------------------
// injected effects
// ---------------------------------------------------------------------------------------------

/** One child process. Resolves with the exit code; it never throws on a non-zero exit. */
export type Spawn = (
  argv: string[],
  options: { cwd: string; env: Record<string, string | undefined>; onStderr?: (chunk: string) => void },
) => Promise<number>;

/** Error text from Node, the SDK or the CDK toolkit that means the network, not the request, failed. */
export const NETWORK_FAILURE = /\b(ENETUNREACH|EHOSTUNREACH|EADDRNOTAVAIL|ENOTFOUND|EAI_AGAIN|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EPIPE|socket hang up)\b/;

/** Runs of one CDK command, first included. The command is create-change-set, synth or diff, all repeatable. */
export const CDK_RUN_ATTEMPTS = 6;

/** Streams the CDK CLI's output to this process's stdio, as `exec_shell` does. */
export const liveSpawn: Spawn = (argv, options) =>
  new Promise((resolve, reject) => {
    const invocation = options.env.IDEA_SEA === '1' && argv[0] === process.execPath
      ? [process.execPath, '__ideactl_internal_cdk__', ...argv.slice(1)] : argv;
    const [command, ...args] = invocation;
    if (command === undefined) throw new GeneralException('empty argv');
    const child = spawnProcess(command, args, {
      cwd: options.cwd, env: options.env, stdio: ['inherit', 'inherit', options.onStderr ? 'pipe' : 'inherit'],
    });
    child.stderr?.on('data', (chunk: Buffer) => {
      process.stderr.write(chunk);
      options.onStderr?.(chunk.toString());
    });
    child.on('error', reject);
    child.on('close', (code) => resolve(code ?? 1));
  });

/** One property-level detail of a change, as `DescribeChangeSet` reports it. */
export interface ResourceChangeDetail {
  Target?: { Attribute?: string; Name?: string; RequiresRecreation?: string };
  ChangeSource?: string;
  CausingEntity?: string;
}

/** One change in a `DescribeChangeSet` response, narrowed to the fields the guard reads. */
export interface ResourceChange {
  Action?: string;
  LogicalResourceId?: string;
  PhysicalResourceId?: string;
  ResourceType?: string;
  Replacement?: string;
  Details?: ResourceChangeDetail[];
}

export interface ChangeSetDescription {
  Status?: string;
  StatusReason?: string;
  ExecutionStatus?: string;
  Changes?: Array<{ ResourceChange?: ResourceChange }>;
  NextToken?: string;
}

export interface StackDescription {
  Tags?: Array<{ Key?: string; Value?: string }>;
  StackStatus?: string;
  StackStatusReason?: string;
  Outputs?: Array<{ OutputKey?: string; OutputValue?: string }>;
}

/**
 * The CloudFormation reads and the one write the guard needs. A functional interface rather than a
 * client so the tests drive every branch from fixtures, in the style of `SynthReads`.
 */
export interface CloudFormationApi {
  describeChangeSet(input: { StackName: string; ChangeSetName: string; NextToken?: string }): Promise<ChangeSetDescription>;
  /** The deployed template body, so the guard can read each removed resource's DeletionPolicy. */
  getTemplate?(stackName: string): Promise<string | undefined>;
  /** Update a stack from a template body the caller edited, keeping every parameter's previous value. */
  updateStack?(input: { StackName: string; TemplateBody: string; ParameterKeys: readonly string[] }): Promise<void>;
  executeChangeSet(input: {
    StackName: string;
    ChangeSetName: string;
    /** False tells CloudFormation to restore the last stable state when execution fails. */
    DisableRollback: boolean;
  }): Promise<void>;
  describeStack(stackName: string): Promise<StackDescription>;
}

/** The two S3 calls the CLI makes on its own behalf. */
export interface S3Api {
  putObject(input: { Bucket: string; Key: string; Body: Uint8Array | string }): Promise<void>;
  getObject(input: { Bucket: string; Key: string }): Promise<string>;
}

/** `ClusterConfigDb`, structurally, so a test can record writes without DynamoDB. */
export interface ConfigWriter {
  syncModulesInDb(modules: ModuleSpec[]): Promise<void>;
  syncClusterSettingsInDb(entries: ConfigEntry[], overwrite?: boolean, source?: 'cli' | 'template'): Promise<void>;
  setConfigEntry(key: string, value: unknown): Promise<void>;
  deleteConfigEntries(configKeyPrefix: string): Promise<void>;
}

export interface ConfigWriterOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  dynamodbKmsKeyId?: string | null;
  createDatabase?: boolean;
}

export interface PromptChoice {
  message: string;
  /** Present for a three-way prompt; absent for a yes/no. */
  choices?: string[];
  default?: string | boolean;
}

/**
 * Everything the CLI does that is not a pure function. Tests build a partial and pass it in, so no
 * command path needs credentials, a network, or a real CDK CLI.
 */
export interface Deps {
  spawn: Spawn;
  cfn: CloudFormationApi;
  s3: S3Api;
  /** `<cluster>.cluster-settings` / `.modules` scanner. */
  scan: TableScanner;
  configWriter(options: ConfigWriterOptions): Promise<ConfigWriter>;
  /** `sts:GetCallerIdentity`, only for the cluster-bucket name fallback. */
  accountId(): Promise<string>;
  /** Identity used by a command, resolved before the command touches its account. */
  callerIdentity?(options: {
    awsRegion: string;
    awsProfile?: string;
  }): Promise<{ account: string; arn: string }>;
  /** Read-only effective ECS account settings used by the ECS deployment prerequisite. */
  ecsAccountSettings?: {
    listAccountSettings(input: {
      awsRegion: string;
      effectiveSettings: true;
      name: string;
    }): Promise<Array<{ name: string; value: string }>>;
  };
  /**
   * Instance termination protection. A change set that replaces a protected instance creates the
   * new one and then fails to delete the old one, and CloudFormation still reports the update as
   * a success, so the old instance runs on unreferenced. The protection is cleared before the
   * change set executes; a deploy without this hook says so instead.
   */
  instanceProtection?: {
    isProtected(input: { awsRegion: string; instanceId: string }): Promise<boolean>;
    setProtected(input: { awsRegion: string; instanceId: string; protected: boolean }): Promise<void>;
  };
  /** HTTPS GET returning the status code, or 0 when the request failed. */
  httpStatus(url: string): Promise<number>;
  sleep(ms: number): Promise<void>;
  now(): number;
  uuid(): string;
  out(line: string): void;
  err(line: string): void;
  prompt(choice: PromptChoice): Promise<string | boolean>;
  /**
   * The bootstrap template context for one host module. `BootstrapContext` has not been ported
   * yet, so a deploy of a module that needs a rendered bootstrap package requires this hook.
   */
  bootstrapContext?(input: BootstrapContextInput): object;
  /** Root of the `idea-bootstrap` source tree; defaults to the packaged copy. */
  bootstrapSourceDir?: string;
  /**
   * Managed prefix-list reads and writes. The cluster stack creates the prefix list and the
   * deploy merges the configured client addresses into it once the list id is readable.
   */
  prefixList?: PrefixListApi;
  /**
   * Secrets Manager, ACM and `openssl`, for the self-signed certificates the deploy generates
   * before the stacks that read their ARNs synthesize.
   */
  certificates?: CertificateDeps;
}

export interface BootstrapContextInput {
  moduleName: string;
  moduleId: string;
  moduleSet: string;
  baseOs: string;
  instanceType: string;
  plan: BootstrapPackagePlan;
  /** Release archives already uploaded for this module, keyed by archive name. */
  releasePackageUris: Record<string, string>;
  config: ClusterConfig;
}

// ---------------------------------------------------------------------------------------------
// change-set guard
// ---------------------------------------------------------------------------------------------

/**
 * How to re-invoke this tool as the synthesis app. Prefers the name on the path, because that is
 * what runs inside the image and from the released artifact, and falls back to this process's own
 * runtime and entry point so a checkout works without installing anything.
 */
function cdkAppInvocation(): string {
  if (process.env.IDEA_SEA === '1') return `"${process.execPath}"`;
  const onPath = spawnSync('sh', ['-c', 'command -v ideactl'], { encoding: 'utf8' });
  if (onPath.status === 0 && onPath.stdout.trim() !== '') return 'ideactl';
  const entry = process.argv[1];
  if (entry === undefined || entry === '') return 'ideactl';
  return `${JSON.stringify(process.execPath)} ${JSON.stringify(entry)}`;
}

/** The change set `cdk deploy --method=prepare-change-set` leaves on the stack. */
export const CDK_DEPLOY_CHANGE_SET_NAME = 'cdk-deploy-change-set';

/**
 * The change-set guard's notion of stateful is the synthesis's notion of stateful: one list, in
 * `src/cdk/stateful.ts`, so the layer that refuses to remove these and the layer that marks them
 * Retain on update-replace cannot come to disagree.
 */
export { STATEFUL_TYPE_PREFIXES, isStatefulType };

export function isCustomResourceType(resourceType: string | undefined): boolean {
  return resourceType !== undefined && resourceType.startsWith('Custom::');
}

export type RefusalClass = 'replacement' | 'custom-resource-remove' | 'stateful-remove';

export interface ChangeSetFinding {
  logicalId: string;
  resourceType: string;
  action: string;
  refusal: RefusalClass;
  reason: string;
}

export interface ChangeSetVerdict {
  /** Findings that were not overridden. A non-empty list means the deploy is refused. */
  refusals: ChangeSetFinding[];
  /** Findings an allow entry let through. Every one of these is printed. */
  allowed: Array<ChangeSetFinding & { allowedBy: string }>;
  /** True when the change set needs no execution. */
  empty: boolean;
}

/**
 * The analytics dashboard target group carries a fresh uuid in its `Name` on every synth, so
 * CloudFormation replaces it on every analytics deploy. Python did the same thing; refusing it
 * would refuse every analytics deploy and teach operators to pass `--allow-replacement` blindly.
 * It is scoped to that one logical ID, that one resource type, and it is printed like any other
 * override.
 */
/**
 * Custom resource types this release retires from every stack. Removing one sends Delete to the
 * Python handler still deployed on the cluster, and each of these answers SUCCESS without acting
 * (`update_cluster_prefix_list` "will not remove IP addresses from the cluster prefix list";
 * `get_user_pool_client_secret` returns at once on Delete), so the removal is safe on every
 * cluster and no operator is asked to override it. Empty this set once every cluster has
 * deployed this release.
 */
export const RETIRED_CUSTOM_RESOURCE_TYPES: ReadonlySet<string> = new Set([
  'Custom::ClusterPrefixList',
  'Custom::GetOAuthCredentials',
]);

/**
 * Resource types CloudFormation "replaces" on every routine change because they are immutable
 * revisions: a new task definition is a new revision, the service rolls to it, and the previous
 * revision stays ACTIVE under its Retain policy for a rollback. Nothing is lost, and refusing it
 * would refuse every image upgrade.
 */
export const REVISIONED_TYPES: ReadonlySet<string> = new Set(['AWS::ECS::TaskDefinition']);

export function builtInAllowedReplacements(clusterName: string): Map<string, string> {
  const dashboardTargetGroup = `${clusterName.replace(/-/g, '')}dashboardtargetgroup`;
  return new Map([[dashboardTargetGroup, 'AWS::ElasticLoadBalancingV2::TargetGroup']]);
}

/**
 * CloudFormation reports Replacement=Conditional when a recreation-capable property takes its value
 * from another resource's attribute (`Endpoint: !GetAtt queue.Arn`), because at plan time it cannot
 * know whether that attribute changes. The change set does know whether that resource is replaced.
 * When every recreation-capable detail is such an attribute of a resource this change set leaves in
 * place (absent from the set, or modified without replacement), the value cannot change and neither
 * can the resource. Anything else, a direct edit of a recreating property, a causing resource that is
 * added or itself replaced, or no property detail at all, stays a refusal.
 */
export function conditionalOnUnreplacedAttributes(change: ResourceChange, changes: readonly ResourceChange[]): boolean {
  const recreating = (change.Details ?? []).filter(
    (detail) => detail.Target?.RequiresRecreation !== undefined && detail.Target.RequiresRecreation !== 'Never',
  );
  if (recreating.length === 0) return false;
  const byId = new Map(changes.map((entry) => [entry.LogicalResourceId, entry]));
  return recreating.every((detail) => {
    if (detail.ChangeSource !== 'ResourceAttribute' || detail.CausingEntity === undefined) return false;
    const cause = byId.get(detail.CausingEntity.split('.')[0] ?? '');
    return cause === undefined || (cause.Action === 'Modify' && cause.Replacement === 'False');
  });
}

/**
 * Logical IDs whose deployed definition carries `DeletionPolicy: Retain`. CDK templates are JSON;
 * anything unparsable yields the empty set, which is the conservative reading.
 */
export function retainedResources(templateBody: string | undefined): Set<string> {
  const retained = new Set<string>();
  if (templateBody === undefined) return retained;
  let template: unknown;
  try {
    template = JSON.parse(templateBody);
  } catch {
    return retained;
  }
  const resources = (template as { Resources?: Record<string, { DeletionPolicy?: unknown }> } | null)?.Resources ?? {};
  for (const [logicalId, resource] of Object.entries(resources)) {
    if (resource?.DeletionPolicy === 'Retain') retained.add(logicalId);
  }
  return retained;
}

/** True when CloudFormation created the change set but found nothing to do. */
export function isEmptyChangeSet(description: ChangeSetDescription): boolean {
  if (description.Status !== 'FAILED') return false;
  const reason = description.StatusReason ?? '';
  return /didn't contain changes|No updates are to be performed/i.test(reason);
}

/**
 * Classifies every change in a change set. Pure: the caller decides what to do with the verdict.
 *
 * `allowReplacement` holds the logical IDs given on the command line. `builtIn` holds the
 * logical ID -> required resource type pairs from `builtInAllowedReplacements`.
 * `allowReplacementOfType` holds the one resource type the replace verb was pointed at, against
 * the component name the operator typed, so the allowance is printed in the operator's words.
 */
export function evaluateChangeSet(
  description: ChangeSetDescription,
  allowReplacement: readonly string[] = [],
  builtIn: ReadonlyMap<string, string> = new Map(),
  allowReplacementOfType: ReadonlyMap<string, string> = new Map(),
  retainedByPolicy: ReadonlySet<string> = new Set(),
): ChangeSetVerdict {
  const changes = description.Changes ?? [];
  const verdict: ChangeSetVerdict = { refusals: [], allowed: [], empty: isEmptyChangeSet(description) };
  const explicit = new Set(allowReplacement);
  const allChanges = changes.flatMap((change) => (change.ResourceChange === undefined ? [] : [change.ResourceChange]));

  for (const change of changes) {
    const resourceChange = change.ResourceChange;
    if (resourceChange === undefined) continue;
    const logicalId = resourceChange.LogicalResourceId ?? '<unknown>';
    const resourceType = resourceChange.ResourceType ?? '<unknown>';
    const action = resourceChange.Action ?? '<unknown>';

    const findings: ChangeSetFinding[] = [];
    if (resourceChange.Replacement === 'True') {
      findings.push({
        logicalId,
        resourceType,
        action,
        refusal: 'replacement',
        reason: `${action} of ${logicalId} (${resourceType}) replaces the resource`,
      });
    } else if (resourceChange.Replacement === 'Conditional' && isStatefulType(resourceType)) {
      // CloudFormation says Conditional when whether it replaces depends on values it will only
      // know at execution time. On anything stateless that is noise. On a stateful resource it is
      // a coin toss with the data on one side of it, so it is refused like a certain replacement,
      // unless the change set itself shows the coin has only one side (see
      // `conditionalOnUnreplacedAttributes`).
      if (conditionalOnUnreplacedAttributes(resourceChange, allChanges)) {
        verdict.allowed.push({
          logicalId,
          resourceType,
          action,
          refusal: 'replacement',
          reason: `${action} of ${logicalId} (${resourceType}) is Replacement=Conditional only through attributes of resources this change set does not replace`,
          allowedBy: 'attribute of an unreplaced resource',
        });
      } else {
        findings.push({
          logicalId,
          resourceType,
          action,
          refusal: 'replacement',
          reason: `${action} of ${logicalId} (${resourceType}) may replace the resource; CloudFormation reports Replacement=Conditional`,
        });
      }
    }
    if (action === 'Remove' && isCustomResourceType(resourceType)) {
      findings.push({
        logicalId,
        resourceType,
        action,
        refusal: 'custom-resource-remove',
        reason: `Remove of custom resource ${logicalId} (${resourceType}) runs its Delete handler`,
      });
    }
    if (action === 'Remove' && isStatefulType(resourceType)) {
      findings.push({
        logicalId,
        resourceType,
        action,
        refusal: 'stateful-remove',
        reason: `Remove of stateful resource ${logicalId} (${resourceType})`,
      });
    }

    for (const finding of findings) {
      const namedComponent = allowReplacementOfType.get(resourceType);
      if (explicit.has(logicalId)) {
        verdict.allowed.push({ ...finding, allowedBy: '--allow-replacement' });
      } else if (builtIn.get(logicalId) === resourceType) {
        verdict.allowed.push({ ...finding, allowedBy: 'built-in allow list' });
      } else if (finding.refusal === 'replacement' && REVISIONED_TYPES.has(resourceType)) {
        verdict.allowed.push({ ...finding, allowedBy: 'a new revision; the previous one is retained' });
      } else if (
        finding.refusal === 'stateful-remove' &&
        resourceType === 'AWS::Route53::RecordSet' &&
        retainedByPolicy.has(logicalId)
      ) {
        // The scheduler hands its existing DNS record to runtime management during cutover.
        // Retaining storage instead can leave applications attached to an empty replacement.
        verdict.allowed.push({ ...finding, allowedBy: 'DeletionPolicy Retain on the deployed resource' });
      } else if (finding.refusal === 'custom-resource-remove' && RETIRED_CUSTOM_RESOURCE_TYPES.has(resourceType)) {
        verdict.allowed.push({ ...finding, allowedBy: 'retired custom resource; its Delete handler is a no-op' });
      } else if (namedComponent !== undefined && finding.refusal === 'replacement') {
        // Only the replacement class, and only the one resource type the operator named. A remove
        // is a different intent and the replace verb never permits it.
        verdict.allowed.push({ ...finding, allowedBy: `replace ${namedComponent}` });
      } else {
        verdict.refusals.push(finding);
      }
    }
  }

  return verdict;
}

/** `errorcodes.CONFIG_ERROR`-shaped refusal, so `main` prints it red and exits non-zero. */
export class ChangeSetRefused extends Error {
  readonly verdict: ChangeSetVerdict;
  constructor(message: string, verdict: ChangeSetVerdict) {
    super(message);
    this.name = 'ChangeSetRefused';
    this.verdict = verdict;
  }
}

/** Raised where Python raises `SystemExit(code)`. */
export class ExitWithCode extends Error {
  readonly code: number;
  constructor(code: number, message = '') {
    super(message);
    this.name = 'ExitWithCode';
    this.code = code;
  }
}

const STACK_STATUS_OK = new Set(['CREATE_COMPLETE', 'UPDATE_COMPLETE', 'IMPORT_COMPLETE']);
const STACK_STATUS_IN_PROGRESS = /_IN_PROGRESS$/;
const STACK_WAIT_TIMEOUT_MS = 4 * 60 * 60_000;

// ---------------------------------------------------------------------------------------------
// the invoker
// ---------------------------------------------------------------------------------------------

export interface CdkInvokerOptions {
  clusterName: string;
  awsRegion: string;
  moduleId: string;
  moduleSet: string;
  awsProfile?: string;
  deploymentId?: string;
  terminationProtection?: boolean;
  rollback?: boolean;
  /** Logical IDs whose replacement or removal the operator has explicitly accepted. */
  allowReplacement?: readonly string[];
  /** Resource type -> the component name the replace verb was given. Empty for every other path. */
  allowReplacementOfType?: ReadonlyMap<string, string>;
  /** Poll interval while waiting for the executed change set. */
  pollIntervalMs?: number;
  deps: Deps;
  /** Set by `open()` from the modules table; `bootstrap` is its own name. */
  moduleName?: string;
  config?: ClusterConfig;
}

export class CdkInvoker {
  readonly clusterName: string;
  readonly awsRegion: string;
  readonly moduleId: string;
  readonly moduleName: string;
  readonly moduleSet: string;
  readonly awsProfile: string | undefined;
  readonly deploymentId: string;
  readonly terminationProtection: boolean;
  readonly rollback: boolean;
  readonly allowReplacement: readonly string[];
  readonly allowReplacementOfType: ReadonlyMap<string, string>;
  readonly deploymentDir: string;
  readonly cdkHome: string;
  private readonly pollIntervalMs: number;
  private readonly deps: Deps;
  private readonly config: ClusterConfig | undefined;

  constructor(options: CdkInvokerOptions) {
    this.clusterName = options.clusterName;
    this.awsRegion = options.awsRegion;
    this.moduleId = options.moduleId;
    this.moduleName = options.moduleName ?? options.moduleId;
    this.moduleSet = options.moduleSet;
    this.awsProfile = options.awsProfile;
    this.deploymentId = options.deploymentId ?? options.deps.uuid();
    this.terminationProtection = options.terminationProtection ?? true;
    this.rollback = options.rollback ?? true;
    this.allowReplacement = options.allowReplacement ?? [];
    this.allowReplacementOfType = options.allowReplacementOfType ?? new Map();
    this.pollIntervalMs = options.pollIntervalMs ?? 15_000;
    this.deps = options.deps;
    this.config = options.config;
    this.deploymentDir = ensureDir(join(clusterDeploymentsDir(this.clusterName, this.awsRegion), this.deploymentId));
    this.cdkHome = setupClusterCdkDir(this.clusterName, this.awsRegion);
  }

  /** Resolves the module name from the modules table, as `CdkInvoker.__init__` does. */
  static async open(options: CdkInvokerOptions): Promise<CdkInvoker> {
    if (options.moduleId === 'bootstrap') {
      return new CdkInvoker({ ...options, moduleName: 'bootstrap' });
    }
    const config =
      options.config ??
      (await ClusterConfig.fromDynamoDb(options.clusterName, options.awsRegion, {
        moduleSet: options.moduleSet,
        scan: options.deps.scan,
      }));
    const moduleInfo = config.moduleInfoById(options.moduleId);
    if (moduleInfo === undefined) {
      throw new GeneralException(`module not found for module_id: ${options.moduleId}`);
    }
    return new CdkInvoker({ ...options, moduleName: moduleInfo.name, config });
  }

  get stackName(): string {
    return `${this.clusterName}-${this.moduleId}`;
  }

  /** `get_cdk_app_cmd`: the `--app` re-entry the CDK CLI runs to synthesize one stack. */
  getCdkAppCmd(): string {
    const args = [
      '--cluster-name',
      this.clusterName,
      '--aws-region',
      this.awsRegion,
      '--module-id',
      this.moduleId,
      '--module-name',
      this.moduleName,
      '--deployment-id',
      this.deploymentId,
      '--termination-protection',
      String(this.terminationProtection),
    ];
    if (this.awsProfile !== undefined && this.awsProfile !== '') {
      args.push('--aws-profile', this.awsProfile);
    }
    // The toolkit runs this as a shell command, so it has to name something the shell can find.
    // Inside the image and from the released artifact that is the tool itself, on the path. From a
    // checkout it is not, so fall back to running this same entry point with the same runtime.
    return `${cdkAppInvocation()} cdk cdk-app ${args.join(' ')}`;
  }

  /**
   * `get_cdk_command`, tokenized. Python assembled a shell string and let the shell split it, so
   * `--rollback true` and `-c key=value` are two tokens each here.
   */
  getCdkCommand(name: string, params: readonly string[] = [], contextParams: Record<string, string> = {}): string[] {
    const argv = [cdkBin(), ...name.split(' '), ...params];
    if (name === 'deploy') argv.push('--rollback', String(this.rollback));
    if (this.awsProfile !== undefined && this.awsProfile !== '') argv.push('--profile', this.awsProfile);
    for (const [key, value] of Object.entries(contextParams)) argv.push('-c', `${key}=${value}`);
    // CDK CLI >= 2.1137 locks cdk.out during synth; scope it per module so parallel
    // --optimize-deployment runs do not collide on the shared cdk.out in the cluster _cdk directory.
    argv.push('--output', `cdk.out.${this.moduleId}`);
    return argv;
  }

  /** The `deploy` argv, including the two flags that force the change-set path. */
  getDeployArgv(contextParams: Record<string, string> = {}): string[] {
    return this.getCdkCommand(
      'deploy',
      [
        '--app',
        this.getCdkAppCmd(),
        '--outputs-file',
        this.outputsFile(),
        '--require-approval',
        'never',
        // The toolkit rejects the deprecated no-execute flag alongside a method, and this is the
        // method that means create the change set without executing it.
        '--method=prepare-change-set',
      ],
      contextParams,
    );
  }

  outputsFile(): string {
    return join(this.deploymentDir, `${this.moduleName}-outputs.json`);
  }

  private env(): Record<string, string | undefined> {
    const env = { ...process.env };
    // Keep the CDK's nodejs credential chain on the profile and region given on the command line.
    if (this.awsProfile !== undefined && this.awsProfile !== '') {
      env.AWS_PROFILE = this.awsProfile;
      env.AWS_DEFAULT_PROFILE = this.awsProfile;
    }
    if (this.awsRegion !== '') env.AWS_DEFAULT_REGION = this.awsRegion;
    return env;
  }

  /** `exec_shell`: run it in the cluster `_cdk` directory, non-zero exit becomes `SystemExit`. */
  async execCdk(argv: string[]): Promise<void> {
    this.deps.out(`shell> ${argv.join(' ')}`);
    for (let run = 1; ; run++) {
      let tail = '';
      const code = await this.deps.spawn(argv, {
        cwd: this.cdkHome,
        env: this.env(),
        onStderr: (chunk) => { tail = (tail + chunk).slice(-8192); },
      });
      if (code === 0) return;
      // A failed network call is repeated with the same backoff as the tool's own AWS calls; any
      // other failure stops the command, as before.
      if (!NETWORK_FAILURE.test(tail) || run >= CDK_RUN_ATTEMPTS) throw new ExitWithCode(code);
      const delay = retryDelayMs(run);
      this.deps.out(`The CDK command failed on a network error; running it again in ${delay / 1000} s (run ${run + 1} of ${CDK_RUN_ATTEMPTS}).`);
      await this.deps.sleep(delay);
    }
  }

  async cdkSynth(): Promise<void> {
    await this.execCdk(this.getCdkCommand('synth', ['--app', this.getCdkAppCmd()]));
  }

  async cdkDiff(): Promise<void> {
    await this.execCdk(this.getCdkCommand('diff', ['--app', this.getCdkAppCmd()]));
  }

  /** Every page of `DescribeChangeSet`, so a large change set is fully inspected. */
  private async describeChangeSetFully(): Promise<ChangeSetDescription> {
    const first = await this.deps.cfn.describeChangeSet({
      StackName: this.stackName,
      ChangeSetName: CDK_DEPLOY_CHANGE_SET_NAME,
    });
    const changes = [...(first.Changes ?? [])];
    let nextToken = first.NextToken;
    while (nextToken !== undefined && nextToken !== '') {
      const page = await this.deps.cfn.describeChangeSet({
        StackName: this.stackName,
        ChangeSetName: CDK_DEPLOY_CHANGE_SET_NAME,
        NextToken: nextToken,
      });
      changes.push(...(page.Changes ?? []));
      nextToken = page.NextToken;
    }
    return { ...first, Changes: changes, NextToken: undefined };
  }

  /**
   * Creates the change set, reads it, and executes it only when nothing stateful is replaced or
   * removed. This is the artifact that stops a logical-ID mistake from destroying a cluster's
   * identity store; the documentation around it is the explanation, this is the enforcement.
   */
  async deployThroughChangeSet(contextParams: Record<string, string> = {}): Promise<ChangeSetVerdict> {
    await this.execCdk(this.getDeployArgv(contextParams));

    const description = await this.describeChangeSetFully();
    const removes = (description.Changes ?? []).some((change) => change.ResourceChange?.Action === 'Remove');
    const retained =
      removes && this.deps.cfn.getTemplate !== undefined
        ? retainedResources(await this.deps.cfn.getTemplate(this.stackName))
        : new Set<string>();
    const verdict = evaluateChangeSet(
      description,
      this.allowReplacement,
      builtInAllowedReplacements(this.clusterName),
      this.allowReplacementOfType,
      retained,
    );

    for (const allowed of verdict.allowed) {
      this.deps.out(`change-set guard: ALLOWED by ${allowed.allowedBy}: ${allowed.reason}`);
    }

    if (verdict.refusals.length > 0) {
      this.deps.err(`change-set guard: REFUSING to execute change set for stack ${this.stackName}`);
      for (const refusal of verdict.refusals) {
        this.deps.err(`  [${refusal.refusal}] ${refusal.reason}`);
      }
      this.deps.err(
        'no change was applied. review the change set, then re-run with ' +
          `--allow-replacement ${verdict.refusals.map((refusal) => refusal.logicalId).join(' --allow-replacement ')}` +
          ' for each resource you have decided to lose.',
      );
      throw new ChangeSetRefused(
        `change-set guard refused ${verdict.refusals.length} change(s) on stack ${this.stackName}`,
        verdict,
      );
    }

    if (verdict.empty) {
      this.deps.out(`${this.stackName}: no changes`);
      return verdict;
    }

    this.deps.out(`change-set guard: ${(description.Changes ?? []).length} change(s) accepted, executing`);
    await this.clearProtectionOnReplacedInstances(description);
    await this.deps.cfn.executeChangeSet({
      StackName: this.stackName,
      ChangeSetName: CDK_DEPLOY_CHANGE_SET_NAME,
      DisableRollback: !this.rollback,
    });
    const stack = await this.waitForStack();
    this.writeOutputsFile(stack);
    return verdict;
  }

  /** The old instance of an accepted replacement is deleted by the update; protection would fail that delete. */
  private async clearProtectionOnReplacedInstances(description: ChangeSetDescription): Promise<void> {
    for (const change of description.Changes ?? []) {
      const resource = change.ResourceChange;
      if (resource?.ResourceType !== 'AWS::EC2::Instance' || resource.Replacement !== 'True') continue;
      const instanceId = resource.PhysicalResourceId;
      if (instanceId === undefined) continue;
      const name = `${instanceId} (${resource.LogicalResourceId ?? '?'})`;
      if (this.deps.instanceProtection === undefined) {
        this.deps.out(`warning: ${name} is being replaced; if it is termination-protected, CloudFormation will fail to delete it and leave it running.`);
        continue;
      }
      try {
        if (!await this.deps.instanceProtection.isProtected({ awsRegion: this.awsRegion, instanceId })) continue;
        await this.deps.instanceProtection.setProtected({ awsRegion: this.awsRegion, instanceId, protected: false });
        this.deps.out(`cleared instance termination protection on ${name}, which this change set replaces`);
      } catch (error) {
        this.deps.out(`warning: could not clear termination protection on ${name}: ${(error as Error).message}. CloudFormation will leave it running after the replacement.`);
      }
    }
  }

  private async waitForStack(): Promise<StackDescription> {
    const startedAt = this.deps.now();
    for (;;) {
      const stack = await this.deps.cfn.describeStack(this.stackName);
      const status = stack.StackStatus ?? '';
      if (STACK_STATUS_IN_PROGRESS.test(status)) {
        if (this.deps.now() - startedAt >= STACK_WAIT_TIMEOUT_MS) {
          throw new ExitWithCode(
            1,
            `Stack ${this.stackName} was still ${status} after ${STACK_WAIT_TIMEOUT_MS / 60_000} minutes. No further modules were deployed. Open the stack events in CloudFormation, resolve the resource that is still changing, then re-run the same deploy.`,
          );
        }
        await this.deps.sleep(this.pollIntervalMs);
        continue;
      }
      if (!STACK_STATUS_OK.has(status)) {
        const reason = stack.StackStatusReason ?? '';
        throw new ExitWithCode(
          1,
          `Stack ${this.stackName} ended ${reason === '' ? status : `${status}: ${reason}`}. No further modules were deployed. Open the stack events in CloudFormation, fix the failing resource, then re-run the same deploy.`,
        );
      }
      return stack;
    }
  }

  /** The `--outputs-file` shape the CDK CLI writes; preparing without executing leaves it to us. */
  private writeOutputsFile(stack: StackDescription): void {
    const outputs: Record<string, string> = {};
    for (const output of stack.Outputs ?? []) {
      if (output.OutputKey !== undefined) outputs[output.OutputKey] = output.OutputValue ?? '';
    }
    writeFileSync(this.outputsFile(), `${JSON.stringify({ [this.stackName]: outputs }, null, 2)}\n`);
  }

  // -------------------------------------------------------------------------------------------
  // per-module invocation (`MODULE_MAPPING_INVOKE_MAPPING`)
  // -------------------------------------------------------------------------------------------

  private async clusterConfig(): Promise<ClusterConfig> {
    if (this.config !== undefined) {
      if (this.config.currentModuleId !== this.moduleId) this.config.setModuleId(this.moduleId);
      return this.config;
    }
    const config = await ClusterConfig.fromDynamoDb(this.clusterName, this.awsRegion, {
      moduleSet: this.moduleSet,
      moduleId: this.moduleId,
      scan: this.deps.scan,
    });
    return config;
  }

  /** `directoryservice` and `bastion-host` refuse to deploy before their prerequisite module. */
  private assertPrerequisiteDeployed(modules: ModuleInfo[], prerequisiteName: string): void {
    for (const module of modules) {
      if (module.name === prerequisiteName && module.status === 'not-deployed') {
        throw new GeneralException(
          `cannot deploy ${this.moduleId}. module: ${module.module_id} is not yet deployed.`,
        );
      }
    }
  }

  /**
   * Builds and uploads the packages a host module needs, then returns the context params for the
   * deploy. Modules with no host have neither, and get an empty object.
   */
  private async publishPackages(config: ClusterConfig, forceBuildBootstrap: boolean): Promise<Record<string, string>> {
    if (this.runsAsContainerTasks(config)) return {};
    const provider = this.moduleName === 'directoryservice' ? config.getString('directoryservice.provider') : undefined;
    const plans = bootstrapPackagePlans(this.moduleName, this.moduleId, this.deploymentId, provider);
    const releaseNames = releasePackageNames(this.moduleName, ideaVersion());
    if (plans.length === 0 && releaseNames.length === 0) return {};

    const clusterS3Bucket = config.getString('cluster.cluster_s3_bucket', undefined, { required: true }) as string;

    const releasePackageUris: Record<string, string> = {};
    for (const packageName of releaseNames) {
      const file = join(downloadsDir(), packageName);
      if (!existsSync(file)) throw new GeneralException(`package not found: ${file}`);
      const uri = releasePackageUri(clusterS3Bucket, packageName);
      this.deps.out(`uploading release package: ${uri} ...`);
      await this.deps.s3.putObject({
        Bucket: clusterS3Bucket,
        Key: `idea/releases/${packageName}`,
        Body: readFileSync(file),
      });
      releasePackageUris[packageName] = uri;
    }

    const contextParams: Record<string, string> = {};
    for (const plan of plans) {
      const uri = await this.publishBootstrapPackage(config, plan, releasePackageUris, clusterS3Bucket, forceBuildBootstrap);
      contextParams[plan.contextParameter] = uri;
    }
    return contextParams;
  }

  /**
   * Whether this module's processes run as container tasks rather than on hosts. The image
   * carries their packages, so nothing downloads a bootstrap archive or a release archive. The
   * condition is the one the module stacks build their host resources from, so a run that retains
   * hosts still publishes what those hosts read.
   */
  private runsAsContainerTasks(config: ClusterConfig): boolean {
    if (!CONTAINER_SERVED_MODULES.includes(this.moduleName)) return false;
    if (!config.getBool('ecs.enabled', false)) return false;
    if (this.moduleName === 'bastion-host') return true;
    return !config.getBool('ecs.retain_existing_hosts', false);
  }

  private async publishBootstrapPackage(
    config: ClusterConfig,
    plan: BootstrapPackagePlan,
    releasePackageUris: Record<string, string>,
    clusterS3Bucket: string,
    forceBuild: boolean,
  ): Promise<string> {
    const buildContext = this.deps.bootstrapContext;
    if (buildContext === undefined) {
      // Guessing the URI without uploading the archive would hand the host a package that does
      // not exist, so this is a hard stop rather than a warning.
      throw new GeneralException(
        `Cannot build the bootstrap package for module ${this.moduleId} on this cluster. This build cannot deploy host modules that need a bootstrap archive. Deploy stack-only modules, or use a release that includes bootstrap-package support.`,
      );
    }
    const baseOs = config.getString(this.baseOsKey(), undefined, { required: true }) as string;
    const instanceType = config.getString(this.instanceTypeKey(), undefined, { required: true }) as string;
    const context = buildContext({
      moduleName: this.moduleName,
      moduleId: this.moduleId,
      moduleSet: this.moduleSet,
      baseOs,
      instanceType,
      plan,
      releasePackageUris,
      config,
    });
    const uri = await buildAndUploadBootstrapPackage({
      sourceDirectory: this.deps.bootstrapSourceDir ?? bootstrapSourceDir(),
      targetPackageBasename: plan.basename,
      components: [...plan.components],
      context,
      tmpDir: this.deploymentDir,
      forceBuild,
      // The plan's basename ends in this deployment's id; the archive takes the rendered tree's
      // content id in its place, so a host whose bootstrap did not change keeps its user data.
      nameByContent: (contentId) => plan.basename.slice(0, -this.deploymentId.length) + contentId,
      baseOs,
      client: { send: (command) => this.deps.s3.putObject(command.input as { Bucket: string; Key: string; Body: Uint8Array }) },
      clusterS3Bucket,
      logger: (message) => this.deps.out(message),
    });
    return uri ?? bootstrapPackageUri(clusterS3Bucket, `${plan.basename}.tar.gz`);
  }

  /** `AMI_UPDATE_KEYS`-shaped: where each module keeps the base OS of its host. */
  private baseOsKey(): string {
    if (this.moduleName === 'cluster-manager') return `${this.moduleId}.ec2.autoscaling.base_os`;
    if (this.moduleName === 'virtual-desktop-controller') return `${this.moduleId}.controller.autoscaling.base_os`;
    return `${this.moduleId}.base_os`;
  }

  private instanceTypeKey(): string {
    if (this.moduleName === 'cluster-manager') return `${this.moduleId}.ec2.autoscaling.instance_type`;
    if (this.moduleName === 'virtual-desktop-controller') return `${this.moduleId}.controller.autoscaling.instance_type`;
    return `${this.moduleId}.instance_type`;
  }

  /** `CdkInvoker.invoke`: a module name outside the mapping is a no-op, exactly as in Python. */
  async invoke(options: { forceBuildBootstrap?: boolean } = {}): Promise<void> {
    if (!DEPLOYABLE_MODULE_NAMES.has(this.moduleName)) {
      this.deps.out(`module name not found: ${this.moduleName}`);
      return;
    }
    const config = await this.clusterConfig();
    if (this.moduleName === 'directoryservice') this.assertPrerequisiteDeployed(config.modules(), 'cluster');
    if (this.moduleName === 'bastion-host') this.assertPrerequisiteDeployed(config.modules(), 'scheduler');

    const contextParams = await this.publishPackages(config, options.forceBuildBootstrap === true);
    await this.deployThroughChangeSet(contextParams);
  }
}

/** `MODULE_MAPPING_INVOKE_MAPPING` keys. */
export const DEPLOYABLE_MODULE_NAMES: ReadonlySet<string> = new Set([
  'cluster',
  'shared-storage',
  'identity-provider',
  'directoryservice',
  'cluster-manager',
  'scheduler',
  'bastion-host',
  'virtual-desktop-controller',
  'analytics',
  'metrics',
  // The container control plane deploys at priority 4.5, between shared storage and the
  // cluster manager. Without this entry an all-module run prints that it cannot find the
  // module and returns, silently updating every stack except this one.
  'ecs',
]);

/** The packaged `idea-bootstrap` tree. */
export function bootstrapSourceDir(): string {
  const candidates = [
    fileURLToPath(new URL('../../resources/bootstrap', import.meta.url)),
    fileURLToPath(new URL('../../../idea-bootstrap', import.meta.url)),
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (found === undefined) throw new GeneralException(`bootstrap source tree not found: ${candidates.join(', ')}`);
  return found;
}
