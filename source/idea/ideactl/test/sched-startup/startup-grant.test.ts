/**
 * Proves the scheduler container role starts, by running it.
 *
 * A test that asserts a policy document contains a permission proves nothing about a container,
 * so this renders the real policy, runs the real role script in the release image against an
 * endpoint that answers only what that document allows, and requires the run to reach the line
 * that starts the module. It then removes the grant and requires the same run to fail there.
 *
 * The rendered policy is compared against the same template rendered with the container flag off,
 * which is how the added statement is isolated: the documents must differ by exactly that
 * statement and nothing else.
 */

import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { renderPolicy } from "../../src/cdk/policy.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { optionalFixtures, optionalService } from "../support/fixtures.ts";

const HERE = fileURLToPath(new URL("./", import.meta.url));
const RAW = fileURLToPath(new URL("../../tools/parity/fixtures/idea-dev27/raw/", import.meta.url));
const SCAN = `${RAW}cluster-settings.scan.json`;
const MODULES = `${RAW}modules.scan.json`;
// The bare local tag is a convenience that a registry push can take away, because tagging the pushed
// image moves the name. When it is gone this must skip, not fail: a missing build artifact is not a
// defect in the thing under test. Set the environment variable to point at any equivalent image,
// including a registry-qualified one.
const IMAGE = process.env.IDEA_IMAGE ?? "idea-control-plane:v26.09.0";
const CONTEXT = process.env.DOCKER_CONTEXT ?? "default";
const SID = "ClusterUserSync";

const haveFixtures = optionalFixtures(
  [SCAN, MODULES],
  "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27",
);

function haveImage(): boolean {
  try {
    execFileSync("docker", ["--context", CONTEXT, "image", "inspect", IMAGE], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

interface Statement {
  Action?: string | string[];
  Resource?: string | string[];
  Sid?: string;
}

interface Document {
  Statement: Statement[];
}

/** Renders scheduler.yml from the captured settings with the container flag set either way. */
function scheduler(containerEnabled: boolean): Document {
  const scan = JSON.parse(readFileSync(SCAN, "utf-8")) as { Items: { key: { S: string }; value: unknown }[] };
  scan.Items = scan.Items.filter((item) => item.key.S !== "ecs.enabled");
  if (containerEnabled) scan.Items.push({ key: { S: "ecs.enabled" }, value: { BOOL: true } });
  const config = ClusterConfig.fromFile(JSON.stringify(scan), readFileSync(MODULES, "utf-8"));
  return renderPolicy("scheduler.yml", {
    config,
    moduleId: config.moduleId("scheduler"),
    vars: {
      compute_node_role_arn: "<<compute-node-role>>",
      scheduler_role_arn: "<<scheduler-role>>",
      spot_fleet_request_role_arn: "<<spot-fleet-role>>",
    },
  }) as Document;
}

const asList = (value: string | string[] | undefined): string[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

describe("the account-table grant the container user sync needs", () => {
  if (!haveFixtures) return;
  const withGrant = scheduler(true);
  const withoutGrant = scheduler(false);

  it("is the only difference the container flag makes to this policy", () => {
    const added = withGrant.Statement.filter(
      (statement) => !withoutGrant.Statement.some((other) => JSON.stringify(other) === JSON.stringify(statement)),
    );
    assert.equal(added.length, 1, `expected one added statement, got ${JSON.stringify(added)}`);
    assert.equal(added[0]?.Sid, SID);
    assert.equal(withGrant.Statement.length, withoutGrant.Statement.length + 1);
  });

  it("grants only the scan the sync performs, on only the three tables it reads", () => {
    const statement = withGrant.Statement.find((entry) => entry.Sid === SID);
    assert.ok(statement !== undefined, `${SID} did not render`);
    assert.deepEqual(asList(statement.Action), ["dynamodb:Scan"]);
    assert.deepEqual(
      asList(statement.Resource).map((arn) => arn.split(":table/")[1]),
      ["idea-dev27.accounts.users", "idea-dev27.accounts.groups", "idea-dev27.accounts.group-members"],
    );
  });

  it("lets the role reach the line that starts the module, and does not without the grant", () => {
    if (!optionalService(haveImage(), `the container image ${IMAGE} in the ${CONTEXT} context`, `docker --context ${CONTEXT} pull ${IMAGE}`)) {
      return;
    }
    // The container engine binds only paths under the home directory.
    const work = mkdtempSync(join(homedir(), ".ideactl-sched-startup-"));
    try {
      writeFileSync(join(work, "with-grant.json"), JSON.stringify(withGrant));
      writeFileSync(join(work, "without-grant.json"), JSON.stringify(withoutGrant));
      const account = asList(withGrant.Statement.find((entry) => entry.Sid === SID)?.Resource)[0]?.split(":")[4];
      assert.ok(account !== undefined && account !== "", "could not read the account from the rendered ARNs");
      const result = spawnSync(
        "bash",
        [join(HERE, "prove.sh"), work, join(work, "with-grant.json"), join(work, "without-grant.json")],
        {
          encoding: "utf8",
          env: {
            ...process.env,
            AWS_ACCOUNT_ID: account,
            AWS_REGION_NAME: "us-east-2",
            DOCKER_CONTEXT: CONTEXT,
            IDEA_CLUSTER_NAME: "idea-dev27",
            IDEA_IMAGE: IMAGE,
          },
          timeout: 20 * 60 * 1_000,
        },
      );
      console.log(result.stdout);
      if (result.stderr !== "") console.error(result.stderr);
      assert.match(result.stdout, /^PASS run 1 reached 'starting scheduler module'$/m);
      assert.match(result.stdout, /^PASS run 2 was refused dynamodb:Scan on the account tables by the policy$/m);
      assert.match(result.stdout, /^PASS run 2 failed the user sync on that refusal$/m);
      assert.match(result.stdout, /^PASS run 2 never reached 'starting scheduler module'$/m);
      assert.match(result.stdout, /^PASS run 3 had exactly one of the two roles generate the pair/m);
      assert.match(result.stdout, /^PASS run 3 published a certificate and key with one modulus/m);
      assert.doesNotMatch(result.stdout, /^FAIL /m);
      assert.equal(result.status, 0);
    } finally {
      if (existsSync(work)) rmSync(work, { force: true, recursive: true });
    }
  });
});
