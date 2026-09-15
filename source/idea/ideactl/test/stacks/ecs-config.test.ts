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
import { ECS_HOST_SETTINGS, ECS_TASK_SETTINGS } from "../support/ecs-settings.ts";

const valuesFile = fileURLToPath(new URL("./ecs-values.yml", import.meta.url));

// `ecs.awsvpc_trunking` is deliberately absent: the account setting is a pre-flight refusal in the
// CLI, never a key the stack can apply. `ecs.image` resolves to the partition's repository at the
// release version tag, because the stack requires the setting and a fresh install has no other
// writer for it; a deploy may replace it with a digest-qualified reference to the same manifest.
// The metrics agent daemon is off until the modules send to it; the image is validated by the
// stack as a digest-pinned private reference.
const expectedSettings = {
  "ecs.datadog.api_key_secret_arn": null,
  "ecs.datadog.enabled": false,
  "ecs.datadog.image": null,
  "ecs.enabled": true,
  ...ECS_HOST_SETTINGS,
  "ecs.image": `public.ecr.aws/s5o2b4m0/idea-control-plane:${ideaVersion()}`,
  "ecs.image_repositories.aws": "public.ecr.aws/s5o2b4m0/idea-control-plane",
  "ecs.image_repositories.aws-us-gov": null,
  ...ECS_TASK_SETTINGS,
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

test("turns the metrics agent daemon on from values when the modules send to it", () => {
  const configDir = mkdtempSync(join(tmpdir(), "ideactl-ecs-config-datadog-"));
  generateConfig(fileURLToPath(new URL("./ecs-values-datadog.yml", import.meta.url)), configDir);
  const settings = flattenConfigDir(configDir);

  assert.equal(settings["ecs.datadog.enabled"], true);
  assert.equal(settings["ecs.datadog.api_key_secret_arn"], "arn:aws:secretsmanager:us-east-2:123456789012:secret:idea-test1-datadog-api-key-AbCdEf");
  assert.equal(settings["ecs.datadog.image"], "123456789012.dkr.ecr.us-east-2.amazonaws.com/datadog/agent@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef");
  assert.equal(settings["metrics.provider"], "dogstatsd");
  assert.equal(settings["metrics.dogstatsd.url"], "unix:///var/run/datadog/dsd.socket");
});
