import assert from 'node:assert/strict';
import test from 'node:test';
import { makeUniqueId } from '../../src/util/unique-id.ts';

test('makeUniqueId refuses unresolved tokens like the reference', () => {
  assert.throws(() => makeUniqueId(['Parent', '${Token[TOKEN.123]}']), /unresolved tokens/);
});
