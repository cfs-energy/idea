import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import test from 'node:test';

test('bastion shell configuration and directory task identities remain valid offline', () => {
  const result = spawnSync('python3', ['-B', join(import.meta.dirname, '../../../idea-test-utils/tests/test_bastion_shell.py')], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
