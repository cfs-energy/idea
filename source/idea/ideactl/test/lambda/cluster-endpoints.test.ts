import assert from "node:assert/strict";
import test from "node:test";
import { ElasticLoadBalancingV2ServiceException } from "@aws-sdk/client-elastic-load-balancing-v2";
import type {
    CfnResponse,
    CfnResponseSender,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    ClusterEndpointsElbv2,
    ClusterEndpointsEvent,
    ClusterEndpointsLogger,
} from "../../src/lambda/idea_custom_resource_cluster_endpoints/index.ts";
import {
    createHandler,
    findRuleArn,
    handler,
} from "../../src/lambda/idea_custom_resource_cluster_endpoints/index.ts";

const context = { logStreamName: "synthetic-log-stream" };

/** Build a complete synthetic custom-resource event. */
function makeEvent(
    requestType: ClusterEndpointsEvent["RequestType"],
    overrides: Partial<ClusterEndpointsEvent["ResourceProperties"]> = {},
): ClusterEndpointsEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticEndpoint",
        ResourceProperties: {
            endpoint_name: "sample-module-endpoint",
            listener_arn: "synthetic-listener",
            default_action: false,
            priority: 10,
            conditions: [
                {
                    Field: "path-pattern",
                    Values: ["/sample/*"],
                },
            ],
            actions: [
                {
                    Type: "forward",
                    TargetGroupArn: "synthetic-target-group",
                },
            ],
            tags: {
                "idea:cluster-name": "sample-cluster",
                "idea:module-id": "sample-module",
            },
            target_group_arn: "ignored-synthetic-target-group",
            ...overrides,
        },
    };
}

/** Supply no-op ELBv2 operations, replacing only behavior relevant to a test. */
function makeElbv2(
    overrides: Partial<ClusterEndpointsElbv2> = {},
): ClusterEndpointsElbv2 {
    return {
        describeRules: async () => ({ $metadata: {}, Rules: [] }),
        describeTags: async () => ({ $metadata: {}, TagDescriptions: [] }),
        modifyListener: async () => ({ $metadata: {} }),
        createRule: async () => ({
            $metadata: {},
            Rules: [{ RuleArn: "synthetic-created-rule" }],
        }),
        modifyRule: async () => ({ $metadata: {} }),
        deleteRule: async () => ({ $metadata: {} }),
        ...overrides,
    };
}

/** Capture CloudFormation responses without issuing an HTTP PUT. */
function makeResponseRecorder(): {
    responses: CfnResponse[];
    sender: CfnResponseSender;
} {
    const responses: CfnResponse[] = [];
    return {
        responses,
        sender: async (response: CfnResponse): Promise<void> => {
            responses.push(response);
        },
    };
}

/** Capture all facts logged by the handler. */
function makeLogger(): ClusterEndpointsLogger & {
    infos: string[];
    warnings: string[];
    errors: string[];
} {
    const infos: string[] = [];
    const warnings: string[] = [];
    const errors: string[] = [];
    return {
        infos,
        warnings,
        errors,
        info: (message: string): void => {
            infos.push(message);
        },
        warning: (message: string): void => {
            warnings.push(message);
        },
        error: (message: string): void => {
            errors.push(message);
        },
    };
}

/**
 * Build a DescribeTags result whose ResourceArn can differ from the RuleArn
 * used to look the rule up.
 */
function taggedRule(
    resourceArn: string,
    tags: Array<{ Key: string; Value: string }>,
): Awaited<ReturnType<ClusterEndpointsElbv2["describeTags"]>> {
    return {
        $metadata: {},
        TagDescriptions: [{ ResourceArn: resourceArn, Tags: tags }],
    };
}

test("exports index.handler for future nodejs22.x Lambda configuration", () => {
    assert.equal(typeof handler, "function");
});

