/** The shipped load balancer log account ids, read as the bootstrap stack reads them. */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { elbAccountIdForRegion, regionElbAccountIdPath } from "../../src/cdk/stacks/bootstrap.ts";

test("every shipped region maps to a twelve-digit id kept as written, me-south-1 with its leading zero", () => {
  // the one shipped id made only of octal digits: the old reader turned it into ten digits
  assert.match(elbAccountIdForRegion("me-south-1") ?? "", /^0[0-7]{11}$/);
  for (const line of readFileSync(regionElbAccountIdPath(), "utf8").split("\n")) {
    const match = /^\s*([a-z0-9-]+)\s*:\s*(\S+)/.exec(line);
    if (match === null || line.trim().startsWith("#")) continue;
    assert.equal(elbAccountIdForRegion(match[1]!), match[2]!.replace(/^['"]|['"]$/g, ""), match[1]);
  }
});

test("a value that is not a twelve-digit account id is refused", () => {
  const directory = mkdtempSync(join(tmpdir(), "ideactl-elb-ids-"));
  try {
    writeFileSync(join(directory, "ids.yml"), "short-region: 0123\n");
    assert.throws(() => elbAccountIdForRegion("short-region", join(directory, "ids.yml")), /not a twelve-digit account id/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
