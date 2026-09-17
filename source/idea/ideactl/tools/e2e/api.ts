#!/usr/bin/env node

import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export type JsonPrimitive = boolean | null | number | string;
export type JsonValue = JsonObject | JsonPrimitive | JsonValue[];

export interface JsonObject {
  [key: string]: JsonValue;
}

export interface IdeaApiClientOptions {
  albHost: string;
  username: string;
  passwordFile: string;
  tokenDirectory?: string;
  insecureTls?: boolean;
  fetchImpl?: typeof fetch;
}

export interface ApiCallResult {
  status: number;
  body: JsonValue;
}

interface RequestEnvelope {
  header: {
    namespace: string;
    request_id: string;
  };
  payload: JsonValue;
}

/** Maps an IDEA API namespace to its externally exposed endpoint. */
export function endpointPath(namespace: string): string {
  if (namespace.startsWith("Scheduler")) {
    return "/scheduler/api/v1";
  }
  if (namespace.startsWith("VirtualDesktop")) {
    return "/vdc/api/v1";
  }
  return "/cluster-manager/api/v1";
}

/** Creates the request object used by every control-plane API call. */
export function requestEnvelope(namespace: string, payload: JsonValue, requestId: string = randomUUID()): RequestEnvelope {
  return {
    header: {
      namespace,
      request_id: requestId,
    },
    payload,
  };
}

/** Returns true when an API response reports that its bearer token is unusable. */
export function isUnauthorizedResponse(body: JsonValue): boolean {
  const bodyObject = asJsonObject(body);
  if (bodyObject === undefined) {
    return false;
  }

  return [bodyObject.error_code, bodyObject.message]
    .filter((value): value is string => typeof value === "string")
    .some((value) => /UNAUTHORIZED_ACCESS|TOKEN_EXPIRED|Unauthorized/.test(value));
}

/** IDEA API client with a password-grant token cache isolated by username. */
export class IdeaApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly options: IdeaApiClientOptions;
  private readonly tokenFile: string;

  public constructor(options: IdeaApiClientOptions) {
    if (options.albHost.trim() === "") {
      throw new Error("albHost must not be empty");
    }
    if (options.username.trim() === "") {
      throw new Error("username must not be empty");
    }
    if (options.passwordFile.trim() === "") {
      throw new Error("passwordFile must not be empty");
    }

    this.baseUrl = normalizeBaseUrl(options.albHost);
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.options = options;
    this.tokenFile = join(
      options.tokenDirectory ?? join(homedir(), ".ideactl", "e2e", "tokens"),
      `${encodeURIComponent(options.username)}.token`,
    );

    // Node's native fetch consults this process setting during TLS setup. It is
    // changed only when the caller explicitly opts into insecure test TLS.
    if (options.insecureTls === true) {
      process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
    }
  }

  /** Sends one namespace request, refreshing and retrying once for an expired token. */
  public async request(namespace: string, payload: JsonValue): Promise<ApiCallResult> {
    const envelope = requestEnvelope(namespace, payload);
    let token = this.readCachedToken() ?? (await this.initiateAuth());
    let result = await this.post(endpointPath(namespace), envelope, token);

    if (isUnauthorizedResponse(result.body)) {
      this.removeCachedToken();
      token = await this.initiateAuth();
      result = await this.post(endpointPath(namespace), envelope, token);
    }

    return result;
  }

  private async initiateAuth(): Promise<string> {
    const password = readFileSync(this.options.passwordFile, "utf8").trim();
    if (password === "") {
      throw new Error(`password file is empty: ${this.options.passwordFile}`);
    }

    const result = await this.post(
      endpointPath("Auth.InitiateAuth"),
      requestEnvelope("Auth.InitiateAuth", {
        auth_flow: "USER_PASSWORD_AUTH",
        username: this.options.username,
        password,
      }),
    );
    const token = accessToken(result.body);
    if (result.status !== 200 || token === undefined) {
      throw new Error(`AUTH_FAILED: ${JSON.stringify(result.body)}`);
    }

    mkdirSync(dirname(this.tokenFile), { recursive: true });
    writeFileSync(this.tokenFile, `${token}\n`, { encoding: "utf8", mode: 0o600 });
    return token;
  }

  private readCachedToken(): string | undefined {
    try {
      const token = readFileSync(this.tokenFile, "utf8").trim();
      return token === "" ? undefined : token;
    } catch (error: unknown) {
      if (isCode(error, "ENOENT")) {
        return undefined;
      }
      throw error;
    }
  }

  private removeCachedToken(): void {
    try {
      unlinkSync(this.tokenFile);
    } catch (error: unknown) {
      if (!isCode(error, "ENOENT")) {
        throw error;
      }
    }
  }

  private async post(path: string, body: RequestEnvelope, token?: string): Promise<ApiCallResult> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token !== undefined) {
      headers.Authorization = `Bearer ${token}`;
    }

    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });

    return {
      status: response.status,
      body: parseJson(await response.text()),
    };
  }
}

