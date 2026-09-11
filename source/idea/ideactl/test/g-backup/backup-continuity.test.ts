/**
 * Backup-continuity assertions for the container cutover.
 *
 * Synthetic cases pin the two supported tag selectors. Fixture assertions
 * inspect local template captures without copying their identifiers here.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, test } from "node:test";

import { App, Stack } from "aws-cdk-lib";
import * as backup from "aws-cdk-lib/aws-backup";
import * as iam from "aws-cdk-lib/aws-iam";
import { Template } from "aws-cdk-lib/assertions";

import { BackupPlan } from "../../src/cdk/constructs/backup.ts";
import {
  addBackupTags,
  IDEA_TAG_BACKUP_PLAN,
  makeContext,
  MODULE_CLUSTER,
} from "../../src/cdk/constructs/base.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";

type Json = boolean | number | string | Json[] | JsonObject | null;
interface JsonObject {
  [key: string]: Json;
}

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const liveTemplateDirectory = join(packageRoot, "tools", "parity", "live");
const administratorTemplates = join(packageRoot, "..", "idea-administrator", "resources", "config", "templates");
const clusterSettingsTemplate = join(administratorTemplates, "cluster", "settings.yml");
const vdcSettingsTemplate = join(administratorTemplates, "virtual-desktop-controller", "settings.yml");

const UNUSED_SYNTH_READS: SynthReads = {
  callerIdentity: async () => ({ account: "123456789012", arn: "synthetic-caller" }),
  describeDomain: async () => {
    throw new Error("addBackupTags does not read domain settings");
  },
  describeListener: async () => ({}),
  describeUserPool: async () => ({}),
  listServiceLinkedRoles: async () => [],
};

/** Validates that an arbitrary parsed value belongs to the JSON domain. */
function isJson(value: unknown): value is Json {
  if (value === null) return true;
  if (typeof value === "boolean" || typeof value === "number" || typeof value === "string") return true;
  if (Array.isArray(value)) return value.every(isJson);
  if (typeof value !== "object") return false;
  return Object.values(value).every(isJson);
}

/** Narrows a validated JSON value to a non-array object. */
function isJsonObject(value: Json): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Parses a CloudFormation template and rejects non-object JSON input. */
function parseTemplate(path: string): JsonObject {
  const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
  if (!isJson(parsed) || !isJsonObject(parsed)) {
    throw new Error(`Template must be a JSON object: ${path}`);
  }
  return parsed;
}

/**
 * Reads the backup selection tag key from a settings template. The value side
 * must stay `{{ cluster_name }}-{{ module_id }}` so the rendered selector
 * matches the plan name the stacks write.
 */
function selectionTagKey(templatePath: string): string {
  const text = readFileSync(templatePath, "utf8");
  const match = /Key=([^,\s]+),Value=\{\{\s*cluster_name\s*\}\}-\{\{\s*module_id\s*\}\}/.exec(text);
  if (match === null) {
    throw new Error(`${templatePath} does not declare the cluster-name module-id backup tag`);
  }
  return match[1];
}

/** Synthesizes one plan and selection from a template-declared selector key. */
function createSelection(planName: string, tagKey: string): Template {
  const app = new App();
  const stack = new Stack(app, "sample-cluster-cluster");
  const vault = new backup.BackupVault(stack, "backup-vault", {
    backupVaultName: "sample-cluster-cluster-backup-vault",
  });
  const role = new iam.Role(stack, "cluster-backup-role", {
    assumedBy: new iam.ServicePrincipal("backup.amazonaws.com"),
    roleName: "sample-cluster-cluster-backup-role-us-east-2",
  });

  new BackupPlan(stack, {
    backupPlanName: planName,
    backupPlanConfig: {
      rules: {
        default: {
          completion_window_minutes: 480,
          delete_after_days: 7,
          schedule_expression: "cron(0 5 * * ? *)",
          start_window_minutes: 60,
        },
      },
      selection: { tags: [`Key=${tagKey},Value=${planName}`] },
    },
    backupRole: role,
    backupVault: vault,
  });

  return Template.fromStack(stack);
}

