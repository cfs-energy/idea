import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

/** JSON values accepted by CloudFormation custom-resource properties and data. */
export type JsonValue =
    | boolean
    | number
    | string
    | null
    | JsonValue[]
    | { [key: string]: JsonValue };

/** CloudFormation request types delivered to custom-resource handlers. */
export type CfnRequestType = "Create" | "Update" | "Delete";

/** Minimal Lambda event shape shared by IDEA custom-resource handlers. */
export interface CfnResourceEvent<
    Properties extends Record<string, JsonValue> = Record<string, JsonValue>,
> {
    RequestType: CfnRequestType;
    ResponseURL: string;
    StackId?: string;
    RequestId?: string;
    LogicalResourceId?: string;
    PhysicalResourceId?: string;
    ResourceProperties: Properties;
    OldResourceProperties?: Properties;
}

/** Minimal Lambda context used by the Python response helper. */
export interface CfnResourceContext {
    logStreamName: string;
}

/** Logging surface used by the helper and injectable in tests. */
export interface CfnLogger {
    info(message: string): void;
    error(message: string, error?: unknown): void;
}

/** Values used to build the exact CloudFormation response body. */
export interface CfnResponse {
    context: CfnResourceContext;
    event: CfnResourceEvent;
    status: "SUCCESS" | "FAILED";
    physicalResourceId?: string;
    noEcho?: boolean | null;
    reason?: string;
    data?: Record<string, JsonValue> | null;
}

/** A hand-rolled HTTP PUT request, exposed so tests do not make network calls. */
export interface CfnResponsePutRequest {
    url: string;
    headers: Readonly<Record<string, string>>;
    body: string;
}

export type CfnResponsePut = (request: CfnResponsePutRequest) => Promise<number>;

/** Optional collaborators for response delivery. */
export interface SendCfnResponseOptions {
    logger?: CfnLogger;
    logResponse?: boolean;
    put?: CfnResponsePut;
}

/** Result returned by business logic wrapped as a custom-resource handler. */
export interface CfnHandlerResult {
    physicalResourceId?: string;
    noEcho?: boolean | null;
    reason?: string;
    data?: Record<string, JsonValue> | null;
}

export type CfnResponseSender = (
    response: CfnResponse,
    options?: SendCfnResponseOptions,
) => Promise<void>;

/** Options controlling common success and failure handling. */
export interface CfnHandlerWrapperOptions<Event extends CfnResourceEvent> {
    physicalResourceId?: (event: Event) => string | undefined;
    errorMessage?: (error: unknown, event: Event) => string;
    failureReason?: (error: unknown, event: Event) => string | undefined;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
    responseOptions?: SendCfnResponseOptions;
}

const consoleLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void => console.error(message, error),
};

/**
 * Return an exception's message using Python's `str(exception)` behavior for
 * ordinary Error objects.
 */
export function errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

/**
 * Quote one string exactly like json.dumps(..., ensure_ascii=True). Handlers that need the same
 * quoting with a different separator or indent layout share this and keep their own layout local.
 */
export function pythonJsonString(value: string): string {
    let result = '"';
    for (let index = 0; index < value.length; index += 1) {
        const code = value.charCodeAt(index);
        const character = value[index];
        if (character === '"' || character === "\\") {
            result += `\\${character}`;
        } else if (character === "\b") {
            result += "\\b";
        } else if (character === "\f") {
            result += "\\f";
        } else if (character === "\n") {
            result += "\\n";
        } else if (character === "\r") {
            result += "\\r";
        } else if (character === "\t") {
            result += "\\t";
        } else if (code < 0x20 || code > 0x7e) {
            result += `\\u${code.toString(16).padStart(4, "0")}`;
        } else {
            result += character;
        }
    }
    return `${result}"`;
}

/**
 * Serialize JSON with Python json.dumps' default comma and colon separators.
 * CloudFormation only supplies JSON-compatible values, so no replacer is needed.
 */
