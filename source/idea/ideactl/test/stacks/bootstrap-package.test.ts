import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import test from "node:test";

import {
  BootstrapPackageBuilder,
  BootstrapPackageError,
  type BootstrapPackageUploadClient,
  bootstrapPackageBasenames,
  bootstrapPackagePlans,
  bootstrapPackageUri,
  buildAndUploadBootstrapPackage,
  uploadBootstrapPackage
} from "../../src/cli/bootstrap-package.ts";
import { requireCapture } from "../support/fixtures.ts";
import { BOOTSTRAP_SOURCE, templateContext } from "./bootstrap-oracle-context.ts";
import { readRawTarArchive, readTarArchive } from "./tar.ts";

requireCapture(
  [BOOTSTRAP_SOURCE],
  "Restore the bootstrap source tree before running bootstrap package tests",
);

function readTarContents(archiveFile: string): Map<string, string> {
  const entries = new Map<string, string>();
  for (const entry of readTarArchive(archiveFile)) {
    entries.set(entry.name, entry.content.toString("utf8"));
  }
  return entries;
}

class FakeS3Client implements BootstrapPackageUploadClient {
  readonly commands: Array<{ bucket?: string; key?: string; body?: Uint8Array }> = [];

  async send(command: { input: { Bucket?: string; Key?: string; Body?: unknown } }): Promise<unknown> {
    const body = command.input.Body;
    this.commands.push({
      bucket: command.input.Bucket,
      key: command.input.Key,
      body: body instanceof Uint8Array ? body : undefined,
    });
    return {};
  }
}

