/**
 * What the config generator must produce for the container module, and must not produce without it.
 *
 * The install and upgrade rehearsals both depend on these four facts. They are asserted here
 * against the shipped templates rather than a fabricated manifest, because a fabricated manifest
 * passes whether or not the generator was changed.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { flattenConfigDir, generateConfigFromTemplates } from "../../src/config/generator.ts";
import type { ModuleEntry } from "../../src/config/generator.ts";
import { loadValuesFile } from "../../src/config/values.ts";
import { ideaVersion } from "../../src/version.ts";

const MODULE_SET_KEY = "global-settings.module_sets.default.ecs.module_id";
const work: string[] = [];

after(() => {
  for (const dir of work) rmSync(dir, { recursive: true, force: true });
});

/** Generate from a values file built on the committed synthetic one, with overrides applied. */
function generate(overrides: Record<string, string> = {}): {
  modules: ModuleEntry[];
  flat: Record<string, unknown>;
} {
  const values = loadValuesFile(join(import.meta.dirname, "./ecs-values.yml"));
  const dir = mkdtempSync(join(tmpdir(), "ideactl-generator-ecs-"));
  work.push(dir);
  const modules = generateConfigFromTemplates({ ...values, ...overrides }, dir);
  return { modules, flat: flattenConfigDir(dir) };
}

describe("the container module in the generated configuration", () => {
  it("adds the module ahead of cluster-manager when the values key is true", () => {
    const { modules } = generate();
    const ids = modules.map((module) => module.id);
    const container = modules.filter((module) => module.id === "ecs");
    assert.deepEqual(container, [
      { name: "ecs", id: "ecs", type: "stack", config_files: ["settings.yml"] },
    ]);
    assert.ok(ids.indexOf("ecs") < ids.indexOf("cluster-manager"), ids.join(","));
    assert.ok(ids.indexOf("shared-storage") < ids.indexOf("ecs"), ids.join(","));
  });

  it("maps the module name to its id under the default module set", () => {
    assert.equal(generate().flat[MODULE_SET_KEY], "ecs");
  });

  it("turns the flag on from the values key", () => {
    assert.equal(generate().flat["ecs.enabled"], true);
  });

  it("resolves a pullable image reference, equal to the partition's repository", () => {
    // A null reference here is what stops a fresh install: the stack requires the setting and
    // refuses. The observability agent image is validated separately and is not interchangeable.
    const flat = generate().flat;
    const repository = flat["ecs.image_repositories.aws"];
    assert.equal(typeof repository, "string");
    assert.equal(flat["ecs.image"], `${String(repository)}:${ideaVersion()}`);
    assert.notEqual(flat["ecs.image"], flat["ecs.datadog.image"]);
  });

  it("leaves the reference null in a partition with no repository", () => {
    const flat = generate({ aws_partition: "aws-us-gov" }).flat;
    assert.equal(flat["ecs.image_repositories.aws-us-gov"], null);
    assert.equal(flat["ecs.image"], null);
  });

  it("generates none of it when the values key is absent", () => {
    const values = loadValuesFile(join(import.meta.dirname, "./ecs-values.yml"));
    delete values.enable_ecs;
    const dir = mkdtempSync(join(tmpdir(), "ideactl-generator-ecs-off-"));
    work.push(dir);
    const modules = generateConfigFromTemplates(values, dir);
    const flat = flattenConfigDir(dir);
    assert.deepEqual(modules.filter((module) => module.id === "ecs"), []);
    assert.equal(flat[MODULE_SET_KEY], undefined);
    assert.deepEqual(Object.keys(flat).filter((key) => key.startsWith("ecs.")), []);
  });

  it("stops the run when the module the splice orders against is gone", () => {
    const values = loadValuesFile(join(import.meta.dirname, "./ecs-values.yml"));
    const templates = mkdtempSync(join(tmpdir(), "ideactl-generator-ecs-anchor-"));
    work.push(templates);
    mkdirSync(join(templates, "global-settings"), { recursive: true });
    writeFileSync(
      join(templates, "idea.yml"),
      "modules:\n  - name: global-settings\n    id: global-settings\n    type: config\n    config_files:\n      - settings.yml\n",
    );
    writeFileSync(join(templates, "global-settings/settings.yml"), "module_sets:\n  default: {}\n");
    assert.throws(
      () =>
        generateConfigFromTemplates(values, join(templates, "out"), { templatesDir: templates }),
      /lists no cluster-manager module/,
    );
  });
});
