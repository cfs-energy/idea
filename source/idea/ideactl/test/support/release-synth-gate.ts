/**
 * Release gate: the packaged executable synthesizes every module stack offline and produces what
 * the source tree produces from the same inputs.
 *
 * The day-zero rehearsal generates a new cluster's settings and replayed reads, then writes each
 * stack's exact inputs. Every stack is synthesized three times through the real `cdk cdk-app`
 * entry point: twice from source and once by the executable, with cdk-nag on as in a deployment.
 * The two source runs mark the values that change on every synthesis by design (random update
 * tokens and name suffixes); everything else in the executable's template and validation report
 * must match source exactly, and a changing value must keep its type and length. A dependency file
 * the executable cannot find, or one that reads differently once bundled (cdk-nag derives rule
 * names from its own file names), fails the release before anything is published.
 */

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { availableParallelism, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { DEFAULT_STACK_REGISTRY } from '../../src/cdk/app.ts';
import { serviceLinkedRolePathPrefixes } from '../../src/cdk/stacks/analytics.ts';
import { listRolesKey } from '../../src/cdk/synth-reads.ts';

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const REHEARSAL = join(PACKAGE_ROOT, 'tools/day-zero/rehearse.ts');
const VALUES = join(PACKAGE_ROOT, 'test/stacks/day-zero-new-cluster.values.yml');
const SOURCE_ENTRY = join(PACKAGE_ROOT, 'src/cli/main.ts');
/** Rendered for `cdk bootstrap --template`, not synthesized by the CDK app. */
const NOT_SYNTHESIZED = new Set(['bootstrap']);

interface EmittedStack {
  moduleId: string;
  moduleName: string;
  clusterName: string;
  awsRegion: string;
  deploymentId: string;
}

interface SynthCase extends EmittedStack {
  /** Directory under the inputs root holding this case's settings, reads and CDK context. */
  inputs: string;
  label: string;
}

interface SynthOutput {
  template: unknown;
  validationReport: unknown;
}

type Difference = { path: string; left: unknown; right: unknown };

/** Every leaf where two JSON values differ, by JSON path. */
export function differences(left: unknown, right: unknown, path = ''): Difference[] {
  if (left === right) return [];
  const leftObject = left !== null && typeof left === 'object';
  const rightObject = right !== null && typeof right === 'object';
  if (!leftObject || !rightObject || Array.isArray(left) !== Array.isArray(right)) {
    return [{ path, left, right }];
  }
  if (Array.isArray(left) && Array.isArray(right)) {
    if (left.length !== right.length) return [{ path: `${path}.length`, left: left.length, right: right.length }];
    return left.flatMap((item, index) => differences(item, right[index], `${path}[${index}]`));
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const keys = [...new Set([...Object.keys(leftRecord), ...Object.keys(rightRecord)])].sort();
  return keys.flatMap((key) => {
    if (!(key in leftRecord) || !(key in rightRecord)) return [{ path: `${path}/${key}`, left: leftRecord[key], right: rightRecord[key] }];
    return differences(leftRecord[key], rightRecord[key], `${path}/${key}`);
  });
}

/**
 * What the executable got wrong: differences from the first source run, except values some source
 * run disagrees on, which must still keep their type and length.
 */
export function unexpectedDifferences(sources: unknown[], executable: unknown): string[] {
  const [first, ...others] = sources;
  const volatile = new Set(others.flatMap((other) => differences(first, other).map((difference) => difference.path)));
  const problems: string[] = [];
  for (const { path, left, right } of differences(first, executable)) {
    if (!volatile.has(path)) {
      problems.push(`${path}: source ${JSON.stringify(left)?.slice(0, 160)}, executable ${JSON.stringify(right)?.slice(0, 160)}`);
      continue;
    }
    const sameShape = typeof left === typeof right && (typeof left !== 'string' || left.length === (right as string).length);
    if (!sameShape) problems.push(`${path}: changes on every synthesis, but the executable's value has a different shape`);
  }
  return problems;
}

/**
 * Extra source syntheses allowed before a difference is reported. Some values carry only a few
 * random characters (a 32-character name ends in about two hex digits of a UUID), so two source
 * runs can agree on one by chance. A value the executable gets "wrong" is reported only if every
 * source run agrees on it: for 8 random bits and 8 extra runs, ten runs agreeing by chance is 2^-72.
 */
export const CONFIRMING_SOURCE_RUNS = 8;

/**
 * Problems that survive confirmation: while the executable differs from source somewhere, another
 * source synthesis is added (up to `limit`) and the comparison is made again against all of them.
 */
export async function confirmedProblems<T>(
  sources: T[],
  executable: T,
  problemsOf: (sources: T[], executable: T) => string[],
  anotherSource: () => Promise<T>,
  limit = CONFIRMING_SOURCE_RUNS,
): Promise<{ problems: string[]; sourceRuns: number }> {
  const runs = [...sources];
  let problems = problemsOf(runs, executable);
  for (let extra = 0; problems.length > 0 && extra < limit; extra += 1) {
    runs.push(await anotherSource());
    problems = problemsOf(runs, executable);
  }
  return { problems, sourceRuns: runs.length };
}

/**
 * A validation report also records where each run wrote its template and the JavaScript call stacks
 * that created each violating construct and its acknowledgement. Those name source files in one run and the bundle in the
 * other, so they are left out; the rules, findings, constructs and template locations are compared.
 */
function normalizedReport(file: string): unknown {
  if (!existsSync(file)) return null;
  const report = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>;
  return JSON.parse(JSON.stringify(report, (key, value) => {
    if (/stacktrace/i.test(key)) return undefined;
    return key === 'templatePath' || key === 'filePath' ? '<output>' : value;
  }));
}

/** One isolated home and temporary directory per synthesis, so concurrent runs never share build output. */
function synthEnvironment(root: string, outDir: string): NodeJS.ProcessEnv {
  const own = `${outDir}.env`;
  for (const name of ['home', 'tmp']) mkdirSync(join(own, name), { recursive: true });
  return {
    // The same sanitized environment as the release smoke: no installed runtime on the path, no
    // AWS credentials or instance metadata, an empty home. cdk-nag stays at its default (on).
    SystemRoot: process.env.SystemRoot, ComSpec: process.env.ComSpec,
    USERPROFILE: join(own, 'home'), HOME: join(own, 'home'), IDEA_USER_HOME: join(own, 'home', '.idea'),
    TEMP: join(own, 'tmp'), TMP: join(own, 'tmp'), TMPDIR: join(own, 'tmp'),
    PATH: join(root, 'empty-path'), AWS_EC2_METADATA_DISABLED: 'true', CDK_DISABLE_CLI_TELEMETRY: '1',
    NODE_PATH: '', LANG: 'en_US.UTF-8', CDK_OUTDIR: outDir,
  };
}

function synthesize(command: string, prefix: string[], synthCase: SynthCase, root: string, outDir: string): Promise<SynthOutput> {
  const args = [
    ...prefix, 'cdk', 'cdk-app',
    '--cluster-name', synthCase.clusterName, '--aws-region', synthCase.awsRegion,
    '--module-id', synthCase.moduleId, '--module-name', synthCase.moduleName,
    '--deployment-id', synthCase.deploymentId, '--termination-protection', 'true',
    '--config-file', join(synthCase.inputs, 'cluster-settings.json'),
    '--synth-reads', join(synthCase.inputs, 'synth-reads.json'),
  ];
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { cwd: synthCase.inputs, env: synthEnvironment(root, outDir) });
    let transcript = '';
    child.stdout.on('data', (chunk: Buffer) => { transcript += chunk.toString(); });
    child.stderr.on('data', (chunk: Buffer) => { transcript += chunk.toString(); });
    child.on('error', reject);
    child.on('close', (code) => {
      const templateFile = join(outDir, `${synthCase.clusterName}-${synthCase.moduleId}.template.json`);
      if (code !== 0 || !existsSync(templateFile)) {
        reject(new Error(`${synthCase.label}: ${command} exited ${String(code)}\n${transcript.slice(-4000)}`));
        return;
      }
      const manifest = JSON.parse(readFileSync(join(outDir, 'manifest.json'), 'utf8')) as { missing?: unknown[] };
      if ((manifest.missing ?? []).length > 0) {
        reject(new Error(`${synthCase.label}: synthesis left context lookups unanswered: ${JSON.stringify(manifest.missing)}`));
        return;
      }
      resolvePromise({
        template: JSON.parse(readFileSync(templateFile, 'utf8')),
        validationReport: normalizedReport(join(outDir, 'validation-report.json')),
      });
    });
  });
}

