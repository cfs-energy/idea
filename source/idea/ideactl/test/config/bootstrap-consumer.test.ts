/**
 * Assumptions the host unpack scripts make about the archive, checked against
 * what the builder actually emits and against the user-data text.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { buildBootstrapUserData } from "../../src/cdk/userdata.ts";
import { buildComponent, unpackLikeLinux, unpackLikeWindows, withWorkdir } from "./bootstrap-helpers.ts";

const LINUX_URI = "s3://sample-bucket/idea/bootstrap/bootstrap-scheduler-deployment.tar.gz";
const WINDOWS_URI = "s3://sample-bucket/idea/bootstrap/bootstrap-vdc-windows-deployment.tar.gz";

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

test("the Windows consumer layout matches a desktop-host package the builder emits", () => {
  withWorkdir((workDirectory) => {
    const archiveFile = buildComponent("virtual-desktop-host-windows", workDirectory);
    const extracted = unpackLikeWindows(archiveFile, join(workDirectory, "win"));
    assert.equal(existsSync(join(extracted, "virtual-desktop-host-windows/Install.ps1")), true);
    assert.equal(existsSync(join(extracted, "virtual-desktop-host-windows/Configure.ps1")), true);
    assert.equal(existsSync(join(extracted, "common")), false);
  });
});

test("a wrapping top-level folder would break the Linux install command, and the builder does not emit one", () => {
  withWorkdir((workDirectory) => {
    const extracted = unpackLikeLinux(
      buildComponent("openldap-server", workDirectory),
      join(workDirectory, "linux"),
    );
    assert.equal(existsSync(join(extracted, "openldap-server/setup.sh")), true);
    assert.equal(existsSync(join(extracted, "openldap-server/openldap-server/setup.sh")), false);
  });
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
