import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

const images = resolve(import.meta.dirname, "../../../../../deployment/ecr");
for (const script of ["idea-control-plane/roles/scheduler.sh"]) {
  for (const state of ["fresh", "existing", "marked", "interrupted", "interrupted-in-habitat", "habitat-failure"]) {
    test(`${script}: ${state}`, () => {
      const work = mkdtempSync(join(tmpdir(), "scheduler-settings-"));
      try {
        const home = join(work, "pbs");
        const record = join(work, "calls");
        const files = ["server_priv/resourcedef", "sched_priv/sched_config", "pbs_environment"];
        writeFileSync(record, "");
        const hooks = join(work, "scheduler/resources/openpbs/hooks");
        mkdirSync(hooks, { recursive: true });
        for (const hook of ["openpbs_hook_handler.py", "calculate_ncpus_hook.py"]) writeFileSync(join(hooks, hook), "# hook\n");
        if (state === "existing" || state === "marked") {
          for (const dir of ["datastore/base", "server_priv", "sched_priv"]) mkdirSync(join(home, dir), { recursive: true });
          writeFileSync(join(home, "datastore/base/server"), "existing database");
          for (const file of files) writeFileSync(join(home, file), "operator configuration\n");
          if (state === "marked") writeFileSync(join(home, ".idea-server-configured"), "existing marker");
        }
        if (state === "interrupted" || state === "interrupted-in-habitat") {
          // a first start that stopped after pbs_habitat (or inside it), before the server was configured
          for (const dir of ["datastore/base", "server_priv", "sched_priv"]) mkdirSync(join(home, dir), { recursive: true });
          writeFileSync(join(home, "sched_priv/sched_config"), "compute_node\n");
          writeFileSync(join(home, ".idea-server-creating"), "earlier start");
          if (state === "interrupted") writeFileSync(join(home, ".idea-habitat-done"), "earlier start");
        }
        const source = readFileSync(join(images, script), "utf8");
        const start = source.indexOf('CREATING="${PBS_HOME}/.idea-server-creating"');
        const end = source.indexOf('\nlog "starting pbs_sched"');
        assert.ok(start >= 0 && end > start);
        // Isolate startup configuration from host paths and installed daemons.
        // The real shell branches still decide whether stored policy is changed.
        // The set-id strip changes the machine it runs on (root in Linux CI); the image check runs it.
        const body = source.slice(start, end).replaceAll(/\/opt\/pbs\/(?:libexec|bin|sbin)\//g, "")
          .replace("/opt/idea/roles/setid_strip.sh /usr /opt", "echo strip-ran >> \"$RECORD\"");
        const result = spawnSync("bash", ["-c", `set -euo pipefail
log() { :; }
install() { :; }
aws() { echo "aws $*" >> "$RECORD"; echo None; }
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
python3.13() { :; }
${body}`], {
          encoding: "utf8", timeout: 10_000,
          env: { ...process.env, PBS_HOME: home, RECORD: record, FAIL_HABITAT: state === "habitat-failure" ? "1" : "0",
            IDEA_SCHEDULER_DNS_NAME: "scheduler.example.invalid", IDEA_CLUSTER_NAME: "sample-cluster",
            AWS_DEFAULT_REGION: "us-east-2", IDEA_APP_DEPLOY_DIR: work,
            // a fresh start also asks execution hosts to re-read the address
            IDEA_ROUTE53_ZONE_ID: state === "fresh" ? "Z0EXAMPLE" : "", TASK_IP: "10.0.0.5",
            PBS_DATA_SERVICE_USER: userInfo().username, IDEA_MODULE_ID: "sched-custom" },
        });
        const calls = readFileSync(record, "utf8");
        if (state === "interrupted-in-habitat") {
          // a possibly partial datastore is neither configured nor started; the log says what to do
          assert.equal(result.status, 1);
          assert.equal(calls, "");
          assert.ok(existsSync(join(home, ".idea-server-creating")));
          assert.ok(!existsSync(join(home, ".idea-server-configured")));
          return;
        }
        if (state === "habitat-failure") {
          assert.notEqual(result.status, 0);
          assert.equal(calls, "habitat\n");
          return;
        }
        assert.equal(result.status, 0, result.stderr);
        if (state === "fresh" || state === "interrupted") {
          assert.equal(calls.includes("habitat"), state === "fresh");
          assert.ok(!existsSync(join(home, ".idea-server-creating")));
          assert.ok(!existsSync(join(home, ".idea-habitat-done")));
          assert.match(calls, /daemons\nstrip-ran\n/);
          // server policy is read under this module's id
          assert.match(calls, /"sched-custom\.openpbs\.server\.flatuid"/);
          if (state === "fresh") {
            // exactly the tags the scheduler's Run Command grant conditions on
            const send = calls.split("\n").find((line) => line.startsWith("aws ssm send-command"));
            assert.ok(send, calls);
            assert.match(send!, / --targets Key=tag:idea:ClusterName,Values=sample-cluster Key=tag:idea:NodeType,Values=compute-node Key=tag:idea:ModuleId,Values=sched-custom --document-name AWS-RunShellScript /);
          }
          assert.match(calls, /set queue normal started = True/);
          assert.match(calls, /set queue normal enabled = True/);
          assert.ok(existsSync(join(home, ".idea-server-configured")));
          assert.match(readFileSync(join(home, files[0]!), "utf8"), /compute_node/);
        } else {
          // An existing datastore keeps its server and queue policy; only hooks (re-imported when
          // the image changed them), the manager list and a PBS-default log_events are refreshed on
          // every start.
          assert.ok(calls.startsWith("daemons\n"), calls);
          assert.doesNotMatch(calls, /set server (?!managers|log_events = 255\n)|create queue|set queue|set sched/);
          assert.match(calls, /^-c set server log_events = 255$/m);
          for (const file of files.filter((f) => f !== "pbs_environment")) assert.equal(readFileSync(join(home, file), "utf8"), "operator configuration\n");
          // pbs_environment is rewritten from the image on every start, after a timestamped copy.
          assert.ok(readdirSync(home).some((f) => f.startsWith("pbs_environment.") && readFileSync(join(home, f), "utf8") === "operator configuration\n"));
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
        // Like the real CLI, registration only succeeds against a broker that is already running.
        stub("dcv-session-manager-broker", 'if grep -q launched "$RECORD"; then echo "Jwk url registered."; echo registered >> "$RECORD"; else echo "Connection refused" >&2; exit 1; fi');
        stub("setpriv", 'echo launched >> "$RECORD"');
        const result = spawnSync("bash", [join(images, "idea-control-plane/roles/broker.sh")], {
          encoding: "utf8", timeout: 10_000,
          env: { ...process.env, PATH: `${work}:${process.env.PATH}`, RECORD: record, MODE: mode,
            IDEA_CLUSTER_NAME: "sample-cluster", IDEA_MODULE_ID: moduleId, AWS_DEFAULT_REGION: "us-east-2",
            IDEA_BROKER_DISCOVERY_ADDRESSES: "broker.example.invalid:47500", IDEA_COGNITO_PROVIDER_URL: "https://example.invalid",
            IDEA_BROKER_CONF_FILE: conf, IDEA_BROKER_REGISTER_INTERVAL: "0.1" },
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
          assert.match(calls, /launched\nregistered/);
        }
      } finally { rmSync(work, { recursive: true, force: true }); }
    });
  }
}

// the vendor logback.xml (broker 2024.0.504) logs health checks and agent heartbeats at INFO
const VENDOR_LOGBACK = `<configuration>
    <root level="INFO">
        <appender-ref ref="STDOUT" />
    </root>
    <logger name="org.eclipse.jetty" level="INFO"/>
    <logger name="dcv.sm.ignite" level="INFO" />
</configuration>
`;
for (const [level, root] of [[undefined, "INFO"], ["debug", "DEBUG"], ["bogus", undefined]] as const) {
  test(`broker log levels: DCV_BROKER_LOG_LEVEL=${level ?? "unset"}`, () => {
    const work = mkdtempSync(join(tmpdir(), "broker-logback-"));
    try {
      const stub = (name: string, body: string) => writeFileSync(join(work, name), `#!/bin/bash\n${body}\n`, { mode: 0o755 });
      stub("aws", "echo None");
      for (const name of ["chown", "setpriv"]) stub(name, "exit 0");
      stub("dcv-session-manager-broker", "echo registered");
      const logback = join(work, "logback.xml");
      writeFileSync(logback, VENDOR_LOGBACK);
      const env = { ...process.env, PATH: `${work}:${process.env.PATH}`,
        IDEA_CLUSTER_NAME: "sample-cluster", IDEA_MODULE_ID: "vdc", AWS_DEFAULT_REGION: "us-east-2",
        IDEA_BROKER_DISCOVERY_ADDRESSES: "broker.example.invalid:47500", IDEA_COGNITO_PROVIDER_URL: "https://example.invalid",
        IDEA_BROKER_CONF_FILE: join(work, "broker.properties"), IDEA_BROKER_LOGBACK_FILE: logback, IDEA_BROKER_REGISTER_INTERVAL: "0.1",
        ...(level === undefined ? {} : { DCV_BROKER_LOG_LEVEL: level }) };
      // twice: a restarted task must not stack duplicate loggers
      const results = [0, 1].map(() => spawnSync("bash", [join(images, "idea-control-plane/roles/broker.sh")], { encoding: "utf8", timeout: 10_000, env }));
      const text = readFileSync(logback, "utf8");
      if (root === undefined) {
        assert.notEqual(results[0]?.status, 0);
        assert.equal(text, VENDOR_LOGBACK);
        return;
      }
      for (const result of results) assert.equal(result.status, 0, result.stderr);
      assert.match(text, new RegExp(`<root level="${root}">`));
      for (const name of ["io.swagger.api.HealthApi", "dcv.sm.filters.LogRequestFilter"]) {
        assert.equal(text.split(`<logger name="${name}" level="WARN" />`).length, 2, name);
      }
      assert.match(text, /<logger name="org.eclipse.jetty" level="INFO"\/>/);
    } finally { rmSync(work, { recursive: true, force: true }); }
  });
}

// a function of the scheduler role, run on its own with stand-ins for the commands it calls
function roleFunction(name: string): string {
  const source = readFileSync(join(images, "idea-control-plane/roles/scheduler.sh"), "utf8");
  const start = source.indexOf(`${name}() {`);
  const end = source.indexOf("\n}\n", start);
  assert.ok(start >= 0 && end > start, name);
  return source.slice(start, end + 3);
}

test("scheduler role: pbs_sched exiting ends its watch, which the role waits on", () => {
  const result = spawnSync("bash", ["-c", `set -euo pipefail
log() { echo "$*"; }
pgrep() { [[ -f "$ALIVE" ]]; }
${roleFunction("watch_pbs_sched")}
touch "$ALIVE"; watch_pbs_sched & pid=$!
sleep 0.3; rm -f "$ALIVE"; wait "$pid"`], {
    encoding: "utf8", timeout: 10_000,
    env: { ...process.env, ALIVE: join(mkdtempSync(join(tmpdir(), "sched-watch-")), "alive"), IDEA_SCHED_WATCH_INTERVAL: "0.05" },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "pbs_sched is not running\n");
  const source = readFileSync(join(images, "idea-control-plane/roles/scheduler.sh"), "utf8");
  assert.match(source, /^wait -n [^\n]*"\$\{SCHED_WATCH_PID\}"/m);
});

test("scheduler role: datastore files with another owner are given to the datastore account", () => {
  const work = mkdtempSync(join(tmpdir(), "datastore-owner-"));
  try {
    const home = join(work, "pbs");
    mkdirSync(join(home, "datastore/base"), { recursive: true });
    writeFileSync(join(home, "datastore/base/1"), "row");
    const record = join(work, "chown");
    // find runs chown itself, so the stand-in is a program on PATH
    const bin = join(work, "bin");
    mkdirSync(bin);
    writeFileSync(join(bin, "chown"), `#!/bin/bash\nprintf '%s\\n' "$@" >> "${record}"\n`, { mode: 0o755 });
    const run = (user: string) => spawnSync("bash", ["-c", `set -euo pipefail
log() { echo "$*"; }
${roleFunction("repair_datastore_owner")}
repair_datastore_owner`], { encoding: "utf8", timeout: 10_000, env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, PBS_HOME: home, PBS_DATA_SERVICE_USER: user } });
    // the caller owns everything: nothing to change
    const own = run(userInfo().username);
    assert.equal(own.status, 0, own.stderr);
    assert.ok(!existsSync(record));
    // another account owns the datastore: every entry is handed over, links not followed
    const other = run("nobody");
    assert.equal(other.status, 0, other.stderr);
    const args = readFileSync(record, "utf8").split("\n").filter(Boolean);
    assert.equal(args[0], "-h");
    assert.match(args[1]!, /^nobody:/);
    assert.deepEqual(args.slice(2).sort(), [join(home, "datastore"), join(home, "datastore/base"), join(home, "datastore/base/1")]);
    assert.match(other.stdout, /gave 3 datastore files back to nobody:/);
  } finally { rmSync(work, { recursive: true, force: true }); }
});

test("broker: registration keeps trying after the first five minutes", () => {
  const work = mkdtempSync(join(tmpdir(), "broker-register-"));
  try {
    const stub = (name: string, body: string) => writeFileSync(join(work, name), `#!/bin/bash\n${body}\n`, { mode: 0o755 });
    const count = join(work, "count");
    stub("aws", "echo None");
    for (const name of ["chown", "setpriv"]) stub(name, "exit 0");
    // fails 61 times, past the old limit of 60, then succeeds
    stub("dcv-session-manager-broker", `n=$(( $(cat "${count}" 2>/dev/null || echo 0) + 1 )); echo $n > "${count}"; (( n > 61 )) && echo "Jwk url registered." || exit 1`);
    const result = spawnSync("bash", [join(images, "idea-control-plane/roles/broker.sh")], {
      encoding: "utf8", timeout: 20_000,
      env: { ...process.env, PATH: `${work}:${process.env.PATH}`,
        IDEA_CLUSTER_NAME: "sample-cluster", IDEA_MODULE_ID: "vdc", AWS_DEFAULT_REGION: "us-east-2",
        IDEA_BROKER_DISCOVERY_ADDRESSES: "broker.example.invalid:47500", IDEA_COGNITO_PROVIDER_URL: "https://example.invalid",
        IDEA_BROKER_CONF_FILE: join(work, "broker.properties"), IDEA_BROKER_LOGBACK_FILE: join(work, "absent.xml"),
        IDEA_BROKER_REGISTER_INTERVAL: "0.01", IDEA_BROKER_REGISTER_RETRY_INTERVAL: "0.01" },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(readFileSync(count, "utf8").trim(), "62");
    assert.match(result.stdout, /retrying every minute/);
    assert.match(result.stdout, /registered https:\/\/example\.invalid as the authorization server/);
  } finally { rmSync(work, { recursive: true, force: true }); }
});