/** Pins all rule and tag values emitted for one backup plan. */
function assertSelectorByValue(planName: string, tagKey: string): void {
  const template = createSelection(planName, tagKey);
  template.hasResourceProperties("AWS::Backup::BackupPlan", {
    BackupPlan: {
      BackupPlanName: planName,
      BackupPlanRule: [
        {
          CompletionWindowMinutes: 480,
          Lifecycle: { DeleteAfterDays: 7 },
          RuleName: "default",
          ScheduleExpression: "cron(0 5 * * ? *)",
          StartWindowMinutes: 60,
        },
      ],
    },
  });
  template.hasResourceProperties("AWS::Backup::BackupSelection", {
    BackupSelection: {
      ListOfTags: [
        {
          ConditionKey: IDEA_TAG_BACKUP_PLAN,
          ConditionType: "STRINGEQUALS",
          ConditionValue: planName,
        },
      ],
      SelectionName: `${planName}-selection`,
    },
  });
}

/** Finds a literal tag value in either standard or EFS tag properties. */
function tagValue(resource: JsonObject, tagKey: string): string | undefined {
  const properties = resource["Properties"];
  if (!isJsonObject(properties)) return undefined;
  // EFS uses FileSystemTags while the EC2 resources use Tags.
  const tags = properties["Tags"] ?? properties["FileSystemTags"];
  if (!Array.isArray(tags)) return undefined;

  for (const tag of tags) {
    if (!isJsonObject(tag) || tag["Key"] !== tagKey || typeof tag["Value"] !== "string") continue;
    return tag["Value"];
  }
  return undefined;
}

/** Extracts the cluster name from a stack description. */
function clusterNameFromTemplate(template: JsonObject): string {
  const description = template["Description"];
  if (typeof description !== "string") throw new Error("Template description is required");
  const match = /Cluster:\s*([^,]+)/.exec(description);
  if (match === null) throw new Error("Template description does not name a cluster");
  return match[1].trim();
}

/** Confirms one existing resource carries the exact cluster plan tag. */
function assertLiveResourceSelector(templateName: string, resourceId: string): void {
  const template = parseTemplate(join(liveTemplateDirectory, templateName));
  const resources = template["Resources"];
  if (!isJsonObject(resources)) throw new Error(`${templateName} has no Resources object`);
  const resource = resources[resourceId];
  if (!isJsonObject(resource)) throw new Error(`${templateName} has no ${resourceId} resource`);

  assert.equal(tagValue(resource, IDEA_TAG_BACKUP_PLAN), `${clusterNameFromTemplate(template)}-cluster`);
}

/** Recursively lists locally captured CloudFormation templates. */
function walkJsonFiles(root: string): string[] {
  if (!existsSync(root)) return [];
  const files: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkJsonFiles(path));
    } else if (entry.isFile() && entry.name.endsWith(".template.json")) {
      files.push(path);
    }
  }
  return files;
}

/** Confirms each captured selection has the tag expression for its plan. */
function assertCapturedSelection(templatePath: string): boolean {
  const template = parseTemplate(templatePath);
  const resources = template["Resources"];
  if (!isJsonObject(resources)) return false;

  let inspected = false;
  for (const resource of Object.values(resources)) {
    if (!isJsonObject(resource) || resource["Type"] !== "AWS::Backup::BackupSelection") continue;
    const properties = resource["Properties"];
    if (!isJsonObject(properties)) throw new Error(`${templatePath}: selection has no properties`);
    const selection = properties["BackupSelection"];
    if (!isJsonObject(selection)) throw new Error(`${templatePath}: selection details are missing`);
    const selectionName = selection["SelectionName"];
    const tags = selection["ListOfTags"];
    if (typeof selectionName !== "string" || !Array.isArray(tags)) {
      throw new Error(`${templatePath}: selection is incomplete`);
    }

    const planReference = properties["BackupPlanId"];
    if (!isJsonObject(planReference) || !Array.isArray(planReference["Fn::GetAtt"])) {
      throw new Error(`${templatePath}: selection does not reference its plan`);
    }
    const [planId] = planReference["Fn::GetAtt"];
    if (typeof planId !== "string") throw new Error(`${templatePath}: selection plan id is invalid`);
    const plan: Json | undefined = resources[planId];
    if (!isJsonObject(plan)) {
      throw new Error(`${templatePath}: selected plan is missing`);
    }
    const planProperties: Json | undefined = plan["Properties"];
    if (!isJsonObject(planProperties)) throw new Error(`${templatePath}: selected plan has no properties`);
    const backupPlan: Json | undefined = planProperties["BackupPlan"];
    if (!isJsonObject(backupPlan) || typeof backupPlan["BackupPlanName"] !== "string") {
      throw new Error(`${templatePath}: selected plan has no name`);
    }
    const planName: string = backupPlan["BackupPlanName"];

    assert.equal(selectionName, `${planName}-selection`);
    assert.deepEqual(tags, [
      {
        ConditionKey: IDEA_TAG_BACKUP_PLAN,
        ConditionType: "STRINGEQUALS",
        ConditionValue: planName,
      },
    ]);
    inspected = true;
  }
  return inspected;
}

