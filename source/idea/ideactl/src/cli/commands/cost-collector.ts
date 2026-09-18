import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Annotations, App, Stack } from "aws-cdk-lib";
import { InvalidArgumentError, type Command } from "commander";
import { EC2Client, DescribeSubnetsCommand, DescribeRouteTablesCommand, type Subnet, type RouteTable } from "@aws-sdk/client-ec2";
import { CostCollectorStack, type CostCollectorNetwork, type CostCollectorSettings } from "../../cdk/stacks/cost-collector.ts";
import { DATADOG_AGENT_IMAGE, requireDigestPinnedImage } from "../../config/datadog-agent.ts";
import { awsClientOptions } from "../aws-client-options.ts";
import { cdkBin, CDK_DEPLOY_CHANGE_SET_NAME, ChangeSetRefused, evaluateChangeSet, ExitWithCode, type Deps } from "../cdk-invoker.ts";

interface TargetOptions {
  awsRegion: string;
  awsProfile?: string;
  stackName: string;
}

export interface CostCollectorOptions extends TargetOptions, CostCollectorSettings {
  subnetIds: string[];
  allowReplacement: string[];
}

export type NetworkReader = (options: TargetOptions & { subnetIds: string[] }) => Promise<CostCollectorNetwork>;

export function collectorNetwork(subnetIds: string[], subnets: Subnet[], tables: RouteTable[]): CostCollectorNetwork {
  const selected = subnetIds.map((id) => {
    const subnet = subnets.find((candidate) => candidate.SubnetId === id);
    if (subnet?.VpcId === undefined) throw new Error(`subnet ${id} was not found`);
    return subnet;
  });
  const vpcId = selected[0]?.VpcId;
  if (vpcId === undefined || selected.some((subnet) => subnet.VpcId !== vpcId)) {
    throw new Error("subnets must belong to one VPC");
  }
  const publicFlags = selected.map((subnet) => {
    const table = tables.find((candidate) => candidate.Associations?.some((a) => a.SubnetId === subnet.SubnetId))
      ?? tables.find((candidate) => candidate.VpcId === vpcId && candidate.Associations?.some((a) => a.Main));
    if (table === undefined) throw new Error(`no route table for ${subnet.SubnetId}`);
    return table.Routes?.some((route) => route.DestinationCidrBlock === "0.0.0.0/0"
      && route.GatewayId?.startsWith("igw-") && route.State === "active") ?? false;
  });
  if (publicFlags.some((value) => value !== publicFlags[0])) {
    throw new Error("subnets must be all public or all private");
  }
  return { vpcId, subnetIds, publicSubnets: publicFlags[0] };
}

export const readCollectorNetwork: NetworkReader = async (options) => {
  const client = new EC2Client(await awsClientOptions(options.awsRegion, options.awsProfile));
  try {
    const { Subnets = [] } = await client.send(new DescribeSubnetsCommand({ SubnetIds: options.subnetIds }));
    const vpcIds = [...new Set(Subnets.flatMap((subnet) => subnet.VpcId === undefined ? [] : [subnet.VpcId]))];
    if (vpcIds.length !== 1) throw new Error("subnets must belong to one VPC");
    const tables: RouteTable[] = [];
    let token: string | undefined;
    do {
      const page = await client.send(new DescribeRouteTablesCommand({
        Filters: [{ Name: "vpc-id", Values: vpcIds }], NextToken: token,
      }));
      tables.push(...page.RouteTables ?? []);
      token = page.NextToken;
    } while (token !== undefined);
    return collectorNetwork(options.subnetIds, Subnets, tables);
  } finally {
    client.destroy();
  }
};

