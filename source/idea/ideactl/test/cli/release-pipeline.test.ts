import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { load } from 'js-yaml';
import { buildProgram } from '../../src/cli/main.ts';
import { fakeDeps } from '../support/deploy-harness.ts';

type Step = { name?: string; run?: string; uses?: string };
type Job = { 'runs-on'?: string; strategy?: { matrix: { include: { target: string; runner: string }[] } };  needs?: string[]; uses?: string; steps?: Step[] };
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..');
const workflowText = readFileSync(join(root, '.github/workflows/build_push.yaml'), 'utf8');
const workflow = load(workflowText) as { jobs: Record<string, Job>; concurrency: { group: string; 'cancel-in-progress': boolean } };
const guard = join(root, 'source/idea/ideactl/scripts/assert-new-release.sh');
const imageScript = workflow.jobs.build_push_ideactl?.steps?.find((step) => step.name === 'Build and test temporary images')?.run;
assert.ok(imageScript);

test('publication requires both validation workflows and extracted artifact smoke tests', () => {
  assert.equal(workflow.jobs.checks?.uses, './.github/workflows/ideactl_checks.yaml');
  assert.equal(workflow.jobs.tests?.uses, './.github/workflows/unit_tests.yaml');
  for (const name of ['ideactl_checks', 'unit_tests']) {
    const called = load(readFileSync(join(root, `.github/workflows/${name}.yaml`), 'utf8')) as { on: Record<string, unknown> };
    assert.ok('workflow_call' in called.on);
  }
  assert.deepEqual(workflow.jobs.build_push_ideactl?.needs, ['release_available', 'checks', 'tests', 'build_ideactl_artifacts', 'build_ideactl_linux_artifact', 'build_ideactl_windows_artifact']);
  assert.ok(workflow.jobs.publish_ideactl_artifacts?.needs?.includes('build_push_ideactl'));
  assert.equal(workflow.concurrency['cancel-in-progress'], false);
  assert.ok(workflow.concurrency.group);
  assert.doesNotMatch(workflowText, /--clobber|release upload/);
  for (const name of ['build_ideactl_artifacts', 'build_ideactl_linux_artifact']) {
    const smoke = workflow.jobs[name]?.steps?.find((step) => step.name?.startsWith('Test the extracted'))?.run;
    assert.ok(smoke);
    assert.match(smoke, /tar -xzf .*\.tar\.gz -C "\$SMOKE_ROOT"/);
    assert.match(smoke, /node test\/support\/release-smoke.ts "\$SMOKE_ROOT\/ideactl"/);
  }
  for (const match of workflowText.matchAll(/(?:source\/idea\/ideactl\/)?test\/[^\s]+\.yml/g)) {
    const path = match[0].startsWith('source/') ? match[0] : `source/idea/ideactl/${match[0]}`;
    assert.ok(readFileSync(join(root, path)).length > 0);
  }
});

test('native runners cover each release target and publication includes Windows checksums', () => {
  assert.deepEqual(workflow.jobs.build_ideactl_artifacts?.strategy?.matrix.include, [
    { target: 'darwin-arm64', runner: 'macos-15' },
  ]);
  assert.deepEqual(workflow.jobs.build_ideactl_linux_artifact?.strategy?.matrix.include, [
    { target: 'linux-arm64', runner: 'ubuntu-24.04-arm' }, { target: 'linux-amd64', runner: 'ubuntu-24.04' },
  ]);
  const windows = workflow.jobs.build_ideactl_windows_artifact;
  assert.equal(windows?.['runs-on'], 'windows-2025');
  assert.equal(workflow.jobs.build_push_ideactl?.['runs-on'], 'ubuntu-24.04-arm', 'the images are built natively, never through emulation');
  const setup = readFileSync(join(root, '.github/actions/setup_dev_environment/action.yml'), 'utf8');
  assert.match(setup, /key: venv-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}-/, 'the virtual environment cache is per architecture');
  assert.doesNotMatch(setup, /linux-x86_64/, 'the AWS CLI download follows the runner architecture');
  const smoke = windows?.steps?.find((step) => step.name?.startsWith('Test the extracted'))?.run ?? '';
  assert.match(smoke, /Expand-Archive/);
  assert.match(smoke, /node test\/support\/release-smoke.ts .*ideactl.exe/);
  assert.match(smoke, /LASTEXITCODE/);
  assert.ok(workflow.jobs.publish_ideactl_artifacts?.needs?.includes('build_ideactl_windows_artifact'));
  const checksums = workflow.jobs.publish_ideactl_artifacts?.steps?.find((step) => step.name === 'Verify and combine checksums')?.run ?? '';
  assert.match(checksums, /\.\/\*\.tar\.gz\.sha256 \.\/\*\.zip\.sha256/);
  assert.match(checksums, /sha256sum --check SHA256SUMS/);
  const publication = workflow.jobs.publish_ideactl_artifacts?.steps?.find((step) => step.name === 'Publish source release')?.run ?? '';
  assert.match(publication, /release\/\*\.zip/);
  assert.match(publication, /unsigned.*SmartScreen.*Run anyway/);
  const container = workflow.jobs.build_ideactl_linux_artifact?.steps?.find((step) => step.name?.includes('container image'))?.run ?? '';
  assert.match(container, /dist\/release\/\$\{\{ matrix.target \}\}/);
});

