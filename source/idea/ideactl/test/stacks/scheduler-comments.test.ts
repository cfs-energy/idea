import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

test('S-25: the ECS-only scheduler does not describe a removed record handover', () => {
  const source = readFileSync(new URL('../../src/cdk/stacks/scheduler.ts', import.meta.url), 'utf8');
  const constructor = source.slice(source.indexOf('  constructor('), source.indexOf('  private get containerScope'));
  assert.doesNotMatch(constructor, /retain-only deploy|routing moves/);
});
