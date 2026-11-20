import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { checkLocks } from './requirements.mjs';
import { root } from './common.mjs';

test('HTTP-only OpenSearch locks exclude the unused gRPC transport', () => {
  for (const name of ['runtime', 'dev']) {
    const lock = readFileSync(join(root, 'requirements', `${name}.txt`), 'utf8');
    assert.doesNotMatch(lock, /^(opensearch-protobufs|grpcio|protobuf)==/m, name);
    const version = lock.match(/^opensearch-py==(\d+)\.(\d+)\.(\d+)(?: \\)?$/m);
    assert.ok(version, `${name} includes the OpenSearch HTTP client`);
    assert.ok(Number(version[1]) < 3 || (Number(version[1]) === 3 && Number(version[2]) < 1));
  }
});

test('lock checks accept identical bytes and reject output drift or orphan locks', () => {
  const directory = mkdtempSync(join(tmpdir(), 'idea-lock-test-'));
  const tracked = join(directory, 'tracked');
  const compiled = join(directory, 'compiled');
  try {
    for (const path of [tracked, compiled]) {
      mkdirSync(path);
      writeFileSync(join(path, 'probe.in'), 'probe==1.0\n');
      writeFileSync(join(path, 'probe.txt'), 'probe==1.0\n');
    }
    assert.doesNotThrow(() => checkLocks(compiled, tracked));
    writeFileSync(join(compiled, 'probe.txt'), 'probe==2.0\n');
    assert.throws(() => checkLocks(compiled, tracked), /Requirements locks differ/);
    writeFileSync(join(compiled, 'probe.txt'), 'probe==1.0\n');
    rmSync(join(compiled, 'probe.in'));
    assert.throws(() => checkLocks(compiled, tracked), /Orphan lock: probe.txt/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('T-21: runtime and development locks exclude retired host-only dependencies', () => {
  for (const file of ['runtime.in', 'runtime.txt', 'dev.txt']) {
    assert.doesNotMatch(readFileSync(join(root, 'requirements', file), 'utf8'), /^(supervisor|cachetools)(?:==|\s*$)/m, file);
  }
});

test('every locked pin carries hashes, setuptools included, and Jinja2 keeps its sandbox floor', () => {
  for (const name of ['runtime', 'dev']) {
    const lock = readFileSync(join(root, 'requirements', `${name}.txt`), 'utf8');
    const entries = lock.split(/\n(?=[a-z0-9])/i).filter((entry) => /^[a-z0-9][^\s]*==/i.test(entry));
    assert.ok(entries.length > 50, name);
    for (const entry of entries) assert.match(entry, /--hash=sha256:[0-9a-f]{64}/, `${name}: ${entry.split(' ')[0]}`);
    assert.match(lock, /^setuptools==/m, `${name} pins setuptools, which an install that requires hashes needs`);
    assert.match(lock, /--generate-hashes/, name);
  }
  assert.match(readFileSync(join(root, 'requirements/runtime.in'), 'utf8'), /^Jinja2>=3\.1\.6$/m);
});

test('the lock tool runs when started through a path with a space or a symlink', async () => {
  const { spawnSync } = await import('node:child_process');
  const { symlinkSync } = await import('node:fs');
  const directory = mkdtempSync(join(tmpdir(), 'idea lock entry '));
  try {
    const link = join(directory, 'requirements.mjs');
    symlinkSync(join(root, 'scripts/tooling/requirements.mjs'), link);
    const result = spawnSync(process.execPath, [link, '--name', 'no-such-lock'], { cwd: root, encoding: 'utf8' });
    assert.notEqual(result.status, 0, 'an entry the guard misses exits 0 having done nothing');
    assert.match(result.stderr, /Unknown requirements: no-such-lock/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
