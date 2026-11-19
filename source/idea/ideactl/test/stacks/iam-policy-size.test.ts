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
/** IAM attaches at most 10 managed policies to one role (the account default quota). */
const ATTACHED_LIMIT = 10;
/** Room kept for cluster.iam.ec2_managed_policy_arns, as 26.11.1 left it. */
const OPERATOR_SLOTS = 5;

type Json = Record<string, any>;

// ARN-shaped, so a reference used as a Resource passes the grammar check below as its ARN would.
// shortcut: a Ref that yields a name rather than an ARN also passes, add per-type Ref values if one slips through.
const PLACEHOLDER = "arn:aws:placeholder:us-east-2:123456789012:".padEnd(80, "x");
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

/** IAM's offline-checkable grammar: the rules CloudFormation otherwise reports only at deploy time. */
const DOCUMENT_KEYS = new Set(["Version", "Id", "Statement"]);
const STATEMENT_KEYS = new Set(["Sid", "Effect", "Principal", "NotPrincipal", "Action", "NotAction", "Resource", "NotResource", "Condition"]);
const OPERATOR = /^(ForAllValues:|ForAnyValue:)?(StringEquals|StringNotEquals|StringEqualsIgnoreCase|StringNotEqualsIgnoreCase|StringLike|StringNotLike|NumericEquals|NumericNotEquals|NumericLessThan|NumericLessThanEquals|NumericGreaterThan|NumericGreaterThanEquals|DateEquals|DateNotEquals|DateLessThan|DateLessThanEquals|DateGreaterThan|DateGreaterThanEquals|Bool|BinaryEquals|IpAddress|NotIpAddress|ArnEquals|ArnLike|ArnNotEquals|ArnNotLike)(IfExists)?$|^Null$/;
const list = (value: unknown): unknown[] => (value === undefined ? [] : Array.isArray(value) ? value : [value]);

/** Grammar errors in one resolved policy document; `identity` documents name resources, resource policies name principals. */
export function grammarErrors(document: unknown, identity: boolean): string[] {
  const errors: string[] = [];
  if (document === null || typeof document !== "object" || Array.isArray(document)) return ["document is not an object"];
  const doc = document as Json;
  for (const key of Object.keys(doc)) if (!DOCUMENT_KEYS.has(key)) errors.push(`unknown top-level key ${key}`);
  if (doc.Version !== undefined && doc.Version !== "2012-10-17") errors.push(`Version ${doc.Version}`);
  const statements = list(doc.Statement) as Json[];
  if (statements.length === 0) errors.push("no statements");
  const sids = new Set<string>();
  for (const [index, statement] of statements.entries()) {
    const at = `Statement[${index}]${statement.Sid ? ` ${statement.Sid}` : ""}`;
    for (const key of Object.keys(statement)) if (!STATEMENT_KEYS.has(key)) errors.push(`${at}: unknown key ${key}`);
    if (statement.Effect !== "Allow" && statement.Effect !== "Deny") errors.push(`${at}: Effect ${statement.Effect}`);
    if (statement.Sid !== undefined) {
      if (!/^[A-Za-z0-9]*$/.test(statement.Sid)) errors.push(`${at}: Sid is not alphanumeric`);
      if (sids.has(statement.Sid)) errors.push(`${at}: duplicate Sid`);
      sids.add(statement.Sid);
    }
    if ((statement.Action === undefined) === (statement.NotAction === undefined)) errors.push(`${at}: needs exactly one of Action, NotAction`);
    for (const action of list(statement.Action ?? statement.NotAction)) {
      if (action !== "*" && !/^[A-Za-z0-9-]+:[A-Za-z0-9*?]+$/.test(String(action))) errors.push(`${at}: action ${String(action)}`);
    }
    const hasPrincipal = statement.Principal !== undefined || statement.NotPrincipal !== undefined;
    if (identity && hasPrincipal) errors.push(`${at}: Principal in an identity policy`);
    if (!identity && !hasPrincipal) errors.push(`${at}: resource policy statement without Principal`);
    if (identity && (statement.Resource === undefined) === (statement.NotResource === undefined)) errors.push(`${at}: needs exactly one of Resource, NotResource`);
    for (const resource of list(statement.Resource ?? statement.NotResource)) {
      const arn = String(resource);
      if (arn !== "*" && !arn.startsWith("arn:")) errors.push(`${at}: resource ${arn} is neither * nor an ARN`);
      const variable = arn.indexOf("${");
      // a policy variable may stand only in the resource part, after the ARN's fifth colon
      if (variable >= 0 && arn.slice(0, variable).split(":").length - 1 < 5) errors.push(`${at}: policy variable before the resource part of ${arn}`);
    }
    if (statement.Condition !== undefined) {
      for (const [operator, block] of Object.entries(statement.Condition as Json)) {
        if (!OPERATOR.test(operator)) errors.push(`${at}: condition operator ${operator}`);
        if (block === null || typeof block !== "object" || Array.isArray(block)) { errors.push(`${at}: ${operator} is not a key map`); continue; }
        const variablesAllowed = /String|Arn/.test(operator);
        for (const [key, values] of Object.entries(block as Json)) {
          if (!/^[A-Za-z0-9-]+:[^\s]+$/.test(key)) errors.push(`${at}: condition key ${key}`);
          if (!variablesAllowed && list(values).some((value) => String(value).includes("${"))) errors.push(`${at}: policy variable with ${operator}`);
        }
      }
    }
  }
  return errors;
}

