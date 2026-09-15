/**
 * Render, unpack, and inspect every component directory the builder can produce.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import {
  BOOTSTRAP_SOURCE,
  buildComponent,
  componentCases,
  isExecutable,
  leftoverJinja,
  listComponents,
  listFiles,
  packageScriptRefs,
  unpackLikeLinux,
  unpackLikeWindows,
  withWorkdir,
} from "./bootstrap-helpers.ts";

const cases = componentCases();

const ALL_COMPONENTS = [
  "bastion-host",
  "cluster-manager",
  "common",
  "compute-node",
  "compute-node-ami-builder",
  "dcv-broker",
  "dcv-connection-gateway",
  "dcv-host-ami-builder",
  "openldap-server",
  "scheduler",
  "virtual-desktop-controller",
  "virtual-desktop-host-linux",
  "virtual-desktop-host-windows",
];

test("the builder source tree has every component directory", () => {
  assert.deepEqual(listComponents(), ALL_COMPONENTS);
  assert.equal(cases.length, ALL_COMPONENTS.length);
});

for (const component of cases) {
  test(`package ${component.name} unpacks to a coherent tree`, () => {
    withWorkdir((workDirectory) => {
      const archiveFile = buildComponent(component.name, workDirectory);
      const archiveBytes = readFileSync(archiveFile);
      assert.ok(archiveBytes.length > 24, "archive is not an empty gzip");
      assert.equal(archiveBytes[0], 0x1f);
      assert.equal(archiveBytes[1], 0x8b);

      const extracted = component.baseOs.includes("windows")
        ? unpackLikeWindows(archiveFile, join(workDirectory, "win"))
        : unpackLikeLinux(archiveFile, join(workDirectory, "linux"));
      const entry = join(extracted, component.entryRelative);
      assert.equal(existsSync(entry), true, `missing entry ${component.entryRelative}`);
      assert.ok(statSync(entry).size > 0, `empty entry ${component.entryRelative}`);

      const files = listFiles(extracted);
      assert.equal(
        files.some((name) => name.includes("_templates/")),
        false,
        "_templates must be inlined, not shipped",
      );
      assert.equal(
        files.some((name) => name.endsWith(".jinja2")),
        false,
        "jinja2 suffixes must be stripped",
      );

      if (!component.baseOs.includes("windows") && component.name !== "common") {
        assert.equal(
          existsSync(join(extracted, "common/bootstrap_common.sh")),
          true,
          "Linux packages include common/bootstrap_common.sh",
        );
      }
      if (component.baseOs.includes("windows")) {
        assert.equal(existsSync(join(extracted, "common")), false, "Windows packages omit common");
      }

      for (const relative of files) {
        const content = readFileSync(join(extracted, relative), "utf8");
        const leftover = leftoverJinja(content);
        assert.deepEqual(leftover, [], `${relative} still contains template markup: ${leftover.join(" | ")}`);
        if (relative.endsWith(".sh") || relative.endsWith(".ps1")) {
          assert.ok(content.trim().length > 0, `${relative} is empty`);
        }
      }

      const entryContent = readFileSync(entry, "utf8");
      for (const ref of packageScriptRefs(component.name, entryContent)) {
        assert.equal(existsSync(join(extracted, ref)), true, `${component.name} references missing ${ref}`);
      }
      for (const relative of files.filter((name) => name.startsWith(`${component.name}/`) && name.endsWith(".sh"))) {
        const content = readFileSync(join(extracted, relative), "utf8");
        for (const ref of packageScriptRefs(component.name, content)) {
          assert.equal(existsSync(join(extracted, ref)), true, `${relative} references missing ${ref}`);
        }
      }

      const executeBit = isExecutable(entry);
      if (component.name === "common" || component.baseOs.includes("windows")) {
        // Copied files keep the source mode. Rendered jinja2 files take the umask.
        assert.equal(executeBit, isExecutable(join(BOOTSTRAP_SOURCE, component.entryRelative)));
      } else {
        assert.equal(
          executeBit,
          false,
          `${component.entryRelative} is rendered without chmod, matching the reference builder`,
        );
      }

      if (component.installCommand.startsWith("/bin/bash ")) {
        const relative = component.installCommand.slice("/bin/bash ".length);
        assert.equal(existsSync(join(extracted, relative)), true, `install command missing ${relative}`);
      }
      if (component.installCommand.startsWith("cd ")) {
        assert.equal(existsSync(join(extracted, component.name, "Install.ps1")), true);
        assert.equal(existsSync(join(extracted, component.name, "Configure.ps1")), true);
        assert.equal(existsSync(join(extracted, component.name, "ConfigureDCVHost.ps1")), true);
      }
    });
  });
}
