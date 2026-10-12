import assert from 'node:assert/strict';
import { spawnSync, type SpawnSyncReturns } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { load } from 'js-yaml';
import { buildProgram } from '../../src/cli/main.ts';
import { fakeDeps } from '../support/deploy-harness.ts';

type Step = { name?: string; run?: string; uses?: string; env?: Record<string, string>; 'working-directory'?: string };
type Job = {
  if?: string;
  'runs-on'?: string;
  strategy?: { matrix: { include: { target: string; runner: string }[] } };
  needs?: string[] | string;
  uses?: string;
  steps?: Step[];
};
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..');
const workflowText = readFileSync(join(root, '.github/workflows/build_push.yaml'), 'utf8');
const workflow = load(workflowText) as { jobs: Record<string, Job>; concurrency: { group: string; 'cancel-in-progress': boolean } };
const scripts = join(root, 'source/idea/ideactl/scripts');
const REAL_JQ = spawnSync('bash', ['-c', 'command -v jq'], { encoding: 'utf8' }).stdout.trim();
assert.ok(REAL_JQ, 'jq is required to exercise the release scripts');

/** The run body of a named step, which must exist. */
function step(job: string, name: string): string {
  const run = workflow.jobs[job]?.steps?.find((candidate) => candidate.name === name)?.run;
  assert.ok(run, `${job}: ${name}`);
  return run;
}
const imageScript = step('build_push_ideactl', 'Build and test temporary image');
const imageNameGuard = step('build_push_ideactl', 'Validate the image name');
const candidateChecksums = step('publish_release_candidate', 'Verify and combine checksums');
const candidatePublication = step('publish_release_candidate', 'Publish the release candidate');
const promoteFind = step('promote_release', 'Find the candidate built from this source');
const promoteDownload = step('promote_release', "Download and verify the candidate's files");
const promoteTag = step('promote_release', 'Tag the candidate image as the release');
const promotePublish = step('promote_release', 'Publish the release');

const DISPATCH_ONLY = "github.event_name == 'workflow_dispatch'";
const BUILD_JOBS = ['build_ideactl_artifacts', 'build_ideactl_linux_artifact', 'build_ideactl_windows_artifact'];
const TREE = 'tree-under-release';
const REPOSITORY = 'registry.example.invalid/idea/idea-control-plane';
const CANDIDATE_IMAGE = `${REPOSITORY}@sha256:candidate`;

test('publication requires both validation workflows and extracted artifact smoke tests', () => {
  assert.equal(workflow.jobs.checks?.uses, './.github/workflows/ideactl_checks.yaml');
  assert.equal(workflow.jobs.tests?.uses, './.github/workflows/unit_tests.yaml');
  for (const name of ['ideactl_checks', 'unit_tests']) {
    const called = load(readFileSync(join(root, `.github/workflows/${name}.yaml`), 'utf8')) as { on: Record<string, unknown> };
    assert.ok('workflow_call' in called.on);
  }
  assert.deepEqual(workflow.jobs.build_push_ideactl?.needs, ['release_available', 'checks', 'tests', ...BUILD_JOBS]);
  assert.deepEqual(workflow.jobs.publish_release_candidate?.needs, [...BUILD_JOBS, 'build_push_ideactl']);
  assert.deepEqual(workflow.jobs.promote_release?.needs, ['release_available']);
  assert.equal(workflow.concurrency['cancel-in-progress'], false);
  assert.ok(workflow.concurrency.group);
  assert.doesNotMatch(workflowText, /--clobber|release upload|release edit|release delete|gh api -X|--method/);
  for (const name of ['build_ideactl_artifacts', 'build_ideactl_linux_artifact']) {
    const smoke = workflow.jobs[name]?.steps?.find((candidate) => candidate.name?.startsWith('Test the extracted'))?.run;
    assert.ok(smoke);
    assert.match(smoke, /tar -xzf .*\.tar\.gz -C "\$SMOKE_ROOT"/);
    assert.match(smoke, /node test\/support\/release-smoke.ts "\$SMOKE_ROOT\/ideactl"/);
  }
  for (const match of workflowText.matchAll(/(?:source\/idea\/ideactl\/)?test\/[^\s]+\.yml/g)) {
    const path = match[0].startsWith('source/') ? match[0] : `source/idea/ideactl/${match[0]}`;
    assert.ok(readFileSync(join(root, path)).length > 0);
  }
});

