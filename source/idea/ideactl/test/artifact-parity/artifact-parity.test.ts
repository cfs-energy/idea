/**
 * Artifact resource trees must agree. Each assembler is run, then the files it
 * wrote are compared. A directory added outside the main resource tree and
 * forgotten by one assembler makes this red.
 */

import assert from "node:assert/strict";
import { existsSync, rmSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { assembleCopyResources, assembleDockerfileResources, assembleSabotagedCopyResources, assembleShellResources, checkoutUnion, CONTAINER_VALUES, diffTrees, generateConfig, isDirectory, listFiles, missingFromCheckout, renderSamplePolicy, scratch, stageTool, treesAgree } from "./trees.ts";

const CONTAINER_TEMPLATE = "config/templates/ecs/settings.yml";

const trees = {
  copy: "",
  shell: "",
  image: "",
  sabotage: "",
};

/**
 * Builds every resource tree the standing check compares.
 */
before(() => {
  assert.equal(existsSync(CONTAINER_VALUES), true, "container values fixture is missing");
  trees.copy = join(scratch("copy"), "resources");
  trees.shell = join(scratch("shell"), "resources");
  trees.image = join(scratch("image"), "resources");
  trees.sabotage = join(scratch("sabotage"), "resources");
  assembleCopyResources(trees.copy);
  assembleShellResources(trees.shell);
  assembleDockerfileResources(trees.image);
  assembleSabotagedCopyResources(trees.sabotage);
});

/**
 * Removes the assembled trees after the file finishes.
 */
after(() => {
  for (const root of Object.values(trees)) {
    if (root !== "") rmSync(root, { recursive: true, force: true });
  }
});

test("current assemblers write the same resource files, including the container overlay", () => {
  assert.equal(listFiles(trees.copy).includes(CONTAINER_TEMPLATE), true);
  assert.equal(treesAgree(trees.copy, trees.shell), true, JSON.stringify(diffTrees(trees.copy, trees.shell)));
  assert.equal(treesAgree(trees.copy, trees.image), true, JSON.stringify(diffTrees(trees.copy, trees.image)));
  assert.deepEqual(missingFromCheckout(trees.copy), []);
  assert.deepEqual(missingFromCheckout(trees.shell), []);
  assert.deepEqual(missingFromCheckout(trees.image), []);
  assert.equal(checkoutUnion().includes(CONTAINER_TEMPLATE), true);
});

test("generated configuration from each assembled tree includes the container module", async () => {
  for (const [name, resources] of Object.entries(trees)) {
    if (name === "sabotage") continue;
    const stage = stageTool(resources);
    const configDir = join(stage, "generated");
    try {
      const generated = await generateConfig(stage, configDir);
      assert.equal(generated.status, 0, `${name} generate failed: ${generated.stdout}\n${generated.stderr}`);
      assert.match(generated.stdout, /generating config from templates/);
      assert.equal(isDirectory(join(configDir, "config", "ecs")), true, `${name} did not emit config/ecs`);
      const policy = await renderSamplePolicy(stage);
      assert.equal((policy as { Version?: string }).Version, "2012-10-17");
    } finally {
      rmSync(stage, { recursive: true, force: true });
    }
  }
});

test("removing the overlay from one assembler makes the comparison and generate fail", async () => {
  const diff = diffTrees(trees.copy, trees.sabotage);
  assert.deepEqual(diff.onlyRight, []);
  assert.deepEqual(diff.mismatched, []);
  assert.equal(diff.onlyLeft.includes(CONTAINER_TEMPLATE), true);
  assert.equal(missingFromCheckout(trees.sabotage).includes(CONTAINER_TEMPLATE), true);
  assert.equal(existsSync(join(trees.sabotage, CONTAINER_TEMPLATE)), false);

  const stage = stageTool(trees.sabotage);
  try {
    const generated = await generateConfig(stage, join(stage, "generated"));
    assert.equal(generated.status, 1);
    assert.match(`${generated.stdout}\n${generated.stderr}`, /template not found: ecs\/settings\.yml/);
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
});
