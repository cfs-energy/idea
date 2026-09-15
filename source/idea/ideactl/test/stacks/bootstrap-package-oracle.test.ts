/**
 * Compares bootstrap archives with a generated oracle.
 *
 * `tools/parity/fixtures/w16b-bootstrap-oracle/<basename>/` holds generated
 * output for the same source tree, components, and context. The test pins
 * rendered bytes, archive entries, ordering, and copied-file modes.
 *
 * The oracle is gitignored because it contains source-file headers. Regenerate
 * it with `oracle_build.py` when the bootstrap source changes.
 */

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { BootstrapPackageBuilder } from "../../src/cli/bootstrap-package.ts";
import { BOOTSTRAP_SOURCE, ORACLE_CASES, caseContext } from "./bootstrap-oracle-context.ts";
import { requireFixtures } from "../support/fixtures.ts";
import { readTarArchive } from "./tar.ts";

const ORACLE = join(import.meta.dirname, "..", "..", "tools", "parity", "fixtures", "w16b-bootstrap-oracle");
requireFixtures(
  [
    BOOTSTRAP_SOURCE,
    ...ORACLE_CASES.map((oracleCase) => join(ORACLE, oracleCase.basename)),
  ],
  "python3 test/stacks/oracle_build.py --out tools/parity/fixtures/w16b-bootstrap-oracle",
);

/** Returns sorted archive names rooted at `./`, with trailing slashes for directories. */
function expectedEntryNames(root: string): string[] {
  const names = ["./"];
  const visit = (directory: string, prefix: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true }).sort((left, right) =>
      left.name < right.name ? -1 : left.name > right.name ? 1 : 0,
    )) {
      const name = `${prefix}${entry.name}`;
      if (entry.isDirectory()) {
        names.push(`${name}/`);
        visit(join(directory, entry.name), `${name}/`);
      } else {
        names.push(name);
      }
    }
  };
  visit(root, "./");
  return names;
}

for (const oracleCase of ORACLE_CASES) {
  const fixture = join(ORACLE, oracleCase.basename);
  test(
    `matches the generated oracle for ${oracleCase.components.join(", ")}`,
    {},
    () => {
      const workDirectory = mkdtempSync(join(tmpdir(), "bootstrap-oracle-"));
      try {
        const archiveFile = new BootstrapPackageBuilder({
          sourceDirectory: BOOTSTRAP_SOURCE,
          targetPackageBasename: oracleCase.basename,
          components: [...oracleCase.components],
          context: caseContext(oracleCase),
          tmpDir: workDirectory,
          baseOs: "amazonlinux2023",
        }).build();

        const entries = readTarArchive(archiveFile);
        assert.deepEqual(
          entries.map((entry) => entry.name),
          expectedEntryNames(fixture),
        );

        for (const entry of entries) {
          if (entry.name.endsWith("/")) continue;
          const relativePath = entry.name.slice("./".length);
          assert.equal(
            entry.content.toString("utf8"),
            readFileSync(join(fixture, relativePath), "utf8"),
            `rendered content differs from the Python oracle: ${relativePath}`,
          );
          // Copied files keep the source mode; rendered files take the umask,
          // which is the building machine's, not the builder's.
          const sourceFile = join(BOOTSTRAP_SOURCE, relativePath);
          const oracleFile = join(fixture, relativePath);
          if (existsSync(sourceFile)) {
            assert.equal(
              entry.mode,
              statSync(oracleFile).mode & 0o7777,
              `copied-file mode differs from the oracle: ${relativePath}`,
            );
          }
          if (relativePath === "common/bootstrap_common.sh") {
            assert.equal(entry.mode, 0o644, `copied-file mode is not the pinned mode: ${relativePath}`);
          }
        }
      } finally {
        rmSync(workDirectory, { recursive: true, force: true });
      }
    },
  );
}
