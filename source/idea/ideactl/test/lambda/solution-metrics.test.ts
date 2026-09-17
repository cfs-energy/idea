/**
 * The metrics function is neutered: it answers CloudFormation exactly as before and sends nothing.
 *
 * It stays deployed for one release because job stacks created before the upgrade still name its
 * ARN on Delete, so the response has to keep its shape: SUCCESS, empty data, and the same constant
 * physical id. What has to change is that no request leaves the function, which is why every test
 * below routes anything it could reach through a recording loopback server.
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";
import type {
    CfnLogger,
    CfnResponse,
    CfnResponseSender,
} from "../../src/lambda/commons/cfn-response.ts";
import type { SolutionMetricsEvent } from "../../src/lambda/idea_solution_metrics/index.ts";
import { createHandler, handler } from "../../src/lambda/idea_solution_metrics/index.ts";

const context = { logStreamName: "synthetic-log-stream" };

/** Capture logs without writing test output. */
function makeLogger(): { logger: CfnLogger; infos: string[]; errors: string[] } {
    const infos: string[] = [];
    const errors: string[] = [];
    return {
        infos,
        errors,
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

/** Record CloudFormation responses instead of contacting a response URL. */
function makeResponseRecorder(): { responses: CfnResponse[]; sender: CfnResponseSender } {
    const responses: CfnResponse[] = [];
    return {
        responses,
        sender: async (response: CfnResponse): Promise<void> => {
            responses.push(response);
        },
    };
}

/**
 * The scheduler's SendAnonymousData input, with the property names Python's build_metrics()
 * produced. Job stacks deployed before this release still send exactly this.
 */
function makeEvent(requestType: SolutionMetricsEvent["RequestType"]): SolutionMetricsEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/cloudformation-response",
        StackId: "synthetic-stack",
        RequestId: `synthetic-${requestType.toLowerCase()}-request`,
        LogicalResourceId: "SendAnonymousData",
        PhysicalResourceId: "previous-physical-id",
        ResourceProperties: {
            ServiceToken: "redacted-service-token",
            DesiredCapacity: "2",
            InstanceType: "['m7i.large']",
            BaseOS: "rhel9",
            StackUUID: "synthetic-stack-uuid",
            Version: "26.09.0",
            Region: "us-east-1",
            Misc: "café",
        },
    };
}

/** The response every request type gets, unchanged from the version that posted. */
function assertSuccessResponse(responses: CfnResponse[]): void {
    assert.equal(responses.length, 1);
    assert.equal(responses[0]?.status, "SUCCESS");
    assert.equal(responses[0]?.physicalResourceId, "SolutionMetricsSO0072");
    assert.deepEqual(responses[0]?.data, {});
}

for (const requestType of ["Create", "Update", "Delete"] as const) {
    test(`${requestType} answers SUCCESS with the constant physical id`, async () => {
        const logs = makeLogger();
        const responses = makeResponseRecorder();

        await createHandler({ logger: logs.logger, responseSender: responses.sender })(
            makeEvent(requestType),
            context,
        );

        assertSuccessResponse(responses.responses);
        assert.deepEqual(logs.errors, []);
    });
}

/** Start a throwaway loopback HTTP server and return its base URL. */
async function startServer(
    onRequest: (request: IncomingMessage, response: ServerResponse) => void,
): Promise<{ baseUrl: string; server: Server }> {
    const server = createServer(onRequest);
    await new Promise<void>((resolve) => {
        server.listen(0, "127.0.0.1", resolve);
    });
    const { port } = server.address() as AddressInfo;
    return { baseUrl: `http://127.0.0.1:${port}`, server };
}

/** Close a throwaway server and wait for its sockets to drain. */
async function stopServer(server: Server): Promise<void> {
    await new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
    });
}

test("the exported handler answers CloudFormation and posts nothing", async () => {
    const seen: string[] = [];
    const { baseUrl, server } = await startServer((request, response) => {
        seen.push(`${request.method ?? ""} ${request.url ?? ""}`);
        request.resume();
        response.writeHead(200);
        response.end();
    });

    // Both destinations the function could reach point at the recorder: the CloudFormation
    // response URL, and the endpoint the posting version read out of the environment. Anything
    // other than the single response PUT is a request this function must no longer make.
    const previousUrl = process.env.AWS_METRICS_URL;
    process.env.AWS_METRICS_URL = `${baseUrl}/metrics`;
    const consoleInfo = console.info;
    console.info = (): void => {};
    try {
        const event = makeEvent("Create");
        event.ResponseURL = `${baseUrl}/cfn-response`;
        await handler(event, context);
    } finally {
        console.info = consoleInfo;
        if (previousUrl === undefined) delete process.env.AWS_METRICS_URL;
        else process.env.AWS_METRICS_URL = previousUrl;
        await stopServer(server);
    }

    assert.deepEqual(seen, ["PUT /cfn-response"]);
});

test("a failing response sender is not swallowed into a silent no-answer", async () => {
    await assert.rejects(
        createHandler({
            logger: makeLogger().logger,
            responseSender: async (): Promise<void> => {
                throw new Error("synthetic response failure");
            },
        })(makeEvent("Delete"), context),
        /synthetic response failure/,
    );
});
