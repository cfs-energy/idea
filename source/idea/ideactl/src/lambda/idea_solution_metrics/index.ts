import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import type {
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import {
    errorMessage,
    pythonJsonString,
    sendCfnResponse,
} from "../commons/cfn-response.ts";

const PHYSICAL_RESOURCE_ID = "SolutionMetricsSO0072";
const SOLUTION_ID = "SO0072";
const DEFAULT_METRICS_URL =
    "https://metrics.awssolutionsbuilder.com/generic";
const METRIC_DENYLIST_KEYS = ["ServiceToken"];

type SolutionMetricsProperties = Record<string, JsonValue>;

export type SolutionMetricsEvent =
    CfnResourceEvent<SolutionMetricsProperties>;

/** HTTP request shape used by the metrics endpoint and unit-test stubs. */
export interface MetricsPostRequest {
    method: "POST";
    url: string;
    body: Buffer;
    headers: Readonly<Record<string, string>>;
}

/** Minimal HTTP response used by the Python handler. */
export interface MetricsPostResponse {
    status: number;
}

/** Logging surface for the facts logged by the Python handler. */
export interface SolutionMetricsLogger {
    info(message: unknown): void;
    error(message: string, error?: unknown): void;
}

/** Injectable collaborators keep unit tests offline and deterministic. */
export interface SolutionMetricsDependencies {
    environment?: Readonly<NodeJS.ProcessEnv>;
    logger?: SolutionMetricsLogger;
    post?: (request: MetricsPostRequest) => Promise<MetricsPostResponse>;
    responseSender?: CfnResponseSender;
    timestamp?: () => string;
}

const defaultLogger: SolutionMetricsLogger = {
    info: (message: unknown): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
};

// Retry connection failures and follow redirects three times.
const MAX_CONNECT_RETRIES = 3;
const MAX_REDIRECTS = 3;
const CONNECT_ERROR_CODES = new Set([
    "ECONNREFUSED",
    "EHOSTUNREACH",
    "ENETUNREACH",
    "ENOTFOUND",
    "EAI_AGAIN",
    "ETIMEDOUT",
]);

interface RawResponse {
    status: number;
    location?: string;
}

/** True for the failures urllib3 classifies as connection (not read) errors. */
function isConnectError(error: unknown): boolean {
    const code = (error as { code?: unknown } | null)?.code;
    return typeof code === "string" && CONNECT_ERROR_CODES.has(code);
}

/** Send one HTTP request and return its status after draining the response. */
async function sendOnce(
    url: URL,
    method: string,
    headers: Readonly<Record<string, string>>,
    body: Buffer,
): Promise<RawResponse> {
    const requestFunction =
        url.protocol === "http:"
            ? httpRequest
            : url.protocol === "https:"
              ? httpsRequest
              : undefined;
    if (requestFunction === undefined) {
        throw new Error(`Unsupported metrics URL protocol: ${url.protocol}`);
    }

    return new Promise<RawResponse>((resolve, reject) => {
        const outgoing = requestFunction(
            url,
            {
                method,
                headers: {
                    ...headers,
                    "content-length": String(body.byteLength),
                },
            },
            (incoming) => {
                const status = incoming.statusCode ?? 0;
                const location = incoming.headers.location;
                incoming.resume();
                resolve({ status, location });
            },
        );
        outgoing.once("error", reject);
        outgoing.end(body);
    });
}

/** Post with urllib3's connect-retry and redirect-following defaults. */
async function nativePost(
    request: MetricsPostRequest,
): Promise<MetricsPostResponse> {
    let url = new URL(request.url);
    let method: string = request.method;
    let body = request.body;
    let connectFailures = 0;
    let redirects = 0;

    for (;;) {
        let response: RawResponse;
        try {
            response = await sendOnce(url, method, request.headers, body);
        } catch (error: unknown) {
            if (isConnectError(error) && connectFailures < MAX_CONNECT_RETRIES) {
                connectFailures += 1;
                continue;
            }
            throw error;
        }

        const redirectable =
            response.status >= 300 &&
            response.status < 400 &&
            response.location !== undefined &&
            redirects < MAX_REDIRECTS;
        if (!redirectable) {
            return { status: response.status };
        }

        redirects += 1;
        url = new URL(response.location as string, url);
        if (response.status === 303) {
            // A 303 uses a bodyless GET. Other redirects replay the POST.
            method = "GET";
            body = Buffer.alloc(0);
        }
    }
}

/**
 * Format UTC like Python's naive datetime.isoformat(). JavaScript supplies
 * millisecond precision, so non-zero fractions are padded to six digits.
 */
function currentPythonUtcTimestamp(): string {
    const isoTimestamp = new Date().toISOString();
    const wholeSeconds = isoTimestamp.slice(0, 19);
    const milliseconds = isoTimestamp.slice(20, 23);
    return milliseconds === "000"
        ? wholeSeconds
        : `${wholeSeconds}.${milliseconds}000`;
}

/** Serialize JSON with Python json.dumps(..., indent=4) whitespace. */
function pythonIndentedJson(value: JsonValue, level = 0): string {
    if (value === null) {
        return "null";
    }
    if (typeof value === "string") {
        return pythonJsonString(value);
    }
    if (typeof value === "boolean") {
        return value ? "true" : "false";
    }
    if (typeof value === "number") {
        if (Number.isNaN(value)) {
            return "NaN";
        }
        if (value === Number.POSITIVE_INFINITY) {
            return "Infinity";
        }
        if (value === Number.NEGATIVE_INFINITY) {
            return "-Infinity";
        }
        return String(value);
    }

    const nextLevel = level + 1;
    const nextIndent = " ".repeat(nextLevel * 4);
    const closingIndent = " ".repeat(level * 4);
    if (Array.isArray(value)) {
        if (value.length === 0) {
            return "[]";
        }
        return [
            "[",
            value
                .map(
                    (entry) =>
                        `${nextIndent}${pythonIndentedJson(entry, nextLevel)}`,
                )
                .join(",\n"),
            `${closingIndent}]`,
        ].join("\n");
    }

    const entries = Object.entries(value);
    if (entries.length === 0) {
        return "{}";
    }
    return [
        "{",
        entries
            .map(
                ([key, entry]) =>
                    `${nextIndent}${pythonJsonString(key)}: ${pythonIndentedJson(entry, nextLevel)}`,
            )
            .join(",\n"),
        `${closingIndent}}`,
    ].join("\n");
}

/** Identify a property bag without an unchecked type assertion. */
function isUnknownRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Narrow an event or property bag before Python-style key access. */
function requireRecord(
    value: unknown,
    missingKey: string,
): Record<string, unknown> {
    if (!isUnknownRecord(value)) {
        throw new Error(`Custom resource event is missing field ${missingKey}.`);
    }
    return value;
}

/** Read a required event key with Python dictionary-subscript behavior. */
function requiredEventValue(event: unknown, key: string): unknown {
    const record = requireRecord(event, key);
    if (!Object.hasOwn(record, key)) {
        throw new Error(`Custom resource event is missing field ${key}.`);
    }
    return record[key];
}

/** Narrow a runtime value to the JSON values delivered by Lambda. */
function isJsonValue(value: unknown): value is JsonValue {
    if (
        value === null ||
        typeof value === "string" ||
        typeof value === "boolean" ||
        typeof value === "number"
    ) {
        return true;
    }
    if (Array.isArray(value)) {
        return value.every(isJsonValue);
    }
    return (
        typeof value === "object" &&
        value !== null &&
        Object.values(value).every(isJsonValue)
    );
}

/** Build and post the anonymous metrics payload, swallowing every failure. */
export async function postMetrics(
    event: unknown,
    dependencies: SolutionMetricsDependencies = {},
): Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;

    try {
        const requestTimestamp =
            dependencies.timestamp?.() ?? currentPythonUtcTimestamp();
        const requestId = requiredEventValue(event, "RequestId");
        const requestType = requiredEventValue(event, "RequestType");
        const properties = requireRecord(
            requiredEventValue(event, "ResourceProperties"),
            "ResourceProperties",
        );
        if (!isJsonValue(requestId) || !isJsonValue(requestType)) {
            throw new TypeError(
                "RequestId and RequestType must be JSON serializable",
            );
        }

        const data: Record<string, JsonValue> = {
            RequestType: requestType,
            RequestTimeStamp: requestTimestamp,
        };
        for (const [key, value] of Object.entries(properties)) {
            if (
                !Object.hasOwn(data, key) &&
                !METRIC_DENYLIST_KEYS.includes(key)
            ) {
                if (!isJsonValue(value)) {
                    throw new TypeError(
                        `Object of type ${typeof value} is not JSON serializable`,
                    );
                }
                // Plain assignment would invoke the prototype setter and silently
                // drop a property literally named __proto__.
                Object.defineProperty(data, key, {
                    value,
                    writable: true,
                    enumerable: true,
                    configurable: true,
                });
            }
        }

        const params = {
            Solution: SOLUTION_ID,
            UUID: requestId,
            Data: data,
        };
        const metrics: Record<string, JsonValue> = {
            TimeStamp: requestTimestamp,
            ...params,
        };
        const jsonData = pythonIndentedJson(metrics);
        logger.info(params);

        const response = await (dependencies.post ?? nativePost)({
            method: "POST",
            url:
                (dependencies.environment ?? process.env).AWS_METRICS_URL ??
                DEFAULT_METRICS_URL,
            body: Buffer.from(jsonData, "utf8"),
            headers: { "content-type": "application/json" },
        });
        logger.info(`ResponseCode: ${response.status}`);
    } catch (error: unknown) {
        logger.error(`failed to post metrics: ${errorMessage(error)}`, error);
    }
}

/** Build the never-fail custom-resource handler with injectable collaborators. */
export function createHandler(
    dependencies: SolutionMetricsDependencies = {},
): (
    event: SolutionMetricsEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: SolutionMetricsEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        try {
            await postMetrics(event, dependencies);
        } catch (error: unknown) {
            // Catch failures from postMetrics.
            logger.error(`failed to post metrics: ${errorMessage(error)}`, error);
        } finally {
            await responseSender(
                {
                    context,
                    event,
                    status: "SUCCESS",
                    data: {},
                    physicalResourceId: PHYSICAL_RESOURCE_ID,
                },
                { logger },
            );
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
