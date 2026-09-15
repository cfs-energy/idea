/**
 * Assumptions the host unpack scripts make about the archive, checked against
 * what the builder actually emits and against the user-data text.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { buildBootstrapUserData } from "../../src/cdk/userdata.ts";
import { buildComponent, unpackLikeLinux, unpackLikeWindows, withWorkdir } from "./bootstrap-helpers.ts";

const LINUX_URI = "s3://sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz";
const WINDOWS_URI = "s3://sample-bucket/idea/bootstrap/bootstrap-vdc-windows-deployment.tar.gz";

test("Linux user data unpacks with tar -xvf -C into a directory named from %.tar.gz*", () => {
  const userdata = buildBootstrapUserData({
    baseOs: "amazonlinux2023",
    awsRegion: "us-east-2",
    bootstrapPackageUri: LINUX_URI,
    installCommands: ["/bin/bash scheduler/setup.sh"],
  });
  assert.match(userdata, /PACKAGE_NAME=\\\$\{!PACKAGE_ARCHIVE%\.tar\.gz\*\}/);
  assert.match(userdata, /tar -xvf \/root\/bootstrap\/\\\$\{!PACKAGE_ARCHIVE\} -C \\\$\{!PACKAGE_DIR\}/);
  assert.match(userdata, /rm \/root\/bootstrap\/latest/);
  assert.equal(userdata.includes("set -e\n"), false);
  assert.match(userdata, /^set -x$/m);
  assert.match(userdata, /source \/root\/bootstrap\/proxy\.cfg/);
  assert.match(userdata, /\/root\/bootstrap\/infra\.cfg/);
  assert.match(userdata, /cd \/root\/bootstrap\/latest/);
  assert.match(userdata, /\/bin\/bash scheduler\/setup\.sh/);
});

test("Linux non-substitution user data does not write infra.cfg", () => {
  const userdata = buildBootstrapUserData({
    baseOs: "amazonlinux2023",
    awsRegion: "us-east-2",
    bootstrapPackageUri: LINUX_URI,
    installCommands: ["/bin/bash virtual-desktop-host-linux/setup.sh"],
    substitutionSupport: false,
  });
  assert.equal(userdata.includes("/root/bootstrap/infra.cfg"), false);
  assert.match(userdata, /tar -xvf \/root\/bootstrap\/\\\$\{PACKAGE_ARCHIVE\} -C \\\$\{PACKAGE_DIR\}/);
});

test("Windows user data extracts in place with Tar -xf and strips only one extension", () => {
  const userdata = buildBootstrapUserData({
    baseOs: "windows2022",
    awsRegion: "us-east-2",
    bootstrapPackageUri: WINDOWS_URI,
    installCommands: [
      "cd \"virtual-desktop-host-windows\"",
      "Import-Module .\\Install.ps1",
      "Install-WindowsEC2Instance -ConfigureForEVDI",
    ],
  });
  assert.match(userdata, /Tar -xf "\$BootstrapDir\\\$PackageArchive"/);
  assert.equal(userdata.includes(" -C "), false);
  assert.match(userdata, /GetFileNameWithoutExtension/);
  assert.match(userdata, /Copy-S3Object[\s\S]*-Force/);
  assert.match(userdata, /cd "virtual-desktop-host-windows"/);
});

test("GetFileNameWithoutExtension of a .tar.gz URI is not the Linux package directory name", () => {
  const leaf = WINDOWS_URI.split("/").pop() ?? "";
  const windowsName = leaf.includes(".") ? leaf.slice(0, leaf.lastIndexOf(".")) : leaf;
  const linuxName = leaf.replace(/\.tar\.gz.*/, "");
  assert.equal(windowsName, "bootstrap-vdc-windows-deployment.tar");
  assert.equal(linuxName, "bootstrap-vdc-windows-deployment");
});

test("the Linux consumer layout matches a scheduler package the builder emits", () => {
  withWorkdir((workDirectory) => {
    const archiveFile = buildComponent("scheduler", workDirectory);
    const extracted = unpackLikeLinux(archiveFile, join(workDirectory, "linux"));
    assert.equal(existsSync(join(extracted, "scheduler/setup.sh")), true);
    assert.equal(existsSync(join(extracted, "common/bootstrap_common.sh")), true);
    assert.equal(existsSync(join(extracted, "scheduler/install_app.sh")), true);
    const wrapping = extracted.split("/").pop();
    assert.equal(wrapping, "bootstrap-scheduler-hard");
    assert.equal(existsSync(join(extracted, "bootstrap-scheduler-hard")), false);
  });
});

