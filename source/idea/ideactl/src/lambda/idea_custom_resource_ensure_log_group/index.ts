import type {
    CfnLogger,
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import {
    errorMessage,
    sendCfnResponse,
} from "../commons/cfn-response.ts";

const CLOUDWATCH_LOGS_CLIENT_PACKAGE = "@aws-sdk/client-cloudwatch-logs";
const RESOURCE_ALREADY_EXISTS_EXCEPTION = "ResourceAlreadyExistsException";

type EnsureLogGroupProperties = Record<string, JsonValue> & {
    LogGroupName: string;
    RetentionInDays?: string;
};

export type EnsureLogGroupEvent =
    CfnResourceEvent<EnsureLogGroupProperties>;

export type EnsureLogGroupOutcome = "created" | "adopted";

/** Minimal CloudWatch Logs operations used by this Lambda. */
export interface EnsureLogGroupLogs {
    createLogGroup(logGroupName: string): Promise<void>;
    putRetentionPolicy(
        logGroupName: string,
        retentionInDays: number,
    ): Promise<void>;
}

/** Injectable collaborators used by unit tests and the production handler. */
export interface EnsureLogGroupHandlerDependencies {
    logs?: () => EnsureLogGroupLogs | Promise<EnsureLogGroupLogs>;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

interface CloudWatchLogsSdkClient {
    send(command: object): Promise<unknown>;
}

interface CloudWatchLogsSdkModule {
    CloudWatchLogsClient: new (
        configuration: Record<string, never>,
    ) => CloudWatchLogsSdkClient;
    CreateLogGroupCommand: new (input: {
        logGroupName: string;
    }) => object;
    PutRetentionPolicyCommand: new (input: {
        logGroupName: string;
        retentionInDays: number;
    }) => object;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
};

/** Validate the lazily loaded SDK package before constructing its client. */
function isCloudWatchLogsSdkModule(
    value: object,
): value is CloudWatchLogsSdkModule {
    return (
        "CloudWatchLogsClient" in value &&
        typeof value.CloudWatchLogsClient === "function" &&
        "CreateLogGroupCommand" in value &&
        typeof value.CreateLogGroupCommand === "function" &&
        "PutRetentionPolicyCommand" in value &&
        typeof value.PutRetentionPolicyCommand === "function"
    );
}

/** Load the service client lazily. */
async function createLogs(): Promise<EnsureLogGroupLogs> {
    const sdkModule: object = await import(CLOUDWATCH_LOGS_CLIENT_PACKAGE);
    if (!isCloudWatchLogsSdkModule(sdkModule)) {
        throw new Error(
            `${CLOUDWATCH_LOGS_CLIENT_PACKAGE} has an unexpected export shape`,
        );
    }

    const client = new sdkModule.CloudWatchLogsClient({});
    return {
        createLogGroup: async (logGroupName: string): Promise<void> => {
            await client.send(
                new sdkModule.CreateLogGroupCommand({ logGroupName }),
            );
        },
        putRetentionPolicy: async (
            logGroupName: string,
            retentionInDays: number,
        ): Promise<void> => {
            await client.send(
                new sdkModule.PutRetentionPolicyCommand({
                    logGroupName,
                    retentionInDays,
                }),
            );
        },
    };
}

/** Match the AWS SDK v3 service exception caught by the Python handler. */
function isResourceAlreadyExistsException(error: unknown): boolean {
    return (
        error instanceof Error &&
        error.name === RESOURCE_ALREADY_EXISTS_EXCEPTION
    );
}

/** Convert the configured string with the accepted Python int() behavior. */
function retentionDaysAsInteger(value: string): number {
    const normalizedValue = value.trim();
    const retentionInDays = Number(normalizedValue);
    if (
        !/^[+-]?[0-9]+$/.test(normalizedValue) ||
        !Number.isFinite(retentionInDays) ||
        !Number.isInteger(retentionInDays)
    ) {
        throw new Error(
            `invalid literal for int() with base 10: '${value}'`,
        );
    }
    return retentionInDays;
}

/** Create or adopt one log group, then apply retention when provided. */
export async function ensureLogGroup(
    logs: EnsureLogGroupLogs,
    logGroupName: string,
    retentionInDays: string | undefined,
    logger: CfnLogger = defaultLogger,
): Promise<EnsureLogGroupOutcome> {
    let outcome: EnsureLogGroupOutcome = "created";
    try {
        await logs.createLogGroup(logGroupName);
    } catch (error: unknown) {
        if (!isResourceAlreadyExistsException(error)) {
            throw error;
        }
        outcome = "adopted";
        logger.info(
            `log group already present, adopting: ${logGroupName}`,
        );
    }

    if (retentionInDays) {
        await logs.putRetentionPolicy(
            logGroupName,
            retentionDaysAsInteger(retentionInDays),
        );
    }
    return outcome;
}

/** Build the custom-resource handler with injectable AWS operations. */
export function createHandler(
    dependencies: EnsureLogGroupHandlerDependencies = {},
): (
    event: EnsureLogGroupEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: EnsureLogGroupEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        const logGroupName = event.ResourceProperties.LogGroupName;
        try {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);
            let data: Record<string, JsonValue> = {};

            if (event.RequestType === "Delete") {
                logger.info(
                    `leaving log group in place on delete: ${logGroupName}`,
                );
            } else {
                const logs = await (dependencies.logs ?? createLogs)();
                const outcome = await ensureLogGroup(
                    logs,
                    logGroupName,
                    event.ResourceProperties.RetentionInDays,
                    logger,
                );
                data = {
                    LogGroupName: logGroupName,
                    Outcome: outcome,
                };
            }

            await responseSender({
                context,
                event,
                status: "SUCCESS",
                data,
                physicalResourceId: logGroupName,
            });
        } catch (error: unknown) {
            logger.error(
                `Failed to ensure log group ${logGroupName}: ${errorMessage(error)}`,
                error,
            );
            await responseSender({
                context,
                event,
                status: "FAILED",
                data: { error: errorMessage(error) },
                physicalResourceId: logGroupName,
            });
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
