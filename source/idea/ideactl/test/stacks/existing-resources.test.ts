import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as resources from '../../src/cdk/constructs/existing-resources.ts';

test('T-22: retired control-plane host key-pair lookup is not exported', () => {
  assert.equal('lookupKeyPair' in resources, false);
});
