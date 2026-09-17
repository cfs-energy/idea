import { failed, passed, requiredOption } from "./shared.ts";
import type { ProofCheck } from "./types.ts";

export const metricsSinkCheck: ProofCheck = {
  name: "metrics-sink",
  description: "Verify the cluster's API invocation metrics reached Datadog in the last 15 minutes.",
  requiredFlags: (options) => options.datadogApiKey && options.datadogAppKey && !options.cluster?.trim() ? ["cluster"] : [],
  async run(context) {
    const { datadogApiKey, datadogAppKey, datadogSite } = context.options;
    if (!datadogApiKey || !datadogAppKey) {
      return { passed: false, skipped: true, observed: ["requires --datadog-api-key and --datadog-app-key"] };
    }
    if (!context.fetch) return failed("this runner cannot query Datadog");
    // The metric tag carries the IDEA cluster name; the runner is given the ECS cluster, named <cluster>-ecs.
    const cluster = requiredOption(context.options, "cluster").replace(/-ecs$/u, "");
    const to = Math.floor(context.now() / 1000);
    const url = new URL(`https://api.${datadogSite ?? "datadoghq.com"}/api/v1/query`);
    url.search = new URLSearchParams({ from: String(to - 900), to: String(to), query: `sum:idea.api_invocations{idea_cluster:${cluster}}` }).toString();
    context.output(`ACTION query Datadog for idea.api_invocations with idea_cluster:${cluster} over the last 15 minutes`);
    const response = await context.fetch(url, {
      headers: { "DD-API-KEY": datadogApiKey, "DD-APPLICATION-KEY": datadogAppKey },
      signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) return failed(`Datadog query returned HTTP ${response.status}`);
    const body = await response.json() as { status?: string; series?: Array<{ pointlist?: Array<[number, number | null]> }> };
    if (body.status !== "ok") return failed("Datadog query did not return status ok");
    let count = 0;
    let newest = 0;
    for (const series of body.series ?? []) {
      for (const [timestamp, value] of series.pointlist ?? []) {
        if (Number.isFinite(timestamp) && typeof value === "number" && Number.isFinite(value)) {
          count += 1;
          newest = Math.max(newest, timestamp);
        }
      }
    }
    const observation = `points=${count} newest=${count ? new Date(newest).toISOString() : "none"}`;
    return count > 0 ? passed(observation) : failed(observation);
  },
};
