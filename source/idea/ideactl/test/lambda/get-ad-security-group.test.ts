import assert from "node:assert/strict";
import test from "node:test";
import type {
    CfnLogger,
    CfnResponse,
    CfnResponseSender,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    DescribeDirectoriesResult,
    GetAdSecurityGroupDirectoryService,
    GetAdSecurityGroupEvent,
} from "../../src/lambda/idea_custom_resource_get_ad_security_group/index.ts";
import {
    createHandler,
    handler,
} from "../../src/lambda/idea_custom_resource_get_ad_security_group/index.ts";

const context = { logStreamName: "synthetic-log-stream" };
const directoryId = "d-synthetic";
const securityGroupId = "sg-synthetic";
/** Stable physical id the Python handler returns on every path. */
const AD_SECURITY_GROUP_PHYSICAL_ID = "ad-controller-security-group-id";

/** Build a synthetic custom-resource event with the Python handler's inputs. */
function makeEvent(
    requestType: GetAdSecurityGroupEvent["RequestType"],
    properties: GetAdSecurityGroupEvent["ResourceProperties"] = {
        DirectoryId: directoryId,
    },
): GetAdSecurityGroupEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticActiveDirectory",
        PhysicalResourceId: AD_SECURITY_GROUP_PHYSICAL_ID,
        ResourceProperties: properties,
    };
}

/** Capture CloudFormation responses without issuing an HTTP request. */
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

/** Capture the facts logged by every handler branch. */
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

/** Build a Directory Service stub returning one configured result. */
function makeDirectoryService(
    result: DescribeDirectoriesResult,
    calls: string[][] = [],
): GetAdSecurityGroupDirectoryService {
    return {
        describeDirectories: async (
            directoryIds: string[],
        ): Promise<DescribeDirectoriesResult> => {
            calls.push(directoryIds);
            return result;
        },
    };
}

test("exports index.handler as a function", () => {
    assert.equal(typeof handler, "function");
});

test("Create describes the directory and returns SecurityGroupId", async () => {
    const calls: string[][] = [];
    const directoryService = makeDirectoryService(
        {
            DirectoryDescriptions: [
                {
                    VpcSettings: {
                        SecurityGroupId: securityGroupId,
                    },
                },
            ],
        },
        calls,
    );
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();
    const event = makeEvent("Create");

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService =>
            directoryService,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(event, context);

    assert.deepEqual(calls, [[directoryId]]);
    assert.deepEqual(recorder.responses, [
        {
            context,
            event,
            status: "SUCCESS",
            data: { SecurityGroupId: securityGroupId },
            physicalResourceId: AD_SECURITY_GROUP_PHYSICAL_ID,
        },
    ]);
    assert.match(capturedLogger.infoMessages[0], /^ReceivedEvent: /);
    assert.ok(
        capturedLogger.infoMessages.includes(
            `AD DirectoryId: ${directoryId}`,
        ),
    );
});

test("Update repeats the lookup and returns the stable physical id", async () => {
    const calls: string[][] = [];
    const recorder = makeResponseRecorder();

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService =>
            makeDirectoryService(
                {
                    DirectoryDescriptions: [
                        {
                            VpcSettings: {
                                SecurityGroupId: securityGroupId,
                            },
                        },
                    ],
                },
                calls,
            ),
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.deepEqual(calls, [[directoryId]]);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        AD_SECURITY_GROUP_PHYSICAL_ID,
    );
    assert.deepEqual(recorder.responses[0].data, {
        SecurityGroupId: securityGroupId,
    });
});

test("Delete succeeds immediately without constructing a Directory Service client", async () => {
    let directoryServiceFactoryCalls = 0;
    const recorder = makeResponseRecorder();

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService => {
            directoryServiceFactoryCalls += 1;
            return makeDirectoryService({});
        },
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.equal(directoryServiceFactoryCalls, 0);
    assert.equal(recorder.responses.length, 1);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        AD_SECURITY_GROUP_PHYSICAL_ID,
    );
    assert.deepEqual(recorder.responses[0].data, {});
});

test("an absent DirectoryDescriptions result reports FAILED", async () => {
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();
    const expectedMessage =
        `Could not find SecurityGroupId for DirectoryId: ${directoryId}`;

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService =>
            makeDirectoryService({}),
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error: expectedMessage,
    });
    assert.deepEqual(capturedLogger.errorMessages, [
        expectedMessage,
    ]);
});

test("an empty DirectoryDescriptions list reports FAILED", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService =>
            makeDirectoryService({ DirectoryDescriptions: [] }),
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error:
            `Could not find SecurityGroupId for DirectoryId: ${directoryId}`,
    });
});

test("a directory without VpcSettings or SecurityGroupId reports FAILED", async () => {
    for (const result of [
        { DirectoryDescriptions: [{}] },
        { DirectoryDescriptions: [{ VpcSettings: {} }] },
    ]) {
        const recorder = makeResponseRecorder();

        await createHandler({
            directoryService: (): GetAdSecurityGroupDirectoryService =>
                makeDirectoryService(result),
            responseSender: recorder.sender,
        })(makeEvent("Create"), context);

        assert.equal(recorder.responses[0].status, "FAILED");
        assert.deepEqual(recorder.responses[0].data, {
            error:
                `Could not find SecurityGroupId for DirectoryId: ${directoryId}`,
        });
    }
});

test("an empty SecurityGroupId string succeeds because Python checks only for None", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService =>
            makeDirectoryService({
                DirectoryDescriptions: [
                    { VpcSettings: { SecurityGroupId: "" } },
                ],
            }),
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.deepEqual(recorder.responses[0].data, {
        SecurityGroupId: "",
    });
});

test("a DescribeDirectories exception reports the Python-prefixed error", async () => {
    const failure = new Error("synthetic describe failure");
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();
    const expectedMessage =
        `Failed to get SecurityGroupId for Directory: ${failure.message}`;

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService => ({
            describeDirectories: async (): Promise<DescribeDirectoriesResult> => {
                throw failure;
            },
        }),
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        AD_SECURITY_GROUP_PHYSICAL_ID,
    );
    assert.deepEqual(recorder.responses[0].data, {
        error: expectedMessage,
    });
    assert.deepEqual(capturedLogger.errorMessages, [
        expectedMessage,
    ]);
});

test("a missing DirectoryId follows Python concatenation failure before the AWS call", async () => {
    let directoryServiceFactoryCalls = 0;
    const recorder = makeResponseRecorder();

    await createHandler({
        directoryService: (): GetAdSecurityGroupDirectoryService => {
            directoryServiceFactoryCalls += 1;
            return makeDirectoryService({});
        },
        responseSender: recorder.sender,
    })(makeEvent("Create", {}), context);

    assert.equal(directoryServiceFactoryCalls, 0);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error:
            "Failed to get SecurityGroupId for Directory: can only concatenate str (not \"NoneType\") to str",
    });
});
