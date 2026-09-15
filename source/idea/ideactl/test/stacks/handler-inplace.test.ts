/**
 * Proves that replacing the handler implementation can update each deployed
 * Lambda in place. Logical ids and Lambda replacement properties are compared
 * with the deployed templates after synthesizing the TypeScript stacks.
 */

import assert from "node:assert/strict";
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";
import { test } from "node:test";

import { buildApp } from "../../src/cdk/app.ts";
import { DEPLOYED_HANDLERS, type NodeHandler } from "../../tools/parity/node-handlers.ts";
import { requireFixtures } from "../support/fixtures.ts";

const PKG = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CLUSTER = "idea-dev27";
const REGION = "us-east-2";
const DEPLOYMENT_ID = "97999f4c-daaa-4813-b8ac-bd7abaedc26b";
const FIXTURES = join(PKG, "tools", "parity", "fixtures", CLUSTER);
const CONFIG_FILE = join(FIXTURES, "cluster-settings.json");
const SYNTH_READS = join(FIXTURES, "synth-reads.json");
const CONTEXT_FILE = join(FIXTURES, "cdk.context.json");

interface JsonObject {
  [key: string]: unknown;
}

interface TemplateResource {
  Type: string;
  Properties: JsonObject;
}

interface Template {
  Resources: Record<string, TemplateResource>;
}

const REPLACEMENT_PROPERTIES = ["FunctionName", "PackageType"] as const;
const NODE_RUNTIME = "nodejs22.x";
const NODE_HANDLER = "index.handler";