test('only a dispatch builds, only a numbered dispatch publishes a candidate, only main promotes', () => {
  for (const name of [...BUILD_JOBS, 'build_push_ideactl']) assert.equal(workflow.jobs[name]?.if, DISPATCH_ONLY, name);
  assert.equal(
    workflow.jobs.publish_release_candidate?.if,
    "github.event_name == 'workflow_dispatch' && github.event.inputs.release_candidate != ''",
  );
  assert.equal(workflow.jobs.promote_release?.if, "github.event_name == 'push' && github.ref_name == 'main'");
  // Every job that can write a release or an image tag is one of the three gated jobs.
  for (const [name, job] of Object.entries(workflow.jobs)) {
    const body = (job.steps ?? []).map((candidate) => candidate.run ?? '').join('\n');
    if (/gh release create|imagetools create/.test(body)) {
      assert.ok(['build_push_ideactl', 'publish_release_candidate', 'promote_release'].includes(name), name);
    }
  }
  // The released tags are written in exactly one place: promotion.
  const releaseTagWriters = Object.entries(workflow.jobs).filter(([, job]) =>
    (job.steps ?? []).some((candidate) => /--tag "\$\{REPOSITORY\}:latest"/.test(candidate.run ?? '')));
  assert.deepEqual(releaseTagWriters.map(([name]) => name), ['promote_release']);
});

test('native runners cover each release target and both publications include Windows checksums', () => {
  assert.deepEqual(workflow.jobs.build_ideactl_artifacts?.strategy?.matrix.include, [
    { target: 'darwin-arm64', runner: 'macos-15' },
  ]);
  assert.deepEqual(workflow.jobs.build_ideactl_linux_artifact?.strategy?.matrix.include, [
    { target: 'linux-arm64', runner: 'ubuntu-24.04-arm' }, { target: 'linux-amd64', runner: 'ubuntu-24.04' },
  ]);
  const windows = workflow.jobs.build_ideactl_windows_artifact;
  assert.equal(windows?.['runs-on'], 'windows-2025');
  assert.equal(workflow.jobs.build_push_ideactl?.['runs-on'], 'ubuntu-24.04-arm', 'the image is built natively, never through emulation');
  const setup = readFileSync(join(root, '.github/actions/setup_dev_environment/action.yml'), 'utf8');
  assert.match(setup, /key: venv-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}-/, 'the virtual environment cache is per architecture');
  assert.doesNotMatch(setup, /linux-x86_64/, 'the AWS CLI download follows the runner architecture');
  const smoke = windows?.steps?.find((candidate) => candidate.name?.startsWith('Test the extracted'))?.run ?? '';
  assert.match(smoke, /Expand-Archive/);
  assert.match(smoke, /node test\/support\/release-smoke.ts .*ideactl.exe/);
  assert.match(smoke, /LASTEXITCODE/);
  assert.equal(workflow.jobs.publish_release_candidate?.steps?.find((candidate) => candidate.name === 'Verify and combine checksums')?.['working-directory'], 'release');
  assert.match(candidateChecksums, /\.\/\*\.tar\.gz\.sha256 \.\/\*\.zip\.sha256/);
  assert.match(candidateChecksums, /sha256sum --check SHA256SUMS/);
  assert.match(promoteDownload, /sha256sum --check SHA256SUMS/);
  for (const publication of [candidatePublication, promotePublish]) {
    assert.match(publication, /release\/\*\.zip /);
    assert.match(publication, /release\/\*\.zip\.sha256/);
    assert.match(publication, /release\/SHA256SUMS/);
  }
  assert.match(promotePublish, /unsigned.*SmartScreen.*Run anyway/);
  const container = workflow.jobs.build_ideactl_linux_artifact?.steps?.find((candidate) => candidate.name?.includes('container image'))?.run ?? '';
  assert.match(container, /dist\/release\/\$\{\{ matrix.target \}\}/);
});

