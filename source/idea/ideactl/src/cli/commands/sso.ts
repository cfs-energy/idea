/**
 * Single sign-on operator commands.
 *
 * The client and configuration writer are injected so callers can provide the
 * live implementations while tests replay each operation without a network.
 */

import { readFileSync } from "node:fs";

import { Command } from "commander";

import { ClusterConfig, ClusterConfigError, isEmpty } from "../../config/cluster-config.ts";

export const SSO_PROVIDER_OIDC = "OIDC";
export const SSO_PROVIDER_SAML = "SAML";

/** Invalid identity-provider input is reported without changing the process exit code. */
export class InvalidSsoParams extends ClusterConfigError {}

export interface CognitoUser {
  Username?: string;
  UserStatus?: string;
  Attributes?: Array<{ Name?: string; Value?: string }>;
}

export interface CognitoApi {
  getIdentityProviderByIdentifier(input: Record<string, unknown>): Promise<{ IdentityProvider?: Record<string, unknown> }>;
  createIdentityProvider(input: Record<string, unknown>): Promise<void>;
  updateIdentityProvider(input: Record<string, unknown>): Promise<void>;
  createUserPoolClient(input: Record<string, unknown>): Promise<{ UserPoolClient?: { ClientId?: string; ClientSecret?: string } }>;
  updateUserPoolClient(input: Record<string, unknown>): Promise<{ UserPoolClient?: { ClientId?: string; ClientSecret?: string } }>;
  listUsers(input: { UserPoolId: string; PaginationToken?: string }): Promise<{ Users?: CognitoUser[]; PaginationToken?: string }>;
  adminLinkProviderForUser(input: Record<string, unknown>): Promise<void>;
}

export interface SecretsApi {
  describeSecret(input: { SecretId: string }): Promise<{ ARN?: string }>;
  createSecret(input: Record<string, unknown>): Promise<{ ARN?: string }>;
  updateSecret(input: Record<string, unknown>): Promise<{ ARN?: string }>;
}

export interface SsoDeps {
  cognito: CognitoApi;
  secrets: SecretsApi;
  config: ClusterConfig;
  setConfigEntry(key: string, value: unknown): Promise<void>;
  sleep(ms: number): Promise<void>;
  out(line: string): void;
}

/** Builds command-scoped dependencies after Commander has parsed the selected AWS profile. */
export type SsoDepsFactory = (options: {
  clusterName: string;
  awsRegion: string;
  awsProfile?: string;
}) => Promise<SsoDeps>;

type SsoDepsSource = SsoDeps | SsoDepsFactory;

export interface SsoConfigureOptions {
  clusterName: string;
  providerName: string;
  providerType: string;
  providerEmailAttribute: string;
  refreshTokenValidityHours?: number;
  oidcClientId?: string;
  oidcClientSecret?: string;
  oidcIssuer?: string;
  oidcAttributesRequestMethod?: string;
  oidcAuthorizeScopes?: string;
  oidcAuthorizeUrl?: string;
  oidcTokenUrl?: string;
  oidcAttributesUrl?: string;
  oidcJwksUri?: string;
  samlMetadataUrl?: string;
  samlMetadataFile?: string;
}

function required(value: string | undefined, name: string): string {
  if (isEmpty(value)) throw new InvalidSsoParams(`${name} is required`);
  return value as string;
}

function identityProviderModuleId(config: ClusterConfig): string {
  return config.moduleId("identity-provider");
}

async function save(deps: SsoDeps, key: string, value: unknown): Promise<void> {
  await deps.setConfigEntry(`${identityProviderModuleId(deps.config)}.${key}`, value);
}

function isNotFound(error: unknown): boolean {
  return error instanceof Error && ((error as { name?: string }).name === "ResourceNotFoundException" || error.message.includes("ResourceNotFoundException"));
}

export function getSamlProviderDetails(options: SsoConfigureOptions): Record<string, string> {
  if (isEmpty(options.samlMetadataUrl) && isEmpty(options.samlMetadataFile)) {
    throw new InvalidSsoParams("Either one of [saml_metadata_url, saml_metadata_file] is required, when provider_type = SAML");
  }
  if (!isEmpty(options.samlMetadataFile)) {
    try {
      return { MetadataFile: readFileSync(options.samlMetadataFile as string, "utf8") };
    } catch {
      throw new InvalidSsoParams(`file not found: ${options.samlMetadataFile}`);
    }
  }
  return { MetadataURL: options.samlMetadataUrl as string };
}