function pythonJsonDumps(value: JsonValue): string {
    if (value === null) {
        return "null";
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
    if (typeof value === "boolean") {
        return value ? "true" : "false";
    }
    if (typeof value === "string") {
        return pythonJsonString(value);
    }
    if (Array.isArray(value)) {
        return `[${value.map(pythonJsonDumps).join(", ")}]`;
    }
    return `{${Object.entries(value)
        .map(
            ([key, entryValue]) =>
                `${pythonJsonString(key)}: ${pythonJsonDumps(entryValue)}`,
        )
        .join(", ")}}`;
}

/**
 * Build the response payload with the same field order, defaults, and null
 * values as idea_lambda_commons.CfnResponse.
 */
export function buildCfnResponsePayload(response: CfnResponse): string {
    const reason =
        response.reason ??
        `See the details in CloudWatch Log Stream: ${response.context.logStreamName}`;
    const physicalResourceId =
        response.physicalResourceId ?? response.context.logStreamName;

    return pythonJsonDumps({
        Status: response.status,
        Reason: reason,
        PhysicalResourceId: physicalResourceId,
        StackId: response.event.StackId ?? null,
        RequestId: response.event.RequestId ?? null,
        LogicalResourceId: response.event.LogicalResourceId ?? null,
        NoEcho: response.noEcho === undefined ? false : response.noEcho,
        Data: response.data === undefined ? null : response.data,
    });
}

/** Send one PUT with Node built-ins and return the HTTP status code. */
async function nativePut(request: CfnResponsePutRequest): Promise<number> {
    const url = new URL(request.url);
    const requestFunction =
        url.protocol === "http:"
            ? httpRequest
            : url.protocol === "https:"
              ? httpsRequest
              : undefined;
    if (requestFunction === undefined) {
        throw new Error(`Unsupported ResponseURL protocol: ${url.protocol}`);
    }

    return new Promise<number>((resolve, reject) => {
        const outgoing = requestFunction(
            url,
            {
                method: "PUT",
                headers: request.headers,
                // Disable connection pooling for this request.
                agent: false,
            },
            (incoming) => {
                const statusCode = incoming.statusCode ?? 0;
                incoming.resume();
                resolve(statusCode);
            },
        );
        outgoing.once("error", reject);
        outgoing.end(request.body);
    });
}

/**
 * Send a CloudFormation response and swallow every serialization, URL, and
 * transport failure so the Lambda handler itself never throws.
 */
export async function sendCfnResponse(
    response: CfnResponse,
    options: SendCfnResponseOptions = {},
): Promise<void> {
    const logger = options.logger ?? consoleLogger;

    try {
        const body = buildCfnResponsePayload(response);
        logger.info(
            options.logResponse === false
                ? "SendingResponse: REDACTED"
                : `SendingResponse: ${body}`,
        );
        const statusCode = await (options.put ?? nativePut)({
            url: response.event.ResponseURL,
            headers: {
                "content-type": "",
                // Python uses len(str), rather than UTF-8 byte length.
                "content-length": String(body.length),
            },
            body,
        });
        logger.info(`StatusCode: ${statusCode}`);
    } catch (error: unknown) {
        logger.error(`SendResponseFailed, Error: ${errorMessage(error)}`, error);
    }
}

/**
 * Wrap handler business logic and swallow response failures.
 */
export function wrapCfnHandler<Event extends CfnResourceEvent>(
    operation: (event: Event, context: CfnResourceContext) => Promise<CfnHandlerResult | void>,
    options: CfnHandlerWrapperOptions<Event> = {},
): (event: Event, context: CfnResourceContext) => Promise<void> {
    const logger = options.logger ?? consoleLogger;
    const responseSender = options.responseSender ?? sendCfnResponse;

    return async (event: Event, context: CfnResourceContext): Promise<void> => {
        let physicalResourceId: string | undefined;

        try {
            physicalResourceId = options.physicalResourceId?.(event);
            const result = await operation(event, context);
            await responseSender(
                {
                    context,
                    event,
                    status: "SUCCESS",
                    physicalResourceId:
                        result?.physicalResourceId ?? physicalResourceId,
                    noEcho: result?.noEcho,
                    reason: result?.reason,
                    data: result?.data ?? {},
                },
                options.responseOptions,
            );
        } catch (error: unknown) {
            // Avoid a second error while building the failure message.
            let message = `HandlerFailed: ${errorMessage(error)}`;
            try {
                message = options.errorMessage?.(error, event) ?? message;
            } catch {
                // Use the default message.
            }
            logger.error(message, error);

            try {
                await responseSender(
                    {
                        context,
                        event,
                        status: "FAILED",
                        physicalResourceId,
                        reason: options.failureReason?.(error, event),
                        data: {},
                    },
                    options.responseOptions,
                );
            } catch (sendError: unknown) {
                logger.error(
                    `SendResponseFailed, Error: ${errorMessage(sendError)}`,
                    sendError,
                );
            }
        }
    };
}
