import assert from "node:assert/strict";
import test from "node:test";

import { Command } from "commander";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import { checkAwsvpcTrunking } from "../../src/cli/commands/upgrade.ts";
import { registerDeleteClusterCommands } from "../../src/cli/commands/delete-cluster.ts";
import { registerDirectoryServiceCommands } from "../../src/cli/commands/directoryservice.ts";
import { registerIntegrationTestCommands } from "../../src/cli/commands/tests.ts";
import { registerSsoCommands } from "../../src/cli/commands/sso.ts";
import { registerSupportCommands } from "../../src/cli/commands/support.ts";
import { registerUtilsCommands } from "../../src/cli/commands/utils.ts";
import { runDeploy } from "../../src/cli/commands/deploy.ts";
import type { Deps } from "../../src/cli/cdk-invoker.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";

function program(): Command {
  return new Command().exitOverride();
}

test("directory-service adapter factory receives the selected profile", async () => {
  const created: string[] = [];
  let selectedProfile = "";
  const cli = program();
  registerDirectoryServiceCommands(cli, async (options) => {
    selectedProfile = options.awsProfile ?? "";
    return {
      secrets: {
        async createSecret(input) {
          created.push(input.Name);
          return {};
        },
      },
      out() {},
    };
  });

  await cli.parseAsync([
    "directoryservice",
    "create-service-account-secrets",
    "--cluster-name", CLUSTER,
    "--aws-region", REGION,
    "--aws-profile", "operator",
    "--username", "admin",
    "--password", "secret",
  ], { from: "user" });

  assert.equal(selectedProfile, "operator");
  assert.deepEqual(created, [
    `${CLUSTER}-directoryservice-None-username`,
    `${CLUSTER}-directoryservice-None-password`,
  ]);
});

test("single sign-on adapter factory loads action-scoped configuration", async () => {
  const output: string[] = [];
  let selectedProfile = "";
  const cli = program();
  registerSsoCommands(cli, async (options) => {
    selectedProfile = options.awsProfile ?? "";
    return {
      config: new ClusterConfig([{ key: "identity-provider.cognito.domain_url", value: "https://example.invalid" }]),
      cognito: {
        async getIdentityProviderByIdentifier() { return {}; },
        async createIdentityProvider() {},
        async updateIdentityProvider() {},
        async createUserPoolClient() { return {}; },
        async updateUserPoolClient() { return {}; },
        async listUsers() { return {}; },
        async adminLinkProviderForUser() {},
      },
      secrets: {
        async describeSecret() { return {}; },
        async createSecret() { return {}; },
        async updateSecret() { return {}; },
      },
      async setConfigEntry() {},
      async sleep() {},
      out(line) { output.push(line); },
    };
  });

  await cli.parseAsync([
    "sso",
    "show-idp-info",
    "--cluster-name", CLUSTER,
    "--aws-region", REGION,
    "--aws-profile", "operator",
    "--provider-type", "OIDC",
  ], { from: "user" });

  assert.equal(selectedProfile, "operator");
  assert.deepEqual(output, ["Redirect URL", "https://example.invalid/oauth2/idpresponse"]);
});

test("utility adapter factory drives the local service matrix", async () => {
  const output: string[] = [];
  const cli = program();
  registerUtilsCommands(cli, async () => ({
    config: new ClusterConfig([]),
    api: {
      async getParametersByPath() { return {}; },
      async describeVpcEndpointServices() { return {}; },
      async getManagedPrefixListEntries() { return {}; },
      async describeManagedPrefixLists() { return {}; },
      async modifyManagedPrefixList() {},
    },
    async dnsSuffix() { return "amazonaws.com"; },
    out(line) { output.push(line); },
  }));

  await cli.parseAsync(["utils", "aws-services"], { from: "user" });
  assert.match(output[0] ?? "", /AWS Service/u);
});

test("support and integration-test adapter factories run with injected dependencies", async () => {
  const supportOutput: string[] = [];
  const supportCli = program();
  registerSupportCommands(supportCli, async (options) => ({
    now: () => new Date("2026-01-02T03:04:05.000Z"),
    async archive(directory) {
      assert.match(directory, /support/u);
      return "support.tar.gz";
    },
    out(line) { supportOutput.push(line); },
  }));
  await supportCli.parseAsync([
    "support", "deployment",
    "--cluster-name", CLUSTER,
    "--aws-region", REGION,
    "--aws-profile", "operator",
  ], { from: "user" });
  assert.equal(supportOutput.at(-1), "Debug Package: support.tar.gz");

  const integrationOutput: string[] = [];
  const integrationCli = program();
  registerIntegrationTestCommands(integrationCli, async () => ({
    config: new ClusterConfig([], [{ module_id: "module", name: "metrics", type: "stack", status: "deployed" }]),
    casesForModule() {
      return [{ id: "case", async run() {} }];
    },
    out(line) { integrationOutput.push(line); },
    err() {},
  }));
  await integrationCli.parseAsync([
    "run-integration-tests", "--cluster-name", CLUSTER, "--aws-region", REGION,
    "--admin-username", "admin", "--admin-password", "secret", "module",
  ], { from: "user" });
  assert.deepEqual(integrationOutput, ["case   [STARTED]", "case   [PASS]"]);
});

