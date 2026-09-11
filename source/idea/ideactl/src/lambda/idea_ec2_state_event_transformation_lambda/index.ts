import {
    DescribeInstancesCommand,
    EC2Client,
} from "@aws-sdk/client-ec2";
import type {
    DescribeInstancesCommandInput,
    DescribeInstancesCommandOutput,
} from "@aws-sdk/client-ec2";
import { inspect } from "node:util";
import type {
    CfnLogger,
    JsonValue,
} from "../commons/cfn-response.ts";
import { errorMessage, pythonJsonString } from "../commons/cfn-response.ts";

export const EC2_STATE_CHANGE_DETAIL_TYPE =
    "EC2 Instance State-change Notification";
export const EVENT_NAMESPACE = "Ec2.StateChangeEvent";
export const ATTRIBUTE_NAME_SANITIZER = /[^a-zA-Z0-9_\-\.]+/g;

type JsonObject = { [key: string]: JsonValue };

export interface Ec2StateDetail {
    [key: string]: JsonValue | undefined;
    "instance-id": string;
    state: string;
    tags?: Record<string, string>;
}

export interface Ec2StateEvent {
    "detail-type": string;
    detail: Ec2StateDetail;
}

export interface SnsMessageAttribute {
    DataType: "String";
    StringValue: string;
}

export interface PublishInput {
    TopicArn: string | undefined;
    MessageStructure: "json";
    MessageAttributes: Record<string, SnsMessageAttribute>;
    Message: string;
}

/** Injectable EC2 operation used to load one instance's tags. */
export interface Ec2StateEc2 {
    describeInstances(
        input: DescribeInstancesCommandInput,
    ): Promise<DescribeInstancesCommandOutput>;
}

/** Injectable SNS operation used to forward a transformed state event. */
export interface Ec2StateSns {
    publish(input: PublishInput): Promise<unknown>;
}

/** Collaborators and environment used by unit tests and the Lambda handler. */
export interface Ec2StateHandlerDependencies {
    ec2?: Ec2StateEc2;
    sns?: Ec2StateSns;
    env?: Readonly<Record<string, string | undefined>>;
    logger?: CfnLogger;
}

interface SnsSdk {
    SNSClient: new (configuration: Record<string, never>) => {
        send(command: object): Promise<unknown>;
    };
    PublishCommand: new (input: PublishInput) => object;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
};

const defaultEc2Client = new EC2Client({});
const defaultEc2: Ec2StateEc2 = {
    describeInstances: (
        input: DescribeInstancesCommandInput,
    ): Promise<DescribeInstancesCommandOutput> =>
        defaultEc2Client.send(new DescribeInstancesCommand(input)),
};

let defaultSnsPromise: Promise<Ec2StateSns> | undefined;

/** Narrow the dynamically loaded SNS client package without using `any`. */
function isSnsSdk(value: unknown): value is SnsSdk {
    if (typeof value !== "object" || value === null) {
        return false;
    }

    return (
        "SNSClient" in value &&
        typeof value.SNSClient === "function" &&
        "PublishCommand" in value &&
        typeof value.PublishCommand === "function"
    );
}

/** Load the requested SNS v3 client lazily. */
async function loadDefaultSns(): Promise<Ec2StateSns> {
    const moduleName = ["@aws-sdk", "client-sns"].join("/");
    const moduleValue: unknown = await import(moduleName);
    if (!isSnsSdk(moduleValue)) {
        throw new TypeError(`${moduleName} does not export the SNS v3 client`);
    }

    const client = new moduleValue.SNSClient({});
    return {
        publish: (input: PublishInput): Promise<unknown> =>
            client.send(new moduleValue.PublishCommand(input)),
    };
}

/** Reuse the production SNS client across warm Lambda invocations. */
function getDefaultSns(): Promise<Ec2StateSns> {
    defaultSnsPromise ??= loadDefaultSns();
    return defaultSnsPromise;
}

