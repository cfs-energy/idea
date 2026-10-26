import assert from "node:assert/strict";
import test from "node:test";

import { buildProgram } from "../../src/cli/main.ts";
import { fakeDeps } from "../support/deploy-harness.ts";

const CLUSTER = "sample-cluster";
const REGION = "us-east-2";

function commandNames(): string[] {
  return buildProgram(fakeDeps()).commands.map((command) => command.name());
}

for (const name of [
  "upgrade-cluster",
  "delete-cluster",
  "delete-backups",
  "sso",
  "directoryservice",
  "shared-storage",
  "utils",
  "support",
  "run-integration-tests",
  "backup-update-global-settings",
]) {
  test(`${name} is registered on the command program`, () => {
    assert.ok(commandNames().includes(name));
  });
}
