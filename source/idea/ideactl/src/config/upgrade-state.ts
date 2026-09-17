/**
 * Durable progress and locking for one cluster upgrade.
 *
 * The record lives in the cluster bucket. Every write is conditional on the
 * object revision, so two operators cannot advance the same record at once.
 */

export const UPGRADE_STATE_OBJECT_KEY = "values/upgrade-state.json";
export const UPGRADE_STATE_SCHEMA_VERSION = 1;
export const DEFAULT_UPGRADE_LEASE_MS = 5 * 60 * 1000;

export const UPGRADE_BOUNDARIES = [
  "preflight",
  "eol-software-stacks",
  "values-file",
  "global-settings",
  "full-configuration",
  "host-settings",
  "protection-sweep",
  "module-deployments",
  "finalization",
] as const;

export type UpgradeBoundary = typeof UPGRADE_BOUNDARIES[number];
export type UpgradeBoundaryStatus = "pending" | "running" | "completed" | "skipped" | "failed";
export type UpgradeOperationStatus = "active" | "failed" | "completed";
export type ProtectionBaselineStatus = "not-recorded" | "recorded" | "unknown";
export type ProtectionRestoreStatus = "pending" | "restored" | "replaced" | "failed";

export interface UpgradePlan {
  clusterName: string;
  awsRegion: string;
  targetVersion: string;
  targetBaseOs: string;
  moduleSet: string;
  selectedModules: readonly string[];
  deploymentId: string;
}

export interface UpgradeStateLocation {
  bucket: string;
  key?: string;
}

export interface VersionedUpgradeStateObject {
  body: string;
  revision: string;
}

export type UpgradeStateWriteCondition =
  | { kind: "absent" }
  | { kind: "revision"; revision: string };

/**
 * Shared object operations needed by the journal.
 *
 * `putObject` must apply its condition atomically. It returns undefined when
 * the condition does not match. For an S3 adapter, use `IfNoneMatch: "*"` for
 * `absent`, `IfMatch` for `revision`, and the ETag as the returned revision.
 */
export interface UpgradeStateObjectApi {
  getObject(input: { bucket: string; key: string }): Promise<VersionedUpgradeStateObject | undefined>;
  putObject(input: {
    bucket: string;
    key: string;
    body: string;
    condition: UpgradeStateWriteCondition;
  }): Promise<{ revision: string } | undefined>;
}

export interface UpgradeBoundaryProgress {
  name: UpgradeBoundary;
  status: UpgradeBoundaryStatus;
  startedAt?: string;
  finishedAt?: string;
  skipReason?: string;
  error?: string;
}

export interface UpgradeSnapshot {
  name: string;
  source: string;
  body: string;
}

export interface ProtectedInstanceState {
  stackName: string;
  instanceId: string;
  originallyProtected: boolean;
  cleared: boolean;
  restoreStatus: ProtectionRestoreStatus;
}

export interface UpgradeProtectionState {
  baseline: ProtectionBaselineStatus;
  instances: ProtectedInstanceState[];
}

export interface UpgradeLease {
  holderId: string;
  expiresAt: number;
}

export interface UpgradeOperationRecord {
  schemaVersion: 1;
  operation: {
    clusterName: string;
    awsRegion: string;
    targetVersion: string;
    targetBaseOs: string;
    moduleSet: string;
    selectedModules: string[];
    deploymentId: string;
  };
  status: UpgradeOperationStatus;
  createdAt: string;
  updatedAt: string;
  lease: UpgradeLease | null;
  boundaries: UpgradeBoundaryProgress[];
  completedModules: string[];
  snapshots: UpgradeSnapshot[];
  packageKeys: string[];
  protection: UpgradeProtectionState;
  recovered: boolean;
  warnings: string[];
}

export interface UpgradeStateSessionOptions {
  holderId: string;
  now?: () => number;
  leaseMs?: number;
}

export interface UpgradeResumeIdentity {
  clusterName: string;
  awsRegion: string;
  deploymentId: string;
}

export interface UpgradeRecoveryEvidence {
  completedBoundaries?: readonly UpgradeBoundary[];
  skippedBoundaries?: Readonly<Partial<Record<UpgradeBoundary, string>>>;
  completedModules?: readonly string[];
  snapshots?: readonly UpgradeSnapshot[];
  packageKeys?: readonly string[];
  protectionBaseline?: readonly {
    stackName: string;
    instanceId: string;
    originallyProtected: boolean;
  }[];
}

export interface UpgradeStateReport {
  found: boolean;
  status?: UpgradeOperationStatus;
  deploymentId?: string;
  stoppedAt?: UpgradeBoundary;
  completedBoundaries: UpgradeBoundary[];
  remainingBoundaries: UpgradeBoundary[];
  completedModules: string[];
  remainingModules: string[];
  protectionBaseline: ProtectionBaselineStatus;
  protectionToRestore: string[];
  warnings: string[];
}

/** Base error for malformed records and invalid state transitions. */
export class UpgradeStateError extends Error {}

/** Raised when a requested durable record does not exist. */
export class UpgradeStateNotFoundError extends UpgradeStateError {}

