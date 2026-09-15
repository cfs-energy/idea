/**
 * IDEA self-signed certificate, defused.
 *
 * Generation moved to the deploy tool (`src/cli/certificates.ts`). This handler is what the four
 * `Custom::SelfSignedCertificate*` resources still in the templates invoke for one more release,
 * so a deployed cluster updates its function in place instead of deleting a custom resource.
 *
 * It never creates, imports or deletes anything. Create and Update find the two secrets by their
 * `idea:SecretName` tag and, when the resource asks for one, the ISSUED ACM certificate for the
 * domain, and return the three values the stacks used to read. Delete does nothing and succeeds:
 * the Python handler's Delete force-destroyed the secrets a live load balancer was serving.
 */

import type {
    CfnLogger,
    CfnResourceContext,
    CfnResourceEvent,
    CfnResponseSender,
    JsonValue,
} from "../commons/cfn-response.ts";
import { errorMessage, sendCfnResponse } from "../commons/cfn-response.ts";

const SECRETS_MANAGER_CLIENT_PACKAGE = "@aws-sdk/client-secrets-manager";
const ACM_CLIENT_PACKAGE = "@aws-sdk/client-acm";

/** The tag the secrets are found by; `GetSecretValue` takes no condition key on the name. */
export const SECRET_NAME_TAG = "idea:SecretName";

/** Named in every failure, because it is what now creates these secrets. */
const GENERATOR = "the deploy tool (ideactl deploy)";

type SelfSignedCertificateProperties = Record<string, JsonValue> & {
    certificate_name?: JsonValue;
    domain_name?: JsonValue;
    create_acm_certificate?: JsonValue;
};

export type SelfSignedCertificateEvent =
    CfnResourceEvent<SelfSignedCertificateProperties>;

export interface SecretSummary {
    Name?: string;
    ARN?: string;
}

/** The one Secrets Manager read this handler makes. */
export interface SelfSignedCertificateSecrets {
    listSecretsByTagValue(
        tagKey: string,
        tagValues: string[],
    ): Promise<SecretSummary[]>;
}

export interface AcmCertificateSummary {
    DomainName?: string;
    CertificateArn?: string;
}

/** The one ACM read this handler makes. */
export interface SelfSignedCertificateAcm {
    listIssuedCertificates(): Promise<AcmCertificateSummary[]>;
}

export interface SelfSignedCertificateHandlerDependencies {
    secrets?: () =>
        | SelfSignedCertificateSecrets
        | Promise<SelfSignedCertificateSecrets>;
    acm?: () => SelfSignedCertificateAcm | Promise<SelfSignedCertificateAcm>;
    logger?: CfnLogger;
    responseSender?: CfnResponseSender;
}

interface SdkClient {
    send(command: object): Promise<Record<string, unknown>>;
}

interface SecretsManagerSdkModule {
    SecretsManagerClient: new (configuration: Record<string, never>) => SdkClient;
    ListSecretsCommand: new (input: {
        Filters: Array<{ Key: string; Values: string[] }>;
    }) => object;
}

interface AcmSdkModule {
    ACMClient: new (configuration: Record<string, never>) => SdkClient;
    ListCertificatesCommand: new (input: {
        CertificateStatuses: string[];
        NextToken?: string;
    }) => object;
}

const defaultLogger: CfnLogger = {
    info: (message: string): void => console.info(message),
    error: (message: string, error?: unknown): void => {
        if (error === undefined) {
            console.error(message);
            return;
        }
        console.error(message, error);
    },
};

function isSecretsManagerSdkModule(
    value: object,
): value is SecretsManagerSdkModule {
    return (
        "SecretsManagerClient" in value &&
        typeof value.SecretsManagerClient === "function" &&
        "ListSecretsCommand" in value &&
        typeof value.ListSecretsCommand === "function"
    );
}

function isAcmSdkModule(value: object): value is AcmSdkModule {
    return (
        "ACMClient" in value &&
        typeof value.ACMClient === "function" &&
        "ListCertificatesCommand" in value &&
        typeof value.ListCertificatesCommand === "function"
    );
}

/** Load the service client lazily. */
async function createSecrets(): Promise<SelfSignedCertificateSecrets> {
    // A literal specifier, so the bundler inlines the pinned client instead of leaving the
    // resolution to whatever the managed runtime ships.
    const sdkModule: object = await import("@aws-sdk/client-secrets-manager");
    if (!isSecretsManagerSdkModule(sdkModule)) {
        throw new Error(
            `${SECRETS_MANAGER_CLIENT_PACKAGE} has an unexpected export shape`,
        );
    }
    const client = new sdkModule.SecretsManagerClient({});
    return {
        listSecretsByTagValue: async (
            tagKey: string,
            tagValues: string[],
        ): Promise<SecretSummary[]> => {
            const result = await client.send(
                new sdkModule.ListSecretsCommand({
                    Filters: [
                        { Key: "tag-key", Values: [tagKey] },
                        { Key: "tag-value", Values: tagValues },
                    ],
                }),
            );
            const list = result.SecretList;
            return Array.isArray(list) ? (list as SecretSummary[]) : [];
        },
    };
}

