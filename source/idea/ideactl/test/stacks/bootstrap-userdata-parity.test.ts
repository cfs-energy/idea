// expected.json contains reference output for the synthetic parameter sets in cases.json.
// A JSON string map preserves trailing indentation without relying on a text fixture.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
  buildBootstrapUserData,
  type BootstrapUserDataParams,
} from '../../src/cdk/userdata.ts';

const here = dirname(fileURLToPath(import.meta.url));
const cases: Record<string, BootstrapUserDataParams> = JSON.parse(
  readFileSync(join(here, 'bootstrap-userdata-cases.json'), 'utf8'),
);

const expected: Record<string, string> = JSON.parse(
  readFileSync(join(here, 'bootstrap-userdata-expected.json'), 'utf8'),
);

for (const [name, params] of Object.entries(cases)) {
  test(`matches reference output: ${name}`, () => {
    assert.equal(buildBootstrapUserData(params), expected[name]);
  });
}

test('windows rejects infra config', () => {
  assert.throws(
    () =>
      buildBootstrapUserData({
        ...cases.windows,
        infraConfig: { ALPHA: 'a' },
      }),
    /infra config is not supported for windows/,
  );
});

test('windows user data stops on a failed download or extraction before any install command runs', () => {
  const userdata = buildBootstrapUserData(cases.windows!);
  const firstInstall = userdata.indexOf(cases.windows!.installCommands[0]!);
  for (const guard of ["$ErrorActionPreference = 'Stop'", 'if ($LASTEXITCODE -ne 0) { throw', 'exit 1', 'Clear-ForeignBootstrap\n']) {
    const at = userdata.indexOf(guard);
    assert.ok(at >= 0 && at < firstInstall, `${guard} precedes the install commands`);
  }
});
