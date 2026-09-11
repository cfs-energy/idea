/**
 * Shared evidence for every proof that a running job survives a control-plane replacement.
 *
 * "The job completed with the expected exit status" cannot tell survival from a requeue: a
 * requeued job is re-run from the beginning by the same submission and completes with the
 * same status, having thrown away whatever work was already done. Two facts do tell them
 * apart, and both live here so the acceptance test and the replacement checks assert the
 * same thing rather than two different things:
 *
 *   1. a job script that witnesses its own second execution. Its first execution writes a
 *      marker on the submitting user's shared home, and a second execution finds the marker
 *      and exits with WITNESS_RERUN_EXIT_STATUS, so a requeued job cannot reach the expected
 *      exit status at all. This one needs nothing but the completed-job record, which is why
 *      it is the discriminator that works on both sides of the cutover.
 *   2. the batch server's start time for the run. A requeue restarts the run, so the start
 *      time moves. A survival leaves it exactly where it was.
 *
 * Measured on the development cluster, and the reason the order above is not the other way
 * round: the active-job record carries neither start_time nor execution_hosts while the job
 * runs, so (2) and any comparison of execution host or instance are available only where the
 * job API reports them. They are compared when reported and named as unreported when not,
 * never compared as one absent value against another. A requeue that lands back on the same
 * execution node, which is what was observed, moves nothing but the start time and the
 * witness, so a host or instance comparison is not a substitute for either.
 */

import type { JsonObject, JsonValue } from "../api.ts";

/** Exit status the witness script uses for its own second execution. */
export const WITNESS_RERUN_EXIT_STATUS = 17;

/** Default directory the witness files are written to, on the submitting user's shared home. */
export const DEFAULT_WITNESS_DIRECTORY = "$HOME/.idea-job-witness";

/** Identity of one execution of a job, as the batch server reports it. */
export interface RunIdentity {
  /** Empty when the job API reports no execution host. See runIdentityObservations. */
  executionHost: string;
  /** Empty when the job API reports no instance for the execution host. */
  instanceId: string;
  startTime: string;
  state: string;
}

/** Both identifiers a submitted job is addressed by. They are not interchangeable. */
export interface SubmittedJob {
  /**
   * The batch server's sequence number. Scheduler.GetActiveJob accepts only this, and
   * refuses a request that carries job_uid alone.
   */
  jobId: string;
  /**
   * Unique for the life of the cluster. Scheduler.GetCompletedJob takes either, and this
   * is the one to give it: a job id is reused once the scheduler is replaced.
   */
  jobUid: string;
}

/** Reads the run identity out of a job object from the scheduler API. */
export function runIdentityFrom(job: JsonObject | undefined): RunIdentity | undefined {
  if (job === undefined) return undefined;
  const host = firstExecutionHost(job);
  return {
    executionHost: stringValue(host?.["host"]) ?? "",
    instanceId: stringValue(host?.["instance_id"]) ?? "",
    startTime: stringValue(job["start_time"]) ?? "",
    state: stringValue(job["state"])?.toUpperCase() ?? "",
  };
}

/**
 * Reasons the run observed after a replacement is not the run observed before it.
 *
 * Each of the three identity fields is compared only when the job API reported it before
 * the replacement. Comparing an absent value against an absent value is not evidence, and
 * a check whose comparison always holds is worse than no comparison. unreportedRunFields
 * names what was not compared and the callers report it, so no pass implies more than it
 * proved; the requeue itself is caught by the witness script's exit status, which needs
 * none of these fields.
 */
export function runIdentityFailures(before: RunIdentity, after: RunIdentity | undefined): string[] {
  const failures: string[] = [];
  if (after === undefined) {
    failures.push("the job was not active after the replacement: the run did not survive");
    return failures;
  }
  if (after.state !== "RUNNING") {
    failures.push(`the job was ${after.state || "in no reported state"} after the replacement, not still running`);
  }
  if (before.startTime !== "" && after.startTime !== before.startTime) {
    // A requeue moves this and, when it lands back on the same node, nothing else.
    failures.push(
      `the batch server restarted the run: start time moved from ${before.startTime} to ${after.startTime}`,
    );
  }
  if (before.executionHost !== "" && after.executionHost !== before.executionHost) {
    failures.push(
      `the run moved execution host, from ${before.executionHost} to ${after.executionHost || "none reported"}`,
    );
  }
  if (before.instanceId !== "" && after.instanceId !== before.instanceId) {
    failures.push(
      `the execution node was replaced, from ${before.instanceId} to ${after.instanceId || "none reported"}`,
    );
  }
  return failures;
}

/**
 * The identity fields the job API did not report before the replacement, so were not
 * compared across it. An empty list means all three were compared.
 */
export function unreportedRunFields(before: RunIdentity): string[] {
  return [
    ...(before.startTime === "" ? ["start_time"] : []),
    ...(before.executionHost === "" ? ["execution_host"] : []),
    ...(before.instanceId === "" ? ["instance_id"] : []),
  ];
}

