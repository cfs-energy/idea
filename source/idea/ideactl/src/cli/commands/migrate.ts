/**
 * Drive the one-phase control-plane migration in its fixed serial order.
 *
 * Cluster reads and writes are injected. Durable progress uses the shared
 * upgrade journal, with exact migration step markers stored as snapshots.
 */

import type { Command } from "commander";

import {
  UpgradeStateJournal,
  readUpgradeState,
  type UpgradeBoundary,
  type UpgradeOperationRecord,
  type UpgradeStateObjectApi,
} from "../../config/upgrade-state.ts";

/** The durable boundaries and operator-facing actions from the migration plan. */
export const MIGRATION_STEPS = [
  {
    id: "PREFLIGHT_PASSED",
    precondition: "Every read-only migration predicate is known and green",
    action: "Record the target-bound pre-flight manifest",
  },
  {
    id: "OPERATION_STARTED",
    precondition: "The pre-flight fingerprints are current and the operation lock is held",
    action: "Capture exact rollback inputs and identify every required package",
  },
  {
    id: "ADMISSION_CLOSED",
    precondition: "Maintenance and PBS administration paths are reachable",
    action: "Announce maintenance, close scheduler admission, and wait for every job inventory to drain",
  },
  {
    id: "WORKLOAD_DRAINED",
    precondition: "Maintenance is enabled, scheduling is disabled, and all submission queues are disabled",
    action: "Confirm empty job inventories and retire compute nodes carrying the legacy scheduler address",
  },
  {
    id: "LEGACY_SCHEDULER_CAPTURED",
    precondition: "Workload inventories are empty and scheduler stop, restart, archive, and restore paths are ready",
    action: "Stop the legacy scheduler and capture a read-back-verified spool archive",
  },
  {
    id: "SOFTWARE_STACKS_RECONCILED",
    precondition: "The legacy scheduler is stopped and its verified archive and host are retained",
    action: "Apply the recorded desktop software-stack reconciliation plan one row at a time",
  },
  {
    id: "CONFIGURATION_STAGED",
    precondition: "Software-stack rows and any required search index changes are reconciled",
    action: "Stage target configuration with container routing disabled and scheduler desired count zero",
  },
  {
    id: "PROVIDERS_COMMITTED",
    precondition: "Configuration snapshots and row journals are complete and admission remains closed",
    action: "Deploy provider stacks serially in the recorded dependency order",
  },
  {
    id: "SCHEDULER_DNS_RETAINED",
    precondition: "Provider stacks are stable and the retain-only scheduler change set has no other effect",
    action: "Commit and verify retention of the existing scheduler DNS record",
  },
  {
    id: "ECS_CONFIGURATION_ACTIVE",
    precondition: "The scheduler DNS record is retained and the legacy scheduler remains stopped",
    action: "Enable container configuration with the stable scheduler name and scheduler desired count zero",
  },
  {
    id: "ECS_STAGED",
    precondition: "Container configuration is active and the scheduler desired and running counts are zero",
    action: "Deploy and prove the shared container capacity while production routes remain on legacy targets",
  },
  {
    id: "PBS_STATE_SEEDED",
    precondition: "Container targets are healthy, the scheduler is at zero, and the source archive is verified",
    action: "Restore and verify the scheduler state in the target shared directory",
  },
  {
    id: "ECS_SCHEDULER_READY",
    precondition: "Shared scheduler state is verified and the legacy scheduler remains stopped",
    action: "Start and prove exactly one container scheduler against the restored state",
  },
  {
    id: "CLUSTER_MANAGER_ROUTED",
    precondition: "The container cluster-manager targets are healthy and the route-only change is safe",
    action: "Route cluster-manager production endpoints to containers and prove the production path",
  },
  {
    id: "CLUSTER_MANAGER_LEGACY_REMOVED",
    precondition: "Cluster-manager production routing is proved on containers and removals match the allow-list",
    action: "Remove only legacy cluster-manager host resources and prove the production path again",
  },
  {
    id: "VDC_ROUTED",
    precondition: "Cluster-manager removal is stable, VDC container targets are healthy, and route-only changes are safe",
    action: "Route controller, broker, and gateway endpoints to containers and prove desktop reconnect",
  },
  {
    id: "VDC_LEGACY_REMOVED",
    precondition: "VDC production routing is proved on containers and removals preserve session infrastructure",
    action: "Remove only legacy VDC control-plane host resources and prove the production paths again",
  },
  {
    id: "SCHEDULER_ROUTED",
    precondition: "VDC removal is stable and container scheduler DNS, targets, and API health are proved",
    action: "Route scheduler endpoints to the container scheduler while retaining the stopped legacy host",
  },
  {
    id: "SCHEDULER_LEGACY_REMOVED",
    precondition: "Scheduler routing is proved, the archive is verified, and removals match the allow-list",
    action: "Remove legacy scheduler host resources while preserving task-owned DNS and shared scheduler state",
  },
  {
    id: "TARGET_PROVED",
    precondition: "All target stacks and routes are stable, and admission remains closed",
    action: "Reconcile protection and values, then run every application and replacement proof",
  },
  {
    id: "ADMISSION_REOPENED",
    precondition: "The complete target proof is retained and every admission control has a recorded prior value",
    action: "Restore PBS controls, clear maintenance, and verify both admission paths",
  },
  {
    id: "OPERATION_COMPLETED",
    precondition: "Admission is verified open and final cluster health is green",
    action: "Record final fingerprints, complete the operation, and release its lock",
  },
] as const;

