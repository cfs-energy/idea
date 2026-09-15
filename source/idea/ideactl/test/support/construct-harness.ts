/**
 * Shared test harness: builds a throwaway `IdeaBaseStack` on the captured replay fixtures and
 * hands back a synthesized template, so each construct can be compared against the matching
 * resources of the live dev27 templates.
 *
 * Fixtures under `tools/parity/{fixtures,live}` are gitignored, and every test that needs them
 * fails when they are absent.
 */

import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { App } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';

import { IdeaBaseStack } from '../../src/cdk/base-stack.ts';
import { makeContext, type IdeaContext } from '../../src/cdk/constructs/base.ts';
import { replaySynthReads } from '../../src/cdk/synth-reads.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';
import { ideaVersion } from '../../src/version.ts';
import { requireCapture } from '../support/fixtures.ts';

export const PKG = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURES = join(PKG, 'tools', 'parity', 'fixtures', 'idea-dev27');
export const CONFIG_FILE = join(FIXTURES, 'cluster-settings.json');
export const SYNTH_READS = join(FIXTURES, 'synth-reads.json');
export const CONTEXT_FILE = join(FIXTURES, 'cdk.context.json');

export const CLUSTER = 'idea-dev27';
export const REGION = 'us-east-2';
export const DEPLOYMENT_ID = '97999f4c-daaa-4813-b8ac-bd7abaedc26b';

export type Json = Record<string, any>;

export const readJson = (path: string): Json => JSON.parse(readFileSync(path, 'utf8')) as Json;

requireCapture(
  [CONFIG_FILE, SYNTH_READS, CONTEXT_FILE],
  "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27",
);

export function liveTemplatePath(stack: string): string {
  return join(PKG, 'tools', 'parity', 'live', `idea-dev27-${stack}.json`);
}

export function haveLive(stack: string): boolean {
  return existsSync(liveTemplatePath(stack));
}

/** Require the live template used by a parity assertion. */
export function requireLiveFixture(stack: string): void {
  requireCapture(
    [liveTemplatePath(stack)],
    "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
  );
}

/** The live resources, by logical ID. */
export function liveResources(stack: string): Json {
  return readJson(liveTemplatePath(stack)).Resources as Json;
}

/** The account the fixtures were captured in; it keys the `Vpc.fromLookup` context entry. */
export function fixtureAccount(): string {
  const reads = readJson(SYNTH_READS);
  return (reads['sts:GetCallerIdentity:{}'] as Json).account as string;
}

/** `app.ts`: the `cdk.json` context block plus `cdk.context.json`, plus path metadata. */
function appContext(): Record<string, unknown> {
  const cdkJson = readJson(join(PKG, 'cdk.json')) as { context?: Record<string, unknown> };
  return {
    'aws:cdk:enable-path-metadata': true,
    ...(cdkJson.context ?? {}),
    ...readJson(CONTEXT_FILE),
  };
}

export interface Harness {
  ctx: IdeaContext;
  base: IdeaBaseStack;
  template(): Json;
}

export interface HarnessOptions {
  moduleId: string;
  moduleName: string;
  /** Cluster-settings scan file; defaults to the captured dev27 scan. */
  configFile?: string;
}

export function harness(options: HarnessOptions): Harness {
  const config = ClusterConfig.fromFile(readFileSync(options.configFile ?? CONFIG_FILE, 'utf8'));
  const ctx = makeContext({
    config,
    awsRegion: REGION,
    moduleId: options.moduleId,
    releaseVersion: ideaVersion(),
    synthReads: replaySynthReads(SYNTH_READS),
  });
  const app = new App({ context: appContext() });
  const base = new IdeaBaseStack({
    scope: app,
    ctx,
    moduleName: options.moduleName,
    deploymentId: DEPLOYMENT_ID,
    terminationProtection: true,
    env: { account: fixtureAccount(), region: REGION },
  });
  return {
    ctx,
    base,
    template: () => Template.fromStack(base.stack).toJSON() as Json,
  };
}

const workdirs: string[] = [];

/**
 * A copy of the dev27 cluster-settings scan with the named keys replaced (added when absent).
 * `null` writes a DynamoDB NULL, which every getter reads as absent.
 */
export function configWith(overrides: Record<string, string | number | boolean | null>): string {
  const scan = readJson(CONFIG_FILE);
  const attribute = (value: string | number | boolean | null): Json => {
    if (value === null) return { NULL: true };
    if (typeof value === 'number') return { N: String(value) };
    if (typeof value === 'boolean') return { BOOL: value };
    return { S: value };
  };
  const remaining = new Set(Object.keys(overrides));
  for (const item of scan.Items as Json[]) {
    const key = item.key?.S as string | undefined;
    if (key !== undefined && remaining.delete(key)) {
      item.value = attribute(overrides[key] as string | number | boolean | null);
    }
  }
  for (const key of remaining) {
    (scan.Items as Json[]).push({
      key: { S: key },
      value: attribute(overrides[key] as string | number | boolean | null),
      version: { N: '1' },
    });
  }
  const workdir = mkdtempSync(join(tmpdir(), 'ideactl-construct-'));
  workdirs.push(workdir);
  const file = join(workdir, 'cluster-settings.json');
  writeFileSync(file, JSON.stringify(scan));
  return file;
}

/** Call from an `after()` hook. */
export function cleanup(): void {
  for (const workdir of workdirs) rmSync(workdir, { recursive: true, force: true });
  workdirs.length = 0;
}
