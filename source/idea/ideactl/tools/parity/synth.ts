// Synthesize one module stack from the replay fixtures and diff it against the template
// the Python administrator produced.
//
//   node synth.ts --cluster CLUSTER --stack metrics [--against synth] [--ignore-version]
//     [--context key=value]... [--live-dir DIR]
//
// `--context` is the deploy path's `-c key=value`, repeatable: the stacks that install a
// bootstrap package read its location from CDK context rather than from the cluster settings.
//
// Default target is the deployed template (tools/parity/live/<cluster>-<stack>.json);
// --against synth targets the Python cdk.out under the fixture instead.
//
// The CDK CLI runs in a temp cwd holding only this package's cdk.json and the fixture's
// cdk.context.json, so it performs no lookups and no AWS calls. Path metadata stays on:
// the parity check compares aws:cdk:path.
//
// A stack whose recorded template predates a gated change this branch already has is compared
// against a synthesis from the settings that template was generated from, and the change is
// proved as an itemised delta rather than tolerated. tools/parity/intended-drift.ts holds the
// entry, its reasons and its removal conditions; there is no flag that turns this off.
//
// Exit 0 PARITY, 1 MISMATCH, 2 usage, missing fixture, or a failed synth.

import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import {
  checkIntendedDrift,
  formatIntendedDrift,
  intendedDriftFor,
  settingsLookup,
  unconditionalBaseline,
  writeDeployedInputSettings,
} from './intended-drift.ts';
import { loadTemplate } from './parity.ts';

const PKG = resolve(import.meta.dirname, '../..');
// Used only when the reference template carries no deployment identifier, which is true of the
// synthetic fixtures and of no captured cluster.
const PLACEHOLDER_DEPLOYMENT_ID = '00000000-0000-0000-0000-000000000000';
type DynamoItem = Record<string, unknown>;

const fail = (message: string): never => {
  console.error(message);
  process.exit(2);
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function attributeString(item: DynamoItem, name: string): string | undefined {
  const attribute = item[name];
  return isRecord(attribute) && typeof attribute.S === 'string' ? attribute.S : undefined;
}

function scanItems(file: string): DynamoItem[] {
  const parsed: unknown = JSON.parse(readFileSync(file, 'utf8'));
  if (!isRecord(parsed)) return fail(`${file}: expected a DynamoDB scan object`);
  if (parsed.Items === undefined) return [];
  if (!Array.isArray(parsed.Items) || !parsed.Items.every(isRecord)) return fail(`${file}: Items must be an array of objects`);
  return parsed.Items;
}

function flag(argv: string[], name: string): string | undefined {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
}

/**
 * `--context key=value`, repeatable, turned into the `-c key=value` arguments the deploy path
 * passes. The bootstrap package locations live in CDK context, not in the cluster settings, so
 * without them the emitted user data says the package is not provided.
 */
function contextArgs(argv: string[]): string[] {
  const args: string[] = [];
  for (const [index, value] of argv.entries()) {
    if (value !== '--context') continue;
    const pair = argv[index + 1];
    if (pair === undefined || !pair.includes('=') || pair.startsWith('=')) {
      fail(`--context takes key=value, got ${pair === undefined ? '<nothing>' : JSON.stringify(pair)}`);
    }
    args.push('-c', pair as string);
  }
  return args;
}

/**
 * The deployment identifier the reference template was generated with. The comparison masks it
 * where it is a settings row, but not where it is embedded in a bootstrap package location inside
 * host user data, so taking it from the reference leaves every other property compared.
 */
function referenceDeploymentId(reference: string): string | undefined {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(reference, 'utf8'));
  } catch {
    return undefined;
  }
  if (!isRecord(parsed) || !isRecord(parsed.Resources)) return undefined;
  for (const resource of Object.values(parsed.Resources)) {
    if (!isRecord(resource) || resource.Type !== 'Custom::ClusterSettings') continue;
    const properties = isRecord(resource.Properties) ? resource.Properties : undefined;
    const settings = isRecord(properties?.settings) ? properties.settings : undefined;
    const id = settings?.deployment_id;
    if (typeof id === 'string' && id !== '') return id;
  }
  return undefined;
}

