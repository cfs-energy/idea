/**
 * The container pre-flight reads an account setting, and it refuses when it has no reader.
 *
 * `deploy` and `quick-setup` reach that pre-flight as soon as a deployment includes the container
 * module, which every new cluster does. The reader was attached only to the upgrade command
 * group's dependencies, so a fresh install stopped on the refusal after bootstrapping and before
 * its first stack. The check is on the live dependency set rather than on a deploy run, because
 * only the live set can be wrong here and asserting it needs no AWS call.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { checkAwsvpcTrunking } from "../../src/cli/commands/upgrade.ts";
import { liveDeps } from "../../src/cli/main.ts";

describe("the container pre-flight's account-settings reader", () => {
  it("is present on the live dependencies deploy and quick-setup use", () => {
    assert.notEqual(liveDeps().ecsAccountSettings, undefined);
  });

  it("is what the pre-flight refuses without", async () => {
    const errors: string[] = [];
    await assert.rejects(
      checkAwsvpcTrunking(
        { accountId: async () => "123456789012", err: (line) => errors.push(line) },
        { awsRegion: "us-east-2" },
      ),
      /account-settings reader is required/,
    );
    assert.deepEqual(errors, []);
  });
});
