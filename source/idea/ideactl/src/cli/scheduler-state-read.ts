/**
 * Read the batch server's own state, read-only, over the systems manager channel.
 *
 * The admission boundary needs three facts about the batch server: scheduling
 * stopped, every queue disabled, and no work still in flight. Before the cutover
 * the batch server runs on a virtual machine that the account already manages, so
 * those facts are readable today without a new interface in the scheduler module.
 *
 * Three properties make this an observation path rather than a command path.
 *
 * 1. What reaches the host is a fixed document. Its content is the constant in
 *    this file, it declares no parameters, and the command names only that
 *    document, so neither this tool nor its caller can put a character on the
 *    host. An operator running the migration needs `ssm:SendCommand` on this one
 *    document, which cannot run anything else, rather than on the shell document,
 *    which can run anything.
 * 2. The content is verified before it is used. A document already present under
 *    the expected name is read back and compared byte for byte, so a document
 *    that was replaced with one that writes is refused rather than run.
 * 3. It fails closed. Every unreadable answer throws. Nothing here returns a
 *    default, because the values that would be defaulted, scheduling off and no
 *    jobs, are exactly the values that would let the boundary pass.
 *
 * After the cutover the batch server is a task and nothing in this file reaches
 * it. That is the caller's decision to make, and the caller refuses there.
 */

import { createHash } from "node:crypto";

import type { AwsClientOptions } from "./aws-client-options.ts";

/** The only thing this tool ever asks a batch server host to do. */
export const SCHEDULER_READ_DOCUMENT_CONTENT = JSON.stringify(
  {
    schemaVersion: "2.2",
    description:
      "Report the batch server's scheduling flag, per-queue enablement and job state counts. Reads only: no parameters, no writes.",
    mainSteps: [
      {
        action: "aws:runShellScript",
        name: "readBatchServerState",
        inputs: {
          runCommand: [
            "set -eu",
            "/opt/pbs/bin/qstat -B -f -F json",
            "echo ---QUEUES---",
            "/opt/pbs/bin/qstat -Q -f -F json",
          ],
        },
      },
    ],
  },
  undefined,
  2,
);

/**
 * The document name carries the content digest, so a release that changes the
 * probe uses a new document instead of disagreeing with one already in place.
 */
export const SCHEDULER_READ_DOCUMENT_NAME = `idea-scheduler-read-state-${
  createHash("sha256").update(SCHEDULER_READ_DOCUMENT_CONTENT).digest("hex").slice(0, 12)
}`;

/** One batch server queue as the server itself reports it. */
export interface BatchServerQueue {
  name: string;
  enabled: boolean;
  started: boolean;
}

/** What the batch server reports about itself at the moment of the call. */
export interface BatchServerState {
  /** The name the running server answers under, which is not always the configured one. */
  serverName: string;
  scheduling: boolean;
  queues: BatchServerQueue[];
  /** Every state the server counts, as reported, so nothing is dropped by grouping. */
  stateCounts: Readonly<Record<string, number>>;
  queuedJobs: number;
  provisioningJobs: number;
  runningJobs: number;
}

/** Raised when the batch server's state cannot be established. Never swallowed. */
export class SchedulerStateUnreadableError extends Error {}

/** One completed document invocation. */
export interface SsmInvocation {
  status: string;
  responseCode: number;
  stdout: string;
  stderr: string;
}

/** The systems manager surface this read needs, as a seam tests drive without a client. */
export interface SsmReadChannel {
  /** The stored content of a document, or undefined when no document has that name. */
  documentContent(name: string): Promise<string | undefined>;
  createDocument(name: string, content: string): Promise<void>;
  /** Run one document on one instance and wait for a terminal result. */
  runDocument(name: string, instanceId: string): Promise<SsmInvocation>;
}

const QUEUE_SEPARATOR = "---QUEUES---";

/** PBS reports its booleans as the strings True and False. Anything else is unreadable. */
function readFlag(value: unknown, label: string): boolean {
  if (typeof value === "boolean") return value;
  if (value === "True") return true;
  if (value === "False") return false;
  throw new SchedulerStateUnreadableError(
    `the batch server reported ${label}=${JSON.stringify(value)}, which is neither True nor False, so its state is not established`,
  );
}

