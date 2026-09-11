import assert from "node:assert/strict";
import test from "node:test";
import type {
    DescribeUserPoolClientCommandOutput,
} from "@aws-sdk/client-cognito-identity-provider";
import type {
    CfnLogger,
    CfnResponse,
    CfnResponseSender,
    SendCfnResponseOptions,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    DescribeUserPoolClientInput,
    UserPoolClientSecretCognito,
    UserPoolClientSecretEvent,
} from "../../src/lambda/idea_custom_resource_get_user_pool_client_secret/index.ts";
import {
    createHandler,
    handler,
} from "../../src/lambda/idea_custom_resource_get_user_pool_client_secret/index.ts";

const context = { logStreamName: "synthetic-log-stream" };
const userPoolId = "synthetic-user-pool";
const clientId = "synthetic-client";
/** Prefix the Python handler uses for every non-Delete physical id. */
const USER_POOL_CLIENT_SECRET_PHYSICAL_ID = "user-pool-client-secret";
const quietLogger: CfnLogger = {
    info: (): void => {},
    error: (): void => {},
};

/** Build a synthetic event with the properties used by the Python handler. */
function makeEvent(
    requestType: UserPoolClientSecretEvent["RequestType"],
): UserPoolClientSecretEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticOAuthCredentials",
        ResourceProperties: {
            UserPoolId: userPoolId,
            ClientId: clientId,
        },
    };
}

interface RecordedResponse {
    response: CfnResponse;
    options: SendCfnResponseOptions | undefined;
}

/** Capture CloudFormation responses without sending a network request. */
function makeResponseRecorder(): {
    responses: RecordedResponse[];
    sender: CfnResponseSender;
} {
    const responses: RecordedResponse[] = [];
    return {
        responses,
        sender: async (
            response: CfnResponse,
            options?: SendCfnResponseOptions,
        ): Promise<void> => {
            responses.push({ response, options });
        },
    };
}

/** Capture the facts logged by each handler branch. */
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

/** Return a Cognito stub and expose every DescribeUserPoolClient input. */
function makeCognito(
    result: DescribeUserPoolClientCommandOutput,
): {
    calls: DescribeUserPoolClientInput[];
    cognito: UserPoolClientSecretCognito;
} {
    const calls: DescribeUserPoolClientInput[] = [];
    return {
        calls,
        cognito: {
            describeUserPoolClient: async (
                input: DescribeUserPoolClientInput,
            ): Promise<DescribeUserPoolClientCommandOutput> => {
                calls.push(input);
                return result;
            },
        },
    };
}

test("exports index.handler as a function", () => {
    assert.equal(typeof handler, "function");
});

test("Create returns ClientSecret under a client-specific physical id without NoEcho", async () => {
    const cognito = makeCognito({
        $metadata: {},
        UserPoolClient: { ClientSecret: "synthetic-secret" },
    });
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();
    const event = makeEvent("Create");

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => cognito.cognito,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(event, context);

    assert.deepEqual(cognito.calls, [{ UserPoolId: userPoolId, ClientId: clientId }]);
    assert.deepEqual(recorder.responses, [
        {
            response: {
                context,
                event,
                status: "SUCCESS",
                data: { ClientSecret: "synthetic-secret" },
                physicalResourceId: `${USER_POOL_CLIENT_SECRET_PHYSICAL_ID}-${clientId}`,
            },
            options: { logResponse: false },
        },
    ]);
    assert.equal("noEcho" in recorder.responses[0].response, false);
    assert.deepEqual(capturedLogger.infoMessages, [
        `ReceivedEvent: ${JSON.stringify(event)}`,
        `UserPoolId: ${userPoolId}, ClientId: ${clientId}`,
    ]);
});

test("Update describes the current client and returns its secret", async () => {
    const cognito = makeCognito({
        $metadata: {},
        UserPoolClient: { ClientSecret: "updated-synthetic-secret" },
    });
    const recorder = makeResponseRecorder();
    const event = makeEvent("Update");

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => cognito.cognito,
        logger: quietLogger,
        responseSender: recorder.sender,
    })(event, context);

    assert.deepEqual(cognito.calls, [{ UserPoolId: userPoolId, ClientId: clientId }]);
    assert.equal(recorder.responses[0].response.status, "SUCCESS");
    assert.deepEqual(recorder.responses[0].response.data, {
        ClientSecret: "updated-synthetic-secret",
    });
    assert.deepEqual(recorder.responses[0].options, { logResponse: false });
});

