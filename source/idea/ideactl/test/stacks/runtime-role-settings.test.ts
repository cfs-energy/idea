import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

const images = resolve(import.meta.dirname, "../../../../../deployment/ecr");
for (const script of ["idea-control-plane/roles/scheduler.sh"]) {
  for (const state of ["fresh", "existing", "marked", "habitat-failure"]) {
    test(`${script}: ${state}`, () => {
      const work = mkdtempSync(join(tmpdir(), "scheduler-settings-"));
      try {
        const home = join(work, "pbs");
        const record = join(work, "calls");
        const files = ["server_priv/resourcedef", "sched_priv/sched_config", "pbs_environment"];
        writeFileSync(record, "");
        if (state === "existing" || state === "marked") {
          for (const dir of ["datastore/base", "server_priv", "sched_priv"]) mkdirSync(join(home, dir), { recursive: true });
          writeFileSync(join(home, "datastore/base/server"), "existing database");
          for (const file of files) writeFileSync(join(home, file), "operator configuration\n");
          if (state === "marked") writeFileSync(join(home, ".idea-server-configured"), "existing marker");
        }
        const source = readFileSync(join(images, script), "utf8");
        const start = source.indexOf('if [[ ! -d "${PBS_HOME}/datastore" ]]');
        const end = source.indexOf("\nshutdown()");
        assert.ok(start >= 0 && end > start);
        // Isolate startup configuration from host paths and installed daemons.
        // The real shell branches still decide whether stored policy is changed.
        const body = source.slice(start, end).replaceAll(/\/opt\/pbs\/(?:libexec|bin|sbin)\//g, "");
        const result = spawnSync("bash", ["-c", `set -euo pipefail
log() { :; }
install() { :; }
aws() { echo None; }
pbs_habitat() {
  echo habitat >> "$RECORD"
  if [[ "$FAIL_HABITAT" == 1 ]]; then return 1; fi
  mkdir -p "$PBS_HOME/datastore" "$PBS_HOME/server_priv" "$PBS_HOME/sched_priv"
  echo compute_node > "$PBS_HOME/sched_priv/sched_config"
}
pbs_init.d() { echo daemons >> "$RECORD"; }
qstat() { return 0; }
qmgr() { echo "$*" >> "$RECORD"; }
pbs_sched() { :; }
${body}`], {
          encoding: "utf8", timeout: 10_000,
          env: { ...process.env, PBS_HOME: home, RECORD: record, FAIL_HABITAT: state === "habitat-failure" ? "1" : "0",
            IDEA_SCHEDULER_DNS_NAME: "scheduler.example.invalid", IDEA_CLUSTER_NAME: "sample-cluster",
            AWS_DEFAULT_REGION: "us-east-2", IDEA_APP_DEPLOY_DIR: work, IDEA_ROUTE53_ZONE_ID: "" },
        });
        const calls = readFileSync(record, "utf8");
        if (state === "habitat-failure") {
          assert.notEqual(result.status, 0);
          assert.equal(calls, "habitat\n");
          return;
        }
        assert.equal(result.status, 0, result.stderr);
        if (state === "fresh") {
          assert.match(calls, /set queue normal started = True/);
          assert.match(calls, /set queue normal enabled = True/);
          assert.ok(existsSync(join(home, ".idea-server-configured")));
          assert.match(readFileSync(join(home, files[0]!), "utf8"), /compute_node/);
        } else {
          assert.equal(calls, "daemons\n");
          for (const file of files) assert.equal(readFileSync(join(home, file), "utf8"), "operator configuration\n");
          assert.equal(existsSync(join(home, ".idea-server-configured")), state === "marked");
        }
      } finally { rmSync(work, { recursive: true, force: true }); }
    });
  }
}
for (const moduleId of ["vdc", "desktop-custom"]) {
  for (const mode of ["configured", "absent", "denied", "unavailable"]) {
    test(`broker ${moduleId}: ${mode}`, () => {
      const work = mkdtempSync(join(tmpdir(), "broker-settings-"));
      try {
        const stub = (name: string, body: string) => writeFileSync(join(work, name), `#!/bin/bash\n${body}\n`, { mode: 0o755 });
        const record = join(work, "calls");
        const conf = join(work, "broker.properties");
        const suffixes = ["client_communication_port", "agent_communication_port", "gateway_communication_port", "session_token_validity", "dynamodb_table.read_capacity.min_units", "dynamodb_table.write_capacity.min_units"];
        const configured = [9444, 9445, 9446, 60, 11, 12];
        stub("aws", `echo "$*" >> "$RECORD"
if [[ "$MODE" == denied ]]; then echo AccessDeniedException >&2; exit 23; fi
if [[ "$MODE" == unavailable ]]; then echo ServiceUnavailable >&2; exit 24; fi
if [[ "$MODE" == absent ]]; then echo None; exit; fi
case "$*" in
${suffixes.map((suffix, index) => `  *'"${moduleId}.dcv_broker.${suffix}"'*) echo ${configured[index]};;`).join("\n")}
  *) echo UnexpectedKey >&2; exit 25;;
esac`);
        stub("chown", "exit 0");
        stub("dcv-session-manager-broker", 'echo registered >> "$RECORD"');
        stub("setpriv", 'echo launched >> "$RECORD"');
        const result = spawnSync("bash", [join(images, "idea-control-plane/roles/broker.sh")], {
          encoding: "utf8", timeout: 10_000,
          env: { ...process.env, PATH: `${work}:${process.env.PATH}`, RECORD: record, MODE: mode,
            IDEA_CLUSTER_NAME: "sample-cluster", IDEA_MODULE_ID: moduleId, AWS_DEFAULT_REGION: "us-east-2",
            IDEA_BROKER_DISCOVERY_ADDRESSES: "broker.example.invalid:47500", IDEA_COGNITO_PROVIDER_URL: "https://example.invalid",
            IDEA_BROKER_CONF_FILE: conf },
        });
        const calls = readFileSync(record, "utf8");
        assert.doesNotMatch(calls, /virtual-desktop-controller/);
        if (mode === "denied" || mode === "unavailable") {
          assert.equal(result.status, mode === "denied" ? 23 : 24);
          assert.match(result.stderr, /AccessDeniedException|ServiceUnavailable/);
          assert.ok(!existsSync(conf));
          assert.doesNotMatch(calls, /registered|launched/);
        } else {
          assert.equal(result.status, 0, result.stderr);
          const text = readFileSync(conf, "utf8");
          const values = mode === "configured" ? configured : [8444, 8445, 8446, 1440, 5, 5];
          const keys = ["client-to-broker-connector-https-port", "agent-to-broker-connector-https-port", "gateway-to-broker-connector-https-port", "connect-session-token-duration-minutes", "dynamodb-table-rcu", "dynamodb-table-wcu"];
          keys.forEach((key, index) => assert.ok(text.includes(`${key} = ${values[index]}\n`), key));
          assert.match(calls, /launched/);
        }
      } finally { rmSync(work, { recursive: true, force: true }); }
    });
  }
}
