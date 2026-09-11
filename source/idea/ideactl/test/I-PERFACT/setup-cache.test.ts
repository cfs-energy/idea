/**
 * Tests the immutable setup cache used by synthesis-heavy test files.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { cacheSetup } from "../support/setup-cache.ts";

test("cached setup runs once and returns an independent clone to each caller", async () => {
  let setupCalls = 0;
  const setup = cacheSetup(async () => {
    setupCalls += 1;
    return { nested: { value: 1 } };
  });

  const first = await setup();
  first.nested.value = 2;
  const second = await setup();

  assert.equal(setupCalls, 1);
  assert.notEqual(first, second);
  assert.notEqual(first.nested, second.nested);
  assert.deepEqual(second, { nested: { value: 1 } });
});
