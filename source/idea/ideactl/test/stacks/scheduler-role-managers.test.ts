/**
 * The scheduler role script's batch-manager reconciliation.
 *
 * No bastion gets a grant: bastions run only on ECS, an administrator runs qmgr from inside the
 * scheduler task, and a grant by an EC2 name or a task's address goes stale, then reaches
 * whichever desktop is given the address next. Grants of that shape from the host era are removed
 * on every start, even when a host-era bastion name is still in settings; operator-added entries
 * stay.
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

/** The runtime manager reconciliation, independent of first-create defaults. */
function managerBlock(): string {
  const start = SOURCE.indexOf("# The NLB name");
  assert.notEqual(start, -1, "the role script reconciles batch managers");
  const end = SOURCE.indexOf('log "starting pbs_sched"', start);
  assert.notEqual(end, -1, "the reconciliation ends before pbs_sched starts");
  return SOURCE.slice(start, end);
}

/** Runs the reconciliation with a settings table holding `rows`, and returns the batch client's arguments. */
function reconcile(rows: Record<string, string>, existing = ""): string[] {
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
      managerBlock().replaceAll("/opt/pbs/bin/qmgr", "qmgr"),
      "",
    ].join("\n"),
    "utf8",
  );
  writeFileSync(record, "", "utf8");
  execFileSync("bash", [harness], { encoding: "utf8" });
  return readFileSync(record, "utf8").split("\n").filter((line) => line !== "");
}

const STALE = ["root", "ip-10-7-0-9.us-east-2.compute.internal"].join("@");

test("prunes a leftover host-era bastion name instead of granting it again", () => {
  const leftover = "root@ip-192-0-2-10.us-east-2.compute.internal";
  const calls = reconcile({ "bastion-host.private_dns_name": leftover.slice(5) }, leftover);
  assert.deepEqual(calls, [`-c set server managers -= ${leftover}`]);
});

test("grants nothing on a cluster with no bastion module", () => {
  assert.deepEqual(reconcile({}), []);
});

test("ignores the private-zone alias, which resolves but never matches a connection", () => {
  const calls = reconcile({ "bastion-host.hostname": "bastion-host.idea-test1.us-east-2.local" });
  assert.deepEqual(calls, [], "an entry built from bastion-host.hostname would be a grant that never matches");
});

test("grants a container bastion nothing and removes the host-era grant", () => {
  assert.deepEqual(reconcile({
    "bastion-host.service_name": "test-bastion-host",
    "bastion-host.private_dns_name": "nlb.elb.amazonaws.com",
  }, `${STALE},root@example.invalid`), [`-c set server managers -= ${STALE}`]);
});

test("keeps an operator-added grant", () => {
  assert.deepEqual(reconcile({ "bastion-host.private_dns_name": "example.invalid" }, "root@example.invalid"), []);
});

test("reconciles managers after first-create defaults have finished", () => {
  const reconciliation = SOURCE.indexOf("# The NLB name");
  const marker = SOURCE.indexOf('log "PBS server configured"');
  assert.ok(marker >= 0 && marker < reconciliation);
  assert.ok(SOURCE.slice(marker, reconciliation).includes("\nfi\n"));
});
