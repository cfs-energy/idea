// Copies the runtime inputs into dist/ so the built package is self-contained.
import { cpSync, mkdirSync, copyFileSync, rmSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(packageRoot, "..", "..", "..");
const packageResources = join(packageRoot, "resources");
const outputResources = resolve(
  process.env.IDEACTL_RESOURCE_OUTPUT_DIR ?? join(packageRoot, "dist", "resources"),
);
const bootstrapSource = resolve(
  process.env.IDEACTL_BOOTSTRAP_SOURCE_DIR ??
    join(repositoryRoot, "source", "idea", "idea-bootstrap"),
);
const runtimeResourceDirectories = [
  "cdk",
  "config",
  "input_params",
  "integration_tests",
  "policies",
];

rmSync(outputResources, { recursive: true, force: true });
mkdirSync(outputResources, { recursive: true });
for (const directory of runtimeResourceDirectories) {
  cpSync(
    join(packageResources, directory),
    join(outputResources, directory),
    { recursive: true },
  );
}
cpSync(bootstrapSource, join(outputResources, "bootstrap"), { recursive: true });
copyFileSync(
  join(repositoryRoot, "IDEA_VERSION.txt"),
  join(dirname(outputResources), "IDEA_VERSION.txt"),
);
console.log("resources copied to dist/resources");
