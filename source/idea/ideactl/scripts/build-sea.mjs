#!/usr/bin/env node
/**
 * Builds native macOS, Linux, or Windows releases with an explicit target.
 * The default remains the same-architecture macOS and Linux pair.
 *
 * `npm run build:dist` produces both. Each file is the official Node runtime
 * for that operating system, with the application, deployment CLI, and
 * resource tree compressed inside. First use extracts the support tree into a
 * per-user temporary directory.
 */

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  copyFileSync,
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PACKAGE_JSON = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));
const VERSION = requireNonemptyString(PACKAGE_JSON.version, "package version");
const DEFAULT_OUTPUT_DIRECTORY = join(PACKAGE_ROOT, "dist", "release");
const INTERNAL_CDK_ARGUMENT = "__ideactl_internal_cdk__";
const TAR_BLOCK_SIZE = 512;

/**
 * Validates an untrusted JSON field before it enters a path or artifact name.
 *
 * @param {unknown} value parsed JSON value
 * @param {string} name field name used in an error
 * @returns {string}
 */
function requireNonemptyString(value, name) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

/**
 * Converts runtime platform names to release artifact conventions.
 *
 * @param {NodeJS.Platform} platform runtime platform
 * @param {string} architecture runtime architecture
 * @returns {string}
 */
function releaseTarget(platform, architecture) {
  if (platform !== "darwin" && platform !== "linux" && platform !== "win32") {
    throw new Error(`unsupported release operating system: ${platform}`);
  }
  const releaseArchitecture =
    architecture === "x64" ? "amd64" : architecture === "arm64" ? "arm64" : undefined;
  if (releaseArchitecture === undefined) {
    throw new Error(`unsupported release processor architecture: ${architecture}`);
  }
  return `${platform === "win32" ? "windows" : platform}-${releaseArchitecture}`;
}

/**
 * Returns the release targets for the builder's processor architecture.
 *
 * @returns {string[]}
 */
function releaseTargets() {
  // Apple no longer ships Intel Macs, so darwin-amd64 is never a release target.
  return [
    ...(process.arch === "arm64" ? [releaseTarget("darwin", process.arch)] : []),
    releaseTarget("linux", process.arch),
  ];
}

/**
 * Parses build controls and rejects a target that does not match the builder.
 *
 * @param {string[]} argv command-line arguments
 * @returns {{
 *   outputDirectory: string;
 *   target: string;
 *   executable: string | undefined;
 *   builder: string | undefined;
 *   shellArtifact: string | undefined;
 * }}
 */
function parseArguments(argv) {
  let outputDirectory = DEFAULT_OUTPUT_DIRECTORY;
  let target = releaseTarget(process.platform, process.arch);
  let executable;
  let builder;
  let shellArtifact;

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (
      argument !== "--output-directory" &&
      argument !== "--target" &&
      argument !== "--executable" &&
      argument !== "--builder" &&
      argument !== "--shell-artifact"
    ) {
      throw new Error(`unknown argument: ${argument}`);
    }
    const value = argv[index + 1];
    if (value === undefined || value === "") {
      throw new Error(`${argument} requires a value`);
    }
    if (argument === "--output-directory") outputDirectory = resolve(value);
    if (argument === "--target") target = value;
    if (argument === "--executable") executable = resolve(value);
    if (argument === "--builder") builder = resolve(value);
    if (argument === "--shell-artifact") shellArtifact = resolve(value);
    index += 1;
  }

  const supportedTargets = process.platform === "win32" ? [releaseTarget("win32", process.arch)] : releaseTargets();
  if (!supportedTargets.includes(target)) {
    throw new Error(
      `unsupported release target for ${process.arch}: ${target}; expected ${supportedTargets.join(" or ")}`,
    );
  }
  const hostTarget = releaseTarget(process.platform, process.arch);
  if (target.startsWith("darwin-") && process.platform !== "darwin") {
    throw new Error(`target ${target} must be built and signed on macOS`);
  }
  if (
    executable === undefined &&
    target !== hostTarget &&
    !(process.platform === "darwin" && target.startsWith("linux-"))
  ) {
    throw new Error(
      `target ${target} requires --executable for that target, current runtime is ${hostTarget}`,
    );
  }
  if (executable !== undefined && !existsSync(executable)) {
    throw new Error(`target runtime executable not found: ${executable}`);
  }
  if (builder !== undefined && !existsSync(builder)) {
    throw new Error(`builder runtime executable not found: ${builder}`);
  }
  if (shellArtifact !== undefined && !existsSync(shellArtifact)) {
    throw new Error(`bundled application artifact not found: ${shellArtifact}`);
  }
  return { outputDirectory, target, executable, builder, shellArtifact };
}