export type MigrationStepId = (typeof MIGRATION_STEPS)[number]["id"];
export type ExecutableMigrationStepId = Exclude<MigrationStepId, "PREFLIGHT_PASSED" | "ADMISSION_CLOSED">;

/** Values fixed for one new or resumed migration operation. */
export interface MigrationContext {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  targetVersion: string;
  targetBaseOs: string;
  imageDigest: string;
  moduleSet: string;
  selectedModules: readonly string[];
  deploymentId: string;
  resuming: boolean;
  /** Operator acceptance of the pre-flight target-template comparison. */
  acceptTemplateComparison?: string;
  /** Operator acceptance of the configuration rows the run would overwrite. */
  acceptDrift?: string;
}

/** A safe, printable observation. It must not contain secret values. */
export interface MigrationObservation {
  ok: boolean;
  detail: string;
}

/** Read-back state required before the scheduler closure can commit. */
export interface SchedulerClosureObservation {
  detail: string;
  maintenanceEnabled: boolean;
  schedulingEnabled: boolean;
  enabledQueues: readonly string[];
  queuedJobs: number;
  provisioningJobs: number;
  runningJobs: number;
}

/**
 * Result of reconciling a step that has a durable started marker but no
 * committed marker.
 */
export interface MigrationReconciliation {
  state: "committed" | "retryable" | "uncertain";
  detail: string;
  schedulerClosure?: SchedulerClosureObservation;
}

