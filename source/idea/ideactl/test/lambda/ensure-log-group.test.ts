import assert from "node:assert/strict";
import test from "node:test";
import type {
    CfnLogger,
    CfnResponse,
    CfnResponseSender,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    EnsureLogGroupEvent,
    EnsureLogGroupLogs,
} from "../../src/lambda/idea_custom_resource_ensure_log_group/index.ts";
import {
    createHandler,
    handler,
} from "../../src/lambda/idea_custom_resource_ensure_log_group/index.ts";

const context = { logStreamName: "synthetic-log-stream" };
const logGroupName =
    "/sample-cluster/sample-module/bedrock-invocations";

/** Build a synthetic custom-resource event with Python-compatible inputs. */
function makeEvent(
    requestType: EnsureLogGroupEvent["RequestType"],
    retentionInDays?: string,
): EnsureLogGroupEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticBedrockInvocationLogGroup",
        PhysicalResourceId: logGroupName,
        ResourceProperties: {
            LogGroupName: logGroupName,
            ...(retentionInDays === undefined
                ? {}
                : { RetentionInDays: retentionInDays }),
        },
    };
}

/** Capture CloudFormation responses without making an HTTP request. */
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

/** Capture the facts logged by each branch. */
function makeLogger(): {
    infoMessages: string[];
    errorMessages: string[];
    logger: CfnLogger;
} {
    const infoMessages: string[] = [];
    const errorMessages: string[] = [];
    return {
        infoMessages,
        errorMessages,
        logger: {
            info: (message: string): void => {
                infoMessages.push(message);
            },
            error: (message: string): void => {
                errorMessages.push(message);
            },
        },
    };
}

test("exports index.handler as a function", async () => {
    const infos: string[] = [];
    const originalInfo = console.info;
    console.info = (message?: unknown): void => {
        if (typeof message === "string") {
            infos.push(message);
        }
    };
    const event = {
        ...makeEvent("Delete"),
        ResponseURL: "ftp://example.invalid/response",
    };
    try {
        await handler(event, context);
    } finally {
        console.info = originalInfo;
    }
    assert.equal(typeof handler, "function");
    assert.equal(infos[0], `ReceivedEvent: ${JSON.stringify(event)}`);
    assert.ok(
        infos.includes(`leaving log group in place on delete: ${logGroupName}`),
    );
});

test("Create creates the group, applies integer retention, and returns created", async () => {
    const calls: Array<
        | { operation: "create"; logGroupName: string }
        | {
              operation: "retention";
              logGroupName: string;
              retentionInDays: number;
          }
    > = [];
    const logs: EnsureLogGroupLogs = {
        createLogGroup: async (name: string): Promise<void> => {
            calls.push({ operation: "create", logGroupName: name });
        },
        putRetentionPolicy: async (
            name: string,
            retentionInDays: number,
        ): Promise<void> => {
            calls.push({
                operation: "retention",
                logGroupName: name,
                retentionInDays,
            });
        },
    };
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        logs: (): EnsureLogGroupLogs => logs,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create", "30"), context);

    assert.deepEqual(calls, [
        { operation: "create", logGroupName },
        {
            operation: "retention",
            logGroupName,
            retentionInDays: 30,
        },
    ]);
    assert.equal(recorder.responses.length, 1);
    assert.deepEqual(recorder.responses[0], {
        context,
        event: makeEvent("Create", "30"),
        status: "SUCCESS",
        data: { LogGroupName: logGroupName, Outcome: "created" },
        physicalResourceId: logGroupName,
    });
    assert.equal(
        capturedLogger.infoMessages[0],
        `ReceivedEvent: ${JSON.stringify(makeEvent("Create", "30"))}`,
    );
});

test("Update adopts an existing group and leaves retention unchanged when omitted", async () => {
    let retentionCalls = 0;
    const alreadyExists = new Error("already exists");
    alreadyExists.name = "ResourceAlreadyExistsException";
    const logs: EnsureLogGroupLogs = {
        createLogGroup: async (): Promise<void> => {
            throw alreadyExists;
        },
        putRetentionPolicy: async (): Promise<void> => {
            retentionCalls += 1;
        },
    };
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        logs: (): EnsureLogGroupLogs => logs,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(retentionCalls, 0);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.deepEqual(recorder.responses[0].data, {
        LogGroupName: logGroupName,
        Outcome: "adopted",
    });
    assert.ok(
        capturedLogger.infoMessages.includes(
            `log group already present, adopting: ${logGroupName}`,
        ),
    );
});

test("Delete leaves the group in place without constructing a Logs client", async () => {
    let logsFactoryCalls = 0;
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        logs: (): EnsureLogGroupLogs => {
            logsFactoryCalls += 1;
            throw new Error("Logs client must not be created for Delete");
        },
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Delete", "30"), context);

    assert.equal(logsFactoryCalls, 0);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.deepEqual(recorder.responses[0].data, {});
    assert.equal(
        recorder.responses[0].physicalResourceId,
        logGroupName,
    );
    assert.ok(
        capturedLogger.infoMessages.includes(
            `leaving log group in place on delete: ${logGroupName}`,
        ),
    );
});

test("an empty RetentionInDays string follows Python truthiness and skips retention", async () => {
    let retentionCalls = 0;
    const logs: EnsureLogGroupLogs = {
        createLogGroup: async (): Promise<void> => {},
        putRetentionPolicy: async (): Promise<void> => {
            retentionCalls += 1;
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        logs: (): EnsureLogGroupLogs => logs,
        logger: makeLogger().logger,
        responseSender: recorder.sender,
    })(makeEvent("Create", ""), context);

    assert.equal(retentionCalls, 0);
    assert.equal(recorder.responses[0].status, "SUCCESS");
});

test("a non-adoption create failure returns FAILED with Python error data", async () => {
    const logs: EnsureLogGroupLogs = {
        createLogGroup: async (): Promise<void> => {
            throw new Error("synthetic create failure");
        },
        putRetentionPolicy: async (): Promise<void> => {},
    };
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        logs: (): EnsureLogGroupLogs => logs,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create", "30"), context);

    assert.equal(recorder.responses.length, 1);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        logGroupName,
    );
    assert.deepEqual(recorder.responses[0].data, {
        error: "synthetic create failure",
    });
    assert.deepEqual(capturedLogger.errorMessages, [
        `Failed to ensure log group ${logGroupName}: synthetic create failure`,
    ]);
});

test("a retention-policy failure returns FAILED after group creation", async () => {
    let createCalls = 0;
    const logs: EnsureLogGroupLogs = {
        createLogGroup: async (): Promise<void> => {
            createCalls += 1;
        },
        putRetentionPolicy: async (): Promise<void> => {
            throw new Error("synthetic retention failure");
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        logs: (): EnsureLogGroupLogs => logs,
        logger: makeLogger().logger,
        responseSender: recorder.sender,
    })(makeEvent("Update", "90"), context);

    assert.equal(createCalls, 1);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error: "synthetic retention failure",
    });
});

test("an invalid retention string fails instead of sending a partial integer", async () => {
    let retentionCalls = 0;
    const logs: EnsureLogGroupLogs = {
        createLogGroup: async (): Promise<void> => {},
        putRetentionPolicy: async (): Promise<void> => {
            retentionCalls += 1;
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        logs: (): EnsureLogGroupLogs => logs,
        logger: makeLogger().logger,
        responseSender: recorder.sender,
    })(makeEvent("Create", "30 days"), context);

    assert.equal(retentionCalls, 0);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error: "invalid literal for int() with base 10: '30 days'",
    });
});
