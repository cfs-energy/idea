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

  assert.match(dockerfile, /rm -rf \/root\/\.cache\/pip;/);
  assert.match(dockerfile, /find node_modules -type f -name '\*\.d\.ts' -delete;/);
});