/** Load the service client lazily. */
async function createAcm(): Promise<SelfSignedCertificateAcm> {
    // A literal specifier, so the bundler inlines the pinned client instead of leaving the
    // resolution to whatever the managed runtime ships.
    const sdkModule: object = await import("@aws-sdk/client-acm");
    if (!isAcmSdkModule(sdkModule)) {
        throw new Error(`${ACM_CLIENT_PACKAGE} has an unexpected export shape`);
    }
    const client = new sdkModule.ACMClient({});
    return {
        listIssuedCertificates: async (): Promise<AcmCertificateSummary[]> => {
            const summaries: AcmCertificateSummary[] = [];
            let nextToken: string | undefined;
            do {
                const page = await client.send(
                    new sdkModule.ListCertificatesCommand({
                        CertificateStatuses: ["ISSUED"],
                        NextToken: nextToken,
                    }),
                );
                const list = page.CertificateSummaryList;
                if (Array.isArray(list)) {
                    summaries.push(...(list as AcmCertificateSummary[]));
                }
                nextToken =
                    typeof page.NextToken === "string" ? page.NextToken : undefined;
            } while (nextToken !== undefined && nextToken !== "");
            return summaries;
        },
    };
}

function requiredProperty(value: JsonValue | undefined, name: string): string {
    if (typeof value === "string" && value !== "") return value;
    throw new Error(`${name} is missing from the resource properties`);
}

/**
 * The three values the stacks read, found rather than created. A secret that is not there is a
 * hard failure naming what creates it: returning nothing would hand a listener an empty ARN.
 */
export async function findCertificate(
    properties: SelfSignedCertificateProperties,
    dependencies: SelfSignedCertificateHandlerDependencies,
    logger: CfnLogger,
): Promise<Record<string, JsonValue>> {
    const certificateName = requiredProperty(
        properties.certificate_name,
        "certificate_name",
    );
    const domainName = requiredProperty(properties.domain_name, "domain_name");
    const certificateSecretName = `${certificateName}-certificate`;
    const privateKeySecretName = `${certificateName}-private-key`;

    const secrets = await (dependencies.secrets ?? createSecrets)();
    const found = await secrets.listSecretsByTagValue(SECRET_NAME_TAG, [
        certificateSecretName,
        privateKeySecretName,
    ]);
    const certificateSecretArn = found.find(
        (secret) => secret.Name === certificateSecretName,
    )?.ARN;
    const privateKeySecretArn = found.find(
        (secret) => secret.Name === privateKeySecretName,
    )?.ARN;

    const missing = [
        certificateSecretArn === undefined ? certificateSecretName : undefined,
        privateKeySecretArn === undefined ? privateKeySecretName : undefined,
    ].filter((name): name is string => name !== undefined);
    if (missing.length > 0) {
        throw new Error(
            `no secret tagged ${SECRET_NAME_TAG}=${missing.join(" or ")}. ` +
                `${GENERATOR} creates these before the stack deploys; this handler only reads them.`,
        );
    }
    logger.info(`found: ${certificateSecretName}, ${privateKeySecretName}`);

    const data: Record<string, JsonValue> = {
        certificate_secret_arn: certificateSecretArn as string,
        private_key_secret_arn: privateKeySecretArn as string,
        acm_certificate_arn: null,
    };
    if (properties.create_acm_certificate !== true && properties.create_acm_certificate !== "true") {
        return data;
    }

    const acm = await (dependencies.acm ?? createAcm)();
    const issued = await acm.listIssuedCertificates();
    const acmCertificateArn = issued.find(
        (certificate) => certificate.DomainName === domainName,
    )?.CertificateArn;
    if (acmCertificateArn === undefined) {
        throw new Error(
            `no ISSUED ACM certificate for domain ${domainName}. ` +
                `${GENERATOR} imports it before the stack deploys; this handler only reads it.`,
        );
    }
    data.acm_certificate_arn = acmCertificateArn;
    return data;
}

/** Build the custom-resource handler with injectable AWS reads. */
export function createHandler(
    dependencies: SelfSignedCertificateHandlerDependencies = {},
): (
    event: SelfSignedCertificateEvent,
    context: CfnResourceContext,
) => Promise<void> {
    const logger = dependencies.logger ?? defaultLogger;
    const responseSender = dependencies.responseSender ?? sendCfnResponse;

    return async (
        event: SelfSignedCertificateEvent,
        context: CfnResourceContext,
    ): Promise<void> => {
        const properties = event.ResourceProperties ?? {};
        const certificateName =
            typeof properties.certificate_name === "string"
                ? properties.certificate_name
                : undefined;
        try {
            logger.info(`ReceivedEvent: ${JSON.stringify(event)}`);

            // Delete keeps the certificate. Nothing in this handler removes a secret or an ACM
            // certificate, so a stack rollback cannot take a load balancer offline.
            if (event.RequestType === "Delete") {
                logger.info(
                    `leaving the certificate and its secrets in place on delete: ${String(certificateName)}`,
                );
                await responseSender({
                    context,
                    event,
                    status: "SUCCESS",
                    data: {},
                    physicalResourceId: certificateName,
                });
                return;
            }

            const data = await findCertificate(properties, dependencies, logger);
            await responseSender({
                context,
                event,
                status: "SUCCESS",
                data,
                physicalResourceId: certificateName,
            });
        } catch (error: unknown) {
            const message = `failed to resolve certificate: ${String(certificateName)} - ${errorMessage(error)}`;
            logger.error(message, error);
            await responseSender({
                context,
                event,
                status: "FAILED",
                data: {},
                physicalResourceId: certificateName,
                reason: message,
            });
        }
    };
}

/** AWS Lambda entry point; stack configuration must use Handler index.handler. */
export const handler = createHandler();
