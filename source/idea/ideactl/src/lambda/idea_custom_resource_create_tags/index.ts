import {
    CreateTagsCommand,
    EC2Client,
} from "@aws-sdk/client-ec2";
import type {
    CreateTagsCommandOutput,
} from "@aws-sdk/client-ec2";
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

type Ec2Tag = Record<string, JsonValue> & {
    Key: string;
    Value: string;
};

type CreateTagsProperties = Record<string, JsonValue> & {
    ResourceId: string;
    Tags: Ec2Tag[];
};

export type CreateTagsEvent = CfnResourceEvent<CreateTagsProperties>;

/** Minimal EC2 client surface used by the handler and its unit tests. */
export interface CreateTagsEc2 {
    send(command: CreateTagsCommand): Promise<CreateTagsCommandOutput>;
}

/** Injectable collaborators for unit tests; production uses the AWS SDK client. */
export interface CreateTagsHandlerDependencies {
    ec2?: () => CreateTagsEc2;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void => console.error(message, error),
};

/** Create a fresh EC2 client for each invocation, matching the Python handler. */
function createEc2(): CreateTagsEc2 {
    return new EC2Client({});
}

/**
 * Build the EC2 tagging handler with injectable collaborators.
 *
 * Create, Update, and Delete use the same tagging path.
 */
export function createHandler(
    dependencies: CreateTagsHandlerDependencies = {},
): (event: CreateTagsEvent, context: CfnResourceContext) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: CreateTagsEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        try {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);

            const resourceId = event.ResourceProperties.ResourceId;
            const tags = event.ResourceProperties.Tags;
            const ec2 = (dependencies.ec2 ?? createEc2)();

            // Keep one serial CreateTags call for every CloudFormation request type.
            await ec2.send(
                new CreateTagsCommand({
                    Resources: [resourceId],
                    Tags: tags,
                }),
            );

            await responseSender(
                {
                    context,
                    event,
                    status: "SUCCESS",
                    data: {},
                    physicalResourceId: resourceId,
                },
                // Route the response helper's own log lines through the same logger.
                { logger },
            );
        } catch (error: unknown) {
            const message = errorMessage(error);
            logger.error(`Failed to Tag EC2 Resource: ${message}`, error);

            // Python uses the exception text for both Data.error and physical id.
            await responseSender(
                {
                    context,
                    event,
                    status: "FAILED",
                    data: { error: message },
                    physicalResourceId: message,
                },
                { logger },
            );
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
