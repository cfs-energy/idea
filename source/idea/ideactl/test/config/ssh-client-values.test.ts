import assert from 'node:assert/strict';
import { join } from 'node:path';
import { test } from 'node:test';
import { buildContext, InvalidParams, loadValuesFile } from '../../src/config/values.ts';
import { compareUpgradeDrift, renderUpgradeDrift } from '../../src/config/upgrade-drift.ts';

const values = loadValuesFile(join(import.meta.dirname, '../stacks/ecs-values.yml'));
for (const key of ['client_ip', 'ssh_client_ip']) {
  for (const value of ['203.0.113.5/32', null, ['2001:db8::1'], ['10.1.2.3/24'], ['bad'], [123], ['1.2.3.4/33'], ['01.2.3.4'], ['1.2.3.4/'], ['256.2.3.4']]) {
    test(`P-40 P-79: rejects ${key}=${JSON.stringify(value)}`, () => {
      assert.throws(() => buildContext({ ...values, [key]: value }), (error) => error instanceof InvalidParams && error.message.includes(key));
    });
  }
  test(`P-40 P-79: normalizes and deduplicates ${key}`, () => {
    assert.deepEqual(buildContext({ ...values, [key]: ['203.0.113.5', '203.0.113.5/32', '0.0.0.0/0', '10.1.2.0/24'] })[key], ['203.0.113.5/32', '0.0.0.0/0', '10.1.2.0/24']);
    assert.deepEqual(buildContext({ ...values, [key]: [] })[key], []);
  });
}
const key = 'cluster.network.ssh_client_ip';
test('P-62: equal values-owned rows produce no change', () => {
  const row = { key, value: ['203.0.113.5/32'] };
  const report = compareUpgradeDrift({ current: [{ ...row, source: 'cli' }], generated: [row], syncFullConfiguration: true });
  assert.deepEqual(report.findings, []);
});
test('P-62 P-79: preview names an overwritten operator SSH restriction and displays old and new lists', () => {
  const report = compareUpgradeDrift({ current: [{ key, value: ['203.0.113.5/32'], source: 'cli' }], generated: [{ key, value: [] }], syncFullConfiguration: true });
  assert.deepEqual(report.changedRowsDifferingFromGenerated, [key]);
  assert.match(renderUpgradeDrift(report), /\["203\.0\.113\.5\/32"\] -> \[\]/);
  assert.match(renderUpgradeDrift(report), /Potential operator edits overwritten.*cluster.network.ssh_client_ip/);
});
