/**
 * The DCV broker's client and gateway listeners admit only the controller and the gateway, the
 * broker tasks take those ports only from the internal load balancer, and the gateway target group
 * sees client addresses so the prefix lists apply. Runs on the day-zero rehearsal, no captures.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";

import { buildApp } from "../../src/cdk/app.ts";
import { IdeaCodeAsset } from "../../src/cdk/code-asset.ts";
import { buildStack } from "../../src/cdk/stacks/cluster.ts";
import { buildStack as buildVdcStack } from "../../src/cdk/stacks/vdc.ts";
import type { StackBuildProps } from "../../src/cdk/app.ts";
import type { ModuleInfo } from "../../src/config/cluster-config.ts";
import { rehearseDayZero } from "../../tools/day-zero/rehearse.ts";

type Json = Record<string, any>;
const VALUES = join(import.meta.dirname, "day-zero-new-cluster.values.yml");
const CLIENT = 8444;
const AGENT = 8445;
const GATEWAY = 8446;

describe("DCV broker listener and gateway ingress", () => {
  const workDir = mkdtempSync(join(tmpdir(), "ideactl-broker-ingress-"));
  const templates = new Map<string, Json>();
  process.setMaxListeners(0);
  process.on("exit", () => rmSync(workDir, { recursive: true, force: true }));

  before(async () => {
    const report = await rehearseDayZero({ valuesFile: VALUES, workDir, emitInputs: join(workDir, "inputs"), completeReads: true });
    for (const stack of report.stacks) {
      if (stack.status !== "SYNTHESIZED") continue;
      const outdir = join(workDir, `cdk.out.${stack.moduleId}`);
      const file = readdirSync(outdir).find((name) => name.endsWith(".template.json"))!;
      templates.set(stack.moduleId, JSON.parse(readFileSync(join(outdir, file), "utf8")));
    }
  });

  const resources = (module: string): Json[] => Object.values(templates.get(module)?.Resources ?? {});
  const vpcRule = (rule: Json) => rule.CidrIp !== undefined;

  /** One module's template, built from the rehearsal's inputs after `mutate` edits the config. */
  async function synth(moduleId: string, moduleName: string, build: (props: StackBuildProps) => unknown, mutate: (props: StackBuildProps) => void): Promise<Json> {
    const inputs = join(workDir, "inputs", moduleId);
    const outdir = join(workDir, `gate-${Math.random().toString(36).slice(2)}`);
    mkdirSync(outdir);
    const originalAssetPath = IdeaCodeAsset.prototype.assetPath;
    const previous = { cwd: process.cwd(), out: process.env.CDK_OUTDIR, nag: process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN };
    try {
      IdeaCodeAsset.prototype.assetPath = () => join(workDir, "empty-lambda-asset");
      mkdirSync(join(workDir, "empty-lambda-asset"), { recursive: true });
      process.chdir(workDir);
      process.env.CDK_OUTDIR = outdir;
      process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = "false";
      const app = await buildApp({
        clusterName: "idea-test1", awsRegion: "us-east-2", moduleId, moduleName,
        deploymentId: "test", terminationProtection: true,
        configFile: join(inputs, "cluster-settings.json"), synthReadsFile: join(inputs, "synth-reads.json"),
      }, { [moduleName]: async () => (props: StackBuildProps) => { mutate(props); return build(props); } } as never);
      return app.synth().getStackByName(`idea-test1-${moduleId}`).template as Json;
    } finally {
      IdeaCodeAsset.prototype.assetPath = originalAssetPath;
      process.chdir(previous.cwd);
      if (previous.out === undefined) delete process.env.CDK_OUTDIR; else process.env.CDK_OUTDIR = previous.out;
      if (previous.nag === undefined) delete process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN; else process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = previous.nag;
    }
  }

  /** The VPC-wide broker ports on the internal load balancer of a cluster stack built in `state`. */
  async function clusterBrokerPorts(state: { modules?: readonly Partial<ModuleInfo>[]; rules?: string[] }): Promise<number[]> {
    const template = await synth("cluster", "cluster", buildStack, (props) => {
      // `--config-file` loads no module rows; the other states put the controller's row in place
      if (state.modules !== undefined) (props.ctx.config as unknown as { moduleList: readonly Partial<ModuleInfo>[] }).moduleList = state.modules;
      if (state.rules !== undefined) props.ctx.config.setEntry("vdc.dcv_broker.listener_ingress_rules", state.rules);
    });
    const group = Object.values(template.Resources as Record<string, Json>).find((resource) => resource.Type === "AWS::EC2::SecurityGroup"
      && resource.Properties.GroupDescription === "Internal load balancer security group")!;
    return (group.Properties.SecurityGroupIngress as Json[])
      .filter((rule) => [CLIENT, AGENT, GATEWAY].includes(rule.FromPort) && vpcRule(rule)).map((rule) => rule.FromPort).sort();
  }

  /** Every ingress source of the security group described as `description`, inline or separate, by class. */
  function sources(template: Json, description: string): Set<string> {
    const resources = template.Resources as Record<string, Json>;
    const [id, group] = Object.entries(resources).find(([, resource]) => resource.Type === "AWS::EC2::SecurityGroup"
      && resource.Properties.GroupDescription === description)!;
    const rules = [...(group.Properties.SecurityGroupIngress ?? []), ...Object.values(resources)
      .filter((resource) => resource.Type === "AWS::EC2::SecurityGroupIngress" && JSON.stringify(resource.Properties.GroupId).includes(id))
      .map((resource) => resource.Properties)] as Json[];
    const found = new Set<string>();
    for (const rule of rules) {
      const text = JSON.stringify(rule);
      if (rule.SourcePrefixListId !== undefined) found.add(/ClusterPrefixList|cluster_prefix_list|Cluster Prefix List/.test(text) ? "cluster prefix list" : `prefix list ${JSON.stringify(rule.SourcePrefixListId)}`);
      else if (/EIP|NAT/.test(text)) found.add("NAT gateway addresses");
      else if (rule.SourceSecurityGroupId !== undefined && /bastion/i.test(text)) found.add("bastion host");
    }
    return found;
  }

  for (const dropAllow of [true, false]) {
    it(`the WAF log and sampled requests carry no bearer token or cookie (drop_allow_actions ${dropAllow})`, async () => {
      const template = await synth("cluster", "cluster", buildStack, (props) =>
        props.ctx.config.setEntry("cluster.load_balancers.external_alb.waf.logging.drop_allow_actions", dropAllow));
      const all = Object.values(template.Resources as Record<string, Json>);
      const acls = all.filter((resource) => resource.Type === "AWS::WAFv2::WebACL");
      const logging = all.filter((resource) => resource.Type === "AWS::WAFv2::LoggingConfiguration");
      assert.ok(acls.length > 0 && logging.length === acls.length);
      for (const acl of acls) {
        assert.deepEqual(acl.Properties.DataProtectionConfig.DataProtections.map((entry: Json) => entry.Field.FieldKeys[0]).sort(), ["authorization", "cookie"]);
      }
      for (const config of logging) {
        assert.deepEqual(config.Properties.RedactedFields.map((field: Json) => field.SingleHeader.Name).sort(), ["authorization", "cookie"]);
      }
    });
  }

  it("a host site hook checksum that is not empty or 64 hex characters stops the module deploy", async () => {
    const { buildStack: buildSchedulerStack } = await import("../../src/cdk/stacks/scheduler.ts");
    for (const [moduleId, moduleName, build, key] of [
      ["scheduler", "scheduler", buildSchedulerStack, "scheduler.compute_node_site_hook_sha256"],
      ["scheduler", "scheduler", buildSchedulerStack, "scheduler.ami_builder_site_hook_sha256"],
      ["vdc", "virtual-desktop-controller", buildVdcStack, "virtual-desktop-controller.dcv_host_site_hook_sha256"],
      ["vdc", "virtual-desktop-controller", buildVdcStack, "virtual-desktop-controller.ami_builder_site_hook_sha256"],
    ] as const) {
      // rows are stored under the module id; the stack reads them by module name
      const stored = key.replace("virtual-desktop-controller.", "vdc.");
      await assert.rejects(synth(moduleId, moduleName, build as never, (props) => props.ctx.config.setEntry(stored, "abc")), new RegExp(`${key} must be empty or a 64-character hex SHA-256`));
    }
    // a valid checksum, upper case included, synthesizes
    await synth("vdc", "virtual-desktop-controller", buildVdcStack, (props) => props.ctx.config.setEntry("vdc.dcv_host_site_hook_sha256", "A".repeat(64)));
  });

  it("the gateway admits every source class the portal's public load balancer admits", async () => {
    const extra = (props: StackBuildProps) => props.ctx.config.setEntry("cluster.network.prefix_list_ids", ["pl-0123456789abcdef0"]);
    const cluster = await synth("cluster", "cluster", buildStack, extra);
    const vdcTemplate = await synth("vdc", "virtual-desktop-controller", buildVdcStack, (props) => {
      extra(props);
      props.ctx.config.setEntry("cluster.network.nat_gateway_ips", ["198.51.100.7", "198.51.100.8"]);
    });
    const portal = sources(cluster, "External Application Load Balancer security group");
    const gateway = sources(vdcTemplate, "Security Group for Virtual Desktop DCV Connection Gateway");
    assert.ok(portal.has("NAT gateway addresses"), [...portal].join(", "));
    // the bastion reaches the portal through the public load balancer; it reaches desktops directly
    portal.delete("bastion host");
    assert.deepEqual([...portal].filter((source) => !gateway.has(source)), []);
    const nat = Object.values(vdcTemplate.Resources as Record<string, Json>).flatMap((resource) => resource.Type === "AWS::EC2::SecurityGroup" ? (resource.Properties.SecurityGroupIngress ?? []) as Json[] : [])
      .filter((rule) => /NAT gateways/.test(String(rule.Description))).map((rule) => `${rule.CidrIp} ${rule.IpProtocol}/${rule.FromPort}`).sort();
    assert.deepEqual(nat, ["198.51.100.7/32 tcp/8443", "198.51.100.8/32 tcp/8443"]);
  });

  const vdc = (version: string, status = "deployed"): Partial<ModuleInfo> => ({ module_id: "vdc", name: "virtual-desktop-controller", type: "app", status, version });


  // The rehearsal itself synthesizes from a settings file with no module rows, which keeps the wide
  // rule (the `cdk --config-file` case below); a fresh install loads the rows, all not deployed.
  it("a new cluster's internal load balancer admits the VPC only on the agent port among the broker ports", async () => {
    assert.deepEqual(await clusterBrokerPorts({ modules: [vdc("", "not-deployed")] }), [AGENT]);
  });
  for (const [state, setup] of [
    ["no module information (cdk --config-file)", {}],
    ["26.11.1 controller deployed", { modules: [vdc("26.11.1")] }],
    ["a 26.11.2 candidate controller without the rules", { modules: [vdc("26.11.2")] }],
    ["a controller stack that rolled back (its rollback removed the row)", { modules: [vdc("26.11.1")] }],
  ] as const) {
    it(`the cluster stack keeps the VPC-wide broker rules: ${state}`, async () => {
      assert.deepEqual(await clusterBrokerPorts(setup), [CLIENT, AGENT, GATEWAY]);
    });
  }

  it("the text the upgrade reads back is in the cluster template exactly while the wide rule is", async () => {
    const { WIDE_BROKER_CLIENT_RULE } = await import("../../src/cdk/broker-rules.ts");
    const template = (state: { modules?: readonly Partial<ModuleInfo>[]; rules?: string[] }) => synth("cluster", "cluster", buildStack, (props) => {
      if (state.modules !== undefined) (props.ctx.config as unknown as { moduleList: readonly Partial<ModuleInfo>[] }).moduleList = state.modules;
      if (state.rules !== undefined) props.ctx.config.setEntry("vdc.dcv_broker.listener_ingress_rules", state.rules);
    });
    assert.ok(JSON.stringify(await template({ modules: [vdc("26.11.1")] })).includes(WIDE_BROKER_CLIENT_RULE));
    assert.ok(!JSON.stringify(await template({ modules: [vdc("26.11.2")], rules: ["sgr-1", "sgr-2"] })).includes(WIDE_BROKER_CLIENT_RULE));
  });

  it("the cluster stack drops them only once the controller stack has published its rules", async () => {
    assert.deepEqual(await clusterBrokerPorts({ modules: [vdc("26.11.2")], rules: ["sgr-1", "sgr-2"] }), [AGENT]);
  });

  it("the controller and the gateway receive the internal load balancer's certificate to verify the broker", () => {
    const definitions = resources("vdc").filter((resource) => resource.Type === "AWS::ECS::TaskDefinition")
      .flatMap((resource) => resource.Properties.ContainerDefinitions as Json[]);
    for (const name of ["controller-container", "dcv-connection-gateway-container"]) {
      const container = definitions.find((definition) => definition.Name === name)!;
      const secret = (container.Secrets as Json[]).find((entry) => entry.Name === "IDEA_INTERNAL_ALB_CERTIFICATE_PEM");
      assert.ok(secret, name);
      assert.match(JSON.stringify(secret.ValueFrom), /:secret:idea-test1-internal-certificate-/);
    }
  });

  it("the controller stack publishes the rule ids from the rules themselves", () => {
    const settings = resources("vdc").find((resource) => resource.Type === "Custom::ClusterSettings")!;
    const published = settings.Properties.settings["dcv_broker.listener_ingress_rules"] as Json[];
    const rules = Object.entries(templates.get("vdc")!.Resources as Record<string, Json>)
      .filter(([, resource]) => resource.Type === "AWS::EC2::SecurityGroupIngress" && /DCV broker listener/.test(String(resource.Properties.Description)))
      .map(([id]) => ({ Ref: id }));
    assert.deepEqual(published, rules);
  });

  it("the desktop controller stack admits the controller to the client listener and the gateway to the gateway listener", () => {
    const rules = resources("vdc").filter((resource) => resource.Type === "AWS::EC2::SecurityGroupIngress"
      && /DCV broker listener/.test(String(resource.Properties.Description)));
    assert.deepEqual(rules.map((rule) => rule.Properties.FromPort).sort(), [CLIENT, GATEWAY]);
    const groupId = (description: string) => {
      const entry = Object.entries(templates.get("vdc")!.Resources as Record<string, Json>).find(([, resource]) =>
        resource.Type === "AWS::EC2::SecurityGroup" && resource.Properties.GroupDescription === description)!;
      return { "Fn::GetAtt": [entry[0], "GroupId"] };
    };
    const byPort = new Map(rules.map((rule) => [rule.Properties.FromPort, rule.Properties]));
    assert.deepEqual(byPort.get(CLIENT)!.SourceSecurityGroupId, groupId("Security Group for Virtual Desktop Controller"));
    assert.deepEqual(byPort.get(GATEWAY)!.SourceSecurityGroupId, groupId("Security Group for Virtual Desktop DCV Connection Gateway"));
    for (const rule of rules) assert.equal(rule.Properties.CidrIp, undefined);
    // and the broker tasks take those two ports from the internal load balancer's group
    const broker = groupId("Security Group for Virtual Desktop DCV Broker");
    const fromBalancer = resources("vdc").filter((resource) => resource.Type === "AWS::EC2::SecurityGroupIngress"
      && JSON.stringify(resource.Properties.GroupId) === JSON.stringify(broker) && /internal load balancer/.test(String(resource.Properties.Description)));
    assert.deepEqual(fromBalancer.map((rule) => rule.Properties.FromPort).sort(), [CLIENT, GATEWAY]);
  });

  it("the broker tasks take the client and gateway ports only from the internal load balancer", () => {
    const group = resources("vdc").find((resource) => resource.Type === "AWS::EC2::SecurityGroup"
      && resource.Properties.GroupDescription === "Security Group for Virtual Desktop DCV Broker")!;
    const open = (group.Properties.SecurityGroupIngress as Json[]).filter(vpcRule)
      .filter((rule) => rule.FromPort <= GATEWAY && rule.ToPort >= CLIENT).map((rule) => [rule.FromPort, rule.ToPort]);
    assert.deepEqual(open, [[AGENT, AGENT]]);
  });

  it("the gateway target group preserves the client address when it carries TCP", () => {
    const groups = resources("vdc").filter((resource) => resource.Type === "AWS::ElasticLoadBalancingV2::TargetGroup"
      && resource.Properties.Port === 8443 && resource.Properties.TargetType === "ip" && resource.Properties.Protocol === "TCP");
    assert.equal(groups.length, 1);
    assert.ok((groups[0]!.Properties.TargetGroupAttributes as Json[])
      .some((attribute) => attribute.Key === "preserve_client_ip.enabled" && attribute.Value === "true"));
  });

});

