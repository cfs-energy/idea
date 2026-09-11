import assert from "node:assert/strict";
import { createHook } from "node:async_hooks";
import { createServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";
import type {
    CfnResponse,
    CfnResponseSender,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    MetricsPostRequest,
    SolutionMetricsEvent,
    SolutionMetricsLogger,
} from "../../src/lambda/idea_solution_metrics/index.ts";
import {
    createHandler,
    handler,
    postMetrics,
} from "../../src/lambda/idea_solution_metrics/index.ts";

const context = { logStreamName: "synthetic-log-stream" };
const timestamp = "2026-09-10T12:34:56.123456";

interface RecordedError {
    message: string;
    error?: unknown;
}

/** Capture metrics and exception logs without writing test output. */
function makeLogger(): {
    logger: SolutionMetricsLogger;
    infos: unknown[];
    errors: RecordedError[];
} {
    const infos: unknown[] = [];
    const errors: RecordedError[] = [];
    return {
        infos,
        errors,
        logger: {
            info: (message: unknown): void => {
                infos.push(message);
            },
            error: (message: string, error?: unknown): void => {
                errors.push({ message, error });
            },
        },
    };
}

/** Record CloudFormation responses instead of contacting a response URL. */
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

/**
 * Build the scheduler's Custom::SendAnonymousMetrics input using the exact
 * property names produced by Python build_metrics().
 */
function makeEvent(
    requestType: SolutionMetricsEvent["RequestType"],
): SolutionMetricsEvent {
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
            Efa: "false",
            ScratchSize: "100",
            RootSize: "50",
            SpotPrice: "false",
            BaseOS: "rhel9",
            StackUUID: "synthetic-stack-uuid",
            KeepForever: "false",
            FsxLustre: "true",
            FsxLustreInfo: {
                DeploymentType: "SCRATCH_2",
                PerUnitStorageThroughput: 200,
                Size: 1200,
            },
            TerminateWhenIdle: "true",
            Dcv: "false",
            Version: "26.09.0",
            Region: "us-east-1",
            Misc: "caf\u00e9",
            RequestType: "must-not-overwrite",
            RequestTimeStamp: "must-not-overwrite",
        },
    };
}

/** Assert the unconditional successful CloudFormation response. */
function assertSuccessResponse(responses: CfnResponse[]): void {
    assert.equal(responses.length, 1);
    assert.equal(responses[0]?.status, "SUCCESS");
    assert.equal(
        responses[0]?.physicalResourceId,
        "SolutionMetricsSO0072",
    );
    assert.deepEqual(responses[0]?.data, {});
}

