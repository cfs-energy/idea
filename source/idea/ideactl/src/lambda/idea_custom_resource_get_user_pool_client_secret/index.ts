import {
    CognitoIdentityProviderClient,
    DescribeUserPoolClientCommand,
} from "@aws-sdk/client-cognito-identity-provider";
import type {
    DescribeUserPoolClientCommandOutput,
} from "@aws-sdk/client-cognito-identity-provider";
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

export const PHYSICAL_RESOURCE_ID = "user-pool-client-secret";

type UserPoolClientSecretProperties = Record<string, JsonValue> & {
    UserPoolId?: string;
    ClientId?: string;
};

export type UserPoolClientSecretEvent =
    CfnResourceEvent<UserPoolClientSecretProperties>;

/** Values passed verbatim from CloudFormation to Cognito. */
export interface DescribeUserPoolClientInput {
    UserPoolId: JsonValue | undefined;
    ClientId: JsonValue | undefined;
}

/** Minimal Cognito operation used by this Lambda. */
export interface UserPoolClientSecretCognito {
    describeUserPoolClient(
        input: DescribeUserPoolClientInput,
    ): Promise<DescribeUserPoolClientCommandOutput>;
}

/** Injectable collaborators used by unit tests and the production handler. */
export interface UserPoolClientSecretHandlerDependencies {
    cognito?: () => UserPoolClientSecretCognito;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void => {
        if (error === undefined) {
            console.error(message);
            return;
        }
        console.error(message, error);
    },
};

/** Create the real Cognito command adapter for Create and Update requests. */
function createCognito(): UserPoolClientSecretCognito {
    const client = new CognitoIdentityProviderClient({});
    return {
        describeUserPoolClient: async (
            input: DescribeUserPoolClientInput,
        ): Promise<DescribeUserPoolClientCommandOutput> => {
            if (typeof input.UserPoolId !== "string") {
                throw new TypeError("UserPoolId must be a string");
            }
            if (typeof input.ClientId !== "string") {
                throw new TypeError("ClientId must be a string");
            }
            return client.send(
                new DescribeUserPoolClientCommand({
                    UserPoolId: input.UserPoolId,
                    ClientId: input.ClientId,
                }),
            );
        },
    };
}

/** Match Python f-string rendering for the optional client identifier. */
function pythonString(value: JsonValue | undefined): string {
    return value === undefined || value === null ? "None" : String(value);
}

/** Build the OAuth client-secret custom-resource handler. */
export function createHandler(
    dependencies: UserPoolClientSecretHandlerDependencies = {},
): (
    event: UserPoolClientSecretEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: UserPoolClientSecretEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        let clientId: JsonValue | undefined;

        try {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);

            // Delete returns before creating a client.
            if (event.RequestType === "Delete") {
                await responseSender({
                    context,
                    event,
                    status: "SUCCESS",
                    data: {},
                    physicalResourceId: PHYSICAL_RESOURCE_ID,
                });
                return;
            }

            // Default ResourceProperties only when it is absent.
            const resourceProperties =
                event.ResourceProperties === undefined
                    ? {}
                    : event.ResourceProperties;
            const userPoolId = resourceProperties.UserPoolId;
            clientId = resourceProperties.ClientId;
            logger.info(
                `UserPoolId: ${pythonString(userPoolId)}, ClientId: ${pythonString(clientId)}`,
            );

            const response = await (
                dependencies.cognito ?? createCognito
            )().describeUserPoolClient({
                UserPoolId: userPoolId,
                ClientId: clientId,
            });
            const clientSecret = response.UserPoolClient?.ClientSecret;
            const physicalResourceId =
                `${PHYSICAL_RESOURCE_ID}-${pythonString(clientId)}`;

            if (clientSecret === undefined) {
                const message =
                    `Could not find ClientSecret for ClientId: ${pythonString(clientId)}`;
                logger.error(message);
                await responseSender({
                    context,
                    event,
                    status: "FAILED",
                    data: { error: message },
                    physicalResourceId,
                });
                return;
            }

            // Omit NoEcho and redact the response log.
            await responseSender(
                {
                    context,
                    event,
                    status: "SUCCESS",
                    data: { ClientSecret: clientSecret },
                    physicalResourceId,
                },
                { logResponse: false },
            );
        } catch (error: unknown) {
            const message =
                `Failed to get ClientSecret for UserPool Client. - ${errorMessage(error)}`;
            logger.error(message, error);
            if (clientId === undefined || clientId === null) {
                clientId = "failed";
            }
            await responseSender({
                context,
                event,
                status: "FAILED",
                data: { error: message },
                physicalResourceId:
                    `${PHYSICAL_RESOURCE_ID}-${pythonString(clientId)}`,
            });
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
