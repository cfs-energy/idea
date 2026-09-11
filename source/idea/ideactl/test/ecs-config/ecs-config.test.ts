/**
 * ECS template surface rendered through the production configuration generator.
 */

import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  flattenConfigDir,
  generateConfig,
} from "../../src/config/generator.ts";
import { ideaVersion } from "../../src/version.ts";

const valuesFile = fileURLToPath(new URL("./values.yml", import.meta.url));

// `ecs.awsvpc_trunking` is deliberately absent: the account setting is a pre-flight refusal in the
// CLI, never a key the stack can apply. `ecs.image` resolves to the partition's repository at the
// release version tag, because the stack requires the setting and a fresh install has no other
// writer for it; a deploy may replace it with a digest-qualified reference to the same manifest.
// The metrics agent image is validated separately as a digest-pinned private reference.
const expectedSettings = {
  "ecs.datadog.api_key_secret_arn": null,
  "ecs.datadog.enabled": false,
  "ecs.datadog.image": "public.ecr.aws/datadog/agent:7.83.1",
  "ecs.enabled": true,
  "ecs.hosts.instance_type": "m7g.large",
  "ecs.hosts.max": 4,
  "ecs.hosts.min": 3,
  "ecs.hosts.volume_size": 60,
  "ecs.image": `public.ecr.aws/s5o2b4m0/idea-control-plane:${ideaVersion()}`,
  "ecs.image_repositories.aws": "public.ecr.aws/s5o2b4m0/idea-control-plane",
  "ecs.image_repositories.aws-us-gov": null,
  "ecs.tasks.cluster-manager.cpu": 256,
  "ecs.tasks.cluster-manager.desired": 2,
  "ecs.tasks.cluster-manager.memory": 1024,
  "ecs.tasks.dcv-broker.cpu": 512,
  "ecs.tasks.dcv-broker.desired": 2,
  "ecs.tasks.dcv-broker.memory": 4096,
  "ecs.tasks.dcv-gateway.cpu": 256,
  "ecs.tasks.dcv-gateway.desired": 2,
  "ecs.tasks.dcv-gateway.memory": 512,
  "ecs.tasks.scheduler.cpu": 512,
  "ecs.tasks.scheduler.desired": 1,
  "ecs.tasks.scheduler.memory": 2048,
  "ecs.tasks.vdc.cpu": 256,
  "ecs.tasks.vdc.desired": 2,
  "ecs.tasks.vdc.memory": 1024,
};

test("renders the complete ECS configuration key set", () => {
  const configDir = mkdtempSync(join(tmpdir(), "ideactl-ecs-config-output-"));
  const modules = generateConfig(valuesFile, configDir);
  const settings = flattenConfigDir(configDir);
  const ecsSettings = Object.fromEntries(
    Object.entries(settings).filter(([key]) => key.startsWith("ecs.")),
  );
  const ids = modules.map((module) => module.id);

  assert.deepStrictEqual(
    modules.filter((module) => module.id === "ecs"),
    [
      {
        name: "ecs",
        id: "ecs",
        type: "stack",
        config_files: ["settings.yml"],
      },
    ],
  );
  assert.ok(ids.includes("cluster-manager"), "production idea.yml must still list cluster-manager");
  assert.ok(ids.indexOf("ecs") < ids.indexOf("cluster-manager"), ids.join(","));
  assert.deepStrictEqual(ecsSettings, expectedSettings);
});