test("the Windows consumer layout matches a desktop-host package the builder emits", () => {
  withWorkdir((workDirectory) => {
    const archiveFile = buildComponent("virtual-desktop-host-windows", workDirectory);
    const extracted = unpackLikeWindows(archiveFile, join(workDirectory, "win"));
    assert.equal(existsSync(join(extracted, "virtual-desktop-host-windows/Install.ps1")), true);
    assert.equal(existsSync(join(extracted, "virtual-desktop-host-windows/Configure.ps1")), true);
    assert.equal(existsSync(join(extracted, "common")), false);
  });
});

test("broker and gateway install scripts source infra.cfg which only substitution user data writes", () => {
  withWorkdir((workDirectory) => {
    const broker = unpackLikeLinux(buildComponent("dcv-broker", workDirectory), join(workDirectory, "broker"));
    const gateway = unpackLikeLinux(
      buildComponent("dcv-connection-gateway", join(workDirectory, "gw-build")),
      join(workDirectory, "gateway"),
    );
    assert.match(readFileSync(join(broker, "dcv-broker/install_app.sh"), "utf8"), /source \/root\/bootstrap\/infra\.cfg/);
    assert.match(
      readFileSync(join(gateway, "dcv-connection-gateway/install_app.sh"), "utf8"),
      /source \/root\/bootstrap\/infra\.cfg/,
    );
  });
});

test("download_bootstrap.sh does not checksum the bootstrap archive before tar", () => {
  const userdata = buildBootstrapUserData({
    baseOs: "amazonlinux2023",
    awsRegion: "us-east-2",
    bootstrapPackageUri: LINUX_URI,
    installCommands: ["/bin/bash scheduler/setup.sh"],
  });
  assert.equal(userdata.includes("sha256sum"), false);
  assert.equal(/s3 cp[\s\S]*\|\|/.test(userdata), false);
});

test("a wrapping top-level folder would break the Linux install command, and the builder does not emit one", () => {
  withWorkdir((workDirectory) => {
    const extracted = unpackLikeLinux(
      buildComponent("bastion-host", workDirectory),
      join(workDirectory, "linux"),
    );
    assert.equal(existsSync(join(extracted, "bastion-host/setup.sh")), true);
    assert.equal(existsSync(join(extracted, "bastion-host/bastion-host/setup.sh")), false);
  });
});

test("Linux install commands invoke bash by path, so a rendered setup.sh need not be executable", () => {
  withWorkdir((workDirectory) => {
    const extracted = unpackLikeLinux(buildComponent("scheduler", workDirectory), join(workDirectory, "linux"));
    assert.equal((statSync(join(extracted, "scheduler/setup.sh")).mode & 0o111) !== 0, false);
    const userdata = buildBootstrapUserData({
      baseOs: "amazonlinux2023",
      awsRegion: "us-east-2",
      bootstrapPackageUri: LINUX_URI,
      installCommands: ["/bin/bash scheduler/setup.sh"],
    });
    assert.match(userdata, /\/bin\/bash scheduler\/setup\.sh/);
    assert.equal(userdata.includes("./scheduler/setup.sh"), false);
  });
});

test("download_bootstrap.sh rm of latest is unguarded and that script has no set -e", () => {
  const userdata = buildBootstrapUserData({
    baseOs: "amazonlinux2023",
    awsRegion: "us-east-2",
    bootstrapPackageUri: LINUX_URI,
    installCommands: ["/bin/bash scheduler/setup.sh"],
  });
  assert.match(userdata, /rm \/root\/bootstrap\/latest/);
  assert.match(userdata, /ln -sf/);
  assert.equal(userdata.includes("rm -f /root/bootstrap/latest"), false);
});

test("Windows VDI user data uses substitutionSupport false, so it never writes infra.cfg", () => {
  const userdata = buildBootstrapUserData({
    baseOs: "windows2022",
    awsRegion: "us-east-2",
    bootstrapPackageUri: WINDOWS_URI,
    installCommands: [
      "cd \"virtual-desktop-host-windows\"",
      "Import-Module .\\Install.ps1",
      "Install-WindowsEC2Instance -ConfigureForEVDI",
    ],
    substitutionSupport: false,
  });
  assert.equal(userdata.includes("infra.cfg"), false);
});

test("Install.ps1 imports Configure.ps1 from the same extracted component directory", () => {
  withWorkdir((workDirectory) => {
    const extracted = unpackLikeWindows(
      buildComponent("virtual-desktop-host-windows", workDirectory),
      join(workDirectory, "win"),
    );
    const install = readFileSync(join(extracted, "virtual-desktop-host-windows/Install.ps1"), "utf8");
    assert.match(install, /Import-Module \.\\Configure\.ps1/);
    assert.equal(existsSync(join(extracted, "virtual-desktop-host-windows/Configure.ps1")), true);
    assert.equal(existsSync(join(extracted, "virtual-desktop-host-windows/ConfigureDCVHost.ps1")), true);
  });
});
