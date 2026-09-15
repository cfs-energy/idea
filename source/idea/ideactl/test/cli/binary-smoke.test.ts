/**
 * Runs the two fused executables from an empty directory when they are present.
 * Skips when `dist/release` has not been built yet.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

/**
 * Maps this process to the two release names `build:dist` writes.
 *
 * @param platform host operating system
 * @param architecture host processor
 * @returns release target name
 */
function releaseTarget(platform: string, architecture: string): string {
  const releaseArchitecture = architecture === "x64" ? "amd64" : architecture;
  return `${platform}-${releaseArchitecture}`;
}

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const releaseRoot = join(packageRoot, "dist", "release");
const valuesFile = join(packageRoot, "test", "cli", "shell-path-values.yml");
const packageVersion = (
  JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8")) as { version: string }
).version;
const hostTarget = releaseTarget(process.platform, process.arch);
const hostExecutable = join(releaseRoot, hostTarget, "ideactl");
const linuxTarget = releaseTarget("linux", process.arch);
const linuxExecutable = join(releaseRoot, linuxTarget, "ideactl");
const linuxPlatform = process.arch === "x64" ? "linux/amd64" : "linux/arm64";

/**
 * Runs one fused executable with no runtime on PATH.
 *
 * @param executable absolute path of the built file
 * @param args command arguments
 * @param smokeRoot isolated home, tmp, and work directories
 * @returns stdout
 */
function runEmptyPath(executable: string, args: string[], smokeRoot: string): string {
  const result = spawnSync(executable, args, {
    cwd: join(smokeRoot, "work"),
    encoding: "utf8",
    env: {
      HOME: join(smokeRoot, "home"),
      IDEA_USER_HOME: join(smokeRoot, "home", ".idea"),
      LANG: "en_US.UTF-8",
      NODE_PATH: "",
      PATH: join(smokeRoot, "empty-path"),
      TMPDIR: join(smokeRoot, "tmp"),
    },
  });
  assert.equal(
    result.status,
    0,
    [`${executable} ${args.join(" ")} failed`, result.stdout, result.stderr].join("\n"),
  );
  return result.stdout;
}

/**
 * Builds the Linux file into a local Amazon Linux image.
 *
 * @returns image tag
 */
function buildLinuxImage(): string {
  const image = "ideactl-linux:local";
  const dockerfile = join(packageRoot, "scripts", "ideactl-linux.Dockerfile");
  const result = spawnSync(
    "docker",
    [
      "build",
      "--load",
      "--platform",
      linuxPlatform,
      "-t",
      image,
      "-f",
      dockerfile,
      dirname(linuxExecutable),
    ],
    { encoding: "utf8" },
  );
  assert.equal(
    result.status,
    0,
    [`linux image build failed`, result.stdout, result.stderr].join("\n"),
  );
  return image;
}

/**
 * Runs the Linux image with no runtime on PATH.
 *
 * @param image local image tag that already contains the fused file
 * @param args command arguments
 * @param smokeRoot isolated directories bind-mounted into the container
 * @returns stdout
 */
function runLinuxContainer(image: string, args: string[], smokeRoot: string): string {
  const result = spawnSync(
    "docker",
    [
      "run",
      "--rm",
      "--platform",
      linuxPlatform,
      "--volume",
      `${smokeRoot}:/smoke`,
      "--workdir",
      "/smoke/work",
      "--env",
      "HOME=/smoke/home",
      "--env",
      "IDEA_USER_HOME=/smoke/home/.idea",
      "--env",
      "LANG=en_US.UTF-8",
      "--env",
      "NODE_PATH=",
      "--env",
      "PATH=/smoke/empty-path",
      "--env",
      "TMPDIR=/smoke/tmp",
      image,
      ...args,
    ],
    { encoding: "utf8" },
  );
  assert.equal(
    result.status,
    0,
    [`linux ${args.join(" ")} failed`, result.stdout, result.stderr].join("\n"),
  );
  return result.stdout;
}

