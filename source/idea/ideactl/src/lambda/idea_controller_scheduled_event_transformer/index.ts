/** Input sent to SQS by the scheduled-event transformer. */
export interface SendMessageInput {
    QueueUrl: string | undefined;
    MessageBody: string;
    MessageGroupId: string;
}

/** Minimal SQS surface used by the handler and injected by unit tests. */
export interface ScheduledEventSqs {
    sendMessage(input: SendMessageInput): Promise<unknown>;
}

/** Logging surface matching the facts emitted by the Python handler. */
export interface ScheduledEventLogger {
    info(message: unknown): void;
    error(message: string, error?: unknown): void;
}

/** Injectable handler collaborators; production defaults are used by Lambda. */
export interface ScheduledEventHandlerDependencies {
    sqs?: () => ScheduledEventSqs | Promise<ScheduledEventSqs>;
    environment?: Readonly<NodeJS.ProcessEnv>;
    logger?: ScheduledEventLogger;
}

interface SqsSdkClient {
    send(command: object): Promise<unknown>;
}

interface SqsSdkModule {
    SQSClient: new (configuration: Record<string, never>) => SqsSdkClient;
    SendMessageCommand: new (input: SendMessageInput) => object;
}

const DETAIL_TYPE = "Scheduled Event";
const SCHEDULED_EVENT = "SCHEDULED_EVENT";
const QUEUE_URL_ENVIRONMENT_KEY = "IDEA_CONTROLLER_EVENTS_QUEUE_URL";
const SQS_CLIENT_MODULE = "@aws-sdk/client-sqs";

const defaultLogger: ScheduledEventLogger = {
    info: (message: unknown): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
};

/** Narrow a dynamic import without using an unchecked type assertion. */
function isSqsSdkModule(value: unknown): value is SqsSdkModule {
    return (
        typeof value === "object" &&
        value !== null &&
        "SQSClient" in value &&
        typeof value.SQSClient === "function" &&
        "SendMessageCommand" in value &&
        typeof value.SendMessageCommand === "function"
    );
}

/** Load the v3 SQS client dynamically. */
async function createSqs(): Promise<ScheduledEventSqs> {
    const importedModule: unknown = await import(SQS_CLIENT_MODULE);
    if (!isSqsSdkModule(importedModule)) {
        throw new Error(`${SQS_CLIENT_MODULE} does not export the SQS v3 client`);
    }

    const client = new importedModule.SQSClient({});
    return {
        sendMessage: async (input: SendMessageInput): Promise<unknown> =>
            client.send(new importedModule.SendMessageCommand(input)),
    };
}

/** Narrow a JSON object to string keys and unknown values. */
function isUnknownRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null;
}

/** Read a required event key with Python dictionary-subscript semantics. */
function requiredEventValue(event: unknown, key: string): unknown {
    if (!isUnknownRecord(event) || !Object.hasOwn(event, key)) {
        throw new Error(`missing event key: ${key}`);
    }
    return event[key];
}

/** Quote a string like Python json.dumps(..., ensure_ascii=True). */
function pythonJsonString(value: string): string {
    let result = "\"";
    for (let index = 0; index < value.length; index += 1) {
        const character = value[index];
        const code = value.charCodeAt(index);
        if (character === "\"" || character === "\\") {
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
 * Serialize the EventBridge time value using Python json.dumps separators.
 * Lambda events are JSON values; unsupported JavaScript-only values fail and
 * are swallowed by the outer handler exactly like Python's json.dumps error.
 */
function pythonJsonDumps(value: unknown): string {
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
    if (Array.isArray(value)) {
        return `[${value.map(pythonJsonDumps).join(", ")}]`;
    }
    if (typeof value === "object") {
        return `{${Object.entries(value)
            .map(
                ([key, entryValue]) =>
                    `${pythonJsonString(key)}: ${pythonJsonDumps(entryValue)}`,
            )
            .join(", ")}}`;
    }
    throw new TypeError(`Object of type ${typeof value} is not JSON serializable`);
}

/** Build the byte-compatible Python json.dumps message body. */
function scheduledMessageBody(time: unknown): string {
    return [
        `{"event_group_id": "${SCHEDULED_EVENT}", `,
        `"event_type": "${SCHEDULED_EVENT}", `,
        `"detail": {"time": ${pythonJsonDumps(time)}}}`,
    ].join("");
}

/**
 * Build the EventBridge handler. Non-scheduled events are ignored, while all
 * malformed-input and SQS errors are logged and swallowed by design.
 */
export function createHandler(
    dependencies: ScheduledEventHandlerDependencies = {},
): (event: unknown, context: unknown) => Promise<void> {
    const environment = dependencies.environment ?? process.env;
    const logger = dependencies.logger ?? defaultLogger;

    return async (event: unknown, _context: unknown): Promise<void> => {
        try {
            const detailType = requiredEventValue(event, "detail-type");
            if (detailType !== DETAIL_TYPE) {
                return;
            }

            const forwardingEventBody = scheduledMessageBody(
                requiredEventValue(event, "time"),
            );
            logger.info("Forwarding scheduled event to Controller");
            const sqs = await (dependencies.sqs ?? createSqs)();
            const response = await sqs.sendMessage({
                QueueUrl: environment[QUEUE_URL_ENVIRONMENT_KEY],
                MessageBody: forwardingEventBody,
                MessageGroupId: SCHEDULED_EVENT,
            });
            logger.info(response);
        } catch (error: unknown) {
            logger.error(
                `error in handling scheduled event: ${JSON.stringify(event)}, error: ${
                    error instanceof Error ? error.message : String(error)
                }`,
                error,
            );
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
