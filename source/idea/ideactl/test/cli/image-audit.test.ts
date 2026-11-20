/**
 * Keeps build-only cache and TypeScript declarations out of the runtime image.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const dockerfileUrl = new URL(
  "../../../../../deployment/ecr/idea-control-plane/Dockerfile",
  import.meta.url,
);

test("removes build-only package payloads in their creating layers", async () => {
  const dockerfile = await readFile(dockerfileUrl, "utf8");

  assert.match(dockerfile, /rm -rf [^;\n]*\/root\/\.cache\/pip;/);
  assert.match(dockerfile, /find node_modules -type f -name '\*\.d\.ts' -delete;/);
});

test("the runtime image carries no npm once the ideactl build is done with it", async () => {
  const dockerfile = await readFile(dockerfileUrl, "utf8");
  const removal = dockerfile.search(
    /rm -rf \/usr\/local\/node\/lib\/node_modules\/npm \/usr\/local\/node\/lib\/node_modules\/corepack \\\n\s+\/usr\/local\/node\/bin\/npm \/usr\/local\/node\/bin\/npx \/usr\/local\/node\/bin\/corepack /,
  );
  assert.ok(removal > 0, "no removal of the bundled npm, npx and corepack");
  const lastNpm = Math.max(...[...dockerfile.matchAll(/\bnpm (ci|run|prune|cache)\b/g)].map((m) => m.index));
  assert.ok(lastNpm < removal, "npm runs after it is removed");
  const layer = dockerfile.slice(dockerfile.lastIndexOf("\nRUN ", lastNpm), dockerfile.indexOf("\n\n", lastNpm));
  assert.ok(layer.includes("/usr/local/node/bin/corepack"), "npm is removed in a different layer from its last use");
});