test("renders join_activedirectory.jinja2 unknown escape through the shared environment", () => {
  const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-shared-jinja-test-"));
  const sourceDirectory = join(workDirectory, "source");
  const componentDirectory = join(sourceDirectory, "common");
  try {
    mkdirSync(componentDirectory, { recursive: true });
    // Use the real bootstrap template that requires Python's unknown-escape behavior.
    writeFileSync(
      join(componentDirectory, "setup.sh.jinja2"),
      readFileSync(join(BOOTSTRAP_SOURCE, "_templates", "linux", "join_activedirectory.jinja2"), "utf8"),
      "utf8",
    );
    const archiveFile = new BootstrapPackageBuilder({
      sourceDirectory,
      targetPackageBasename: "bootstrap-shared-jinja",
      components: ["common"],
      context: {
        base_os: "amazonlinux2023",
        config: {
          get_string(): string {
            return "IDEA Admins";
          },
          get_bool(): boolean {
            return false;
          },
        },
      },
      tmpDir: workDirectory,
    }).build();

    assert.match(
      readTarContents(archiveFile).get("./common/setup.sh") ?? "",
      /AD_SUDOERS_GROUP_NAME_ESCAPED="IDEA\\ Admins"/,
    );
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
});

for (const [name, components, variables] of [
  ["directoryservice", ["openldap-server"], {}],
] as Array<[string, string[], Record<string, string>]>) {
  test(`renders the ${name} deployment component`, () => {
    const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-package-component-test-"));
    try {
      const context = templateContext() as Record<string, unknown>;
      context.vars = { ...(context.vars as Record<string, string>), ...variables };
      const archiveFile = new BootstrapPackageBuilder({
        sourceDirectory: BOOTSTRAP_SOURCE,
        targetPackageBasename: `bootstrap-${name}-deployment`,
        components,
        context,
        tmpDir: workDirectory,
        baseOs: "amazonlinux2023",
      }).build();
      const archive = readTarContents(archiveFile);
      const component = components.at(-1);
      assert.ok(component !== undefined);
      assert.equal(archive.has(`./${component}/`), true);
      const setup = archive.get(`./${component}/setup.sh`);
      assert.ok(setup !== undefined && setup.length > 0, `${name}: setup.sh missing or empty`);
      assert.match(setup, /AWS_DEFAULT_REGION=us-east-2/);
      assert.match(setup, /IDEA_CLUSTER_NAME=sample-cluster/);
      for (const uri of Object.values(variables)) {
        assert.match(setup, new RegExp(uri.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
      }
    } finally {
      rmSync(workDirectory, { recursive: true, force: true });
    }
  });
}

test("preserves the source mode of a copied bootstrap file", () => {
  const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-package-mode-test-"));
  const sourceDirectory = join(workDirectory, "source");
  const componentDirectory = join(sourceDirectory, "common");
  try {
    mkdirSync(componentDirectory, { recursive: true });
    const sourceFile = join(componentDirectory, "copied.sh");
    writeFileSync(sourceFile, "echo copied\n", "utf8");
    chmodSync(sourceFile, 0o755);
    const archiveFile = new BootstrapPackageBuilder({
      sourceDirectory,
      targetPackageBasename: "bootstrap-mode",
      components: ["common"],
      context: {},
      tmpDir: workDirectory,
    }).build();
    const copied = readTarArchive(archiveFile).find((entry) => entry.name === "./common/copied.sh");
    assert.ok(copied !== undefined, "copied.sh missing from archive");
    assert.equal(copied.mode, 0o755);
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
});

test("re-archives an existing rendered directory without rendering it", () => {
  const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-package-cache-test-"));
  const sourceDirectory = join(workDirectory, "source");
  const targetDirectory = join(workDirectory, "bootstrap-cache");
  try {
    mkdirSync(join(sourceDirectory, "common"), { recursive: true });
    writeFileSync(join(sourceDirectory, "common", "setup.sh.jinja2"), "first={{ context.value }}\n", {
      encoding: "utf8",
      flush: true,
    });
    const first = new BootstrapPackageBuilder({
      sourceDirectory,
      targetPackageBasename: "bootstrap-cache",
      components: ["common"],
      context: { value: "rendered" },
      tmpDir: workDirectory,
    }).build();
    writeFileSync(join(targetDirectory, "common", "setup.sh"), "customized\n", "utf8");
    const second = new BootstrapPackageBuilder({
      sourceDirectory,
      targetPackageBasename: "bootstrap-cache",
      components: ["common"],
      context: { value: "ignored" },
      tmpDir: workDirectory,
    }).build();

    assert.equal(first, second);
    assert.equal(readTarContents(second).get("./common/setup.sh"), "customized\n");
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
});

test("writes PAX headers for long paths that the bootstrap extractor can unpack", () => {
  const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-package-pax-test-"));
  const sourceDirectory = join(workDirectory, "source");
  const targetDirectory = join(workDirectory, "bootstrap-pax");
  const prefixSegments = ["a".repeat(30), "b".repeat(30), "c".repeat(30)];
  const extendedSegments = Array.from({ length: 9 }, () => "d".repeat(30));
  const prefixFile = join(targetDirectory, ...prefixSegments, "file.txt");
  const extendedFile = join(targetDirectory, ...extendedSegments, "file.txt");

  try {
    mkdirSync(join(sourceDirectory, "common"), { recursive: true });
    mkdirSync(join(targetDirectory, ...prefixSegments), { recursive: true });
    mkdirSync(join(targetDirectory, ...extendedSegments), { recursive: true });
    writeFileSync(prefixFile, "prefix payload\n", "utf8");
    writeFileSync(extendedFile, "extended payload\n", "utf8");

    const directories = [targetDirectory];
    for (const segments of [prefixSegments, extendedSegments]) {
      let directory = targetDirectory;
      for (const segment of segments) {
        directory = join(directory, segment);
        directories.push(directory);
      }
    }
    for (const directory of directories) utimesSync(directory, 0, 0);
    utimesSync(prefixFile, 0, 0);
    utimesSync(extendedFile, 0, 0);

    const archiveFile = new BootstrapPackageBuilder({
      sourceDirectory,
      targetPackageBasename: "bootstrap-pax",
      components: ["common"],
      context: {},
      tmpDir: workDirectory,
    }).build();
    const prefixArchivePath = `./${prefixSegments.join("/")}/file.txt`;
    const extendedArchivePath = `./${extendedSegments.join("/")}/file.txt`;
    const rawEntries = readRawTarArchive(archiveFile);
    const paxHeaders = rawEntries.filter((entry) => entry.type === "x");
    const paxPayloads = paxHeaders.map((entry) => entry.content.toString("utf8"));

    assert.ok(paxHeaders.every((entry) => entry.rawName === "././@PaxHeader" && entry.mode === 0));
    assert.ok(paxPayloads.includes("13 mtime=0.0\n"));
    assert.ok(paxPayloads.some((payload) => payload.endsWith(`path=${prefixArchivePath}\n13 mtime=0.0\n`)));
    assert.ok(paxPayloads.some((payload) => payload.endsWith(`path=${extendedArchivePath}\n13 mtime=0.0\n`)));
    assert.equal(readTarContents(archiveFile).get(prefixArchivePath), "prefix payload\n");
    assert.equal(readTarContents(archiveFile).get(extendedArchivePath), "extended payload\n");

    const extractionDirectory = join(workDirectory, "extracted");
    mkdirSync(extractionDirectory);
    execFileSync("tar", ["-xvf", archiveFile, "-C", extractionDirectory], { stdio: "pipe" });
    assert.equal(readFileSync(join(extractionDirectory, ...prefixSegments, "file.txt"), "utf8"), "prefix payload\n");
    assert.equal(readFileSync(join(extractionDirectory, ...extendedSegments, "file.txt"), "utf8"), "extended payload\n");
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
});

test("requires at least one requested component", () => {
  assert.throws(
    () =>
      new BootstrapPackageBuilder({
        sourceDirectory: "/tmp",
        targetPackageBasename: "bootstrap",
        components: [],
        context: {},
      }),
    BootstrapPackageError,
  );
});

test("uses the expected bootstrap names, URIs, and CDK context argument layout", () => {
  assert.deepEqual(bootstrapPackageBasenames("vdc", "deployment"), {
    standard: "bootstrap-vdc-deployment",
  });
  assert.deepEqual(
    bootstrapPackagePlans("directoryservice", "directoryservice", "deployment", "openldap"),
    [
      {
        basename: "bootstrap-directoryservice-deployment",
        components: ["common", "openldap-server"],
        contextParameter: "bootstrap_package_uri",
      },
    ],
  );
  assert.deepEqual(
    bootstrapPackagePlans("directoryservice", "directoryservice", "deployment"),
    [],
  );
  assert.deepEqual(
    bootstrapPackagePlans("directoryservice", "directoryservice", "deployment", "activedirectory"),
    [],
  );
  assert.deepEqual(bootstrapPackagePlans("virtual-desktop-controller", "vdc", "deployment"), []);
  assert.equal(
    bootstrapPackageUri("sample-bucket", "/tmp/bootstrap-vdc-deployment.tar.gz"),
    "s3://sample-bucket/idea/bootstrap/bootstrap-vdc-deployment.tar.gz",
  );
});

test("uploads bootstrap packages through an injected S3 client", async () => {
  const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-package-upload-test-"));
  const archiveFile = join(workDirectory, "bootstrap-scheduler-deployment.tar.gz");
  const releaseName = "idea-scheduler-26.09.0.tar.gz";
  const releaseFile = join(workDirectory, releaseName);
  try {
    writeFileSync(archiveFile, "bootstrap-data", "utf8");
    writeFileSync(releaseFile, "release-data", "utf8");
    const client = new FakeS3Client();

    assert.equal(
      await uploadBootstrapPackage({ client, clusterS3Bucket: "sample-bucket", archiveFile }),
      "s3://sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz",
    );
    assert.deepEqual(client.commands, [
      {
        bucket: "sample-bucket",
        key: "idea/bootstrap/bootstrap-scheduler-deployment.tar.gz",
        body: Buffer.from("bootstrap-data"),
      },

    ]);

    const unuploaded = await buildAndUploadBootstrapPackage({
      sourceDirectory: BOOTSTRAP_SOURCE,
      targetPackageBasename: "bootstrap-local-deployment",
      components: ["openldap-server"],
      context: templateContext(),
      tmpDir: workDirectory,
      baseOs: "amazonlinux2023",
      client,
      clusterS3Bucket: "sample-bucket",
      upload: false,
    });
    assert.equal(unuploaded, undefined);
    assert.equal(client.commands.length, 1);
    assert.equal(
      client.commands[0]?.key,
      "idea/bootstrap/bootstrap-scheduler-deployment.tar.gz",
    );
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
});

test("an archive named by content keeps its name while the rendered tree is unchanged", () => {
  const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-content-name-test-"));
  const sourceDirectory = join(workDirectory, "source");
  const componentDirectory = join(sourceDirectory, "common");
  const build = (tmpDir: string): string =>
    new BootstrapPackageBuilder({
      sourceDirectory,
      targetPackageBasename: "bootstrap-bastion-host-deployment",
      components: ["common"],
      context: { base_os: "amazonlinux2023" },
      tmpDir,
      nameByContent: (contentId) => `bootstrap-bastion-host-${contentId}`,
    }).build();
  try {
    mkdirSync(componentDirectory, { recursive: true });
    writeFileSync(join(componentDirectory, "setup.sh.jinja2"), "echo {{ context.base_os }}\n", "utf8");
    const first = build(join(workDirectory, "one"));
    const second = build(join(workDirectory, "two"));
    assert.equal(basename(second), basename(first));
    assert.match(basename(first), /^bootstrap-bastion-host-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tar\.gz$/);
    writeFileSync(join(componentDirectory, "setup.sh.jinja2"), "echo {{ context.base_os }} changed\n", "utf8");
    const third = build(join(workDirectory, "three"));
    assert.notEqual(basename(third), basename(first));
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
});

for (const provider of ["openldap", "activedirectory", "aws_managed_activedirectory"]) {
  test(`builds the ECS host directory package for ${provider}`, () => {
    const directory = mkdtempSync(join(tmpdir(), "ecs-bootstrap-"));
    try {
      const plans = bootstrapPackagePlans("ecs", "ecs", "deployment", provider);
      assert.deepEqual(plans, [{ basename: "bootstrap-ecs-deployment", components: ["common", "ecs-host"], contextParameter: "bootstrap_package_uri" }]);
      const context = templateContext() as Record<string, any>;
      const getString = context.config.get_string;
      context.config.get_string = (key: string, ...args: unknown[]) => key === "directoryservice.provider" ? provider : getString(key, ...args);
      const archive = readTarContents(new BootstrapPackageBuilder({ sourceDirectory: BOOTSTRAP_SOURCE, targetPackageBasename: plans[0].basename, components: plans[0].components, context, tmpDir: directory }).build());
      assert.deepEqual([...archive.keys()], ["./", "./common/", "./common/bootstrap_common.sh", "./common/terminal_host.py", "./ecs-host/", "./ecs-host/directory_join.sh"]);
      const script = archive.get("./ecs-host/directory_join.sh")!;
      assert.match(script, /source \/etc\/environment/);
      assert.match(script, /source "\$\{SCRIPT_DIR\}\/..\/common\/bootstrap_common.sh"/);
      assert.match(script, provider === "openldap" ? /ldap_id_use_start_tls = True/ : /ADAutomation.PresetComputer/);
      execFileSync("bash", ["-n"], { input: script });
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}

test("ECS without a directory provider has no bootstrap package", () => {
  assert.deepEqual(bootstrapPackagePlans("ecs", "ecs", "deployment"), []);
});
