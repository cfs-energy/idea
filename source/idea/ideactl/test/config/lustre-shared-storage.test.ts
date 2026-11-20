/**
 * FSx for Lustre is refused for the apps and data storage: at config generation, and for a stored
 * configuration before a deploy changes anything. Job scratch is not affected.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { refuseLustreAppsOrData } from "../../src/cli/deployment-helper.ts";
import { generateConfigFromTemplates } from "../../src/config/generator.ts";
import { loadValuesFile } from "../../src/config/values.ts";

const VALUES = join(import.meta.dirname, "../stacks/day-zero-new-cluster.values.yml");

for (const name of ["apps", "data"] as const) {
  test(`config generation refuses FSx for Lustre for the ${name} storage and names the supported providers`, () => {
    const directory = mkdtempSync(join(tmpdir(), "ideactl-lustre-"));
    try {
      const values = { ...loadValuesFile(VALUES), [`storage_${name}_provider`]: "fsx_lustre" };
      assert.throws(() => generateConfigFromTemplates(values, directory),
        new RegExp(`not supported for the ${name} storage: use efs\\..*job scratch`));
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  test(`a stored configuration with Lustre ${name} storage is refused before a deploy`, () => {
    assert.throws(() => refuseLustreAppsOrData((key) => key === `shared-storage.${name}.provider` ? "fsx_lustre" : undefined),
      new RegExp(`shared-storage.${name}.provider is fsx_lustre with no fsx_lustre.dns and fsx_lustre.mount_name.*no cluster can run in this state`));
    // an entry completed with its mount name mounts on the hosts, and is left alone
    refuseLustreAppsOrData((key) => key === `shared-storage.${name}.provider` ? "fsx_lustre"
      : key.endsWith(".fsx_lustre.dns") ? "fs-1.fsx.us-east-2.amazonaws.com" : key.endsWith(".fsx_lustre.mount_name") ? "abcdefgh" : undefined);
  });
}

test("EFS apps and data storage generate as before", () => {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-lustre-"));
  try {
    assert.ok(generateConfigFromTemplates(loadValuesFile(VALUES), directory).length > 0);
    refuseLustreAppsOrData(() => "efs");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
