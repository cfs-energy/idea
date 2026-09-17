#!/usr/bin/env node
/**
 * Bundles the Node Lambda handlers once, at build time, so synth is a path lookup.
 *
 * Each handler under src/lambda becomes one self-contained <output>/<package>/index.mjs.
 * Nothing is external: the SDK version the bundle carries is the one this package pins.
 *
 * Usage: build-lambda-bundles.mjs [<output dir>]
 */

import { rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { bundleNodeLambda, nodeLambdaPackages } from "../src/cdk/code-asset.ts";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(process.argv[2] ?? join(packageRoot, "dist", "resources", "lambda_assets"));

for (const lambdaPackage of nodeLambdaPackages()) {
  // Only the directories this script produces are removed: the Python assets share the output.
  const target = join(output, lambdaPackage);
  rmSync(target, { recursive: true, force: true });
  bundleNodeLambda(lambdaPackage, target);
  console.log(`lambda bundle: ${target}`);
}
