import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
    DeleteCommand,
    DynamoDBDocumentClient,
    UpdateCommand,
} from "@aws-sdk/lib-dynamodb";
import type {
    CfnLogger,
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import {
    errorMessage,
    wrapCfnHandler,
} from "../commons/cfn-response.ts";

type ClusterSettings = Record<string, JsonValue>;

type ClusterSettingsProperties = Record<string, JsonValue> & {
    cluster_name: string;
    module_id: string;
    version: string;
    settings: ClusterSettings;
};

export type ClusterSettingsEvent = CfnResourceEvent<ClusterSettingsProperties>;

/** Minimal DynamoDB document-client surface used by this handler. */
export interface ClusterSettingsDynamoDb {
    send(command: UpdateCommand | DeleteCommand): Promise<unknown>;
}

/** Injectable collaborators used by unit tests and the production handler. */
export interface ClusterSettingsHandlerDependencies {
    dynamoDb?: () => ClusterSettingsDynamoDb;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void => console.error(message, error),
};

/** Create the real document client lazily, after Lambda invocation starts. */
function createDynamoDb(): ClusterSettingsDynamoDb {
    const client = DynamoDBDocumentClient.from(new DynamoDBClient({}));
    return {
        send: async (command: UpdateCommand | DeleteCommand): Promise<unknown> =>
            client.send(command),
    };
}

/**
 * Build the update-cluster-settings Lambda handler with injectable clients.
 * Values are passed directly to lib-dynamodb so their JSON types are preserved.
 */
export function createHandler(
    dependencies: ClusterSettingsHandlerDependencies = {},
): (event: ClusterSettingsEvent, context: CfnResourceContext) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;

    return wrapCfnHandler(
        async (event: ClusterSettingsEvent): Promise<void> => {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);

            const resourceProperties = event.ResourceProperties;
            const oldResourceProperties = event.OldResourceProperties;
            const clusterName = resourceProperties.cluster_name;
            const moduleId = resourceProperties.module_id;
            const version = resourceProperties.version;
            const stackName = `${clusterName}-${moduleId}`;
            const settings = resourceProperties.settings;
            const clusterSettingsTableName = `${clusterName}.cluster-settings`;
            const modulesTableName = `${clusterName}.modules`;
            const dynamoDb = (dependencies.dynamoDb ?? createDynamoDb)();

            if (event.RequestType === "Delete") {
                for (const key of Object.keys(settings)) {
                    const configKey = `${moduleId}.${key}`;
                    logger.info(`deleting config: ${configKey}`);
                    await dynamoDb.send(
                        new DeleteCommand({
                            TableName: clusterSettingsTableName,
                            Key: { key: configKey },
                        }),
                    );
                }

                await dynamoDb.send(
                    new UpdateCommand({
                        TableName: modulesTableName,
                        Key: { module_id: moduleId },
                        UpdateExpression:
                            "SET #status=:status, #stack_name=:stack_name, #version=:version",
                        ExpressionAttributeNames: {
                            "#status": "status",
                            "#stack_name": "stack_name",
                            "#version": "version",
                        },
                        ExpressionAttributeValues: {
                            ":status": "not-deployed",
                            ":stack_name": null,
                            ":version": null,
                        },
                    }),
                );
                return;
            }

            for (const [key, value] of Object.entries(settings)) {
                const configKey = `${moduleId}.${key}`;
                logger.info(`updating config: ${configKey} = ${String(value)}`);
                await dynamoDb.send(
                    new UpdateCommand({
                        TableName: clusterSettingsTableName,
                        Key: { key: configKey },
                        UpdateExpression:
                            "SET #value=:value, #source=:source ADD #version :version",
                        ExpressionAttributeNames: {
                            "#value": "value",
                            "#version": "version",
                            "#source": "source",
                        },
                        ExpressionAttributeValues: {
                            ":value": value,
                            ":source": "stack",
                            ":version": 1,
                        },
                    }),
                );
            }

            await dynamoDb.send(
                new UpdateCommand({
                    TableName: modulesTableName,
                    Key: { module_id: moduleId },
                    UpdateExpression:
                        "SET #status=:status, #stack_name=:stack_name, #version=:version",
                    ExpressionAttributeNames: {
                        "#status": "status",
                        "#stack_name": "stack_name",
                        "#version": "version",
                    },
                    ExpressionAttributeValues: {
                        ":status": "deployed",
                        ":stack_name": stackName,
                        ":version": version,
                    },
                }),
            );

            // Match Python's OldResourceProperties delta: only removed keys are deleted.
            if (oldResourceProperties !== undefined && oldResourceProperties !== null) {
                for (const oldKey of Object.keys(oldResourceProperties.settings ?? {})) {
                    if (!Object.hasOwn(settings, oldKey)) {
                        const configKey = `${moduleId}.${oldKey}`;
                        logger.info(`deleting config: ${configKey}`);
                        await dynamoDb.send(
                            new DeleteCommand({
                                TableName: clusterSettingsTableName,
                                Key: { key: configKey },
                            }),
                        );
                    }
                }
            }
        },
        {
            // Python reads these through event.get('ResourceProperties', {}) and str(None) == 'None'.
            physicalResourceId: (event: ClusterSettingsEvent): string =>
                `${pyStr(event.ResourceProperties?.cluster_name)}-${pyStr(event.ResourceProperties?.module_id)}-settings`,
            errorMessage: (error: unknown, event: ClusterSettingsEvent): string =>
                `failed to update cluster settings for module: ${pyStr(event.ResourceProperties?.module_id)} - ${errorMessage(error)}`,
            logger,
            responseSender: dependencies.responseSender,
        },
    );
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();

function pyStr(value: unknown): string {
    return value === undefined || value === null ? "None" : String(value);
}