test("Create posts the Python-compatible anonymous metrics payload", async () => {
    const requests: MetricsPostRequest[] = [];
    const logs = makeLogger();
    const responses = makeResponseRecorder();

    await createHandler({
        environment: {},
        logger: logs.logger,
        post: async (
            request: MetricsPostRequest,
        ): Promise<{ status: number }> => {
            requests.push(request);
            return { status: 200 };
        },
        responseSender: responses.sender,
        timestamp: (): string => timestamp,
    })(makeEvent("Create"), context);

    assert.equal(requests.length, 1);
    assert.equal(requests[0]?.method, "POST");
    assert.equal(
        requests[0]?.url,
        "https://metrics.awssolutionsbuilder.com/generic",
    );
    assert.deepEqual(requests[0]?.headers, {
        "content-type": "application/json",
    });
    assert.equal(
        requests[0]?.body.toString("utf8"),
        [
            "{",
            "    \"TimeStamp\": \"2026-09-10T12:34:56.123456\",",
            "    \"Solution\": \"SO0072\",",
            "    \"UUID\": \"synthetic-create-request\",",
            "    \"Data\": {",
            "        \"RequestType\": \"Create\",",
            "        \"RequestTimeStamp\": \"2026-09-10T12:34:56.123456\",",
            "        \"DesiredCapacity\": \"2\",",
            "        \"InstanceType\": \"['m7i.large']\",",
            "        \"Efa\": \"false\",",
            "        \"ScratchSize\": \"100\",",
            "        \"RootSize\": \"50\",",
            "        \"SpotPrice\": \"false\",",
            "        \"BaseOS\": \"rhel9\",",
            "        \"StackUUID\": \"synthetic-stack-uuid\",",
            "        \"KeepForever\": \"false\",",
            "        \"FsxLustre\": \"true\",",
            "        \"FsxLustreInfo\": {",
            "            \"DeploymentType\": \"SCRATCH_2\",",
            "            \"PerUnitStorageThroughput\": 200,",
            "            \"Size\": 1200",
            "        },",
            "        \"TerminateWhenIdle\": \"true\",",
            "        \"Dcv\": \"false\",",
            "        \"Version\": \"26.09.0\",",
            "        \"Region\": \"us-east-1\",",
            "        \"Misc\": \"caf\\u00e9\"",
            "    }",
            "}",
        ].join("\n"),
    );
    assert.deepEqual(logs.infos, [
        {
            Solution: "SO0072",
            UUID: "synthetic-create-request",
            Data: {
                RequestType: "Create",
                RequestTimeStamp: timestamp,
                DesiredCapacity: "2",
                InstanceType: "['m7i.large']",
                Efa: "false",
                ScratchSize: "100",
                RootSize: "50",
                SpotPrice: "false",
                BaseOS: "rhel9",
                StackUUID: "synthetic-stack-uuid",
                KeepForever: "false",
                FsxLustre: "true",
                FsxLustreInfo: {
                    DeploymentType: "SCRATCH_2",
                    PerUnitStorageThroughput: 200,
                    Size: 1200,
                },
                TerminateWhenIdle: "true",
                Dcv: "false",
                Version: "26.09.0",
                Region: "us-east-1",
                Misc: "caf\u00e9",
            },
        },
        "ResponseCode: 200",
    ]);
    assert.deepEqual(logs.errors, []);
    assertSuccessResponse(responses.responses);
});

test("Update uses AWS_METRICS_URL and keeps the constant physical id", async () => {
    const requests: MetricsPostRequest[] = [];
    const responses = makeResponseRecorder();

    await createHandler({
        environment: {
            AWS_METRICS_URL: "https://example.invalid/custom-metrics",
        },
        logger: makeLogger().logger,
        post: async (
            request: MetricsPostRequest,
        ): Promise<{ status: number }> => {
            requests.push(request);
            return { status: 202 };
        },
        responseSender: responses.sender,
        timestamp: (): string => timestamp,
    })(makeEvent("Update"), context);

    assert.equal(requests[0]?.url, "https://example.invalid/custom-metrics");
    assertSuccessResponse(responses.responses);
});

test("Delete still posts metrics and ignores the incoming physical id", async () => {
    const requests: MetricsPostRequest[] = [];
    const responses = makeResponseRecorder();

    await createHandler({
        environment: {},
        logger: makeLogger().logger,
        post: async (
            request: MetricsPostRequest,
        ): Promise<{ status: number }> => {
            requests.push(request);
            return { status: 200 };
        },
        responseSender: responses.sender,
        timestamp: (): string => timestamp,
    })(makeEvent("Delete"), context);

    assert.equal(requests.length, 1);
    assert.match(
        requests[0]?.body.toString("utf8") ?? "",
        /"RequestType": "Delete"/,
    );
    assertSuccessResponse(responses.responses);
});

test("a non-2xx metrics response is logged but does not fail CloudFormation", async () => {
    const logs = makeLogger();
    const responses = makeResponseRecorder();

    await createHandler({
        environment: {},
        logger: logs.logger,
        post: async (): Promise<{ status: number }> => ({ status: 503 }),
        responseSender: responses.sender,
        timestamp: (): string => timestamp,
    })(makeEvent("Create"), context);

    assert.equal(logs.infos[1], "ResponseCode: 503");
    assert.deepEqual(logs.errors, []);
    assertSuccessResponse(responses.responses);
});

