#!/usr/bin/env node

/**
 * Does the running batch server use the name the settings table says it uses?
 *
 * `scheduler.use_stable_server_name` being true is not the same fact as the running server
 * answering under the stable name. Nothing re-renders `/etc/pbs.conf` on a host that already
 * exists, and until now nothing checked it: the development cluster carries the row set to
 * true while its running server reports the instance hostname, in its configuration and in
 * its own reply. That matters at the cutover, because execution nodes take the server's name
 * from their own configuration, and a node holding a name the new server does not answer to
 * cannot find its server. The job it is running is then what gets requeued.
 *
 * The companion check, scheduler-stable-name, asks the same question of the client hosts.
 * This one asks it of the server, which is the half that had no reader.
 *
 * Both the host bootstrap and the container role set `PBS_SERVER` to the first label of
 * `scheduler.private_dns_name` (`configure_openpbs_server.jinja2`, `roles/scheduler.sh`), and
 * both put the full name and that label in `/etc/hosts`, so either form is accepted and the
 * evidence records which was seen.
 */

import { isIP } from "node:net";

import {
  createSshRunner,
  optionalFlag,
  parseValueFlags,
  requiredFlag,
  type RemoteRunner,
  type SshOptions,
} from "./scheduler-proving.ts";

/** What the scheduler reports about its own name. */
export interface SchedulerServerEvidence {
  /** The address `/etc/hosts` pins the configured name to, empty when it pins none. */
  hostsPin: string;
  /** `PBS_SERVER` from `/etc/pbs.conf`. */
  pbsServer: string;
  /** The name the batch server answers under, from `qstat -B`. */
  reportedServer: string;
  /** The address the configured name resolves to, empty when it does not resolve. */
  resolvedIpv4: string;
  /** `sched_host` from `qmgr print sched`. Evidence only: PBS sets it from the local host. */
  schedHost: string;
}

/** A completed inspection of the server's own name. */
export interface SchedulerServerNameResult {
  observed: string[];
  passed: boolean;
}

/** A configured name is accepted in full or as its first label. */
function acceptedNames(configuredServerName: string): string[] {
  const label = configuredServerName.split(".")[0] ?? configuredServerName;
  return label === configuredServerName ? [configuredServerName] : [configuredServerName, label];
}

/**
 * Reasons the running batch server does not use the configured name. Every reason names both
 * values, because "the row is on" and "the server uses it" are the two facts being compared
 * and a refusal that prints one of them cannot be acted on.
 */
export function schedulerServerNameFailures(
  configuredServerName: string,
  evidence: SchedulerServerEvidence,
): string[] {
  const failures: string[] = [];
  const accepted = acceptedNames(configuredServerName);
  if (evidence.pbsServer === "") {
    failures.push(
      `/etc/pbs.conf on the scheduler reports no PBS_SERVER, and the settings row says ${configuredServerName}`,
    );
  } else if (!accepted.includes(evidence.pbsServer)) {
    failures.push(
      `the settings row says the batch server is ${configuredServerName} and /etc/pbs.conf says PBS_SERVER=${evidence.pbsServer}. ` +
        "Nothing re-renders an existing host, so the row was turned on after this host was built",
    );
  }
  if (evidence.reportedServer === "") {
    failures.push("the batch server did not answer qstat -B, so the name it uses cannot be read");
  } else if (!accepted.includes(evidence.reportedServer)) {
    failures.push(
      `the settings row says the batch server is ${configuredServerName} and the running server reports ${evidence.reportedServer}`,
    );
  }
  if (isIP(evidence.resolvedIpv4) !== 4) {
    failures.push(`${configuredServerName} does not resolve to an IPv4 address from the scheduler`);
  }
  return failures;
}

/** The remote probe. Reads only: no qmgr set, no file is written. */
export function serverProbeCommand(configuredServerName: string): string {
  if (!/^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$/.test(configuredServerName)) {
    throw new Error("the configured server name must be a host name");
  }
  return [
    "set -u",
    'pbs_server=$(awk -F= \'$1 == "PBS_SERVER" { print $2; exit }\' /etc/pbs.conf 2>/dev/null || true)',
    "reported=$(/opt/pbs/bin/qstat -B -f 2>/dev/null | awk '/^Server:/ { print $2; exit }')",
    "sched_host=$(/opt/pbs/bin/qmgr -c \"print sched\" 2>/dev/null | awk '/sched_host/ { print $NF; exit }')",
    `resolved=$(getent ahostsv4 ${configuredServerName} 2>/dev/null | awk 'NR == 1 { print $1 }')`,
    `hosts_pin=$(awk -v name=${configuredServerName} '$1 !~ /^#/ { for (field = 2; field <= NF; field += 1) if ($field == name) pin = $1 } END { print pin }' /etc/hosts 2>/dev/null || true)`,
    "printf 'PBS_SERVER=%s\\nREPORTED=%s\\nSCHED_HOST=%s\\nRESOLVED_IPV4=%s\\nHOSTS_PIN=%s\\n' \"$pbs_server\" \"$reported\" \"$sched_host\" \"$resolved\" \"$hosts_pin\"",
  ].join("; ");
}

