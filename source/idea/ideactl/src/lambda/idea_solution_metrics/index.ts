import type {
    CfnLogger,
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import { sendCfnResponse } from "../commons/cfn-response.ts";

/**
 * Anonymous telemetry is off. The function stays deployed with this body for one release because
 * job stacks created before the upgrade still name its ARN as the service token of their
 * SendAnonymousData resource, and a Delete against a service token that no longer exists leaves
 * the stack unable to tear down. It answers every request the way it always did, and sends
 * nothing anywhere.
 */

const PHYSICAL_RESOURCE_ID = "SolutionMetricsSO0072";

type SolutionMetricsProperties = Record<string, JsonValue>;

export type SolutionMetricsEvent =
    CfnResourceEvent<SolutionMetricsProperties>;

export interface SolutionMetricsDependencies {
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
};

/** Build the never-fail custom-resource handler with injectable collaborators. */
export function createHandler(
    dependencies: SolutionMetricsDependencies = {},
): (
    event: SolutionMetricsEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: SolutionMetricsEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        logger.info(
            `anonymous metrics are disabled; ${event.RequestType} acknowledged without sending anything`,
        );
        await responseSender(
            {
                context,
                event,
                status: "SUCCESS",
                data: {},
                physicalResourceId: PHYSICAL_RESOURCE_ID,
            },
            { logger },
        );
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