type SettingsScan = { Items: Array<{ key: { S: string }; value: { S?: string; N?: string; L?: Array<{ S?: string }> } }> };

function readSettings(inputs: string): SettingsScan {
  return JSON.parse(readFileSync(join(inputs, 'cluster-settings.json'), 'utf8')) as SettingsScan;
}

/**
 * Answers the VPC lookup a stack makes for the cluster's network, the way the CDK CLI would from
 * the account. The rehearsal fills settings a deployed stack would publish with placeholders, so
 * subnet settings that are not subnet ids get generated ones, and the VPC answer carries the same
 * ids in three zones. Any other lookup stays unanswered and fails the gate, so it gets an answer here.
 */
function answerVpcLookup(inputs: string, account: string, region: string): void {
  const scan = readSettings(inputs);
  const setting = (key: string) => scan.Items.find((item) => item.key.S === key);
  const zones = ['a', 'b', 'c'].map((zone) => `${region}${zone}`);
  const contextFile = join(inputs, 'cdk.context.json');
  const context = JSON.parse(readFileSync(contextFile, 'utf8')) as Record<string, unknown>;
  // A new VPC spreads across the region's zones.
  context[`availability-zones:account=${account}:region=${region}`] = zones;
  writeFileSync(contextFile, `${JSON.stringify(context, null, 2)}\n`);
  const vpcId = setting('cluster.network.vpc_id')?.value.S;
  if (vpcId === undefined || vpcId === '') return;
  const group = (kind: 'Public' | 'Private', key: string, octet: number) => {
    const item = setting(key);
    const listed = item?.value.L?.map((entry) => entry.S ?? '') ?? [];
    const ids = zones.map((_zone, index) => (listed[index]?.startsWith('subnet-')
      ? listed[index]
      : `subnet-${kind === 'Public' ? '0a' : '0b'}${String(index).padStart(15, '0')}`));
    if (item !== undefined) item.value = { L: ids.map((id) => ({ S: id })) };
    return {
      name: kind, type: kind,
      subnets: zones.map((availabilityZone, index) => ({
        subnetId: ids[index],
        cidr: `10.0.${String(octet + index)}.0/24`,
        availabilityZone,
        routeTableId: `rtb-${kind === 'Public' ? '0a' : '0b'}${String(index).padStart(15, '0')}`,
      })),
    };
  };
  const subnetGroups = [group('Public', 'cluster.network.public_subnets', 0), group('Private', 'cluster.network.private_subnets', 16)];
  writeFileSync(join(inputs, 'cluster-settings.json'), JSON.stringify(scan));
  context[`vpc-provider:account=${account}:filter.vpc-id=${vpcId}:region=${region}:returnAsymmetricSubnets=true`] = {
    vpcId, vpcCidrBlock: '10.0.0.0/16', ownerAccountId: account, availabilityZones: [], subnetGroups,
  };
  writeFileSync(contextFile, `${JSON.stringify(context, null, 2)}\n`);
}

