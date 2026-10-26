import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { generateConfigFromTemplates, flattenConfigDir } from '../../src/config/generator.ts';
import { loadValuesFile } from '../../src/config/values.ts';
import { ideaVersion } from '../../src/version.ts';

test('P-61: fresh GovCloud configuration has a usable public release image', () => {
  const dir = mkdtempSync(join(tmpdir(), 'govcloud-image-'));
  try {
    const values = loadValuesFile(join(import.meta.dirname, '../stacks/ecs-values.yml'));
    generateConfigFromTemplates({ ...values, aws_partition: 'aws-us-gov', aws_region: 'us-gov-west-1' }, dir);
    const config = flattenConfigDir(dir);
    assert.equal(config['ecs.image'], `public.ecr.aws/s5o2b4m0/idea-control-plane:${ideaVersion()}`);
    assert.equal(config['ecs.image_repositories.aws-us-gov'], null);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('P-57 S-33: operator guides distinguish initial image and upgrade repository selection', () => {
  const docs = resolve(import.meta.dirname, '../../../../../docs/first-time-users/cluster-operations/update-idea-cluster');
  for (const name of ['upgrade-cluster.md', 'ecs-control-plane.md']) {
    const text = readFileSync(join(docs, name), 'utf8');
    assert.match(text, /ecs\.image_repositories\.aws-us-gov/);
    assert.match(text, /fresh installs default to the public release image/);
    assert.match(text, /must set both `ecs\.image_repositories\.aws-us-gov` and\n`ecs\.image`/);
    assert.match(text, /--skip-release-image-check/);
    assert.doesNotMatch(text, /authoritative/);
  }
});