/** Injected cluster operations used by the serial driver. */
export interface MigrationStepExecutor {
  checkPrecondition(
    step: MigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation>;
  execute(
    step: ExecutableMigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationObservation>;
  closeScheduler(context: Readonly<MigrationContext>): Promise<SchedulerClosureObservation>;
  reconcile(
    step: MigrationStepId,
    context: Readonly<MigrationContext>,
  ): Promise<MigrationReconciliation>;
}

/** Dependencies whose live implementations are supplied by the command tree. */
export interface MigrateDeps {
  stateObjects: UpgradeStateObjectApi;
  steps: MigrationStepExecutor;
  uuid(): string;
  targetVersion(): string;
  out(line: string): void;
  now?: () => number;
}

/** Command options for a new operation or a durable resume. */
export interface MigrateOptions {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
  stateBucket: string;
  targetBaseOs?: string;
  imageDigest?: string;
  moduleSet: string;
  selectedModules?: readonly string[];
  deploymentId?: string;
  resume?: string;
  acceptTemplateComparison?: string;
  acceptDrift?: string;
}

/** A refusal is safe to show as a single operator-facing command error. */
export class MigrationRefusedError extends Error {}

interface MigrationBoundaryGroup {
  boundary: UpgradeBoundary;
  steps: readonly MigrationStepId[];
}

const MIGRATION_BOUNDARY_GROUPS: readonly MigrationBoundaryGroup[] = [
  { boundary: "preflight", steps: ["PREFLIGHT_PASSED", "OPERATION_STARTED"] },
  {
    boundary: "eol-software-stacks",
    steps: [
      "ADMISSION_CLOSED",
      "WORKLOAD_DRAINED",
      "LEGACY_SCHEDULER_CAPTURED",
      "SOFTWARE_STACKS_RECONCILED",
    ],
  },
  { boundary: "values-file", steps: ["CONFIGURATION_STAGED"] },
  { boundary: "global-settings", steps: ["PROVIDERS_COMMITTED"] },
  { boundary: "full-configuration", steps: ["SCHEDULER_DNS_RETAINED", "ECS_CONFIGURATION_ACTIVE"] },
  { boundary: "host-settings", steps: ["ECS_STAGED", "PBS_STATE_SEEDED", "ECS_SCHEDULER_READY"] },
  {
    boundary: "protection-sweep",
    steps: [
      "CLUSTER_MANAGER_ROUTED",
      "CLUSTER_MANAGER_LEGACY_REMOVED",
      "VDC_ROUTED",
      "VDC_LEGACY_REMOVED",
      "SCHEDULER_ROUTED",
      "SCHEDULER_LEGACY_REMOVED",
    ],
  },
  { boundary: "module-deployments", steps: ["TARGET_PROVED"] },
  { boundary: "finalization", steps: ["ADMISSION_REOPENED", "OPERATION_COMPLETED"] },
];

const INPUTS_SNAPSHOT = "migration:inputs";
const INPUTS_SOURCE = "migration-command";
const IMAGE_DIGEST = /^[^\s]+@sha256:[0-9a-f]{64}$/;

function requireText(value: string | undefined, label: string): string {
  if (value === undefined || value.trim() === "") {
    throw new TypeError(`${label} must be a non-empty string`);
  }
  return value;
}

function requireUniqueModules(modules: readonly string[] | undefined): string[] {
  if (modules === undefined || modules.length === 0) {
    throw new TypeError("At least one selected module is required for a new migration");
  }
  const result = modules.map((moduleId) => requireText(moduleId, "Selected module"));
  if (new Set(result).size !== result.length) {
    throw new TypeError("Selected modules must not contain duplicates");
  }
  return result;
}

function requireImageDigest(value: string | undefined): string {
  const image = requireText(value, "Image digest");
  if (!IMAGE_DIGEST.test(image)) {
    throw new TypeError("Image must be an immutable reference ending in @sha256 followed by 64 lowercase hexadecimal characters");
  }
  return image;
}

function startedMarker(step: MigrationStepId): string {
  return `migration:${step}:started`;
}

function committedMarker(step: MigrationStepId): string {
  return `migration:${step}:committed`;
}

function hasSnapshot(record: UpgradeOperationRecord, name: string): boolean {
  return record.snapshots.some((snapshot) => snapshot.name === name);
}

function committedSteps(record: UpgradeOperationRecord): MigrationStepId[] {
  return MIGRATION_STEPS
    .map((step) => step.id)
    .filter((step) => hasSnapshot(record, committedMarker(step)));
}

function stepById(stepId: MigrationStepId): (typeof MIGRATION_STEPS)[number] {
  const step = MIGRATION_STEPS.find((candidate) => candidate.id === stepId);
  if (step === undefined) throw new TypeError(`Unknown migration step: ${stepId}`);
  return step;
}

function validateObservation(observation: MigrationObservation, label: string): void {
  if (typeof observation.ok !== "boolean") {
    throw new TypeError(`${label} must include a boolean ok value`);
  }
  requireText(observation.detail, `${label} detail`);
}

function validateCount(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative integer`);
  }
}

function schedulerClosureFailure(observation: SchedulerClosureObservation): string | undefined {
  requireText(observation.detail, "Scheduler closure detail");
  if (typeof observation.maintenanceEnabled !== "boolean" || typeof observation.schedulingEnabled !== "boolean") {
    throw new TypeError("Scheduler closure state must include boolean maintenance and scheduling values");
  }
  if (!Array.isArray(observation.enabledQueues) || observation.enabledQueues.some((queue) => typeof queue !== "string" || queue === "")) {
    throw new TypeError("Scheduler closure enabledQueues must contain non-empty strings");
  }
  validateCount(observation.queuedJobs, "Queued job count");
  validateCount(observation.provisioningJobs, "Provisioning job count");
  validateCount(observation.runningJobs, "Running job count");
  if (!observation.maintenanceEnabled) return "maintenance is not enabled";
  if (observation.schedulingEnabled) return "PBS scheduling is still enabled";
  if (observation.enabledQueues.length > 0) {
    return `submission queues are still enabled: ${observation.enabledQueues.join(", ")}`;
  }
  const activeJobs = observation.queuedJobs + observation.provisioningJobs + observation.runningJobs;
  if (activeJobs > 0) {
    return `the queue is not drained: ${observation.queuedJobs} queued, ${observation.provisioningJobs} provisioning, ${observation.runningJobs} running`;
  }
  return undefined;
}

function renderSchedulerObservation(observation: SchedulerClosureObservation): string {
  return [
    observation.detail,
    `maintenance=${observation.maintenanceEnabled}`,
    `scheduling=${observation.schedulingEnabled}`,
    `enabledQueues=${observation.enabledQueues.length}`,
    `queued=${observation.queuedJobs}`,
    `provisioning=${observation.provisioningJobs}`,
    `running=${observation.runningJobs}`,
  ].join("; ");
}

async function checkPrecondition(
  deps: MigrateDeps,
  context: Readonly<MigrationContext>,
  stepId: MigrationStepId,
): Promise<MigrationObservation> {
  const step = stepById(stepId);
  deps.out(`CHECK [${step.id}] ${step.precondition}`);
  const observation = await deps.steps.checkPrecondition(step.id, context);
  validateObservation(observation, `Precondition ${step.id}`);
  deps.out(`OBSERVED [${step.id}] ${observation.ok ? "PASS" : "FAIL"}: ${observation.detail}`);
  if (!observation.ok) {
    throw new MigrationRefusedError(`${step.id} refused: ${observation.detail}`);
  }
  return observation;
}

async function recordMarker(
  journal: UpgradeStateJournal,
  name: string,
  detail: string,
): Promise<void> {
  await journal.recordSnapshot({
    name,
    source: INPUTS_SOURCE,
    body: JSON.stringify({ detail }),
  });
}

async function markProvedModules(
  deps: MigrateDeps,
  journal: UpgradeStateJournal,
): Promise<void> {
  for (const moduleId of journal.record().operation.selectedModules) {
    await journal.runModule(moduleId, async () => {
      deps.out(`OBSERVED [TARGET_PROVED] module ${moduleId} is included in the retained target proof`);
    });
  }
}

async function commitReconciledStep(
  deps: MigrateDeps,
  journal: UpgradeStateJournal,
  stepId: MigrationStepId,
  reconciliation: MigrationReconciliation,
): Promise<boolean> {
  requireText(reconciliation.detail, `Reconciliation ${stepId} detail`);
  if (
    reconciliation.state !== "committed" &&
    reconciliation.state !== "retryable" &&
    reconciliation.state !== "uncertain"
  ) {
    throw new TypeError(`Reconciliation ${stepId} has an invalid state`);
  }
  deps.out(`OBSERVED [${stepId}] reconciliation ${reconciliation.state}: ${reconciliation.detail}`);
  if (reconciliation.state === "uncertain") {
    throw new MigrationRefusedError(
      `${stepId} has a started marker but its external result is uncertain: ${reconciliation.detail}`,
    );
  }
  if (reconciliation.state === "retryable") return false;
  if (stepId === "ADMISSION_CLOSED") {
    if (reconciliation.schedulerClosure === undefined) {
      throw new MigrationRefusedError("ADMISSION_CLOSED reconciliation did not include scheduler read-back state");
    }
    const failure = schedulerClosureFailure(reconciliation.schedulerClosure);
    deps.out(`OBSERVED [ADMISSION_CLOSED] ${renderSchedulerObservation(reconciliation.schedulerClosure)}`);
    if (failure !== undefined) throw new MigrationRefusedError(`ADMISSION_CLOSED refused: ${failure}`);
  }
  if (stepId === "TARGET_PROVED") await markProvedModules(deps, journal);
  await recordMarker(journal, committedMarker(stepId), reconciliation.detail);
  return true;
}

async function executeStep(
  deps: MigrateDeps,
  journal: UpgradeStateJournal,
  context: Readonly<MigrationContext>,
  stepId: MigrationStepId,
  prechecked?: MigrationObservation,
): Promise<void> {
  if (hasSnapshot(journal.record(), committedMarker(stepId))) return;

  const precondition = prechecked ?? await checkPrecondition(deps, context, stepId);
  const started = hasSnapshot(journal.record(), startedMarker(stepId));
  if (started) {
    deps.out(`RECONCILE [${stepId}] A started marker exists without a committed marker`);
    const reconciliation = await deps.steps.reconcile(stepId, context);
    if (await commitReconciledStep(deps, journal, stepId, reconciliation)) return;
  } else {
    await recordMarker(journal, startedMarker(stepId), precondition.detail);
  }

  const step = stepById(stepId);
  if (stepId === "PREFLIGHT_PASSED") {
    deps.out(`RUN [${step.id}] ${step.action}`);
    await recordMarker(journal, committedMarker(stepId), precondition.detail);
    return;
  }

  if (stepId === "ADMISSION_CLOSED") {
    deps.out(
      "ANNOUNCE [ADMISSION_CLOSED] The scheduler is closing. Maintenance, PBS scheduling, and every submission queue must remain closed until the migration is proved.",
    );
    deps.out(`RUN [${step.id}] ${step.action}`);
    const observation = await deps.steps.closeScheduler(context);
    const failure = schedulerClosureFailure(observation);
    deps.out(`OBSERVED [${step.id}] ${renderSchedulerObservation(observation)}`);
    if (failure !== undefined) throw new MigrationRefusedError(`${step.id} refused: ${failure}`);
    await recordMarker(journal, committedMarker(stepId), observation.detail);
    return;
  }

  deps.out(`RUN [${step.id}] ${step.action}`);
  const observation = await deps.steps.execute(stepId, context);
  validateObservation(observation, `Step ${step.id}`);
  deps.out(`OBSERVED [${step.id}] ${observation.ok ? "PASS" : "FAIL"}: ${observation.detail}`);
  if (!observation.ok) {
    throw new MigrationRefusedError(`${step.id} refused after execution: ${observation.detail}`);
  }
  if (stepId === "TARGET_PROVED") await markProvedModules(deps, journal);
  await recordMarker(journal, committedMarker(stepId), observation.detail);
}

function parseInputs(record: UpgradeOperationRecord): { imageDigest: string } | undefined {
  const snapshot = record.snapshots.find((candidate) => candidate.name === INPUTS_SNAPSHOT);
  if (snapshot === undefined) return undefined;
  let value: unknown;
  try {
    value = JSON.parse(snapshot.body);
  } catch (error) {
    throw new MigrationRefusedError(
      `The migration inputs record is invalid JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new MigrationRefusedError("The migration inputs record must be an object");
  }
  const imageDigest = (value as Record<string, unknown>)["imageDigest"];
  if (typeof imageDigest !== "string") {
    throw new MigrationRefusedError("The migration inputs record has no image digest");
  }
  return { imageDigest: requireImageDigest(imageDigest) };
}

async function recordInputs(
  journal: UpgradeStateJournal,
  context: Readonly<MigrationContext>,
): Promise<void> {
  await journal.recordSnapshot({
    name: INPUTS_SNAPSHOT,
    source: INPUTS_SOURCE,
    body: JSON.stringify({ imageDigest: context.imageDigest }),
  });
}

function newContext(
  deps: MigrateDeps,
  options: MigrateOptions,
): MigrationContext {
  if (options.resume !== undefined) {
    throw new TypeError("newContext cannot create a resumed operation");
  }
  if (options.deploymentId !== undefined && options.deploymentId.trim() === "") {
    throw new TypeError("Deployment ID must be a non-empty string");
  }
  return {
    clusterName: requireText(options.clusterName, "Cluster name"),
    awsRegion: requireText(options.awsRegion, "AWS region"),
    awsProfile: options.awsProfile,
    targetVersion: requireText(deps.targetVersion(), "Target version"),
    targetBaseOs: requireText(options.targetBaseOs, "Target base OS"),
    imageDigest: requireImageDigest(options.imageDigest),
    moduleSet: requireText(options.moduleSet, "Module set"),
    selectedModules: requireUniqueModules(options.selectedModules),
    deploymentId: options.deploymentId ?? requireText(deps.uuid(), "Generated deployment ID"),
    resuming: false,
    ...acceptances(options),
  };
}

/** Acceptances are per invocation: each one names the exact report it accepts. */
function acceptances(options: MigrateOptions): Pick<MigrationContext, "acceptTemplateComparison" | "acceptDrift"> {
  return {
    ...(options.acceptTemplateComparison === undefined
      ? {}
      : { acceptTemplateComparison: requireText(options.acceptTemplateComparison, "Accepted template comparison") }),
    ...(options.acceptDrift === undefined
      ? {}
      : { acceptDrift: requireText(options.acceptDrift, "Accepted drift report") }),
  };
}

function resumedContext(
  deps: MigrateDeps,
  options: MigrateOptions,
  record: UpgradeOperationRecord,
): MigrationContext {
  const installedVersion = requireText(deps.targetVersion(), "Target version");
  if (record.operation.targetVersion !== installedVersion) {
    throw new MigrationRefusedError(
      `Migration ${record.operation.deploymentId} targets release ${record.operation.targetVersion}, but this release is ${installedVersion}`,
    );
  }
  const inputs = parseInputs(record);
  const imageDigest = inputs?.imageDigest ?? requireImageDigest(options.imageDigest);
  if (options.imageDigest !== undefined && requireImageDigest(options.imageDigest) !== imageDigest) {
    throw new MigrationRefusedError("The supplied image digest does not match the durable migration record");
  }
  return {
    clusterName: record.operation.clusterName,
    awsRegion: record.operation.awsRegion,
    awsProfile: options.awsProfile,
    targetVersion: record.operation.targetVersion,
    targetBaseOs: record.operation.targetBaseOs,
    imageDigest,
    moduleSet: record.operation.moduleSet,
    selectedModules: [...record.operation.selectedModules],
    deploymentId: record.operation.deploymentId,
    resuming: true,
    ...acceptances(options),
  };
}

function validateCommandIdentity(options: MigrateOptions): void {
  requireText(options.clusterName, "Cluster name");
  requireText(options.awsRegion, "AWS region");
  requireText(options.stateBucket, "State bucket");
  if (options.awsProfile !== undefined) requireText(options.awsProfile, "AWS profile");
  if (options.resume !== undefined) {
    requireText(options.resume, "Resume deployment ID");
    if (options.deploymentId !== undefined) {
      throw new TypeError("--resume and --deployment-id cannot be used together");
    }
  }
}

function renderProgress(deps: MigrateDeps, journal: UpgradeStateJournal): void {
  const report = journal.report();
  const committed = committedSteps(journal.record());
  deps.out(
    `PROGRESS deployment=${report.deploymentId ?? "unknown"} status=${report.status ?? "unknown"} next=${report.stoppedAt ?? "completion"}`,
  );
  deps.out(`PROGRESS committed migration steps: ${committed.length === 0 ? "none" : committed.join(", ")}`);
}

/**
 * Run or resume the complete one-phase migration.
 *
 * Every mutating action gets a started marker before execution and a committed
 * marker only after the executor returns its owning-service observation.
 */
export async function migrateCluster(deps: MigrateDeps, options: MigrateOptions): Promise<void> {
  validateCommandIdentity(options);
  const location = { bucket: options.stateBucket };
  const holderId = requireText(deps.uuid(), "Journal holder ID");
  let journal: UpgradeStateJournal;
  let context: MigrationContext;
  let initialPreflight: MigrationObservation | undefined;

  if (options.resume === undefined) {
    context = newContext(deps, options);
    initialPreflight = await checkPrecondition(deps, context, "PREFLIGHT_PASSED");
    journal = await UpgradeStateJournal.start(
      deps.stateObjects,
      location,
      {
        clusterName: context.clusterName,
        awsRegion: context.awsRegion,
        targetVersion: context.targetVersion,
        targetBaseOs: context.targetBaseOs,
        moduleSet: context.moduleSet,
        selectedModules: context.selectedModules,
        deploymentId: context.deploymentId,
      },
      { holderId, now: deps.now },
    );
    await recordInputs(journal, context);
  } else {
    const record = await readUpgradeState(deps.stateObjects, location);
    if (record === undefined) {
      throw new MigrationRefusedError(`No durable record exists for migration ${options.resume}`);
    }
    if (
      record.operation.clusterName !== options.clusterName ||
      record.operation.awsRegion !== options.awsRegion ||
      record.operation.deploymentId !== options.resume
    ) {
      throw new MigrationRefusedError(
        `The durable record does not belong to ${options.clusterName}/${options.awsRegion}/${options.resume}`,
      );
    }
    context = resumedContext(deps, options, record);
    initialPreflight = await checkPrecondition(deps, context, "PREFLIGHT_PASSED");
    journal = await UpgradeStateJournal.resume(
      deps.stateObjects,
      location,
      {
        clusterName: context.clusterName,
        awsRegion: context.awsRegion,
        deploymentId: context.deploymentId,
      },
      { holderId, now: deps.now },
    );
    if (parseInputs(journal.record()) === undefined) await recordInputs(journal, context);
  }

  renderProgress(deps, journal);
  for (const group of MIGRATION_BOUNDARY_GROUPS) {
    await journal.runBoundary(group.boundary, async () => {
      for (const step of group.steps) {
        const prechecked = step === "PREFLIGHT_PASSED" ? initialPreflight : undefined;
        await executeStep(deps, journal, context, step, prechecked);
      }
    });
  }
  await journal.complete();
  deps.out(`COMPLETE migration ${context.deploymentId}`);
}

function collectModule(value: string, previous: string[] = []): string[] {
  return [...previous, value];
}

interface RegisteredMigrateOptions extends Omit<MigrateOptions, "selectedModules"> {
  selectedModule?: string[];
}

/** Register the migration command with replayable dependencies. */
export function registerMigrateCommands(program: Command, deps: MigrateDeps): void {
  program
    .command("migrate")
    .description("run or resume the one-phase control-plane migration")
    .requiredOption("--cluster-name <cluster-name>", "Cluster Name")
    .requiredOption("--aws-region <aws-region>", "AWS Region")
    .option("--aws-profile <aws-profile>", "AWS Profile Name")
    .requiredOption("--state-bucket <state-bucket>", "Bucket containing the durable operation record")
    .option("--target-base-os <target-base-os>", "Target Base OS for a new migration")
    .option("--image-digest <image-digest>", "Immutable control-plane image digest")
    .option("--module-set <module-set>", "Name of the ModuleSet", "default")
    .option("--selected-module <module-id>", "Deployable module ID, repeat for each module", collectModule)
    .option("--deployment-id <deployment-id>", "Deployment ID for a new migration")
    .option("--resume <deployment-id>", "Resume an incomplete migration")
    .option(
      "--accept-template-comparison <fingerprint>",
      "Accept the target-template comparison named by this fingerprint",
    )
    .option("--accept-drift <fingerprint>", "Accept overwriting the configuration rows named by this fingerprint")
    .action(async (commandOptions: RegisteredMigrateOptions) => {
      await migrateCluster(deps, {
        ...commandOptions,
        imageDigest: commandOptions.imageDigest,
        selectedModules: commandOptions.selectedModule,
      });
    });
}
