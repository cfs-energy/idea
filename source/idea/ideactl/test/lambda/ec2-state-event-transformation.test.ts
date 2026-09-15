import assert from "node:assert/strict";
import test from "node:test";
import type {
    CfnLogger,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    Ec2StateEc2,
    Ec2StateEvent,
    Ec2StateSns,
    PublishInput,
} from "../../src/lambda/idea_ec2_state_event_transformation_lambda/index.ts";
import {
    createHandler,
    EC2_STATE_CHANGE_DETAIL_TYPE,
    EVENT_NAMESPACE,
    handler,
} from "../../src/lambda/idea_ec2_state_event_transformation_lambda/index.ts";

const environment = {
    IDEA_CLUSTER_NAME_TAG_KEY: "idea:ClusterName",
    IDEA_CLUSTER_NAME_TAG_VALUE: "synthetic-cluster",
    IDEA_EC2_STATE_SNS_TOPIC_ARN: "synthetic-topic",
    IDEA_TAG_PREFIX: "idea:",
};

function makeEvent(
    detailType = "EC2 Instance State-change Notification",
): Ec2StateEvent {
    return {
        "detail-type": detailType,
        detail: {
            "instance-id": "i-synthetic",
            state: "running",
            note: "café",
        },
    };
}

function makeLogger(): {
    errors: string[];
    infos: string[];
    logger: CfnLogger;
} {
    const errors: string[] = [];
    const infos: string[] = [];
    return {
        errors,
        infos,
        logger: {
            info: (message: string): void => {
                infos.push(message);
            },
            error: (message: string): void => {
                errors.push(message);
            },
        },
    };
}

function ec2WithTags(
    tags: Array<{ Key?: string; Value?: string }>,
): Ec2StateEc2 {
    return {
        describeInstances: async () => ({
            $metadata: {},
            Reservations: [{ Instances: [{ Tags: tags }] }],
        }),
    };
}

test("exports index.handler for the future nodejs22.x Lambda configuration", async () => {
    const errors: unknown[] = [];
    const originalError = console.error;
    console.error = (message?: unknown): void => {
        errors.push(message);
    };
    try {
        await handler(
            {
                "detail-type": "Synthetic Unrelated Type",
                detail: {
                    "instance-id": "i-synthetic",
                    state: "running",
                },
            },
            undefined,
        );
    } finally {
        console.error = originalError;
    }
    assert.equal(typeof handler, "function");
    assert.deepEqual(errors, [
        "ERROR. Invalid detail type Synthetic Unrelated Type",
    ]);
});

test("publishes the Python message renditions, attributes, mutation, and logs", async () => {
    const event = makeEvent();
    const logger = makeLogger();
    const publishInputs: PublishInput[] = [];
    const publishResponse = { MessageId: "synthetic-message" };
    const sns: Ec2StateSns = {
        publish: async (input) => {
            publishInputs.push(input);
            return publishResponse;
        },
    };

    await createHandler({
        ec2: ec2WithTags([
            { Key: "Name", Value: "ignored" },
            { Key: "idea:ClusterName", Value: "synthetic-cluster" },
            { Key: "idea:Project/Name++", Value: "alpha" },
            { Key: "idea:Node.Type-1", Value: "node" },
        ]),
        sns,
        env: environment,
        logger: logger.logger,
    })(event, undefined);

    assert.equal(
        EC2_STATE_CHANGE_DETAIL_TYPE,
        "EC2 Instance State-change Notification",
    );
    assert.deepEqual(Object.keys(publishInputs[0].MessageAttributes), [
        "idea_ClusterName",
        "idea_Project_Name_",
        "idea_Node.Type-1",
    ]);
    assert.deepEqual(event.detail.tags, {
        "idea:ClusterName": "synthetic-cluster",
        "idea:Project/Name++": "alpha",
        "idea:Node.Type-1": "node",
    });
    assert.deepEqual(publishInputs, [
        {
            TopicArn: "synthetic-topic",
            MessageStructure: "json",
            MessageAttributes: {
                idea_ClusterName: {
                    DataType: "String",
                    StringValue: "synthetic-cluster",
                },
                idea_Project_Name_: {
                    DataType: "String",
                    StringValue: "alpha",
                },
                "idea_Node.Type-1": {
                    DataType: "String",
                    StringValue: "node",
                },
            },
            Message:
                "{\"default\": \"{\\\"header\\\": {\\\"namespace\\\": \\\"Ec2.StateChangeEvent\\\", \\\"request_id\\\": \\\"i-synthetic\\\"}, \\\"payload\\\": {\\\"instance-id\\\": \\\"i-synthetic\\\", \\\"state\\\": \\\"running\\\", \\\"note\\\": \\\"caf\\\\u00e9\\\", \\\"tags\\\": {\\\"idea:ClusterName\\\": \\\"synthetic-cluster\\\", \\\"idea:Project/Name++\\\": \\\"alpha\\\", \\\"idea:Node.Type-1\\\": \\\"node\\\"}}}\", \"sqs\": {\"header\": {\"namespace\": \"Ec2.StateChangeEvent\", \"request_id\": \"i-synthetic\"}, \"payload\": {\"instance-id\": \"i-synthetic\", \"state\": \"running\", \"note\": \"caf\\u00e9\", \"tags\": {\"idea:ClusterName\": \"synthetic-cluster\", \"idea:Project/Name++\": \"alpha\", \"idea:Node.Type-1\": \"node\"}}}}",
        },
    ]);
    assert.deepEqual(logger.infos, [
        "forwarding ec2-state-event for i-synthetic for state running",
        "{ MessageId: 'synthetic-message' }",
    ]);
    assert.deepEqual(logger.errors, []);
    assert.equal(EVENT_NAMESPACE, "Ec2.StateChangeEvent");
});

