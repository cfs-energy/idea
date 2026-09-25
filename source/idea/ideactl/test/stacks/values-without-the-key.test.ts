/** New configurations default to containers; regeneration preserves the recorded shape. */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { configGenerate } from "../../src/cli/commands/config.ts";
import type { Deps } from "../../src/cli/cdk-invoker.ts";
import { flattenConfigDir, generateConfigFromTemplates } from "../../src/config/generator.ts";
import { loadValuesFile } from "../../src/config/values.ts";

const work: string[] = [];

after(() => {
  for (const dir of work) rmSync(dir, { recursive: true, force: true });
});

function temp(prefix: string): string {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  work.push(dir);
  return dir;
}

/** A values file that says nothing about the control-plane shape. */
function valuesFileWithoutKey(): string {
  const values = loadValuesFile(join(import.meta.dirname, "./ecs-values.yml"));
  delete values.enable_ecs;
  const dir = temp("ideactl-no-key-");
  const file = join(dir, "values.yml");
  writeFileSync(
    file,
    Object.entries(values)
      .map(([key, value]) => `${key}: ${typeof value === "string" ? JSON.stringify(value) : String(value)}`)
      .join("\n"),
  );
  return file;
}

/** Inert configuration generation effects. */
function deps(): Deps & { errors: string[] } {
  const errors: string[] = [];
  return {
    errors,
    spawn: async () => 0,
    cfn: {
      describeChangeSet: async () => ({}),
      executeChangeSet: async () => {},
      describeStack: async () => ({}),
    },
    s3: { putObject: async () => {}, getObject: async () => "" },
    scan: async () => ({}),
    configWriter: async () => ({
      syncModulesInDb: async () => {},
      syncClusterSettingsInDb: async () => {},
      setConfigEntry: async () => {},
      deleteConfigEntries: async () => {},
    }),
    accountId: async () => "123456789012",
    httpStatus: async () => 0,
    sleep: async () => {},
    now: () => 0,
    uuid: () => "00000000-0000-0000-0000-000000000000",
    out: () => {},
    err: (line: string) => errors.push(line),
    prompt: async () => true,
  } as Deps & { errors: string[] };
}

describe("a values file that does not say which control plane to build", () => {
  it("defaults a new install to containers", async () => {
    const effects = deps();
    const configDir = temp("ideactl-no-key-out-");
    const values = await configGenerate(effects, {
      valuesFile: valuesFileWithoutKey(), configDir, force: true,
    });
    assert.equal(values["enable_ecs"], true);
    assert.equal(loadValuesFile(join(configDir, "values.yml"))["enable_ecs"], true);
    assert.equal(flattenConfigDir(join(configDir, "config"))["ecs.enabled"], true);
    assert.deepEqual(effects.errors, []);
  });

  it("preserves an explicit host value from an existing values file", async () => {
    const file = valuesFileWithoutKey();
    writeFileSync(file, `${readFileSync(file, "utf8")}\nenable_ecs: false\n`);
    const configDir = temp("ideactl-host-values-");
    const values = await configGenerate(deps(), { valuesFile: file, configDir, force: true });
    assert.equal(values["enable_ecs"], false);
    assert.equal(flattenConfigDir(join(configDir, "config"))["ecs.enabled"], undefined);
  });

  it("lets an existing cluster regenerate what it already has", async () => {
    const effects = deps();
    const configDir = temp("ideactl-no-key-regen-");
    const values = await configGenerate(effects, {
      valuesFile: valuesFileWithoutKey(),
      configDir,
      force: true,
      regenerate: true,
    });
    assert.equal(values["enable_ecs"], undefined);
    assert.deepEqual(effects.errors, []);
  });

  it("leaves the generator itself producing no container module for such a file", () => {
    // Historical replay uses the generator directly.
    const values = loadValuesFile(join(import.meta.dirname, "./ecs-values.yml"));
    delete values.enable_ecs;
    const dir = temp("ideactl-no-key-generator-");
    const modules = generateConfigFromTemplates(values, dir);
    assert.deepEqual(modules.filter((module) => module.id === "ecs"), []);
    assert.deepEqual(
      Object.keys(flattenConfigDir(dir)).filter((key) => key.startsWith("ecs.")),
      [],
    );
  });
});
