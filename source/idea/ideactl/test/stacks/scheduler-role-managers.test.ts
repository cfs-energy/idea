/**
 * The scheduler role script's batch-manager grant.
 *
 * A person, not this tool, closes admission before a migration and reopens it afterwards, and
 * after the cutover the batch server's host is a task with no shell into it. The grant puts the
 * bastion host in the server's `managers` list so those `qmgr` commands have an authorised
 * client over the batch protocol.
 *
 * Two things have to hold and neither is visible by reading the line:
 *
 *  - It must name the client's reverse-resolved DNS name. The server matches a connection against the name it
 *    reverse-resolves the caller's address to. The private-zone alias in `bastion-host.hostname`
 *    resolves forward to the same address but canonicalizes to itself, so an entry built from it
 *    is accepted and never matches: a grant that silently does nothing.
 *  - A cluster with no bastion module must get no grant rather than a malformed one.
 *
 * The block is executed rather than pattern-matched, with `setting` and the batch client stubbed.
 * The one rewrite the harness makes is the client's absolute path, which cannot be stubbed on PATH.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { after, test } from "node:test";

const ROLE_SCRIPT = resolve(
  import.meta.dirname,
  "../../../../../deployment/ecr/idea-control-plane/roles/scheduler.sh",
);
const SOURCE = readFileSync(ROLE_SCRIPT, "utf8");
const workdirs: string[] = [];

after(() => {
  for (const workdir of workdirs) rmSync(workdir, { force: true, recursive: true });
});

/** The runtime grant, independent of first-create defaults. */
function grantBlock(): string {
  const start = SOURCE.indexOf("# The NLB name");
  assert.notEqual(start, -1, "the role script grants the bastion host batch-manager rights");
  const end = SOURCE.indexOf('log "starting pbs_sched"', start);
  assert.notEqual(end, -1, "the grant is a closed conditional");
  return SOURCE.slice(start, end);
}

/** Runs the grant with a settings table holding `rows`, and returns the batch client's arguments. */
function runGrant(rows: Record<string, string>, existing = ""): string[] {
  const workdir = mkdtempSync(join(tmpdir(), "ideactl-managers-"));
  workdirs.push(workdir);
  const record = join(workdir, "qmgr-calls");
  const harness = join(workdir, "harness.sh");
  writeFileSync(
    harness,
    [
      "set -euo pipefail",
      "log() { :; }",
      // The real helper returns its second argument when the row is absent.
      "setting() {",
      "  case \"$1\" in",
      ...Object.entries(rows).map(([key, value]) => `    ${key}) printf '%s' '${value}';;`),
      "    *) printf '%s' \"$2\";;",
      "  esac",
      "}",
      `qmgr() { if [[ "$2" == 'list server managers' ]]; then echo '    managers = ${existing}'; else printf '%s\\n' "$*" >> '${record}'; fi; }`,
      "python3.13() { echo ip-192-0-2-20.us-east-2.compute.internal; }",
      grantBlock().replaceAll("/opt/pbs/bin/qmgr", "qmgr"),
      "",
    ].join("\n"),
    "utf8",
  );
  writeFileSync(record, "", "utf8");
  execFileSync("bash", [harness], { encoding: "utf8" });
  return readFileSync(record, "utf8").split("\n").filter((line) => line !== "");
}

test("grants the bastion host batch-manager rights under the name the server matches", () => {
  const calls = runGrant({ "bastion-host.private_dns_name": "ip-192-0-2-10.us-east-2.compute.internal" });
  assert.deepEqual(calls, [
    '-c set server managers += root@ip-192-0-2-10.us-east-2.compute.internal',
  ]);
});

test("grants nothing on a cluster with no bastion module", () => {
  assert.deepEqual(runGrant({}), []);
});

test("ignores the private-zone alias, which resolves but never matches a connection", () => {
  const calls = runGrant({ "bastion-host.hostname": "bastion-host.idea-test1.us-east-2.local" });
  assert.deepEqual(calls, [], "an entry built from bastion-host.hostname would be a grant that never matches");
});

test("grants the container's outbound hostname rather than the NLB", () => {
  assert.deepEqual(runGrant({
    "bastion-host.service_name": "test-bastion-host",
    "bastion-host.private_dns_name": "nlb.elb.amazonaws.com",
    "cluster.network.security_groups.bastion-host": "sg-bastion",
  }), ['-c set server managers += root@ip-192-0-2-20.us-east-2.compute.internal']);
});

test("does not add an existing grant again", () => {
  assert.deepEqual(runGrant({ "bastion-host.private_dns_name": "host.example" }, "root@host.example"), []);
});

test("refreshes grants after first-create defaults have finished", () => {
  const grant = SOURCE.indexOf("# The NLB name");
  const marker = SOURCE.indexOf('log "PBS server configured"');
  assert.ok(marker >= 0 && marker < grant);
  assert.ok(SOURCE.slice(marker, grant).includes("\nfi\n"));
});