function exercise(mode: string, script: string): { status: number | null; output: string; commands: string[][] } {
  const directory = mkdtempSync(join(tmpdir(), 'release-pipeline-'));
  try {
    const bin = join(directory, 'bin');
    mkdirSync(bin);
    const log = join(directory, 'commands.jsonl');
    writeFileSync(log, '');
    const stub = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const commands = fs.readFileSync(process.env.COMMAND_LOG, 'utf8').trim().split('\\n').filter(Boolean).map(JSON.parse);
fs.appendFileSync(process.env.COMMAND_LOG, JSON.stringify([name, ...args]) + '\\n');
const mode = process.env.RELEASE_TEST_MODE;
if (name === 'gh') {
  if (args[0] === 'release') process.exit(0);
  const count = commands.filter(c => c[0] === 'gh').length;
  if (mode === 'existing' || (mode === 'late-existing' && count > 0)) process.exit(0);
  console.error(mode === 'unauthorized' ? 'gh: Forbidden (HTTP 403)' : mode === 'network' ? 'connection failed' : 'gh: Not Found (HTTP 404)');
  process.exit(1);
}
if (name === 'jq') console.log(args.at(-1).includes('scheduler') ? 'sha256:scheduler' : 'sha256:control');
if (name === 'docker' && args[0] === 'buildx' && args[1] === 'build' && mode === 'build-failure') process.exit(1);
if (name === 'docker' && args[0] === 'run') {
  if (mode === 'smoke-failure' ||
      (mode === 'control-failure' && args.includes('about')) || (mode === 'config-failure' && args.includes('generate'))) process.exit(1);
  if (args.includes('generate')) {
    const mount = args.find(a => a.endsWith(':/tmp/config'));
    fs.mkdirSync(path.join(mount.split(':')[0], 'config'), { recursive: true });
    fs.writeFileSync(path.join(mount.split(':')[0], 'config/idea.yml'), 'ok');
  }
}
`;
    for (const name of ['gh', 'aws', 'docker', 'jq']) writeFileSync(join(bin, name), stub, { mode: 0o755 });
    for (const name of ['dist', 'deployment/ecr/idea-scheduler-pbs', 'deployment/ecr/idea-control-plane', 'source/idea/ideactl/scripts', 'source/idea/ideactl/test/cli']) mkdirSync(join(directory, name), { recursive: true });
    cpSync(guard, join(directory, 'source/idea/ideactl/scripts/assert-new-release.sh'));
    writeFileSync(join(directory, 'IDEA_VERSION.txt'), '1.2.3\n');
    writeFileSync(join(directory, 'dist/all-1.2.3.tar.gz'), 'archive');
    writeFileSync(join(directory, 'dist/idea-dcv-connection-gateway-1.2.3.tar.gz'), 'archive');
    if (mode !== 'missing-fixture') writeFileSync(join(directory, 'source/idea/ideactl/test/cli/shell-path-values.yml'), 'fixture');
    const result = spawnSync('bash', ['-c', script], {
      cwd: directory, encoding: 'utf8',
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, COMMAND_LOG: log, RELEASE_TEST_MODE: mode,
        GITHUB_REPOSITORY: 'example/project', GITHUB_SHA: 'test-commit', GITHUB_RUN_ID: '5', GITHUB_RUN_ATTEMPT: '2',
        ECR_REGISTRY: 'registry.example.invalid', SCHEDULER_IMAGE_NAME: 'scheduler', CONTROL_PLANE_IMAGE_NAME: 'control' },
    });
    return { status: result.status, output: result.stdout + result.stderr, commands: readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line) as string[]) };
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test('only an explicit missing release permits publication', () => {
  for (const mode of ['absent', 'existing', 'unauthorized', 'network']) {
    const result = exercise(mode, 'bash source/idea/ideactl/scripts/assert-new-release.sh');
    assert.equal(result.status, mode === 'absent' ? 0 : 1, result.output);
    assert.deepEqual(result.commands[0], ['gh', 'api', 'repos/example/project/releases/tags/v1.2.3']);
    if (mode === 'existing') assert.match(result.output, /already exists.*Refusing to overwrite/);
    if (mode === 'network' || mode === 'unauthorized') assert.match(result.output, /Could not verify/);
  }
});

test('temporary images are smoked natively before their exact digests are promoted', () => {
  const result = exercise('absent', imageScript);
  assert.equal(result.status, 0, result.output);
  const builds = result.commands.filter((args) => args[0] === 'docker' && args[2] === 'build');
  assert.equal(builds.length, 2);
  for (const build of builds) {
    assert.equal(build.filter((arg) => arg === '-t').length, 1);
    assert.match(build[build.indexOf('-t') + 1] ?? '', /:build-test-commit-5-2$/);
  }
  assert.ok(builds[1]?.includes('PBS_IMAGE=registry.example.invalid/scheduler@sha256:scheduler'));
  const smoke = result.commands.filter((args) => args[1] === 'run');
  assert.equal(smoke.length, 3);
  assert.ok(smoke.every((args) => !args.includes('--platform')), 'the images are built and smoked natively on the arm64 runner');
  const promotions = result.commands.filter((args) => args[2] === 'imagetools');
  assert.equal(promotions.length, 2);
  for (const [index, name] of ['scheduler', 'control'].entries()) {
    const promotion = promotions[index];
    assert.ok(promotion);
    assert.deepEqual(promotion.slice(4), ['--tag', `registry.example.invalid/${name}:v1.2.3`, '--tag', `registry.example.invalid/${name}:1.2.3`, '--tag', `registry.example.invalid/${name}:latest`, `registry.example.invalid/${name}@sha256:${name}`]);
    assert.ok(result.commands.indexOf(promotion) > result.commands.indexOf(smoke.at(-1)!));
  }
});

test('build, smoke, fixture, or release-check failures never promote image tags', () => {
  for (const mode of ['existing', 'late-existing', 'network', 'missing-fixture', 'build-failure', 'smoke-failure', 'control-failure', 'config-failure']) {
    const result = exercise(mode, imageScript);
    assert.notEqual(result.status, 0, mode);
    assert.equal(result.commands.some((args) => args[2] === 'imagetools'), false, mode);
  }
});

test('only complete CDK replay bypasses the live identity banner', async () => {
  for (const replay of [[], ['--config-file', 'settings.json'], ['--synth-reads', 'reads.json'], ['--config-file', 'settings.json', '--synth-reads', 'reads.json']]) {
    let calls = 0;
    const deps = fakeDeps();
    deps.callerIdentity = async () => { calls++; return { account: '1'.repeat(12), arn: 'synthetic-identity', userId: 'test' }; };
    const program = buildProgram(deps);
    program.commands.find((command) => command.name() === 'cdk')?.commands.find((command) => command.name() === 'cdk-app')?.action(() => {});
    await program.parseAsync(['cdk', 'cdk-app', '--cluster-name', 'idea-test1', '--aws-region', 'us-east-2', '--module-id', 'identity-provider', '--module-name', 'identity-provider', ...replay], { from: 'user' });
    assert.equal(calls, replay.length === 4 ? 0 : 1);
  }
});


test('the publishing step creates only an absent release and never uploads replacements', () => {
  const script = workflow.jobs.publish_ideactl_artifacts?.steps?.find((step) => step.name === 'Publish source release')?.run;
  assert.ok(script);
  for (const mode of ['absent', 'existing', 'network']) {
    const result = exercise(mode, script);
    assert.equal(result.status, mode === 'absent' ? 0 : 1, result.output);
    const writes = result.commands.filter((args) => args[0] === 'gh' && args[1] === 'release');
    assert.equal(writes.length, mode === 'absent' ? 1 : 0);
    if (writes[0]) {
      assert.deepEqual(writes[0].slice(0, 4), ['gh', 'release', 'create', 'v1.2.3']);
      assert.ok(writes[0].includes('test-commit'));
    }
  }
});
