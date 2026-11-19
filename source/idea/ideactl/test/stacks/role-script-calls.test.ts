/**
 * Every aws CLI call the container role scripts and the host bootstrap shell make is allowed for
 * the role that runs it, by the policies synthesized for that role, with the resource and request
 * context the call presents.
 *
 * The narrowed grants of 26.11.2 were checked against the Python services only, and the scheduler
 * role script's Run Command to compute nodes lost its permission without a test failing. This test
 * reads the calls from the scripts, so a new call fails until it is mapped to a request here, and
 * a narrowed grant fails the call it removes.
 *
 * The stacks come from the day-zero rehearsal, so no captured fixtures are needed. The evaluator
 * covers the condition operators the policies use and throws on any other; AWS managed policies
 * are not read, so a call allowed only by one of them fails here.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { before, describe, it } from "node:test";
import { execFileSync, spawnSync } from "node:child_process";

import { rehearseDayZero } from "../../tools/day-zero/rehearse.ts";

type Json = Record<string, any>;
type Context = Record<string, string | string[]>;
interface Request { action: string; resource: string; context?: Context }

const VALUES = join(import.meta.dirname, "day-zero-new-cluster.values.yml");
const REPO = execFileSync("git", ["rev-parse", "--show-toplevel"], { cwd: import.meta.dirname, encoding: "utf8" }).trim();
const ACCOUNT = "123456789012";
const REGION = "us-east-2";
const CLUSTER = "idea-test1";
const INSTANCE = "i-0123456789abcdef0";
const INSTANCE_ARN = `arn:aws:ec2:${REGION}:${ACCOUNT}:instance/${INSTANCE}`;
const PSEUDO: Record<string, string> = {
  "AWS::AccountId": ACCOUNT, "AWS::Partition": "aws", "AWS::Region": REGION, "AWS::URLSuffix": "amazonaws.com", "AWS::NoValue": "",
};

// --- the policies a role holds -------------------------------------------------------------------

function resolve(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(resolve);
  if (value === null || typeof value !== "object") return value;
  const entries = Object.entries(value as Json);
  if (entries.length === 1) {
    const [key, arg] = entries[0]!;
    if (key === "Ref") return PSEUDO[arg as string] ?? `REF:${arg}`;
    if (key === "Fn::Join") return (resolve(arg[1]) as string[]).join(arg[0]);
    if (key === "Fn::Sub") {
      const text = String(Array.isArray(arg) ? arg[0] : arg);
      // ${ec2:InstanceID} and the like are policy variables, not CloudFormation parameters
      return text.replace(/\$\{([^}:]+(?:::[^}]+)?)\}/g, (whole, name: string) => PSEUDO[name] ?? (name.includes(":") && !name.startsWith("AWS::") ? whole : `REF:${name}`));
    }
    if (key.startsWith("Fn::")) return `${key}:${JSON.stringify(arg)}`;
  }
  return Object.fromEntries(entries.map(([k, v]) => [k, resolve(v)]));
}

const list = <T>(value: T | T[] | undefined): T[] => (value === undefined ? [] : Array.isArray(value) ? value : [value]);

/** The resolved statements of every policy attached to the named role. */
function roleStatements(templates: Map<string, Json>, roleNamePart: string): Json[] {
  for (const template of templates.values()) {
    const resources = template.Resources as Record<string, Json>;
    const entry = Object.entries(resources).find(([, resource]) => resource.Type === "AWS::IAM::Role"
      && String(resolve(resource.Properties.RoleName)).includes(roleNamePart));
    if (entry === undefined) continue;
    const [roleId, role] = entry;
    const documents: Json[] = (role.Properties.Policies ?? []).map((policy: Json) => policy.PolicyDocument);
    for (const arn of list(role.Properties.ManagedPolicyArns)) {
      if (typeof arn === "object" && arn.Ref !== undefined) documents.push(resources[arn.Ref]!.Properties.PolicyDocument);
      else if (typeof arn === "string" && arn.includes(":policy/day-zero-cluster-iam-policies-")) {
        // a shared host policy of the cluster stack, imported by an ARN the rehearsal fills in
        const name = arn.split("day-zero-cluster-iam-policies-")[1]!.replace(/-arn$/, "");
        const shared = Object.values(templates.get("cluster")!.Resources as Record<string, Json>).find((resource) =>
          resource.Type === "AWS::IAM::ManagedPolicy" && String(resolve(resource.Properties.ManagedPolicyName)).includes(name));
        assert.ok(shared, `shared policy ${name} not found in the cluster stack`);
        documents.push(shared.Properties.PolicyDocument);
      }
    }
    for (const resource of Object.values(resources)) {
      if (!["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"].includes(resource.Type)) continue;
      if (list(resource.Properties.Roles).some((ref: Json) => ref?.Ref === roleId)) documents.push(resource.Properties.PolicyDocument);
    }
    return documents.flatMap((document) => list((resolve(document) as Json).Statement) as Json[]);
  }
  throw new Error(`no role named like ${roleNamePart}`);
}

// --- a small identity-policy evaluator -----------------------------------------------------------

