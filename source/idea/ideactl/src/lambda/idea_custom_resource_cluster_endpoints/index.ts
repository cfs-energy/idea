import {
    CreateRuleCommand,
    DeleteRuleCommand,
    DescribeRulesCommand,
    DescribeTagsCommand,
    ElasticLoadBalancingV2Client,
    ElasticLoadBalancingV2ServiceException,
    ModifyListenerCommand,
    ModifyRuleCommand,
} from "@aws-sdk/client-elastic-load-balancing-v2";
import type {
    Action,
    CreateRuleCommandInput,
    CreateRuleCommandOutput,
    DeleteRuleCommandInput,
    DeleteRuleCommandOutput,
    DescribeRulesCommandInput,
    DescribeRulesCommandOutput,
    DescribeTagsCommandInput,
    DescribeTagsCommandOutput,
    ModifyListenerCommandInput,
    ModifyListenerCommandOutput,
    ModifyRuleCommandInput,
    ModifyRuleCommandOutput,
    RuleCondition,
} from "@aws-sdk/client-elastic-load-balancing-v2";
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

const ENDPOINT_NAME_TAG = "idea:EndpointName";
const NOT_PROVIDED = "__NOT_PROVIDED__";
const FIXED_RESPONSE_BODY = '{"success": true, "message": "OK"}';

type EndpointAction = Action & Record<string, JsonValue>;
type EndpointCondition = RuleCondition & Record<string, JsonValue>;

type ClusterEndpointProperties = Record<string, JsonValue> & {
    endpoint_name?: string | null;
    listener_arn?: string | null;
    default_action?: boolean;
    priority?: number | string | null;
    conditions?: EndpointCondition[] | null;
    actions?: EndpointAction[] | null;
    tags?: Record<string, string> | null;
};

export type ClusterEndpointsEvent =
    CfnResourceEvent<ClusterEndpointProperties>;

/** ELBv2 operations used by the endpoint custom resource. */
export interface ClusterEndpointsElbv2 {
    describeRules(
        input: DescribeRulesCommandInput,
    ): Promise<DescribeRulesCommandOutput>;
    describeTags(
        input: DescribeTagsCommandInput,
    ): Promise<DescribeTagsCommandOutput>;
    modifyListener(
        input: ModifyListenerCommandInput,
    ): Promise<ModifyListenerCommandOutput>;
    createRule(input: CreateRuleCommandInput): Promise<CreateRuleCommandOutput>;
    modifyRule(input: ModifyRuleCommandInput): Promise<ModifyRuleCommandOutput>;
    deleteRule(input: DeleteRuleCommandInput): Promise<DeleteRuleCommandOutput>;
}

/** Logging surface with Python logger.warning parity. */
export interface ClusterEndpointsLogger extends CfnLogger {
    warning(message: string): void;
}

/** Injectable collaborators used by unit tests and the production handler. */
export interface ClusterEndpointsHandlerDependencies {
    elbv2?: () => ClusterEndpointsElbv2;
    logger?: ClusterEndpointsLogger;
    responseSender?: CfnResponseSender;
    sleep?: (milliseconds: number) => Promise<void>;
}

const defaultLogger: ClusterEndpointsLogger = {
    info: (message: string): void => console.info(message),
    warning: (message: string): void => console.warn(message),
    error: (message: string, error?: unknown): void => console.error(message, error),
};

/** Sleep between serial DescribeTags calls to preserve Python request pacing. */
function sleep(milliseconds: number): Promise<void> {
    return new Promise<void>((resolve) => {
        setTimeout(resolve, milliseconds);
    });
}

/** Create the real ELBv2 command adapter lazily after input validation. */
function createElbv2(): ClusterEndpointsElbv2 {
    const client = new ElasticLoadBalancingV2Client({});
    return {
        describeRules: (
            input: DescribeRulesCommandInput,
        ): Promise<DescribeRulesCommandOutput> =>
            client.send(new DescribeRulesCommand(input)),
        describeTags: (
            input: DescribeTagsCommandInput,
        ): Promise<DescribeTagsCommandOutput> =>
            client.send(new DescribeTagsCommand(input)),
        modifyListener: (
            input: ModifyListenerCommandInput,
        ): Promise<ModifyListenerCommandOutput> =>
            client.send(new ModifyListenerCommand(input)),
        createRule: (
            input: CreateRuleCommandInput,
        ): Promise<CreateRuleCommandOutput> =>
            client.send(new CreateRuleCommand(input)),
        modifyRule: (
            input: ModifyRuleCommandInput,
        ): Promise<ModifyRuleCommandOutput> =>
            client.send(new ModifyRuleCommand(input)),
        deleteRule: (
            input: DeleteRuleCommandInput,
        ): Promise<DeleteRuleCommandOutput> =>
            client.send(new DeleteRuleCommand(input)),
    };
}