/** Stand-in for gh, docker, git, aws and jq that logs every call and answers per test mode. */
const STUB = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const previous = fs.readFileSync(process.env.COMMAND_LOG, 'utf8').trim().split('\\n').filter(Boolean).map(JSON.parse);
fs.appendFileSync(process.env.COMMAND_LOG, JSON.stringify([name, ...args]) + '\\n');
const mode = process.env.RELEASE_TEST_MODE;
const rcMode = process.env.RC_MODE || 'absent';
const releases = process.env.RELEASES_DIR;
function answer(state) {
  if (state === 'existing') process.exit(0);
  console.error(state === 'unauthorized' ? 'gh: Forbidden (HTTP 403)' : state === 'network' ? 'connection failed' : 'gh: Not Found (HTTP 404)');
  process.exit(1);
}
if (name === 'gh') {
  if (args[0] === 'api') {
    const tag = args[1].split('/').pop();
    const candidate = /-rc\\./.test(tag);
    const earlier = previous.filter(c => c[0] === 'gh' && c[1] === 'api' && /-rc\\./.test(c[2].split('/').pop()) === candidate).length;
    const state = candidate ? rcMode : mode;
    if (state === 'late-existing') answer(earlier > 0 ? 'existing' : 'absent');
    answer(state);
  }
  if (args[0] === 'release' && args[1] === 'create') process.exit(mode === 'create-failure' ? 1 : 0);
  if (args[0] === 'release' && args[1] === 'list') {
    const filter = args[args.indexOf('--jq') + 1];
    const result = spawnSync(process.env.REAL_JQ, ['-r', filter, path.join(releases, 'list.json')], { encoding: 'utf8' });
    process.stdout.write(result.stdout); process.stderr.write(result.stderr); process.exit(result.status);
  }
  if (args[0] === 'release' && args[1] === 'view') {
    console.log(mode === 'foreign-author' ? 'someone-else' : 'github-actions[bot]');
    process.exit(0);
  }
  if (args[0] === 'release' && args[1] === 'download') {
    const source = path.join(releases, args[2]);
    const destination = args[args.indexOf('--dir') + 1];
    const pattern = args.includes('--pattern') ? args[args.indexOf('--pattern') + 1] : undefined;
    const files = fs.existsSync(source) ? fs.readdirSync(source).filter(f => pattern === undefined || f === pattern) : [];
    if (files.length === 0) { console.error('no assets match the file pattern'); process.exit(1); }
    fs.mkdirSync(destination, { recursive: true });
    for (const f of files) fs.copyFileSync(path.join(source, f), path.join(destination, f));
    process.exit(0);
  }
  process.exit(0);
}
if (name === 'jq') {
  if (args.includes('.["containerimage.digest"]')) { console.log('sha256:control'); process.exit(0); }
  const result = spawnSync(process.env.REAL_JQ, args, { stdio: 'inherit' });
  process.exit(result.status);
}
if (name === 'git') {
  if (args[0] === 'rev-parse' && args[1] === 'HEAD^{tree}') { console.log(process.env.TREE); process.exit(0); }
  process.exit(1);
}
if (name === 'docker') {
  if (args[0] === 'buildx' && args[1] === 'build' && mode === 'build-failure') process.exit(1);
  if (args[0] === 'buildx' && args[1] === 'imagetools' && args[2] === 'inspect') {
    if (mode === 'missing-image') process.exit(1);
    if (args.includes('--format')) console.log(JSON.stringify(((mode === 'digest-mismatch' && !args[3].includes('-rc.')) || (mode === 'rc-digest-mismatch' && args[3].includes('-rc.'))) ? 'sha256:other' : process.env.TAGGED_DIGEST));
    process.exit(0);
  }
  if (args[0] === 'run') {
    if (mode === 'smoke-failure' ||
        (mode === 'control-failure' && args.includes('about')) || (mode === 'config-failure' && args.includes('generate'))) process.exit(1);
    if (args.includes('generate')) {
      const mount = args.find(a => a.endsWith(':/tmp/config'));
      fs.mkdirSync(path.join(mount.split(':')[0], 'config'), { recursive: true });
      fs.writeFileSync(path.join(mount.split(':')[0], 'config/idea.yml'), 'ok');
    }
  }
}
`;

type Run = { status: number | null; stdout: string; output: string };
type Release = { tag: string; prerelease: boolean; candidate?: Record<string, unknown>; assets?: Record<string, string> };

/** A scratch checkout with stubbed tools; scripts run in it in order, sharing files and one command log. */
class Harness {
  readonly directory = mkdtempSync(join(tmpdir(), 'release-pipeline-'));
  private readonly log = join(this.directory, 'commands.jsonl');
  private readonly releases = join(this.directory, '.releases');
  readonly output = join(this.directory, 'github-output');

  private readonly mode: string;

  constructor(mode: string, options: { missingFixture?: boolean; releases?: Release[] } = {}) {
    this.mode = mode;
    const bin = join(this.directory, '.bin');
    mkdirSync(bin);
    writeFileSync(this.log, '');
    writeFileSync(this.output, '');
    for (const name of ['gh', 'aws', 'docker', 'jq', 'git']) writeFileSync(join(bin, name), STUB, { mode: 0o755 });
    for (const name of ['dist', 'deployment/ecr/idea-control-plane', 'source/idea/ideactl/scripts', 'source/idea/ideactl/test/cli']) {
      mkdirSync(join(this.directory, name), { recursive: true });
    }
    for (const name of ['assert-new-release.sh', 'assert-new-candidate.sh', 'find-release-candidate.sh']) {
      cpSync(join(scripts, name), join(this.directory, 'source/idea/ideactl/scripts', name));
    }
    writeFileSync(join(this.directory, 'IDEA_VERSION.txt'), '1.2.3\n');
    writeFileSync(join(this.directory, 'idea-admin.sh'), 'IDEA_DOCKER_REPO_DEFAULT="registry.example.invalid/idea/idea-control-plane"\n');
    writeFileSync(join(this.directory, 'dist/all-1.2.3.tar.gz'), 'archive');
    writeFileSync(join(this.directory, 'dist/idea-dcv-connection-gateway-1.2.3.tar.gz'), 'archive');
    if (!options.missingFixture) writeFileSync(join(this.directory, 'source/idea/ideactl/test/cli/shell-path-values.yml'), 'fixture');
    mkdirSync(this.releases);
    const list = (options.releases ?? []).map((release) => ({ tagName: release.tag, isPrerelease: release.prerelease }));
    writeFileSync(join(this.releases, 'list.json'), JSON.stringify(list));
    for (const release of options.releases ?? []) {
      const folder = join(this.releases, release.tag);
      mkdirSync(folder);
      for (const [file, body] of Object.entries(release.assets ?? {})) writeFileSync(join(folder, file), body);
      if (release.candidate) writeFileSync(join(folder, 'candidate.json'), JSON.stringify(release.candidate));
    }
  }

  run(script: string, env: Record<string, string> = {}, cwd = this.directory): Run {
    const result: SpawnSyncReturns<string> = spawnSync('bash', ['-c', script], {
      cwd, encoding: 'utf8',
      env: {
        ...process.env, PATH: `${join(this.directory, '.bin')}:${process.env.PATH}`, COMMAND_LOG: this.log, RELEASE_TEST_MODE: this.mode,
        REAL_JQ, RELEASES_DIR: this.releases, TREE, TAGGED_DIGEST: 'sha256:candidate', GITHUB_OUTPUT: this.output,
        GITHUB_REPOSITORY: 'example/project', GITHUB_SHA: 'test-commit', GITHUB_RUN_ID: '5', GITHUB_RUN_ATTEMPT: '2',
        ECR_REGISTRY: 'registry.example.invalid/idea', CONTROL_PLANE_IMAGE_NAME: 'idea-control-plane', RELEASE_CANDIDATE: '', ...env,
      },
    });
    return { status: result.status, stdout: result.stdout, output: result.stdout + result.stderr };
  }

  /** Runs steps in order, as a job does, stopping at the first failure. */
  steps(list: { script: string; env?: Record<string, string>; cwd?: string }[]): Run {
    let last: Run = { status: 0, stdout: '', output: '' };
    for (const item of list) {
      last = this.run(item.script, item.env, item.cwd ? join(this.directory, item.cwd) : this.directory);
      if (last.status !== 0) return last;
    }
    return last;
  }

  get commands(): string[][] {
    return readFileSync(this.log, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line) as string[]);
  }

  close(): void {
    rmSync(this.directory, { recursive: true, force: true });
  }
}

function harness<T>(mode: string, body: (h: Harness) => T, options?: ConstructorParameters<typeof Harness>[1]): T {
  const h = new Harness(mode, options);
  try {
    return body(h);
  } finally {
    h.close();
  }
}

const imagetoolsWrites = (commands: string[][]) =>
  commands.filter((args) => args[0] === 'docker' && args[2] === 'imagetools' && args[3] === 'create');
const releaseWrites = (commands: string[][]) => commands.filter((args) => args[0] === 'gh' && args[1] === 'release' && args[2] === 'create');
const RELEASED_TAGS = [`${REPOSITORY}:v1.2.3`, `${REPOSITORY}:1.2.3`, `${REPOSITORY}:latest`];

test('only an explicit missing release permits publication', () => {
  for (const mode of ['absent', 'existing', 'unauthorized', 'network']) {
    harness(mode, (h) => {
      const result = h.run('bash source/idea/ideactl/scripts/assert-new-release.sh');
      assert.equal(result.status, mode === 'absent' ? 0 : 1, result.output);
      assert.deepEqual(h.commands[0], ['gh', 'api', 'repos/example/project/releases/tags/v1.2.3']);
      if (mode === 'existing') assert.match(result.output, /already exists.*Refusing to overwrite/);
      if (mode === 'network' || mode === 'unauthorized') assert.match(result.output, /Could not verify/);
    });
  }
});

test('a candidate number is checked before any lookup, and only explicit absences of the release and the candidate pass', () => {
  for (const number of ['', '0', '-1', 'abc', '1.5', '01', '2 ', '1;true']) {
    harness('absent', (h) => {
      const result = h.run(`bash source/idea/ideactl/scripts/assert-new-candidate.sh ${JSON.stringify(number)}`);
      assert.notEqual(result.status, 0, `candidate '${number}'`);
      assert.equal(h.commands.length, 0, `candidate '${number}' must not reach GitHub`);
    });
  }
  for (const [release, candidate, status] of [
    ['absent', 'absent', 0], ['existing', 'absent', 1], ['unauthorized', 'absent', 1], ['network', 'absent', 1],
    ['absent', 'existing', 1], ['absent', 'unauthorized', 1], ['absent', 'network', 1],
  ] as const) {
    for (const number of ['1', '12']) {
      harness(release, (h) => {
        const result = h.run(`bash source/idea/ideactl/scripts/assert-new-candidate.sh ${number}`, { RC_MODE: candidate });
        assert.equal(result.status, status, `${release}/${candidate}: ${result.output}`);
        assert.deepEqual(h.commands[0], ['gh', 'api', 'repos/example/project/releases/tags/v1.2.3']);
        if (release === 'absent') assert.deepEqual(h.commands[1], ['gh', 'api', `repos/example/project/releases/tags/v1.2.3-rc.${number}`]);
        if (candidate === 'existing') assert.match(result.output, /already exists\. Use the next number/);
        if (candidate === 'network' || candidate === 'unauthorized') assert.match(result.output, /Could not verify that the release candidate is absent/);
      });
    }
  }
});

test('a dispatch must name a candidate or a private repository, never both and never neither', () => {
  for (const ref of ['main', 'patch']) {
    for (const [name, candidate, status] of [
      ['', '', 1], ['', '3', 0], ['control-test', '', 0], ['control-test', '3', 1],
    ] as const) {
      const result: SpawnSyncReturns<string> = spawnSync('bash', ['-c', imageNameGuard], {
        encoding: 'utf8',
        env: { ...process.env, GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_REF_NAME: ref, CONTROL_PLANE_IMAGE_NAME: name, RELEASE_CANDIDATE: candidate },
      });
      assert.equal(result.status, status, `${ref} name='${name}' candidate='${candidate}': ${result.stderr}`);
    }
  }
});

test('the temporary image is smoked natively, and a candidate takes only its rc tag on that exact digest', () => {
  harness('absent', (h) => {
    const result = h.run(imageScript, { RELEASE_CANDIDATE: '4' });
    assert.equal(result.status, 0, result.output);
    const commands = h.commands;
    const builds = commands.filter((args) => args[0] === 'docker' && args[2] === 'build');
    assert.equal(builds.length, 1);
    assert.equal(builds[0]!.filter((arg) => arg === '-t').length, 1);
    assert.match(builds[0]![builds[0]!.indexOf('-t') + 1] ?? '', /:build-test-commit-5-2$/);
    assert.ok(builds[0]?.includes('linux/arm64'));
    assert.ok(builds[0]?.includes('type=gha,version=2,mode=max,scope=idea-control-plane'), 'intermediate stages are cached');
    const smoke = commands.filter((args) => args[0] === 'docker' && args[1] === 'run');
    assert.equal(smoke.length, 3);
    assert.ok(smoke.every((args) => !args.includes('--platform')), 'the image is built and smoked natively on the arm64 runner');
    assert.ok(smoke.every((args) => args.includes(`${REPOSITORY}@sha256:control`)));
    assert.ok(smoke[0]?.includes('/opt/pbs/sbin/pbs_server.bin --version'));
    assert.deepEqual(smoke[1]?.slice(-2), ['ideactl', 'about']);
    assert.ok(smoke[2]?.includes('generate'));
    const writes = imagetoolsWrites(commands);
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0]!.slice(4), ['--tag', `${REPOSITORY}:1.2.3-rc.4`, `${REPOSITORY}@sha256:control`]);
    assert.ok(commands.indexOf(writes[0]!) > commands.indexOf(smoke.at(-1)!), 'every smoke runs before the tag');
    // The candidate is checked absent again just before its tag is written.
    const lastCandidateCheck = commands.map((args, index) => [args, index] as const)
      .filter(([args]) => args[0] === 'gh' && args[2]?.endsWith('/releases/tags/v1.2.3-rc.4')).at(-1);
    assert.ok(lastCandidateCheck && lastCandidateCheck[1] > commands.indexOf(smoke.at(-1)!));
    assert.match(readFileSync(h.output, 'utf8'), new RegExp(`^image=${REPOSITORY}@sha256:control$`, 'm'));
  });
});

test('a private dispatch writes no tag, and no dispatch ever writes a released tag', () => {
  harness('absent', (h) => {
    const result = h.run(imageScript, { RELEASE_CANDIDATE: '' });
    assert.equal(result.status, 0, result.output);
    assert.equal(imagetoolsWrites(h.commands).length, 0);
    assert.match(result.output, /Private build .*no release tags are written by a dispatch/);
  });
  for (const candidate of ['', '7']) {
    harness('absent', (h) => {
      h.run(imageScript, { RELEASE_CANDIDATE: candidate });
      const tags = imagetoolsWrites(h.commands).flat();
      for (const released of RELEASED_TAGS) assert.ok(!tags.includes(released), `${candidate || 'private'}: ${released}`);
    });
  }
});

test('build, smoke, fixture, or release-check failures never tag an image', () => {
  for (const candidate of ['', '4']) {
    for (const mode of ['existing', 'late-existing', 'network', 'missing-fixture', 'build-failure', 'smoke-failure', 'control-failure', 'config-failure']) {
      harness(mode, (h) => {
        const result = h.run(imageScript, { RELEASE_CANDIDATE: candidate });
        // A private build writes no tag, so a release appearing after its first check does not stop it.
        if (!(candidate === '' && mode === 'late-existing')) assert.notEqual(result.status, 0, `${candidate}/${mode}`);
        assert.equal(imagetoolsWrites(h.commands).length, 0, `${candidate}/${mode}`);
      }, { missingFixture: mode === 'missing-fixture' });
    }
  }
  // A candidate that already exists, or whose lookup fails, when its tag is about to be written.
  for (const rc of ['existing', 'network', 'unauthorized']) {
    harness('absent', (h) => {
      const result = h.run(imageScript, { RELEASE_CANDIDATE: '4', RC_MODE: rc });
      assert.notEqual(result.status, 0, rc);
      assert.equal(imagetoolsWrites(h.commands).length, 0, rc);
    });
  }
});

test('pull requests smoke OpenPBS and ideactl in the single deployment image', () => {
  const script = readFileSync(join(root, 'scripts/ci-smoke-images.sh'), 'utf8');
  harness('absent', (h) => {
    const result = h.run(script);
    assert.equal(result.status, 0, result.output);
    const smoke = h.commands.filter((args) => args[0] === 'docker' && args[1] === 'run');
    assert.equal(smoke.length, 3);
    assert.ok(smoke.every((args) => args.includes('idea-control-plane-ci:latest')));
    assert.ok(smoke[0]?.includes('/opt/pbs/sbin/pbs_server.bin --version'));
  });
  for (const mode of ['smoke-failure', 'control-failure', 'config-failure']) {
    harness(mode, (h) => assert.notEqual(h.run(script).status, 0, mode));
  }
});

/** Release files with genuine checksums, as the build jobs upload them. */
function releaseFiles(): Record<string, string> {
  const files: Record<string, string> = {};
  const sums: string[] = [];
  for (const [name, body] of [['ideactl-v1.2.3-linux-amd64.tar.gz', 'linux'], ['ideactl-v1.2.3-windows-amd64.zip', 'windows']] as const) {
    const hash = createHash('sha256').update(body).digest('hex');
    files[name] = body;
    files[`${name}.sha256`] = `${hash}  ${name}\n`;
    sums.push(`${hash}  ${name}`);
  }
  files['SHA256SUMS'] = sums.sort().join('\n') + '\n';
  return files;
}

test('a candidate is published as a prerelease with its tree, commit and image digest, once', () => {
  const run = (h: Harness, env: Record<string, string>) => {
    mkdirSync(join(h.directory, 'release'));
    for (const [name, body] of Object.entries(releaseFiles())) if (name !== 'SHA256SUMS') writeFileSync(join(h.directory, 'release', name), body);
    return h.steps([
      { script: candidateChecksums, cwd: 'release' },
      { script: candidatePublication, env: { RELEASE_CANDIDATE: '4', IMAGE: `${REPOSITORY}@sha256:control`, ...env } },
    ]);
  };
  harness('absent', (h) => {
    const result = run(h, {});
    assert.equal(result.status, 0, result.output);
    const writes = releaseWrites(h.commands);
    assert.equal(writes.length, 1);
    const create = writes[0]!;
    assert.equal(create[3], 'v1.2.3-rc.4');
    assert.ok(create.includes('--prerelease'));
    assert.equal(create[create.indexOf('--target') + 1], 'test-commit');
    for (const file of ['release/ideactl-v1.2.3-windows-amd64.zip', 'release/ideactl-v1.2.3-windows-amd64.zip.sha256', 'release/SHA256SUMS', 'release/candidate.json']) {
      assert.ok(create.includes(file), file);
    }
    assert.equal(h.run('cd release && sha256sum --check SHA256SUMS').status, 0);
    assert.match(readFileSync(join(h.directory, 'release/SHA256SUMS'), 'utf8'), /[a-f0-9]{64}  candidate\.json/);
    assert.deepEqual(JSON.parse(readFileSync(join(h.directory, 'release/candidate.json'), 'utf8')), {
      version: '1.2.3', candidate: 4, tree: TREE, commit: 'test-commit', image: `${REPOSITORY}@sha256:control`,
    });
    assert.match(create[create.indexOf('--notes') + 1] ?? '', /idea-control-plane:1\.2\.3-rc\.4 \(sha256:control\)/);
  });
  for (const [label, mode, env] of [
    ['candidate exists', 'absent', { RC_MODE: 'existing' }],
    ['candidate lookup fails', 'absent', { RC_MODE: 'network' }],
    ['release exists', 'existing', {}],
    ['image without a digest', 'absent', { IMAGE: `${REPOSITORY}:1.2.3-rc.4` }],
    ['no image', 'absent', { IMAGE: '' }],
  ] as const) {
    harness(mode, (h) => {
      const result = run(h, env);
      assert.notEqual(result.status, 0, label);
      assert.equal(releaseWrites(h.commands).length, 0, label);
    });
  }
  harness('absent', (h) => {
    const files = releaseFiles();
    files['ideactl-v1.2.3-linux-amd64.tar.gz'] = 'tampered';
    mkdirSync(join(h.directory, 'release'));
    for (const [name, body] of Object.entries(files)) if (name !== 'SHA256SUMS') writeFileSync(join(h.directory, 'release', name), body);
    const result = h.steps([
      { script: candidateChecksums, cwd: 'release' },
      { script: candidatePublication, env: { RELEASE_CANDIDATE: '4', IMAGE: `${REPOSITORY}@sha256:control` } },
    ]);
    assert.notEqual(result.status, 0, 'a file that does not match its checksum');
    assert.equal(releaseWrites(h.commands).length, 0);
  });
});

/** Prerelease fixtures: the version, its tree and its digest per tag. */
function candidate(tag: string, version: string, tree: string, assets = releaseFiles()): Release {
  const number = Number(tag.split('-rc.')[1]);
  const manifest = { version, candidate: number, tree, commit: `commit-${tag}`, image: CANDIDATE_IMAGE };
  assets['SHA256SUMS'] += `${createHash('sha256').update(JSON.stringify(manifest)).digest('hex')}  candidate.json\n`;
  return { tag, prerelease: true, assets, candidate: manifest };
}

test('promotion finds the highest candidate built from exactly this tree and version', () => {
  const releases: Release[] = [
    { tag: 'v1.2.2', prerelease: false },
    candidate('v1.2.3-rc.2', '1.2.3', TREE),
    candidate('v1.2.3-rc.10', '1.2.3', TREE),
    candidate('v1.2.3-rc.11', '1.2.3', 'other-tree'),
    { tag: 'v1.2.3-rc.12', prerelease: true },
    candidate('v1.2.3-rc.13', '9.9.9', TREE),
    candidate('v1.2.30-rc.99', '1.2.30', TREE),
    candidate('v1x2y3-rc.98', '1.2.3', TREE),
    { ...candidate('v1.2.3-rc.97', '1.2.3', TREE), prerelease: false },
  ];
  harness('absent', (h) => {
    const result = h.run('bash source/idea/ideactl/scripts/find-release-candidate.sh');
    assert.equal(result.status, 0, result.output);
    assert.equal(result.stdout, 'v1.2.3-rc.10\n', 'stdout carries only the tag');
    assert.match(result.output, /v1\.2\.3-rc\.13 was built from tree/);
    assert.match(result.output, /v1\.2\.3-rc\.11 was built from tree other-tree/);
    assert.match(result.output, /v1\.2\.3-rc\.12 has no candidate\.json; skipped/);
    const downloads = h.commands.filter((args) => args[1] === 'release' && args[2] === 'download').map((args) => args[3]);
    assert.ok(!downloads.includes('v1.2.30-rc.99') && !downloads.includes('v1x2y3-rc.98') && !downloads.includes('v1.2.3-rc.97'));
  }, { releases });
  for (const set of [
    [],
    [candidate('v1.2.3-rc.1', '1.2.3', 'other-tree'), { tag: 'v1.2.3-rc.2', prerelease: true }, candidate('v1.2.30-rc.1', '1.2.30', TREE)],
  ]) {
    harness('absent', (h) => {
      const result = h.run('bash source/idea/ideactl/scripts/find-release-candidate.sh');
      assert.notEqual(result.status, 0);
      assert.equal(result.stdout, '');
      assert.match(result.output, /No release candidate for v1\.2\.3 was built from tree tree-under-release/);
    }, { releases: set });
  }
});

test('promotion warns and skips malformed or incomplete candidate manifests', () => {
  for (const manifest of ['{bad json', '{}', '{"version":"1.2.3"}', '{"tree":"tree-under-release"}',
    '{"version":"1.2.3","tree":null}', '{"version":"1.2.3","tree":""}', '{"version":123,"tree":"tree-under-release"}']) {
    const malformed = { tag: 'v1.2.3-rc.2', prerelease: true, assets: { 'candidate.json': manifest } };
    for (const valid of [true, false]) {
      harness('absent', (h) => {
        const result = h.run('bash source/idea/ideactl/scripts/find-release-candidate.sh');
        assert.equal(result.status, valid ? 0 : 1, result.output);
        assert.match(result.output, /::warning::v1\.2\.3-rc\.2 has invalid or incomplete candidate\.json; skipped\./);
        assert.equal(result.stdout, valid ? 'v1.2.3-rc.1\n' : '');
        if (!valid) assert.match(result.output, /::error::No release candidate/);
      }, { releases: [malformed, ...(valid ? [candidate('v1.2.3-rc.1', '1.2.3', TREE)] : [])] });
    }
  }
});

/** The promote job's steps in order, with the candidate tag passed on as the workflow does. */
function promote(h: Harness): Run {
  const found = h.run(promoteFind);
  if (found.status !== 0) return found;
  const tag = /^tag=(.*)$/m.exec(readFileSync(h.output, 'utf8'))?.[1] ?? '';
  return h.steps([
    { script: promoteDownload, env: { TAG: tag } },
    { script: promoteTag, env: { CANDIDATE_TAG: tag } },
    { script: promotePublish, env: { TAG: tag } },
  ]);
}

test('promotion re-tags the candidate digest and republishes its files unchanged', () => {
  harness('absent', (h) => {
    const result = promote(h);
    assert.equal(result.status, 0, result.output);
    const commands = h.commands;
    const writes = imagetoolsWrites(commands);
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0]!.slice(4), [
      '--tag', `${REPOSITORY}:v1.2.3`, '--tag', `${REPOSITORY}:1.2.3`, '--tag', `${REPOSITORY}:latest`, CANDIDATE_IMAGE,
    ]);
    const write = commands.indexOf(writes[0]!);
    const verified = commands.slice(write + 1).filter((args) => args[2] === 'imagetools' && args[3] === 'inspect' && args.includes('--format'));
    assert.deepEqual(verified.map((args) => args[4]), RELEASED_TAGS, 'each released tag is read back after the write');
    const releasesCreated = releaseWrites(commands);
    assert.equal(releasesCreated.length, 1);
    const create = releasesCreated[0]!;
    assert.equal(create[3], 'v1.2.3');
    assert.ok(!create.includes('--prerelease'));
    assert.equal(create[create.indexOf('--target') + 1], 'test-commit');
    assert.ok(commands.indexOf(create) > commands.indexOf(verified.at(-1)!));
    for (const file of ['release/ideactl-v1.2.3-windows-amd64.zip', 'release/ideactl-v1.2.3-windows-amd64.zip.sha256', 'release/SHA256SUMS']) {
      assert.ok(create.includes(file), file);
    }
    assert.ok(create.includes('release/candidate.json'));
    const notes = create[create.indexOf('--notes') + 1] ?? '';
    assert.match(notes, /Released unchanged from v1\.2\.3-rc\.3: .*digest sha256:candidate\./);
    assert.match(notes, /unsigned.*SmartScreen.*Run anyway/);
    // The released files are the candidate's bytes.
    for (const [name, body] of Object.entries(candidate('v1.2.3-rc.3', '1.2.3', TREE).assets!)) assert.equal(readFileSync(join(h.directory, 'release', name), 'utf8'), body, name);
  }, { releases: [candidate('v1.2.3-rc.3', '1.2.3', TREE)] });
});

test('promotion stops before any tag or release on a missing, altered or unverifiable candidate', () => {
  const tampered = releaseFiles();
  tampered['ideactl-v1.2.3-windows-amd64.zip'] = 'tampered';
  const undigested = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  undigested.candidate!.image = `${REPOSITORY}:1.2.3-rc.3`;
  const unnamed = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  unnamed.candidate!.image = '';
  const wrongRepository = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  wrongRepository.candidate!.image = 'foreign.invalid/repository@sha256:candidate';
  for (const release of [undigested, unnamed, wrongRepository]) {
    release.assets!['SHA256SUMS'] = releaseFiles()['SHA256SUMS']! +
      `${createHash('sha256').update(JSON.stringify(release.candidate)).digest('hex')}  candidate.json\n`;
  }
  const tamperedManifest = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  tamperedManifest.candidate!.commit = 'altered';
  const missingManifestChecksum = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  missingManifestChecksum.assets!['SHA256SUMS'] = releaseFiles()['SHA256SUMS']!;
  const cases: [string, string, Release[], { tags: number }][] = [
    ['foreign release author', 'foreign-author', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    ['tampered candidate manifest', 'absent', [tamperedManifest], { tags: 0 }],
    ['manifest checksum missing', 'absent', [missingManifestChecksum], { tags: 0 }],
    ['wrong repository', 'absent', [wrongRepository], { tags: 0 }],
    ['rc tag digest mismatch', 'rc-digest-mismatch', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    ['no candidate', 'absent', [], { tags: 0 }],
    ['candidate from another tree', 'absent', [candidate('v1.2.3-rc.3', '1.2.3', 'other-tree')], { tags: 0 }],
    ['file altered after the candidate was proven', 'absent', [candidate('v1.2.3-rc.3', '1.2.3', TREE, tampered)], { tags: 0 }],
    ['candidate image without a digest', 'absent', [undigested], { tags: 0 }],
    ['candidate without an image', 'absent', [unnamed], { tags: 0 }],
    ['candidate image no longer in the registry', 'missing-image', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    ['release already exists', 'existing', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    ['release lookup fails', 'network', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    // The registry answers with another digest after the write: the release is not published.
    ['released tag reads back another digest', 'digest-mismatch', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 1 }],
  ];
  for (const [label, mode, releases, expected] of cases) {
    harness(mode, (h) => {
      const result = promote(h);
      assert.notEqual(result.status, 0, label);
      assert.equal(imagetoolsWrites(h.commands).length, expected.tags, label);
      assert.equal(releaseWrites(h.commands).length, 0, label);
    }, { releases });
  }
});

test('the release is created only when absent and never replaced', () => {
  for (const mode of ['absent', 'existing', 'late-existing', 'network', 'unauthorized']) {
    harness(mode, (h) => {
      mkdirSync(join(h.directory, 'release'));
      for (const [name, body] of Object.entries(releaseFiles())) writeFileSync(join(h.directory, 'release', name), body);
      writeFileSync(join(h.directory, 'release/candidate.json'), JSON.stringify({ image: CANDIDATE_IMAGE }));
      // A release that appears between the tag step and publication is not replaced.
      if (mode === 'late-existing') h.run('bash source/idea/ideactl/scripts/assert-new-release.sh');
      const result = h.run(promotePublish, { TAG: 'v1.2.3-rc.3' });
      assert.equal(result.status, mode === 'absent' ? 0 : 1, `${mode}: ${result.output}`);
      const writes = releaseWrites(h.commands);
      assert.equal(writes.length, mode === 'absent' ? 1 : 0, mode);
      if (writes[0]) {
        assert.deepEqual(writes[0].slice(0, 4), ['gh', 'release', 'create', 'v1.2.3']);
        assert.ok(writes[0].includes('test-commit'));
      }
    });
  }
  assert.ok(existsSync(join(scripts, 'assert-new-release.sh')));
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
