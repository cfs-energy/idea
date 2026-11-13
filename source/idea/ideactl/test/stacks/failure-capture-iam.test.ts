/**
 * The console of a failed host is read by the module that disposes of it. The grant is limited to
 * this cluster's instances, since the console of anything else in the account is none of its
 * business. A role still without it only finds no cause; the upgrade's stack update brings it.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { it } from "node:test";

const POLICIES = join(import.meta.dirname, "../../resources/policies");

for (const file of ["virtual-desktop-controller.yml", "scheduler-image-pipeline.yml"]) {
  it(`${file} reads the console of this cluster's instances only`, () => {
    const statements = readFileSync(join(POLICIES, file), "utf8").split(/\n  - /);
    const grant = statements.find((statement) => statement.includes("ec2:GetConsoleOutput"));
    assert.ok(grant, "no grant");
    assert.match(grant, /aws:ResourceTag\/idea:ClusterName: '\{\{ context\.cluster_name \}\}'/);
    assert.match(grant, /instance\/\*/);
    assert.doesNotMatch(grant, /Resource: '\*'/);
  });
}
