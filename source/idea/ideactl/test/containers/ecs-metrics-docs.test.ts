import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

test('U-13: metric guidance distinguishes formatter tags from agent enrichment', () => {
  const doc = readFileSync(new URL('../../../../../docs/first-time-users/cluster-operations/update-idea-cluster/ecs-control-plane.md', import.meta.url), 'utf8');
  const introduction = doc.split('\n\n').find((paragraph) => paragraph.startsWith('With `metrics_provider: dogstatsd`'));
  assert.ok(introduction);
  assert.match(introduction, /plus `env` on non-job runtime series/);
  assert.match(doc, /The job formatter preserves its historical module and component tags and omits `env`/);
  assert.match(doc, /origin detection/);
  assert.match(doc, /`service:scheduler`/);
  assert.match(doc, /HTTP backfills bypass the agent and lack this enrichment/);
  assert.match(doc, /overlapping delivery windows by `idea_cluster` can double-count/);
});


test('U-18/U-19/U-23: job timing is reachable from navigation and operator guidance', () => {
  const docs = new URL('../../../../../docs/', import.meta.url);
  const target = new URL('modules/hpc-workloads/admin-documentation/job-completion-timing.md', docs);
  for (const path of ['SUMMARY.md', 'changelog.md', 'first-time-users/cluster-operations/update-idea-cluster/ecs-control-plane.md']) {
    const page = new URL(path, docs);
    const text = readFileSync(page, 'utf8');
    const links = [...text.matchAll(/\[[^\]]+\]\(([^)]+)\)/g)].map((match) => new URL(match[1]!, page).href);
    assert.ok(links.includes(target.href), `${path} links to job completion timing`);
  }
  const changelog = readFileSync(new URL('changelog.md', docs), 'utf8');
  const duration = changelog.split('\n').find((line) => line.startsWith('* **Native job duration**:'));
  assert.ok(duration);
  assert.doesNotMatch(duration, /recover missing timestamps from cached jobs and execution hooks/);
  assert.match(duration, /An endpoint missing from fresh PBS history is never filled from another source/);
  assert.match(readFileSync(target, 'utf8'), /Neither\s+missing endpoint is filled from cached job timestamps or hooks/);
});
