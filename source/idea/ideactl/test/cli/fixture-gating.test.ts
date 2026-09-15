import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
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

// A file that requires a private capture ends before registering tests in a declared public
// checkout, and fails loudly anywhere else; both are observed through a child process, since
// the public-checkout path ends the process.
test("a required capture ends the file quietly in a public checkout and loudly elsewhere", () => {
  const root = mkdtempSync(join(tmpdir(), "ideactl-capture-"));
  const fixtures = join(dirname(fileURLToPath(import.meta.url)), "..", "support", "fixtures.ts");
  const script = join(root, "probe.test.ts");
  writeFileSync(
    script,
    [
      `import { requireCapture } from ${JSON.stringify(fixtures)};`,
      `requireCapture([${JSON.stringify(join(root, "absent.json"))}], "node tools/parity/capture.ts --live");`,
      'console.log("REACHED THE TESTS");',
      "",
    ].join("\n"),
  );
  try {
    const publicCheckout = spawnSync(process.execPath, [script], { encoding: "utf8", env: { ...process.env, [PUBLIC_CHECKOUT_ENV]: "1" } });
    assert.equal(publicCheckout.status, 0, publicCheckout.stderr);
    assert.match(publicCheckout.stdout, /^PRIVATE CAPTURE ABSENT, tests not run: .*absent\.json \(regenerate with: node tools\/parity\/capture\.ts --live\)$/m);
    assert.doesNotMatch(publicCheckout.stdout, /REACHED THE TESTS/);
    const env = { ...process.env };
    delete env[PUBLIC_CHECKOUT_ENV];
    const privateCheckout = spawnSync(process.execPath, [script], { encoding: "utf8", env });
    assert.notEqual(privateCheckout.status, 0);
    assert.match(privateCheckout.stderr, /Required fixture is missing/);
  } finally {
    rmSync(root, { force: true, recursive: true });
  }
});