const grammar = new Map<string, string[]>();

/** Records the grammar errors of every policy document in the template, keyed by construct path. */
function checkGrammar(template: Json): void {
  const resources = template.Resources as Record<string, Json>;
  const add = (id: string, label: string, document: unknown, identity: boolean): void => {
    const errors = grammarErrors(resolve(document), identity);
    if (errors.length) grammar.set(`${String(resources[id]?.Metadata?.["aws:cdk:path"] ?? id)} ${label}`, errors);
  };
  for (const [id, resource] of Object.entries(resources)) {
    const properties = resource.Properties ?? {};
    if (resource.Type === "AWS::IAM::Role") {
      for (const policy of properties.Policies ?? []) add(id, String(policy.PolicyName), policy.PolicyDocument, true);
      add(id, "trust", properties.AssumeRolePolicyDocument, false);
    } else if (resource.Type === "AWS::IAM::Policy" || resource.Type === "AWS::IAM::ManagedPolicy") {
      add(id, "", properties.PolicyDocument, true);
    } else if (["AWS::S3::BucketPolicy", "AWS::SQS::QueuePolicy", "AWS::SNS::TopicPolicy"].includes(resource.Type)) {
      add(id, "", properties.PolicyDocument, false);
    }
  }
}

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
  const templates = new Map<string, Json>();
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
      const template = JSON.parse(readFileSync(join(outdir, file), "utf8")) as Json;
      templates.set(stack.moduleId, template);
      measure(template);
      checkGrammar(template);
      synthesized.push(stack.moduleId);
    }
  });

  it("S-24: retains only compute host roles and publishes task sender identities", () => {
    for (const module of ["scheduler", "cluster-manager", "vdc"]) {
      const template = templates.get(module);
      assert.ok(template, `${module} synthesized`);
      const resources = Object.values(template.Resources) as Json[];
      const hostRoles = resources.filter((resource) => resource.Type === "AWS::IAM::Role"
        && /ec2\.amazonaws/.test(JSON.stringify(resolve(resource.Properties.AssumeRolePolicyDocument))));
      assert.equal(hostRoles.length, module === "cluster-manager" ? 0 : 1, `${module}: obsolete host role`);
      if (hostRoles.length) {
        assert.match(hostRoles[0]!.Properties.RoleName, module === "scheduler" ? /compute-node/ : /vdc-host/);
      }
      const settings = resources.filter((resource) => resource.Type === "Custom::ClusterSettings");
      assert.equal(settings.length, 1);
      const rows = settings[0]!.Properties.settings;
      assert.ok(rows, "published settings");
      for (const key of ["iam_role_arn", "dcv_broker_role_arn", "dcv_broker_role_name", "controller_iam_role_arn", "controller_iam_role_name"]) {
        assert.ok(!(key in rows), `${module}: obsolete ${key}`);
      }
      if (module === "vdc") {
        for (const key of ["dcv_broker_role_id", "controller_iam_role_id"]) {
          const [role, attr] = rows[key]["Fn::GetAtt"];
          assert.equal(attr, "RoleId");
          assert.match(JSON.stringify(template.Resources[role].Properties.AssumeRolePolicyDocument), /ecs-tasks/);
        }
      }
    }
  });

  it("measured the scheduler and desktop controller task roles", () => {
    assert.ok(synthesized.includes("scheduler") && synthesized.includes("vdc"), synthesized.join(","));
    for (const role of [/scheduler-?task-?role/, /controller-?task-?role/]) {
      assert.ok([...inline.keys()].some((path) => role.test(path)), `${role} was not measured`);
    }
  });

  it("gives the desktop gateway task role only the command-session grants: the gateway calls no AWS API", () => {
    const resources = Object.entries(templates.get("vdc")?.Resources ?? {}) as Array<[string, Json]>;
    const gateway = resources.filter(([, resource]) => resource.Type === "AWS::IAM::Role"
      && String(resource.Metadata?.["aws:cdk:path"]).includes("dcv-connection-gateway-task-role"));
    assert.equal(gateway.length, 1, "the gateway task role is synthesized");
    assert.deepEqual(gateway[0]![1].Properties.ManagedPolicyArns ?? [], [], "no instance managed policies on the gateway");
    const policies = resources.filter(([, resource]) => resource.Type === "AWS::IAM::Policy"
      && JSON.stringify(resource.Properties.Roles).includes(`"${gateway[0]![0]}"`));
    // The one policy is the one CDK writes for command sessions (enableExecuteCommand).
    assert.deepEqual(policies.map(([, resource]) => String(resource.Metadata?.["aws:cdk:path"]).split("/").slice(-2)[0]), ["DefaultPolicy"]);
    const actions = (policies[0]![1].Properties.PolicyDocument.Statement as Json[]).flatMap((statement) => [statement.Action].flat());
    for (const action of actions) assert.match(action, /^(ssmmessages:|logs:(CreateLogStream|DescribeLogGroups|DescribeLogStreams|PutLogEvents)$)/);
  });

  it(`keeps every role's inline policies under ${INLINE_LIMIT} bytes together`, () => {
    const over = [...inline].filter(([, bytes]) => bytes >= INLINE_LIMIT).map(([path, bytes]) => `${path}: ${bytes}`);
    assert.deepEqual(over, []);
  });

  it("writes every policy document in IAM's grammar: known keys, ARN or * resources, variables only in the resource part", () => {
    assert.ok(synthesized.length > 0);
    assert.deepEqual(Object.fromEntries(grammar), {});
  });

  it("the cluster manager task cannot write host packages or deployment objects in the cluster bucket", () => {
    const resources = Object.values(templates.get("cluster-manager")?.Resources ?? {}) as Json[];
    const denies = resources.filter((resource) => resource.Type === "AWS::IAM::ManagedPolicy")
      .flatMap((resource) => resource.Properties.PolicyDocument.Statement as Json[])
      .filter((statement) => statement.Effect === "Deny" && [statement.Action].flat().includes("s3:PutObject"));
    assert.equal(denies.length, 1);
    const prefixes = [denies[0]!.Resource].flat().map((arn) => String(resolve(arn)).replace(/^arn:aws:s3:::[^/]+\//, ""));
    assert.deepEqual(prefixes, ["idea/*", "cdk/*", "values/*"]);
  });

  it("the grammar check rejects a policy variable standing for the whole resource", () => {
    const statement = { Effect: "Allow", Action: "ec2:CreateTags", Resource: "${ec2:SourceInstanceARN}" };
    assert.equal(grammarErrors({ Version: "2012-10-17", Statement: [statement] }, true).length, 2);
    const fixed = { ...statement, Resource: "arn:aws:ec2:us-east-2:123456789012:instance/*" };
    assert.deepEqual(grammarErrors({ Version: "2012-10-17", Statement: [fixed] }, true), []);
    assert.deepEqual(grammarErrors({ Version: "2012-10-17", Statement: [{ ...fixed, Resource: "${aws:x}" }], Extra: 1 }, true).length, 3);
  });

  it(`keeps every managed policy under ${MANAGED_LIMIT} characters`, () => {
    const over = [...managed].filter(([, chars]) => chars >= MANAGED_LIMIT).map(([path, chars]) => `${path}: ${chars}`);
    assert.deepEqual(over, []);
  });

  // IAM attaches at most 10 managed policies to a role by default, and the operator's
  // cluster.iam.ec2_managed_policy_arns (none in the rehearsal) join the task and host roles. 26.11.1
  // left room for 5 of them; a policy of ours added since must not take one.
  it(`leaves every role room for ${OPERATOR_SLOTS} of the operator's managed policies under IAM's ${ATTACHED_LIMIT}`, () => {
    const counts: string[] = [];
    for (const [module, template] of templates) {
      const resources = template.Resources as Record<string, Json>;
      for (const [roleId, role] of Object.entries(resources)) {
        if (role.Type !== "AWS::IAM::Role") continue;
        const attached = new Set<string>(list(role.Properties?.ManagedPolicyArns).map((arn) => JSON.stringify(arn)));
        for (const [id, policy] of Object.entries(resources)) {
          if (policy.Type === "AWS::IAM::ManagedPolicy" && list(policy.Properties?.Roles).some((ref) => (ref as Json).Ref === roleId)) attached.add(JSON.stringify({ Ref: id }));
        }
        if (/task-role|controller/.test(roleId) || attached.size > 0) counts.push(`${module} ${String(role.Metadata?.["aws:cdk:path"] ?? roleId)}: ${attached.size}`);
        assert.ok(attached.size + OPERATOR_SLOTS <= ATTACHED_LIMIT, `${module} ${roleId}: ${attached.size} managed policies of ours`);
      }
    }
    // the desktop controller task role gained the project move policy this release
    assert.ok(counts.some((line) => /vdc .*controller-task-role.*: 3$/.test(line)), counts.join("\n"));
  });
});