/** Writes every stack's inputs from the day-zero rehearsal and returns the cases to synthesize. */
function prepareCases(inputsRoot: string): SynthCase[] {
  const rehearsal = spawnSync(process.execPath, [REHEARSAL, '--values-file', VALUES, '--complete-reads', '--emit-inputs', inputsRoot], {
    cwd: PACKAGE_ROOT, encoding: 'utf8',
  });
  assert.equal(rehearsal.status, 0, `day-zero rehearsal failed\n${rehearsal.stdout}\n${rehearsal.stderr}`);
  const stacks = JSON.parse(readFileSync(join(inputsRoot, 'modules.json'), 'utf8')) as EmittedStack[];

  // Every module the CDK app can build must be synthesized here, including any added later.
  const synthesizable = Object.keys(DEFAULT_STACK_REGISTRY).filter((name) => !NOT_SYNTHESIZED.has(name)).sort();
  assert.deepEqual([...new Set(stacks.map((stack) => stack.moduleName))].sort(), synthesizable,
    'the release synthesis gate must synthesize every module; extend the day-zero values file to enable any that are missing');

  const cases: SynthCase[] = stacks.map((stack) => ({ ...stack, inputs: join(inputsRoot, stack.moduleId), label: stack.moduleId }));
  for (const synthCase of cases) {
    const reads = JSON.parse(readFileSync(join(synthCase.inputs, 'synth-reads.json'), 'utf8')) as Record<string, { account?: string }>;
    const account = reads['sts:GetCallerIdentity:{}']?.account;
    assert.ok(account, `${synthCase.label}: rehearsal reads must answer the caller identity`);
    answerVpcLookup(synthCase.inputs, account, synthCase.awsRegion);
  }

  // A cluster upgraded in place already has the OpenSearch service-linked role; the analytics
  // stack builds a different tree then.
  const analytics = cases.find((synthCase) => synthCase.moduleName === 'analytics');
  assert.ok(analytics, 'the day-zero values file must enable analytics');
  const existingRole = join(inputsRoot, 'analytics-existing-role');
  cpSync(analytics.inputs, existingRole, { recursive: true });
  const reads = JSON.parse(readFileSync(join(existingRole, 'synth-reads.json'), 'utf8')) as Record<string, unknown>;
  for (const prefix of serviceLinkedRolePathPrefixes('amazonaws.com')) {
    assert.ok(listRolesKey(prefix) in reads, `rehearsal reads must answer ${listRolesKey(prefix)}`);
    reads[listRolesKey(prefix)] = [{
      Path: `${prefix}/`, RoleName: 'AWSServiceRoleForAmazonOpenSearchService', RoleId: 'AROAEXAMPLEROLEID00000',
      Arn: `arn:aws:iam::123456789012:role${prefix}/AWSServiceRoleForAmazonOpenSearchService`, CreateDate: '2026-01-01T00:00:00Z',
    }];
  }
  writeFileSync(join(existingRole, 'synth-reads.json'), JSON.stringify(reads));
  cases.push({ ...analytics, inputs: existingRole, label: 'analytics (existing service-linked role)' });
  return cases;
}