/** Parses the labelled probe output. Absent labels read as empty, never as a pass. */
export function parseServerEvidence(stdout: string): SchedulerServerEvidence {
  const fields = new Map<string, string>();
  for (const line of stdout.split(/\r?\n/)) {
    const separator = line.indexOf("=");
    if (separator <= 0) continue;
    fields.set(line.slice(0, separator), line.slice(separator + 1).trim());
  }
  return {
    hostsPin: fields.get("HOSTS_PIN") ?? "",
    pbsServer: fields.get("PBS_SERVER") ?? "",
    reportedServer: fields.get("REPORTED") ?? "",
    resolvedIpv4: fields.get("RESOLVED_IPV4") ?? "",
    schedHost: fields.get("SCHED_HOST") ?? "",
  };
}

/** Runs the probe on the scheduler and compares what it reports with the configured name. */
export async function runSchedulerServerNameCheck(
  options: { configuredServerName: string; schedulerHost: string },
  remote: RemoteRunner,
): Promise<SchedulerServerNameResult> {
  const observed = [`configured_server=${options.configuredServerName}`];
  let result;
  try {
    result = await remote.run(options.schedulerHost, serverProbeCommand(options.configuredServerName));
  } catch (error: unknown) {
    return { observed: [...observed, `remote_error=${oneLine(error instanceof Error ? error.message : String(error))}`], passed: false };
  }
  if (result.exitCode !== 0) {
    return { observed: [...observed, `probe_exit=${result.exitCode}`, `stderr=${oneLine(result.stderr)}`], passed: false };
  }
  const evidence = parseServerEvidence(result.stdout);
  observed.push(
    `pbs_server=${evidence.pbsServer || "missing"}`,
    `reported_server=${evidence.reportedServer || "missing"}`,
    `sched_host=${evidence.schedHost || "missing"}`,
    `resolved_ipv4=${evidence.resolvedIpv4 || "missing"}`,
    `hosts_pin=${evidence.hostsPin || "none"}`,
  );
  const failures = schedulerServerNameFailures(options.configuredServerName, evidence);
  return { observed: [...observed, ...failures], passed: failures.length === 0 };
}

/** Parses the standalone check's flags. */
export function parseSchedulerServerNameOptions(argv: readonly string[]): {
  check: { configuredServerName: string; schedulerHost: string };
  ssh: SshOptions;
} {
  const values = parseValueFlags(
    argv,
    new Set(["configured-server", "scheduler-host", "ssh-identity-file", "ssh-option", "ssh-port", "ssh-user"]),
    new Set(["ssh-option"]),
  );
  return {
    check: {
      configuredServerName: requiredFlag(values, "configured-server"),
      schedulerHost: requiredFlag(values, "scheduler-host"),
    },
    ssh: {
      identityFile: optionalFlag(values, "ssh-identity-file"),
      options: (values.get("ssh-option") ?? []).map(parseSshOption),
      port: optionalPort(optionalFlag(values, "ssh-port")),
      user: requiredFlag(values, "ssh-user"),
    },
  };
}

function parseSshOption(value: string): string {
  if (!/^[A-Za-z][A-Za-z0-9]*=.+$/.test(value) || /[\r\n]/.test(value)) {
    throw new Error("--ssh-option must use Name=Value");
  }
  return value;
}

function optionalPort(value: string | undefined): number | undefined {
  if (value === undefined) return undefined;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65_535) {
    throw new Error("--ssh-port must be an integer from 1 through 65535");
  }
  return parsed;
}

function oneLine(value: string): string {
  return value.replaceAll(/\s+/g, " ").trim();
}

async function main(): Promise<void> {
  try {
    const { check, ssh } = parseSchedulerServerNameOptions(process.argv.slice(2));
    const result = await runSchedulerServerNameCheck(check, createSshRunner(ssh));
    for (const observation of result.observed) {
      console.log(`OBSERVED ${observation}`);
    }
    console.log(`${result.passed ? "PASS" : "FAIL"} scheduler-server-name`);
    process.exitCode = result.passed ? 0 : 1;
  } catch (error: unknown) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 2;
  }
}

if (import.meta.main) {
  await main();
}
