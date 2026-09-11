import assert from "node:assert/strict";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  BootstrapPackageBuilder,
  BootstrapPackageError,
  type BootstrapPackageUploadClient,
  bootstrapContextParameterArgs,
  bootstrapPackageBasenames,
  bootstrapPackagePlans,
  bootstrapPackageUri,
  buildAndUploadBootstrapPackage,
  releasePackageUri,
  releasePackageNames,
  uploadBootstrapPackage,
  uploadReleasePackage,
} from "../../src/cli/bootstrap-package.ts";
import { BOOTSTRAP_SOURCE, templateContext } from "./oracle-context.ts";
import { requireFixtures } from "../support/fixtures.ts";
import { readRawTarArchive, readTarArchive } from "./tar.ts";

requireFixtures(
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

test(
  "builds the exact rendered archive layout from the real bootstrap tree",
  {},
  () => {
    const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-package-test-"));
    try {
      const archiveFile = new BootstrapPackageBuilder({
        sourceDirectory: BOOTSTRAP_SOURCE,
        targetPackageBasename: "bootstrap-vdc-dcv-connection-gateway-deployment",
        components: ["dcv-connection-gateway"],
        context: templateContext(),
        tmpDir: workDirectory,
        baseOs: "amazonlinux2023",
      }).build();
      const archive = readTarContents(archiveFile);

      assert.deepEqual([...archive.keys()], [
        "./",
        "./common/",
        "./common/bootstrap_common.sh",
        "./dcv-connection-gateway/",
        "./dcv-connection-gateway/install_app.sh",
        "./dcv-connection-gateway/setup.sh",
      ]);
      assert.match(
        archive.get("./dcv-connection-gateway/setup.sh") ?? "",
        /AWS_DEFAULT_REGION=us-east-2[\s\S]*IDEA_CLUSTER_NAME=sample-cluster/,
      );
      assert.match(
        archive.get("./dcv-connection-gateway/install_app.sh") ?? "",
        /INTERNAL_ALB_ENDPOINT="https:\/\/example\.invalid"[\s\S]*GATEWAY_TO_BROKER_PORT="8445"/,
      );
      assert.match(
        archive.get("./dcv-connection-gateway/install_app.sh") ?? "",
        /url = \\"\$\{INTERNAL_ALB_ENDPOINT\}:\$\{GATEWAY_TO_BROKER_PORT\}\\"/,
      );
      assert.equal(
        archive.get("./dcv-connection-gateway/setup.sh")?.includes(".jinja2"),
        false,
      );
    } finally {
      rmSync(workDirectory, { recursive: true, force: true });
    }
  },
);

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
  ["cluster-manager", ["cluster-manager"], { app_package_uri: "s3://sample-bucket/release.tar.gz" }],
  ["scheduler", ["scheduler"], { app_package_uri: "s3://sample-bucket/release.tar.gz" }],
  ["bastion-host", ["bastion-host"], {}],
  [
    "virtual-desktop-controller",
    ["virtual-desktop-controller"],
    { controller_package_uri: "s3://sample-bucket/release.tar.gz" },
  ],
  ["dcv-broker", ["dcv-broker"], {}],
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
    controller: "bootstrap-vdc-controller-deployment",
    dcvBroker: "bootstrap-vdc-dcv-broker-deployment",
    dcvConnectionGateway: "bootstrap-vdc-dcv-connection-gateway-deployment",
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
  assert.deepEqual(
    bootstrapPackagePlans("virtual-desktop-controller", "vdc", "deployment"),
    [
      {
        basename: "bootstrap-vdc-controller-deployment",
        components: ["virtual-desktop-controller"],
        contextParameter: "controller_bootstrap_package_uri",
      },
      {
        basename: "bootstrap-vdc-dcv-broker-deployment",
        components: ["dcv-broker"],
        contextParameter: "dcv_broker_bootstrap_package_uri",
      },
      {
        basename: "bootstrap-vdc-dcv-connection-gateway-deployment",
        components: ["dcv-connection-gateway"],
        contextParameter: "dcv_connection_gateway_package_uri",
      },
    ],
  );
  assert.deepEqual(releasePackageNames("virtual-desktop-controller", "26.09.0"), [
    "idea-virtual-desktop-controller-26.09.0.tar.gz",
    "idea-dcv-connection-gateway-26.09.0.tar.gz",
  ]);
  assert.equal(
    bootstrapPackageUri("sample-bucket", "/tmp/bootstrap-vdc-deployment.tar.gz"),
    "s3://sample-bucket/idea/bootstrap/bootstrap-vdc-deployment.tar.gz",
  );
  assert.equal(
    releasePackageUri("sample-bucket", "idea-scheduler-26.09.0.tar.gz"),
    "s3://sample-bucket/idea/releases/idea-scheduler-26.09.0.tar.gz",
  );
  assert.deepEqual(
    bootstrapContextParameterArgs({
      bootstrapPackageUri: "s3://sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz",
    }),
    ["-c bootstrap_package_uri=s3://sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz"],
  );
  assert.deepEqual(
    bootstrapContextParameterArgs({
      controllerBootstrapPackageUri: "s3://sample-bucket/idea/bootstrap/bootstrap-vdc-controller-deployment.tar.gz",
      dcvBrokerBootstrapPackageUri: "s3://sample-bucket/idea/bootstrap/bootstrap-vdc-dcv-broker-deployment.tar.gz",
      dcvConnectionGatewayPackageUri:
        "s3://sample-bucket/idea/bootstrap/bootstrap-vdc-dcv-connection-gateway-deployment.tar.gz",
    }),
    [
      "-c controller_bootstrap_package_uri=s3://sample-bucket/idea/bootstrap/bootstrap-vdc-controller-deployment.tar.gz",
      "-c dcv_broker_bootstrap_package_uri=s3://sample-bucket/idea/bootstrap/bootstrap-vdc-dcv-broker-deployment.tar.gz",
      "-c dcv_connection_gateway_package_uri=s3://sample-bucket/idea/bootstrap/bootstrap-vdc-dcv-connection-gateway-deployment.tar.gz",
    ],
  );
});

test("uploads bootstrap and release packages through an injected S3 client", async () => {
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
    assert.equal(
      await uploadReleasePackage({
        client,
        clusterS3Bucket: "sample-bucket",
        packageDistDir: workDirectory,
        packageName: releaseName,
      }),
      "s3://sample-bucket/idea/releases/idea-scheduler-26.09.0.tar.gz",
    );
    assert.deepEqual(client.commands, [
      {
        bucket: "sample-bucket",
        key: "idea/bootstrap/bootstrap-scheduler-deployment.tar.gz",
        body: Buffer.from("bootstrap-data"),
      },
      {
        bucket: "sample-bucket",
        key: `idea/releases/${releaseName}`,
        body: Buffer.from("release-data"),
      },
    ]);

    const unuploaded = await buildAndUploadBootstrapPackage({
      sourceDirectory: BOOTSTRAP_SOURCE,
      targetPackageBasename: "bootstrap-local-deployment",
      components: ["dcv-connection-gateway"],
      context: templateContext(),
      tmpDir: workDirectory,
      baseOs: "amazonlinux2023",
      client,
      clusterS3Bucket: "sample-bucket",
      upload: false,
    });
    assert.equal(unuploaded, undefined);
    assert.equal(client.commands.length, 2);
    assert.equal(
      client.commands[0]?.key,
      "idea/bootstrap/bootstrap-scheduler-deployment.tar.gz",
    );
  } finally {
    rmSync(workDirectory, { recursive: true, force: true });
  }
});
