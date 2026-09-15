/**
 * The Node handlers have to be loadable by the Lambda runtime, which the template cannot show.
 *
 * A bundle that fails to resolve an import, or whose CommonJS shim is missing, deploys and then
 * fails at invoke with the custom resource never answering: the stack sits in CREATE_IN_PROGRESS
 * until it times out. So each bundle is imported here, exactly as `index.handler` names it.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { IdeaCodeAsset, distResourcesDir, nodeLambdaPackages } from "../../src/cdk/code-asset.ts";

const PKG = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const BUNDLE_SCRIPT = join(PKG, "scripts", "build-lambda-bundles.mjs");

const temporaries: string[] = [];
const temporary = (prefix: string): string => {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  temporaries.push(dir);
  return dir;
};

after(() => {
  for (const dir of temporaries) rmSync(dir, { recursive: true, force: true });
});

/** Bundles every handler once, into a directory this file owns. */
function buildBundles(): string {
  const output = temporary("ideactl-lambda-bundles-");
  const result = spawnSync(process.execPath, [BUNDLE_SCRIPT, output], { encoding: "utf8" });
  assert.equal(
    result.status,
    0,
    [`bundle build failed`, result.stdout, result.stderr, result.error?.message ?? ""].join("\n"),
  );
  return output;
}

const bundles = buildBundles();

test("every bundled handler loads and exports a handler function", async () => {
  const packages = nodeLambdaPackages();
  assert.ok(packages.length > 0, "no handler packages found under src/lambda");

  for (const packageName of packages) {
    const bundle = join(bundles, packageName, "index.mjs");
    assert.equal(existsSync(bundle), true, `${packageName}: the build wrote no ${bundle}`);
    const module: Record<string, unknown> = await import(pathToFileURL(bundle).href);
    assert.equal(
      typeof module.handler,
      "function",
      `${packageName}: index.handler is ${typeof module.handler}, so the runtime cannot invoke it`,
    );
  }
});

test("a bundle imports nothing the runtime has to resolve for it", () => {
  const external: string[] = [];
  for (const packageName of nodeLambdaPackages()) {
    const text = readFileSync(join(bundles, packageName, "index.mjs"), "utf8");
    for (const match of text.matchAll(/^import .*? from "([^"]+)";$/gm)) {
      if (match[1]?.startsWith("node:") === false) external.push(`${packageName}: ${match[1]}`);
    }
  }
  assert.deepEqual(external, [], `bundles with a static import the deployed runtime must supply: ${external.join(", ")}`);
});

test("a package with no prebuilt asset is bundled on demand", () => {
  const home = temporary("ideactl-lambda-ondemand-");
  const previous = process.env.IDEA_USER_HOME;
  process.env.IDEA_USER_HOME = home;
  try {
    const packageName = nodeLambdaPackages()[0] as string;
    assert.equal(
      existsSync(join(distResourcesDir(), "lambda_assets", packageName)),
      false,
      "this check needs a source tree with no prebuilt assets beside the resources",
    );

    const asset = new IdeaCodeAsset(packageName);
    assert.equal(asset.lambdaHandler, "index.handler");
    const root = asset.assetPath();
    assert.ok(root.startsWith(home), `${packageName}: built at ${root}, expected it under ${home}`);
    assert.equal(existsSync(join(root, "index.mjs")), true, `${packageName}: ${root} has no index.mjs`);

    // The second call is the checksum cache, not a second bundle.
    assert.equal(new IdeaCodeAsset(packageName).assetPath(), root);
  } finally {
    if (previous === undefined) delete process.env.IDEA_USER_HOME;
    else process.env.IDEA_USER_HOME = previous;
  }
});
