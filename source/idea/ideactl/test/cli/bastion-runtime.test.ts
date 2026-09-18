import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { join } from 'node:path';

test('bastion startup persists host identity and validates directory task identities offline', () => {
  const result = spawnSync('python3', ['-B', join(import.meta.dirname, '../containers/bastion-runtime.py')], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
