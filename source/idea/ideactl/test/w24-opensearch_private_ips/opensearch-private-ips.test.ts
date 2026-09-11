import assert from "node:assert/strict";
import test from "node:test";
import type {
    DescribeNetworkInterfacesCommandInput,
} from "@aws-sdk/client-ec2";
import type {
    CfnResponse,
    CfnResponseSender,
    SendCfnResponseOptions,
} from "../../src/lambda/commons/cfn-response.ts";
import type {
    OpenSearchPrivateIpsEc2,
    OpenSearchPrivateIpsEc2Response,
    OpenSearchPrivateIpsEvent,
    OpenSearchPrivateIpsLogger,
} from "../../src/lambda/idea_custom_resource_opensearch_private_ips/index.ts";
import {
    createHandler,
    handler,
} from "../../src/lambda/idea_custom_resource_opensearch_private_ips/index.ts";

const context = { logStreamName: "synthetic-log-stream" };
const domainName = "synthetic-domain";
/** Stable physical id the Python handler returns on every path. */
const OPENSEARCH_PRIVATE_IPS_PHYSICAL_ID = "opensearch-private-ip-addresses";

/** Build a synthetic event using the Python handler's property names. */
function makeEvent(
    requestType: OpenSearchPrivateIpsEvent["RequestType"],
    includeDomainName = true,
): OpenSearchPrivateIpsEvent {
    return {
        RequestType: requestType,
        ResponseURL: "https://example.invalid/response",
        StackId: "synthetic-stack",
        RequestId: "synthetic-request",
        LogicalResourceId: "SyntheticOpenSearchPrivateIps",
        ResourceProperties: includeDomainName
            ? {
                  DomainName: domainName,
                  UpdateToken: "synthetic-update-token",
              }
            : { UpdateToken: "synthetic-update-token" },
    };
}

/** Capture CloudFormation responses without making an HTTP request. */
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

/** Capture all facts logged by the handler. */
function makeLogger(): {
    infoMessages: string[];
    errorMessages: string[];
    debugMessages: string[];
    logger: OpenSearchPrivateIpsLogger;
} {
    const infoMessages: string[] = [];
    const errorMessages: string[] = [];
    const debugMessages: string[] = [];
    return {
        infoMessages,
        errorMessages,
        debugMessages,
        logger: {
            info: (message: string): void => {
                infoMessages.push(message);
            },
            error: (message: string): void => {
                errorMessages.push(message);
            },
            debug: (message: string): void => {
                debugMessages.push(message);
            },
        },
    };
}

/** Build an EC2 stub returning one configured response. */
function makeEc2(
    response: OpenSearchPrivateIpsEc2Response,
    inputs: DescribeNetworkInterfacesCommandInput[] = [],
): OpenSearchPrivateIpsEc2 {
    return {
        describeNetworkInterfaces: async (
            input: DescribeNetworkInterfacesCommandInput,
        ): Promise<OpenSearchPrivateIpsEc2Response> => {
            inputs.push(input);
            return response;
        },
    };
}

test("exports index.handler as a function", () => {
    assert.equal(typeof handler, "function");
});

test("Create uses the exact filters and returns every private IP in source order", async () => {
    const inputs: DescribeNetworkInterfacesCommandInput[] = [];
    const ec2 = makeEc2(
        {
            NetworkInterfaces: [
                {
                    PrivateIpAddresses: [
                        { PrivateIpAddress: "198.51.100.20" },
                        {},
                        { PrivateIpAddress: null },
                        { PrivateIpAddress: "192.0.2.11" },
                    ],
                },
                {
                    PrivateIpAddresses: [
                        { PrivateIpAddress: "192.0.2.10" },
                    ],
                },
            ],
        },
        inputs,
    );
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();
    const event = makeEvent("Create");

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 => ec2,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(event, context);

    assert.deepEqual(inputs, [
        {
            Filters: [
                { Name: "description", Values: [`ES ${domainName}`] },
                {
                    Name: "requester-id",
                    Values: ["amazon-elasticsearch"],
                },
                { Name: "status", Values: ["in-use"] },
            ],
        },
    ]);
    assert.deepEqual(recorder.responses, [
        {
            context,
            event,
            status: "SUCCESS",
            data: {
                IpAddresses: "198.51.100.20,192.0.2.11,192.0.2.10",
            },
            physicalResourceId: OPENSEARCH_PRIVATE_IPS_PHYSICAL_ID,
        },
    ]);
    assert.match(capturedLogger.infoMessages[0], /^ReceivedEvent: /);
    assert.ok(
        capturedLogger.infoMessages.includes(
            `OpenSearch DomainName: ${domainName}`,
        ),
    );
    assert.equal(capturedLogger.debugMessages.length, 2);
});

test("Update performs the same discovery and succeeds", async () => {
    let describeCalls = 0;
    const ec2: OpenSearchPrivateIpsEc2 = {
        describeNetworkInterfaces: async () => {
            describeCalls += 1;
            return {
                NetworkInterfaces: [
                    {
                        PrivateIpAddresses: [
                            { PrivateIpAddress: "203.0.113.30" },
                        ],
                    },
                ],
            };
        },
    };
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 => ec2,
        logger: makeLogger().logger,
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(describeCalls, 1);
    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.deepEqual(recorder.responses[0].data, {
        IpAddresses: "203.0.113.30",
    });
});