test("backup deletion adapter factory is resolved before the command runs", async () => {
  let resolved = "";
  const cli = program();
  registerDeleteClusterCommands(cli, async (options, command) => {
    resolved = `${command}:${options.awsProfile}`;
    return {
      async loadConfig() { return undefined; },
      async findInstances() { return []; },
      async instanceTerminationProtection() { return false; },
      async disableInstanceTerminationProtection() {},
      async terminateInstance() {},
      async getTaggedStacks() { return { stacks: [] }; },
      async describeStack() { return { stackName: "" }; },
      async disableStackTerminationProtection() {},
      async deleteStack() {},
      async findAppInstance() { return undefined; },
      async sendAppCleanup() { return ""; },
      async appCleanupStatus() { return []; },
      async findBedrockProjects() { return []; },
      async deleteBedrockProjectResources() {},
      async listUserPools() { return { pools: [] }; },
      async describeUserPool() { return {}; },
      async disableUserPoolDeletionProtection() {},
      async describeLambdaNetworkInterfaces() { return []; },
      async deleteNetworkInterface() {},
      async describeBackupVault() {},
      async listRecoveryPoints() { return []; },
      async deleteRecoveryPoint() {},
      async listTables() { return { tableNames: [] }; },
      async deleteTable() {},
      async listDynamoDbAlarms() { return []; },
      async deleteAlarms() {},
      async listLogGroups() { return []; },
      async deleteLogGroup() {},
      async accountId() { return "111111111111"; },
      async bucketExists() { return false; },
      async deleteAllBucketObjectVersions() {},
      async deleteBucket() {},
      async prompt() { return true; },
      async sleep() {},
      out() {},
      err() {},
    };
  });
  await cli.parseAsync([
    "delete-backups", "--cluster-name", CLUSTER, "--aws-region", REGION,
    "--aws-profile", "operator", "--force",
  ], { from: "user" });
  assert.equal(resolved, "delete-backups:operator");
});

test("ECS prerequisite runs before deployment and refuses disabled trunking", async () => {
  const errors: string[] = [];
  const deps: Deps = {
    spawn: async () => 0,
    cfn: {
      async describeChangeSet() { return {}; },
      async executeChangeSet() {},
      async describeStack() { return {}; },
    },
    s3: {
      async putObject() {},
      async getObject() { return ""; },
    },
    async scan(input) {
      if (input.TableName === `${CLUSTER}.modules`) {
        return { Items: [{ module_id: "ecs", name: "ecs", type: "stack" }] };
      }
      return { Items: [{ key: "ecs.enabled", value: true }] };
    },
    async configWriter() {
      throw new Error("not reached");
    },
    async accountId() { return "111111111111"; },
    ecsAccountSettings: {
      async listAccountSettings() { return [{ name: "awsvpcTrunking", value: "disabled" }]; },
    },
    async httpStatus() { return 200; },
    async sleep() {},
    now: () => 0,
    uuid: () => "id",
    out() {},
    err(line) { errors.push(line); },
    async prompt() { return true; },
  };

  await assert.rejects(
    () => runDeploy(deps, ["ecs"], {
      clusterName: CLUSTER,
      awsRegion: REGION,
      awsProfile: "operator",
      moduleSet: "default",
    }),
    { name: "ExitWithCode" },
  );
  assert.equal(errors.length, 3);
  assert.match(errors[2] ?? "", /--profile operator/u);
});

test("the ECS prerequisite helper remains injectable without a live call", async () => {
  let reads = 0;
  await checkAwsvpcTrunking({
    async accountId() { return "111111111111"; },
    ecsAccountSettings: {
      async listAccountSettings() {
        reads += 1;
        return [{ name: "awsvpcTrunking", value: "enabled" }];
      },
    },
    err() {},
  }, { awsRegion: REGION, awsProfile: "operator" });
  assert.equal(reads, 1);
});

test("upgrade live client options include fromIni when a profile is set", async () => {
  const { upgradeLiveClientOptions } = await import("../../src/cli/commands/upgrade.ts");
  const withProfile = await upgradeLiveClientOptions("us-east-2", "operator");
  assert.equal(withProfile.region, "us-east-2");
  assert.ok(withProfile.credentials !== undefined);
  const withoutProfile = await upgradeLiveClientOptions("us-east-2", undefined);
  assert.equal(withoutProfile.credentials, undefined);
});

test("createLiveDeleteClusterDeps builds live dependencies for both deletion commands", async () => {
  const { createLiveDeleteClusterDeps } = await import("../../src/cli/live-operator-adapters.ts");
  const factory = createLiveDeleteClusterDeps({
    async spawn() { return 0; },
    cfn: { async describeChangeSet() { return {}; }, async executeChangeSet() {}, async describeStack() { return {}; } },
    s3: { async putObject() {}, async getObject() { return ""; } },
    async scan() { return { Items: [] }; },
    async configWriter() { throw new Error("not reached"); },
    async accountId() { return "123456789012"; },
    async httpStatus() { return 200; },
    async sleep() {},
    now: () => 0,
    uuid: () => "id",
    out() {},
    err() {},
    async prompt() { return false; },
  });
  for (const command of ["delete-cluster", "delete-backups"] as const) {
    const built = await factory({ clusterName: CLUSTER, awsRegion: REGION }, command);
    // No member may be left as a refusing placeholder: the command's own logic is complete, so a
    // stub here is the only thing that can stop a real teardown.
    const unimplemented = Object.entries(built)
      .filter(([, value]) => typeof value === "function")
      .map(([name]) => name)
      .filter((name) => /^unavailable/.test(name));
    assert.deepEqual(unimplemented, []);
    for (const name of [
      "findInstances",
      "getTaggedStacks",
      "describeStack",
      "deleteStack",
      "stackFailedResources",
      "describeLambdaNetworkInterfaces",
      "deleteNetworkInterface",
      "listUserPools",
      "listTables",
      "listDynamoDbAlarms",
      "listLogGroups",
      "bucketExists",
      "deleteAllBucketObjectVersions",
      "findBedrockProjects",
      "deleteBedrockProjectResources",
    ]) {
      assert.equal(typeof (built as unknown as Record<string, unknown>)[name], "function", `${command} is missing ${name}`);
    }
  }
});