requireFixtures(
  [
    CONFIG_FILE,
    SYNTH_READS,
    CONTEXT_FILE,
    ...[...new Set(DEPLOYED_HANDLERS.map((handler) => handler.moduleId))].map((moduleId) =>
      join(PKG, "tools", "parity", "live", `${CLUSTER}-${moduleId}.json`),
    ),
  ],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

/** Validates a parsed JSON object before it is used as a template section. */
function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Reads one template, rejecting malformed resource records instead of masking them. */
function readTemplate(path: string): Template {
  const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
  if (!isObject(parsed) || !isObject(parsed.Resources)) {
    throw new Error(`${path}: expected a template Resources object`);
  }

  const resources: Record<string, TemplateResource> = {};
  for (const [logicalId, value] of Object.entries(parsed.Resources)) {
    if (!isObject(value) || typeof value.Type !== "string") {
      throw new Error(`${path}: ${logicalId} is not a resource`);
    }
    resources[logicalId] = {
      Type: value.Type,
      Properties: isObject(value.Properties) ? value.Properties : {},
    };
  }
  return { Resources: resources };
}

/** Synthesizes one stack with the captured configuration and synth-time reads. */
async function synthModule(handler: NodeHandler): Promise<Template> {
  const workdir = mkdtempSync(join(tmpdir(), "ideactl-handler-inplace-"));
  const previousCwd = process.cwd();
  const previousOutdir = process.env.CDK_OUTDIR;
  const previousNag = process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;

  try {
    cpSync(CONTEXT_FILE, join(workdir, "cdk.context.json"));
    cpSync(join(PKG, "cdk.json"), join(workdir, "cdk.json"));
    process.chdir(workdir);
    process.env.CDK_OUTDIR = join(workdir, `cdk.out.${handler.moduleId}`);
    process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = "false";

    const app = await buildApp({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleId: handler.moduleId,
      moduleName: handler.moduleName,
      deploymentId: DEPLOYMENT_ID,
      terminationProtection: true,
      configFile: CONFIG_FILE,
      synthReadsFile: SYNTH_READS,
    });
    app.synth();
    return readTemplate(join(process.env.CDK_OUTDIR, `${CLUSTER}-${handler.moduleId}.template.json`));
  } finally {
    process.chdir(previousCwd);
    if (previousOutdir === undefined) delete process.env.CDK_OUTDIR;
    else process.env.CDK_OUTDIR = previousOutdir;
    if (previousNag === undefined) delete process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN;
    else process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN = previousNag;
    rmSync(workdir, { recursive: true, force: true });
  }
}

/** Reads one rewritten handler and requires the file to export `handler`. */
function assertRewrittenHandlerSource(packageName: string): void {
  const source = join(PKG, "src", "lambda", packageName, "index.ts");
  assert.ok(existsSync(source), `${packageName}: rewritten handler source is missing`);
  const text = readFileSync(source, "utf8");
  assert.match(
    text,
    /export (?:async function|const) handler\b/,
    `${packageName}: rewritten handler source does not export handler`,
  );
}

/** Lists every way a synthesized function is not the bundled Node handler. */
function runtimeFindings(handler: NodeHandler, emitted: Template): string[] {
  const actual = emitted.Resources[handler.logicalId];
  if (actual === undefined) return [`${handler.packageName}: logical id ${handler.logicalId} was replaced or removed`];
  const findings: string[] = [];
  if (actual.Properties.Runtime !== NODE_RUNTIME) {
    findings.push(`${handler.packageName}: Runtime is ${String(actual.Properties.Runtime)}, not ${NODE_RUNTIME}`);
  }
  if (actual.Properties.Handler !== NODE_HANDLER) {
    findings.push(`${handler.packageName}: Handler is ${String(actual.Properties.Handler)}, not ${NODE_HANDLER}`);
  }
  return findings;
}

/** Lists every property difference that would replace a Lambda function. */
function replacementFindings(
  handler: NodeHandler,
  deployed: Template,
  emitted: Template,
): string[] {
  const live = deployed.Resources[handler.logicalId];
  const actual = emitted.Resources[handler.logicalId];
  if (live === undefined) return [`${handler.packageName}: deployed logical id ${handler.logicalId} is missing`];
  if (actual === undefined) return [`${handler.packageName}: logical id ${handler.logicalId} was replaced or removed`];

  const findings: string[] = [];
  if (live.Type !== "AWS::Lambda::Function") {
    findings.push(`${handler.packageName}: deployed ${handler.logicalId} is ${live.Type}, not AWS::Lambda::Function`);
  }
  if (actual.Type !== "AWS::Lambda::Function") {
    findings.push(`${handler.packageName}: ${handler.logicalId} changed type to ${actual.Type}`);
  }
  for (const property of REPLACEMENT_PROPERTIES) {
    if (!isDeepStrictEqual(live.Properties[property], actual.Properties[property])) {
      findings.push(`${handler.packageName}: ${property} would replace ${handler.logicalId}`);
    }
  }
  return findings;
}

test("all rewritten handler functions keep their deployed identity and run the Node bundle", async () => {
  const emitted = new Map<string, Template>();
  const findings: string[] = [];

  for (const handler of DEPLOYED_HANDLERS) {
    assertRewrittenHandlerSource(handler.packageName);

    let template = emitted.get(handler.moduleId);
    if (template === undefined) {
      template = await synthModule(handler);
      emitted.set(handler.moduleId, template);
    }

    const deployed = readTemplate(join(PKG, "tools", "parity", "live", `${CLUSTER}-${handler.moduleId}.json`));
    findings.push(...replacementFindings(handler, deployed, template));
    findings.push(...runtimeFindings(handler, template));
  }

  assert.deepEqual(findings, [], `replacement findings:\n${findings.join("\n")}`);
});

test("the guard reports a changed Lambda name as a replacement", async () => {
  const handler = DEPLOYED_HANDLERS[0];
  const deployed = readTemplate(
    join(PKG, "tools", "parity", "live", `${CLUSTER}-${handler.moduleId}.json`),
  );
  const emitted = await synthModule(handler);
  const liveFunction = deployed.Resources[handler.logicalId];
  const emittedFunction = emitted.Resources[handler.logicalId];
  assert.ok(
    liveFunction !== undefined,
    `${handler.packageName}: deployed logical id ${handler.logicalId} is missing`,
  );
  assert.ok(
    emittedFunction !== undefined,
    `${handler.packageName}: logical id ${handler.logicalId} was replaced or removed`,
  );
  assert.equal(
    emittedFunction.Properties.FunctionName,
    liveFunction.Properties.FunctionName,
  );
  assert.deepEqual(replacementFindings(handler, deployed, emitted), []);

  const renamed: Template = {
    Resources: {
      [handler.logicalId]: {
        Type: emittedFunction.Type,
        Properties: {
          ...emittedFunction.Properties,
          FunctionName: "sample-cluster-handler-next",
        },
      },
    },
  };
  assert.deepEqual(replacementFindings(handler, deployed, renamed), [
    `${handler.packageName}: FunctionName would replace ${handler.logicalId}`,
  ]);
});