/** Raised when another process owns or changes the durable record. */
export class UpgradeStateConflictError extends UpgradeStateError {}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === "string");
}

function isBoundary(value: unknown): value is UpgradeBoundary {
  return typeof value === "string" && UPGRADE_BOUNDARIES.some((boundary) => boundary === value);
}

function isBoundaryStatus(value: unknown): value is UpgradeBoundaryStatus {
  return value === "pending" || value === "running" || value === "completed" || value === "skipped" || value === "failed";
}

function isOperationStatus(value: unknown): value is UpgradeOperationStatus {
  return value === "active" || value === "failed" || value === "completed";
}

function isProtectionBaseline(value: unknown): value is ProtectionBaselineStatus {
  return value === "not-recorded" || value === "recorded" || value === "unknown";
}

function isRestoreStatus(value: unknown): value is ProtectionRestoreStatus {
  return value === "pending" || value === "restored" || value === "replaced" || value === "failed";
}

function requireNonEmptyString(value: unknown, path: string): string {
  if (!isNonEmptyString(value)) throw new UpgradeStateError(`Invalid upgrade record: ${path} must be a non-empty string`);
  return value;
}

function optionalString(value: unknown, path: string): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string") throw new UpgradeStateError(`Invalid upgrade record: ${path} must be a string`);
  return value;
}

function requireStringArray(value: unknown, path: string): string[] {
  if (!isStringArray(value)) throw new UpgradeStateError(`Invalid upgrade record: ${path} must be an array of strings`);
  return [...value];
}

function assertUnique(values: readonly string[], path: string): void {
  if (new Set(values).size !== values.length) throw new UpgradeStateError(`${path} contains duplicate values`);
}

function parseBoundaryProgress(value: unknown, index: number): UpgradeBoundaryProgress {
  if (!isRecord(value)) throw new UpgradeStateError(`Invalid upgrade record: boundaries[${index}] must be an object`);
  const name = value["name"];
  const status = value["status"];
  if (!isBoundary(name)) throw new UpgradeStateError(`Invalid upgrade record: boundaries[${index}].name is invalid`);
  if (!isBoundaryStatus(status)) throw new UpgradeStateError(`Invalid upgrade record: boundaries[${index}].status is invalid`);
  return {
    name,
    status,
    startedAt: optionalString(value["startedAt"], `boundaries[${index}].startedAt`),
    finishedAt: optionalString(value["finishedAt"], `boundaries[${index}].finishedAt`),
    skipReason: optionalString(value["skipReason"], `boundaries[${index}].skipReason`),
    error: optionalString(value["error"], `boundaries[${index}].error`),
  };
}

function parseSnapshot(value: unknown, index: number): UpgradeSnapshot {
  if (!isRecord(value)) throw new UpgradeStateError(`Invalid upgrade record: snapshots[${index}] must be an object`);
  return {
    name: requireNonEmptyString(value["name"], `snapshots[${index}].name`),
    source: requireNonEmptyString(value["source"], `snapshots[${index}].source`),
    body: requireNonEmptyString(value["body"], `snapshots[${index}].body`),
  };
}

function parseProtectedInstance(value: unknown, index: number): ProtectedInstanceState {
  if (!isRecord(value)) throw new UpgradeStateError(`Invalid upgrade record: protection.instances[${index}] must be an object`);
  if (typeof value["originallyProtected"] !== "boolean") {
    throw new UpgradeStateError(`Invalid upgrade record: protection.instances[${index}].originallyProtected must be a boolean`);
  }
  if (typeof value["cleared"] !== "boolean") {
    throw new UpgradeStateError(`Invalid upgrade record: protection.instances[${index}].cleared must be a boolean`);
  }
  const restoreStatus = value["restoreStatus"];
  if (!isRestoreStatus(restoreStatus)) {
    throw new UpgradeStateError(`Invalid upgrade record: protection.instances[${index}].restoreStatus is invalid`);
  }
  return {
    stackName: requireNonEmptyString(value["stackName"], `protection.instances[${index}].stackName`),
    instanceId: requireNonEmptyString(value["instanceId"], `protection.instances[${index}].instanceId`),
    originallyProtected: value["originallyProtected"],
    cleared: value["cleared"],
    restoreStatus,
  };
}

