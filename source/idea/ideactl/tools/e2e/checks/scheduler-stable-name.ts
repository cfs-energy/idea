#!/usr/bin/env node

import { isIP } from "node:net";

import {
  createSshRunner,
  optionalFlag,
  parseValueFlags,
  requiredFlag,
  type RemoteRunner,
  type SshOptions,
} from "./scheduler-proving.ts";

/** One host whose OpenPBS client configuration is inspected. */
export interface SchedulerNode {
  host: string;
  name: string;
}

/** Inputs for checking that hosts use the stable scheduler name. */
export interface StableSchedulerNameOptions {
  expectedAddress?: string;
  /** The stable name, `scheduler.private_dns_name`; clients use its first label. */
  expectedServer: string;
  nodes: readonly SchedulerNode[];
}

/** A completed stable-name inspection with machine-readable observations. */
export interface StableSchedulerNameResult {
  observed: string[];
  passed: boolean;
}

interface NodeEvidence {
  hostsPin: string;
  pbsServer: string;
  qstat: string;
  resolvedIpv4: string;
  stableIpv4: string;
}

/**
 * The remote probe. `stableName` is validated as a DNS name before it is put in the command.
 * A pin on either name fails: the short name must reach the server through the search path,
 * the same record the stable name follows across a scheduler replacement.
 */
export function probeCommand(stableName: string): string {
  return [
    "set -u",
    `stable_name=${stableName}`,
    "pbs_server=$(awk -F= '$1 == \"PBS_SERVER\" { print $2; exit }' /etc/pbs.conf 2>/dev/null || true)",
    "hosts_pin=$(awk -v short=\"$pbs_server\" -v full=\"$stable_name\" '$1 !~ /^#/ { for (i = 2; i <= NF; i += 1) if ($i == short || $i == full) pin = $1 } END { print pin }' /etc/hosts 2>/dev/null || true)",
    "resolved_ipv4=$(getent ahostsv4 \"$pbs_server\" 2>/dev/null | awk 'NR == 1 { print $1 }')",
    "stable_ipv4=$(getent ahostsv4 \"$stable_name\" 2>/dev/null | awk 'NR == 1 { print $1 }')",
    "qstat=FAIL",
    "if /opt/pbs/bin/qstat -B >/dev/null 2>&1; then qstat=OK; fi",
    "printf 'PBS_SERVER=%s\\nHOSTS_PIN=%s\\nRESOLVED_IPV4=%s\\nSTABLE_IPV4=%s\\nQSTAT_B=%s\\n' \"$pbs_server\" \"$hosts_pin\" \"$resolved_ipv4\" \"$stable_ipv4\" \"$qstat\"",
  ].join("; ");
}

/** The batch server's own name: the first label of the stable name (roles/scheduler.sh). */
export function serverNameOf(stableName: string): string {
  return stableName.split(".")[0] ?? stableName;
}

/**
 * Inspects scheduler clients and compute nodes for PBS_SERVER equal to the batch server's own
 * name (so a short job id in a dependency completes to the id the server releases), resolved
 * through DNS to the same address as the stable name, no hosts-file pin, and a working
 * scheduler query.
 */
export async function runStableSchedulerNameCheck(
  options: StableSchedulerNameOptions,
  remote: RemoteRunner,
): Promise<StableSchedulerNameResult> {
  if (!/^[A-Za-z0-9][A-Za-z0-9.-]*$/.test(options.expectedServer)) {
    throw new Error("--expected-server must be a DNS name");
  }
  const expectedPbsServer = serverNameOf(options.expectedServer);
  const command = probeCommand(options.expectedServer);
  const observed: string[] = [];
  let passed = true;

  for (const node of options.nodes) {
    let result;
    try {
      result = await remote.run(node.host, command);
    } catch (error: unknown) {
      observed.push(`node=${node.name} remote_error=${errorMessage(error)}`);
      passed = false;
      continue;
    }
    if (result.exitCode !== 0) {
      observed.push(`node=${node.name} ssh_exit=${result.exitCode} stderr=${oneLine(result.stderr)}`);
      passed = false;
      continue;
    }

    const evidence = parseNodeEvidence(result.stdout);
    const nodeObserved = [
      `node=${node.name}`,
      `pbs_server=${evidence.pbsServer || "missing"}`,
      `hosts_pin=${evidence.hostsPin || "none"}`,
      `resolved_ipv4=${evidence.resolvedIpv4 || "missing"}`,
      `stable_ipv4=${evidence.stableIpv4 || "missing"}`,
      `qstat_b=${evidence.qstat || "missing"}`,
    ].join(" ");
    observed.push(nodeObserved);

    if (evidence.pbsServer !== expectedPbsServer) {
      passed = false;
      observed.push(`node=${node.name} expected_pbs_server=${expectedPbsServer}`);
    }
    if (evidence.hostsPin !== "") {
      passed = false;
      observed.push(`node=${node.name} pinned_address=${evidence.hostsPin}`);
    }
    if (isIP(evidence.resolvedIpv4) !== 4) {
      passed = false;
      observed.push(`node=${node.name} pbs_server_did_not_resolve_to_ipv4`);
    }
    if (evidence.resolvedIpv4 !== evidence.stableIpv4) {
      passed = false;
      observed.push(`node=${node.name} pbs_server_and_stable_name_resolve_differently`);
    }
    if (options.expectedAddress !== undefined && evidence.resolvedIpv4 !== options.expectedAddress) {
      passed = false;
      observed.push(`node=${node.name} expected_address=${options.expectedAddress}`);
    }
    if (evidence.qstat !== "OK") {
      passed = false;
      observed.push(`node=${node.name} qstat_b_failed`);
    }
  }
  return { observed, passed };
}