/** Evidence lines for a before-and-after pair, naming each value that was not reported. */
export function runIdentityObservations(before: RunIdentity, after: RunIdentity | undefined): string[] {
  const value = (field: string) => (field === "" ? "unreported" : field);
  return [
    `start_time_before=${value(before.startTime)}`,
    `execution_host_before=${value(before.executionHost)}`,
    `instance_before=${value(before.instanceId)}`,
    ...(after === undefined
      ? ["run_after=not_active"]
      : [
          `start_time_after=${value(after.startTime)}`,
          `execution_host_after=${value(after.executionHost)}`,
          `instance_after=${value(after.instanceId)}`,
          `state_after=${value(after.state)}`,
        ]),
  ];
}

/**
 * The job script. It witnesses its own executions: a second execution finds the marker
 * already present, records the rerun, and exits WITNESS_RERUN_EXIT_STATUS, so a requeue
 * cannot pass as a clean finish. The marker is written before the wait, so an interrupted
 * first run still witnesses itself.
 */
export function witnessJobScript(options: {
  identifier: string;
  queue: string;
  sleepSeconds: number;
  witnessDirectory: string;
}): string {
  const marker = `${options.witnessDirectory}/${options.identifier}.run`;
  const reruns = `${options.witnessDirectory}/${options.identifier}.reruns`;
  const beats = `${options.witnessDirectory}/${options.identifier}.beat`;
  return [
    "#!/bin/bash",
    `#PBS -N ${options.identifier}`,
    `#PBS -q ${options.queue}`,
    "#PBS -P default",
    `mkdir -p ${options.witnessDirectory}`,
    `if [ -e ${marker} ]; then`,
    `  date -u +%Y-%m-%dT%H:%M:%SZ >> ${reruns}`,
    `  exit ${WITNESS_RERUN_EXIT_STATUS}`,
    "fi",
    `date -u +%Y-%m-%dT%H:%M:%SZ > ${marker}`,
    `hostname >> ${marker}`,
    `end=$(( $(date +%s) + ${options.sleepSeconds} ))`,
    'while [ "$(date +%s)" -lt "${end}" ]; do',
    `  printf '%s %s\\n' "$(date +%s)" "$(hostname)" >> ${beats}`,
    "  sleep 10",
    "done",
    "",
  ].join("\n");
}

/** The scheduler API surface the shared job helpers need. */
export interface JobApi {
  request(namespace: string, payload: JsonValue): Promise<{ body: JsonValue; status: number }>;
}

/** Submits the witnessing job script and returns both of its identifiers. */
export async function submitWitnessJob(
  api: JobApi,
  options: { identifier: string; queue: string; sleepSeconds: number; witnessDirectory: string },
): Promise<SubmittedJob> {
  const script = witnessJobScript(options);
  const result = await api.request("Scheduler.SubmitJob", {
    client_submission_id: options.identifier,
    job_script: Buffer.from(script, "utf8").toString("base64"),
    job_script_interpreter: "pbs",
  });
  const job = jobPayload(result);
  const jobId = stringValue(job?.["job_id"]);
  const jobUid = stringValue(job?.["job_uid"]);
  if (!apiSucceeded(result) || jobId === undefined || jobUid === undefined) {
    throw new Error(`the job was not accepted: ${JSON.stringify(result.body)}`);
  }
  return { jobId, jobUid };
}

/** Reads the active job's run identity, or undefined when it is not active. */
export async function activeRunIdentity(api: JobApi, job: SubmittedJob): Promise<RunIdentity | undefined> {
  let result: { body: JsonValue; status: number };
  try {
    // job_id, not job_uid: the active-job namespace refuses a request without it.
    result = await api.request("Scheduler.GetActiveJob", { job_id: job.jobId });
  } catch {
    return undefined;
  }
  if (!apiSucceeded(result)) return undefined;
  return runIdentityFrom(jobPayload(result));
}

/** Reads the completed job, which is the only record that carries its execution hosts. */
export async function completedJob(api: JobApi, job: SubmittedJob): Promise<JsonObject | undefined> {
  let result: { body: JsonValue; status: number };
  try {
    result = await api.request("Scheduler.GetCompletedJob", { job_uid: job.jobUid });
  } catch {
    return undefined;
  }
  return apiSucceeded(result) ? jobPayload(result) : undefined;
}

/** Extracts the job object from a scheduler API response. */
export function jobPayload(result: { body: JsonValue; status: number }): JsonObject | undefined {
  return asObject(asObject(asObject(result.body)?.["payload"])?.["job"]);
}

/** Checks the transport and application success fields of a scheduler API response. */
export function apiSucceeded(result: { body: JsonValue; status: number }): boolean {
  return result.status === 200 && asObject(result.body)?.["success"] === true;
}

function firstExecutionHost(job: JsonObject): JsonObject | undefined {
  const hosts = job["execution_hosts"];
  return Array.isArray(hosts) ? asObject(hosts[0]) : undefined;
}

function asObject(value: JsonValue | undefined): JsonObject | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;
}

function stringValue(value: JsonValue | undefined): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

/** Reads a finite numeric field, used for the completed job's exit status. */
export function numberValue(value: JsonValue | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}