test("Create creates a tagged non-default rule and returns an empty Data object", async () => {
    const createInputs: Array<
        Parameters<ClusterEndpointsElbv2["createRule"]>[0]
    > = [];
    const elbv2 = makeElbv2({
        createRule: async (input) => {
            createInputs.push(input);
            return {
                $metadata: {},
                Rules: [{ RuleArn: "synthetic-created-rule" }],
            };
        },
    });
    const recorder = makeResponseRecorder();
    const logger = makeLogger();
    const event = makeEvent("Create");

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        logger,
        responseSender: recorder.sender,
    })(event, context);

    assert.deepEqual(createInputs, [
        {
            ListenerArn: "synthetic-listener",
            Conditions: [
                {
                    Field: "path-pattern",
                    Values: ["/sample/*"],
                },
            ],
            Priority: 10,
            Actions: [
                {
                    Type: "forward",
                    TargetGroupArn: "synthetic-target-group",
                },
            ],
            Tags: [
                { Key: "idea:cluster-name", Value: "sample-cluster" },
                { Key: "idea:module-id", Value: "sample-module" },
                {
                    Key: "idea:EndpointName",
                    Value: "sample-module-endpoint",
                },
            ],
        },
    ]);
    assert.equal(
        event.ResourceProperties.tags?.["idea:EndpointName"],
        "sample-module-endpoint",
    );
    assert.match(
        logger.infos.at(-1) ?? "",
        /rule created\. rule arn: synthetic-created-rule/,
    );
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        "sample-module-endpoint",
    );
    assert.deepEqual(recorder.responses[0].data, {});
});

