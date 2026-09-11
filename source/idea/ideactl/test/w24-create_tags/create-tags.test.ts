import assert from "node:assert/strict";
import test from "node:test";
import { CreateTagsCommand } from "@aws-sdk/client-ec2";
import type {
    CfnLogger,
    CfnResponse,
    CfnResponseSender,
    SendCfnResponseOptions,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    CreateTagsEc2,
    CreateTagsEvent,
} from "../../src/lambda/idea_custom_resource_create_tags/index.ts";
import {
    createHandler,
    handler,
} from "../../src/lambda/idea_custom_resource_create_tags/index.ts";

const context = { logStreamName: "synthetic-log-stream" };

/** Build the exact ResourceId and Tags shape emitted by CreateTagsCustomResource. */
function makeEvent(requestType: CreateTagsEvent["RequestType"]): CreateTagsEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticEndpointTags",
        ResourceProperties: {
            ResourceId: "vpce-synthetic",
            Tags: [
                { Key: "Name", Value: "sample-service-vpc-endpoint" },
                { Key: "idea:ClusterName", Value: "sample-cluster" },
            ],
        },
    };
}

/** Record CloudFormation responses without making an HTTP request. */
function makeResponseRecorder(): {
    responses: CfnResponse[];
    options: Array<SendCfnResponseOptions | undefined>;
    sender: CfnResponseSender;
} {
    const responses: CfnResponse[] = [];
    const options: Array<SendCfnResponseOptions | undefined> = [];
    return {
        responses,
        options,
        sender: async (
            response: CfnResponse,
            senderOptions?: SendCfnResponseOptions,
        ): Promise<void> => {
            responses.push(response);
            options.push(senderOptions);
        },
    };
}

/** Record EC2 commands and return a successful synthetic SDK response. */
function makeEc2Recorder(): {
    commands: CreateTagsCommand[];
    ec2: CreateTagsEc2;
} {
    const commands: CreateTagsCommand[] = [];
    return {
        commands,
        ec2: {
            send: async (
                command: CreateTagsCommand,
            ): Promise<Awaited<ReturnType<CreateTagsEc2["send"]>>> => {
                commands.push(command);
                return { $metadata: {} };
            },
        },
    };
}

/** Assert the Python handler's common successful response and EC2 request. */
async function assertSuccessfulRequest(
    requestType: CreateTagsEvent["RequestType"],
): Promise<void> {
    const ec2Recorder = makeEc2Recorder();
    const responseRecorder = makeResponseRecorder();

    await createHandler({
        ec2: (): CreateTagsEc2 => ec2Recorder.ec2,
        responseSender: responseRecorder.sender,
    })(makeEvent(requestType), context);

    assert.equal(ec2Recorder.commands.length, 1);
    assert.ok(ec2Recorder.commands[0] instanceof CreateTagsCommand);
    assert.deepEqual(ec2Recorder.commands[0].input, {
        Resources: ["vpce-synthetic"],
        Tags: [
            { Key: "Name", Value: "sample-service-vpc-endpoint" },
            { Key: "idea:ClusterName", Value: "sample-cluster" },
        ],
    });
    assert.equal(responseRecorder.responses.length, 1);
    assert.equal(responseRecorder.responses[0].status, "SUCCESS");
    assert.equal(
        responseRecorder.responses[0].physicalResourceId,
        "vpce-synthetic",
    );
    assert.deepEqual(responseRecorder.responses[0].data, {});
}

test("Create tags the resource and uses ResourceId as the physical id", async () => {
    await assertSuccessfulRequest("Create");
});

test("Update repeats the same CreateTags call", async () => {
    await assertSuccessfulRequest("Update");
});

test("Delete also repeats CreateTags instead of removing tags", async () => {
    await assertSuccessfulRequest("Delete");
});

test("reports FAILED with Python's error data and physical id when EC2 fails", async () => {
    const logEntries: string[] = [];
    const logger: CfnLogger = {
        info: (message: string): void => {
            logEntries.push(message);
        },
        error: (message: string): void => {
            logEntries.push(message);
        },
    };
    const ec2: CreateTagsEc2 = {
        send: async (): Promise<Awaited<ReturnType<CreateTagsEc2["send"]>>> => {
            throw new Error("synthetic EC2 failure");
        },
    };
    const responseRecorder = makeResponseRecorder();

    await assert.doesNotReject(
        createHandler({
            ec2: (): CreateTagsEc2 => ec2,
            logger,
            responseSender: responseRecorder.sender,
        })(makeEvent("Create"), context),
    );

    assert.equal(responseRecorder.responses.length, 1);
    assert.equal(responseRecorder.responses[0].status, "FAILED");
    assert.equal(
        responseRecorder.responses[0].physicalResourceId,
        "synthetic EC2 failure",
    );
    assert.deepEqual(responseRecorder.responses[0].data, {
        error: "synthetic EC2 failure",
    });
    assert.match(logEntries[0] ?? "", /^ReceivedEvent: /);
    assert.equal(
        logEntries[1],
        "Failed to Tag EC2 Resource: synthetic EC2 failure",
    );
});

test("exports the Lambda entry point as handler", () => {
    assert.equal(typeof handler, "function");
});

test("the response helper receives the injected logger on both outcomes", async () => {
    const logger: CfnLogger = {
        info: (): void => {},
        error: (): void => {},
    };

    const successRecorder = makeResponseRecorder();
    await createHandler({
        ec2: (): CreateTagsEc2 => makeEc2Recorder().ec2,
        logger,
        responseSender: successRecorder.sender,
    })(makeEvent("Create"), context);
    assert.equal(successRecorder.options[0]?.logger, logger);

    const failureRecorder = makeResponseRecorder();
    await createHandler({
        ec2: (): CreateTagsEc2 => ({
            send: async (): Promise<never> => {
                throw new Error("synthetic CreateTags failure");
            },
        }),
        logger,
        responseSender: failureRecorder.sender,
    })(makeEvent("Create"), context);
    assert.equal(failureRecorder.responses[0].status, "FAILED");
    assert.equal(failureRecorder.options[0]?.logger, logger);
});