async function eachLimited<T>(items: T[], limit: number, action: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const failures: unknown[] = [];
  const worker = async (): Promise<void> => {
    while (next < items.length) {
      const item = items[next++];
      try { await action(item); } catch (error) { failures.push(error); }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  if (failures.length > 0) {
    throw new Error(failures.map((failure) => (failure instanceof Error ? failure.message : String(failure))).join('\n\n'));
  }
}

/** Runs the gate against a packaged executable. Throws with every problem found. */
export async function gateReleaseSynthesis(executable: string, options: { keep?: boolean } = {}): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'ideactl release synth-'));
  try {
    for (const name of ['home', 'tmp', 'empty-path', 'inputs', 'out']) mkdirSync(join(root, name));
    const cases = prepareCases(join(root, 'inputs'));
    await eachLimited(cases, Math.max(1, Math.min(4, availableParallelism())), async (synthCase) => {
      const out = join(root, 'out', synthCase.label.replace(/[^A-Za-z0-9-]+/g, '-'));
      // Settle all three before reporting, so no synthesis is still writing when the work tree goes.
      const settled = await Promise.allSettled([
        synthesize(process.execPath, [SOURCE_ENTRY], synthCase, root, join(out, 'source-a')),
        synthesize(process.execPath, [SOURCE_ENTRY], synthCase, root, join(out, 'source-b')),
        synthesize(executable, [], synthCase, root, join(out, 'executable')),
      ]);
      const failed = settled.flatMap((result) => (result.status === 'rejected' ? [result.reason] : []));
      if (failed.length > 0) {
        throw new Error(failed.map((reason) => (reason instanceof Error ? reason.message : String(reason))).join('\n'));
      }
      const [sourceA, sourceB, packaged] = settled.map((result) => (result as PromiseFulfilledResult<SynthOutput>).value);
      let extraRun = 0;
      const { problems, sourceRuns } = await confirmedProblems(
        [sourceA, sourceB],
        packaged,
        (sources, executableOutput) => [
          ...unexpectedDifferences(sources.map((source) => source.template), executableOutput.template)
            .map((problem) => `template ${problem}`),
          ...unexpectedDifferences(sources.map((source) => source.validationReport), executableOutput.validationReport)
            .map((problem) => `validation report ${problem}`),
        ],
        () => synthesize(process.execPath, [SOURCE_ENTRY], synthCase, root, join(out, `source-extra-${String(++extraRun)}`)),
      );
      if (problems.length > 0) {
        throw new Error(
          `${synthCase.label}: the executable's synthesis differs from all ${String(sourceRuns)} source syntheses\n  ${problems.slice(0, 25).join('\n  ')}`,
        );
      }
      if (sourceRuns > 2) console.log(`confirmed changing values with ${String(sourceRuns)} source syntheses: ${synthCase.label}`);
      console.log(`PASS executable synthesis matches source: ${synthCase.label}`);
    });
    console.log(`PASS extracted release: all ${String(cases.length)} module syntheses match source`);
  } finally {
    if (options.keep === true) console.log(`kept ${root}`);
    else rmSync(root, { recursive: true, force: true, maxRetries: 5 });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  assert.ok(process.argv[2], 'release executable path is required');
  await gateReleaseSynthesis(resolve(process.argv[2]), { keep: process.argv.includes('--keep') });
}
