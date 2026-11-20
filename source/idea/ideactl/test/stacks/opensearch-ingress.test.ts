/**
 * The OpenSearch domain answers unsigned requests, so the network decides who can use it: the
 * analytics sink in the domain's own group and the three control-plane services, never the whole
 * VPC. Synthesized from the day-zero rehearsal, so it runs without captured fixtures. The upgrade
 * order that keeps the services connected while the VPC-wide rule goes is pinned in
 * test/cli/deployment-helper.test.ts.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";

import { rehearseDayZero } from "../../tools/day-zero/rehearse.ts";

type Json = Record<string, any>;

const VALUES = join(import.meta.dirname, "day-zero-new-cluster.values.yml");

describe("OpenSearch ingress", () => {
  const templates = new Map<string, Json>();
  const workDir = mkdtempSync(join(tmpdir(), "ideactl-opensearch-ingress-"));
  process.setMaxListeners(0);
  process.on("exit", () => rmSync(workDir, { recursive: true, force: true }));

  before(async () => {
    const report = await rehearseDayZero({ valuesFile: VALUES, workDir, completeReads: true });
    for (const stack of report.stacks) {
      if (stack.status !== "SYNTHESIZED") continue;
      const outdir = join(workDir, `cdk.out.${stack.moduleId}`);
      const file = readdirSync(outdir).find((name) => name.endsWith(".template.json"));
      if (file !== undefined) templates.set(stack.moduleId, JSON.parse(readFileSync(join(outdir, file), "utf8")) as Json);
    }
  });

  const resources = (module: string): Array<[string, Json]> => {
    const template = templates.get(module);
    assert.ok(template, `${module} synthesized`);
    return Object.entries(template.Resources as Record<string, Json>);
  };

  it("admits no address range to the OpenSearch group, only the sink's own group", () => {
    const groups = resources("analytics").filter(([id, resource]) =>
      resource.Type === "AWS::EC2::SecurityGroup" && id.includes("opensearchsecuritygroup"));
    assert.equal(groups.length, 1);
    const [groupId, group] = groups[0]!;
    const inline = (group.Properties.SecurityGroupIngress ?? []) as Json[];
    const separate = resources("analytics")
      .filter(([, resource]) => resource.Type === "AWS::EC2::SecurityGroupIngress"
        && JSON.stringify(resource.Properties.GroupId).includes(groupId))
      .map(([, resource]) => resource.Properties as Json);
    const rules = [...inline, ...separate];
    assert.deepEqual(rules.filter((rule) => rule.CidrIp !== undefined || rule.CidrIpv6 !== undefined), []);
    assert.ok(rules.some((rule) => JSON.stringify(rule.SourceSecurityGroupId).includes(groupId) && rule.FromPort === 443));
  });

  for (const [module, groupPath] of [
    ["cluster-manager", "cluster-manager-security-group"],
    ["scheduler", "scheduler-security-group"],
    ["vdc", "vdc-controller-security-group"],
  ] as const) {
    it(`${module} admits its own service to OpenSearch on 443`, () => {
      const rules = resources(module).filter(([, resource]) => resource.Type === "AWS::EC2::SecurityGroupIngress"
        && String(resource.Metadata?.["aws:cdk:path"]).endsWith("/opensearch-ingress"));
      assert.equal(rules.length, 1);
      const rule = rules[0]![1].Properties as Json;
      assert.equal(rule.FromPort, 443);
      assert.equal(rule.ToPort, 443);
      const source = rule.SourceSecurityGroupId["Fn::GetAtt"]?.[0] as string;
      const sourcePath = String(templates.get(module)!.Resources[source]?.Metadata?.["aws:cdk:path"]);
      assert.ok(sourcePath.includes(groupPath), `${module} rule comes from ${sourcePath}`);
    });
  }
});
