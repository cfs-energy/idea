import assert from "node:assert/strict";
import test from "node:test";
import type {
    CfnLogger,
    CfnResourceEvent,
    CfnResponse,
    CfnResponsePutRequest,
} from "../../src/lambda/commons/cfn-response.ts";
import {
    buildCfnResponsePayload,
    sendCfnResponse,
} from "../../src/lambda/commons/cfn-response.ts";

const event: CfnResourceEvent = {
    RequestType: "Create",
    ResponseURL: "https://example.invalid/response",
    StackId: "synthetic-stack",
    RequestId: "synthetic-request",
    LogicalResourceId: "SyntheticResource",
    ResourceProperties: {},
};

const context = { logStreamName: "synthetic-log-stream" };

const quietLogger: CfnLogger = {
    info: (): void => {},
    error: (): void => {},
};

test("builds the Python response body fields, order, spacing, and defaults", () => {
    const body = buildCfnResponsePayload({
        context,
        event,
        status: "SUCCESS",
        data: { nested: { enabled: true }, values: [1, null, "x"] },
    });

    assert.equal(
        body,
        '{"Status": "SUCCESS", "Reason": "See the details in CloudWatch Log Stream: synthetic-log-stream", "PhysicalResourceId": "synthetic-log-stream", "StackId": "synthetic-stack", "RequestId": "synthetic-request", "LogicalResourceId": "SyntheticResource", "NoEcho": false, "Data": {"nested": {"enabled": true}, "values": [1, null, "x"]}}',
    );
});

test("preserves explicit empty response values instead of applying defaults", () => {
    const body = buildCfnResponsePayload({
        context,
        event,
        status: "FAILED",
        physicalResourceId: "",
        reason: "",
        noEcho: null,
        data: null,
    });

    assert.deepEqual(JSON.parse(body), {
        Status: "FAILED",
        Reason: "",
        PhysicalResourceId: "",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticResource",
        NoEcho: null,
        Data: null,
    });
});

test("uses Python json.dumps ASCII escaping", () => {
    const body = buildCfnResponsePayload({
        context,
        event,
        status: "SUCCESS",
        data: { message: "café \u{1f600}" },
    });

    assert.match(body, /"message": "caf\\u00e9 \\ud83d\\ude00"/);
});

test("PUTs with an empty content-type and explicit Python-style content length", async () => {
    let captured: CfnResponsePutRequest | undefined;
    const response: CfnResponse = {
        context,
        event,
        status: "SUCCESS",
        physicalResourceId: "synthetic-resource",
        data: {},
    };

    await sendCfnResponse(response, {
        logger: quietLogger,
        put: async (request: CfnResponsePutRequest): Promise<number> => {
            captured = request;
            return 200;
        },
    });

    assert.ok(captured);
    assert.equal(captured.url, event.ResponseURL);
    assert.equal(captured.headers["content-type"], "");
    assert.equal(captured.headers["content-length"], String(captured.body.length));
    assert.equal(JSON.parse(captured.body).PhysicalResourceId, "synthetic-resource");
});

test("never throws when the response PUT fails", async () => {
    await assert.doesNotReject(
        sendCfnResponse(
            {
                context,
                event,
                status: "FAILED",
                data: {},
            },
            {
                logger: quietLogger,
                put: async (): Promise<number> => {
                    throw new Error("synthetic transport failure");
                },
            },
        ),
    );
});