export function getOidcProviderDetails(options: SsoConfigureOptions): Record<string, string> {
  const clientId = required(options.oidcClientId, "oidc_client_id");
  const clientSecret = required(options.oidcClientSecret, "oidc_client_secret");
  const issuer = required(options.oidcIssuer, "oidc_issuer");
  const details: Record<string, string> = {
    client_id: clientId,
    client_secret: clientSecret,
    attributes_request_method: options.oidcAttributesRequestMethod ?? "GET",
    authorize_scopes: (options.oidcAuthorizeScopes ?? "openid").replaceAll(",", " "),
    oidc_issuer: issuer,
  };
  const optional: ReadonlyArray<[string, string | undefined]> = [
    ["authorize_url", options.oidcAuthorizeUrl],
    ["token_url", options.oidcTokenUrl],
    ["attributes_url", options.oidcAttributesUrl],
    ["jwks_uri", options.oidcJwksUri],
  ];
  for (const [key, value] of optional) if (!isEmpty(value)) details[key] = value as string;
  return details;
}

function callbackUrls(config: ClusterConfig): string[] {
  const loadBalancerDns = config.getString("cluster.load_balancers.external_alb.load_balancer_dns_name", undefined, { required: true }) as string;
  const customDns = config.getString("cluster.load_balancers.external_alb.certificates.custom_dns_name") ??
    config.getString("cluster.load_balancers.external_alb.custom_dns_name");
  const contextPath = config.getString("cluster-manager.server.web_resources_context_path", undefined, { required: true }) as string;
  const path = contextPath === "/" ? "/sso/oauth2/callback" : `${contextPath}/oauth2/callback`;
  const callbackPath = path.startsWith("/") ? path : `/${path}`;
  return [
    `https://${loadBalancerDns}${callbackPath}`,
    ...(isEmpty(customDns) ? [] : [`https://${customDns}${callbackPath}`]),
  ];
}

async function existingIdentityProvider(deps: SsoDeps, userPoolId: string, identifier: string): Promise<boolean> {
  try {
    const result = await deps.cognito.getIdentityProviderByIdentifier({ UserPoolId: userPoolId, IdpIdentifier: identifier });
    return result.IdentityProvider !== undefined;
  } catch (error) {
    if (isNotFound(error)) return false;
    throw error;
  }
}

async function configureIdentityProvider(deps: SsoDeps, options: SsoConfigureOptions): Promise<void> {
  required(options.providerName, "provider_name");
  required(options.providerType, "provider_type");
  required(options.providerEmailAttribute, "provider_email_attribute");
  const userPoolId = deps.config.getString("identity-provider.cognito.user_pool_id", undefined, { required: true }) as string;
  const identifier = deps.config.getString("identity-provider.cognito.sso_idp_identifier", "single-sign-on-identity-provider") as string;
  const providerDetails = options.providerType === SSO_PROVIDER_SAML ? getSamlProviderDetails(options) :
    options.providerType === SSO_PROVIDER_OIDC ? getOidcProviderDetails(options) :
      (() => { throw new InvalidSsoParams("provider type must be one of: SAML or OIDC"); })();
  const request = {
    UserPoolId: userPoolId,
    ProviderName: options.providerName,
    ProviderDetails: providerDetails,
    AttributeMapping: { email: options.providerEmailAttribute },
    IdpIdentifiers: [identifier],
  };
  if (await existingIdentityProvider(deps, userPoolId, identifier)) {
    await deps.cognito.updateIdentityProvider(request);
  } else {
    await deps.cognito.createIdentityProvider({ ...request, ProviderType: options.providerType });
  }
  await save(deps, "cognito.sso_idp_provider_name", options.providerName);
  await save(deps, "cognito.sso_idp_provider_type", options.providerType);
  await save(deps, "cognito.sso_idp_identifier", identifier);
  await save(deps, "cognito.sso_idp_provider_email_attribute", options.providerEmailAttribute);
}