describe("service stacks publish their own OpenSearch rule", () => {
  const workDir = mkdtempSync(join(tmpdir(), "ideactl-opensearch-rule-"));
  const templates = new Map<string, Json>();
  process.on("exit", () => rmSync(workDir, { recursive: true, force: true }));
  before(async () => {
    // complete reads fill the analytics group id, so each service builds its rule
    const report = await rehearseDayZero({ valuesFile: VALUES, workDir, emitInputs: join(workDir, "inputs"), completeReads: true });
    for (const stack of report.stacks) {
      if (stack.status !== "SYNTHESIZED") continue;
      const outdir = join(workDir, `cdk.out.${stack.moduleId}`);
      const file = readdirSync(outdir).find((name) => name.endsWith(".template.json"))!;
      templates.set(stack.moduleId, JSON.parse(readFileSync(join(outdir, file), "utf8")));
    }
  });
  for (const module of ["cluster-manager", "scheduler", "vdc"]) {
    it(`${module} writes the id of its OpenSearch rule, so an analytics upgrade can tell the rule exists`, () => {
      const resources = templates.get(module)!.Resources as Record<string, Json>;
      const rule = Object.entries(resources).find(([, resource]) => resource.Type === "AWS::EC2::SecurityGroupIngress"
        && /to OpenSearch/.test(String(resource.Properties.Description)))!;
      const settings = Object.values(resources).find((resource) => resource.Type === "Custom::ClusterSettings")!;
      assert.deepEqual(settings.Properties.settings.opensearch_ingress_rule, { Ref: rule[0] });
    });
  }
});

