import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { directoryJoinCommands } from "../../src/cdk/directory-join.ts";

function runHost(options: { provider?: string; joined?: boolean; member?: boolean; fail?: string } = {}) {
  const directory = mkdtempSync(join(tmpdir(), "directory-wrapper-"));
  try {
    const bin = join(directory, "bin");
    mkdirSync(bin);
    mkdirSync(join(directory, "etc/sssd"), { recursive: true });
    mkdirSync(join(directory, "etc/openldap/cacerts"), { recursive: true });
    mkdirSync(join(directory, "var/lib/idea/directory"), { recursive: true });
    writeFileSync(join(directory, "etc/sssd/sssd.conf"), "configured");
    writeFileSync(join(directory, "etc/openldap/cacerts/openldap-server.pem"), "certificate");
    if (options.joined) writeFileSync(join(directory, "var/lib/idea/directory-joined"), "");
    // the membership test passes only once the join script has run (or the host was joined before)
    if (options.joined || options.member) writeFileSync(join(directory, "member"), "");
    const stub = `#!/bin/bash
name=$(basename "$0")
echo "$name $*" >> "$TEST_ROOT/calls"
case "$name" in
  curl) echo '{"ContainerInstanceArn":"arn:synthetic:container-instance"}';;
  jq) echo 'arn:synthetic:container-instance';;
  sleep)
    count=$(cat "$TEST_ROOT/sleeps" 2>/dev/null || echo 0)
    echo $((count + 1)) > "$TEST_ROOT/sleeps"
    if (( count >= 2 )); then kill -TERM "$PPID"; fi;;
  aws)
    if [[ "$FAIL" == fetch && "$1" == s3 && ! -e "$TEST_ROOT/failed" ]] ||
       [[ "$FAIL" == attribute && "$1" == ecs && ! -e "$TEST_ROOT/failed" ]]; then
      touch "$TEST_ROOT/failed"; exit 1
    fi;;
  adcli) [[ "$FAIL" != membership && -e "$TEST_ROOT/member" ]] || exit 1;;
  bash) [[ "$*" != *directory_join.sh* ]] || touch "$TEST_ROOT/member";;
  openssl) [[ "$FAIL" != certificate || "$1" != x509 ]] || exit 1;;
esac
exit 0
`;
    for (const command of ["aws", "tar", "bash", "sssctl", "adcli", "systemctl", "authselect", "openssl", "nfsidmap", "curl", "jq", "sleep"]) {
      writeFileSync(join(bin, command), stub, { mode: 0o755 });
    }
    const commands = directoryJoinCommands({ packageUri: "s3://sample-bucket/idea/bootstrap/host.tar.gz", clusterName: "synthetic", ecsClusterName: "synthetic-ecs", region: "us-east-2", provider: options.provider ?? "activedirectory", domain: "example.invalid" });
    const start = commands.indexOf("#!/bin/bash");
    const end = commands.indexOf("IDEA_DIRECTORY_WRAPPER");
    const script = commands.slice(start, end).join("\n").replaceAll("/var/lib/idea", `${directory}/var/lib/idea`).replaceAll("/etc/", `${directory}/etc/`);
    const result = spawnSync("/bin/bash", ["-c", script], { env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, TEST_ROOT: directory, FAIL: options.fail ?? "" }, encoding: "utf8", timeout: 30000 });
    assert.equal(result.error, undefined);
    return { status: result.status, signal: result.signal, calls: readFileSync(join(directory, "calls"), "utf8"), joined: existsSync(join(directory, "var/lib/idea/directory-joined")) };
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("retries a failed package fetch and publishes only after directory readiness", () => {
  const result = runHost({ fail: "fetch" });
  assert.equal(result.status, 0);
  assert.equal(result.calls.match(/aws s3 cp/g)?.length, 2);
  assert.equal(result.calls.match(/bash .*directory_join.sh/g)?.length, 1);
  assert.ok(result.calls.indexOf("adcli testjoin") < result.calls.indexOf("nfsidmap -c"));
  assert.ok(result.calls.indexOf("nfsidmap -c") < result.calls.indexOf("aws ecs put-attributes"));
  assert.equal(result.joined, true);
});

test("retries attribute publication without joining the directory again", () => {
  const result = runHost({ fail: "attribute" });
  assert.equal(result.status, 0);
  assert.equal(result.calls.match(/bash .*directory_join.sh/g)?.length, 1);
  assert.equal(result.calls.match(/aws ecs put-attributes/g)?.length, 2);
});

for (const provider of ["activedirectory", "openldap"]) {
  test(`a joined ${provider} host skips the package on reboot and republishes readiness`, () => {
    const result = runHost({ joined: true, provider });
    assert.equal(result.status, 0);
    assert.doesNotMatch(result.calls, /aws s3 cp|directory_join.sh/);
    assert.match(result.calls, /nfsidmap -c[\s\S]*aws ecs put-attributes/);
  });
}

test("a host that passes the membership test is not made to leave and rejoin", () => {
  const result = runHost({ member: true });
  assert.equal(result.status, 0);
  assert.doesNotMatch(result.calls, /aws s3 cp|directory_join.sh/);
  assert.match(result.calls, /adcli testjoin[\s\S]*systemctl restart sssd[\s\S]*aws ecs put-attributes/);
  assert.equal(result.joined, true);
});

test("failed AD membership never publishes readiness", () => {
  const result = runHost({ fail: "membership" });
  assert.notEqual(result.status, 0);
  assert.equal(result.joined, false);
  assert.doesNotMatch(result.calls, /aws ecs put-attributes/);
});

test("an offline joined AD host retries readiness without leaving the directory", () => {
  const result = runHost({ joined: true, fail: "membership" });
  assert.notEqual(result.status, 0);
  assert.doesNotMatch(result.calls, /directory_join.sh|aws ecs put-attributes/);
});

test("OpenLDAP rejects an invalid certificate before marking the host joined", () => {
  const result = runHost({ provider: "openldap", fail: "certificate" });
  assert.notEqual(result.status, 0);
  assert.equal(result.joined, false);
  assert.doesNotMatch(result.calls, /aws ecs put-attributes/);
});
