#!/usr/bin/env node
/**
 * Builds the direct-runtime release artifact.
 *
 * The artifact contains one bundled application file, the pinned deployment
 * CLI, and the resource files read at run time. It does not contain a package
 * installation tree and does not require a compiler or package manager.
 */

import { build, transformSync } from "esbuild";
import { spawnSync } from "node:child_process";
import { builtinModules } from "node:module";
import {
  chmodSync,
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PACKAGE_JSON = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));
const DEFAULT_OUTPUT = join(PACKAGE_ROOT, "dist", "ideactl-shell");
const DEFAULT_ARCHIVE = join(PACKAGE_ROOT, "dist", `ideactl-shell-${PACKAGE_JSON.version}.tar.gz`);
const NODE_MODULES = join(PACKAGE_ROOT, "node_modules");
/**
 * Runs before the bundled code. CommonJS dependencies get `require`; `__ideactlPackagePath` maps a
 * dependency file's path under node_modules to its shipped copy. The bundle is
 * dist/src/cli/main.js and shipped packages are under dist/node_modules.
 */
export const BUNDLE_BANNER = [
  'import { createRequire as __ideactlCreateRequire } from "node:module";',
  'import { join as __ideactlJoinPath } from "node:path";',
  "const require = __ideactlCreateRequire(import.meta.url);",
  "const __filename = import.meta.filename;",
  "const __dirname = import.meta.dirname;",
  'const __ideactlPackagePath = (path) => __ideactlJoinPath(import.meta.dirname, "..", "..", "node_modules", path);',
].join(" ");
/** Package files a run never reads: type declarations, source maps, documentation, jsii manifests. */
const UNSHIPPED_PACKAGE_FILE = /(?:\.d\.[cm]?ts|\.map|\.md|^\.jsii(?:\.gz)?)$/;
const MODULE_PATH_IDENTIFIER = /\b__(?:dirname|filename)\b/;
const MODULE_PATH_DECLARATION = /\b(?:const|let|class|function)\s+__(?:dirname|filename)\b/;
const MODULE_META_PATH = /\bimport\.meta\.(?:url|dirname|filename)\b/;

/**
 * Parses the two output controls accepted by the release task.
 *
 * @param {string[]} argv command-line arguments
 * @returns {{ output: string, archive: string | undefined }}
 */
function parseArguments(argv) {
  let output = DEFAULT_OUTPUT;
  let archive = DEFAULT_ARCHIVE;

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--no-archive") {
      archive = undefined;
      continue;
    }
    if (argument !== "--output" && argument !== "--archive") {
      throw new Error(`unknown argument: ${argument}`);
    }
    const value = argv[index + 1];
    if (value === undefined || value === "") {
      throw new Error(`${argument} requires a path`);
    }
    if (argument === "--output") output = resolve(value);
    if (argument === "--archive") archive = resolve(value);
    index += 1;
  }

  if (archive !== undefined && archive.startsWith(`${output}/`)) {
    throw new Error("--archive must be outside --output");
  }
  return { output, archive };
}

/**
 * Runs a required build command and forwards its transcript.
 *
 * @param {string} command executable name or path
 * @param {string[]} args command arguments
 * @param {NodeJS.ProcessEnv} [env] optional environment
 */