/** `Transit:0 Queued:0 Held:0 Waiting:0 Running:0 Exiting:0 Begun:0` */
function parseStateCounts(value: unknown): Record<string, number> {
  if (typeof value !== "string" || value.trim() === "") {
    throw new SchedulerStateUnreadableError(
      "the batch server reported no state_count, so the number of jobs in flight is not established",
    );
  }
  const counts: Record<string, number> = {};
  for (const field of value.trim().split(/\s+/)) {
    const separator = field.indexOf(":");
    const name = field.slice(0, separator);
    const count = Number(field.slice(separator + 1));
    if (separator <= 0 || !Number.isSafeInteger(count) || count < 0) {
      throw new SchedulerStateUnreadableError(
        `the batch server reported an unreadable state_count field: ${JSON.stringify(field)}`,
      );
    }
    counts[name] = count;
  }
  if (Object.keys(counts).length === 0) {
    throw new SchedulerStateUnreadableError("the batch server reported an empty state_count");
  }
  return counts;
}

function sumStates(counts: Readonly<Record<string, number>>, states: readonly string[]): number {
  return states.reduce((total, state) => total + (counts[state] ?? 0), 0);
}

function parseJsonSection(text: string, label: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new SchedulerStateUnreadableError(
      `the ${label} reply from the batch server is not JSON (${error instanceof Error ? error.message : String(error)})`,
    );
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new SchedulerStateUnreadableError(`the ${label} reply from the batch server is not an object`);
  }
  return parsed as Record<string, unknown>;
}

/**
 * Turn the probe's output into the three facts the boundary needs.
 *
 * `total_jobs` is deliberately not used: with job history enabled it counts
 * finished jobs too, so a drained server reports a non-zero total. The per-state
 * counts are the live figures.
 */
export function parseBatchServerState(stdout: string): BatchServerState {
  const separator = stdout.indexOf(QUEUE_SEPARATOR);
  if (separator < 0) {
    throw new SchedulerStateUnreadableError(
      "the batch server probe returned no queue section, so its output is incomplete",
    );
  }
  const serverReply = parseJsonSection(stdout.slice(0, separator), "server");
  const queueReply = parseJsonSection(stdout.slice(separator + QUEUE_SEPARATOR.length), "queue");

  const servers = serverReply["Server"];
  if (typeof servers !== "object" || servers === null || Array.isArray(servers)) {
    throw new SchedulerStateUnreadableError("the batch server reply names no server, so it did not answer");
  }
  const [serverName, serverEntry] = Object.entries(servers as Record<string, unknown>)[0] ?? [];
  if (serverName === undefined || typeof serverEntry !== "object" || serverEntry === null) {
    throw new SchedulerStateUnreadableError("the batch server reply names no server, so it did not answer");
  }
  const server = serverEntry as Record<string, unknown>;

  const queueEntries = queueReply["Queue"];
  if (typeof queueEntries !== "object" || queueEntries === null || Array.isArray(queueEntries)) {
    throw new SchedulerStateUnreadableError(
      "the batch server reported no queue list, so per-queue admission is not established",
    );
  }
  const queues = Object.entries(queueEntries as Record<string, unknown>).map(([name, value]) => {
    if (typeof value !== "object" || value === null) {
      throw new SchedulerStateUnreadableError(`the batch server reported queue ${name} without any attributes`);
    }
    const attributes = value as Record<string, unknown>;
    return {
      name,
      enabled: readFlag(attributes["enabled"], `queue ${name} enabled`),
      started: readFlag(attributes["started"], `queue ${name} started`),
    };
  });

  const stateCounts = parseStateCounts(server["state_count"]);
  return {
    serverName,
    scheduling: readFlag(server["scheduling"], "scheduling"),
    queues,
    stateCounts,
    // Held is counted with queued rather than ignored: a held job is work the server still holds.
    queuedJobs: sumStates(stateCounts, ["Queued", "Waiting", "Transit", "Held"]),
    provisioningJobs: sumStates(stateCounts, ["Begun"]),
    runningJobs: sumStates(stateCounts, ["Running", "Exiting"]),
  };
}

/**
 * Ensure the fixed document is in place and is the one this release wrote.
 *
 * A document under the expected name whose content differs is refused rather
 * than repaired: the only reasons for a difference are a document this release
 * did not write and one that was edited, and neither is safe to run.
 */
export async function ensureSchedulerReadDocument(channel: SsmReadChannel): Promise<"present" | "created"> {
  const stored = await channel.documentContent(SCHEDULER_READ_DOCUMENT_NAME);
  if (stored === undefined) {
    await channel.createDocument(SCHEDULER_READ_DOCUMENT_NAME, SCHEDULER_READ_DOCUMENT_CONTENT);
    const written = await channel.documentContent(SCHEDULER_READ_DOCUMENT_NAME);
    if (written !== SCHEDULER_READ_DOCUMENT_CONTENT) {
      throw new SchedulerStateUnreadableError(
        `the read-only document ${SCHEDULER_READ_DOCUMENT_NAME} did not read back as written, so what would run on the batch server host is not known`,
      );
    }
    return "created";
  }
  if (stored !== SCHEDULER_READ_DOCUMENT_CONTENT) {
    throw new SchedulerStateUnreadableError(
      `the document ${SCHEDULER_READ_DOCUMENT_NAME} exists with content this release did not write, so what it would run on the batch server host is not known. Inspect it, and delete it if it is not the read-only probe.`,
    );
  }
  return "present";
}

