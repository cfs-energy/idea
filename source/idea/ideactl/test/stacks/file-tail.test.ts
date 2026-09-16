import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { appendFileSync, mkdirSync, mkdtempSync, rmSync, renameSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';

import { fileTailScript } from '../../src/cdk/constructs/container.ts';

// Starting the shell does not mean its followers have opened their files yet.
// Observe a fresh append before exercising writes that must appear exactly once.
async function waitForFollower(path: string, output: () => string): Promise<void> {
  const marker = `follower-ready:${path}\n`;
  const deadline = Date.now() + 10_000;
  while (!output().includes(marker) && Date.now() < deadline) {
    appendFileSync(path, marker);
    await delay(100);
  }
  assert.ok(output().includes(marker), `follower did not read ${path}`);
}

test('discovers date-named and later files while keeping existing followers', { timeout: 30_000 }, async () => {
  const directory = mkdtempSync(join(tmpdir(), 'file-tail-'));
  const logs = join(directory, "logs with ' quotes");
  const laterDirectory = join(directory, 'later');
  mkdirSync(logs);
  mkdirSync(join(logs, 'not-a-file'));
  writeFileSync(join(logs, '20260915'), 'first-day\n');
  writeFileSync(join(logs, '.hidden'), 'hidden-entry\n');
  const script = fileTailScript([logs, laterDirectory, logs]);
  assert.match(script, /^set -euo pipefail/);
  assert.match(script, /sleep 30/);
  assert.doesNotMatch(script, /\*\.log|exec tail/);

  // Short scans exercise daily rollover without waiting for the production interval.
  // The real follower still runs, so lost appends and duplicate readers remain observable.
  const child = spawn('bash', ['-c', `sleep() { command sleep 0.05; }\n${script}`], {
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  let errors = '';
  child.stdout.on('data', (chunk) => { output += chunk; });
  child.stderr.on('data', (chunk) => { errors += chunk; });
  const closed = new Promise<void>((resolve) => child.on('close', () => resolve()));
  async function waitFor(line: string): Promise<void> {
    const deadline = Date.now() + 10_000;
    while (!output.includes(line) && Date.now() < deadline && child.exitCode === null) await delay(20);
    assert.ok(output.includes(line), `${line}: stdout=${output}, stderr=${errors}`);
  }
  try {
    // Files present at start are followed from their end: what they already hold is history
    // (a month of accounting records) and must not replay into the log group on every start.
    await waitForFollower(join(logs, '20260915'), () => output);
    await waitForFollower(join(logs, '.hidden'), () => output);
    appendFileSync(join(logs, '20260915'), 'first-appended\n');
    appendFileSync(join(logs, '.hidden'), 'hidden-appended\n');
    await waitFor('first-appended');
    await waitFor('hidden-appended');
    mkdirSync(laterDirectory);
    writeFileSync(join(laterDirectory, '20260916'), 'second-day\n');
    writeFileSync(join(logs, 'later.log'), 'later-log\n');
    appendFileSync(join(logs, '20260915'), 'still-following\n');
    await waitFor('second-day');
    await waitFor('later-log');
    await waitFor('still-following');
    await delay(150);
    for (const line of ['first-appended', 'hidden-appended', 'second-day', 'later-log', 'still-following']) {
      assert.equal(output.split('\n').filter((entry) => entry === line).length, 1, line);
    }
    for (const line of ['first-day', 'hidden-entry']) {
      assert.equal(output.split('\n').filter((entry) => entry === line).length, 0, `${line} is history and stays out`);
    }
    assert.equal(child.exitCode, null);
    assert.equal(errors, '');
  } finally {
    if (child.pid !== undefined) process.kill(-child.pid, 'SIGKILL');
    await closed;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('an empty directory list remains a valid polling script', async () => {
  const child = spawn('bash', ['-c', `sleep() { exit 0; }\n${fileTailScript([])}`]);
  const code = await new Promise((resolve) => child.on('close', resolve));
  assert.equal(code, 0);
});

test('application rotation does not rediscover the renamed archive', { timeout: 30_000 }, async () => {
  const directory = mkdtempSync(join(tmpdir(), 'application-tail-'));
  const path = join(directory, 'application.log');
  writeFileSync(path, 'history\n');
  const script = fileTailScript([directory], true);
  const child = spawn('bash', ['-c', `sleep() { command sleep 0.05; }\n${script}`], {
    detached: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', (chunk) => { output += chunk; });
  const closed = new Promise<void>((resolve) => child.on('close', () => resolve()));
  async function waitFor(line: string): Promise<void> {
    const deadline = Date.now() + 10_000;
    while (!output.includes(line) && Date.now() < deadline) await delay(25);
    assert.ok(output.includes(line), output);
  }
  try {
    await waitForFollower(path, () => output);
    appendFileSync(path, 'before-rotation\n');
    await waitFor('before-rotation');
    renameSync(path, `${path}.2026-09-15`);
    writeFileSync(path, 'after-rotation\n');
    await waitFor('after-rotation');
    await delay(250);
    assert.equal(output.split('before-rotation').length - 1, 1);
    assert.equal(output.split('after-rotation').length - 1, 1);
    assert.ok(!output.includes('history'));
  } finally {
    if (child.pid !== undefined) process.kill(-child.pid, 'SIGKILL');
    await closed;
    rmSync(directory, { recursive: true, force: true });
  }
});