/**
 * Runs one required build command and includes its output in failures.
 *
 * @param {string} command executable name or path
 * @param {string[]} args command arguments
 * @param {string} cwd working directory
 * @param {NodeJS.ProcessEnv} env child environment
 */
function run(command, args, cwd = PACKAGE_ROOT, env = process.env) {
  const result = spawnSync(command, args, {
    cwd,
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
 * Returns whether a runtime was compiled with direct SEA generation enabled.
 *
 * @param {string} executable runtime executable
 * @param {string} probeConfig deliberately absent configuration path
 * @returns {boolean}
 */
function canBuildSea(executable, probeConfig) {
  const result = spawnSync(executable, ["--build-sea", probeConfig], {
    cwd: PACKAGE_ROOT,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error !== undefined) throw result.error;
  return !`${result.stdout}${result.stderr}`.includes("Single executable application is disabled");
}

/**
 * Downloads and verifies the official runtime when the installed runtime has
 * SEA generation disabled. The cache stays under node_modules and is not a
 * release input until its published checksum has been verified.
 *
 * @param {string} temporaryRoot temporary build root
 * @returns {string}
 */
function officialRuntime(target, temporaryRoot) {
  const separator = target.lastIndexOf("-");
  const platform = target.startsWith("windows-") ? "win" : target.slice(0, separator);
  const releaseArchitecture = target.slice(separator + 1) === "amd64" ? "x64" : "arm64";
  const directoryName = `node-${process.version}-${platform}-${releaseArchitecture}`;
  const archiveName = `${directoryName}.${platform === "win" ? "zip" : "tar.gz"}`;
  const downloadRoot = `https://nodejs.org/dist/${process.version}`;
  const cacheRoot = join(PACKAGE_ROOT, "node_modules", ".cache", "ideactl-sea", directoryName);
  const cachedExecutable = join(cacheRoot, ...(platform === "win" ? ["node.exe"] : ["bin", "node"]));
  if (existsSync(cachedExecutable)) return cachedExecutable;

  const downloadDirectory = join(temporaryRoot, "node-download");
  const archive = join(downloadDirectory, archiveName);
  const checksums = join(downloadDirectory, "SHASUMS256.txt");
  const extraction = join(downloadDirectory, "extract");
  mkdirSync(downloadDirectory, { recursive: true });
  mkdirSync(extraction, { recursive: true });
  run("curl", ["--fail", "--location", "--silent", "--show-error", "-o", checksums, `${downloadRoot}/SHASUMS256.txt`]);
  run("curl", ["--fail", "--location", "--silent", "--show-error", "-o", archive, `${downloadRoot}/${archiveName}`]);

  const checksumLine = readFileSync(checksums, "utf8")
    .split(/\r?\n/)
    .find((line) => line.endsWith(`  ${archiveName}`));
  if (checksumLine === undefined) {
    throw new Error(`published checksum not found for ${archiveName}`);
  }
  const expected = checksumLine.split(/\s+/)[0];
  const actual = createHash("sha256").update(readFileSync(archive)).digest("hex");
  if (actual !== expected) {
    throw new Error(`runtime checksum mismatch for ${archiveName}: expected ${expected}, got ${actual}`);
  }

  run("tar", ["-xf", archive, "-C", extraction]);
  const extractedExecutable = join(extraction, directoryName, ...(platform === "win" ? ["node.exe"] : ["bin", "node"]));
  if (!existsSync(extractedExecutable)) {
    throw new Error(`runtime archive did not contain ${directoryName}/bin/node`);
  }
  mkdirSync(dirname(cacheRoot), { recursive: true });
  rmSync(cacheRoot, { recursive: true, force: true });
  cpSync(join(extraction, directoryName), cacheRoot, { recursive: true });
  return cachedExecutable;
}

/**
 * Downloads the official runtime for the current host when SEA is disabled.
 *
 * @param {string} temporaryRoot temporary build root
 * @returns {string}
 */
function officialSeaBuilder(temporaryRoot) {
  return officialRuntime(releaseTarget(process.platform, process.arch), temporaryRoot);
}

/**
 * Selects a SEA-enabled builder and fails if an explicit builder is disabled.
 *
 * @param {string | undefined} requested explicit builder
 * @param {string} temporaryRoot temporary build root
 * @returns {string}
 */
function seaBuilder(requested, temporaryRoot) {
  const probe = join(temporaryRoot, "missing-sea-config.json");
  const candidate = requested ?? process.execPath;
  if (canBuildSea(candidate, probe)) return candidate;
  if (requested !== undefined) {
    throw new Error(`builder runtime has single-executable generation disabled: ${requested}`);
  }
  const official = officialSeaBuilder(temporaryRoot);
  if (!canBuildSea(official, probe)) {
    throw new Error(`official runtime has single-executable generation disabled: ${official}`);
  }
  return official;
}

/**
 * Copies one required path and reports the missing input directly.
 *
 * @param {string} source required source path
 * @param {string} destination destination path
 */
function copyRequired(source, destination) {
  if (!existsSync(source)) throw new Error(`required distribution input not found: ${source}`);
  mkdirSync(dirname(destination), { recursive: true });
  if (statSync(source).isDirectory()) {
    cpSync(source, destination, { recursive: true });
  } else {
    copyFileSync(source, destination);
  }
}

/**
 * Stages only files the executable needs after extraction.
 *
 * @param {string} shellArtifact direct-runtime distribution root
 * @param {string} runtimeRoot temporary support-tree root
 */
function stageRuntime(shellArtifact, runtimeRoot) {
  const shellDist = join(shellArtifact, "dist");
  copyRequired(
    join(shellDist, "src", "cli"),
    join(runtimeRoot, "dist", "src", "cli"),
  );
  copyRequired(
    join(shellDist, "src", "IDEA_VERSION.txt"),
    join(runtimeRoot, "dist", "src", "IDEA_VERSION.txt"),
  );
  copyRequired(
    join(shellDist, "resources"),
    join(runtimeRoot, "dist", "resources"),
  );
  copyRequired(
    join(shellDist, "node_modules", "aws-cdk"),
    join(runtimeRoot, "dist", "node_modules", "aws-cdk"),
  );
  copyRequired(
    join(shellDist, "custom-resource-handlers"),
    join(runtimeRoot, "dist", "custom-resource-handlers"),
  );
  copyRequired(join(shellDist, "cdk.json"), join(runtimeRoot, "dist", "cdk.json"));

  writeFileSync(
    join(runtimeRoot, "launcher.cjs"),
    `"use strict";
module.exports = import("./dist/src/cli/main.js").then(({ run }) => run());
`,
  );
}

/**
 * Lists a tree in stable archive order and rejects unsupported file types.
 *
 * @param {string} root tree root
 * @returns {Array<{ archiveName: string; path: string; isDirectory: boolean }>}
 */
function tarEntries(root) {
  const entries = [];
  const visit = (directory) => {
    const names = readdirSync(directory).sort();
    for (const name of names) {
      const path = join(directory, name);
      const stats = lstatSync(path);
      const archiveName = relative(root, path).split(sep).join("/");
      if (stats.isSymbolicLink()) {
        throw new Error(`symbolic links are not supported in the release artifact: ${archiveName}`);
      }
      if (stats.isDirectory()) {
        entries.push({ archiveName, path, isDirectory: true });
        visit(path);
      } else if (stats.isFile()) {
        entries.push({ archiveName, path, isDirectory: false });
      } else {
        throw new Error(`unsupported file type in release artifact: ${archiveName}`);
      }
    }
  };
  visit(root);
  return entries;
}

/**
 * Writes an ASCII field into a tar header.
 *
 * @param {Buffer} target destination header
 * @param {string} value field value
 * @param {number} offset byte offset
 * @param {number} length field length
 */
function writeString(target, value, offset, length) {
  const encoded = Buffer.from(value, "utf8");
  if (encoded.length > length) throw new Error(`tar field exceeds ${String(length)} bytes: ${value}`);
  encoded.copy(target, offset);
}

/**
 * Writes a fixed-width octal tar field.
 *
 * @param {Buffer} target destination header
 * @param {number} value non-negative integer
 * @param {number} offset byte offset
 * @param {number} length field length
 */
function writeOctal(target, value, offset, length) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`invalid tar numeric field: ${String(value)}`);
  }
  const encoded = value.toString(8).padStart(length - 1, "0");
  if (encoded.length >= length) throw new Error(`tar numeric field is too large: ${String(value)}`);
  writeString(target, `${encoded}\0`, offset, length);
}

/**
 * Splits a path into the POSIX ustar name and prefix fields.
 *
 * @param {string} archiveName relative archive path
 * @returns {{ name: string; prefix: string }}
 */
function splitTarPath(archiveName) {
  if (Buffer.byteLength(archiveName, "utf8") <= 100) {
    return { name: archiveName, prefix: "" };
  }
  const separators = [...archiveName.matchAll(/\//g)].map((match) => match.index ?? -1).reverse();
  for (const separator of separators) {
    const prefix = archiveName.slice(0, separator);
    const name = archiveName.slice(separator + 1);
    if (Buffer.byteLength(prefix, "utf8") <= 155 && Buffer.byteLength(name, "utf8") <= 100) {
      return { name, prefix };
    }
  }
  throw new Error(`archive path exceeds the ustar path limit: ${archiveName}`);
}

/**
 * Builds one deterministic POSIX ustar header.
 *
 * @param {{ archiveName: string; path: string; isDirectory: boolean }} entry archive entry
 * @returns {Buffer}
 */
function tarHeader(entry) {
  const stats = lstatSync(entry.path);
  const archiveName = entry.isDirectory ? `${entry.archiveName}/` : entry.archiveName;
  const { name, prefix } = splitTarPath(archiveName);
  const header = Buffer.alloc(TAR_BLOCK_SIZE);
  writeString(header, name, 0, 100);
  writeOctal(header, stats.mode & 0o777, 100, 8);
  writeOctal(header, 0, 108, 8);
  writeOctal(header, 0, 116, 8);
  writeOctal(header, entry.isDirectory ? 0 : stats.size, 124, 12);
  writeOctal(header, 0, 136, 12);
  header.fill(0x20, 148, 156);
  writeString(header, entry.isDirectory ? "5" : "0", 156, 1);
  writeString(header, "ustar\0", 257, 6);
  writeString(header, "00", 263, 2);
  writeString(header, prefix, 345, 155);
  const checksum = header.reduce((total, byte) => total + byte, 0);
  writeString(header, `${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8);
  return header;
}

/**
 * Creates a deterministic gzip-compressed ustar archive in memory.
 *
 * @param {string} root source tree
 * @returns {Buffer}
 */
function createTarGz(root) {
  const blocks = [];
  for (const entry of tarEntries(root)) {
    blocks.push(tarHeader(entry));
    if (entry.isDirectory) continue;
    const contents = readFileSync(entry.path);
    blocks.push(contents);
    const remainder = contents.length % TAR_BLOCK_SIZE;
    if (remainder !== 0) blocks.push(Buffer.alloc(TAR_BLOCK_SIZE - remainder));
  }
  blocks.push(Buffer.alloc(TAR_BLOCK_SIZE * 2));
  return gzipSync(Buffer.concat(blocks), { level: 9 });
}

/**
 * Measures regular-file bytes in a tree.
 *
 * @param {string} root tree root
 * @returns {number}
 */
function treeBytes(root) {
  let bytes = 0;
  const visit = (path) => {
    const stats = lstatSync(path);
    if (stats.isDirectory()) {
      for (const name of readdirSync(path)) visit(join(path, name));
    } else if (stats.isFile()) {
      bytes += stats.size;
    }
  };
  visit(root);
  return bytes;
}

/**
 * Produces the injected CommonJS launcher.
 *
 * @param {string} runtimeHash support archive digest
 * @returns {string}
 */
function seaLauncher(runtimeHash) {
  return `"use strict";
const { createRequire } = require("node:module");
const {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const { getAsset } = require("node:sea");
const { tmpdir } = require("node:os");
const { delimiter, dirname, join, resolve, sep } = require("node:path");
const { gunzipSync } = require("node:zlib");

const VERSION = ${JSON.stringify(VERSION)};
const RUNTIME_HASH = ${JSON.stringify(runtimeHash)};
const INTERNAL_CDK_ARGUMENT = ${JSON.stringify(INTERNAL_CDK_ARGUMENT)};
const BLOCK_SIZE = 512;

function field(buffer, offset, length) {
  const end = buffer.indexOf(0, offset);
  const limit = end >= offset && end < offset + length ? end : offset + length;
  return buffer.toString("utf8", offset, limit);
}

function octal(buffer, offset, length) {
  const value = field(buffer, offset, length).trim();
  if (!/^[0-7]*$/.test(value)) throw new Error("invalid embedded archive numeric field");
  return value === "" ? 0 : Number.parseInt(value, 8);
}

function extractRuntime(destination) {
  const archive = gunzipSync(Buffer.from(getAsset("runtime.tar.gz")));
  let offset = 0;
  while (offset + BLOCK_SIZE <= archive.length) {
    const header = archive.subarray(offset, offset + BLOCK_SIZE);
    offset += BLOCK_SIZE;
    if (header.every((byte) => byte === 0)) break;
    const name = field(header, 0, 100);
    const prefix = field(header, 345, 155);
    const archiveName = prefix === "" ? name : \`\${prefix}/\${name}\`;
    const size = octal(header, 124, 12);
    const mode = octal(header, 100, 8) & 0o777;
    const type = field(header, 156, 1) || "0";
    const target = resolve(destination, archiveName);
    const destinationPrefix = \`\${resolve(destination)}\${sep}\`;
    if (!target.startsWith(destinationPrefix)) {
      throw new Error(\`invalid embedded archive path: \${archiveName}\`);
    }
    if (type === "5") {
      mkdirSync(target, { recursive: true, mode });
      chmodSync(target, mode);
    } else if (type === "0") {
      mkdirSync(dirname(target), { recursive: true });
      writeFileSync(target, archive.subarray(offset, offset + size), { mode });
      chmodSync(target, mode);
    } else {
      throw new Error(\`unsupported embedded archive entry type: \${type}\`);
    }
    offset += Math.ceil(size / BLOCK_SIZE) * BLOCK_SIZE;
  }
}

function ensureRuntime() {
  const uid = typeof process.getuid === "function" ? String(process.getuid()) : "user";
  const parent = join(tmpdir(), \`ideactl-\${uid}\`);
  const destination = join(parent, \`\${VERSION}-\${RUNTIME_HASH.slice(0, 16)}\`);
  const marker = join(destination, ".complete");
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  chmodSync(parent, 0o700);
  if (!existsSync(marker) || readFileSync(marker, "utf8") !== RUNTIME_HASH) {
    if (existsSync(destination)) {
      if (lstatSync(destination).isSymbolicLink()) {
        throw new Error(\`refusing symbolic-link runtime directory: \${destination}\`);
      }
      rmSync(destination, { recursive: true, force: true });
    }
    const staging = mkdtempSync(join(parent, ".extract-"));
    try {
      extractRuntime(staging);
      writeFileSync(join(staging, ".complete"), RUNTIME_HASH, { mode: 0o600 });
      try {
        renameSync(staging, destination);
      } catch (error) {
        if (!existsSync(marker) || readFileSync(marker, "utf8") !== RUNTIME_HASH) throw error;
        rmSync(staging, { recursive: true, force: true });
      }
    } catch (error) {
      rmSync(staging, { recursive: true, force: true });
      throw error;
    }
  }

  process.env.IDEA_CDK_BIN = process.execPath;
  process.env.IDEA_SEA = "1";
  process.env.PATH = \`\${dirname(process.execPath)}\${delimiter}\${process.env.PATH ?? ""}\`;
  return destination;
}

async function main() {
  const runtimeRoot = ensureRuntime();
  const requireFromRuntime = createRequire(join(runtimeRoot, "launcher.cjs"));
  if (process.argv[2] === INTERNAL_CDK_ARGUMENT) {
    process.argv.splice(2, 1);
    requireFromRuntime(join(runtimeRoot, "dist", "node_modules", "aws-cdk", "bin", "cdk"));
    return;
  }
  const code = await requireFromRuntime(join(runtimeRoot, "launcher.cjs"));
  if (typeof code === "number" && code !== 0) process.exitCode = code;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
`;
}

/**
 * Writes the release archive containing only the executable.
 *
 * @param {string} executable built executable path
 * @param {string} archive output archive path
 * @param {string} temporaryRoot temporary build root
 */
function writeReleaseArchive(executable, archive, temporaryRoot) {
  const releaseRoot = join(temporaryRoot, "release-package");
  mkdirSync(releaseRoot);
  if (archive.endsWith(".zip")) {
    copyFileSync(executable, join(releaseRoot, "ideactl.exe"));
    run("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command",
      "$ErrorActionPreference = 'Stop'; Compress-Archive -Force -LiteralPath $env:IDEA_ZIP_SOURCE -DestinationPath $env:IDEA_ZIP_DESTINATION"],
      PACKAGE_ROOT, { ...process.env, IDEA_ZIP_SOURCE: join(releaseRoot, "ideactl.exe"), IDEA_ZIP_DESTINATION: archive });
    return;
  }
  copyFileSync(executable, join(releaseRoot, "ideactl"));
  chmodSync(join(releaseRoot, "ideactl"), 0o755);
  writeFileSync(archive, createTarGz(releaseRoot));
}

/**
 * Builds, verifies, measures, and checksums the host release artifact.
 */
function main() {
  const { outputDirectory, target, executable, builder, shellArtifact } = parseArguments(
    process.argv.slice(2),
  );
  const temporaryRoot = mkdtempSync(join(tmpdir(), "ideactl-sea-build-"));
  const shellRoot = shellArtifact ?? join(temporaryRoot, "shell");
  const runtimeRoot = join(temporaryRoot, "runtime");
  const runtimeArchive = join(temporaryRoot, "runtime.tar.gz");
  const launcher = join(temporaryRoot, "sea-launcher.cjs");
  const seaConfig = join(temporaryRoot, "sea-config.json");
  const targetDirectory = join(outputDirectory, target);
  const builtExecutable = join(targetDirectory, target.startsWith("windows-") ? "ideactl.exe" : "ideactl");
  const artifactBase = `ideactl-v${VERSION}-${target}`;
  const releaseArchive = join(outputDirectory, `${artifactBase}.${target.startsWith("windows-") ? "zip" : "tar.gz"}`);
  const checksumFile = `${releaseArchive}.sha256`;
  const metadataFile = join(outputDirectory, `${artifactBase}.json`);

  try {
    const builderExecutable = seaBuilder(builder, temporaryRoot);
    let targetExecutable = executable;
    const hostTarget = releaseTarget(process.platform, process.arch);
    if (targetExecutable === undefined && target !== hostTarget) {
      targetExecutable = officialRuntime(target, temporaryRoot);
    }
    if (shellArtifact === undefined) {
      run(process.execPath, [
        join(PACKAGE_ROOT, "scripts", "build-shell-bundle.mjs"),
        "--output",
        shellRoot,
        "--no-archive",
      ]);
    }

    mkdirSync(runtimeRoot, { recursive: true });
    stageRuntime(shellRoot, runtimeRoot);
    const extractedBytes = treeBytes(runtimeRoot);
    const runtimeBytes = createTarGz(runtimeRoot);
    writeFileSync(runtimeArchive, runtimeBytes);
    const runtimeHash = createHash("sha256").update(runtimeBytes).digest("hex");
    writeFileSync(launcher, seaLauncher(runtimeHash));

    rmSync(targetDirectory, { recursive: true, force: true });
    mkdirSync(targetDirectory, { recursive: true });
    const config = {
      main: launcher,
      mainFormat: "commonjs",
      output: builtExecutable,
      disableExperimentalSEAWarning: true,
      useSnapshot: false,
      useCodeCache: false,
      execArgvExtension: "env",
      assets: { "runtime.tar.gz": runtimeArchive },
    };
    if (targetExecutable !== undefined) config.executable = targetExecutable;
    writeFileSync(seaConfig, `${JSON.stringify(config, null, 2)}\n`);
    run(builderExecutable, ["--build-sea", seaConfig]);
    if (target.startsWith("darwin-")) {
      if (process.platform !== "darwin") {
        throw new Error(`target ${target} must be signed on a macOS builder`);
      }
      run("codesign", ["--sign", "-", "--force", builtExecutable]);
    }
    chmodSync(builtExecutable, 0o755);

    if (target === releaseTarget(process.platform, process.arch)) {
      run(builtExecutable, ["about"]);
    }

    mkdirSync(outputDirectory, { recursive: true });
    writeReleaseArchive(builtExecutable, releaseArchive, temporaryRoot);
    const releaseBytes = readFileSync(releaseArchive);
    const releaseHash = createHash("sha256").update(releaseBytes).digest("hex");
    writeFileSync(checksumFile, `${releaseHash}  ${basename(releaseArchive)}\n`);

    const metadata = {
      schemaVersion: 1,
      packageVersion: VERSION,
      target,
      buildRuntime: process.version,
      seaBuilder: builderExecutable === process.execPath ? "installed runtime" : "verified official runtime",
      selfContained: true,
      runTimeRequirements: [],
      resourceHandling:
        "Resources and the pinned deployment CLI are embedded and extracted to a per-user temporary cache.",
      sizes: {
        executableBytes: statSync(builtExecutable).size,
        releaseArchiveBytes: statSync(releaseArchive).size,
        embeddedSupportArchiveBytes: runtimeBytes.length,
        extractedSupportBytes: extractedBytes,
      },
      sha256: releaseHash,
    };
    writeFileSync(metadataFile, `${JSON.stringify(metadata, null, 2)}\n`);

    console.log(`target: ${target}`);
    console.log(`executable bytes: ${String(metadata.sizes.executableBytes)}`);
    console.log(`release archive bytes: ${String(metadata.sizes.releaseArchiveBytes)}`);
    console.log(`embedded support bytes: ${String(metadata.sizes.embeddedSupportArchiveBytes)}`);
    console.log(`extracted support bytes: ${String(metadata.sizes.extractedSupportBytes)}`);
    console.log(`sha256: ${releaseHash}`);
    console.log(`archive: ${releaseArchive}`);
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
}

/**
 * Builds both same-architecture release files with one shared application bundle.
 *
 * @param {string[]} args command-line arguments without a target
 */
function buildReleasePair(args) {
  if (process.platform !== "darwin") {
    throw new Error("the two-target build must run on macOS; pass --target for a native build");
  }
  if (args.includes("--executable") || args.includes("--builder") || args.includes("--shell-artifact")) {
    throw new Error("--executable, --builder, and --shell-artifact require an explicit --target");
  }

  const temporaryRoot = mkdtempSync(join(tmpdir(), "ideactl-release-pair-"));
  const shellRoot = join(temporaryRoot, "shell");
  try {
    run(process.execPath, [
      join(PACKAGE_ROOT, "scripts", "build-shell-bundle.mjs"),
      "--output",
      shellRoot,
      "--no-archive",
    ]);
    for (const target of releaseTargets()) {
      run(process.execPath, [
        fileURLToPath(import.meta.url),
        ...args,
        "--target",
        target,
        "--shell-artifact",
        shellRoot,
      ]);
    }
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
}

const buildArguments = process.argv.slice(2);
if (buildArguments.includes("--target")) {
  main();
} else {
  buildReleasePair(buildArguments);
}