/** Render values that Python interpolates into log and error strings. */
function pythonString(value: string | null | undefined): string {
    return value === null || value === undefined ? "None" : value;
}

/**
 * Match Python int() for the number and decimal-string values emitted by CDK.
 * Unsupported values throw before an ELBv2 client is constructed.
 */
function pythonInt(value: number | string | null | undefined): number {
    if (typeof value === "number" && Number.isFinite(value)) {
        return Math.trunc(value);
    }
    if (typeof value === "string" && /^[+-]?\d+$/.test(value.trim())) {
        return Number.parseInt(value, 10);
    }
    throw new TypeError(
        value === null || value === undefined
            ? "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
            : `invalid literal for int() with base 10: '${String(value)}'`,
    );
}

/**
 * Find one endpoint rule by tag.
 * DescribeTags runs serially to avoid concurrent rule deletion during VDC teardown.
 */
export async function findRuleArn(
    elbv2: ClusterEndpointsElbv2,
    listenerArn: string,
    endpointName: string,
    options: {
        logger?: ClusterEndpointsLogger;
        sleep?: (milliseconds: number) => Promise<void>;
    } = {},
): Promise<string | undefined> {
    const logger = options.logger ?? defaultLogger;
    const sleepFunction = options.sleep ?? sleep;
    const describeRulesResult = await elbv2.describeRules({
        ListenerArn: listenerArn,
        PageSize: 100,
    });
    const ruleArns = (describeRulesResult.Rules ?? []).map(
        (rule) => rule.RuleArn,
    );

    for (const ruleArn of ruleArns) {
        try {
            const describeTagResults = await elbv2.describeTags({
                // Python passes every returned RuleArn through, including a missing value.
                ResourceArns: [ruleArn as string],
            });
            for (const tagDescription of describeTagResults.TagDescriptions ?? []) {
                for (const tag of tagDescription.Tags ?? []) {
                    if (
                        tag.Key === ENDPOINT_NAME_TAG &&
                        endpointName === tag.Value
                    ) {
                        return tagDescription.ResourceArn;
                    }
                }
            }
            await sleepFunction(1_000);
        } catch (error: unknown) {
            if (!(error instanceof ElasticLoadBalancingV2ServiceException)) {
                throw error;
            }
            logger.warning(
                `failed to fetch tags for rule arn: ${pythonString(ruleArn)} - ${String(error)}`,
            );
            await sleepFunction(2_000);
        }
    }

    return undefined;
}

/** Return the Python handler's endpoint-name default for response identity. */
function endpointName(event: ClusterEndpointsEvent): string | null {
    return event.ResourceProperties.endpoint_name === undefined
        ? NOT_PROVIDED
        : event.ResourceProperties.endpoint_name;
}

