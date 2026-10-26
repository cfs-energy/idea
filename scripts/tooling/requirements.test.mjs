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
    const version = lock.match(/^opensearch-py==(\d+)\.(\d+)\.(\d+)$/m);
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