/**
 * Creates an isolated directory tree for an empty-path run.
 *
 * @returns temporary root that the caller must remove
 */
function makeSmokeRoot(): string {
  mkdirSync(join(packageRoot, "dist"), { recursive: true });
  const smokeRoot = mkdtempSync(join(packageRoot, "dist", "ideactl-binary-smoke-"));
  mkdirSync(join(smokeRoot, "config"), { recursive: true });
  mkdirSync(join(smokeRoot, "home"), { recursive: true });
  mkdirSync(join(smokeRoot, "tmp"), { recursive: true });
  mkdirSync(join(smokeRoot, "empty-path"), { recursive: true });
  mkdirSync(join(smokeRoot, "work"), { recursive: true });
  cpSync(valuesFile, join(smokeRoot, "values.yml"));
  return smokeRoot;
}

test("host artifact prints the version and generates configuration with an empty PATH", {
  skip: existsSync(hostExecutable) ? false : "host release executable is not built",
}, () => {
  const smokeRoot = makeSmokeRoot();
  try {
    const about = runEmptyPath(hostExecutable, ["about"], smokeRoot);
    const generated = runEmptyPath(
      hostExecutable,
      [
        "config",
        "generate",
        "--values-file",
        join(smokeRoot, "values.yml"),
        "--config-dir",
        join(smokeRoot, "config"),
        "--force",
      ],
      smokeRoot,
    );
    assert.match(about, new RegExp(`^ideactl ${packageVersion}$`, "m"));
    assert.match(generated, /generating config from templates/);
    assert.equal(existsSync(join(smokeRoot, "config", "config", "idea.yml")), true);
    process.stdout.write(`$ ideactl about\n${about}`);
    process.stdout.write(
      `$ ideactl config generate --values-file values.yml --config-dir config --force\n${generated}`,
    );
  } finally {
    rmSync(smokeRoot, { recursive: true, force: true });
  }
});

test("release archives exist for both same-architecture targets with checksums", {
  skip: existsSync(join(releaseRoot, `ideactl-v${packageVersion}-${hostTarget}.tar.gz`))
    ? false
    : "release archives are not built",
}, () => {
  for (const target of [hostTarget, linuxTarget]) {
    const archive = join(releaseRoot, `ideactl-v${packageVersion}-${target}.tar.gz`);
    const checksumFile = `${archive}.sha256`;
    assert.equal(existsSync(archive), true, `missing ${archive}`);
    assert.equal(existsSync(checksumFile), true, `missing ${checksumFile}`);
    const checksumText = readFileSync(checksumFile, "utf8").trim();
    assert.match(checksumText, /^[a-f0-9]{64}  /);
    process.stdout.write(`${checksumText}\n`);
  }
});

test("linux artifact prints the version and generates configuration in a container", {
  skip: existsSync(linuxExecutable) && process.platform === "darwin"
    ? false
    : "linux release executable is not built on this macOS host",
}, () => {
  const smokeRoot = makeSmokeRoot();
  try {
    const image = buildLinuxImage();
    const about = runLinuxContainer(image, ["about"], smokeRoot);
    const generated = runLinuxContainer(
      image,
      [
        "config",
        "generate",
        "--values-file",
        "/smoke/values.yml",
        "--config-dir",
        "/smoke/config",
        "--force",
      ],
      smokeRoot,
    );
    assert.match(about, new RegExp(`^ideactl ${packageVersion}$`, "m"));
    assert.equal(existsSync(join(smokeRoot, "config", "config", "idea.yml")), true);
    process.stdout.write(`$ docker run ... /ideactl about\n${about}`);
    process.stdout.write(`$ docker run ... /ideactl config generate\n${generated}`);
  } finally {
    rmSync(smokeRoot, { recursive: true, force: true });
  }
});