export async function deployCostCollector(deps: Deps, options: CostCollectorOptions, readNetwork: NetworkReader): Promise<void> {
  requireDigestPinnedImage(options.agentImage, "agent image");
  if (options.awsRegion.startsWith("us-gov-") || options.awsRegion.startsWith("cn-")) {
    throw new Error("Cost Explorer requires a commercial AWS region");
  }
  const network = await readNetwork(options);
  const directory = mkdtempSync(join(tmpdir(), "idea-cost-collector-"));
  try {
    const app = new App({ outdir: directory });
    new CostCollectorStack(app, options.stackName, {
      ...options, ...network, env: { account: await deps.accountId(), region: options.awsRegion },
    });
    app.synth();
    await invoke(deps, options, directory, ["deploy", "--method=prepare-change-set", "--require-approval", "never"]);
    const request = { StackName: options.stackName, ChangeSetName: CDK_DEPLOY_CHANGE_SET_NAME };
    const description = await deps.cfn.describeChangeSet(request);
    while (description.NextToken !== undefined) {
      const page = await deps.cfn.describeChangeSet({ ...request, NextToken: description.NextToken });
      description.Changes = [...description.Changes ?? [], ...page.Changes ?? []];
      description.NextToken = page.NextToken;
    }
    const verdict = evaluateChangeSet(description, options.allowReplacement);
    if (verdict.refusals.length > 0) {
      const reasons = verdict.refusals.map((finding) => `${finding.reason}; use --allow-replacement ${finding.logicalId}`).join("\n");
      throw new ChangeSetRefused(reasons, verdict);
    }
    if (verdict.empty) {
      deps.out(`${options.stackName}: no changes`);
      return;
    }
    await deps.cfn.executeChangeSet({ ...request, DisableRollback: false });
    for (;;) {
      const stack = await deps.cfn.describeStack(options.stackName);
      if (stack.StackStatus?.endsWith("_IN_PROGRESS")) {
        await deps.sleep(5000);
        continue;
      }
      if (!["CREATE_COMPLETE", "UPDATE_COMPLETE"].includes(stack.StackStatus ?? "")) {
        throw new Error(`${options.stackName}: ${stack.StackStatus}: ${stack.StackStatusReason ?? ""}`);
      }
      deps.out(`${options.stackName}: ${stack.StackStatus}`);
      return;
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

async function invoke(deps: Deps, options: TargetOptions, directory: string, args: string[]): Promise<void> {
  const argv = [cdkBin(), ...args, options.stackName, "--app", directory];
  if (options.awsProfile !== undefined) argv.push("--profile", options.awsProfile);
  const code = await deps.spawn(argv, {
    cwd: directory,
    env: { ...process.env, AWS_DEFAULT_REGION: options.awsRegion, AWS_REGION: options.awsRegion },
  });
  if (code !== 0) throw new ExitWithCode(code);
}

export async function destroyCostCollector(deps: Deps, options: TargetOptions & { force?: boolean }): Promise<void> {
  if (!options.force && await deps.prompt({ message: `Delete cost collector stack ${options.stackName}?`, default: false }) !== true) return;
  const directory = mkdtempSync(join(tmpdir(), "idea-cost-collector-"));
  try {
    // Destroy needs only the stack identity, so expired image references and removed subnets
    // cannot prevent the operator from removing a collector that no longer runs.
    const app = new App({ outdir: directory });
    const stack = new Stack(app, options.stackName, { env: { account: await deps.accountId(), region: options.awsRegion } });
    Annotations.of(stack).acknowledgeWarning("CloudFormation-Validate::F0001", "Destroy uses the stack identity and never deploys this empty template.");
    app.synth();
    await invoke(deps, options, directory, ["destroy", "--force"]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

function positiveInteger(value: string): number {
  if (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(Number(value))) {
    throw new InvalidArgumentError("must be a positive integer");
  }
  return Number(value);
}

export function registerCostCollectorCommands(program: Command, deps: Deps, readNetwork = readCollectorNetwork): void {
  const group = program.command("cost-collector").description("Deploy or remove account spend collection without a cluster");
  const target = (name: string) => group.command(name)
    .requiredOption("--aws-region <region>", "Commercial AWS region")
    .option("--aws-profile <profile>", "AWS profile")
    .requiredOption("--stack-name <name>", "Collector stack name");
  target("deploy")
    .requiredOption("--cluster-name <name>", "idea_cluster metric tag value")
    .requiredOption("--control-plane-image <image>", "Control-plane image")
    .option("--agent-image <image>", "Digest-pinned Datadog agent image. Default: the release's official public ECR image", DATADOG_AGENT_IMAGE)
    .requiredOption("--datadog-api-key-secret-arn <arn>", "Datadog API key secret ARN")
    .requiredOption("--subnet-ids <ids...>", "Subnets in one VPC, all public or all private")
    .option("--interval-hours <hours>", "Collection interval", positiveInteger, 6)
    .option("--lookback-days <days>", "Trailing full days", positiveInteger, 3)
    .option("--module-tag <key>", "Module cost allocation tag", "idea:ModuleId")
    .option("--project-tag <key>", "Project cost allocation tag", "idea:Project")
    .option("--owner-tag <key>", "Owner cost allocation tag", "idea:JobOwner")
    .option("--by-account", "Collect linked account spend", false)
    .option("--allow-replacement <logical-id>", "Accept replacement of this resource", (value: string, previous: string[]) => [...previous, value], [])
    .action(async (options: CostCollectorOptions) => deployCostCollector(deps, options, readNetwork));
  target("destroy")
    .option("--force", "Skip deletion confirmation")
    .action(async (options: TargetOptions & { force?: boolean }) => destroyCostCollector(deps, options));
}