/** Serialize the SNS message with Python json.dumps' default whitespace. */
export function pythonJsonDumps(value: JsonValue): string {
    if (value === null) {
        return "null";
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

/** Require one EC2 tag field. */
function tagField(
    tag: { Key?: string; Value?: string },
    field: "Key" | "Value",
    context: { detailType: string; instanceId: string; clusterName: string | undefined },
): string {
    const value = tag[field];
    if (value === undefined) {
        const cluster =
            context.clusterName === undefined
                ? "cluster name is unset (IDEA_CLUSTER_NAME_TAG_VALUE)"
                : `cluster ${context.clusterName}`;
        throw new Error(
            `EC2 tag is missing field ${field}. The event was ${context.detailType} for instance ${context.instanceId} in ${cluster}.`,
        );
    }
    return value;
}

/** Format logged objects without dropping any event or response fields. */
function loggedValue(value: unknown): string {
    return inspect(value, { breakLength: Number.POSITIVE_INFINITY, compact: true });
}

/** Validate that an EventBridge detail contains only JSON values. */
function detailPayload(detail: Ec2StateDetail): JsonObject {
    const payload: JsonObject = {};
    for (const [key, value] of Object.entries(detail)) {
        if (value === undefined) {
            throw new TypeError(`Object of type undefined is not JSON serializable: ${key}`);
        }
        payload[key] = value;
    }
    return payload;
}

/**
 * Build the EventBridge handler. All failures are logged and swallowed so a
 * malformed event or transient AWS error is not retried by this Lambda.
 */
export function createHandler(
    dependencies: Ec2StateHandlerDependencies = {},
): (event: Ec2StateEvent, context: unknown) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const env = dependencies.env ?? process.env;
    const ec2 = dependencies.ec2 ?? defaultEc2;

    return async (event: Ec2StateEvent, _context: unknown): Promise<void> => {
        try {
            const detailType = event["detail-type"];
            if (detailType !== EC2_STATE_CHANGE_DETAIL_TYPE) {
                logger.error(`ERROR. Invalid detail type ${detailType}`);
                return;
            }

            const clusterNameTagKey = env.IDEA_CLUSTER_NAME_TAG_KEY;
            const clusterNameTagValue = env.IDEA_CLUSTER_NAME_TAG_VALUE;
            const ideaTagPrefix = env.IDEA_TAG_PREFIX;

            const instanceId = event.detail["instance-id"];
            const state = event.detail.state;
            // Initialize tags before loading instance tags.
            event.detail.tags = {};
            const response = await ec2.describeInstances({
                InstanceIds: [instanceId],
            });
            const instanceTags =
                response.Reservations?.[0]?.Instances?.[0]?.Tags;
            if (instanceTags === undefined) {
                throw new TypeError("'NoneType' object is not iterable");
            }

            let clusterMatch = false;
            const messageAttributes: Record<string, SnsMessageAttribute> = {};

            for (const tag of instanceTags) {
                const key = tagField(tag, "Key", {
                    detailType,
                    instanceId,
                    clusterName: clusterNameTagValue,
                });
                if (ideaTagPrefix === undefined) {
                    throw new TypeError("startswith first arg must be str or a tuple of str, not NoneType");
                }
                if (key.startsWith(ideaTagPrefix)) {
                    const value = tagField(tag, "Value", {
                        detailType,
                        instanceId,
                        clusterName: clusterNameTagValue,
                    });
                    event.detail.tags[key] = value;
                    messageAttributes[
                        key.replace(ATTRIBUTE_NAME_SANITIZER, "_").trim()
                    ] = {
                        DataType: "String",
                        StringValue: value,
                    };
                }

                if (
                    key === clusterNameTagKey &&
                    tagField(tag, "Value", {
                        detailType,
                        instanceId,
                        clusterName: clusterNameTagValue,
                    }) === clusterNameTagValue
                ) {
                    clusterMatch = true;
                }
            }

            if (!clusterMatch) {
                logger.info(
                    `tag_key(s): ${clusterNameTagKey ?? "None"} and tag_value(s): ${clusterNameTagValue ?? "None"} on instance-id: ${instanceId} not found. NO=OP.`,
                );
                return;
            }

            const forwardingEvent: JsonObject = {
                header: {
                    namespace: EVENT_NAMESPACE,
                    request_id: instanceId,
                },
                payload: detailPayload(event.detail),
            };

            logger.info(
                `forwarding ec2-state-event for ${instanceId} for state ${state}`,
            );
            const sns = dependencies.sns ?? (await getDefaultSns());
            const publishResponse = await sns.publish({
                TopicArn: env.IDEA_EC2_STATE_SNS_TOPIC_ARN,
                MessageStructure: "json",
                MessageAttributes: messageAttributes,
                Message: pythonJsonDumps({
                    default: pythonJsonDumps(forwardingEvent),
                    sqs: forwardingEvent,
                }),
            });

            logger.info(loggedValue(publishResponse));
        } catch (error: unknown) {
            logger.error(
                `Error in Handling ec2 state change event: ${loggedValue(event)}, error: ${errorMessage(error)}`,
                error,
            );
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
