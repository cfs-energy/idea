import type { JsonObject, JsonValue } from "../api.ts";

export const CHECK_NAMES = [
  "desktop-end-to-end",
  "desktop-ssh",
  "desktop-stream",
  "gateway-task-kill",
  "broker-task-kill",
  "scheduler-replacement",
  "scheduler-image-upgrade",
  "job-burst",
  "api-load",
  "gateway-load",
  "metrics-sink",
  "account-reconcile",
] as const;

export type CheckName = (typeof CHECK_NAMES)[number];

export interface ApiClient {
  request(namespace: string, payload: JsonValue): Promise<{ body: JsonValue; status: number }>;
}

export interface CloudClient {
  healthyTargetCount(targetGroup: string): Promise<number>;
  serviceRunningCount(cluster: string, service: string): Promise<{ desired: number; running: number }>;
  stopTask(cluster: string, task: string): Promise<void>;
}

export interface GatewayConnection {
  close(): void;
  isOpen(): boolean;
}

/** What the gateway answered a DCV connection request with, and how long it took. */
export interface DcvSessionOutcome {
  elapsedMs: number;
  reply: import("../dcv-setup.ts").DcvServerReply;
}

export interface GatewayConnector {
  connect(host: string, port: number, insecureTls: boolean): Promise<GatewayConnection>;
  /**
   * Opens a DCV web session the way the web client does: the `/ws` WebSocket with the `dcv`
   * subprotocol, then the connection request. Resolves with the server's first reply; rejects
   * when the socket closes or nothing arrives in time.
   */
  openSession?(input: { url: string; sessionId: string; authenticationToken: string; timeoutMs: number }): Promise<DcvSessionOutcome>;
}

export interface ProcessResult {
  exitCode: number;
  stderr: string;
  stdout: string;
}

export interface ProcessRunner {
  run(command: string, args: string[]): Promise<ProcessResult>;
}

export interface ProofMatrixOptions {
  ldapUri?: string;
  ldapBindDn?: string;
  ldapPasswordFile?: string;
  ldapUserBase?: string;
  albHost?: string;
  apiMaxErrorCount?: number;
  apiMaxP95Ms?: number;
  apiRps?: number;
  apiSeconds?: number;
  apiWorkers?: number;
  bastionHost?: string;
  brokerService?: string;
  brokerTargetGroup?: string;
  brokerTask?: string;
  checks: CheckName[];
  cluster?: string;
  datadogApiKey?: string;
  datadogAppKey?: string;
  datadogSite?: string;
  desktopRequest?: JsonObject;
  expectedExitStatus?: number;
  gatewayConnections?: number;
  gatewayHoldSeconds?: number;
  gatewayHost?: string;
  gatewayMaxFailures?: number;
  gatewayMaxP95Ms?: number;
  gatewayPort?: number;
  gatewayRampSeconds?: number;
  gatewayService?: string;
  gatewayTargetGroup?: string;
  gatewayTask?: string;
  insecureTls: boolean;
  jobCount?: number;
  jobSleepSeconds?: number;
  passwordFile?: string;
  pollSeconds?: number;
  readyTimeoutSeconds?: number;
  region?: string;
  replacementTimeoutSeconds?: number;
  schedulerService?: string;
  schedulerTask?: string;
  tokenDirectory?: string;
  upgradeCommand?: string;
  username?: string;
}

export interface CheckContext {
  api: ApiClient;
  fetch?: typeof fetch;
  cloud: CloudClient;
  gateway: GatewayConnector;
  now(): number;
  options: ProofMatrixOptions;
  output(line: string): void;
  processes: ProcessRunner;
  sleep(milliseconds: number): Promise<void>;
  /** One fresh connection to a neutral host outside the cluster; defaults to the live control URL. */
  control?(): Promise<true | string>;
}

export interface CheckResult {
  skipped?: boolean;
  observed: string[];
  passed: boolean;
}

export interface ProofCheck {
  description: string;
  name: CheckName;
  requiredFlags(options: ProofMatrixOptions): string[];
  run(context: CheckContext): Promise<CheckResult>;
}