function run(command, args, env = process.env) {
  const result = spawnSync(command, args, {
    cwd: PACKAGE_ROOT,
    env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.stdout !== "") process.stdout.write(result.stdout);
  if (result.stderr !== "") process.stderr.write(result.stderr);
  if (result.error !== undefined) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited with status ${String(result.status)}`);
  }
}

/**
 * Returns the first existing directory or file from a candidate list.
 *
 * @param {string[]} candidates ordered paths
 * @param {string} description value named in an error
 * @returns {string}
 */
function firstExisting(candidates, description) {
  const found = candidates.find((candidate) => existsSync(candidate));
  if (found === undefined) {
    throw new Error(`${description} not found: ${candidates.join(", ")}`);
  }
  return found;
}

/**
 * Replaces the run-time stack lookup with static imports in a temporary copy.
 * Static imports let the bundler include every stack in the single application
 * file without changing the source module owned by the stack port.
 *
 * @param {string} sourceRoot copied source tree
 */
function makeStackImportsStatic(sourceRoot) {
  const appFile = join(sourceRoot, "cdk", "app.ts");
  let source = readFileSync(appFile, "utf8").replaceAll("\r\n", "\n");
  const localImport =
    "import { liveSynthReads, replaySynthReads, type SynthReads } from './synth-reads.ts';";
  const staticImports = [
    "import { buildStack as buildAnalyticsStack } from './stacks/analytics.ts';",
    "import { buildStack as buildBastionHostStack } from './stacks/bastion-host.ts';",
    "import { buildStack as buildBootstrapStack } from './stacks/bootstrap.ts';",
    "import { buildStack as buildClusterStack } from './stacks/cluster.ts';",
    "import { buildStack as buildClusterManagerStack } from './stacks/cluster-manager.ts';",
    "import { buildStack as buildDirectoryServiceStack } from './stacks/directoryservice.ts';",
    "import { buildStack as buildEcsStack } from './stacks/ecs.ts';",
    "import { buildStack as buildIdentityProviderStack } from './stacks/identity-provider.ts';",
    "import { buildStack as buildMetricsStack } from './stacks/metrics.ts';",
    "import { buildStack as buildSchedulerStack } from './stacks/scheduler.ts';",
    "import { buildStack as buildSharedStorageStack } from './stacks/shared-storage.ts';",
    "import { buildStack as buildVirtualDesktopControllerStack } from './stacks/vdc.ts';",
  ].join("\n");
  if (!source.includes(localImport)) {
    throw new Error("cannot locate the stack registry import point");
  }
  source = source.replace(localImport, `${localImport}\n${staticImports}`);

  const registryStart = source.indexOf("/**\n * Module name -> the file under `stacks/`");
  const registryEnd = source.indexOf("/** `Utils.get_as_bool", registryStart);
  if (registryStart < 0 || registryEnd < 0) {
    throw new Error("cannot locate the dynamic stack registry");
  }
  const staticRegistry = `/**
 * Every stack is a static bundle input. The returned promise preserves the
 * StackRegistry contract used by buildApp.
 */
export const DEFAULT_STACK_REGISTRY: StackRegistry = {
  "analytics": async () => buildAnalyticsStack,
  "bastion-host": async () => buildBastionHostStack,
  "bootstrap": async () => buildBootstrapStack,
  "cluster": async () => buildClusterStack,
  "cluster-manager": async () => buildClusterManagerStack,
  "directoryservice": async () => buildDirectoryServiceStack,
  "ecs": async () => buildEcsStack,
  "identity-provider": async () => buildIdentityProviderStack,
  "metrics": async () => buildMetricsStack,
  "scheduler": async () => buildSchedulerStack,
  "shared-storage": async () => buildSharedStorageStack,
  "virtual-desktop-controller": async () => buildVirtualDesktopControllerStack,
};

`;
  source = `${source.slice(0, registryStart)}${staticRegistry}${source.slice(registryEnd)}`;
  writeFileSync(appFile, source);
}

/**
 * Recursively measures regular files and records native add-ons.
 *
 * @param {string} root tree to measure
 * @returns {{ bytes: number, files: number, nativeModules: string[] }}
 */
function measureTree(root) {
  const result = { bytes: 0, files: 0, nativeModules: [] };
  const visit = (path) => {
    const stats = statSync(path);
    if (stats.isDirectory()) {
      for (const name of readdirSync(path)) visit(join(path, name));
      return;
    }
    if (!stats.isFile()) return;
    result.bytes += stats.size;
    result.files += 1;
    if (path.endsWith(".node")) result.nativeModules.push(path.slice(root.length + 1));
  };
  visit(root);
  return result;
}

/**
 * Finds package names represented in the bundle metadata.
 *
 * @param {Record<string, unknown>} metadata parsed bundle metadata
 * @returns {string[]}
 */
function bundledPackages(metadata) {
  const inputs =
    metadata.inputs !== null && typeof metadata.inputs === "object"
      ? Object.keys(metadata.inputs)
      : [];
  const packages = new Set();
  for (const input of inputs) {
    const marker = "node_modules/";
    const markerIndex = input.lastIndexOf(marker);
    if (markerIndex < 0) continue;
    const parts = input.slice(markerIndex + marker.length).split("/");
    const packageName = parts[0]?.startsWith("@")
      ? `${parts[0]}/${parts[1] ?? ""}`
      : parts[0];
    if (packageName !== undefined && !packageName.endsWith("/")) {
      packages.add(packageName);
    }
  }
  return [...packages].sort();
}

/**
 * Lists external imports that the runtime does not provide.
 *
 * @param {Record<string, unknown>} metadata parsed bundle metadata
 * @returns {string[]}
 */
function externalRuntimeImports(metadata) {
  const builtins = new Set(
    builtinModules.flatMap((name) => [
      name,
      name.startsWith("node:") ? name.slice(5) : `node:${name}`,
    ]),
  );
  const outputs =
    metadata.outputs !== null && typeof metadata.outputs === "object"
      ? Object.values(metadata.outputs)
      : [];
  const imports = [];
  for (const output of outputs) {
    if (output === null || typeof output !== "object") continue;
    const entries = Array.isArray(output.imports) ? output.imports : [];
    for (const entry of entries) {
      if (entry === null || typeof entry !== "object") continue;
      if (entry.external !== true || typeof entry.path !== "string") continue;
      if (!builtins.has(entry.path)) imports.push(entry.path);
    }
  }
  return [...new Set(imports)].sort();
}

/**
 * Copies the package resource groups that production command paths read.
 *
 * @param {string} destination output resources directory
 * @param {string} lambdaAssets prebuilt Lambda asset directory
 * @returns {Record<string, number>} exact source bytes by resource group
 */
function copyRuntimeResources(destination, lambdaAssets) {
  const resourceRoot = firstExisting([join(PACKAGE_ROOT, "resources")], "package resources");
  const groups = ["cdk", "config", "input_params", "integration_tests", "policies"];
  const measurements = {};
  mkdirSync(destination, { recursive: true });
  for (const group of groups) {
    const source = firstExisting([join(resourceRoot, group)], `resource group ${group}`);
    cpSync(source, join(destination, group), { recursive: true });
    measurements[group] = measureTree(source).bytes;
  }

  const bootstrap = firstExisting(
    [
      join(resourceRoot, "bootstrap"),
      join(PACKAGE_ROOT, "..", "idea-bootstrap"),
    ],
    "bootstrap resources",
  );
  cpSync(bootstrap, join(destination, "bootstrap"), { recursive: true });
  measurements.bootstrap = measureTree(bootstrap).bytes;

  cpSync(lambdaAssets, join(destination, "lambda_assets"), { recursive: true });
  measurements.lambda_assets = measureTree(lambdaAssets).bytes;
  return measurements;
}

/**
 * Locates a file inside the package's node_modules: the package it belongs to and its path
 * there with forward slashes. Returns undefined for files outside node_modules.
 *
 * @param {string} file absolute file path
 * @param {string} [nodeModules] node_modules directory the bundle resolves from
 * @returns {{ packageName: string, file: string } | undefined}
 */
export function nodeModulesLocation(file, nodeModules = NODE_MODULES) {
  // esbuild reports real paths; compare real paths, so a symlinked checkout or temporary
  // directory still matches.
  const real = (path) => {
    try {
      return realpathSync.native(path);
    } catch {
      return resolve(path);
    }
  };
  const path = relative(real(nodeModules), real(file));
  if (path === "" || path.startsWith("..") || /^[A-Za-z]:/.test(path)) return undefined;
  const posix = path.split(sep).join("/");
  const parts = posix.split("/");
  const packageName = parts[0].startsWith("@") ? `${parts[0]}/${parts[1]}` : parts[0];
  return { packageName, file: posix };
}

/**
 * Returns the code with comments removed, so a path named only in documentation is not taken
 * for a use. Code esbuild cannot parse on its own is returned unchanged, which errs toward a match.
 *
 * @param {string} source module source
 * @returns {string}
 */
export function executableText(source) {
  try {
    // Whitespace minification is what drops every comment, documentation blocks included.
    return transformSync(source, { loader: "js", legalComments: "none", minifyWhitespace: true, logLevel: "silent" }).code;
  } catch {
    return source;
  }
}

/**
 * Bundling moves every dependency file into one output file, so `__dirname` and `__filename`
 * in a dependency would name the bundle instead of the dependency. CDK and cdk-nag read files
 * beside their own modules (Lambda handlers, rule names), so each such file gets its own
 * `__dirname` and `__filename` again, pointing at the same file in a shipped copy of its
 * package under `dist/node_modules`.
 *
 * @param {Map<string, { packageName: string, file: string, code: string }>} rewritten filled with every rewritten file, by its path under node_modules
 * @param {string} [nodeModules] node_modules directory the bundle resolves from
 * @returns {import("esbuild").Plugin}
 */
export function modulePathPlugin(rewritten, nodeModules = NODE_MODULES) {
  return {
    name: "module-paths",
    setup(pluginBuild) {
      pluginBuild.onLoad({ filter: /\.[cm]?js$/ }, (args) => {
        const location = nodeModulesLocation(args.path, nodeModules);
        if (location === undefined) return undefined;
        const source = readFileSync(args.path, "utf8");
        if (!MODULE_PATH_IDENTIFIER.test(source)) return undefined;
        const code = executableText(source);
        if (!MODULE_PATH_IDENTIFIER.test(code)) return undefined;
        if (MODULE_PATH_DECLARATION.test(code)) {
          throw new Error(`${location.file} declares its own __dirname or __filename, which the bundle cannot relocate`);
        }
        rewritten.set(location.file, { ...location, code });
        const directory = location.file.slice(0, location.file.lastIndexOf("/"));
        const declarations =
          `var __dirname = __ideactlPackagePath(${JSON.stringify(directory)}),` +
          ` __filename = __ideactlPackagePath(${JSON.stringify(location.file)});`;
        // A "use strict" directive only counts as the first statement, so the declarations follow it.
        const prologue = /^(?:#![^\n]*\n)?(?:\s*(["'])use strict\1;?)?/.exec(source)?.[0] ?? "";
        return {
          contents: `${prologue}\n${declarations}\n${source.slice(prologue.length)}`,
          loader: "js",
          resolveDir: dirname(args.path),
        };
      });
    },
  };
}

/**
 * Copies a package for run-time file reads, without the files a run never opens.
 *
 * @param {string} packageName package directory name under node_modules
 * @param {string} destination output node_modules directory
 * @param {string} [nodeModules] node_modules directory the package is copied from
 */
export function shipPackage(packageName, destination, nodeModules = NODE_MODULES) {
  const source = join(nodeModules, packageName);
  cpSync(source, join(destination, packageName), {
    recursive: true,
    // .bin holds npm's command shims (symbolic links); nothing in a run reads them.
    filter: (path) => basename(path) !== ".bin" && !UNSHIPPED_PACKAGE_FILE.test(basename(path)),
  });
}

/**
 * Fails the build unless every dependency path the bundle can resolve at run time exists.
 *
 * Every dependency input that still uses `__dirname`, `__filename`, or `import.meta` paths after
 * comments are removed must have been rewritten; a rewritten module must be CommonJS (the
 * declarations are module-scoped only there); and every path built from `__dirname` with literal
 * arguments must exist in the shipped package tree. Paths with computed parts cannot be checked
 * here; they are reported with counts, and the release synthesis gate exercises the ones IDEA's
 * stacks reach.
 *
 * @param {import("esbuild").Metafile} metafile bundle metadata
 * @param {Map<string, { packageName: string, file: string, code: string }>} rewritten rewritten modules
 * @param {string} shippedRoot output node_modules directory
 * @param {{ workingDirectory?: string, nodeModules?: string }} [roots] build working directory and node_modules
 * @returns {{ checked: number, computed: Record<string, number> }}
 */
export function verifyModulePaths(metafile, rewritten, shippedRoot, roots = {}) {
  const workingDirectory = roots.workingDirectory ?? PACKAGE_ROOT;
  const nodeModules = roots.nodeModules ?? NODE_MODULES;
  const problems = [];
  for (const [input, meta] of Object.entries(metafile.inputs)) {
    const file = resolve(workingDirectory, input);
    const location = nodeModulesLocation(file, nodeModules);
    if (location === undefined) continue;
    const entry = rewritten.get(location.file);
    if (entry === undefined) {
      if (!existsSync(file)) continue;
      const code = executableText(readFileSync(file, "utf8"));
      if (MODULE_PATH_IDENTIFIER.test(code)) problems.push(`${location.file} uses __dirname or __filename without a rewrite`);
      if (MODULE_META_PATH.test(code)) problems.push(`${location.file} reads its location from import.meta, which names the bundle`);
      continue;
    }
    if (meta.format === "esm") problems.push(`${location.file} is an ES module; its __dirname rewrite is not module-scoped`);
    if (MODULE_META_PATH.test(entry.code)) problems.push(`${location.file} reads its location from import.meta, which names the bundle`);
  }

  let checked = 0;
  const computed = {};
  const literalJoin = /\b(?:join|resolve)\)?\(\s*__dirname((?:\s*,\s*(?:"[^"\\]*"|'[^'\\]*'))*)\s*\)/g;
  for (const entry of rewritten.values()) {
    const directory = entry.file.slice(0, entry.file.lastIndexOf("/"));
    if (!existsSync(join(shippedRoot, ...entry.file.split("/")))) problems.push(`${entry.file} is not in the shipped package tree`);
    let literalUses = 0;
    for (const match of entry.code.matchAll(literalJoin)) {
      literalUses += 1;
      const parts = [...match[1].matchAll(/"([^"\\]*)"|'([^'\\]*)'/g)].map((part) => part[1] ?? part[2]);
      const target = join(shippedRoot, ...directory.split("/"), ...parts);
      checked += 1;
      if (!existsSync(target)) problems.push(`${entry.file}: ${match[0]} resolves to ${target}, which is not shipped`);
    }
    const uses = (entry.code.match(/\b__dirname\b/g) ?? []).length;
    if (uses > literalUses) computed[entry.file] = uses - literalUses;
  }
  if (problems.length > 0) {
    throw new Error(`bundled dependency paths would not resolve at run time:\n  ${problems.join("\n  ")}`);
  }
  return { checked, computed };
}

/**
 * Writes the executable entry file. It imports the application bundle and
 * mirrors the bundle's error-to-exit-code handling using only the runtime.
 *
 * @param {string} output artifact root
 */
function writeLauncher(output) {
  const launcher = join(output, "bin", "ideactl");
  mkdirSync(dirname(launcher), { recursive: true });
  writeFileSync(
    launcher,
    `#!/usr/bin/env node
const { run } = await import("../dist/src/cli/main.js");
run().then(
  (code) => {
    if (code !== 0) process.exitCode = code;
  },
  (error) => {
    console.error(error);
    process.exitCode = 1;
  },
);
`,
  );
  chmodSync(launcher, 0o755);
}

/**
 * Builds, validates, measures, and optionally archives the direct-runtime
 * distribution.
 */
async function main() {
  const { output, archive } = parseArguments(process.argv.slice(2));
  const temporary = mkdtempSync(join(tmpdir(), "ideactl-shell-build-"));
  const copiedSource = join(temporary, "src");
  const optionalWatcherShim = join(temporary, "optional-watcher.cjs");
  const lambdaAssets = join(temporary, "lambda_assets");
  const bundleFile = join(output, "dist", "src", "cli", "main.js");
  const metadataFile = join(output, "bundle-metadata.json");

  try {
    rmSync(output, { recursive: true, force: true });
    mkdirSync(dirname(bundleFile), { recursive: true });
    cpSync(join(PACKAGE_ROOT, "src"), copiedSource, { recursive: true });
    makeStackImportsStatic(copiedSource);
    writeFileSync(
      optionalWatcherShim,
      `"use strict";
module.exports = {
  watch() {
    throw new Error("template watching is not available in the release artifact");
  },
};
`,
    );

    // The API avoids platform-specific package-manager launchers.
    const rewritten = new Map();
    const bundleResult = await build({
      entryPoints: [join(copiedSource, "cli", "main.ts")],
      bundle: true,
      platform: "node",
      format: "esm",
      target: "node22",
      legalComments: "external",
      alias: { chokidar: optionalWatcherShim },
      banner: { js: BUNDLE_BANNER },
      plugins: [modulePathPlugin(rewritten)],
      absWorkingDir: PACKAGE_ROOT,
      logLevel: "warning",
      metafile: true,
      outfile: bundleFile,
      nodePaths: [NODE_MODULES],
    });
    writeFileSync(metadataFile, JSON.stringify(bundleResult.metafile));
    chmodSync(bundleFile, 0o755);

    // Dependencies that read files beside their own modules get the whole package, minus files
    // a run never opens, at the path their rewritten __dirname names.
    const shippedRoot = join(output, "dist", "node_modules");
    const shippedPackages = [...new Set([...rewritten.values()].map((entry) => entry.packageName))].sort();
    for (const packageName of shippedPackages) shipPackage(packageName, shippedRoot);
    const modulePaths = verifyModulePaths(bundleResult.metafile, rewritten, shippedRoot);
    console.log(`relocated dependency modules: ${String(rewritten.size)} in ${shippedPackages.join(", ")}`);
    console.log(`checked literal dependency paths: ${String(modulePaths.checked)}`);
    for (const [file, count] of Object.entries(modulePaths.computed).sort()) {
      console.log(`computed dependency path, shipped with its package but not checked here: ${file} (${String(count)})`);
    }

    run(process.execPath, [join(PACKAGE_ROOT, "scripts", "build-lambda-bundles.mjs"), lambdaAssets]);
    const resources = copyRuntimeResources(
      join(output, "dist", "resources"),
      lambdaAssets,
    );
    mkdirSync(shippedRoot, { recursive: true });
    cpSync(
      firstExisting(
        [
          join(PACKAGE_ROOT, "node_modules", "aws-cdk"),
        ],
        "pinned deployment CLI",
      ),
      join(output, "dist", "node_modules", "aws-cdk"),
      { recursive: true },
    );
    copyFileSync(join(PACKAGE_ROOT, "cdk.json"), join(output, "dist", "cdk.json"));
    mkdirSync(join(output, "dist", "src"), { recursive: true });
    copyFileSync(
      firstExisting(
        [
          join(PACKAGE_ROOT, "IDEA_VERSION.txt"),
          join(PACKAGE_ROOT, "..", "..", "..", "IDEA_VERSION.txt"),
        ],
        "IDEA_VERSION.txt",
      ),
      join(output, "dist", "src", "IDEA_VERSION.txt"),
    );
    writeLauncher(output);

    const metadata = JSON.parse(readFileSync(metadataFile, "utf8"));
    const externalImports = externalRuntimeImports(metadata);
    if (externalImports.length > 0) {
      throw new Error(`external runtime imports found: ${externalImports.join(", ")}`);
    }
    const packages = bundledPackages(metadata);
    rmSync(metadataFile);
    const bundle = measureTree(bundleFile);
    const deploymentCli = measureTree(join(output, "dist", "node_modules", "aws-cdk"));
    const shippedPackageTree = shippedPackages.map((name) => measureTree(join(shippedRoot, name)));
    const resourceTree = measureTree(join(output, "dist", "resources"));
    const artifactBeforeManifest = measureTree(output);
    const nativeModules = [
      ...bundle.nativeModules,
      ...deploymentCli.nativeModules,
      ...resourceTree.nativeModules,
      ...shippedPackageTree.flatMap((tree) => tree.nativeModules),
    ];
    if (nativeModules.length > 0) {
      throw new Error(`native modules found in artifact: ${nativeModules.join(", ")}`);
    }

    const manifest = {
      schemaVersion: 1,
      packageVersion: PACKAGE_JSON.version,
      runtime: {
        node: PACKAGE_JSON.engines.node,
        packageManagerRequired: false,
        compilerRequired: false,
        nativeModules,
        externalImports,
      },
      contentBytes: {
        applicationBundle: bundle.bytes,
        deploymentCli: deploymentCli.bytes,
        shippedPackages: shippedPackageTree.reduce((total, tree) => total + tree.bytes, 0),
        resources: resourceTree.bytes,
        artifactBeforeManifest: artifactBeforeManifest.bytes,
      },
      resourceSourceBytes: resources,
      bundledPackages: packages,
      shippedPackages,
      relocatedModules: rewritten.size,
      resourceReads: [
        "resources/bootstrap",
        "resources/cdk",
        "resources/config",
        "resources/input_params",
        "resources/integration_tests",
        "resources/lambda_assets when prebuilt, otherwise bundled on demand",
        "resources/policies",
      ],
    };
    writeFileSync(join(output, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);

    run(process.execPath, [join(output, "bin", "ideactl"), "about"]);
    run(process.execPath, [
      join(output, "dist", "node_modules", "aws-cdk", "bin", "cdk"),
      "--version",
    ]);

    const finalArtifact = measureTree(output);
    console.log(`application bundle bytes: ${String(bundle.bytes)}`);
    console.log(`deployment CLI bytes: ${String(deploymentCli.bytes)}`);
    console.log(`resource bytes: ${String(resourceTree.bytes)}`);
    console.log(`artifact bytes: ${String(finalArtifact.bytes)}`);
    console.log(`native modules: ${nativeModules.length === 0 ? "none" : nativeModules.join(", ")}`);

    if (archive !== undefined) {
      mkdirSync(dirname(archive), { recursive: true });
      rmSync(archive, { force: true });
      run("tar", ["-czf", archive, "-C", dirname(output), basename(output)]);
      console.log(`archive bytes: ${String(statSync(archive).size)}`);
      console.log(`archive: ${archive}`);
    }
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] !== undefined && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  await main();
}