/** The one `*.aws.region` row; the cluster module id is not always `cluster`. */
function regionFromSettings(configFile: string): string {
  const items = scanItems(configFile);
  for (const item of items) {
    const key = attributeString(item, 'key');
    const value = attributeString(item, 'value');
    if (key?.endsWith('.aws.region') && value !== undefined) return value;
  }
  return fail(`${configFile}: no *.aws.region setting`);
}

function moduleName(modulesFile: string, moduleId: string): string {
  const items = scanItems(modulesFile);
  for (const item of items) {
    if (attributeString(item, 'module_id') === moduleId) {
      const name = attributeString(item, 'name');
      if (name !== undefined) return name;
    }
  }
  return fail(`${modulesFile}: no module with id ${moduleId}`);
}

function main(argv: string[]): number {
  // Synthesize the way a deploy does: the wrapper defaults the security scan off, and two
  // stacks trip error-level rules that the reference implementation never sees.
  process.env.IDEA_ADMIN_ENABLE_CDK_NAG_SCAN ??= 'false';
  const cluster = flag(argv, '--cluster');
  const stack = flag(argv, '--stack');
  if (!cluster || !stack) {
    console.error(
      "usage: synth.ts --cluster C --stack MODULE_ID [--against synth] [--ignore-version] [--context key=value]... [--deployment-id ID] [--app-override CMD] [--fixtures DIR] [--live-dir DIR] [--keep]",
    );
    return 2;
  }
  // Validated before anything is created, so a typo exits before a temp directory is left behind.
  const contextFlags = contextArgs(argv);
  const fixtures = join(flag(argv, '--fixtures') ?? join(PKG, 'tools/parity/fixtures'), cluster);
  const liveDir = resolve(flag(argv, "--live-dir") ?? join(PKG, "tools/parity/live"));
  const context = [join(fixtures, 'cdk.context.json'), join(fixtures, 'python/_cdk/cdk.context.json')].find(existsSync);
  if (!context) {
    // Without it the CDK CLI would synthesize a dummy VPC and the diff would be meaningless.
    return fail(`no cdk.context.json for ${cluster} (looked under ${fixtures}); run capture.ts --from-local --cluster ${cluster} --region <region> first`);
  }
  const expected =
    flag(argv, '--against') === 'synth'
      ? join(fixtures, `python/_cdk/cdk.out.${stack}`, `${cluster}-${stack}.template.json`)
      : join(liveDir, `${cluster}-${stack}.json`);
  if (!existsSync(expected)) return fail(`no reference template: ${expected}`);

  const configFile = join(fixtures, 'cluster-settings.json');
  const modulesFile = join(fixtures, 'modules.json');
  const synthReadsFile = join(fixtures, 'synth-reads.json');
  const missing = [configFile, modulesFile, synthReadsFile].filter((file) => !existsSync(file));
  if (missing.length > 0) {
    return fail(`missing replay fixtures: ${missing.join(', ')}; run capture.ts --live --cluster ${cluster} --region <region> (or --from-raw) first`);
  }

  const appOverride = flag(argv, '--app-override');
  const deploymentId = flag(argv, '--deployment-id') ?? referenceDeploymentId(expected) ?? PLACEHOLDER_DEPLOYMENT_ID;
  const expectedTemplate = loadTemplate(expected);
  const drift = intendedDriftFor(cluster, stack, appOverride === undefined ? expectedTemplate : undefined);
  if (drift !== undefined && appOverride !== undefined) {
    // The itemised check needs both syntheses to come from the real app, one per settings shape.
    return fail(`--app-override cannot be used with ${cluster} ${stack}: its recorded template has intended drift that only the real app can reproduce`);
  }
  const appFor = (config: string): string =>
    appOverride ??
    [
      'node',
      // The parity gate checks the current source tree without requiring a shared build output.
      join(PKG, 'src/cdk/app.ts'),
      '--cluster-name', cluster,
      '--aws-region', regionFromSettings(configFile),
      '--module-id', stack,
      '--module-name', moduleName(modulesFile, stack),
      '--deployment-id', deploymentId,
      '--termination-protection', 'true',
      '--config-file', config,
      '--synth-reads', synthReadsFile,
    ].join(' ');

  const cwd = mkdtempSync(join(tmpdir(), `parity-${cluster}-${stack}-`));
  const keep = argv.includes('--keep');
  const ignoreVersion = argv.includes('--ignore-version');
  try {
    copyFileSync(join(PKG, 'cdk.json'), join(cwd, 'cdk.json'));
    copyFileSync(context, join(cwd, 'cdk.context.json'));

    /** One synthesis in the fixture-only cwd. Returns its template path, or nothing on failure. */
    const synthesize = (outDir: string, config: string): string | undefined => {
      const run = spawnSync(
        join(PKG, 'node_modules/.bin/cdk'),
        ['synth', '--app', appFor(config), '--output', outDir, '--no-notices', '--no-version-reporting', '--quiet', ...contextFlags],
        { cwd, stdio: 'inherit' },
      );
      if (run.status !== 0) {
        console.error(`cdk synth failed (exit ${run.status}) in ${cwd}`);
        return undefined;
      }
      const template = join(cwd, outDir, `${cluster}-${stack}.template.json`);
      if (!existsSync(template)) {
        console.error(`cdk synth wrote no ${template}`);
        return undefined;
      }
      return template;
    };

    const asCaptured = synthesize(`cdk.out.${stack}`, configFile);
    if (asCaptured === undefined) return 2;

    // With intended drift the reference template stays the oracle, and the comparison it is the
    // oracle for is the one driven by the settings it was generated from.
    let actual = asCaptured;
    let driftFailures: string[] | undefined;
    if (drift !== undefined) {
      console.log(formatIntendedDrift(drift));
      // With no rows to put back the two syntheses are the same one, so it is not run twice.
      let deployedInput = asCaptured;
      if (Object.keys(drift.deployedInput).length > 0) {
        const deployedInputConfig = join(cwd, 'cluster-settings.deployed-input.json');
        writeDeployedInputSettings(configFile, drift.deployedInput, deployedInputConfig);
        const synthesized = synthesize(`cdk.out.${stack}-deployed-input`, deployedInputConfig);
        if (synthesized === undefined) return 2;
        deployedInput = synthesized;
      }
      actual = deployedInput;
      const settings = settingsLookup(configFile);
      driftFailures = checkIntendedDrift({
        drift,
        deployed: expectedTemplate,
        asCaptured: loadTemplate(asCaptured),
        deployedInputSynth: loadTemplate(deployedInput),
        settings,
        ignoreVersion,
      });
      // A settings-gated cause is undone by synthesizing from the rows the recorded template was
      // generated from. An unconditional one has no row to put back, so it is undone in the
      // template instead, and that is what the plain comparison runs against. Undoing it is not
      // tolerating it: every one of those differences is itemised above and the check just run
      // fails if any is missing, extra, or reverts to anything but the recorded value.
      if (drift.causes.some((cause) => cause.unconditional === true)) {
        const baselineFile = join(cwd, `${cluster}-${stack}.baseline.json`);
        writeFileSync(baselineFile, JSON.stringify(unconditionalBaseline(drift, loadTemplate(deployedInput), settings)));
        actual = baselineFile;
      }
    }

    const diff = spawnSync(
      process.execPath,
      [join(PKG, 'tools/parity/parity.ts'), 'diff', ...(ignoreVersion ? ['--ignore-version'] : []), expected, actual],
      { stdio: 'inherit' },
    );
    let status = diff.status ?? 2;
    if (drift !== undefined && driftFailures !== undefined) {
      for (const failure of driftFailures) console.log(`DRIFT     ${failure}`);
      console.log(
        driftFailures.length === 0
          ? `DRIFT OK  ${drift.differences.length} itemised differences, every one present and reverted to the recorded shape`
          : `DRIFT FAIL  ${driftFailures.length} problem(s) with the itemised expectation`,
      );
      if (driftFailures.length > 0 && status === 0) status = 1;
    }
    if (status === 0 && !keep) rmSync(cwd, { recursive: true, force: true });
    else console.error(`synth output kept in ${cwd}`);
    return status;
  } catch (e) {
    console.error(`${e}`);
    console.error(`synth output kept in ${cwd}`);
    return 2;
  }
}

process.exit(main(process.argv.slice(2)));
