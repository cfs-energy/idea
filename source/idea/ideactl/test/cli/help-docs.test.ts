import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('the administrator guide contains the real root help', () => {
  const result = spawnSync(process.execPath, [new URL('../../src/cli/main.ts', import.meta.url).pathname, '--help'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const guide = readFileSync(new URL('../../../../../docs/developer-portal/idea-cli-utility.md', import.meta.url), 'utf8');
  assert.equal(guide.split('```text\n')[1]?.split('```')[0], result.stdout);
  assert.doesNotMatch(guide, /via `patch`/);
});
