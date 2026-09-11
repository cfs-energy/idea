import { randomUUID } from "node:crypto";
import type { JsonObject, JsonValue } from "../api.ts";
import type { CheckContext, CheckResult, GatewayConnection, ProofMatrixOptions } from "./types.ts";

/** Returns a failed result without throwing away observations already collected. */
export function failed(...observed: string[]): CheckResult {
  return { observed, passed: false };
}

/** Returns a successful result with the supplied observations. */
export function passed(...observed: string[]): CheckResult {
  return { observed, passed: true };
}

/** Waits until a predicate succeeds or its configured deadline expires. */
export async function waitUntil(
  context: CheckContext,
  timeoutSeconds: number,
  description: string,
  predicate: () => Promise<boolean>,
): Promise<boolean> {
  const deadline = context.now() + timeoutSeconds * 1_000;
  while (context.now() <= deadline) {
    if (await predicate()) {
      return true;
    }
    await context.sleep((context.options.pollSeconds ?? 10) * 1_000);
  }
  context.output(`observed timeout while waiting for ${description}`);
  return false;
}

/** Validates an API response's transport and IDEA success fields. */
export function apiSucceeded(result: { body: JsonValue; status: number }): boolean {
  return result.status === 200 && asObject(result.body)?.success === true;
}

/** Extracts a JSON object while preserving the API client's JSON value contract. */
export function asObject(value: JsonValue | undefined): JsonObject | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;
}

/** Extracts a string property from a JSON object. */
export function stringField(value: JsonObject | undefined, key: string): string | undefined {
  const candidate = value?.[key];
  return typeof candidate === "string" && candidate !== "" ? candidate : undefined;
}

/** Extracts a numeric property from a JSON object. */
export function numberField(value: JsonObject | undefined, key: string): number | undefined {
  const candidate = value?.[key];
  return typeof candidate === "number" && Number.isFinite(candidate) ? candidate : undefined;
}

/** Extracts the API payload object. */
export function payloadObject(result: { body: JsonValue; status: number }): JsonObject | undefined {
  return asObject(asObject(result.body)?.payload);
}

/** Creates a desktop and waits for its ready state. */
export async function createReadyDesktop(context: CheckContext): Promise<{ elapsedMs: number; session: JsonObject } | CheckResult> {
  const request = context.options.desktopRequest;
  if (request === undefined) {
    return failed("desktop request was not supplied");
  }

  const startedAt = context.now();
  const created = await context.api.request("VirtualDesktop.CreateSession", request);
  const session = asObject(payloadObject(created)?.session);
  if (!apiSucceeded(created) || session === undefined) {
    return failed(`create response=${JSON.stringify(created.body)}`);
  }

  const ready = await waitUntil(
    context,
    context.options.readyTimeoutSeconds ?? 1_800,
    "desktop ready state",
    async () => {
      const result = await context.api.request("VirtualDesktop.GetSessionInfo", { session });
      const current = asObject(payloadObject(result)?.session);
      if (!apiSucceeded(result) || current === undefined) {
        return false;
      }
      Object.assign(session, current);
      return stringField(current, "state") === "READY";
    },
  );
  if (!ready) {
    return failed(`session=${JSON.stringify(session)}`, "desktop did not reach READY");
  }
  return { elapsedMs: context.now() - startedAt, session };
}

/** Deletes a desktop and records an API failure as an exception. */
export async function deleteDesktop(context: CheckContext, session: JsonObject): Promise<void> {
  const result = await context.api.request("VirtualDesktop.DeleteSessions", {
    sessions: [{ ...session, force: true }],
  });
  if (!apiSucceeded(result)) {
    throw new Error(`desktop cleanup failed: ${JSON.stringify(result.body)}`);
  }
}

/** Opens a gateway connection and ensures it is live before returning it. */
export async function openGatewayConnection(context: CheckContext): Promise<GatewayConnection> {
  const host = requiredOption(context.options, "gatewayHost");
  const port = context.options.gatewayPort ?? 443;
  const connection = await context.gateway.connect(host, port, context.options.insecureTls);
  if (!connection.isOpen()) {
    connection.close();
    throw new Error("gateway connection closed before verification");
  }
  return connection;
}