test("Delete succeeds immediately with the constant physical id and no Cognito call", async () => {
    let cognitoFactoryCalls = 0;
    const recorder = makeResponseRecorder();
    const event = makeEvent("Delete");

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => {
            cognitoFactoryCalls += 1;
            throw new Error("Cognito must not be created for Delete");
        },
        logger: quietLogger,
        responseSender: recorder.sender,
    })(event, context);

    assert.equal(cognitoFactoryCalls, 0);
    assert.deepEqual(recorder.responses, [
        {
            response: {
                context,
                event,
                status: "SUCCESS",
                data: {},
                physicalResourceId: USER_POOL_CLIENT_SECRET_PHYSICAL_ID,
            },
            options: undefined,
        },
    ]);
});

test("a client without ClientSecret reports FAILED with Python error data", async () => {
    const cognito = makeCognito({
        $metadata: {},
        UserPoolClient: {},
    });
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => cognito.cognito,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    const message =
        `Could not find ClientSecret for ClientId: ${clientId}`;
    assert.equal(recorder.responses[0].response.status, "FAILED");
    assert.deepEqual(recorder.responses[0].response.data, { error: message });
    assert.equal(
        recorder.responses[0].response.physicalResourceId,
        `${USER_POOL_CLIENT_SECRET_PHYSICAL_ID}-${clientId}`,
    );
    assert.equal(recorder.responses[0].options, undefined);
    assert.deepEqual(capturedLogger.errorMessages, [message]);
});

test("an absent UserPoolClient reports the same missing-secret failure", async () => {
    const cognito = makeCognito({ $metadata: {} });
    const recorder = makeResponseRecorder();

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => cognito.cognito,
        logger: quietLogger,
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(recorder.responses[0].response.status, "FAILED");
    assert.deepEqual(recorder.responses[0].response.data, {
        error: `Could not find ClientSecret for ClientId: ${clientId}`,
    });
});

test("an empty ClientSecret succeeds because Python rejects only None", async () => {
    const cognito = makeCognito({
        $metadata: {},
        UserPoolClient: { ClientSecret: "" },
    });
    const recorder = makeResponseRecorder();

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => cognito.cognito,
        logger: quietLogger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].response.status, "SUCCESS");
    assert.deepEqual(recorder.responses[0].response.data, { ClientSecret: "" });
    assert.deepEqual(recorder.responses[0].options, { logResponse: false });
});

test("a Cognito failure is caught and returned with the exact Python prefix", async () => {
    const failure = new Error("synthetic describe failure");
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => ({
            describeUserPoolClient: async (): Promise<DescribeUserPoolClientCommandOutput> => {
                throw failure;
            },
        }),
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    const message =
        "Failed to get ClientSecret for UserPool Client. - synthetic describe failure";
    assert.equal(recorder.responses[0].response.status, "FAILED");
    assert.deepEqual(recorder.responses[0].response.data, { error: message });
    assert.equal(
        recorder.responses[0].response.physicalResourceId,
        `${USER_POOL_CLIENT_SECRET_PHYSICAL_ID}-${clientId}`,
    );
    assert.deepEqual(capturedLogger.errorMessages, [message]);
});

test("missing identifiers follow the exception path and use the failed suffix", async () => {
    const inputs: DescribeUserPoolClientInput[] = [];
    const recorder = makeResponseRecorder();
    const event: UserPoolClientSecretEvent = {
        ...makeEvent("Create"),
        ResourceProperties: {},
    };

    await createHandler({
        cognito: (): UserPoolClientSecretCognito => ({
            describeUserPoolClient: async (
                input: DescribeUserPoolClientInput,
            ): Promise<DescribeUserPoolClientCommandOutput> => {
                inputs.push(input);
                throw new TypeError("ClientId must be a string");
            },
        }),
        logger: quietLogger,
        responseSender: recorder.sender,
    })(event, context);

    assert.deepEqual(inputs, [{ UserPoolId: undefined, ClientId: undefined }]);
    assert.equal(recorder.responses[0].response.status, "FAILED");
    assert.equal(
        recorder.responses[0].response.physicalResourceId,
        `${USER_POOL_CLIENT_SECRET_PHYSICAL_ID}-failed`,
    );
    assert.deepEqual(recorder.responses[0].response.data, {
        error:
            "Failed to get ClientSecret for UserPool Client. - ClientId must be a string",
    });
});