async function configureUserPoolClient(deps: SsoDeps, options: SsoConfigureOptions): Promise<void> {
  const userPoolId = deps.config.getString("identity-provider.cognito.user_pool_id", undefined, { required: true }) as string;
  const configuredClientId = deps.config.getString("identity-provider.cognito.sso_client_id");
  const request: Record<string, unknown> = {
    UserPoolId: userPoolId,
    ClientName: "single-sign-on-client",
    AccessTokenValidity: 1,
    IdTokenValidity: 1,
    RefreshTokenValidity: options.refreshTokenValidityHours === undefined || options.refreshTokenValidityHours <= 0 ? 12 : options.refreshTokenValidityHours,
    TokenValidityUnits: { AccessToken: "hours", IdToken: "hours", RefreshToken: "hours" },
    ReadAttributes: ["address", "birthdate", "custom:aws_region", "custom:cluster_name", "custom:password_last_set", "custom:password_max_age", "email", "email_verified", "family_name", "gender", "given_name", "locale", "middle_name", "name", "nickname", "phone_number", "phone_number_verified", "picture", "preferred_username", "profile", "updated_at", "website", "zoneinfo"],
    AllowedOAuthFlows: ["code"],
    AllowedOAuthScopes: ["email", "openid", "aws.cognito.signin.user.admin"],
    CallbackURLs: callbackUrls(deps.config),
    SupportedIdentityProviders: [options.providerName],
    AllowedOAuthFlowsUserPoolClient: true,
  };
  let client: { ClientId?: string; ClientSecret?: string } | undefined;
  if (!isEmpty(configuredClientId)) {
    client = (await deps.cognito.updateUserPoolClient({ ...request, ClientId: configuredClientId })).UserPoolClient;
  } else {
    client = (await deps.cognito.createUserPoolClient({ ...request, GenerateSecret: true })).UserPoolClient;
    const secretName = `${options.clusterName}-sso-client-secret`;
    const kmsKeyId = deps.config.getString("cluster.secretsmanager.kms_key_id");
    let existing: { ARN?: string } | undefined;
    try {
      existing = await deps.secrets.describeSecret({ SecretId: secretName });
    } catch (error) {
      if (!isNotFound(error)) throw error;
    }
    const secretRequest: Record<string, unknown> = existing === undefined ? {
      Name: secretName,
      Description: `Single Sign-On OAuth2 Client Secret for Cluster: ${options.clusterName}`,
      Tags: [{ Key: "idea:ClusterName", Value: options.clusterName }, { Key: "idea:ModuleName", Value: "cluster-manager" }],
      SecretString: client?.ClientSecret,
    } : { SecretId: existing.ARN, SecretString: client?.ClientSecret };
    if (!isEmpty(kmsKeyId)) secretRequest.KmsKeyId = kmsKeyId;
    const secret = existing === undefined ? await deps.secrets.createSecret(secretRequest) : await deps.secrets.updateSecret(secretRequest);
    await save(deps, "cognito.sso_client_secret", secret.ARN);
    await save(deps, "cognito.sso_client_id", client?.ClientId);
  }
}

async function linkExistingUsers(
  deps: SsoDeps,
  configured?: { providerName: string; providerType: string; providerEmailAttribute: string },
): Promise<void> {
  const userPoolId = deps.config.getString("identity-provider.cognito.user_pool_id", undefined, { required: true }) as string;
  const providerName = configured?.providerName ?? deps.config.getString("identity-provider.cognito.sso_idp_provider_name", undefined, { required: true }) as string;
  const providerType = configured?.providerType ?? deps.config.getString("identity-provider.cognito.sso_idp_provider_type", undefined, { required: true }) as string;
  const adminUsername = deps.config.getString("cluster.administrator_username", undefined, { required: true }) as string;
  const attribute = providerType === SSO_PROVIDER_OIDC ? "email" :
    configured?.providerEmailAttribute ?? deps.config.getString("identity-provider.cognito.sso_idp_provider_email_attribute", undefined, { required: true }) as string;
  let token: string | undefined;
  do {
    const page = await deps.cognito.listUsers({ UserPoolId: userPoolId, PaginationToken: token });
    for (const user of page.Users ?? []) {
      try {
        if (user.UserStatus === "EXTERNAL_PROVIDER") continue;
        const username = user.Username ?? "";
        if (adminUsername.includes(username) || username.startsWith("clusteradmin")) {
          deps.out(`system administration user found: ${username}. skip linking with IDP.`);
          continue;
        }
        const email = user.Attributes?.find((entry) => entry.Name === "email")?.Value;
        // The JSON string identity attribute is not treated as a list.
        const alreadyLinked = false;
        if (isEmpty(email)) continue;
        if (alreadyLinked) {
          deps.out(`user: ${username}, email: ${email} already linked. skip.`);
          continue;
        }
        deps.out(`linking user: ${username}, email: ${email} ...`);
        await deps.cognito.adminLinkProviderForUser({
          UserPoolId: userPoolId,
          DestinationUser: { ProviderName: "Cognito", ProviderAttributeName: "cognito:username", ProviderAttributeValue: username },
          SourceUser: { ProviderName: providerName, ProviderAttributeName: attribute, ProviderAttributeValue: email },
        });
        await deps.sleep(200);
      } catch (error) {
        deps.out(`failed to link user: ${JSON.stringify(user)} with IDP: ${providerName} - ${String(error)}`);
      }
    }
    token = page.PaginationToken;
  } while (!isEmpty(token));
}

/** Execute the identity-provider, client, linking and final enablement sequence. */
export async function configureSso(deps: SsoDeps, options: SsoConfigureOptions): Promise<void> {
  await configureIdentityProvider(deps, options);
  await configureUserPoolClient(deps, options);
  await linkExistingUsers(deps, {
    providerName: options.providerName,
    providerType: options.providerType,
    providerEmailAttribute: options.providerEmailAttribute,
  });
  await save(deps, "cognito.sso_enabled", true);
}

