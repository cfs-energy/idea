/**
 * Rollback and replacement-approval contracts for guarded deployments.
 *
 * Every external effect is replayed in memory. The failure replay chooses its
 * terminal stack status from the exact ExecuteChangeSet rollback input.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import {
  CdkInvoker,
  CDK_DEPLOY_CHANGE_SET_NAME,
  type ChangeSetDescription,
  type Deps,
} from "../../src/cli/cdk-invoker.ts";
import { DeploymentHelper } from "../../src/cli/deployment-helper.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";
const DEPLOYMENT_ID = "00000000-0000-4000-8000-000000000001";

interface ExecuteChangeSetInput {
  StackName: string;
  ChangeSetName: string;
  DisableRollback: boolean;
}

interface ReplayOptions {
  changes?: NonNullable<ChangeSetDescription["Changes"]>;
  failExecution?: boolean;
  tables?: Record<string, Array<Record<string, unknown>>>;
}

interface Replay {
  deps: Deps;
  executions: ExecuteChangeSetInput[];
  stdout: string[];
  stackStatus(): string;
}

const home = mkdtempSync(join(tmpdir(), "ideactl-rollback-"));
const previousHome = process.env.IDEA_USER_HOME;
const previousCdkBin = process.env.IDEA_CDK_BIN;

before(() => {
  process.env.IDEA_USER_HOME = home;
  process.env.IDEA_CDK_BIN = "/opt/idea/bin/cdk";
});

after(() => {
  if (previousHome === undefined) delete process.env.IDEA_USER_HOME;
  else process.env.IDEA_USER_HOME = previousHome;
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  rmSync(home, { recursive: true, force: true });
});

/** Build a credential-free replay of the guarded deployment boundary. */
function replay(options: ReplayOptions = {}): Replay {
  const executions: ExecuteChangeSetInput[] = [];
  const stdout: string[] = [];
  let terminalStatus = "UPDATE_COMPLETE";

  const deps: Deps = {
    async spawn() {
      return 0;
    },
    cfn: {
      async describeChangeSet() {
        return { Status: "CREATE_COMPLETE", Changes: options.changes ?? [] };
      },
      async executeChangeSet(input) {
        executions.push(input);
        if (options.failExecution === true) {
          terminalStatus = input.DisableRollback ? "UPDATE_FAILED" : "UPDATE_ROLLBACK_COMPLETE";
        }
      },
      async describeStack() {
        return { StackStatus: terminalStatus, StackStatusReason: "simulated resource failure" };
      },
    },
    s3: {
      async putObject() {},
      async getObject() {
        return "";
      },
    },
    async scan(input) {
      return { Items: options.tables?.[input.TableName] ?? [] };
    },
    async configWriter() {
      return {
        async syncModulesInDb() {},
        async syncClusterSettingsInDb() {},
        async setConfigEntry() {},
        async deleteConfigEntries() {},
      };
    },
    async accountId() {
      return "123456789012";
    },
    async httpStatus() {
      return 200;
    },
    async sleep() {},
    now() {
      return 0;
    },
    uuid() {
      return DEPLOYMENT_ID;
    },
    out(line) {
      stdout.push(line);
    },
    err() {},
    async prompt() {
      return true;
    },
  };

  return {
    deps,
    executions,
    stdout,
    stackStatus: () => terminalStatus,
  };
}

/** Return one replacement entry in the shape produced by DescribeChangeSet. */
function replacement(logicalId: string): NonNullable<ChangeSetDescription["Changes"]>[number] {
  return {
    ResourceChange: {
      Action: "Modify",
      LogicalResourceId: logicalId,
      ResourceType: "AWS::EC2::Instance",
      Replacement: "True",
    },
  };
}

/** Construct the invoker used by direct rollback checks. */
function invoker(deps: Deps, rollback?: boolean, allowReplacement: readonly string[] = []): CdkInvoker {
  return new CdkInvoker({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleId: "analytics",
    moduleName: "analytics",
    moduleSet: "default",
    deploymentId: DEPLOYMENT_ID,
    rollback,
    allowReplacement,
    deps,
  });
}

test("a failed deployment requests rollback and reaches a rollback terminal state", async () => {
  const state = replay({ changes: [], failExecution: true });

  await assert.rejects(() => invoker(state.deps).deployThroughChangeSet(), /UPDATE_ROLLBACK_COMPLETE/);

  assert.deepEqual(state.executions, [{
    StackName: `${CLUSTER}-analytics`,
    ChangeSetName: CDK_DEPLOY_CHANGE_SET_NAME,
    DisableRollback: false,
  }]);
  assert.equal(state.stackStatus(), "UPDATE_ROLLBACK_COMPLETE");
});

test("an explicit no-rollback request remains available for parity", async () => {
  const state = replay({ changes: [], failExecution: true });

  await assert.rejects(() => invoker(state.deps, false).deployThroughChangeSet(), /UPDATE_FAILED/);

  assert.equal(state.executions[0]?.DisableRollback, true);
  assert.equal(state.stackStatus(), "UPDATE_FAILED");
});

test("upgrade-mode deployment carries one approval list and prints every override", async () => {
  const allowed = ["schedulerhost", "directoryhost"];
  const state = replay({
    changes: allowed.map(replacement),
    tables: {
      [`${CLUSTER}.modules`]: [{
        module_id: "analytics",
        name: "analytics",
        type: "stack",
        status: "deployed",
        stack_name: `${CLUSTER}-analytics`,
      }],
      [`${CLUSTER}.cluster-settings`]: [],
    },
  });
  const helper = await DeploymentHelper.open({
    clusterName: CLUSTER,
    awsRegion: REGION,
    moduleSet: "default",
    deploymentId: DEPLOYMENT_ID,
    upgrade: true,
    allModules: true,
    allowReplacement: allowed,
    deps: state.deps,
  });

  await helper.invoke();

  assert.deepEqual(
    state.stdout.filter((line) => line.startsWith("change-set guard: ALLOWED by")),
    allowed.map(
      (logicalId) =>
        `change-set guard: ALLOWED by --allow-replacement: Modify of ${logicalId} (AWS::EC2::Instance) replaces the resource`,
    ),
  );
});
