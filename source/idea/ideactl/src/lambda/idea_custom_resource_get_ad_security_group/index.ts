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

const DIRECTORY_SERVICE_CLIENT_PACKAGE =
    "@aws-sdk/client-directory-service";

export const PHYSICAL_RESOURCE_ID =
    "ad-controller-security-group-id";

type GetAdSecurityGroupProperties = Record<string, JsonValue> & {
    DirectoryId?: JsonValue;
};

export type GetAdSecurityGroupEvent =
    CfnResourceEvent<GetAdSecurityGroupProperties>;

/** Directory Service response fields read by the Python handler. */
export interface DirectoryDescription {
    VpcSettings?: {
        SecurityGroupId?: string;
    };
}

/** Minimal result of Directory Service DescribeDirectories. */
export interface DescribeDirectoriesResult {
    DirectoryDescriptions?: DirectoryDescription[];
}

/** Injectable Directory Service operation used by unit tests. */
export interface GetAdSecurityGroupDirectoryService {
    describeDirectories(
        directoryIds: string[],
    ): Promise<DescribeDirectoriesResult>;
}

/** Injectable collaborators for the production and test handlers. */
export interface GetAdSecurityGroupHandlerDependencies {
    directoryService?: () =>
        | GetAdSecurityGroupDirectoryService
        | Promise<GetAdSecurityGroupDirectoryService>;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

interface DirectoryServiceSdkClient {
    send(command: object): Promise<DescribeDirectoriesResult>;
}

interface DirectoryServiceSdkModule {
    DirectoryServiceClient: new (
        configuration: Record<string, never>,
    ) => DirectoryServiceSdkClient;
    DescribeDirectoriesCommand: new (input: {
        DirectoryIds: string[];
    }) => object;
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

/** Validate the lazily loaded AWS SDK package before using its exports. */
function isDirectoryServiceSdkModule(
    value: object,
): value is DirectoryServiceSdkModule {
    return (
        "DirectoryServiceClient" in value &&
        typeof value.DirectoryServiceClient === "function" &&
        "DescribeDirectoriesCommand" in value &&
        typeof value.DescribeDirectoriesCommand === "function"
    );
}

/** Load the service client lazily. */
async function createDirectoryService(): Promise<GetAdSecurityGroupDirectoryService> {
    // A literal specifier, so the bundler inlines the pinned client instead of leaving the
    // resolution to whatever the managed runtime ships.
    const sdkModule: object = await import("@aws-sdk/client-directory-service");
    if (!isDirectoryServiceSdkModule(sdkModule)) {
        throw new Error(
            `${DIRECTORY_SERVICE_CLIENT_PACKAGE} has an unexpected export shape`,
        );
    }

    const client = new sdkModule.DirectoryServiceClient({});
    return {
        describeDirectories: (
            directoryIds: string[],
        ): Promise<DescribeDirectoriesResult> =>
            client.send(
                new sdkModule.DescribeDirectoriesCommand({
                    DirectoryIds: directoryIds,
                }),
            ),
    };
}

/**
 * Match the Python string-concatenation failure when DirectoryId is missing or
 * is not the CloudFormation string expected by the handler.
 */
function requireDirectoryId(value: JsonValue | undefined): string {
    if (typeof value === "string") {
        return value;
    }

    const pythonType =
        value === null || value === undefined
            ? "NoneType"
            : Array.isArray(value)
              ? "list"
              : typeof value === "number"
                ? Number.isInteger(value)
                    ? "int"
                    : "float"
                : typeof value === "boolean"
                  ? "bool"
                  : "dict";
    throw new TypeError(
        `can only concatenate str (not "${pythonType}") to str`,
    );
}

/** Build the custom-resource handler with injectable Directory Service calls. */
export function createHandler(
    dependencies: GetAdSecurityGroupHandlerDependencies = {},
): (
    event: GetAdSecurityGroupEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender =
        dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: GetAdSecurityGroupEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        try {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);

            // Delete is an immediate no-op and must not construct an AWS client.
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

            const directoryId = requireDirectoryId(
                event.ResourceProperties?.DirectoryId,
            );
            logger.info(`AD DirectoryId: ${directoryId}`);

            const directoryService = await (
                dependencies.directoryService ??
                createDirectoryService
            )();
            const response =
                await directoryService.describeDirectories([
                    directoryId,
                ]);
            const directories =
                response.DirectoryDescriptions ?? [];
            const securityGroupId =
                directories.length > 0
                    ? directories[0].VpcSettings?.SecurityGroupId
                    : undefined;

            if (securityGroupId === undefined) {
                const message =
                    `Could not find SecurityGroupId for DirectoryId: ${directoryId}`;
                logger.error(message);
                await responseSender({
                    context,
                    event,
                    status: "FAILED",
                    data: { error: message },
                    physicalResourceId: PHYSICAL_RESOURCE_ID,
                });
                return;
            }

            await responseSender({
                context,
                event,
                status: "SUCCESS",
                data: { SecurityGroupId: securityGroupId },
                physicalResourceId: PHYSICAL_RESOURCE_ID,
            });
        } catch (error: unknown) {
            const message =
                `Failed to get SecurityGroupId for Directory: ${errorMessage(error)}`;
            logger.error(message, error);
            await responseSender({
                context,
                event,
                status: "FAILED",
                data: { error: message },
                physicalResourceId: PHYSICAL_RESOURCE_ID,
            });
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
