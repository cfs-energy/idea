/**
 * Every role's inline policies together stay under IAM's 10,240-byte limit, and every
 * customer-managed policy under its 6,144-character limit, with headroom.
 *
 * Only CloudFormation used to find the limit, at deploy time: the scheduler stack rolled back with
 * "Maximum policy size of 10240 bytes exceeded" once the image pipeline grants joined an inline
 * policy that was already near it, and nothing offline had measured it. IAM does not count
 * whitespace, so the measure is the minified document with its intrinsics resolved to values of
 * the length a deployed template holds; a reference to another resource stands in as an
 * 80-character ARN.
 *
 * The stacks come from the day-zero rehearsal of the container-cluster values file, so the test
 * runs without captured fixtures. The thresholds leave room for a cluster whose features add the
 * statements the sample does not turn on.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, describe, it } from "node:test";

import { rehearseDayZero } from "../../tools/day-zero/rehearse.ts";

const VALUES = join(import.meta.dirname, "day-zero-new-cluster.values.yml");
/** IAM allows 10,240 bytes across all inline policies of one role. */
const INLINE_LIMIT = 9_000;
/** IAM allows 6,144 characters in one managed policy. */
const MANAGED_LIMIT = 6_000;

type Json = Record<string, any>;

const PLACEHOLDER = "x".repeat(80);
const PSEUDO: Record<string, string> = {
  "AWS::AccountId": "123456789012",
  "AWS::NoValue": "",
  "AWS::Partition": "aws",
  "AWS::Region": "us-east-2",
  "AWS::URLSuffix": "amazonaws.com",
};

/** The document as IAM stores it: intrinsics resolved to deployed-length values. */
function resolve(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(resolve);
  if (value === null || typeof value !== "object") return value;
  const entries = Object.entries(value as Json);
  if (entries.length === 1) {
    const [key, arg] = entries[0]!;
    if (key === "Ref") return PSEUDO[arg as string] ?? PLACEHOLDER;
    if (key === "Fn::Join") return (resolve(arg[1]) as string[]).join(arg[0]);
    if (key === "Fn::Sub") {
      const text = String(Array.isArray(arg) ? arg[0] : arg);
      return text.replace(/\$\{[^}]+\}/g, (name) => PSEUDO[name.slice(2, -1)] ?? PLACEHOLDER);
    }
    if (key.startsWith("Fn::")) return PLACEHOLDER;
  }
  return Object.fromEntries(entries.map(([k, v]) => [k, resolve(v)]));
}

const size = (document: unknown): number => JSON.stringify(resolve(document)).length;

const inline = new Map<string, number>();
const managed = new Map<string, number>();

/** Adds the template's inline bytes per role and its managed policy sizes, keyed by construct path. */
function measure(template: Json): void {
  const resources = template.Resources as Record<string, Json>;
  const pathOf = (id: string): string => String(resources[id]?.Metadata?.["aws:cdk:path"] ?? id);
  const addInline = (roleId: string, bytes: number): void => {
    inline.set(pathOf(roleId), (inline.get(pathOf(roleId)) ?? 0) + bytes);
  };
  for (const [id, resource] of Object.entries(resources)) {
    if (resource.Type === "AWS::IAM::Role") {
      for (const policy of resource.Properties?.Policies ?? []) addInline(id, size(policy.PolicyDocument));
    } else if (resource.Type === "AWS::IAM::Policy") {
      for (const role of resource.Properties.Roles ?? []) {
        if (typeof role.Ref === "string") addInline(role.Ref, size(resource.Properties.PolicyDocument));
      }
    } else if (resource.Type === "AWS::IAM::ManagedPolicy") {
      managed.set(pathOf(id), size(resource.Properties.PolicyDocument));
    }
  }
}

describe("IAM policy sizes in the synthesized stacks", () => {
  const workDir = mkdtempSync(join(tmpdir(), "ideactl-iam-size-"));
  const synthesized: string[] = [];
  // Every CDK app the rehearsal builds writes its tree file from a beforeExit hook, one hook per
  // stack, so the directory has to outlive the tests and the hook count exceeds the default cap.
  process.setMaxListeners(0);
  process.on("exit", () => rmSync(workDir, { recursive: true, force: true }));

  before(async () => {
    const report = await rehearseDayZero({ valuesFile: VALUES, workDir });
    for (const stack of report.stacks) {
      if (stack.status !== "SYNTHESIZED") continue;
      const outdir = join(workDir, `cdk.out.${stack.moduleId}`);
      const file = readdirSync(outdir).find((name) => name.endsWith(".template.json"));
      assert.ok(file !== undefined, `${stack.moduleId}: no template in ${outdir}`);
      measure(JSON.parse(readFileSync(join(outdir, file), "utf8")) as Json);
      synthesized.push(stack.moduleId);
    }
  });

  it("measured the scheduler and desktop controller task roles", () => {
    assert.ok(synthesized.includes("scheduler") && synthesized.includes("vdc"), synthesized.join(","));
    for (const role of [/scheduler-?task-?role/, /controller-?task-?role/]) {
      assert.ok([...inline.keys()].some((path) => role.test(path)), `${role} was not measured`);
    }
  });

  it(`keeps every role's inline policies under ${INLINE_LIMIT} bytes together`, () => {
    const over = [...inline].filter(([, bytes]) => bytes >= INLINE_LIMIT).map(([path, bytes]) => `${path}: ${bytes}`);
    assert.deepEqual(over, []);
  });

  it(`keeps every managed policy under ${MANAGED_LIMIT} characters`, () => {
    const over = [...managed].filter(([, chars]) => chars >= MANAGED_LIMIT).map(([path, chars]) => `${path}: ${chars}`);
    assert.deepEqual(over, []);
  });
});
