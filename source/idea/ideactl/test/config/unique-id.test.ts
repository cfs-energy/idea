import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { makeUniqueId } from '../../src/util/unique-id.ts';
import { requireCapture } from '../support/fixtures.ts';

const LIVE_DIR = fileURLToPath(new URL('../../tools/parity/live', import.meta.url));
requireCapture([LIVE_DIR], "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2");

test('makeUniqueId: rules that do not need fixtures', () => {
  // single component: no hash
  assert.equal(makeUniqueId(['idea-dev27-cluster-external-cert', 'Default']), 'ideadev27clusterexternalcert');
  // `Resource` dropped from the human part but kept in the hash
  assert.equal(makeUniqueId(['log-retention', 'Resource']), 'logretentionB69DFB48');
  // A component ending with the next component is deduplicated.
  assert.equal(makeUniqueId(['LogRetention', 'Resource']), 'LogRetentionDD0A1FA1');
  assert.throws(() => makeUniqueId(['Default']), /empty set of components/);
});

test('makeUniqueId: 240-char truncation, hash still appended', () => {
  const components = ['a'.repeat(300), 'b'];
  const id = makeUniqueId(components);
  assert.equal(id.length, 240 + 8);
  assert.equal(id.slice(0, 240), 'a'.repeat(240));
});

test('makeUniqueId reproduces every live logical id from aws:cdk:path', () => {
  const files = readdirSync(LIVE_DIR).filter((f) => f.endsWith('.json')).sort();
  assert.ok(files.length > 0, `Required fixture directory has no templates: ${LIVE_DIR}`);

  let checked = 0;
  let noPath = 0;
  const failures: string[] = [];
  for (const file of files) {
    const template = JSON.parse(readFileSync(join(LIVE_DIR, file), 'utf8')) as {
      Resources?: Record<string, { Metadata?: Record<string, unknown> }>;
    };
    for (const [logicalId, resource] of Object.entries(template.Resources ?? {})) {
      if (logicalId === 'CDKMetadata') continue;
      const path = resource.Metadata?.['aws:cdk:path'];
      if (typeof path !== 'string') {
        noPath += 1;
        continue;
      }
      // strip the leading stack-name component
      const components = path.split('/').slice(1);
      const actual = makeUniqueId(components);
      if (actual !== logicalId) {
        failures.push(`${file} ${path}: expected ${logicalId}, got ${actual}`);
      }
      checked += 1;
    }
  }

  assert.deepEqual(failures, []);
  // 312 logical ids across the eleven Python-deployed templates, plus the 25 of the container
  // capacity stack's template, which joined the reference when that stack was first deployed.
  assert.equal(checked, 337);
});
