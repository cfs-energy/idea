import {
    DeleteRolePermissionsBoundaryCommand,
    GetRoleCommand,
    IAMClient,
    ListRolesCommand,
} from "@aws-sdk/client-iam";
import type {
    DeleteRolePermissionsBoundaryCommandInput,
    DeleteRolePermissionsBoundaryCommandOutput,
    GetRoleCommandInput,
    GetRoleCommandOutput,
    ListRolesCommandInput,
    ListRolesCommandOutput,
} from "@aws-sdk/client-iam";
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

export const PHYSICAL_RESOURCE_ID = "project-role-boundaries";

type ProjectRoleBoundariesProperties = Record<string, JsonValue> & {
    RolePath: string;
    BoundaryPolicyArn: string;
};

export type ProjectRoleBoundariesEvent =
    CfnResourceEvent<ProjectRoleBoundariesProperties>;

/** Injectable IAM operations used by the project-boundary sweep. */
export interface ProjectRoleBoundariesIam {
    listRoles(input: ListRolesCommandInput): Promise<ListRolesCommandOutput>;
    getRole(input: GetRoleCommandInput): Promise<GetRoleCommandOutput>;
    deleteRolePermissionsBoundary(
        input: DeleteRolePermissionsBoundaryCommandInput,
    ): Promise<DeleteRolePermissionsBoundaryCommandOutput>;
}

/** Injectable collaborators used by unit tests and the production handler. */
export interface ProjectRoleBoundariesHandlerDependencies {
    iam?: () => ProjectRoleBoundariesIam;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void => console.error(message, error),
};

/** Create the real IAM command adapter lazily, only for Delete requests. */
function createIam(): ProjectRoleBoundariesIam {
    const client = new IAMClient({});
    return {
        listRoles: (input: ListRolesCommandInput): Promise<ListRolesCommandOutput> =>
            client.send(new ListRolesCommand(input)),
        getRole: (input: GetRoleCommandInput): Promise<GetRoleCommandOutput> =>
            client.send(new GetRoleCommand(input)),
        deleteRolePermissionsBoundary: (
            input: DeleteRolePermissionsBoundaryCommandInput,
        ): Promise<DeleteRolePermissionsBoundaryCommandOutput> =>
            client.send(new DeleteRolePermissionsBoundaryCommand(input)),
    };
}

/**
 * Return whether IAM reported that a role disappeared during the serial sweep.
 *
 * Matched by error name rather than by class so a duplicated copy of the IAM
 * client in a bundle cannot break the swallow, matching boto3, which compares
 * the wire error code.
 */
function isNoSuchEntity(error: unknown): boolean {
    return error instanceof Error && error.name === "NoSuchEntityException";
}

/**
 * Drop the boundary reference from every project role still carrying it.
 *
 * ListRoles does not return PermissionsBoundary, so every listed role is read
 * with GetRole before a matching boundary is deleted.
 */
export async function clearBoundaries(
    iam: ProjectRoleBoundariesIam,
    rolePath: string,
    boundaryArn: string,
    logger: CfnLogger = defaultLogger,
): Promise<number> {
    let cleared = 0;
    let marker: string | undefined;
    // Paging ends on IsTruncated, like the boto3 list_roles paginator; a Marker
    // returned on a final page is not a request for another call.
    let truncated = false;

    do {
        const page = await iam.listRoles(
            marker === undefined
                ? { PathPrefix: rolePath }
                : { PathPrefix: rolePath, Marker: marker },
        );
        for (const listed of page.Roles ?? []) {
            const roleName = listed.RoleName;

            let role: GetRoleCommandOutput["Role"];
            try {
                role = (await iam.getRole({ RoleName: roleName })).Role;
            } catch (error: unknown) {
                if (!isNoSuchEntity(error)) {
                    throw error;
                }
                logger.info(
                    `role no longer present, nothing to clear: ${roleName}`,
                );
                continue;
            }

            if (
                role?.PermissionsBoundary?.PermissionsBoundaryArn !== boundaryArn
            ) {
                continue;
            }

            try {
                await iam.deleteRolePermissionsBoundary({ RoleName: roleName });
                cleared += 1;
                logger.info(`cleared project boundary from role: ${roleName}`);
            } catch (error: unknown) {
                if (!isNoSuchEntity(error)) {
                    throw error;
                }
                logger.info(
                    `role no longer present, nothing to clear: ${roleName}`,
                );
            }
        }
        marker = page.Marker;
        // Stop when the page is final or has no next token.
        truncated = page.IsTruncated === true && marker !== undefined;
    } while (truncated);

    return cleared;
}

/** Build the never-fail project-boundary custom-resource handler. */
export function createHandler(
    dependencies: ProjectRoleBoundariesHandlerDependencies = {},
): (
    event: ProjectRoleBoundariesEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;

    return wrapCfnHandler(
        async (
            event: ProjectRoleBoundariesEvent,
        ): Promise<{ data: Record<string, JsonValue> }> => {
            if (event.RequestType !== "Delete") {
                return { data: {} };
            }

            // Delete always reports SUCCESS.
            try {
                logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);
                const properties = event.ResourceProperties;
                const cleared = await clearBoundaries(
                    (dependencies.iam ?? createIam)(),
                    properties.RolePath,
                    properties.BoundaryPolicyArn,
                    logger,
                );
                logger.info(
                    `cleared project boundary from ${cleared} role(s)`,
                );
                return { data: { ClearedCount: String(cleared) } };
            } catch (error: unknown) {
                const message = errorMessage(error);
                logger.error(
                    `Failed to clear project role boundaries: ${message}`,
                    error,
                );
                return { data: { error: message } };
            }
        },
        {
            physicalResourceId: (): string => PHYSICAL_RESOURCE_ID,
            logger,
            responseSender: dependencies.responseSender,
            // Route the response helper's own log lines through the same logger.
            responseOptions: { logger },
        },
    );
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
