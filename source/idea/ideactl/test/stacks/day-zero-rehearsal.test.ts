/**
 * The fresh container-cluster rehearsal is a single offline command.
 *
 * The fixture carries the same values-file request as an operator and contains
 * no captured account state. The IAM service-linked-role check stays blocked,
 * which proves the rehearsal reports an account-only read instead of inventing
 * its result.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { validateDeploymentDependencies } from "../../tools/day-zero/rehearse.ts";

const PKG = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const REHEARSAL = join(PKG, "tools/day-zero/rehearse.ts");
const VALUES = join(PKG, "test/stacks/day-zero-new-cluster.values.yml");

test("the new container-cluster rehearsal checks the generated deployment order offline", () => {
  const result = spawnSync(process.execPath, [REHEARSAL, "--values-file", VALUES], {
    cwd: PKG,
    encoding: "utf8",
  });

  const diagnostic = result.stdout.length > 0 ? result.stdout : result.stderr;
  assert.equal(result.status, 0, diagnostic);
  assert.match(result.stdout, /^INSTALL_INTENT containers=true$/m);
  assert.match(result.stdout, /^GENERATED_STATE valid$/m);
  assert.match(
    result.stdout,
    /^ORDER cluster -> analytics -> identity-provider -> directoryservice -> metrics -> shared-storage -> ecs -> cluster-manager -> scheduler -> vdc -> bastion-host$/m,
  );
  assert.equal((result.stdout.match(/^STACK /gm) ?? []).length, 11);
  assert.match(result.stdout, /^STACK cluster SYNTHESIZED /m);
  assert.match(result.stdout, /^STACK ecs SYNTHESIZED /m);
  assert.match(result.stdout, /^CREATE_PATH cluster new VPC emitted when use_existing_vpc=false$/m);
  assert.match(result.stdout, /^DEPENDENCY_CHECK all observed stack setting sources precede their consumers$/m);
  assert.match(result.stdout, /^STACK analytics BLOCKED /m);
  assert.match(result.stdout, /^OFFLINE_BLOCK analytics: live read required: SynthReadMiss: iam:ListRoles:/m);
  assert.match(result.stdout, /^RESULT no ordering or missing-value problems found$/m);
});

test("the ordering check rejects a setting source deployed after its consumer", () => {
  const problems = validateDeploymentDependencies({
    deploymentOrder: ["consumer", "provider"],
    stacks: [
      {
        moduleId: "consumer",
        moduleName: "consumer",
        status: "SYNTHESIZED",
        reads: ["provider.setting"],
        priorSettingSources: ["provider"],
        publishedSettings: [],
        freshResourceChecks: [],
      },
      {
        moduleId: "provider",
        moduleName: "provider",
        status: "SYNTHESIZED",
        reads: [],
        priorSettingSources: [],
        publishedSettings: ["provider.setting"],
        freshResourceChecks: [],
      },
    ],
  });

  assert.deepEqual(problems, ["consumer: setting source provider is not deployed before its consumer"]);
});
