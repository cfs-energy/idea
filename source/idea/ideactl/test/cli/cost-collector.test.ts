import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { EC2Client, DescribeSubnetsCommand, DescribeRouteTablesCommand } from "@aws-sdk/client-ec2";
import { Command } from "commander";
import { buildProgram } from "../../src/cli/main.ts";
import { collectorNetwork, deployCostCollector, readCollectorNetwork, registerCostCollectorCommands, type CostCollectorOptions } from "../../src/cli/commands/cost-collector.ts";
import { fakeDeps, change } from "../support/deploy-harness.ts";

const options: CostCollectorOptions = {
  awsRegion: "us-east-1", stackName: "billing-costs", clusterName: "gov-cluster",
  controlPlaneImage: "123456789012.dkr.ecr.us-east-1.amazonaws.com/control:release",
  agentImage: `123456789012.dkr.ecr.us-east-1.amazonaws.com/agent@sha256:${"a".repeat(64)}`,
  datadogApiKeySecretArn: "arn:aws:secretsmanager:us-east-1:123456789012:secret:datadog-abcdef",
  subnetIds: ["subnet-0123456789abcdef0"], allowReplacement: [],
};
const network = async () => ({ vpcId: "vpc-0123456789abcdef0", subnetIds: options.subnetIds, publicSubnets: true });
const args = ["cost-collector", "deploy", "--aws-region", options.awsRegion, "--stack-name", options.stackName,
  "--cluster-name", options.clusterName, "--control-plane-image", options.controlPlaneImage,
  "--agent-image", options.agentImage, "--datadog-api-key-secret-arn", options.datadogApiKeySecretArn,
  "--subnet-ids", ...options.subnetIds];

function program(deps = fakeDeps()): Command {
  const result = new Command().exitOverride().configureOutput({ writeErr: () => {} });
  registerCostCollectorCommands(result, deps, network);
  return result;
}

test("root program registers deploy and destroy", () => {
  const group = buildProgram(fakeDeps()).commands.find((command) => command.name() === "cost-collector");
  assert.deepEqual(group?.commands.map((command) => command.name()), ["deploy", "destroy"]);
});

test("deploy synthesizes from flags without cluster tables and prepares before executing", async () => {
  const deps = fakeDeps();
  deps.scan = async () => { throw new Error("must not read cluster tables"); };
  const spawn = deps.spawn;
  let directory = "";
  deps.spawn = async (argv, spawnOptions) => {
    assert.equal(deps.executed.length, 0);
    assert.equal(spawnOptions.env.AWS_REGION, options.awsRegion);
    directory = spawnOptions.cwd;
    const template = JSON.parse(readFileSync(join(directory, `${options.stackName}.template.json`), "utf8"));
    const task = Object.values(template.Resources).find((r: any) => r.Type === "AWS::ECS::TaskDefinition") as any;
    const env = Object.fromEntries(task.Properties.ContainerDefinitions[1].Environment.map((e: any) => [e.Name, e.Value]));
    assert.equal(env.IDEA_COST_METRICS_INTERVAL_HOURS, "12");
    assert.equal(env.IDEA_COST_METRICS_LOOKBACK_DAYS, "8");
    assert.equal(env.IDEA_COST_METRICS_BY_ACCOUNT, "true");
    assert.equal(env.IDEA_COST_METRICS_MODULE_TAG, "custom:Module");
    return spawn(argv, spawnOptions);
  };
  await program(deps).parseAsync([...args, "--aws-profile", "billing", "--interval-hours", "12", "--lookback-days", "8", "--by-account", "--module-tag", "custom:Module"], { from: "user" });
  assert.ok(deps.spawns[0].includes("--method=prepare-change-set"));
  assert.deepEqual(deps.spawns[0].slice(-2), ["--profile", "billing"]);
  assert.deepEqual(deps.executed, [{ StackName: options.stackName, ChangeSetName: "cdk-deploy-change-set", DisableRollback: false }]);
  assert.equal(existsSync(directory), false);
});

test("guard reads every page, refuses resource loss and accepts explicit permission", async () => {
  const pages = () => [
    { Status: "CREATE_COMPLETE", Changes: [], NextToken: "1" },
    { Status: "CREATE_COMPLETE", Changes: [change("Remove", "Logs", "AWS::Logs::LogGroup")] },
  ];
  const refused = fakeDeps({ changeSetPages: pages() });
  await assert.rejects(deployCostCollector(refused, options, network), /allow-replacement Logs/);
  assert.deepEqual(refused.describeChangeSetCalls, [undefined, "1"]);
  assert.equal(refused.executed.length, 0);
  const allowed = fakeDeps({ changeSetPages: pages() });
  await deployCostCollector(allowed, { ...options, allowReplacement: ["Logs"] }, network);
  assert.equal(allowed.executed.length, 1);
});

test("task revisions are accepted, empty changes are skipped and failed deployments surface", async () => {
  const revision = fakeDeps({ changeSet: { Status: "CREATE_COMPLETE", Changes: [change("Modify", "Task", "AWS::ECS::TaskDefinition", "True")] } });
  await deployCostCollector(revision, options, network);
  assert.equal(revision.executed.length, 1);
  const empty = fakeDeps({ changeSet: { Status: "FAILED", StatusReason: "The submitted information didn't contain changes." } });
  await deployCostCollector(empty, options, network);
  assert.equal(empty.executed.length, 0);
  const failed = fakeDeps({ stack: { StackStatus: "UPDATE_ROLLBACK_COMPLETE", StackStatusReason: "task failed" } });
  await assert.rejects(deployCostCollector(failed, options, network), /task failed/);
  const spawnFailed = fakeDeps({ spawnExitCodes: [7] });
  await assert.rejects(deployCostCollector(spawnFailed, options, network));
  assert.equal(spawnFailed.executed.length, 0);
});

