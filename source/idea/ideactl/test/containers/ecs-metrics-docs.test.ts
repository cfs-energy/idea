import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

test('S-30: the metrics introduction limits env to non-job runtime series', () => {
  const doc = readFileSync(new URL('../../../../../docs/first-time-users/cluster-operations/update-idea-cluster/ecs-control-plane.md', import.meta.url), 'utf8');
  const introduction = doc.split('\n\n').find((paragraph) => paragraph.startsWith('With `metrics_provider: dogstatsd`'));
  assert.ok(introduction);
  assert.match(introduction, /plus `env` on non-job runtime series/);
  assert.match(doc, /Job series also retain their historical module and component tags without `env`/);
});