/** Parses the labelled output emitted by the remote probe command. */
export function parseNodeEvidence(stdout: string): NodeEvidence {
  const fields = new Map<string, string>();
  for (const line of stdout.split(/\r?\n/)) {
    const separator = line.indexOf("=");
    if (separator <= 0) {
      continue;
    }
    const key = line.slice(0, separator);
    const value = line.slice(separator + 1).trim();
    if (["PBS_SERVER", "HOSTS_PIN", "RESOLVED_IPV4", "STABLE_IPV4", "QSTAT_B"].includes(key)) {
      fields.set(key, value);
    }
  }
  return {
    hostsPin: fields.get("HOSTS_PIN") ?? "",
    pbsServer: fields.get("PBS_SERVER") ?? "",
    qstat: fields.get("QSTAT_B") ?? "",
    resolvedIpv4: fields.get("RESOLVED_IPV4") ?? "",
    stableIpv4: fields.get("STABLE_IPV4") ?? "",
  };
}

/** Parses the standalone check's flags into SSH and check options. */
export function parseStableSchedulerNameOptions(argv: readonly string[]): {
  check: StableSchedulerNameOptions;
  ssh: SshOptions;
} {
  const values = parseValueFlags(
    argv,
    new Set([
      "expected-address",
      "expected-server",
      "node",
      "ssh-identity-file",
      "ssh-option",
      "ssh-port",
      "ssh-user",
    ]),
    new Set(["node", "ssh-option"]),
  );
  const expectedServer = requiredFlag(values, "expected-server");
  const nodes = (values.get("node") ?? []).map(parseNode);
  if (nodes.length === 0) {
    throw new Error("supply at least one --node <label=host>");
  }
  const expectedAddress = optionalFlag(values, "expected-address");
  if (expectedAddress !== undefined && isIP(expectedAddress) !== 4) {
    throw new Error("--expected-address must be an IPv4 address");
  }
  return {
    check: { expectedAddress, expectedServer, nodes },
    ssh: {
      identityFile: optionalFlag(values, "ssh-identity-file"),
      options: (values.get("ssh-option") ?? []).map(parseSshOption),
      port: optionalPort(optionalFlag(values, "ssh-port")),
      user: requiredFlag(values, "ssh-user"),
    },
  };
}

/** Converts the `label=host` node flag into an unambiguous observation label. */
function parseNode(value: string): SchedulerNode {
  const separator = value.indexOf("=");
  if (separator <= 0 || separator === value.length - 1) {
    throw new Error("--node must use label=host");
  }
  const name = value.slice(0, separator);
  const host = value.slice(separator + 1);
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(name) || /[\r\n]/.test(host)) {
    throw new Error("--node contains an invalid label or host");
  }
  return { host, name };
}

/** Validates an SSH `-o Name=Value` argument before it reaches the child process. */
function parseSshOption(value: string): string {
  if (!/^[A-Za-z][A-Za-z0-9]*=.+$/.test(value) || /[\r\n]/.test(value)) {
    throw new Error("--ssh-option must use Name=Value");
  }
  return value;
}

/** Validates an optional TCP port. */
function optionalPort(value: string | undefined): number | undefined {
  if (value === undefined) {
    return undefined;
  }
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65_535) {
    throw new Error("--ssh-port must be an integer from 1 through 65535");
  }
  return parsed;
}

/** Makes command errors safe for a single-line evidence record. */
function errorMessage(error: unknown): string {
  return error instanceof Error ? oneLine(error.message) : oneLine(String(error));
}

/** Removes line breaks from child-process diagnostics. */
function oneLine(value: string): string {
  return value.replaceAll(/\s+/g, " ").trim();
}

async function main(): Promise<void> {
  try {
    const { check, ssh } = parseStableSchedulerNameOptions(process.argv.slice(2));
    const result = await runStableSchedulerNameCheck(check, createSshRunner(ssh));
    for (const observation of result.observed) {
      console.log(`OBSERVED ${observation}`);
    }
    console.log(`${result.passed ? "PASS" : "FAIL"} scheduler-stable-name`);
    process.exitCode = result.passed ? 0 : 1;
  } catch (error: unknown) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 2;
  }
}

if (import.meta.main) {
  await main();
}