test("Update scans tags serially, sleeps after misses, and modifies the matching rule", async () => {
    const describeTagInputs: Array<
        Parameters<ClusterEndpointsElbv2["describeTags"]>[0]
    > = [];
    const modifyInputs: Array<
        Parameters<ClusterEndpointsElbv2["modifyRule"]>[0]
    > = [];
    const sleeps: number[] = [];
    const elbv2 = makeElbv2({
        describeRules: async () => ({
            $metadata: {},
            Rules: [
                { RuleArn: "synthetic-rule-wrong-value" },
                { RuleArn: "synthetic-rule-wrong-key" },
                { RuleArn: "synthetic-rule-match" },
            ],
        }),
        describeTags: async (input) => {
            describeTagInputs.push(input);
            const ruleArn = input.ResourceArns?.[0];
            if (ruleArn === "synthetic-rule-wrong-value") {
                return taggedRule(ruleArn, [
                    {
                        Key: "idea:EndpointName",
                        Value: "other-module-endpoint",
                    },
                ]);
            }
            if (ruleArn === "synthetic-rule-wrong-key") {
                return taggedRule(ruleArn, [
                    {
                        Key: "idea:cluster-name",
                        Value: "sample-module-endpoint",
                    },
                ]);
            }
            if (ruleArn === "synthetic-rule-match") {
                return taggedRule("synthetic-resource-match", [
                    {
                        Key: "idea:EndpointName",
                        Value: "sample-module-endpoint",
                    },
                ]);
            }
            return { $metadata: {}, TagDescriptions: [] };
        },
        modifyRule: async (input) => {
            modifyInputs.push(input);
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        responseSender: recorder.sender,
        sleep: async (milliseconds: number): Promise<void> => {
            sleeps.push(milliseconds);
        },
    })(makeEvent("Update"), context);

    assert.deepEqual(describeTagInputs, [
        { ResourceArns: ["synthetic-rule-wrong-value"] },
        { ResourceArns: ["synthetic-rule-wrong-key"] },
        { ResourceArns: ["synthetic-rule-match"] },
    ]);
    assert.deepEqual(sleeps, [1_000, 1_000]);
    assert.deepEqual(modifyInputs, [
        {
            RuleArn: "synthetic-resource-match",
            Conditions: [
                {
                    Field: "path-pattern",
                    Values: ["/sample/*"],
                },
            ],
            Actions: [
                {
                    Type: "forward",
                    TargetGroupArn: "synthetic-target-group",
                },
            ],
        },
    ]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("Delete finds the tagged rule and deletes it", async () => {
    const deleteInputs: Array<
        Parameters<ClusterEndpointsElbv2["deleteRule"]>[0]
    > = [];
    const elbv2 = makeElbv2({
        describeRules: async (input) => {
            assert.deepEqual(input, {
                ListenerArn: "synthetic-listener",
                PageSize: 100,
            });
            return {
                $metadata: {},
                Rules: [
                    { RuleArn: "synthetic-rule-wrong-value" },
                    { RuleArn: "synthetic-rule-wrong-key" },
                    { RuleArn: "synthetic-rule-match" },
                ],
            };
        },
        describeTags: async (input) => {
            const ruleArn = input.ResourceArns?.[0];
            if (ruleArn === "synthetic-rule-wrong-value") {
                return taggedRule(ruleArn, [
                    {
                        Key: "idea:EndpointName",
                        Value: "other-module-endpoint",
                    },
                ]);
            }
            if (ruleArn === "synthetic-rule-wrong-key") {
                return taggedRule(ruleArn, [
                    {
                        Key: "idea:cluster-name",
                        Value: "sample-module-endpoint",
                    },
                ]);
            }
            if (ruleArn === "synthetic-rule-match") {
                return taggedRule("synthetic-resource-match", [
                    {
                        Key: "idea:EndpointName",
                        Value: "sample-module-endpoint",
                    },
                ]);
            }
            return { $metadata: {}, TagDescriptions: [] };
        },
        deleteRule: async (input) => {
            deleteInputs.push(input);
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        responseSender: recorder.sender,
        sleep: async (): Promise<void> => {},
    })(makeEvent("Delete"), context);

    assert.deepEqual(deleteInputs, [{ RuleArn: "synthetic-resource-match" }]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("default-action Create modifies the listener without validating conditions or priority", async () => {
    const modifyInputs: Array<
        Parameters<ClusterEndpointsElbv2["modifyListener"]>[0]
    > = [];
    const elbv2 = makeElbv2({
        modifyListener: async (input) => {
            modifyInputs.push(input);
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        responseSender: recorder.sender,
    })(
        makeEvent("Create", {
            endpoint_name: "broker-client-endpoint",
            default_action: true,
            priority: 0,
            conditions: [],
        }),
        context,
    );

    assert.deepEqual(modifyInputs, [
        {
            ListenerArn: "synthetic-listener",
            DefaultActions: [
                {
                    Type: "forward",
                    TargetGroupArn: "synthetic-target-group",
                },
            ],
        },
    ]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        "broker-client-endpoint",
    );
});

test("default-action Update uses the same listener modification branch", async () => {
    const modifyInputs: Array<
        Parameters<ClusterEndpointsElbv2["modifyListener"]>[0]
    > = [];
    const elbv2 = makeElbv2({
        modifyListener: async (input) => {
            modifyInputs.push(input);
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        responseSender: recorder.sender,
    })(makeEvent("Update", { default_action: true }), context);

    assert.deepEqual(modifyInputs, [
        {
            ListenerArn: "synthetic-listener",
            DefaultActions: [
                {
                    Type: "forward",
                    TargetGroupArn: "synthetic-target-group",
                },
            ],
        },
    ]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("default-action Delete resets the listener to Python's fixed JSON response", async () => {
    const modifyInputs: Array<
        Parameters<ClusterEndpointsElbv2["modifyListener"]>[0]
    > = [];
    const elbv2 = makeElbv2({
        modifyListener: async (input) => {
            modifyInputs.push(input);
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        responseSender: recorder.sender,
    })(makeEvent("Delete", { default_action: true }), context);

    assert.deepEqual(modifyInputs, [
        {
            ListenerArn: "synthetic-listener",
            DefaultActions: [
                {
                    Type: "fixed-response",
                    FixedResponseConfig: {
                        MessageBody: '{"success": true, "message": "OK"}',
                        StatusCode: "200",
                        ContentType: "application/json",
                    },
                },
            ],
        },
    ]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("Update warns and succeeds when no tagged rule exists", async () => {
    let modifyCalls = 0;
    const sleeps: number[] = [];
    const logger = makeLogger();
    const elbv2 = makeElbv2({
        describeRules: async () => ({
            $metadata: {},
            Rules: [
                { RuleArn: "synthetic-rule-wrong-value" },
                { RuleArn: "synthetic-rule-wrong-key" },
            ],
        }),
        describeTags: async (input) => {
            const ruleArn = input.ResourceArns?.[0] ?? "";
            return ruleArn === "synthetic-rule-wrong-value"
                ? taggedRule(ruleArn, [
                      {
                          Key: "idea:EndpointName",
                          Value: "other-module-endpoint",
                      },
                  ])
                : taggedRule(ruleArn, [
                      {
                          Key: "idea:cluster-name",
                          Value: "sample-module-endpoint",
                      },
                  ]);
        },
        modifyRule: async () => {
            modifyCalls += 1;
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        logger,
        responseSender: recorder.sender,
        sleep: async (milliseconds: number): Promise<void> => {
            sleeps.push(milliseconds);
        },
    })(makeEvent("Update"), context);

    assert.equal(modifyCalls, 0);
    assert.deepEqual(sleeps, [1_000, 1_000]);
    assert.deepEqual(logger.warnings, [
        "rule not found for target group. rule update skipped.",
    ]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("Delete warns and succeeds when no tagged rule exists", async () => {
    let deleteCalls = 0;
    const logger = makeLogger();
    const elbv2 = makeElbv2({
        describeRules: async () => ({
            $metadata: {},
            Rules: [
                { RuleArn: "synthetic-rule-wrong-value" },
                { RuleArn: "synthetic-rule-wrong-key" },
            ],
        }),
        describeTags: async (input) => {
            const ruleArn = input.ResourceArns?.[0] ?? "";
            return ruleArn === "synthetic-rule-wrong-value"
                ? taggedRule(ruleArn, [
                      {
                          Key: "idea:EndpointName",
                          Value: "other-module-endpoint",
                      },
                  ])
                : taggedRule(ruleArn, [
                      {
                          Key: "idea:cluster-name",
                          Value: "sample-module-endpoint",
                      },
                  ]);
        },
        deleteRule: async () => {
            deleteCalls += 1;
            return { $metadata: {} };
        },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        logger,
        responseSender: recorder.sender,
        sleep: async (): Promise<void> => {},
    })(makeEvent("Delete"), context);

    assert.equal(deleteCalls, 0);
    assert.deepEqual(logger.warnings, [
        "rule could not be deleted. rule arn not found for target group",
    ]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("findRuleArn sleeps two seconds after a service ClientError and continues serially", async () => {
    const calls: string[] = [];
    const sleeps: number[] = [];
    const logger = makeLogger();
    const clientError = new ElasticLoadBalancingV2ServiceException({
        name: "SyntheticClientError",
        $fault: "client",
        $metadata: {},
        message: "synthetic tag race",
    });
    const elbv2 = makeElbv2({
        describeRules: async () => ({
            $metadata: {},
            Rules: [
                { RuleArn: "synthetic-deleted-rule" },
                { RuleArn: "synthetic-rule-wrong-value" },
                { RuleArn: "synthetic-rule-wrong-key" },
                { RuleArn: "synthetic-live-rule" },
            ],
        }),
        describeTags: async (input) => {
            const ruleArn = input.ResourceArns?.[0] ?? "";
            calls.push(ruleArn);
            if (ruleArn === "synthetic-deleted-rule") {
                throw clientError;
            }
            if (ruleArn === "synthetic-rule-wrong-value") {
                return taggedRule(ruleArn, [
                    {
                        Key: "idea:EndpointName",
                        Value: "other-module-endpoint",
                    },
                ]);
            }
            if (ruleArn === "synthetic-rule-wrong-key") {
                return taggedRule(ruleArn, [
                    {
                        Key: "idea:cluster-name",
                        Value: "sample-module-endpoint",
                    },
                ]);
            }
            return taggedRule("synthetic-resource-live", [
                {
                    Key: "idea:EndpointName",
                    Value: "sample-module-endpoint",
                },
            ]);
        },
    });

    const result = await findRuleArn(
        elbv2,
        "synthetic-listener",
        "sample-module-endpoint",
        {
            logger,
            sleep: async (milliseconds: number): Promise<void> => {
                sleeps.push(milliseconds);
            },
        },
    );

    assert.equal(result, "synthetic-resource-live");
    assert.deepEqual(calls, [
        "synthetic-deleted-rule",
        "synthetic-rule-wrong-value",
        "synthetic-rule-wrong-key",
        "synthetic-live-rule",
    ]);
    assert.deepEqual(sleeps, [2_000, 1_000, 1_000]);
    assert.match(
        logger.warnings[0],
        /^failed to fetch tags for rule arn: synthetic-deleted-rule - SyntheticClientError: synthetic tag race$/,
    );
});

test("findRuleArn rethrows non-service DescribeTags errors", async () => {
    const elbv2 = makeElbv2({
        describeRules: async () => ({
            $metadata: {},
            Rules: [{ RuleArn: "synthetic-rule" }],
        }),
        describeTags: async () => {
            throw new TypeError("synthetic parameter failure");
        },
    });

    await assert.rejects(
        findRuleArn(
            elbv2,
            "synthetic-listener",
            "sample-module-endpoint",
            { sleep: async (): Promise<void> => {} },
        ),
        /synthetic parameter failure/,
    );
});

test("missing endpoint_name reports FAILED with the sentinel physical id", async () => {
    const recorder = makeResponseRecorder();
    let factoryCalls = 0;
    const event = makeEvent("Create");
    delete event.ResourceProperties.endpoint_name;

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => {
            factoryCalls += 1;
            return makeElbv2();
        },
        responseSender: recorder.sender,
    })(event, context);

    assert.equal(factoryCalls, 0);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        "__NOT_PROVIDED__",
    );
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: __NOT_PROVIDED__ - endpoint_name is required and cannot be empty",
    );
});

test("null endpoint_name reports FAILED and lets the response helper use its fallback id", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({ responseSender: recorder.sender })(
        makeEvent("Delete", { endpoint_name: null }),
        context,
    );

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(recorder.responses[0].physicalResourceId, undefined);
    assert.equal(
        recorder.responses[0].reason,
        "failed to Delete endpoint: None - endpoint_name is required and cannot be empty",
    );
});

test("missing listener_arn reports the Python validation failure", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({ responseSender: recorder.sender })(
        makeEvent("Create", { listener_arn: null }),
        context,
    );

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: sample-module-endpoint - listener_arn is required and cannot be empty",
    );
});

test("empty conditions report FAILED before constructing an ELBv2 client", async () => {
    const recorder = makeResponseRecorder();
    let factoryCalls = 0;

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => {
            factoryCalls += 1;
            return makeElbv2();
        },
        responseSender: recorder.sender,
    })(makeEvent("Create", { conditions: [] }), context);

    assert.equal(factoryCalls, 0);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: sample-module-endpoint - conditions[] is required and cannot be empty",
    );
});

test("empty actions report FAILED before constructing an ELBv2 client", async () => {
    const recorder = makeResponseRecorder();
    let factoryCalls = 0;

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => {
            factoryCalls += 1;
            return makeElbv2();
        },
        responseSender: recorder.sender,
    })(makeEvent("Create", { actions: [] }), context);

    assert.equal(factoryCalls, 0);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: sample-module-endpoint - actions[] is required and cannot be empty",
    );
});

test("non-positive priority reports FAILED", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({ responseSender: recorder.sender })(
        makeEvent("Create", { priority: 0 }),
        context,
    );

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: sample-module-endpoint - priority must be greater than 0",
    );
});

test("missing priority reproduces Python int(None) failure", async () => {
    const recorder = makeResponseRecorder();
    const event = makeEvent("Create");
    delete event.ResourceProperties.priority;

    await createHandler({ responseSender: recorder.sender })(event, context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: sample-module-endpoint - int() argument must be a string, a bytes-like object or a real number, not 'NoneType'",
    );
});

test("null tags reproduce Python's item-assignment failure", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({ responseSender: recorder.sender })(
        makeEvent("Create", { tags: null }),
        context,
    );

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: sample-module-endpoint - 'NoneType' object does not support item assignment",
    );
});

test("Create reports FAILED when ELBv2 returns no Rules", async () => {
    const recorder = makeResponseRecorder();
    const elbv2 = makeElbv2({
        createRule: async () => ({ $metadata: {}, Rules: [] }),
    });

    await createHandler({
        elbv2: (): ClusterEndpointsElbv2 => elbv2,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].reason,
        "failed to Create endpoint: sample-module-endpoint - list index out of range",
    );
});

test("an ELBv2 failure reports FAILED and the handler never throws", async () => {
    const recorder = makeResponseRecorder();
    const elbv2 = makeElbv2({
        describeRules: async () => {
            throw new Error("synthetic ELBv2 failure");
        },
    });

    await assert.doesNotReject(
        createHandler({
            elbv2: (): ClusterEndpointsElbv2 => elbv2,
            responseSender: recorder.sender,
            sleep: async (): Promise<void> => {},
        })(makeEvent("Update"), context),
    );

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        "sample-module-endpoint",
    );
    assert.equal(
        recorder.responses[0].reason,
        "failed to Update endpoint: sample-module-endpoint - synthetic ELBv2 failure",
    );
    assert.deepEqual(recorder.responses[0].data, {});
});