test("Delete succeeds without constructing an EC2 client", async () => {
    let ec2FactoryCalls = 0;
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 => {
            ec2FactoryCalls += 1;
            throw new Error("EC2 must not be created for Delete");
        },
        logger: makeLogger().logger,
        responseSender: recorder.sender,
    })(makeEvent("Delete"), context);

    assert.equal(ec2FactoryCalls, 0);
    assert.deepEqual(recorder.responses[0], {
        context,
        event: makeEvent("Delete"),
        status: "SUCCESS",
        data: {},
        physicalResourceId: OPENSEARCH_PRIVATE_IPS_PHYSICAL_ID,
    });
});

test("an omitted NetworkInterfaces list returns the Python empty-result failure", async () => {
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 =>
            makeEc2({}),
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error: "No in-use IP addresses found",
    });
    assert.deepEqual(capturedLogger.errorMessages, [
        "No in-use IP addresses found",
    ]);
});

test("an omitted PrivateIpAddresses list returns the empty-result failure", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 =>
            makeEc2({
                NetworkInterfaces: [{}],
            }),
        logger: makeLogger().logger,
        responseSender: recorder.sender,
    })(makeEvent("Update"), context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error: "No in-use IP addresses found",
    });
});

test("a missing PrivateIpAddress is skipped without discarding later addresses", async () => {
    const recorder = makeResponseRecorder();

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 =>
            makeEc2({
                NetworkInterfaces: [
                    {
                        PrivateIpAddresses: [
                            {},
                            { PrivateIpAddress: "192.0.2.40" },
                        ],
                    },
                ],
            }),
        logger: makeLogger().logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.deepEqual(recorder.responses[0].data, {
        IpAddresses: "192.0.2.40",
    });
});

test("an EC2 exception returns Python's domain-specific failure data", async () => {
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();
    const ec2: OpenSearchPrivateIpsEc2 = {
        describeNetworkInterfaces: async () => {
            throw new Error("synthetic EC2 failure");
        },
    };

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 => ec2,
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].status, "FAILED");
    assert.equal(
        recorder.responses[0].physicalResourceId,
        OPENSEARCH_PRIVATE_IPS_PHYSICAL_ID,
    );
    assert.deepEqual(recorder.responses[0].data, {
        error: `Exception getting in-use private IP addresses for ES idea-${domainName}`,
    });
    assert.deepEqual(capturedLogger.errorMessages, [
        "Failed to get ES Private IP Address: synthetic EC2 failure",
    ]);
});

test("a missing DomainName follows Python's concatenation error path before EC2", async () => {
    let ec2FactoryCalls = 0;
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 => {
            ec2FactoryCalls += 1;
            return makeEc2({});
        },
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create", false), context);

    assert.equal(ec2FactoryCalls, 0);
    assert.equal(recorder.responses[0].status, "FAILED");
    assert.deepEqual(recorder.responses[0].data, {
        error:
            "Exception getting in-use private IP addresses for ES idea-None",
    });
    assert.deepEqual(capturedLogger.errorMessages, [
        'Failed to get ES Private IP Address: can only concatenate str (not "NoneType") to str',
    ]);
});

test("the response helper receives the injected logger", async () => {
    const recorder = makeResponseRecorder();
    const capturedLogger = makeLogger();

    await createHandler({
        ec2: (): OpenSearchPrivateIpsEc2 =>
            makeEc2({
                NetworkInterfaces: [
                    { PrivateIpAddresses: [{ PrivateIpAddress: "192.0.2.10" }] },
                ],
            }),
        logger: capturedLogger.logger,
        responseSender: recorder.sender,
    })(makeEvent("Create"), context);

    assert.equal(recorder.responses[0].status, "SUCCESS");
    assert.equal(recorder.options[0]?.logger, capturedLogger.logger);
});

test("the default logger writes no per-interface debug blob", async () => {
    const recorder = makeResponseRecorder();
    const originalDebug = console.debug;
    const originalInfo = console.info;
    const debugMessages: string[] = [];
    const infoMessages: string[] = [];
    const networkInterface = {
        PrivateIpAddresses: [
            { PrivateIpAddress: "192.0.2.10" },
        ],
    };
    const interfaceBlob = JSON.stringify(networkInterface);
    console.debug = (message?: unknown): void => {
        debugMessages.push(String(message));
    };
    console.info = (message?: unknown): void => {
        infoMessages.push(String(message));
    };

    try {
        await createHandler({
            ec2: (): OpenSearchPrivateIpsEc2 =>
                makeEc2({
                    NetworkInterfaces: [networkInterface],
                }),
            responseSender: recorder.sender,
        })(makeEvent("Create"), context);
    } finally {
        console.debug = originalDebug;
        console.info = originalInfo;
    }

    assert.equal(recorder.responses[0].status, "SUCCESS");
    // Positive control: the default logger is in use and does write info lines.
    assert.ok(infoMessages.length > 0);
    assert.equal(debugMessages.length, 0);
    assert.equal(infoMessages.includes(interfaceBlob), false);
    assert.equal(debugMessages.includes(interfaceBlob), false);
});
