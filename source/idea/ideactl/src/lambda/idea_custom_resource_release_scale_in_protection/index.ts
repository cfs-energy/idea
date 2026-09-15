import type {
    CfnLogger,
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import { errorMessage, sendCfnResponse } from "../commons/cfn-response.ts";

type ReleaseScaleInProtectionProperties = Record<string, JsonValue> & {
    AutoScalingGroupName: string;
};

export type ReleaseScaleInProtectionEvent =
    CfnResourceEvent<ReleaseScaleInProtectionProperties>;

/** The one group description the release needs: which members still carry the flag. */
export interface AutoScalingGroupMembers {
    protectedInstanceIds: string[];
}

/** Minimal Auto Scaling operations used by this Lambda. */
export interface ReleaseScaleInProtectionAutoScaling {
    /** Undefined when the group does not exist, which is not an error here. */
    describeGroup(name: string): Promise<AutoScalingGroupMembers | undefined>;
    clearGroupDefault(name: string): Promise<void>;
    clearInstanceProtection(name: string, instanceIds: string[]): Promise<void>;
}

export interface ReleaseScaleInProtectionHandlerDependencies {
    autoScaling?: () =>
        | ReleaseScaleInProtectionAutoScaling
        | Promise<ReleaseScaleInProtectionAutoScaling>;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
};

/** Load the service client lazily, with a literal specifier so the bundler inlines it. */
async function createAutoScaling(): Promise<ReleaseScaleInProtectionAutoScaling> {
    const sdk = await import("@aws-sdk/client-auto-scaling");
    const client = new sdk.AutoScalingClient({});
    return {
        describeGroup: async (name) => {
            const result = await client.send(
                new sdk.DescribeAutoScalingGroupsCommand({ AutoScalingGroupNames: [name] }),
            );
            const group = result.AutoScalingGroups?.[0];
            if (group === undefined) return undefined;
            return {
                protectedInstanceIds: (group.Instances ?? [])
                    .filter((instance) => instance.ProtectedFromScaleIn === true)
                    .map((instance) => instance.InstanceId)
                    .filter((id): id is string => typeof id === "string"),
            };
        },
        clearGroupDefault: async (name) => {
            await client.send(
                new sdk.UpdateAutoScalingGroupCommand({
                    AutoScalingGroupName: name,
                    NewInstancesProtectedFromScaleIn: false,
                }),
            );
        },
        clearInstanceProtection: async (name, instanceIds) => {
            await client.send(
                new sdk.SetInstanceProtectionCommand({
                    AutoScalingGroupName: name,
                    InstanceIds: instanceIds,
                    ProtectedFromScaleIn: false,
                }),
            );
        },
    };
}

/**
 * Clears the group default and every member that still carries the flag. A group that has gone,
 * or that never launched, is left alone.
 */
export async function releaseScaleInProtection(
    autoScaling: ReleaseScaleInProtectionAutoScaling,
    name: string,
    logger: CfnLogger = defaultLogger,
): Promise<void> {
    const group = await autoScaling.describeGroup(name);
    if (group === undefined) {
        logger.info(`no group named ${name}; nothing to release`);
        return;
    }
    await autoScaling.clearGroupDefault(name);
    if (group.protectedInstanceIds.length > 0) {
        await autoScaling.clearInstanceProtection(name, group.protectedInstanceIds);
    }
    logger.info(
        `released scale-in protection on ${name} and ${group.protectedInstanceIds.length} instance(s)`,
    );
}

/** Build the custom-resource handler with injectable AWS operations. */
export function createHandler(
    dependencies: ReleaseScaleInProtectionHandlerDependencies = {},
): (event: ReleaseScaleInProtectionEvent, context: CfnResourceContext) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (event, context): Promise<void> => {
        const name = event.ResourceProperties.AutoScalingGroupName;
        const physicalResourceId = `scale-in-protection-${name}`;
        try {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);
            // Only a delete releases anything: the flag is what protects a running task from a
            // scale-in, and it has to go only when the group itself is going.
            if (event.RequestType === "Delete") {
                const autoScaling = await (dependencies.autoScaling ?? createAutoScaling)();
                await releaseScaleInProtection(autoScaling, name, logger);
            }
            await responseSender({
                context,
                event,
                status: "SUCCESS",
                data: {},
                physicalResourceId,
            });
        } catch (error: unknown) {
            logger.error(
                `Failed to release scale-in protection on ${name}: ${errorMessage(error)}`,
                error,
            );
            await responseSender({
                context,
                event,
                status: "FAILED",
                data: { error: errorMessage(error) },
                physicalResourceId,
            });
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
