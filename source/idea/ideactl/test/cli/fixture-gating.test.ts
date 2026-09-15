import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  PUBLIC_CHECKOUT_ENV,
  optionalFixtures,
  requireFixtures,
  requiredService,
} from "../support/fixtures.ts";

test("required fixtures name each missing path and regeneration command", () => {
  const root = mkdtempSync(join(tmpdir(), "ideactl-fixtures-"));
  const missing = join(root, "missing.json");
  const alsoMissing = join(root, "also-missing.json");
  const regenerate = "node tools/parity/capture.ts --from-raw INPUT --out OUTPUT";
  try {
    assert.throws(
      () => requireFixtures([missing, alsoMissing], regenerate),
      {
        message: [
          "Required fixture is missing:",
          `  ${missing}`,
          `  ${alsoMissing}`,
          `Regenerate it with: ${regenerate}`,
        ].join("\n"),
      },
    );
  } finally {
    rmSync(root, { force: true, recursive: true });
  }
});

test("optional fixtures skip only in explicit public checkout mode", () => {
  const root = mkdtempSync(join(tmpdir(), "ideactl-fixtures-"));
  const missing = join(root, "missing.json");
  const present = join(root, "present.json");
  writeFileSync(present, "{}\n");
  const previous = process.env[PUBLIC_CHECKOUT_ENV];
  try {
    delete process.env[PUBLIC_CHECKOUT_ENV];
    assert.throws(() => optionalFixtures([missing], "regenerate-fixture"));
    process.env[PUBLIC_CHECKOUT_ENV] = "1";
    assert.equal(optionalFixtures([missing], "regenerate-fixture"), false);
    // Public checkout still runs optional coverage when the fixture is on disk.
    assert.equal(optionalFixtures([present], "regenerate-fixture"), true);
  } finally {
    if (previous === undefined) delete process.env[PUBLIC_CHECKOUT_ENV];
    else process.env[PUBLIC_CHECKOUT_ENV] = previous;
    rmSync(root, { force: true, recursive: true });
  }
});

test("required services name their setup command", () => {
  const setup = "java -jar ~/.idea/lib/dynamodb-local/DynamoDBLocal.jar";
  assert.throws(
    () => requiredService("local database emulator", setup),
    {
      message: `Required service is unavailable: local database emulator\nStart it with: ${setup}`,
    },
  );
});
