import { request } from "node:https";

import { apiSucceeded, openGatewayConnection } from "./shared.ts";
import type { CheckContext } from "./types.ts";

export interface AvailabilitySample {
  endpoint: string;
  samples: number;
  failures: number;
  /** Samples that succeeded but took longer than a second: latency, not an outage. */
  slow: number;
  longestGapMs: number;
  /** The first failures, each "<iso time> <reason>", so a single failed sample can be explained. */
  failureDetails: string[];
  /** Failed samples discounted because the neutral control host failed at the same time: this
   * client's network dropped, not the cluster. They count neither as failures nor toward a gap. */
  clientDrops?: number;
}

/** A neutral host outside AWS, reached with a fresh connection each second as the control. */
export const DEFAULT_CONTROL_URL = "https://www.cloudflare.com/cdn-cgi/trace";

interface Taken { started: number; ended: number; ok: boolean; reason: string; elapsedMs: number }

/** One fresh TCP+TLS connection and request, never pooled, so a client drop shows as it would for a new user. */
function freshGet(url: string, timeoutMs: number): Promise<true | string> {
  return new Promise((resolve) => {
    const req = request(url, { agent: false, headers: { connection: "close" }, timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode !== undefined && res.statusCode < 500 ? true : `http ${res.statusCode}`);
    });
    req.on("timeout", () => req.destroy(new Error(`timeout ${timeoutMs}ms`)));
    req.on("error", (error) => resolve(`error ${error.message}`));
    req.end();
  });
}

/**
 * Summarise one endpoint's samples, discounting failures that overlap a control failure. The
 * budget a failure must meet is unchanged; only samples the client could not have made are set aside.
 */
export function summarise(endpoint: string, samples: Taken[], control: Taken[]): AvailabilitySample {
  const controlDown = control.filter((c) => !c.ok);
  const clientSide = (t: Taken) => controlDown.some((c) => c.started <= t.ended && t.started <= c.ended);
  const row: AvailabilitySample = { endpoint, samples: samples.length, failures: 0, slow: 0, longestGapMs: 0, failureDetails: [], clientDrops: 0 };
  let gapStarted: number | undefined;
  for (const t of samples) {
    if (!t.ok && clientSide(t)) {
      row.clientDrops! += 1;
      if (row.failureDetails.length < MAX_FAILURE_DETAILS) row.failureDetails.push(`${new Date(t.started).toISOString()} client drop: ${t.reason}`);
      if (gapStarted !== undefined) { row.longestGapMs = Math.max(row.longestGapMs, t.started - gapStarted); gapStarted = undefined; }
      continue;
    }
    if (!t.ok) {
      row.failures += 1;
      if (row.failureDetails.length < MAX_FAILURE_DETAILS) row.failureDetails.push(`${new Date(t.started).toISOString()} ${t.reason}`);
      gapStarted ??= t.started;
    } else {
      if (t.elapsedMs > SLOW_MS) row.slow += 1;
      if (gapStarted !== undefined) { row.longestGapMs = Math.max(row.longestGapMs, t.ended - gapStarted); gapStarted = undefined; }
    }
  }
  const last = samples.at(-1);
  if (gapStarted !== undefined && last !== undefined) row.longestGapMs = Math.max(row.longestGapMs, last.ended - gapStarted, 1_000);
  return row;
}

const SAMPLE_TIMEOUT_MS = 5_000;
const SLOW_MS = 1_000;
const MAX_FAILURE_DETAILS = 200;

/** Poll each endpoint independently every second, including while the upgrade process waits. */
export async function measureUpgradeAvailability<T>(
  context: CheckContext,
  upgrade: () => Promise<T>,
): Promise<{ result: T; availability: AvailabilitySample[] }> {
  const probes: Array<{ endpoint: string; check: () => Promise<true | string> }> = [
    {
      endpoint: "portal",
      check: async () => {
        const [page, api] = await Promise.all([
          (context.fetch ?? fetch)(`https://${context.options.albHost}/`, { signal: AbortSignal.timeout(SAMPLE_TIMEOUT_MS) }),
          context.api.request("Projects.ListProjects", {}),
        ]);
        await page.body?.cancel();
        if (!page.ok) return `page http ${page.status}`;
        return apiSucceeded(api) ? true : `api http ${api.status} ${JSON.stringify(api.body).slice(0, 120)}`;
      },
    },
    {
      endpoint: "scheduler",
      check: async () => {
        const api = await context.api.request("Scheduler.ListActiveJobs", {});
        return apiSucceeded(api) ? true : `api http ${api.status} ${JSON.stringify(api.body).slice(0, 120)}`;
      },
    },
    {
      endpoint: "gateway",
      check: async () => {
        const connection = await openGatewayConnection(context);
        connection.close();
        return true;
      },
    },
  ];
  const controlUrl = (context.options as { controlUrl?: string }).controlUrl ?? DEFAULT_CONTROL_URL;
  const control = { endpoint: "control", check: context.control ?? (() => freshGet(controlUrl, SAMPLE_TIMEOUT_MS)) };
  let finished = false;
  const availability: AvailabilitySample[] = [];
  // A bounded sample counts a timeout as a failure and a slow success as slow. A slow endpoint
  // cannot pause the others.
  async function sample(check: () => Promise<true | string>): Promise<Taken> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const started = context.now();
    try {
      const outcome = await Promise.race([
        check().catch((error: unknown) => `error ${error instanceof Error ? error.message : String(error)}`),
        new Promise<string>((resolve) => { timer = setTimeout(() => resolve(`timeout ${SAMPLE_TIMEOUT_MS}ms`), SAMPLE_TIMEOUT_MS); }),
      ]);
      const ended = context.now();
      return { started, ended, ok: outcome === true, reason: outcome === true ? "" : outcome, elapsedMs: ended - started };
    } finally {
      clearTimeout(timer);
    }
  }
  // Refuse to start a disruptive proof against an endpoint that is already unavailable.
  for (const probe of probes) {
    const first = await sample(probe.check);
    if (!first.ok) throw new Error(`${probe.endpoint} is unavailable before the upgrade: ${first.reason}`);
  }
  const taken = new Map<string, Taken[]>();
  const monitors = [...probes, control].map(async (probe) => {
    const rows: Taken[] = [];
    taken.set(probe.endpoint, rows);
    do {
      const started = context.now();
      rows.push(await sample(probe.check));
      if (finished) break;
      await context.sleep(Math.max(0, 1_000 - (context.now() - started)));
    } while (true);
  });
  let result: T;
  try {
    result = await upgrade();
  } finally {
    finished = true;
    await Promise.all(monitors);
    const controlRows = taken.get("control") ?? [];
    for (const probe of probes) availability.push(summarise(probe.endpoint, taken.get(probe.endpoint) ?? [], controlRows));
    context.output(`OBSERVED availability control=${controlUrl} samples=${controlRows.length} failures=${controlRows.filter((c) => !c.ok).length}`);
    for (const row of availability) {
      context.output(`OBSERVED availability endpoint=${row.endpoint} samples=${row.samples} failures=${row.failures} client_drops=${row.clientDrops ?? 0} slow=${row.slow} longest_gap_ms=${row.longestGapMs}`);
      for (const detail of row.failureDetails) context.output(`OBSERVED availability failure endpoint=${row.endpoint} at=${detail}`);
    }
  }
  return { result, availability };
}