test("deployment waits for completion", async () => {
  const deps = fakeDeps();
  let calls = 0;
  deps.cfn.describeStack = async () => ({ StackStatus: calls++ === 0 ? "CREATE_IN_PROGRESS" : "CREATE_COMPLETE" });
  await deployCostCollector(deps, options, network);
  assert.deepEqual(deps.sleeps, [5000]);
});

test("destroy needs only stack, region and confirmation", async () => {
  const destroyArgs = ["cost-collector", "destroy", "--aws-region", options.awsRegion, "--stack-name", options.stackName];
  const declined = fakeDeps({ answers: [false] });
  await program(declined).parseAsync(destroyArgs, { from: "user" });
  assert.equal(declined.spawns.length, 0);
  for (const force of [false, true]) {
    const deps = fakeDeps({ answers: [true] });
    if (force) deps.prompt = async () => { throw new Error("force must skip prompt"); };
    await program(deps).parseAsync([...destroyArgs, ...(force ? ["--force"] : [])], { from: "user" });
    assert.deepEqual(deps.spawns[0].slice(1, 4), ["destroy", "--force", options.stackName]);
  }
});

test("required flags and positive schedule values are enforced", async () => {
  await assert.rejects(program().parseAsync(["cost-collector", "deploy"], { from: "user" }));
  for (const value of ["0", "-1", "1.5", "oops"]) {
    await assert.rejects(program().parseAsync([...args, "--interval-hours", value], { from: "user" }), /positive integer/);
    await assert.rejects(program().parseAsync([...args, "--lookback-days", value], { from: "user" }), /positive integer/);
  }
});

test("invalid image and noncommercial region fail before network reads", async () => {
  const noRead = async (): Promise<never> => { throw new Error("network should not be read"); };
  await assert.rejects(deployCostCollector(fakeDeps(), { ...options, agentImage: "agent:latest" }, noRead), /private ECR/);
  for (const awsRegion of ["us-gov-west-1", "cn-north-1"]) {
    await assert.rejects(deployCostCollector(fakeDeps(), { ...options, awsRegion }, noRead), /commercial AWS/);
  }
});

test("subnet routes determine public IPs including inherited main routes", () => {
  const subnets = [{ SubnetId: "subnet-a", VpcId: "vpc-a", MapPublicIpOnLaunch: false }, { SubnetId: "subnet-b", VpcId: "vpc-a" }];
  const publicTable = { VpcId: "vpc-a", Associations: [{ Main: true }], Routes: [{ DestinationCidrBlock: "0.0.0.0/0", GatewayId: "igw-a", State: "active" as const }] };
  assert.equal(collectorNetwork(["subnet-a", "subnet-b"], subnets, [publicTable]).publicSubnets, true);
  const privateTable = { VpcId: "vpc-a", Associations: [{ SubnetId: "subnet-a" }], Routes: [{ DestinationCidrBlock: "0.0.0.0/0", NatGatewayId: "nat-a", State: "active" as const }] };
  assert.equal(collectorNetwork(["subnet-a"], subnets, [privateTable, publicTable]).publicSubnets, false);
  assert.throws(() => collectorNetwork(["subnet-a", "subnet-b"], subnets, [privateTable, publicTable]), /all public or all private/);
  assert.throws(() => collectorNetwork(["missing"], subnets, [publicTable]), /not found/);
  assert.throws(() => collectorNetwork(["subnet-a"], subnets, []), /no route table/);
  assert.throws(() => collectorNetwork([], [], []), /one VPC/);
  assert.throws(() => collectorNetwork(["subnet-a", "subnet-b"], [subnets[0], { ...subnets[1], VpcId: "vpc-b" }], []), /one VPC/);
});


test("network reader follows route table pages and closes its client", async (t) => {
  const tokens: Array<string | undefined> = [];
  const destroy = t.mock.method(EC2Client.prototype, "destroy", () => {});
  t.mock.method(EC2Client.prototype, "send", async (command: unknown) => {
    if (command instanceof DescribeSubnetsCommand) {
      assert.deepEqual(command.input.SubnetIds, options.subnetIds);
      return { Subnets: [{ SubnetId: options.subnetIds[0], VpcId: "vpc-a" }] };
    }
    assert.ok(command instanceof DescribeRouteTablesCommand);
    assert.deepEqual(command.input.Filters, [{ Name: "vpc-id", Values: ["vpc-a"] }]);
    tokens.push(command.input.NextToken);
    return command.input.NextToken === undefined ? { RouteTables: [], NextToken: "next" } : {
      RouteTables: [{ VpcId: "vpc-a", Associations: [{ Main: true }], Routes: [
        { DestinationCidrBlock: "0.0.0.0/0", GatewayId: "igw-a", State: "active" },
      ] }],
    };
  });
  assert.deepEqual(await readCollectorNetwork(options), { vpcId: "vpc-a", subnetIds: options.subnetIds, publicSubnets: true });
  assert.deepEqual(tokens, [undefined, "next"]);
  assert.equal(destroy.mock.callCount(), 1);
});

test("network read failures propagate and close the client", async (t) => {
  const destroy = t.mock.method(EC2Client.prototype, "destroy", () => {});
  t.mock.method(EC2Client.prototype, "send", async () => { throw new Error("access denied"); });
  await assert.rejects(readCollectorNetwork(options), /access denied/);
  assert.equal(destroy.mock.callCount(), 1);
});