/** Parse and validate an object read from shared storage. */
export function parseUpgradeOperationRecord(body: string): UpgradeOperationRecord {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch (error) {
    throw new UpgradeStateError(`Invalid upgrade record JSON: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!isRecord(parsed)) throw new UpgradeStateError("Invalid upgrade record: root must be an object");
  if (parsed["schemaVersion"] !== UPGRADE_STATE_SCHEMA_VERSION) {
    throw new UpgradeStateError(`Unsupported upgrade record schema: ${String(parsed["schemaVersion"])}`);
  }

  const operationValue = parsed["operation"];
  if (!isRecord(operationValue)) throw new UpgradeStateError("Invalid upgrade record: operation must be an object");
  const selectedModules = requireStringArray(operationValue["selectedModules"], "operation.selectedModules");
  assertUnique(selectedModules, "operation.selectedModules");

  const status = parsed["status"];
  if (!isOperationStatus(status)) throw new UpgradeStateError("Invalid upgrade record: status is invalid");

  const leaseValue = parsed["lease"];
  let lease: UpgradeLease | null;
  if (leaseValue === null) {
    lease = null;
  } else {
    if (!isRecord(leaseValue)) throw new UpgradeStateError("Invalid upgrade record: lease must be an object or null");
    if (typeof leaseValue["expiresAt"] !== "number" || !Number.isFinite(leaseValue["expiresAt"])) {
      throw new UpgradeStateError("Invalid upgrade record: lease.expiresAt must be a finite number");
    }
    lease = {
      holderId: requireNonEmptyString(leaseValue["holderId"], "lease.holderId"),
      expiresAt: leaseValue["expiresAt"],
    };
  }

  const boundariesValue = parsed["boundaries"];
  if (!Array.isArray(boundariesValue)) throw new UpgradeStateError("Invalid upgrade record: boundaries must be an array");
  const boundaries = boundariesValue.map(parseBoundaryProgress);
  if (
    boundaries.length !== UPGRADE_BOUNDARIES.length ||
    boundaries.some((entry, index) => entry.name !== UPGRADE_BOUNDARIES[index])
  ) {
    throw new UpgradeStateError("Invalid upgrade record: boundaries must contain every boundary in order");
  }

  const snapshotsValue = parsed["snapshots"];
  if (!Array.isArray(snapshotsValue)) throw new UpgradeStateError("Invalid upgrade record: snapshots must be an array");
  const snapshots = snapshotsValue.map(parseSnapshot);
  assertUnique(snapshots.map((snapshot) => snapshot.name), "snapshots");

  const protectionValue = parsed["protection"];
  if (!isRecord(protectionValue)) throw new UpgradeStateError("Invalid upgrade record: protection must be an object");
  const baseline = protectionValue["baseline"];
  if (!isProtectionBaseline(baseline)) throw new UpgradeStateError("Invalid upgrade record: protection.baseline is invalid");
  const instancesValue = protectionValue["instances"];
  if (!Array.isArray(instancesValue)) {
    throw new UpgradeStateError("Invalid upgrade record: protection.instances must be an array");
  }
  const instances = instancesValue.map(parseProtectedInstance);
  assertUnique(instances.map((instance) => instance.instanceId), "protection.instances");
  if (baseline !== "recorded" && instances.length > 0) {
    throw new UpgradeStateError("Invalid upgrade record: protection instances require a recorded baseline");
  }

  const completedModules = requireStringArray(parsed["completedModules"], "completedModules");
  assertUnique(completedModules, "completedModules");
  const packageKeys = requireStringArray(parsed["packageKeys"], "packageKeys");
  assertUnique(packageKeys, "packageKeys");
  const warnings = requireStringArray(parsed["warnings"], "warnings");
  if (typeof parsed["recovered"] !== "boolean") {
    throw new UpgradeStateError("Invalid upgrade record: recovered must be a boolean");
  }

  return {
    schemaVersion: 1,
    operation: {
      clusterName: requireNonEmptyString(operationValue["clusterName"], "operation.clusterName"),
      awsRegion: requireNonEmptyString(operationValue["awsRegion"], "operation.awsRegion"),
      targetVersion: requireNonEmptyString(operationValue["targetVersion"], "operation.targetVersion"),
      targetBaseOs: requireNonEmptyString(operationValue["targetBaseOs"], "operation.targetBaseOs"),
      moduleSet: requireNonEmptyString(operationValue["moduleSet"], "operation.moduleSet"),
      selectedModules,
      deploymentId: requireNonEmptyString(operationValue["deploymentId"], "operation.deploymentId"),
    },
    status,
    createdAt: requireNonEmptyString(parsed["createdAt"], "createdAt"),
    updatedAt: requireNonEmptyString(parsed["updatedAt"], "updatedAt"),
    lease,
    boundaries,
    completedModules,
    snapshots,
    packageKeys,
    protection: { baseline, instances },
    recovered: parsed["recovered"],
    warnings,
  };
}

/** Serialize a record in a stable, human-readable form. */
export function serializeUpgradeOperationRecord(record: UpgradeOperationRecord): string {
  return `${JSON.stringify(record, null, 2)}\n`;
}

function validatePlan(plan: UpgradePlan): void {
  for (const [name, value] of Object.entries({
    clusterName: plan.clusterName,
    awsRegion: plan.awsRegion,
    targetVersion: plan.targetVersion,
    targetBaseOs: plan.targetBaseOs,
    moduleSet: plan.moduleSet,
    deploymentId: plan.deploymentId,
  })) {
    if (!isNonEmptyString(value)) throw new UpgradeStateError(`${name} must be a non-empty string`);
  }
  if (plan.selectedModules.some((moduleId) => !isNonEmptyString(moduleId))) {
    throw new UpgradeStateError("selectedModules must contain non-empty strings");
  }
  assertUnique(plan.selectedModules, "selectedModules");
}

function validateSessionOptions(options: UpgradeStateSessionOptions): void {
  if (!isNonEmptyString(options.holderId)) throw new UpgradeStateError("holderId must be a non-empty string");
  const leaseMs = options.leaseMs ?? DEFAULT_UPGRADE_LEASE_MS;
  if (!Number.isSafeInteger(leaseMs) || leaseMs <= 0) throw new UpgradeStateError("leaseMs must be a positive integer");
}

function timestamp(now: number): string {
  if (!Number.isFinite(now)) throw new UpgradeStateError("now() must return a finite timestamp");
  return new Date(now).toISOString();
}

function freshRecord(
  plan: UpgradePlan,
  options: Required<Pick<UpgradeStateSessionOptions, "holderId">> & {
    now: () => number;
    leaseMs: number;
  },
  recovered: boolean,
): UpgradeOperationRecord {
  const now = options.now();
  const at = timestamp(now);
  return {
    schemaVersion: 1,
    operation: {
      clusterName: plan.clusterName,
      awsRegion: plan.awsRegion,
      targetVersion: plan.targetVersion,
      targetBaseOs: plan.targetBaseOs,
      moduleSet: plan.moduleSet,
      selectedModules: [...plan.selectedModules],
      deploymentId: plan.deploymentId,
    },
    status: "active",
    createdAt: at,
    updatedAt: at,
    lease: { holderId: options.holderId, expiresAt: now + options.leaseMs },
    boundaries: UPGRADE_BOUNDARIES.map((name) => ({ name, status: "pending" })),
    completedModules: [],
    snapshots: [],
    packageKeys: [],
    protection: { baseline: "not-recorded", instances: [] },
    recovered,
    warnings: [],
  };
}

function normalizedOptions(options: UpgradeStateSessionOptions): {
  holderId: string;
  now: () => number;
  leaseMs: number;
} {
  validateSessionOptions(options);
  return {
    holderId: options.holderId,
    now: options.now ?? Date.now,
    leaseMs: options.leaseMs ?? DEFAULT_UPGRADE_LEASE_MS,
  };
}

function normalizeLocation(location: UpgradeStateLocation): { bucket: string; key: string } {
  if (!isNonEmptyString(location.bucket)) throw new UpgradeStateError("bucket must be a non-empty string");
  const key = location.key ?? UPGRADE_STATE_OBJECT_KEY;
  if (!isNonEmptyString(key)) throw new UpgradeStateError("key must be a non-empty string");
  return { bucket: location.bucket, key };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function boundarySettled(progress: UpgradeBoundaryProgress): boolean {
  return progress.status === "completed" || progress.status === "skipped";
}

/** Return a compact view suitable for a status command. */
export function reportUpgradeState(record: UpgradeOperationRecord | undefined): UpgradeStateReport {
  if (record === undefined) {
    return {
      found: false,
      completedBoundaries: [],
      remainingBoundaries: [],
      completedModules: [],
      remainingModules: [],
      protectionBaseline: "unknown",
      protectionToRestore: [],
      warnings: ["No durable upgrade record was found."],
    };
  }
  const completedBoundaries = record.boundaries
    .filter(boundarySettled)
    .map((boundary) => boundary.name);
  const remainingBoundaries = record.boundaries
    .filter((boundary) => !boundarySettled(boundary))
    .map((boundary) => boundary.name);
  const stoppedAt = record.boundaries.find((boundary) => boundary.status === "failed" || boundary.status === "running")?.name
    ?? remainingBoundaries[0];
  const protectionToRestore = record.protection.baseline === "recorded"
    ? record.protection.instances
      .filter((instance) =>
        instance.originallyProtected &&
        instance.restoreStatus !== "restored" &&
        instance.restoreStatus !== "replaced"
      )
      .map((instance) => instance.instanceId)
    : [];
  return {
    found: true,
    status: record.status,
    deploymentId: record.operation.deploymentId,
    stoppedAt: record.status === "completed" ? undefined : stoppedAt,
    completedBoundaries,
    remainingBoundaries,
    completedModules: [...record.completedModules],
    remainingModules: record.operation.selectedModules.filter((moduleId) => !record.completedModules.includes(moduleId)),
    protectionBaseline: record.protection.baseline,
    protectionToRestore,
    warnings: [...record.warnings],
  };
}

/** Read the latest operation without acquiring its lock. */
export async function readUpgradeState(
  api: UpgradeStateObjectApi,
  location: UpgradeStateLocation,
): Promise<UpgradeOperationRecord | undefined> {
  const resolved = normalizeLocation(location);
  const object = await api.getObject(resolved);
  return object === undefined ? undefined : parseUpgradeOperationRecord(object.body);
}

/**
 * Owns one conditionally updated upgrade record.
 *
 * Callers should use `runBoundary` around every durable phase and `runModule`
 * around each stack deployment. A failed callback is recorded before the
 * original error is rethrown.
 */
export class UpgradeStateJournal {
  private readonly api: UpgradeStateObjectApi;
  private readonly location: { bucket: string; key: string };
  private readonly holderId: string;
  private readonly now: () => number;
  private readonly leaseMs: number;
  private current: UpgradeOperationRecord;
  private revision: string;
  private writeQueue: Promise<void> = Promise.resolve();

  private constructor(input: {
    api: UpgradeStateObjectApi;
    location: { bucket: string; key: string };
    options: ReturnType<typeof normalizedOptions>;
    record: UpgradeOperationRecord;
    revision: string;
  }) {
    this.api = input.api;
    this.location = input.location;
    this.holderId = input.options.holderId;
    this.now = input.options.now;
    this.leaseMs = input.options.leaseMs;
    this.current = input.record;
    this.revision = input.revision;
  }

  /** Create a new operation, replacing only a completed prior record. */
  static async start(
    api: UpgradeStateObjectApi,
    location: UpgradeStateLocation,
    plan: UpgradePlan,
    sessionOptions: UpgradeStateSessionOptions,
  ): Promise<UpgradeStateJournal> {
    validatePlan(plan);
    const resolved = normalizeLocation(location);
    const options = normalizedOptions(sessionOptions);
    const existing = await api.getObject(resolved);
    const record = freshRecord(plan, options, false);
    const condition: UpgradeStateWriteCondition = existing === undefined
      ? { kind: "absent" }
      : { kind: "revision", revision: existing.revision };
    if (existing !== undefined) {
      const previous = parseUpgradeOperationRecord(existing.body);
      if (previous.status !== "completed") {
        throw new UpgradeStateConflictError(
          `Upgrade ${previous.operation.deploymentId} is ${previous.status}; resume it before starting another upgrade`,
        );
      }
    }
    const result = await api.putObject({
      ...resolved,
      body: serializeUpgradeOperationRecord(record),
      condition,
    });
    if (result === undefined) throw new UpgradeStateConflictError("Another upgrade acquired the operation record");
    return new UpgradeStateJournal({ api, location: resolved, options, record, revision: result.revision });
  }

  /** Acquire an incomplete record after its previous holder failed or expired. */
  static async resume(
    api: UpgradeStateObjectApi,
    location: UpgradeStateLocation,
    identity: UpgradeResumeIdentity,
    sessionOptions: UpgradeStateSessionOptions,
  ): Promise<UpgradeStateJournal> {
    const resolved = normalizeLocation(location);
    const options = normalizedOptions(sessionOptions);
    const existing = await api.getObject(resolved);
    if (existing === undefined) {
      throw new UpgradeStateNotFoundError(`No durable record exists for upgrade ${identity.deploymentId}`);
    }
    const record = parseUpgradeOperationRecord(existing.body);
    if (
      record.operation.clusterName !== identity.clusterName ||
      record.operation.awsRegion !== identity.awsRegion ||
      record.operation.deploymentId !== identity.deploymentId
    ) {
      throw new UpgradeStateConflictError(
        `The durable record belongs to ${record.operation.clusterName}/${record.operation.awsRegion}/${record.operation.deploymentId}`,
      );
    }
    if (record.status === "completed") {
      throw new UpgradeStateConflictError(`Upgrade ${identity.deploymentId} is already completed`);
    }
    const now = options.now();
    if (record.lease !== null && record.lease.holderId !== options.holderId && record.lease.expiresAt > now) {
      throw new UpgradeStateConflictError(
        `Upgrade ${identity.deploymentId} is locked by another run until ${timestamp(record.lease.expiresAt)}`,
      );
    }
    record.status = "active";
    record.updatedAt = timestamp(now);
    record.lease = { holderId: options.holderId, expiresAt: now + options.leaseMs };
    const result = await api.putObject({
      ...resolved,
      body: serializeUpgradeOperationRecord(record),
      condition: { kind: "revision", revision: existing.revision },
    });
    if (result === undefined) throw new UpgradeStateConflictError("Another run resumed this upgrade first");
    return new UpgradeStateJournal({ api, location: resolved, options, record, revision: result.revision });
  }

  /**
   * Recreate a missing record from verified external evidence.
   *
   * If the protection sweep was reached without an exact saved baseline,
   * protection remains unknown and completion is refused.
   */
  static async recover(
    api: UpgradeStateObjectApi,
    location: UpgradeStateLocation,
    plan: UpgradePlan,
    evidence: UpgradeRecoveryEvidence,
    sessionOptions: UpgradeStateSessionOptions,
  ): Promise<UpgradeStateJournal> {
    validatePlan(plan);
    const resolved = normalizeLocation(location);
    const options = normalizedOptions(sessionOptions);
    if (await api.getObject(resolved) !== undefined) {
      throw new UpgradeStateConflictError("A durable upgrade record already exists");
    }
    const record = freshRecord(plan, options, true);
    const completed = new Set(evidence.completedBoundaries ?? []);
    const skipped = evidence.skippedBoundaries ?? {};
    for (const boundary of record.boundaries) {
      if (completed.has(boundary.name)) {
        boundary.status = "completed";
        boundary.finishedAt = record.updatedAt;
      } else {
        const reason = skipped[boundary.name];
        if (reason !== undefined) {
          boundary.status = "skipped";
          boundary.skipReason = reason;
          boundary.finishedAt = record.updatedAt;
        }
      }
    }
    UpgradeStateJournal.assertSettledPrefix(record.boundaries);

    record.completedModules = [...(evidence.completedModules ?? [])];
    assertUnique(record.completedModules, "completedModules");
    if (record.completedModules.some((moduleId) => !record.operation.selectedModules.includes(moduleId))) {
      throw new UpgradeStateError("completedModules contains a module outside the upgrade plan");
    }
    record.snapshots = (evidence.snapshots ?? []).map((snapshot) => ({ ...snapshot }));
    assertUnique(record.snapshots.map((snapshot) => snapshot.name), "snapshots");
    record.packageKeys = [...(evidence.packageKeys ?? [])];
    assertUnique(record.packageKeys, "packageKeys");

    const protectionReached = completed.has("protection-sweep") ||
      skipped["module-deployments"] !== undefined ||
      completed.has("module-deployments") ||
      completed.has("finalization") ||
      record.completedModules.length > 0;
    if (evidence.protectionBaseline !== undefined) {
      record.protection = {
        baseline: "recorded",
        instances: UpgradeStateJournal.buildProtectionBaseline(evidence.protectionBaseline),
      };
    } else if (protectionReached) {
      record.protection.baseline = "unknown";
      record.warnings.push(
        "The original termination-protection flags were not recovered. Restore them from an external inventory before completion.",
      );
    }

    const result = await api.putObject({
      ...resolved,
      body: serializeUpgradeOperationRecord(record),
      condition: { kind: "absent" },
    });
    if (result === undefined) throw new UpgradeStateConflictError("Another run recreated the upgrade record first");
    return new UpgradeStateJournal({ api, location: resolved, options, record, revision: result.revision });
  }

  /** Return a detached copy of the current durable state. */
  record(): UpgradeOperationRecord {
    return structuredClone(this.current);
  }

  /** Return completed and remaining work for a status or resume command. */
  report(): UpgradeStateReport {
    return reportUpgradeState(this.current);
  }

  /** Renew ownership before a long-running external operation. */
  async heartbeat(): Promise<void> {
    await this.update(() => {});
  }

  /** Run one phase unless it is already completed or explicitly skipped. */
  async runBoundary(boundary: UpgradeBoundary, operation: () => Promise<void>): Promise<boolean> {
    const progress = this.boundary(boundary);
    if (boundarySettled(progress)) return false;
    this.assertEarlierBoundariesSettled(boundary);
    await this.update((draft) => {
      const entry = UpgradeStateJournal.boundaryIn(draft, boundary);
      entry.status = "running";
      entry.startedAt ??= draft.updatedAt;
      delete entry.finishedAt;
      delete entry.skipReason;
      delete entry.error;
    });
    try {
      await this.withHeartbeat(operation);
      await this.update((draft) => {
        const entry = UpgradeStateJournal.boundaryIn(draft, boundary);
        entry.status = "completed";
        entry.finishedAt = draft.updatedAt;
        delete entry.error;
      });
      return true;
    } catch (error) {
      try {
        await this.failBoundary(boundary, errorMessage(error));
      } catch (recordError) {
        throw new AggregateError(
          [error, recordError],
          `Upgrade failed at ${boundary}, and recording the failure also failed`,
        );
      }
      throw error;
    }
  }

  /** Mark an optional boundary as intentionally skipped. */
  async skipBoundary(boundary: UpgradeBoundary, reason: string): Promise<void> {
    if (!isNonEmptyString(reason)) throw new UpgradeStateError("A skipped boundary requires a reason");
    const progress = this.boundary(boundary);
    if (boundarySettled(progress)) return;
    this.assertEarlierBoundariesSettled(boundary);
    await this.update((draft) => {
      const entry = UpgradeStateJournal.boundaryIn(draft, boundary);
      entry.status = "skipped";
      entry.skipReason = reason;
      entry.finishedAt = draft.updatedAt;
      delete entry.error;
    });
  }

  /** Run one selected module unless its successful commit is already recorded. */
  async runModule(moduleId: string, operation: () => Promise<void>): Promise<boolean> {
    if (!this.current.operation.selectedModules.includes(moduleId)) {
      throw new UpgradeStateError(`Module ${moduleId} is not in this upgrade plan`);
    }
    if (this.current.completedModules.includes(moduleId)) return false;
    if (this.boundary("module-deployments").status !== "running") {
      throw new UpgradeStateError("Module deployment must run inside the module-deployments boundary");
    }
    await this.heartbeat();
    await operation();
    await this.update((draft) => {
      if (!draft.completedModules.includes(moduleId)) draft.completedModules.push(moduleId);
    });
    return true;
  }

  /** Reopen a recorded module when live stack and table checks do not match. */
  async reopenModule(moduleId: string, reason: string): Promise<void> {
    if (!this.current.operation.selectedModules.includes(moduleId)) {
      throw new UpgradeStateError(`Module ${moduleId} is not in this upgrade plan`);
    }
    if (!isNonEmptyString(reason)) throw new UpgradeStateError("A reopened module requires a reason");
    if (!this.current.completedModules.includes(moduleId)) return;
    if (this.boundary("module-deployments").status !== "running") {
      throw new UpgradeStateError("A module must be reopened inside the module-deployments boundary");
    }
    await this.update((draft) => {
      draft.completedModules = draft.completedModules.filter((candidate) => candidate !== moduleId);
      draft.warnings.push(`Module ${moduleId} was reopened: ${reason}`);
    });
  }

  /** Save an exact table or file snapshot before the first related mutation. */
  async recordSnapshot(snapshot: UpgradeSnapshot): Promise<void> {
    if (!isNonEmptyString(snapshot.name) || !isNonEmptyString(snapshot.source) || !isNonEmptyString(snapshot.body)) {
      throw new UpgradeStateError("Snapshot name, source, and body must be non-empty strings");
    }
    const existing = this.current.snapshots.find((candidate) => candidate.name === snapshot.name);
    if (existing !== undefined) {
      if (existing.source !== snapshot.source || existing.body !== snapshot.body) {
        throw new UpgradeStateError(`Snapshot ${snapshot.name} is already recorded with different contents`);
      }
      return;
    }
    await this.update((draft) => {
      draft.snapshots.push({ ...snapshot });
    });
  }

  /** Record an uploaded object so a resumed run can reuse it. */
  async recordPackageKey(key: string): Promise<void> {
    if (!isNonEmptyString(key)) throw new UpgradeStateError("Package key must be a non-empty string");
    if (this.current.packageKeys.includes(key)) return;
    await this.update((draft) => {
      draft.packageKeys.push(key);
    });
  }

  /**
   * Persist every original protection flag before clearing the first one.
   *
   * Recording only instances that were changed is unsafe because a crash can
   * occur after the API write and before the changed marker is persisted.
   */
  async recordProtectionBaseline(
    instances: readonly {
      stackName: string;
      instanceId: string;
      originallyProtected: boolean;
    }[],
  ): Promise<void> {
    if (this.boundary("protection-sweep").status !== "running") {
      throw new UpgradeStateError("Protection baseline must be recorded inside the protection-sweep boundary");
    }
    const baseline = UpgradeStateJournal.buildProtectionBaseline(instances);
    if (this.current.protection.baseline === "recorded") {
      if (JSON.stringify(this.current.protection.instances) !== JSON.stringify(baseline)) {
        throw new UpgradeStateError("A different termination-protection baseline is already recorded");
      }
      return;
    }
    if (this.current.protection.baseline === "unknown") {
      throw new UpgradeStateError("The original termination-protection baseline is unknown");
    }
    await this.update((draft) => {
      draft.protection = { baseline: "recorded", instances: baseline };
    });
  }

  /** Mark that one originally protected instance was cleared. */
  async markProtectionCleared(instanceId: string): Promise<void> {
    await this.update((draft) => {
      const instance = UpgradeStateJournal.protectedInstanceIn(draft, instanceId);
      if (!instance.originallyProtected) {
        throw new UpgradeStateError(`Instance ${instanceId} was not originally protected`);
      }
      instance.cleared = true;
    });
  }

  /** Record the result of restoring one originally protected instance. */
  async markProtectionRestore(
    instanceId: string,
    status: Exclude<ProtectionRestoreStatus, "pending">,
  ): Promise<void> {
    await this.update((draft) => {
      const instance = UpgradeStateJournal.protectedInstanceIn(draft, instanceId);
      if (!instance.originallyProtected) {
        throw new UpgradeStateError(`Instance ${instanceId} was not originally protected`);
      }
      instance.restoreStatus = status;
    });
  }

  /** Return every original true flag that still needs reconciliation. */
  protectionToRestore(): ProtectedInstanceState[] {
    if (this.current.protection.baseline === "unknown") {
      throw new UpgradeStateError(
        "The original termination-protection flags are unknown. Recover them from an external inventory before continuing.",
      );
    }
    if (this.current.protection.baseline === "not-recorded") return [];
    return structuredClone(
      this.current.protection.instances.filter((instance) =>
        instance.originallyProtected &&
        instance.restoreStatus !== "restored" &&
        instance.restoreStatus !== "replaced"
      ),
    );
  }

  /** Close the operation after every boundary and protection restore settles. */
  async complete(): Promise<void> {
    if (this.current.boundaries.some((boundary) => !boundarySettled(boundary))) {
      throw new UpgradeStateError("Cannot complete an upgrade while boundaries remain");
    }
    const missingModules = this.current.operation.selectedModules.filter(
      (moduleId) => !this.current.completedModules.includes(moduleId),
    );
    if (this.boundary("module-deployments").status !== "skipped" && missingModules.length > 0) {
      throw new UpgradeStateError(`Cannot complete an upgrade before these modules: ${missingModules.join(", ")}`);
    }
    if (this.current.protection.baseline === "unknown") {
      throw new UpgradeStateError("Cannot complete an upgrade with an unknown termination-protection baseline");
    }
    const unrestored = this.protectionToRestore();
    if (unrestored.length > 0) {
      throw new UpgradeStateError(
        `Cannot complete an upgrade before termination protection is reconciled for: ${unrestored.map((entry) => entry.instanceId).join(", ")}`,
      );
    }
    await this.update((draft) => {
      draft.status = "completed";
      draft.lease = null;
    }, false);
  }

  private static boundaryIn(record: UpgradeOperationRecord, boundary: UpgradeBoundary): UpgradeBoundaryProgress {
    const progress = record.boundaries.find((entry) => entry.name === boundary);
    if (progress === undefined) throw new UpgradeStateError(`Upgrade record has no ${boundary} boundary`);
    return progress;
  }

  private boundary(boundary: UpgradeBoundary): UpgradeBoundaryProgress {
    return UpgradeStateJournal.boundaryIn(this.current, boundary);
  }

  private static protectedInstanceIn(record: UpgradeOperationRecord, instanceId: string): ProtectedInstanceState {
    if (record.protection.baseline !== "recorded") {
      throw new UpgradeStateError("Termination-protection baseline has not been recorded");
    }
    const instance = record.protection.instances.find((candidate) => candidate.instanceId === instanceId);
    if (instance === undefined) throw new UpgradeStateError(`Instance ${instanceId} is not in the protection baseline`);
    return instance;
  }

  private static buildProtectionBaseline(
    instances: readonly {
      stackName: string;
      instanceId: string;
      originallyProtected: boolean;
    }[],
  ): ProtectedInstanceState[] {
    const result: ProtectedInstanceState[] = [];
    for (const instance of instances) {
      if (!isNonEmptyString(instance.stackName) || !isNonEmptyString(instance.instanceId)) {
        throw new UpgradeStateError("Protection baseline entries require a stackName and instanceId");
      }
      result.push({
        stackName: instance.stackName,
        instanceId: instance.instanceId,
        originallyProtected: instance.originallyProtected,
        cleared: false,
        restoreStatus: "pending",
      });
    }
    assertUnique(result.map((instance) => instance.instanceId), "protection baseline");
    return result;
  }

  private static assertSettledPrefix(boundaries: readonly UpgradeBoundaryProgress[]): void {
    let foundUnsettled = false;
    for (const boundary of boundaries) {
      if (boundarySettled(boundary)) {
        if (foundUnsettled) {
          throw new UpgradeStateError(`Recovered boundary ${boundary.name} has an unsettled predecessor`);
        }
      } else {
        foundUnsettled = true;
      }
    }
  }

  private assertEarlierBoundariesSettled(boundary: UpgradeBoundary): void {
    const target = UPGRADE_BOUNDARIES.indexOf(boundary);
    for (const earlier of this.current.boundaries.slice(0, target)) {
      if (!boundarySettled(earlier)) {
        throw new UpgradeStateError(`Boundary ${boundary} cannot run before ${earlier.name} is settled`);
      }
    }
  }

  private assertOwned(): void {
    if (this.current.status !== "active") {
      throw new UpgradeStateError(`Upgrade record is ${this.current.status}; resume it before writing`);
    }
    if (this.current.lease?.holderId !== this.holderId) {
      throw new UpgradeStateConflictError("This run no longer owns the upgrade record");
    }
  }

  private async failBoundary(boundary: UpgradeBoundary, message: string): Promise<void> {
    await this.update((draft) => {
      const progress = UpgradeStateJournal.boundaryIn(draft, boundary);
      progress.status = "failed";
      progress.error = message;
      progress.finishedAt = draft.updatedAt;
      draft.status = "failed";
      draft.lease = null;
    }, false);
  }

  private async withHeartbeat(operation: () => Promise<void>): Promise<void> {
    let heartbeatRunning = false;
    let heartbeatFailure: unknown;
    const interval = setInterval(() => {
      if (heartbeatRunning || heartbeatFailure !== undefined) return;
      heartbeatRunning = true;
      void this.heartbeat()
        .catch((error: unknown) => {
          heartbeatFailure = error;
        })
        .finally(() => {
          heartbeatRunning = false;
        });
    }, Math.max(25, Math.floor(this.leaseMs / 3)));
    try {
      await operation();
      if (heartbeatFailure !== undefined) throw heartbeatFailure;
    } finally {
      clearInterval(interval);
    }
  }

  private async update(
    mutate: (draft: UpgradeOperationRecord) => void,
    renewLease = true,
  ): Promise<void> {
    const write = async (): Promise<void> => {
      this.assertOwned();
      const draft = structuredClone(this.current);
      const now = this.now();
      draft.updatedAt = timestamp(now);
      if (renewLease) draft.lease = { holderId: this.holderId, expiresAt: now + this.leaseMs };
      mutate(draft);
      const result = await this.api.putObject({
        ...this.location,
        body: serializeUpgradeOperationRecord(draft),
        condition: { kind: "revision", revision: this.revision },
      });
      if (result === undefined) {
        throw new UpgradeStateConflictError("The upgrade record changed in another run");
      }
      this.current = draft;
      this.revision = result.revision;
    };
    const result = this.writeQueue.then(write, write);
    this.writeQueue = result.catch(() => {});
    await result;
  }
}
