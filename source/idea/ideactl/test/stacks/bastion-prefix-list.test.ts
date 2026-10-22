import assert from "node:assert/strict";
import test from "node:test";
import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import type { IdeaContext } from "../../src/cdk/constructs/base.ts";
import { BastionHostSecurityGroup, ExternalLoadBalancerSecurityGroup } from "../../src/cdk/constructs/network.ts";

function build(bastionPrefixListId?: string) {
  const stack = new Stack(new App(), "cluster", { env: { account: "123456789012", region: "us-east-2" } });
  const config = new ClusterConfig(Object.entries({
    "cluster.aws.partition": "aws",
    "cluster.network.prefix_list_ids": ["pl-11111111111111111"],
    "directoryservice.provider": "openldap",
  }).map(([key, value]) => ({ key, value })));
  const ctx = { config, clusterName: "sample", awsRegion: "us-east-2" } as IdeaContext;
  const vpc = new ec2.Vpc(stack, "vpc", { maxAzs: 1, natGateways: 0 });
  const clusterList = new ec2.CfnPrefixList(stack, "cluster-prefix-list", { addressFamily: "IPv4", maxEntries: 10, prefixListName: "sample-prefix-list" });
  const bastion = new BastionHostSecurityGroup(ctx, "bastion", stack, vpc, clusterList.attrPrefixListId, bastionPrefixListId);
  const external = new ExternalLoadBalancerSecurityGroup(ctx, "external", stack, vpc, clusterList.attrPrefixListId, bastion);
  const template = Template.fromStack(stack);
  const groupId = (group: ec2.SecurityGroup) => stack.getLogicalId(group.node.defaultChild as ec2.CfnSecurityGroup);
  // every ingress rule on a group, inline or standalone, as [port, source]
  const ingress = (group: ec2.SecurityGroup) => {
    const inline = template.findResources("AWS::EC2::SecurityGroup")[groupId(group)]!.Properties.SecurityGroupIngress ?? [];
    const standalone = Object.values(template.findResources("AWS::EC2::SecurityGroupIngress"))
      .map((resource) => resource.Properties)
      .filter((rule) => JSON.stringify(rule.GroupId).includes(groupId(group)));
    return [...inline, ...standalone].map((rule) => [rule.FromPort, rule.SourcePrefixListId ?? rule.CidrIp ?? "group"]);
  };
  return { bastion: ingress(bastion), external: ingress(external), clusterListRef: { "Fn::GetAtt": [stack.getLogicalId(clusterList), "PrefixListId"] } };
}

test("ssh_client_ip moves bastion SSH onto its own prefix list and leaves the portal alone", () => {
  const { bastion, external, clusterListRef } = build("pl-bbbbbbbbbbbbbbbbb");
  const ssh = bastion.filter(([port]) => port === 22).map(([, source]) => JSON.stringify(source));
  assert.equal(ssh.length, 2);
  assert.ok(ssh.includes(JSON.stringify("pl-bbbbbbbbbbbbbbbbb")));
  assert.ok(!ssh.includes(JSON.stringify("pl-11111111111111111")));
  assert.ok(!ssh.includes(JSON.stringify(clusterListRef)));
  const portal = external.filter(([port]) => port === 443).map(([, source]) => JSON.stringify(source));
  assert.ok(portal.includes(JSON.stringify(clusterListRef)));
  assert.ok(portal.includes(JSON.stringify("pl-11111111111111111")));
});

test("without ssh_client_ip the bastion keeps the cluster and operator prefix lists", () => {
  const { bastion, clusterListRef } = build();
  const ssh = bastion.filter(([port]) => port === 22).map(([, source]) => JSON.stringify(source));
  assert.ok(ssh.includes(JSON.stringify(clusterListRef)));
  assert.ok(ssh.includes(JSON.stringify("pl-11111111111111111")));
});