test("a metrics POST failure is logged and still returns SUCCESS", async () => {
    const logs = makeLogger();
    const responses = makeResponseRecorder();

    await assert.doesNotReject(
        createHandler({
            environment: {},
            logger: logs.logger,
            post: async (): Promise<{ status: number }> => {
                throw new Error("synthetic POST failure");
            },
            responseSender: responses.sender,
            timestamp: (): string => timestamp,
        })(makeEvent("Create"), context),
    );

    assert.equal(logs.errors.length, 1);
    assert.equal(
        logs.errors[0]?.message,
        "failed to post metrics: synthetic POST failure",
    );
    assertSuccessResponse(responses.responses);
});

test("postMetrics logs and swallows a missing RequestId", async () => {
    const logs = makeLogger();
    let postCalls = 0;

    await assert.doesNotReject(
        postMetrics(
            {
                RequestType: "Create",
                ResourceProperties: {},
            },
            {
                environment: {},
                logger: logs.logger,
                post: async (): Promise<{ status: number }> => {
                    postCalls += 1;
                    return { status: 200 };
                },
                timestamp: (): string => timestamp,
            },
        ),
    );

    assert.equal(postCalls, 0);
    assert.equal(logs.errors[0]?.message, "failed to post metrics: Custom resource event is missing field RequestId.");
});

test("postMetrics logs and swallows a missing RequestType", async () => {
    const logs = makeLogger();

    await postMetrics(
        {
            RequestId: "synthetic-request",
            ResourceProperties: {},
        },
        {
            environment: {},
            logger: logs.logger,
            post: async (): Promise<{ status: number }> => ({ status: 200 }),
            timestamp: (): string => timestamp,
        },
    );

    assert.equal(
        logs.errors[0]?.message,
        "failed to post metrics: Custom resource event is missing field RequestType.",
    );
});

test("postMetrics logs and swallows missing ResourceProperties", async () => {
    const logs = makeLogger();

    await postMetrics(
        {
            RequestId: "synthetic-request",
            RequestType: "Create",
        },
        {
            environment: {},
            logger: logs.logger,
            post: async (): Promise<{ status: number }> => ({ status: 200 }),
            timestamp: (): string => timestamp,
        },
    );

    assert.equal(
        logs.errors[0]?.message,
        "failed to post metrics: Custom resource event is missing field ResourceProperties.",
    );
});

test("postMetrics logs and swallows a non-serializable property", async () => {
    const logs = makeLogger();

    await postMetrics(
        {
            RequestId: "synthetic-request",
            RequestType: "Create",
            ResourceProperties: {
                Invalid: undefined,
            },
        },
        {
            environment: {},
            logger: logs.logger,
            post: async (): Promise<{ status: number }> => ({ status: 200 }),
            timestamp: (): string => timestamp,
        },
    );

    assert.match(
        logs.errors[0]?.message ?? "",
        /^failed to post metrics: Object of type undefined is not JSON serializable$/,
    );
});

test("a property named __proto__ is relayed like Python's data[k] = v", async () => {
    const requests: MetricsPostRequest[] = [];
    // JSON.parse is how Lambda builds the event, and the only way to get an own
    // __proto__ key: an object literal would set the prototype instead.
    const properties = JSON.parse(
        '{"__proto__": "polluted", "BaseOS": "rhel9"}',
    ) as Record<string, string>;

    await postMetrics(
        {
            RequestId: "synthetic-request",
            RequestType: "Create",
            ResourceProperties: properties,
        },
        {
            environment: {},
            logger: makeLogger().logger,
            post: async (
                request: MetricsPostRequest,
            ): Promise<{ status: number }> => {
                requests.push(request);
                return { status: 200 };
            },
            timestamp: (): string => timestamp,
        },
    );

    assert.match(
        requests[0]?.body.toString("utf8") ?? "",
        /"__proto__": "polluted"/,
    );
});

