import {
    DescribeNetworkInterfacesCommand,
    EC2Client,
} from "@aws-sdk/client-ec2";
import type {
    DescribeNetworkInterfacesCommandInput,
} from "@aws-sdk/client-ec2";
import type {
    CfnLogger,
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import {
    errorMessage,
    sendCfnResponse,
} from "../commons/cfn-response.ts";

export const PHYSICAL_RESOURCE_ID = "opensearch-private-ip-addresses";

type OpenSearchPrivateIpsProperties = Record<string, JsonValue> & {
    DomainName?: JsonValue;
    UpdateToken?: JsonValue;
};

export type OpenSearchPrivateIpsEvent =
    CfnResourceEvent<OpenSearchPrivateIpsProperties>;

/** Minimal response shape, including Python's explicit `None` edge cases. */
export interface OpenSearchPrivateIpsEc2Response {
    NetworkInterfaces?:
        | Array<{
              PrivateIpAddresses?:
                  | Array<{ PrivateIpAddress?: string | null }>
                  | null;
          }>
        | null;
}

/** The single EC2 operation used to discover OpenSearch ENI addresses. */
export interface OpenSearchPrivateIpsEc2 {
    describeNetworkInterfaces(
        input: DescribeNetworkInterfacesCommandInput,
    ): Promise<OpenSearchPrivateIpsEc2Response>;
}

/** Logger shape including Python's debug-level network-interface logging. */
export interface OpenSearchPrivateIpsLogger extends CfnLogger {
    debug?(message: string): void;
}

/** Injectable collaborators used by unit tests and the production handler. */
export interface OpenSearchPrivateIpsHandlerDependencies {
    ec2?: () => OpenSearchPrivateIpsEc2;
    logger?: OpenSearchPrivateIpsLogger;
    responseSender?: CfnResponseSender;
}

const defaultLogger: OpenSearchPrivateIpsLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void =>
        console.error(message, error),
    // Debug logs are not emitted.
};

/** Construct the AWS SDK v3 adapter only after the Delete fast path. */
function createEc2(): OpenSearchPrivateIpsEc2 {
    const client = new EC2Client({});
    return {
        describeNetworkInterfaces: (
            input: DescribeNetworkInterfacesCommandInput,
        ): Promise<OpenSearchPrivateIpsEc2Response> =>
            client.send(new DescribeNetworkInterfacesCommand(input)),
    };
}

/** Render the values relevant to the Python handler's f-string error data. */
function pythonString(value: JsonValue): string {
    if (value === null) {
        return "None";
    }
    if (typeof value === "boolean") {
        return value ? "True" : "False";
    }
    if (typeof value === "string" || typeof value === "number") {
        return String(value);
    }
    if (Array.isArray(value)) {
        return `[${value.map(pythonString).join(", ")}]`;
    }
    return `{${Object.entries(value)
        .map(([key, entry]) => `'${key}': '${pythonString(entry)}'`)
        .join(", ")}}`;
}

/** Match string concatenation at the Python logging statement. */
function requireDomainName(value: JsonValue): string {
    if (typeof value === "string") {
        return value;
    }

    const pythonType =
        value === null
            ? "NoneType"
            : Array.isArray(value)
              ? "list"
              : typeof value === "number"
                ? Number.isInteger(value)
                    ? "int"
                    : "float"
                : typeof value === "boolean"
                  ? "bool"
                  : "dict";
    throw new TypeError(
        `can only concatenate str (not "${pythonType}") to str`,
    );
}

/** Build the OpenSearch ENI discovery custom-resource handler. */
export function createHandler(
    dependencies: OpenSearchPrivateIpsHandlerDependencies = {},
): (
    event: OpenSearchPrivateIpsEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: OpenSearchPrivateIpsEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        let domainNameValue: JsonValue = null;

        try {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);

            // Delete deliberately performs no EC2 operation.
            if (event.RequestType === "Delete") {
                await responseSender(
                    {
                        context,
                        event,
                        status: "SUCCESS",
                        data: {},
                        physicalResourceId: PHYSICAL_RESOURCE_ID,
                    },
                    { logger },
                );
                return;
            }

            domainNameValue = event.ResourceProperties.DomainName ?? null;
            const domainName = requireDomainName(domainNameValue);
            logger.info(`OpenSearch DomainName: ${domainName}`);

            const ec2 = (dependencies.ec2 ?? createEc2)();
            const response = await ec2.describeNetworkInterfaces({
                Filters: [
                    { Name: "description", Values: [`ES ${domainName}`] },
                    {
                        Name: "requester-id",
                        Values: ["amazon-elasticsearch"],
                    },
                    { Name: "status", Values: ["in-use"] },
                ],
            });

            const networkInterfaces =
                response.NetworkInterfaces === undefined
                    ? []
                    : response.NetworkInterfaces;
            if (networkInterfaces === null) {
                throw new TypeError("'NoneType' object is not iterable");
            }

            const result: string[] = [];
            for (const networkInterface of networkInterfaces) {
                logger.debug?.(JSON.stringify(networkInterface));
                const privateIpAddresses =
                    networkInterface.PrivateIpAddresses === undefined
                        ? []
                        : networkInterface.PrivateIpAddresses;
                if (privateIpAddresses === null) {
                    throw new TypeError("'NoneType' object is not iterable");
                }
                for (const privateIpAddress of privateIpAddresses) {
                    const ipAddress = privateIpAddress.PrivateIpAddress;
                    if (ipAddress === undefined || ipAddress === null) {
                        continue;
                    }
                    result.push(ipAddress);
                }
            }

            if (result.length === 0) {
                const message = "No in-use IP addresses found";
                logger.error(message);
                await responseSender(
                    {
                        context,
                        event,
                        status: "FAILED",
                        data: { error: message },
                        physicalResourceId: PHYSICAL_RESOURCE_ID,
                    },
                    { logger },
                );
                return;
            }

            await responseSender(
                {
                    context,
                    event,
                    status: "SUCCESS",
                    data: { IpAddresses: result.join(",") },
                    physicalResourceId: PHYSICAL_RESOURCE_ID,
                },
                { logger },
            );
        } catch (error: unknown) {
            logger.error(
                `Failed to get ES Private IP Address: ${errorMessage(error)}`,
                error,
            );
            const errorData = `Exception getting in-use private IP addresses for ES idea-${pythonString(domainNameValue)}`;
            await responseSender(
                {
                    context,
                    event,
                    status: "FAILED",
                    data: { error: errorData },
                    physicalResourceId: PHYSICAL_RESOURCE_ID,
                },
                { logger },
            );
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
