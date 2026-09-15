/**
 * A values file with no control-plane shape key is refused before anything is created.
 *
 * The installer writes the key, so a file without one was written by hand, carried over from
 * before the container control plane, or copied between clusters. Generating from it silently
 * produces the host shape, which no longer builds, and the failure lands partway through a deploy
 * with nothing pointing at the missing key.
 *
 * The refusal lives on the command rather than in the generator on purpose, and this file pins
 * both halves: the generator still produces what the reference implementation produced for a
 * values file with no key, which is what the captured-cluster comparison depends on, and the
 * command stops a new install that would use it.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
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

/** Inert effects: `configGenerate` reaches no AWS call before the refusal. */
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
  it("stops a new install and names the key", async () => {
    const effects = deps();
    await assert.rejects(
      configGenerate(effects, {
        valuesFile: valuesFileWithoutKey(),
        configDir: temp("ideactl-no-key-out-"),
        force: true,
      }),
    );
    const printed = effects.errors.join("\n");
    assert.match(printed, /enable_ecs/);
    assert.match(printed, /--regenerate/);
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
    // The captured-cluster comparison generates straight from a values file with no key and
    // expects exactly what the implementation being replaced produced. A refusal here would take
    // that gate red, which is why it is on the command instead.
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