/** Build the cluster-endpoints custom-resource handler. */
export function createHandler(
    dependencies: ClusterEndpointsHandlerDependencies = {},
): (
    event: ClusterEndpointsEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;

    return wrapCfnHandler(
        async (event: ClusterEndpointsEvent): Promise<void> => {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);
            const resourceProperties = event.ResourceProperties;
            const name = endpointName(event);
            if (name === null || name === NOT_PROVIDED) {
                throw new Error("endpoint_name is required and cannot be empty");
            }

            const listenerArn = resourceProperties.listener_arn;
            if (listenerArn === null || listenerArn === undefined) {
                throw new Error("listener_arn is required and cannot be empty");
            }

            const defaultAction = resourceProperties.default_action ?? false;
            const conditions = resourceProperties.conditions ?? [];
            if (!defaultAction && conditions.length === 0) {
                throw new Error("conditions[] is required and cannot be empty");
            }

            const actions = resourceProperties.actions ?? [];
            if (!defaultAction && actions.length === 0) {
                throw new Error("actions[] is required and cannot be empty");
            }

            let priority = -1;
            if (!defaultAction) {
                priority = pythonInt(resourceProperties.priority);
                if (priority <= 0) {
                    throw new Error("priority must be greater than 0");
                }
            }

            const tags =
                resourceProperties.tags === undefined
                    ? {}
                    : resourceProperties.tags;
            if (tags === null) {
                throw new TypeError(
                    "'NoneType' object does not support item assignment",
                );
            }
            tags[ENDPOINT_NAME_TAG] = name;
            const resourceTags = Object.entries(tags).map(([Key, Value]) => ({
                Key,
                Value,
            }));
            const elbv2 = (dependencies.elbv2 ?? createElbv2)();

            if (defaultAction) {
                if (
                    event.RequestType === "Create" ||
                    event.RequestType === "Update"
                ) {
                    await elbv2.modifyListener({
                        ListenerArn: listenerArn,
                        DefaultActions: actions,
                    });
                    logger.info("default action modified");
                } else {
                    await elbv2.modifyListener({
                        ListenerArn: listenerArn,
                        DefaultActions: [
                            {
                                Type: "fixed-response",
                                FixedResponseConfig: {
                                    MessageBody: FIXED_RESPONSE_BODY,
                                    StatusCode: "200",
                                    ContentType: "application/json",
                                },
                            },
                        ],
                    });
                    logger.info("default action reset to fixed response");
                }
            } else if (event.RequestType === "Create") {
                // Adopt a rule that already carries this endpoint's name rather than making a
                // second one. Two resources can legitimately describe one endpoint: the container
                // stack owns the target group and creates the rule so its services have a load
                // balancer to name, and the module stack creates the same endpoint later in the
                // run. A plain create collides on the priority; adopting converges.
                //
                // On a cluster that already has the rule this is also what the cutover needs: the
                // rule is modified where it stands, so it keeps its own identity and its priority,
                // and the endpoint is re-routed rather than replaced.
                const adopted = await findRuleArn(elbv2, listenerArn, name, {
                    logger,
                    sleep: dependencies.sleep,
                });
                if (adopted !== undefined) {
                    await elbv2.modifyRule({
                        RuleArn: adopted,
                        Conditions: conditions,
                        Actions: actions,
                    });
                    logger.info(`rule adopted. rule arn: ${adopted}`);
                } else {
                    const result = await elbv2.createRule({
                        ListenerArn: listenerArn,
                        Conditions: conditions,
                        Priority: priority,
                        Actions: actions,
                        Tags: resourceTags,
                    });
                    const rules = result.Rules ?? [];
                    if (rules.length === 0) {
                        throw new Error("list index out of range");
                    }
                    const ruleArn = rules[0].RuleArn;
                    logger.info(
                        `rule created. rule arn: ${pythonString(ruleArn)}`,
                    );
                }
            } else if (event.RequestType === "Update") {
                const ruleArn = await findRuleArn(elbv2, listenerArn, name, {
                    logger,
                    sleep: dependencies.sleep,
                });
                if (ruleArn !== undefined) {
                    await elbv2.modifyRule({
                        RuleArn: ruleArn,
                        Conditions: conditions,
                        Actions: actions,
                    });
                    logger.info(`rule modified. rule arn: ${ruleArn}`);
                } else {
                    logger.warning(
                        "rule not found for target group. rule update skipped.",
                    );
                }
            } else if (event.RequestType === "Delete") {
                const ruleArn = await findRuleArn(elbv2, listenerArn, name, {
                    logger,
                    sleep: dependencies.sleep,
                });
                if (ruleArn !== undefined) {
                    await elbv2.deleteRule({ RuleArn: ruleArn });
                    logger.info(`rule deleted. rule arn: ${ruleArn}`);
                } else {
                    logger.warning(
                        "rule could not be deleted. rule arn not found for target group",
                    );
                }
            }
        },
        {
            physicalResourceId: (event: ClusterEndpointsEvent): string | undefined => {
                const name = endpointName(event);
                return name === null ? undefined : name;
            },
            errorMessage: (
                error: unknown,
                event: ClusterEndpointsEvent,
            ): string =>
                `failed to ${event.RequestType} endpoint: ${pythonString(endpointName(event))} - ${errorMessage(error)}`,
            failureReason: (
                error: unknown,
                event: ClusterEndpointsEvent,
            ): string =>
                `failed to ${event.RequestType} endpoint: ${pythonString(endpointName(event))} - ${errorMessage(error)}`,
            logger,
            responseSender: dependencies.responseSender,
        },
    );
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