/** Start a throwaway loopback HTTP server and return its base URL. */
async function startServer(
    onRequest: (
        request: IncomingMessage,
        response: ServerResponse,
    ) => void,
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

/** True when an async-hook resource is a TCP connect to a known loopback port. */
function isLoopbackConnect(
    resource: unknown,
    port: number,
): boolean {
    if (typeof resource !== "object" || resource === null) {
        return false;
    }
    if (!("address" in resource) || !("port" in resource)) {
        return false;
    }
    return resource.address === "127.0.0.1" && resource.port === port;
}

test("the real POST path follows a redirect like urllib3 does", async () => {
    const paths: string[] = [];
    const { baseUrl, server } = await startServer((request, response) => {
        paths.push(request.url ?? "");
        request.resume();
        if (request.url === "/redirect") {
            response.writeHead(302, { location: "/final" });
            response.end();
            return;
        }
        response.writeHead(200);
        response.end();
    });
    const logs = makeLogger();

    try {
        await postMetrics(
            {
                RequestId: "synthetic-request",
                RequestType: "Create",
                ResourceProperties: {},
            },
            {
                environment: { AWS_METRICS_URL: `${baseUrl}/redirect` },
                logger: logs.logger,
                timestamp: (): string => timestamp,
            },
        );
    } finally {
        await stopServer(server);
    }

    assert.deepEqual(paths, ["/redirect", "/final"]);
    assert.equal(logs.infos[1], "ResponseCode: 200");
    assert.deepEqual(logs.errors, []);
});

test("a refused metrics endpoint stops retrying and is swallowed", async () => {
    const { baseUrl, server } = await startServer((_request, response) => {
        response.end();
    });
    const { port } = server.address() as AddressInfo;
    await stopServer(server);
    const logs = makeLogger();
    let connectAttempts = 0;
    const hook = createHook({
        init(_asyncId: number, type: string, _triggerAsyncId: number, resource: unknown): void {
            if (type === "TCPCONNECTWRAP" && isLoopbackConnect(resource, port)) {
                connectAttempts += 1;
            }
        },
    });

    hook.enable();
    try {
        await assert.doesNotReject(
            postMetrics(
                {
                    RequestId: "synthetic-request",
                    RequestType: "Create",
                    ResourceProperties: {},
                },
                {
                    environment: { AWS_METRICS_URL: baseUrl },
                    logger: logs.logger,
                    timestamp: (): string => timestamp,
                },
            ),
        );
    } finally {
        hook.disable();
    }

    // urllib3 Retry(3): one initial connect plus three retries, then give up.
    assert.equal(connectAttempts, 4);
    assert.match(logs.errors[0]?.message ?? "", /failed to post metrics: /);
    assert.match(logs.errors[0]?.message ?? "", /ECONNREFUSED/);
});

test("the exported handler posts metrics and answers CloudFormation for real", async () => {
    const seen: { method: string; url: string; body: string }[] = [];
    const { baseUrl, server } = await startServer((request, response) => {
        const chunks: Buffer[] = [];
        request.on("data", (chunk: Buffer) => chunks.push(chunk));
        request.on("end", () => {
            seen.push({
                method: request.method ?? "",
                url: request.url ?? "",
                body: Buffer.concat(chunks).toString("utf8"),
            });
            response.writeHead(200);
            response.end();
        });
    });

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
        if (previousUrl === undefined) {
            delete process.env.AWS_METRICS_URL;
        } else {
            process.env.AWS_METRICS_URL = previousUrl;
        }
        await stopServer(server);
    }

    assert.deepEqual(
        seen.map((entry) => `${entry.method} ${entry.url}`),
        ["POST /metrics", "PUT /cfn-response"],
    );
    assert.match(seen[0]?.body ?? "", /"Solution": "SO0072"/);
    assert.match(
        seen[1]?.body ?? "",
        /"PhysicalResourceId": "SolutionMetricsSO0072"/,
    );
    assert.match(seen[1]?.body ?? "", /"Status": "SUCCESS"/);
});
