import { request as httpsRequest } from "node:https";

/**
 * Kinesis sink for the analytics stream: every module writes its documents onto the stream and
 * this turns one batch into one OpenSearch `_bulk` request.
 *
 * The domain is reached over its VPC endpoint under a resource policy, so the request carries no
 * authentication, which is what the Python handler did and what the domain still expects.
 */

/** The two log lines this handler produces. */
export interface AnalyticsSinkLogger {
    info(message: string): void;
    error(message: string, error?: unknown): void;
}

/** One `_bulk` response, narrowed to what the summary line reads. */
export interface BulkResponse {
    status: number;
    body: string;
}

/** The request the handler makes, injectable so unit tests stay offline. */
export type BulkRequest = (input: {
    url: string;
    body: string;
}) => Promise<BulkResponse>;

export interface AnalyticsSinkDependencies {
    endpoint?: string;
    request?: BulkRequest;
    logger?: AnalyticsSinkLogger;
}

interface KinesisRecord {
    kinesis: { data: string };
}

export interface AnalyticsSinkEvent {
    Records: KinesisRecord[];
}

/** One decoded stream record. `entry` is absent on a delete. */
interface AnalyticsEntry {
    document_id: string;
    index_id: string;
    action: string;
    entry?: unknown;
    timestamp: number | string;
}

const CREATE_ENTRY = "CREATE_ENTRY";
const UPDATE_ENTRY = "UPDATE_ENTRY";
const CONTENT_TYPE = "application/x-ndjson";

const defaultLogger: AnalyticsSinkLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
};

/** Compare two timestamps the way Python's `sorted` does, numbers or strings alike. */
function compareTimestamps(
    left: number | string,
    right: number | string,
): number {
    if (left === right) return 0;
    return left < right ? -1 : 1;
}

/** Decode one Kinesis record's base64 payload. */
export function decodeRecord(record: KinesisRecord): AnalyticsEntry {
    return JSON.parse(
        Buffer.from(record.kinesis.data, "base64").toString("utf8"),
    ) as AnalyticsEntry;
}

/**
 * The newline-delimited bulk body, oldest record first. The sort is stable, so two records
 * carrying the same timestamp keep the order the stream delivered them in.
 */
export function buildBulkBody(entries: readonly AnalyticsEntry[]): string {
    const lines: string[] = [];
    for (const entry of [...entries].sort((left, right) =>
        compareTimestamps(left.timestamp, right.timestamp),
    )) {
        const target = { _index: entry.index_id, _id: entry.document_id };
        if (entry.action === CREATE_ENTRY) {
            lines.push(JSON.stringify({ create: target }));
            lines.push(JSON.stringify(entry.entry));
        } else if (entry.action === UPDATE_ENTRY) {
            lines.push(JSON.stringify({ update: target }));
            lines.push(JSON.stringify({ doc: entry.entry }));
        } else {
            lines.push(JSON.stringify({ delete: target }));
        }
    }
    // A bulk body ends with a newline; without it OpenSearch rejects the last action.
    return lines.length === 0 ? "" : `${lines.join("\n")}\n`;
}

/** `https://<host>/_bulk`, whether or not the configured endpoint carries a scheme. */
export function bulkUrl(endpoint: string): string {
    return `${endpoint.startsWith("https://") ? endpoint : `https://${endpoint}`}/_bulk`;
}

/** POST the bulk body over TLS with no authentication. */
const nativeRequest: BulkRequest = async ({ url, body }) => {
    const payload = Buffer.from(body, "utf8");
    return new Promise<BulkResponse>((resolve, reject) => {
        const outgoing = httpsRequest(
            url,
            {
                method: "POST",
                headers: {
                    "content-type": CONTENT_TYPE,
                    "content-length": String(payload.byteLength),
                },
            },
            (incoming) => {
                const chunks: Buffer[] = [];
                incoming.on("data", (chunk: Buffer) => chunks.push(chunk));
                incoming.on("end", () =>
                    resolve({
                        status: incoming.statusCode ?? 0,
                        body: Buffer.concat(chunks).toString("utf8"),
                    }),
                );
            },
        );
        outgoing.once("error", reject);
        outgoing.end(payload);
    });
};

/** One line naming what the domain did with the batch. */
function responseSummary(actions: number, response: BulkResponse): string {
    let errors: unknown = "unknown";
    let items: unknown = "unknown";
    try {
        const parsed = JSON.parse(response.body) as {
            errors?: unknown;
            items?: unknown[];
        };
        errors = parsed.errors;
        items = Array.isArray(parsed.items) ? parsed.items.length : "unknown";
    } catch {
        // a body that is not JSON is itself the summary
    }
    return `bulk request of ${actions} action(s): status ${response.status}, items ${String(items)}, errors ${String(errors)}`;
}

/** Build the sink handler with an injectable request function. */
export function createHandler(
    dependencies: AnalyticsSinkDependencies = {},
): (event: AnalyticsSinkEvent) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const post = dependencies.request ?? nativeRequest;

    return async (event: AnalyticsSinkEvent): Promise<void> => {
        try {
            const entries = (event.Records ?? []).map(decodeRecord);
            const body = buildBulkBody(entries);
            if (body === "") {
                logger.info("no analytics records in this batch");
                return;
            }
            const endpoint =
                dependencies.endpoint ?? process.env.opensearch_endpoint ?? "";
            logger.info(responseSummary(entries.length, await post({ url: bulkUrl(endpoint), body })));
        } catch (error: unknown) {
            // Never throw. A thrown error makes the stream event source retry the whole batch and
            // block the shard behind it until the records age out; the Python handler swallowed
            // for the same reason.
            logger.error(
                `error while processing analytics request: ${error instanceof Error ? error.message : String(error)}`,
                error,
            );
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
