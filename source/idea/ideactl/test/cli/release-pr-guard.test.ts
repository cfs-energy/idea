import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { load } from 'js-yaml';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..');
const scripts = join(root, 'source/idea/ideactl/scripts');

function run(title: string, files: { version: string; changelog: string }, released: boolean) {
  const directory = mkdtempSync(join(tmpdir(), 'release-pr-'));
  try {
    const bin = join(directory, 'bin');
    mkdirSync(bin);
    mkdirSync(join(directory, 'source/idea/ideactl/scripts'), { recursive: true });
    for (const name of ['assert-release-pr.sh', 'assert-new-release.sh']) cpSync(join(scripts, name), join(directory, 'source/idea/ideactl/scripts', name));
    writeFileSync(join(directory, 'IDEA_VERSION.txt'), `${files.version}\n`);
    writeFileSync(join(directory, 'CHANGELOG.md'), files.changelog);
    writeFileSync(join(bin, 'gh'), released ? '#!/bin/sh\nexit 0\n' : '#!/bin/sh\necho "gh: Not Found (HTTP 404)" >&2\nexit 1\n', { mode: 0o755 });
    const result = spawnSync('bash', ['source/idea/ideactl/scripts/assert-release-pr.sh'], {
      cwd: directory, encoding: 'utf8',
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, PR_TITLE: title, GITHUB_REPOSITORY: 'example/project' },
    });
    return { status: result.status, output: result.stdout + result.stderr };
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

const bumped = { version: '26.09.5', changelog: '# Change Log\n\n## [26.09.5] - 2026-09-29\n' };

test('a release pull request passes only with its version, its heading and no existing release', () => {
  assert.equal(run('26.09.5', bumped, false).status, 0);
  assert.match(run('26.09.5: fixes', { ...bumped, version: '26.09.4' }, false).output, /title says 26\.09\.5 but IDEA_VERSION\.txt says 26\.09\.4/);
  assert.match(run('26.09.5', { ...bumped, changelog: '## [Unreleased]\n' }, false).output, /no ## \[26\.09\.5\] heading/);
  assert.match(run('26.09.5', bumped, true).output, /already exists/);
});

test('other pull requests are not held to a version', () => {
  const result = run('renovate: bump a dependency', { version: '26.09.4', changelog: '## [Unreleased]\n' }, true);
  assert.equal(result.status, 0, result.output);
});

test('the check runs on pull requests before review', () => {
  const workflow = load(readFileSync(join(root, '.github/workflows/ideactl_checks.yaml'), 'utf8')) as { jobs: { checks: { steps: Array<{ name?: string; if?: string; run?: string; env?: Record<string, string> }> } } };
  const step = workflow.jobs.checks.steps.find((candidate) => candidate.run?.includes('assert-release-pr.sh'));
  assert.equal(step?.if, "github.event_name == 'pull_request'");
  assert.equal(step?.env?.PR_TITLE, '${{ github.event.pull_request.title }}');
});