/** Read the batch server's state, or throw. There is no third answer. */
export async function readBatchServerState(
  channel: SsmReadChannel,
  instanceId: string,
): Promise<BatchServerState> {
  await ensureSchedulerReadDocument(channel);
  const invocation = await channel.runDocument(SCHEDULER_READ_DOCUMENT_NAME, instanceId);
  if (invocation.status !== "Success" || invocation.responseCode !== 0) {
    throw new SchedulerStateUnreadableError(
      `the batch server state read on ${instanceId} ended ${invocation.status} with exit code ${invocation.responseCode}: ${
        invocation.stderr.replaceAll(/\s+/g, " ").trim() || "no error output"
      }`,
    );
  }
  return parseBatchServerState(invocation.stdout);
}

/** One printable line naming every fact the boundary rests on. */
export function renderBatchServerState(state: BatchServerState): string {
  const enabled = state.queues.filter((queue) => queue.enabled).map((queue) => queue.name);
  const counts = Object.entries(state.stateCounts)
    .map(([name, count]) => `${name}:${count}`)
    .join(" ");
  return [
    `server=${state.serverName}`,
    `scheduling=${state.scheduling}`,
    `queues=${state.queues.length}`,
    `enabled=${enabled.length === 0 ? "none" : enabled.join(",")}`,
    `state_count=${counts}`,
  ].join("; ");
}

/** Where the read happens. */
export interface SsmReadTarget {
  awsRegion: string;
  awsProfile?: string;
  sleep(ms: number): Promise<void>;
  /** Client options, so this file shares the command tree's one credential path. */
  clientOptions(awsRegion: string, awsProfile?: string): Promise<AwsClientOptions>;
}

const TERMINAL_STATUSES = new Set(["Success", "Cancelled", "TimedOut", "Failed"]);
const POLL_INTERVAL_MS = 2_000;
const POLL_LIMIT = 45;

/** The live channel. It calls three read actions and one document create, and nothing else. */
export function liveSsmReadChannel(target: SsmReadTarget): SsmReadChannel {
  const client = async () => {
    const sdk = await import("@aws-sdk/client-ssm");
    return { sdk, client: new sdk.SSMClient(await target.clientOptions(target.awsRegion, target.awsProfile)) };
  };

  return {
    async documentContent(name) {
      const { sdk, client: ssm } = await client();
      try {
        const result = await ssm.send(new sdk.GetDocumentCommand({ Name: name, DocumentFormat: "JSON" }));
        return result.Content;
      } catch (error) {
        if ((error as { name?: string })?.name === "InvalidDocument") return undefined;
        throw error;
      }
    },
    async createDocument(name, content) {
      const { sdk, client: ssm } = await client();
      await ssm.send(
        new sdk.CreateDocumentCommand({
          Name: name,
          Content: content,
          DocumentType: "Command",
          DocumentFormat: "JSON",
          TargetType: "/AWS::EC2::Instance",
        }),
      );
    },
    async runDocument(name, instanceId) {
      const { sdk, client: ssm } = await client();
      const sent = await ssm.send(
        new sdk.SendCommandCommand({ DocumentName: name, InstanceIds: [instanceId] }),
      );
      const commandId = sent.Command?.CommandId;
      if (commandId === undefined) {
        throw new SchedulerStateUnreadableError("the batch server state read returned no command id");
      }
      for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
        await target.sleep(POLL_INTERVAL_MS);
        let result;
        try {
          result = await ssm.send(
            new sdk.GetCommandInvocationCommand({ CommandId: commandId, InstanceId: instanceId }),
          );
        } catch (error) {
          // The invocation is not registered for a moment after the command is accepted.
          if ((error as { name?: string })?.name === "InvocationDoesNotExist") continue;
          throw error;
        }
        const status = result.Status ?? "Pending";
        if (!TERMINAL_STATUSES.has(status)) continue;
        return {
          status,
          responseCode: result.ResponseCode ?? -1,
          stdout: result.StandardOutputContent ?? "",
          stderr: result.StandardErrorContent ?? "",
        };
      }
      throw new SchedulerStateUnreadableError(
        `the batch server state read on ${instanceId} did not finish within ${(POLL_INTERVAL_MS * POLL_LIMIT) / 1000} seconds`,
      );
    },
  };
}