/** Submits a uniquely named short job and returns its persistent identifier. */
export async function submitJob(context: CheckContext, prefix: string): Promise<string> {
  const sleepSeconds = context.options.jobSleepSeconds ?? 120;
  const script = [
    "#!/bin/bash",
    `#PBS -N ${prefix}-${randomUUID()}`,
    "#PBS -q normal",
    "#PBS -P default",
    `/bin/sleep ${sleepSeconds}`,
    "",
  ].join("\n");
  const result = await context.api.request("Scheduler.SubmitJob", {
    client_submission_id: `${prefix}-${randomUUID()}`,
    job_script: Buffer.from(script, "utf8").toString("base64"),
    job_script_interpreter: "pbs",
  });
  const job = asObject(payloadObject(result)?.job);
  const jobUid = stringField(job, "job_uid") ?? stringField(job, "job_id");
  if (!apiSucceeded(result) || jobUid === undefined) {
    throw new Error(`job submission failed: ${JSON.stringify(result.body)}`);
  }
  return jobUid;
}

/** Polls a completed-job record until the expected exit status is observed. */
export async function waitForJobCompletion(context: CheckContext, jobUid: string): Promise<CheckResult> {
  let observed = "not found";
  const completed = await waitUntil(
    context,
    context.options.readyTimeoutSeconds ?? 1_800,
    `completion of job ${jobUid}`,
    async () => {
      const result = await context.api.request("Scheduler.GetCompletedJob", { job_uid: jobUid });
      const job = asObject(payloadObject(result)?.job);
      const exitStatus = numberField(job, "exit_status");
      observed = `job=${jobUid} exit_status=${exitStatus === undefined ? "missing" : exitStatus}`;
      return apiSucceeded(result) && exitStatus === (context.options.expectedExitStatus ?? 0);
    },
  );
  return completed ? passed(observed) : failed(observed);
}

/** Waits for a service to restore desired task and target health counts. */
export async function waitForServiceRecovery(
  context: CheckContext,
  service: string,
  targetGroup: string,
): Promise<CheckResult> {
  const cluster = requiredOption(context.options, "cluster");
  let observed = "not checked";
  const recovered = await waitUntil(
    context,
    context.options.replacementTimeoutSeconds ?? 900,
    `${service} replacement health`,
    async () => {
      const serviceCounts = await context.cloud.serviceRunningCount(cluster, service);
      const healthy = await context.cloud.healthyTargetCount(targetGroup);
      observed = `service=${service} running=${serviceCounts.running}/${serviceCounts.desired} healthy_targets=${healthy}`;
      return serviceCounts.desired > 0 && serviceCounts.running >= serviceCounts.desired && healthy >= serviceCounts.desired;
    },
  );
  return recovered ? passed(observed) : failed(observed);
}

/** Waits for an ECS service to restore its desired running task count. */
export async function waitForServiceRunning(context: CheckContext, service: string): Promise<CheckResult> {
  const cluster = requiredOption(context.options, "cluster");
  let observed = "not checked";
  const recovered = await waitUntil(
    context,
    context.options.replacementTimeoutSeconds ?? 900,
    `${service} task replacement`,
    async () => {
      const counts = await context.cloud.serviceRunningCount(cluster, service);
      observed = `service=${service} running=${counts.running}/${counts.desired}`;
      return counts.desired > 0 && counts.running >= counts.desired;
    },
  );
  return recovered ? passed(observed) : failed(observed);
}

/** Checks the currently selected check has an option before it is used. */
export function requiredOption<K extends keyof ProofMatrixOptions>(options: ProofMatrixOptions, key: K): NonNullable<ProofMatrixOptions[K]> {
  const value = options[key];
  if (value === undefined || value === "") {
    throw new Error(`missing required --${camelToKebab(key)}`);
  }
  return value as NonNullable<ProofMatrixOptions[K]>;
}

/** Formats option keys consistently in validation errors. */
export function camelToKebab(value: string): string {
  return value.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}
