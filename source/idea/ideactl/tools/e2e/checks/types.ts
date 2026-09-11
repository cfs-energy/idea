import type { JsonObject, JsonValue } from "../api.ts";

export const CHECK_NAMES = [
  "desktop-end-to-end",
  "gateway-task-kill",
  "broker-task-kill",
  "scheduler-replacement",
  "job-burst",
  "api-load",
  "gateway-load",
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

export interface GatewayConnector {
  connect(host: string, port: number, insecureTls: boolean): Promise<GatewayConnection>;
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
  albHost?: string;
  apiMaxErrorCount?: number;
  apiMaxP95Ms?: number;
  apiRps?: number;
  apiSeconds?: number;
  apiWorkers?: number;
  brokerService?: string;
  brokerTargetGroup?: string;
  brokerTask?: string;
  checks: CheckName[];
  cluster?: string;
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
  username?: string;
}

export interface CheckContext {
  api: ApiClient;
  cloud: CloudClient;
  gateway: GatewayConnector;
  now(): number;
  options: ProofMatrixOptions;
  output(line: string): void;
  processes: ProcessRunner;
  sleep(milliseconds: number): Promise<void>;
}

export interface CheckResult {
  observed: string[];
  passed: boolean;
}

export interface ProofCheck {
  description: string;
  name: CheckName;
  requiredFlags(options: ProofMatrixOptions): string[];
  run(context: CheckContext): Promise<CheckResult>;
}