/** Return the redirect information printed by `sso show-idp-info`. */
export function showIdpInfo(config: ClusterConfig, providerType: string): { redirectUrl: string; entityId?: string } {
  if (![SSO_PROVIDER_SAML, SSO_PROVIDER_OIDC].includes(providerType.trim().toUpperCase())) {
    throw new ClusterConfigError("Invalid provider type. Must be one of: [SAML, OIDC]");
  }
  const domain = config.getString("identity-provider.cognito.domain_url", undefined, { required: true }) as string;
  if (providerType === SSO_PROVIDER_SAML) {
    const pool = config.getString("identity-provider.cognito.user_pool_id", undefined, { required: true }) as string;
    return { redirectUrl: `${domain}/saml2/idpresponse`, entityId: `urn:amazon:cognito:sp:${pool}` };
  }
  return { redirectUrl: `${domain}/oauth2/idpresponse` };
}

/** Register the `sso` command group. Its injected dependencies are supplied by the command core. */
export function registerSsoCommands(program: Command, deps: SsoDepsSource): Command {
  const resolveDeps = async (options: {
    clusterName: string;
    awsRegion: string;
    awsProfile?: string;
  }): Promise<SsoDeps> => typeof deps === "function" ? deps(options) : deps;
  const group = program.command("sso").description("single sign-on configuration");
  group.command("show-idp-info")
    .requiredOption("--cluster-name <cluster-name>")
    .requiredOption("--aws-region <aws-region>")
    .option("--aws-profile <aws-profile>")
    .requiredOption("--provider-type <provider-type>")
    .action(async (options: { clusterName: string; awsRegion: string; awsProfile?: string; providerType: string }) => {
      const actionDeps = await resolveDeps(options);
      const result = showIdpInfo(actionDeps.config, options.providerType);
      actionDeps.out("Redirect URL");
      actionDeps.out(result.redirectUrl);
      if (result.entityId !== undefined) {
        actionDeps.out("Entity ID");
        actionDeps.out(result.entityId);
      }
    });
  group.command("configure")
    .requiredOption("--cluster-name <cluster-name>")
    .requiredOption("--aws-region <aws-region>")
    .option("--aws-profile <aws-profile>")
    .requiredOption("--provider-name <provider-name>")
    .requiredOption("--provider-type <provider-type>")
    .requiredOption("--provider-email-attribute <provider-email-attribute>")
    .option("--refresh-token-validity-hours <hours>", "Refresh token validity in hours. Default: 12", Number)
    .option("--oidc-client-id <id>").option("--oidc-client-secret <secret>").option("--oidc-issuer <issuer>")
    .option("--oidc-attributes-request-method <method>").option("--oidc-authorize-scopes <scopes>")
    .option("--oidc-authorize-url <url>").option("--oidc-token-url <url>").option("--oidc-attributes-url <url>")
    .option("--oidc-jwks-uri <uri>").option("--saml-metadata-url <url>").option("--saml-metadata-file <file>")
    .action(async (options: Record<string, unknown>) => {
      const actionDeps = await resolveDeps({
        clusterName: String(options.clusterName),
        awsRegion: String(options.awsRegion),
        awsProfile: typeof options.awsProfile === "string" ? options.awsProfile : undefined,
      });
      try {
        await configureSso(actionDeps, {
          clusterName: String(options.clusterName), providerName: String(options.providerName), providerType: String(options.providerType),
          providerEmailAttribute: String(options.providerEmailAttribute), refreshTokenValidityHours: options.refreshTokenValidityHours as number | undefined,
          oidcClientId: options.oidcClientId as string | undefined, oidcClientSecret: options.oidcClientSecret as string | undefined,
          oidcIssuer: options.oidcIssuer as string | undefined, oidcAttributesRequestMethod: options.oidcAttributesRequestMethod as string | undefined,
          oidcAuthorizeScopes: options.oidcAuthorizeScopes as string | undefined, oidcAuthorizeUrl: options.oidcAuthorizeUrl as string | undefined,
          oidcTokenUrl: options.oidcTokenUrl as string | undefined, oidcAttributesUrl: options.oidcAttributesUrl as string | undefined,
          oidcJwksUri: options.oidcJwksUri as string | undefined, samlMetadataUrl: options.samlMetadataUrl as string | undefined,
          samlMetadataFile: options.samlMetadataFile as string | undefined,
        });
      } catch (error) {
        if (error instanceof InvalidSsoParams) {
          actionDeps.out(error.message);
          return;
        }
        throw error;
      }
    });
  return group;
}