test("rejects an unrelated detail type without constructing AWS requests", async () => {
    const logger = makeLogger();
    let ec2Calls = 0;
    const ec2: Ec2StateEc2 = {
        describeInstances: async () => {
            ec2Calls += 1;
            return { $metadata: {} };
        },
    };

    await createHandler({
        ec2,
        env: environment,
        logger: logger.logger,
    })(makeEvent("Synthetic Detail"), undefined);

    assert.equal(ec2Calls, 0);
    assert.deepEqual(logger.errors, [
        "ERROR. Invalid detail type Synthetic Detail",
    ]);
});

test("does not publish when the cluster-name tag does not match", async () => {
    const logger = makeLogger();
    let publishCalls = 0;
    const sns: Ec2StateSns = {
        publish: async () => {
            publishCalls += 1;
            return {};
        },
    };

    await createHandler({
        ec2: ec2WithTags([
            { Key: "idea:ClusterName", Value: "another-cluster" },
        ]),
        sns,
        env: environment,
        logger: logger.logger,
    })(makeEvent(), undefined);

    assert.equal(publishCalls, 0);
    assert.deepEqual(logger.infos, [
        "tag_key(s): idea:ClusterName and tag_value(s): synthetic-cluster on instance-id: i-synthetic not found. NO=OP.",
    ]);
});

test("logs and swallows an EC2 failure", async () => {
    const event = makeEvent();
    const logger = makeLogger();
    const ec2: Ec2StateEc2 = {
        describeInstances: async () => {
            throw new Error("synthetic EC2 failure");
        },
    };

    await assert.doesNotReject(
        createHandler({
            ec2,
            env: environment,
            logger: logger.logger,
        })(event, undefined),
    );

    assert.deepEqual(event.detail.tags, {});
    assert.equal(logger.errors.length, 1);
    assert.match(
        logger.errors[0],
        /^Error in Handling ec2 state change event: .*error: synthetic EC2 failure$/,
    );
});

test("logs and swallows an SNS publish failure", async () => {
    const logger = makeLogger();
    const sns: Ec2StateSns = {
        publish: async () => {
            throw new Error("synthetic SNS failure");
        },
    };

    await assert.doesNotReject(
        createHandler({
            ec2: ec2WithTags([
                { Key: "idea:ClusterName", Value: "synthetic-cluster" },
            ]),
            sns,
            env: environment,
            logger: logger.logger,
        })(makeEvent(), undefined),
    );

    assert.equal(logger.errors.length, 1);
    assert.match(
        logger.errors[0],
        /^Error in Handling ec2 state change event: .*error: synthetic SNS failure$/,
    );
});

test("loads tags then logs and swallows a missing IDEA tag prefix", async () => {
    const logger = makeLogger();
    let ec2Calls = 0;
    const ec2: Ec2StateEc2 = {
        describeInstances: async () => {
            ec2Calls += 1;
            return {
                $metadata: {},
                Reservations: [{
                    Instances: [{
                        Tags: [{ Key: "idea:ClusterName", Value: "synthetic-cluster" }],
                    }],
                }],
            };
        },
    };

    await createHandler({
        ec2,
        env: {
            IDEA_CLUSTER_NAME_TAG_KEY: "idea:ClusterName",
            IDEA_CLUSTER_NAME_TAG_VALUE: "synthetic-cluster",
        },
        logger: logger.logger,
    })(makeEvent(), undefined);

    assert.equal(ec2Calls, 1);
    assert.equal(logger.errors.length, 1);
    assert.match(logger.errors[0], /startswith first arg/);
});

test("logs and swallows an EC2 response with no instance tags", async () => {
    const logger = makeLogger();
    const ec2: Ec2StateEc2 = {
        describeInstances: async () => ({
            $metadata: {},
            Reservations: [{ Instances: [{}] }],
        }),
    };

    await createHandler({
        ec2,
        env: environment,
        logger: logger.logger,
    })(makeEvent(), undefined);

    assert.equal(logger.errors.length, 1);
    assert.match(logger.errors[0], /'NoneType' object is not iterable$/);
});