function normalizeBaseUrl(albHost: string): string {
  const supplied = albHost.trim().replace(/\/+$/, "");
  return /^https?:\/\//.test(supplied) ? supplied : `https://${supplied}`;
}

function asJsonObject(value: JsonValue): JsonObject | undefined {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value;
  }
  return undefined;
}

function parseJson(text: string): JsonValue {
  const parsed: unknown = JSON.parse(text);
  if (!isJsonValue(parsed)) {
    throw new Error("API response was not JSON data");
  }
  return parsed;
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return true;
  }
  if (Array.isArray(value)) {
    return value.every(isJsonValue);
  }
  if (typeof value === "object") {
    return Object.values(value).every(isJsonValue);
  }
  return false;
}

function accessToken(body: JsonValue): string | undefined {
  const payload = asJsonObject(body)?.payload;
  const auth = payload === undefined ? undefined : asJsonObject(payload)?.auth;
  const token = auth === undefined ? undefined : asJsonObject(auth)?.access_token;
  return typeof token === "string" ? token : undefined;
}

function isCode(error: unknown, code: string): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === code;
}

function usage(): string {
  return [
    "Usage: api.ts --alb-host <host> --username <user> --password-file <path> --namespace <name> --payload <json> [options]",
    "",
    "Options:",
    "  --token-dir <path>  Directory for one cached token per user",
    "  --insecure          Disable TLS certificate verification for this process",
  ].join("\n");
}

function requiredFlag(flags: Map<string, string | boolean>, name: string): string {
  const value = flags.get(name);
  if (typeof value !== "string" || value === "") {
    throw new Error(`missing required --${name}`);
  }
  return value;
}

function parseFlags(argv: string[]): Map<string, string | boolean> {
  const flags = new Map<string, string | boolean>();
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) {
      throw new Error(`unexpected argument: ${argument}`);
    }
    const name = argument.slice(2);
    if (name === "insecure" || name === "help") {
      flags.set(name, true);
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`missing value for --${name}`);
    }
    flags.set(name, value);
    index += 1;
  }
  return flags;
}

async function main(): Promise<void> {
  const flags = parseFlags(process.argv.slice(2));
  if (flags.get("help") === true) {
    console.log(usage());
    return;
  }

  const payloadText = requiredFlag(flags, "payload");
  const parsed: unknown = JSON.parse(payloadText);
  if (!isJsonValue(parsed)) {
    throw new Error("--payload must be JSON data");
  }

  const tokenDirectory = flags.get("token-dir");
  const client = new IdeaApiClient({
    albHost: requiredFlag(flags, "alb-host"),
    username: requiredFlag(flags, "username"),
    passwordFile: requiredFlag(flags, "password-file"),
    tokenDirectory: typeof tokenDirectory === "string" ? tokenDirectory : undefined,
    insecureTls: flags.get("insecure") === true,
  });
  const result = await client.request(requiredFlag(flags, "namespace"), parsed);
  console.log(JSON.stringify(result.body));
}

if (import.meta.main) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : String(error));
    console.error(usage());
    process.exitCode = 2;
  });
}
