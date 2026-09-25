/**
 * The account-level task ENI trunking pre-flight runs on a deploy that reaches
 * the ecs module, including the all-module deploy the one-phase migration uses.
 *
 * The guard has to key on the modules the deployment resolves to, not on the
 * module names the operator typed, because `deploy all` names nothing.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { Deps } from "../../src/cli/cdk-invoker.ts";
import { runDeploy } from "../../src/cli/commands/deploy.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";
const ACCOUNT = "111111111111";

/** Replays a cluster whose module table carries ecs, with trunking off in the account. */
function deps(errors: string[]): Deps {
  return {
    spawn: async () => {
      throw new Error("a refused pre-flight must not reach the cdk invocation");
    },
    cfn: {
      async describeChangeSet() {
        return {};
      },
      async executeChangeSet() {},
      async describeStack() {
        return {};
      },
    },
    s3: {
      async putObject() {},
      async getObject() {
        return "";
      },
    },
    async scan(input: { TableName: string }) {
      if (input.TableName === `${CLUSTER}.modules`) {
        return { Items: [{ module_id: "ecs", name: "ecs", type: "stack" }] };
      }
      return { Items: [{ key: "ecs.enabled", value: true }] };
    },
    async configWriter() {
      throw new Error("a refused pre-flight must not write settings");
    },
    async accountId() {
      return ACCOUNT;
    },
    ecsAccountSettings: {
      async listAccountSettings() {
        return [{ name: "awsvpcTrunking", value: "disabled" }];
      },
    },
    async httpStatus() {
      return 200;
    },
    async sleep() {},
    now: () => 0,
    uuid: () => "id",
    out() {},
    err(line: string) {
      errors.push(line);
    },
    async prompt() {
      return true;
    },
  } as Deps;
}

test("an all-module deploy runs the trunking pre-flight and refuses", async () => {
  const errors: string[] = [];
  await assert.rejects(
    () =>
      runDeploy(deps(errors), ["all"], {
        clusterName: CLUSTER,
        awsRegion: REGION,
        awsProfile: "sample-profile",
        moduleSet: "default",
      }),
    { name: "ExitWithCode" },
    "deploy all reaches ecs, so the account pre-flight has to run",
  );
  assert.equal(errors.length, 3, "the refusal names the setting, the account and the command");
  assert.match(errors[0] ?? "", /awsvpcTrunking is not enabled for account 111111111111 in us-east-2/u);
  assert.equal(
    errors[2],
    "aws ecs put-account-setting-default --name awsvpcTrunking --value enabled --region us-east-2 --profile sample-profile",
  );
});

for (const enabled of [undefined, false]) {
  test(`new host deployment refuses before mutation with container setting=${enabled}`, async () => {
    const errors: string[] = [];
    const effects = deps(errors);
    effects.scan = async ({ TableName }) => ({ Items: TableName.endsWith(".modules")
      ? [{ module_id: "scheduler", name: "scheduler", type: "app", status: "deployed" }]
      : enabled === undefined ? [] : [{ key: "ecs.enabled", value: enabled }] });
    effects.cfn.describeStack = async () => {
      throw Object.assign(new Error("Stack does not exist"), { name: "ValidationError" });
    };
    await assert.rejects(runDeploy(effects, ["all"], {
      clusterName: CLUSTER, awsRegion: REGION, moduleSet: "default", upgrade: true,
    }), { name: "ExitWithCode" });
    assert.deepEqual(errors, ["New host-shaped deployments are no longer supported. An existing host cluster upgrades with upgrade-cluster."]);
  });
}

test("an existing host stack passes the new deployment guard", async () => {
  const errors: string[] = [];
  const effects = deps(errors);
  effects.scan = async ({ TableName }) => ({ Items: TableName.endsWith(".modules")
    ? [{ module_id: "scheduler", name: "scheduler", type: "app", status: "deployed" }]
    : [] });
  let reads = 0;
  effects.cfn.describeStack = async () => { reads += 1; return { StackStatus: "CREATE_COMPLETE" }; };
  await assert.rejects(runDeploy(effects, ["all"], {
    clusterName: CLUSTER, awsRegion: REGION, moduleSet: "default",
  }), { name: "ExitWithCode" });
  assert.equal(reads, 1);
  assert.deepEqual(errors, []);
});

test("a stack lookup failure is not treated as a missing host cluster", async () => {
  const errors: string[] = [];
  const effects = deps(errors);
  effects.scan = async ({ TableName }) => ({ Items: TableName.endsWith(".modules")
    ? [{ module_id: "scheduler", name: "scheduler", type: "app" }] : [] });
  const failure = new Error("Access denied");
  effects.cfn.describeStack = async () => { throw failure; };
  await assert.rejects(runDeploy(effects, ["all"], {
    clusterName: CLUSTER, awsRegion: REGION, moduleSet: "default",
  }), (error) => error === failure);
  assert.deepEqual(errors, []);
});