describe("the stored form of the published broker listener rule ids", () => {
  it("the settings handler stores the list as written, the gate reads it back, and a rollback deletes it", async () => {
    const { createHandler } = await import("../../src/lambda/idea_custom_resource_update_cluster_settings/index.ts");
    const { UpdateCommand, DeleteCommand } = await import("@aws-sdk/lib-dynamodb");
    const { marshall } = await import("@aws-sdk/util-dynamodb");
    const { ClusterConfig } = await import("../../src/config/cluster-config.ts");
    const { vpcWideBrokerRuleNeeded } = await import("../../src/cdk/stacks/cluster.ts");
    const table = new Map<string, unknown>();
    const dynamoDb = { send: async (command: InstanceType<typeof UpdateCommand> | InstanceType<typeof DeleteCommand>) => {
      const key = String(command.input.Key?.["key"] ?? "");
      if (command instanceof DeleteCommand) table.delete(key);
      else if (command.input.TableName?.endsWith(".cluster-settings")) table.set(key, command.input.ExpressionAttributeValues?.[":value"]);
      return {};
    } };
    const handler = createHandler({ dynamoDb: () => dynamoDb as never, responseSender: async () => {}, logger: { info() {}, error() {} } as never });
    const event = (requestType: "Create" | "Update", settings: Record<string, unknown>, old?: Record<string, unknown>) => ({
      RequestType: requestType, ResponseURL: "https://example.invalid/r", StackId: "s", RequestId: "r", LogicalResourceId: "l",
      ResourceProperties: { cluster_name: "idea-test1", module_id: "vdc", version: "26.11.2", settings },
      ...(old === undefined ? {} : { OldResourceProperties: { cluster_name: "idea-test1", module_id: "vdc", version: "26.11.2", settings: old } }),
    }) as never;
    // CloudFormation hands the custom resource the two resolved rule ids as a JSON list
    await handler(event("Create", { deployment_id: "d", "dcv_broker.listener_ingress_rules": ["sgr-0a", "sgr-0b"] }), { logStreamName: "x" } as never);
    const config = (rows: Map<string, unknown>) => ClusterConfig.fromFile(JSON.stringify({ Items: [
      ...[...rows].map(([key, value]) => marshall({ key, value }, { removeUndefinedValues: true })),
      marshall({ key: "global-settings.module_sets.default.virtual-desktop-controller.module_id", value: "vdc" }),
    ] }), JSON.stringify({ Items: [marshall({ module_id: "vdc", name: "virtual-desktop-controller", type: "app", status: "deployed", version: "26.11.2" })] }));
    assert.deepEqual(table.get("vdc.dcv_broker.listener_ingress_rules"), ["sgr-0a", "sgr-0b"]);
    assert.equal(vpcWideBrokerRuleNeeded(config(table)), false);
    // a rollback updates back to the old properties, which did not carry the row
    await handler(event("Update", { deployment_id: "d" }, { deployment_id: "d", "dcv_broker.listener_ingress_rules": ["sgr-0a", "sgr-0b"] }), { logStreamName: "x" } as never);
    assert.equal(table.has("vdc.dcv_broker.listener_ingress_rules"), false);
    assert.equal(vpcWideBrokerRuleNeeded(config(table)), true);
  });
});