const glob = (pattern: string, value: string, ignoreCase = false): boolean => {
  const source = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${source}$`, ignoreCase ? "is" : "s").test(value);
};

/** Substitutes policy variables; undefined when one is not in the request. */
function substitute(text: string, context: Context): string | undefined {
  let missing = false;
  const out = text.replace(/\$\{([^}]+)\}/g, (_, name: string) => {
    const value = context[name];
    if (typeof value !== "string") { missing = true; return ""; }
    return value;
  });
  return missing ? undefined : out;
}

function conditionHolds(operator: string, key: string, expected: unknown, context: Context): boolean {
  const match = /^(ForAllValues:|ForAnyValue:)?([A-Za-z]+?)(IfExists)?$/.exec(operator);
  if (match === null) throw new Error(`operator ${operator}`);
  const [, set, base, ifExists] = match;
  const present = context[key] !== undefined;
  const actual = list(context[key]).map(String);
  const wanted = list(expected as string | string[]).map((value) => substitute(String(value), context));
  if (base === "Null") return (String(expected) === "true") === !present;
  if (!present) {
    if (set === "ForAllValues:") return true;
    if (ifExists) return true;
    return /Not/.test(base!) && set === undefined;
  }
  const test: Record<string, (a: string, w: string) => boolean> = {
    StringEquals: (a, w) => a === w,
    StringLike: (a, w) => glob(w, a),
    ArnEquals: (a, w) => a === w,
    ArnLike: (a, w) => glob(w, a),
    Bool: (a, w) => a.toLowerCase() === w.toLowerCase(),
  };
  const negated = base!.replace("Not", "");
  const one = test[negated];
  if (one === undefined) throw new Error(`operator ${operator}`);
  const matches = (a: string) => wanted.some((w) => w !== undefined && one(a, w));
  const negate = base!.includes("Not");
  // a set operator applies the (negated) test to each request value: ForAllValues:StringNotEquals
  // holds only when every value differs from every listed one
  if (set === "ForAllValues:") return actual.every((a) => negate !== matches(a));
  if (set === "ForAnyValue:") return actual.some((a) => negate !== matches(a));
  return negate !== actual.some(matches);
}

function statementApplies(statement: Json, request: Request): boolean {
  const context = request.context ?? {};
  const actions = statement.Action !== undefined ? list(statement.Action) : list(statement.NotAction);
  const actionMatch = actions.some((action: string) => glob(action, request.action, true));
  if (statement.Action !== undefined ? !actionMatch : actionMatch) return false;
  const resources = statement.Resource !== undefined ? list(statement.Resource) : list(statement.NotResource);
  const resourceMatch = resources.some((resource: string) => {
    const pattern = substitute(String(resource), context);
    return pattern !== undefined && glob(pattern, request.resource);
  });
  if (statement.Resource !== undefined ? !resourceMatch : resourceMatch) return false;
  for (const [operator, keys] of Object.entries((statement.Condition ?? {}) as Json)) {
    for (const [key, expected] of Object.entries(keys as Json)) {
      if (!conditionHolds(operator, key, expected, context)) return false;
    }
  }
  return true;
}

export function allowed(statements: Json[], request: Request): boolean {
  if (statements.some((statement) => statement.Effect === "Deny" && statementApplies(statement, request))) return false;
  return statements.some((statement) => statement.Effect === "Allow" && statementApplies(statement, request));
}

// --- the calls the scripts make ------------------------------------------------------------------

const CALL = /(?:^|[\s;|&(`"=])(?:aws|\$AWS|\$\{AWS\})\s+((?:--[a-z-]+(?:[= ][^\s-]\S*)?\s+)*)([a-z0-9-]+)\s+([a-z0-9-]+)/g;
// AWS Tools for PowerShell cmdlets in the Windows host scripts; instance metadata is not an API call
const CMDLET = /\b((?:Copy|Read|Write|Get|New|Remove|Send|Set|Invoke|Start|Stop|Publish|Update)-(?:S3|EC2|SSM|SQS|SNS|SEC|CWL|DDB|KMS|IAM|STS|FSX|EFS|CFN|ASA|CW)[A-Za-z]*)\b/g;

interface Call { file: string; line: number; call: string }

function scriptCalls(): Call[] {
  const files = execFileSync("git", ["ls-files", "deployment/ecr", "source/idea/idea-bootstrap", "source/idea/*/resources/scripts"], { cwd: REPO, encoding: "utf8" }).split("\n").filter(Boolean);
  const calls: Call[] = [];
  for (const file of files) {
    let text: string;
    try { text = readFileSync(join(REPO, file), "utf8"); } catch { continue; }
    text.split("\n").forEach((line, index) => {
      if (/^\s*#/.test(line)) return;
      for (const match of line.matchAll(CALL)) {
        if (match[2]!.startsWith("-")) continue;
        calls.push({ file, line: index + 1, call: `${match[2]} ${match[3]}` });
      }
      for (const match of line.matchAll(CMDLET)) {
        if (match[1] === "Get-EC2InstanceMetadata") continue;
        calls.push({ file, line: index + 1, call: `ps ${match[1]}` });
      }
    });
  }
  return calls;
}

/** The roles that run a script, by its path. */
function rolesOf(file: string): string[] {
  const roles: Array<[RegExp, string[]]> = [
    [/^deployment\/ecr\/idea-control-plane\/entrypoint\.sh$/, ["-cluster-manager-task-role-", "-scheduler-task-role-", "-vdc-controller-task-role-"]],
    [/\/roles\/scheduler\.sh$/, ["-scheduler-task-role-"]],
    [/\/roles\/broker\.sh$/, ["-vdc-broker-task-role-"]],
    [/^source\/idea\/idea-scheduler\/resources\/scripts\//, ["-scheduler-task-role-"]],
    [/\/idea-bootstrap\/(compute-node|compute-node-ami-builder)\//, ["-scheduler-compute-node-role-"]],
    [/\/idea-bootstrap\/(virtual-desktop-host-|dcv-host-ami-builder|_templates\/windows\/)/, ["-vdc-host-role-"]],
    [/\/idea-bootstrap\/(common|_templates\/linux)\//, ["-scheduler-compute-node-role-", "-vdc-host-role-"]],
  ];
  const found = roles.find(([pattern]) => pattern.test(file));
  if (found === undefined) throw new Error(`${file}: no role is mapped for this script`);
  return found[1];
}

const BUCKET = `${CLUSTER}-cluster-${REGION}-${ACCOUNT}`;
const vol = `arn:aws:ec2:${REGION}:${ACCOUNT}:volume/vol-0123456789abcdef0`;
const eni = `arn:aws:ec2:${REGION}:${ACCOUNT}:network-interface/eni-0123456789abcdef0`;
// ec2:InstanceID is the tagged instance (a CreateTags condition key of the instance resource in
// the EC2 service reference); ec2:SourceInstanceARN is the instance the credentials were issued to.
const statusTag = (key: string, target = INSTANCE): Request => ({
  action: "ec2:CreateTags", resource: `arn:aws:ec2:${REGION}:${ACCOUNT}:instance/${target}`,
  context: { "ec2:SourceInstanceARN": INSTANCE_ARN, "ec2:InstanceID": target, "aws:TagKeys": [key] },
});

/**
 * The requests each call makes, by script and call. A call absent here fails the test until it is
 * mapped; an entry with `reason` is not evaluated, and says why.
 */
const REQUESTS: Array<{ file: RegExp; call: string; requests?: Request[]; reason?: string }> = [
  { file: /entrypoint\.sh|roles\/(broker|scheduler)\.sh/, call: "dynamodb get-item",
    requests: [{ action: "dynamodb:GetItem", resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.cluster-settings` }] },
  { file: /roles\/scheduler\.sh/, call: "route53 change-resource-record-sets",
    requests: [{ action: "route53:ChangeResourceRecordSets", resource: "arn:aws:route53:::hostedzone/day-zero-id" }] },
  // Tag-targeted: SSM evaluates the document, then each instance the tags resolve to with its tags.
  { file: /roles\/scheduler\.sh/, call: "ssm send-command", requests: [
    { action: "ssm:SendCommand", resource: `arn:aws:ssm:${REGION}::document/AWS-RunShellScript` },
    { action: "ssm:SendCommand", resource: INSTANCE_ARN,
      context: { "ssm:resourceTag/idea:ClusterName": CLUSTER, "ssm:resourceTag/idea:ModuleId": "scheduler", "ssm:resourceTag/idea:NodeType": "compute-node" } },
  ] },
  // The daily scheduler log archive (send_logs_s3.sh) under logs/<module id>/.
  { file: /send_logs_s3\.sh/, call: "s3 sync", requests: [
    { action: "s3:ListBucket", resource: `arn:aws:s3:::${BUCKET}` },
    { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/logs/scheduler/server_logs/20261118` },
  ] },
  { file: /send_logs_s3\.sh/, call: "s3 cp", requests: [
    { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/logs/scheduler/accounting/20261118` },
  ] },
  { file: /image_checks/, call: "ec2 create-tags", requests: [statusTag("idea:AmiBuilderStatus")] },
  { file: /gpu_drivers|set_kernel/, call: "ec2 create-tags", requests: [statusTag("idea:BootstrapStatus")] },
  // A new volume or interface carries no tag yet; one already tagged carries this cluster's name.
  { file: /tag_ebs_volumes/, call: "ec2 create-tags", requests: [
    { action: "ec2:CreateTags", resource: vol, context: { "aws:TagKeys": ["idea:ClusterName", "Name"] } },
    { action: "ec2:CreateTags", resource: vol, context: { "aws:TagKeys": ["Name"], "aws:ResourceTag/idea:ClusterName": CLUSTER } },
  ] },
  { file: /tag_network_interface/, call: "ec2 create-tags", requests: [
    { action: "ec2:CreateTags", resource: eni, context: { "aws:TagKeys": ["idea:ClusterName", "Name"] } },
  ] },
  { file: /tag_ebs_volumes/, call: "ec2 describe-volumes", requests: [{ action: "ec2:DescribeVolumes", resource: "*" }] },
  { file: /tag_network_interface/, call: "ec2 describe-network-interfaces", requests: [{ action: "ec2:DescribeNetworkInterfaces", resource: "*" }] },
  { file: /Configure\.ps1/, call: "ec2 describe-instance-types", requests: [{ action: "ec2:DescribeInstanceTypes", resource: "*" }] },
  { file: /bootstrap_common|scratch_storage/, call: "fsx describe-file-systems", requests: [{ action: "fsx:DescribeFileSystems", resource: "*" }] },
  { file: /scratch_storage/, call: "resourcegroupstaggingapi get-resources", requests: [{ action: "tag:GetResources", resource: "*" }] },
  { file: /image_checks/, call: "logs create-log-stream", requests: [{ action: "logs:CreateLogStream",
    resource: `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/${CLUSTER}/vdc/ami-builder:log-stream:bootstrap_${INSTANCE}` }] },
  { file: /image_checks/, call: "logs put-log-events", requests: [{ action: "logs:PutLogEvents",
    resource: `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/${CLUSTER}/vdc/ami-builder:log-stream:bootstrap_${INSTANCE}` }] },
  // The Windows host scripts' AWS Tools for PowerShell calls.
  { file: /dcv-host-ami-builder-windows/, call: "ps New-EC2Tag", requests: [statusTag("idea:AmiBuilderStatus")] },
  { file: /dcv-host-ami-builder-windows/, call: "ps New-CWLLogStream", requests: [{ action: "logs:CreateLogStream",
    resource: `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/${CLUSTER}/vdc/ami-builder:log-stream:bootstrap_${INSTANCE}` }] },
  { file: /dcv-host-ami-builder-windows/, call: "ps Write-CWLLogEvent", requests: [{ action: "logs:PutLogEvents",
    resource: `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/${CLUSTER}/vdc/ami-builder:log-stream:bootstrap_${INSTANCE}` }] },
  { file: /windows\/tag_ebs_volumes/, call: "ps New-EC2Tag", requests: [
    { action: "ec2:CreateTags", resource: vol, context: { "aws:TagKeys": ["idea:ClusterName", "Name"] } }] },
  { file: /windows\/tag_network_interface/, call: "ps New-EC2Tag", requests: [
    { action: "ec2:CreateTags", resource: eni, context: { "aws:TagKeys": ["idea:ClusterName", "Name"] } }] },
  { file: /windows\/tag_/, call: "ps Get-EC2Tag", requests: [{ action: "ec2:DescribeTags", resource: "*" }] },
  { file: /windows\/tag_ebs_volumes/, call: "ps Get-EC2Volume", requests: [{ action: "ec2:DescribeVolumes", resource: "*" }] },
  { file: /windows\/tag_network_interface/, call: "ps Get-EC2NetworkInterface", requests: [{ action: "ec2:DescribeNetworkInterfaces", resource: "*" }] },
  { file: /windows\/join_activedirectory|Configure\.ps1/, call: "ps Send-SQSMessage", reason: "queue named by a setting; grant unchanged in this branch" },
  { file: /windows\/join_activedirectory/, call: "ps Get-DDBItem", reason: "AD automation table named by a setting; grant unchanged in this branch" },
  // Not narrowed in 26.11.2. Each names a resource from a setting the rehearsal fills with a placeholder.
  { file: /bootstrap_common/, call: "secretsmanager get-secret-value", reason: "directory credentials secret named by a setting; grant unchanged in this branch" },
  { file: /join_activedirectory|configure_dcv_host_post_reboot/, call: "sqs send-message", reason: "queue named by a setting; grant unchanged in this branch" },
  { file: /join_activedirectory/, call: "dynamodb get-item", reason: "AD automation table named by a setting; grant unchanged in this branch" },
  // The GPU drivers come from AWS's public driver buckets with a recursive copy, which lists.
  { file: /nvidia_drivers/, call: "s3 cp", requests: [
    { action: "s3:ListBucket", resource: "arn:aws:s3:::ec2-windows-nvidia-drivers" },
    { action: "s3:GetObject", resource: "arn:aws:s3:::ec2-windows-nvidia-drivers/latest/driver.exe" },
  ] },
  { file: /gpu_drivers/, call: "s3 cp", requests: [
    { action: "s3:ListBucket", resource: "arn:aws:s3:::ec2-linux-nvidia-drivers" },
    { action: "s3:GetObject", resource: "arn:aws:s3:::ec2-linux-nvidia-drivers/latest/driver.run" },
  ] },
  { file: /amd_drivers/, call: "s3 cp", requests: [
    { action: "s3:ListBucket", resource: "arn:aws:s3:::ec2-amd-windows-drivers" },
    { action: "s3:GetObject", resource: "arn:aws:s3:::ec2-amd-windows-drivers/latest/driver.zip" },
  ] },
];

/**
 * The object writes the Python services make (boto3 upload_file), mapped by hand since they are not
 * shell: compute_node_ami_builder.py:370, cloudformation_stack_builder.py:333 (scheduler);
 * dcv_host_image_builder.py:387, virtual_desktop_controller_utils.py:820 (controller).
 */
const SERVICE_WRITES: Array<{ role: string; requests: Request[] }> = [
  { role: "-scheduler-task-role-", requests: [
    { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/idea/scheduler/bootstrap/job.tar.gz` },
    // the job's log group with its retention (cloudformation_stack_builder.py ensure_job_log_group),
    // which hosts no longer set
    { action: "logs:CreateLogGroup", resource: `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/${CLUSTER}/scheduler/job-1234-abcd:*` },
    { action: "logs:PutRetentionPolicy", resource: `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/${CLUSTER}/scheduler/job-1234-abcd:*` },
    // the job metrics backfill's status row (ideasdk metrics/history_backfill.py set_config_entry)
    { action: "dynamodb:UpdateItem", resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.cluster-settings`,
      context: { "dynamodb:LeadingKeys": ["scheduler.metrics.backfill.jobs"] } },
  ] },
  { role: "-vdc-controller-task-role-", requests: [
    { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/idea/vdc/bootstrap/builder.tar.gz` },
    { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/idea/vdc/dcv-host-bootstrap/desk-1/host.tar.gz` },
    // the stopped-desktop cleanup's own receipt table, created at start (vdc fe429654
    // initialize_desktop_cleanup_table, SDK dynamodb_create_table) and read with key `delivered`
    ...["dynamodb:CreateTable", "dynamodb:DescribeTable", "dynamodb:UpdateTimeToLive", "dynamodb:TagResource"]
      .map((action) => ({ action, resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.vdc.desktop-cleanup` })),
    { action: "dynamodb:GetItem", resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.vdc.desktop-cleanup`,
      context: { "dynamodb:LeadingKeys": ["delivered"] } },
  ] },
  // the API server's load balancer subnet read (ideasdk soca_server.py, cm-server d80e2c68), in
  // every service that serves the API; the call is on the merged tree, not on this branch
  ...["-cluster-manager-task-role-", "-vdc-controller-task-role-", "-scheduler-task-role-"].map((role) => ({ role, requests: [
    { action: "ec2:DescribeSubnets", resource: "*" },
  ] })),
  // an administrator's project move (virtual_desktop_admin_api.py move_session_to_project): the
  // profile association of this cluster's desktop, the project role passed to ec2, and the
  // project tags on the instance and its volumes
  { role: "-vdc-controller-task-role-", requests: [
    { action: "ec2:DescribeIamInstanceProfileAssociations", resource: "*" },
    ...["ec2:AssociateIamInstanceProfile", "ec2:ReplaceIamInstanceProfileAssociation", "ec2:DisassociateIamInstanceProfile"].map((action) => ({ action, resource: INSTANCE_ARN,
      context: { "aws:ResourceTag/idea:ClusterName": CLUSTER, "aws:ResourceTag/idea:NodeType": "virtual-desktop-dcv-host" } })),
    { action: "iam:PassRole", resource: `arn:aws:iam::${ACCOUNT}:role/idea/${CLUSTER}/projects/project-role`, context: { "iam:PassedToService": "ec2.amazonaws.com" } },
    ...["ec2:CreateTags", "ec2:DeleteTags"].flatMap((action) => [
      { action, resource: INSTANCE_ARN, context: { "aws:TagKeys": ["idea:Project", "idea:ProjectMovedBy"], "aws:ResourceTag/idea:ClusterName": CLUSTER, "aws:ResourceTag/idea:ModuleId": "vdc" } },
      { action, resource: vol, context: { "aws:TagKeys": ["idea:Project", "idea:ProjectMovedBy"], "aws:ResourceTag/idea:ClusterName": CLUSTER } },
    ]),
  ] as Request[] },
  // the cluster manager records that receipt once the email has gone (cm-accounts 72715b54)
  { role: "-cluster-manager-task-role-", requests: [
    { action: "dynamodb:PutItem", resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.vdc.desktop-cleanup`,
      context: { "dynamodb:LeadingKeys": ["delivered"] } },
    { action: "dynamodb:PutItem", resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.scheduler.scratch-cleanup`,
      context: { "dynamodb:LeadingKeys": ["delivered"] } },
  ] },
];

/** Requests the user data makes before any script runs: the package fetch and its status tag. */
const USER_DATA: Array<{ role: string; requests: Request[] }> = [
  { role: "-scheduler-compute-node-role-", requests: [
    { action: "s3:GetObject", resource: `arn:aws:s3:::${BUCKET}/idea/scheduler/bootstrap/compute-node.tar.gz` },
    statusTag("idea:BootstrapStatus"),
  ] },
  { role: "-vdc-host-role-", requests: [
    { action: "s3:GetObject", resource: `arn:aws:s3:::${BUCKET}/idea/vdc/dcv-host-bootstrap/host.tar.gz` },
    statusTag("idea:BootstrapStatus"),
    // the Windows user data's failure tag on an image builder, which launches with the desktop host
    // profile (dcv_host_image_builder.py, virtual-desktop-controller.dcv_host_instance_profile_arn)
    statusTag("idea:AmiBuilderStatus"),
  ] },
];

describe("role scripts and host bootstrap calls are allowed for their roles", () => {
  const workDir = mkdtempSync(join(tmpdir(), "ideactl-role-calls-"));
  const templates = new Map<string, Json>();
  process.setMaxListeners(0);
  process.on("exit", () => rmSync(workDir, { recursive: true, force: true }));

  before(async () => {
    const report = await rehearseDayZero({ valuesFile: VALUES, workDir });
    for (const stack of report.stacks) {
      if (stack.status !== "SYNTHESIZED") continue;
      const outdir = join(workDir, `cdk.out.${stack.moduleId}`);
      const file = readdirSync(outdir).find((name) => name.endsWith(".template.json"))!;
      templates.set(stack.moduleId, JSON.parse(readFileSync(join(outdir, file), "utf8")));
    }
  });

  it("every call the scripts make is mapped to the requests it sends", () => {
    const unmapped = scriptCalls().filter(({ file, call }) => !REQUESTS.some((entry) => entry.call === call && entry.file.test(file)))
      .map(({ file, line, call }) => `${file}:${line} ${call}`);
    assert.deepEqual(unmapped, []);
  });

  it("each mapped request is allowed for every role that runs the script", () => {
    const denied: string[] = [];
    const seen = new Set<string>();
    for (const { file, call } of scriptCalls()) {
      const entry = REQUESTS.find((candidate) => candidate.call === call && candidate.file.test(file))!;
      if (entry.requests === undefined) continue;
      for (const role of rolesOf(file)) {
        const statements = roleStatements(templates, role);
        for (const request of entry.requests) {
          const key = `${role} ${request.action} ${request.resource} ${JSON.stringify(request.context)}`;
          if (seen.has(key)) continue;
          seen.add(key);
          if (!allowed(statements, request)) denied.push(`${relative(".", file)} ${call}: ${role} ${request.action} ${request.resource}`);
        }
      }
    }
    for (const { role, requests } of [...USER_DATA, ...SERVICE_WRITES]) {
      const statements = roleStatements(templates, role);
      for (const request of requests) if (!allowed(statements, request)) denied.push(`user data: ${role} ${request.action} ${request.resource}`);
    }
    assert.deepEqual(denied, []);
  });

  // Lambda invocations made with a role's credentials: a job stack created before 26.09.1 holds a
  // SendAnonymousData custom resource whose service token is the cluster's solution metrics
  // function, and the scheduler deletes those stacks with its own role, which invokes it. No
  // service or script calls lambda:Invoke directly (checked by the scan below).
  it("every Lambda function invoked with a role's credentials is allowed for that role", () => {
    const functions = Object.values(templates.get("cluster")!.Resources as Record<string, Json>)
      .filter((resource) => resource.Type === "AWS::Lambda::Function").map((resource) => String(resolve(resource.Properties.FunctionName)));
    const metrics = functions.find((name) => name.endsWith("-solution-metrics"));
    assert.ok(metrics, functions.join(", "));
    const scheduler = roleStatements(templates, "-scheduler-task-role-");
    assert.equal(allowed(scheduler, { action: "lambda:InvokeFunction", resource: `arn:aws:lambda:${REGION}:${ACCOUNT}:function:${metrics}` }), true);
    for (const other of functions.filter((name) => name !== metrics)) {
      assert.equal(allowed(scheduler, { action: "lambda:InvokeFunction", resource: `arn:aws:lambda:${REGION}:${ACCOUNT}:function:${other}` }), false, other);
    }
    const search = spawnSync("git", ["grep", "-nE", "lambda invoke|\\.invoke\\(FunctionName|InvokeCommand|lambda_client|client\\('lambda'\\)|\\.lambda_\\(\\)", "--",
      "deployment/ecr", "source/idea/idea-bootstrap", "source/idea/idea-sdk/src", "source/idea/idea-scheduler/src", "source/idea/idea-virtual-desktop-controller/src"],
      { cwd: REPO, encoding: "utf8" });
    assert.ok(search.status === 0 || search.status === 1, search.stderr);
    assert.deepEqual(search.stdout.split("\n").filter(Boolean), [], "a direct Lambda invocation needs a request mapped here");
  });

  // Every AWS client call the Python services make (boto3 `aws().<service>().<method>(`,
  // `<service>_client.<method>(`, and `<name>.<method>(` where the file binds `<name> = …<service>_client`
  // or `<name> = ….aws().<service>()` earlier in the same function), by package, must name an action some statement of that service's
  // task role allows. The resource and conditions are not checked here (the mapped requests above
  // do that for the calls that matter); this catches a call with no grant at all.
  it("every AWS client call in the Python services names an action its task role allows", () => {
    const packages: Array<[string, string[]]> = [
      ["-scheduler-task-role-", ["source/idea/idea-scheduler/src", "source/idea/idea-sdk/src/ideasdk/server"]],
      ["-vdc-controller-task-role-", ["source/idea/idea-virtual-desktop-controller/src", "source/idea/idea-sdk/src/ideasdk/server"]],
      ["-cluster-manager-task-role-", ["source/idea/idea-cluster-manager/src", "source/idea/idea-sdk/src/ideasdk/server"]],
    ];
    const prefixes: Record<string, string> = {
      ec2: "ec2", _ec2: "ec2", s3: "s3", ssm: "ssm", _ssm: "ssm", sqs: "sqs", sns: "sns", cloudformation: "cloudformation",
      dynamodb: "dynamodb", dynamodb_table: "dynamodb", logs: "logs", secretsmanager: "secretsmanager", cognito_idp: "cognito-idp",
      cost_explorer: "ce", ds: "ds", fsx: "fsx", efs: "elasticfilesystem", service_quotas: "servicequotas",
      application_autoscaling: "application-autoscaling", autoscaling: "autoscaling", iam: "iam", ses: "ses", budgets: "budgets",
      kinesis: "kinesis", elbv2: "elasticloadbalancing", acm: "acm", pricing: "pricing", backup: "backup", route53: "route53",
      bedrock: "bedrock", cloudtrail: "cloudtrail", ecs: "ecs", cloudwatch: "cloudwatch", eventbridge: "events", sts: "sts", kms: "kms",
    };
    // product clients and SDK helpers that are not AWS calls
    const notAws = new Set(["accounts", "projects", "notifications", "notification_async", "opensearch", "os", "unix_socket", "api", "rest",
      "dcv_broker", "ddb", "_ddb", "evdi", "ldap", "events", "token", "scheduler", "vdc", "cluster_manager", "http", "metrics", "auth",
      "web", "terminal", "ws", "redis", "smtp", "socket", "mock", "boto", "session", "self", "context", "pbs", "openpbs", "async", "sync",
      "instance_metadata"]);
    // IAM names a few calls by another action: batch SQS calls by the single one, and a DynamoDB
    // transaction by the item actions inside it (checked with the mapped requests)
    const special: Record<string, string> = { upload_file: "PutObject", download_file: "GetObject", generate_presigned_url: "GetObject",
      delete_message_batch: "DeleteMessage", send_message_batch: "SendMessage", change_message_visibility_batch: "ChangeMessageVisibility" };
    const authorizedPerItem = new Set(["transact_write_items", "transact_get_items"]);
    // calls made only when an optional feature is on, which the rehearsal leaves off
    const featureGated: Record<string, string> = {
      "-cluster-manager-task-role- logs:StopQuery": "Bedrock usage (cluster-manager.bedrock.enabled grants it with StartQuery)",
      "-cluster-manager-task-role- logs:StartQuery": "Bedrock usage (granted on the invocation log group when Bedrock is on)",
      "-cluster-manager-task-role- logs:GetQueryResults": "Bedrock usage (cluster-manager.bedrock.enabled)",
    };
    const calls = new RegExp(String.raw`\.aws\(\)\s*\.\s*(\w+)\(\)\s*\.\s*(\w+)\(|\b(\w+)_client\s*\.\s*(\w+)\(`, "g");
    const aliases = new RegExp(String.raw`^[ \t]*(\w+)[ \t]*=[ \t]*(?:[\w.]*\.)?(?:(\w+)_client|aws\(\)[ \t]*\.[ \t]*(\w+)\(\))[ \t]*$`, "gm");
    const unmapped: string[] = [];
    const denied: string[] = [];
    for (const [role, roots] of packages) {
      const statements = roleStatements(templates, role).filter((statement) => statement.Effect === "Allow");
      const files = execFileSync("git", ["ls-files", ...roots], { cwd: REPO, encoding: "utf8" }).split("\n").filter((file) => file.endsWith(".py"));
      const seen = new Set<string>();
      for (const file of files) {
        const text = readFileSync(join(REPO, file), "utf8");
        const found: Array<[string, string]> = [...text.matchAll(calls)].map((match) => [match[1] ?? match[3]!, match[2] ?? match[4]!]);
        for (const alias of text.matchAll(aliases)) {
          // the name holds that client until the function ends (the next def)
          const start = alias.index! + alias[0].length;
          const end = /\n[ \t]*(?:async[ \t]+)?def[ \t]/.exec(text.slice(start));
          const body = text.slice(start, end === null ? undefined : start + end.index);
          for (const call of body.matchAll(new RegExp(String.raw`(?<![\w.])${alias[1]!}\s*\.\s*(\w+)\(`, "g"))) found.push([alias[2] ?? alias[3]!, call[1]!]);
        }
        for (const [accessor, method] of found) {
          if (notAws.has(accessor) || method === "Table" || method === "get_paginator" || method.startsWith("_") || authorizedPerItem.has(method)) continue;
          const prefix = prefixes[accessor];
          if (prefix === undefined) { unmapped.push(`${file}: ${accessor}.${method}`); continue; }
          const action = `${prefix}:${special[method] ?? method.split("_").map((part) => part[0]!.toUpperCase() + part.slice(1)).join("")}`;
          if (seen.has(action)) continue;
          seen.add(action);
          const granted = statements.some((statement) => list(statement.Action).some((pattern: string) => glob(pattern, action, true)));
          if (!granted && featureGated[`${role} ${action}`] === undefined) denied.push(`${role} ${action} (${file})`);
        }
      }
      assert.ok(seen.size > 10, `${role}: the scan found only ${seen.size} calls; did the package paths move?`);
    }
    assert.deepEqual(unmapped, [], "a client accessor needs its IAM prefix above, or a place in notAws");
    assert.deepEqual(denied, []);
  });

  it("the evaluator refuses what the narrowed grants refuse", () => {
    const scheduler = roleStatements(templates, "-scheduler-task-role-");
    assert.equal(allowed(scheduler, { action: "ssm:SendCommand", resource: INSTANCE_ARN,
      context: { "ssm:resourceTag/idea:ClusterName": "another-cluster", "ssm:resourceTag/idea:NodeType": "compute-node" } }), false);
    assert.equal(allowed(scheduler, { action: "ssm:SendCommand", resource: INSTANCE_ARN,
      context: { "ssm:resourceTag/idea:ClusterName": CLUSTER, "ssm:resourceTag/idea:NodeType": "vdc-host" } }), false);
    assert.equal(allowed(scheduler, { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/idea/vdc/bootstrap/x` }), false);
    assert.equal(allowed(scheduler, { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/idea/vdc2/bootstrap/x` }), false, "another module set's packages");
    assert.equal(allowed(scheduler, { action: "s3:PutObject", resource: `arn:aws:s3:::${BUCKET}/logs/vdc/x` }), false);
    // Run Command keys on the module as well, and the identity tags are written only at launch
    const sendTo = (tags: Record<string, string>) => ({ action: "ssm:SendCommand", resource: INSTANCE_ARN,
      context: Object.fromEntries(Object.entries(tags).map(([key, value]) => [`ssm:resourceTag/idea:${key}`, value])) });
    assert.equal(allowed(scheduler, sendTo({ ClusterName: CLUSTER, ModuleId: "vdc", NodeType: "ami-builder" })), false, "a desktop image builder");
    assert.equal(allowed(scheduler, sendTo({ ClusterName: CLUSTER, ModuleId: "scheduler", NodeType: "ami-builder" })), true);
    const retag = (role: Json[], key: string, atLaunch: boolean) => allowed(role, { action: "ec2:CreateTags", resource: INSTANCE_ARN,
      context: { "aws:TagKeys": [key], "aws:ResourceTag/idea:ClusterName": CLUSTER, "aws:ResourceTag/idea:ModuleId": "vdc", ...(atLaunch ? { "ec2:CreateAction": "RunInstances" } : {}) } });
    assert.equal(retag(scheduler, "idea:NodeType", false), false);
    assert.equal(retag(scheduler, "idea:NodeType", true), true);
    // only job stacks are created or deleted
    const stack = `arn:aws:cloudformation:${REGION}:${ACCOUNT}:stack/${CLUSTER}-cluster/x`;
    const job = { "aws:ResourceTag/idea:ClusterName": CLUSTER, "aws:ResourceTag/idea:StackType": "job" };
    assert.equal(allowed(scheduler, { action: "cloudformation:DeleteStack", resource: stack, context: job }), true);
    assert.equal(allowed(scheduler, { action: "cloudformation:DeleteStack", resource: stack, context: { "aws:ResourceTag/idea:ClusterName": CLUSTER } }), false);
    assert.equal(allowed(scheduler, { action: "cloudformation:CreateStack", resource: stack, context: { "aws:RequestTag/idea:ClusterName": CLUSTER, "aws:RequestTag/idea:StackType": "job" } }), true);
    assert.equal(allowed(scheduler, { action: "cloudformation:CreateStack", resource: stack, context: { "aws:RequestTag/idea:ClusterName": CLUSTER } }), false);
    // the cluster's private zone only, and the controller changes no record
    assert.equal(allowed(scheduler, { action: "route53:ChangeResourceRecordSets", resource: "arn:aws:route53:::hostedzone/Z0PUBLIC" }), false);
    const controller = roleStatements(templates, "-vdc-controller-task-role-");
    assert.ok(!controller.some((statement) => list(statement.Action).some((action: string) => action.startsWith("route53:"))));
    // the controller reaches its own module's instances only
    assert.equal(allowed(controller, sendTo({ ClusterName: CLUSTER, ModuleId: "scheduler", NodeType: "compute-node" })), false);
    assert.equal(allowed(controller, sendTo({ ClusterName: CLUSTER, ModuleId: "vdc", NodeType: "virtual-desktop-dcv-host" })), true);
    const stop = (moduleId: string) => allowed(controller, { action: "ec2:StopInstances", resource: INSTANCE_ARN,
      context: { "aws:ResourceTag/idea:ClusterName": CLUSTER, "aws:ResourceTag/idea:ModuleId": moduleId } });
    assert.equal(stop("scheduler"), false);
    assert.equal(stop("vdc"), true);
    assert.equal(retag(controller, "idea:ModuleId", false), false);
    assert.equal(allowed(controller, { action: "ec2:CreateTags", resource: INSTANCE_ARN,
      context: { "aws:TagKeys": ["idea:DCVSessionId"], "aws:ResourceTag/idea:ClusterName": CLUSTER, "aws:ResourceTag/idea:ModuleId": "vdc" } }), true, "the session tags the controller writes");
    const host = roleStatements(templates, "-vdc-host-role-");
    assert.equal(allowed(host, statusTag("idea:BootstrapStatus", "i-0another")), false);
    assert.equal(allowed(host, statusTag("idea:ClusterName")), false);
    // hosts set no log retention; the deploy and the scheduler do
    const retention = (role: Json[], group: string) => allowed(role, { action: "logs:PutRetentionPolicy", resource: `arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/${CLUSTER}/${group}:*` });
    assert.equal(retention(host, "vdc/dcv-host"), false);
    assert.equal(retention(roleStatements(templates, "-scheduler-compute-node-role-"), "scheduler/job-1234-abcd"), false);
    assert.equal(retention(scheduler, "vdc/dcv-host"), false, "the scheduler sets retention on job groups only");
    // the move tags a volume of this cluster only, and never its identity tags
    const controllerRole = roleStatements(templates, "-vdc-controller-task-role-");
    assert.equal(allowed(controllerRole, { action: "ec2:CreateTags", resource: vol, context: { "aws:TagKeys": ["idea:Project"], "aws:ResourceTag/idea:ClusterName": "another-cluster" } }), false);
    assert.equal(allowed(controllerRole, { action: "ec2:CreateTags", resource: vol, context: { "aws:TagKeys": ["idea:ClusterName"], "aws:ResourceTag/idea:ClusterName": CLUSTER } }), false);
    assert.equal(allowed(controllerRole, { action: "ec2:CreateTags", resource: vol, context: { "aws:TagKeys": ["idea:Project", "idea:ClusterName"], "aws:ResourceTag/idea:ClusterName": CLUSTER } }), false, "one excluded key among several");
    assert.equal(allowed(controllerRole, { action: "ec2:DeleteTags", resource: vol, context: { "aws:ResourceTag/idea:ClusterName": CLUSTER } }), false, "a DeleteTags with no keys removes every tag");
    // the cluster manager writes receipt rows only, and not into another module's other tables
    const clusterManager = roleStatements(templates, "-cluster-manager-task-role-");
    assert.equal(allowed(clusterManager, { action: "dynamodb:PutItem", resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.vdc.desktop-cleanup`,
      context: { "dynamodb:LeadingKeys": ["s-1"] } }), false);
    assert.equal(allowed(roleStatements(templates, "-vdc-controller-task-role-"), { action: "dynamodb:GetItem",
      resource: `arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${CLUSTER}.scheduler.scratch-cleanup`, context: { "dynamodb:LeadingKeys": ["delivered"] } }), false,
      "the controller reads its own receipt table, not the scheduler's");
    assert.equal(allowed(host, { action: "s3:ListBucket", resource: `arn:aws:s3:::${BUCKET}` }), false);
    assert.equal(allowed(host, { action: "ec2:CreateTags", resource: vol, context: { "aws:TagKeys": ["Name"], "aws:ResourceTag/idea:ClusterName": "another-cluster" } }), false);
  });
});