describe("backup continuity", () => {
  test("the cluster plan selects only the cluster infrastructure tag", () => {
    const tagKey = selectionTagKey(clusterSettingsTemplate);
    assert.equal(tagKey, IDEA_TAG_BACKUP_PLAN);
    assertSelectorByValue("sample-cluster-cluster", tagKey);
  });

  test("the VDI plan selects only the VDI host tag", () => {
    const tagKey = selectionTagKey(vdcSettingsTemplate);
    assert.equal(tagKey, IDEA_TAG_BACKUP_PLAN);
    assertSelectorByValue("sample-cluster-vdc", tagKey);
  });

  test("addBackupTags writes the cluster plan selector", () => {
    const app = new App();
    const stack = new Stack(app, "sample-cluster-cluster");
    const role = new iam.Role(stack, "tagged-role", {
      assumedBy: new iam.ServicePrincipal("backup.amazonaws.com"),
      roleName: "sample-cluster-tagged-role",
    });
    addBackupTags(
      makeContext({
        awsRegion: "us-east-2",
        config: new ClusterConfig([{ key: "cluster.cluster_name", value: "sample-cluster" }]),
        moduleId: MODULE_CLUSTER,
        releaseVersion: "0.0.0",
        synthReads: UNUSED_SYNTH_READS,
      }),
      role,
    );

    const template = Template.fromStack(stack).toJSON() as JsonObject;
    const resources = template["Resources"];
    if (!isJsonObject(resources)) throw new Error("tagged role stack has no Resources");
    const tagKey = selectionTagKey(clusterSettingsTemplate);
    let found = false;
    for (const resource of Object.values(resources)) {
      if (!isJsonObject(resource) || resource["Type"] !== "AWS::IAM::Role") continue;
      assert.equal(tagValue(resource, tagKey), `sample-cluster-${MODULE_CLUSTER}`);
      found = true;
    }
    assert.ok(found, "tagged role");
  });

  for (const [templateName, resourceId] of [
    ["idea-dev27-shared-storage.json", "appsstorageefs"],
    ["idea-dev27-shared-storage.json", "datastorageefs"],
    ["idea-dev27-scheduler.json", "schedulerinstance"],
    ["idea-dev27-bastion-host.json", "bastionhostinstance"],
  ]) {
    const path = join(liveTemplateDirectory, templateName);
    test(`${templateName}/${resourceId} carries the cluster selector`, { skip: !existsSync(path) }, () => {
      assertLiveResourceSelector(templateName, resourceId);
    });
  }

  const fixtureRoots = [
    join(packageRoot, "tools", "parity", "fixtures"),
    join(homedir(), ".idea", "clusters"),
  ];
  const capturedTemplates = fixtureRoots.flatMap(walkJsonFiles);

  test(
    "every captured backup selection matches its plan name by value",
    { skip: capturedTemplates.length === 0 },
    () => {
      const inspected = capturedTemplates.filter(assertCapturedSelection).length;
      assert.ok(inspected > 0, "No backup selections found in the local template captures");
    },
  );
});
