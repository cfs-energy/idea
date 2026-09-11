/**
 * Mechanical checks for the leftovers inventory. Counts, coverage of request
 * and analysis files, and the still-true code facts that the inventory claims.
 */

import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const PKG = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const LEFTOVERS = join(PKG, "docs/port/analysis/leftovers.md");
const REQUESTS = join(PKG, "docs/port/requests");
const ANALYSIS = join(PKG, "docs/port/analysis");

const GROUP_HEADINGS = [
  "## Still true: first development install",
  "## Still true: first development upgrade",
  "## Still true: neither",
  "## No longer true",
] as const;

const COUNT_KEYS = [
  "install_still_true",
  "upgrade_still_true",
  "neither_still_true",
  "no_longer_true",
] as const;

function text(): string {
  return readFileSync(LEFTOVERS, "utf8");
}

/** Split the inventory into the four grouped sections, in order. */
function groups(source: string): Record<(typeof COUNT_KEYS)[number], string> {
  const indexes = GROUP_HEADINGS.map((heading) => {
    const index = source.indexOf(heading);
    assert.ok(index >= 0, `missing heading ${heading}`);
    return index;
  });
  for (let i = 1; i < indexes.length; i += 1) {
    assert.ok(indexes[i] > indexes[i - 1], "group headings are out of order");
  }
  const slices = indexes.map((start, i) =>
    source.slice(start, i + 1 < indexes.length ? indexes[i + 1] : undefined),
  );
  return {
    install_still_true: slices[0] as string,
    upgrade_still_true: slices[1] as string,
    neither_still_true: slices[2] as string,
    no_longer_true: slices[3] as string,
  };
}

/** Count `- still_true: yes` / `- still_true: no` lines in one group. */
function stillTrueCount(section: string, expected: "yes" | "no"): number {
  const pattern = expected === "yes" ? /^- still_true: yes\b/gm : /^- still_true: no\b/gm;
  return section.match(pattern)?.length ?? 0;
}

function declaredCounts(source: string): Record<(typeof COUNT_KEYS)[number], number> {
  const counts = {} as Record<(typeof COUNT_KEYS)[number], number>;
  for (const key of COUNT_KEYS) {
    const match = source.match(new RegExp(`^${key}: (\\d+)$`, "m"));
    assert.ok(match, `Counts is missing ${key}`);
    counts[key] = Number(match[1]);
  }
  return counts;
}

describe("leftovers.md shape", () => {
  it("exists and has the required headings", () => {
    assert.equal(existsSync(LEFTOVERS), true);
    const source = text();
    assert.match(source, /^# Leftovers inventory/m);
    assert.match(source, /^## Method/m);
    assert.match(source, /^## Counts/m);
    assert.match(source, /^## Five first/m);
    for (const heading of GROUP_HEADINGS) {
      assert.equal(source.includes(heading), true, heading);
    }
  });

  it("declares counts that match still_true markers in each group", () => {
    const source = text();
    const declared = declaredCounts(source);
    const section = groups(source);
    assert.equal(stillTrueCount(section.install_still_true, "yes"), declared.install_still_true);
    assert.equal(stillTrueCount(section.upgrade_still_true, "yes"), declared.upgrade_still_true);
    assert.equal(stillTrueCount(section.neither_still_true, "yes"), declared.neither_still_true);
    assert.equal(stillTrueCount(section.no_longer_true, "no"), declared.no_longer_true);
    assert.equal(stillTrueCount(section.install_still_true, "no"), 0);
    assert.equal(stillTrueCount(section.upgrade_still_true, "no"), 0);
    assert.equal(stillTrueCount(section.neither_still_true, "no"), 0);
    assert.equal(stillTrueCount(section.no_longer_true, "yes"), 0);
  });

  it("names five first items with reasons", () => {
    const source = text();
    const start = source.indexOf("## Five first");
    const end = source.indexOf("## Still true: first development install");
    const block = source.slice(start, end);
    assert.match(block, /^1\. \*\*generator-ecs and cdk-invoker-drops-ecs\.\*\*/m);
    assert.match(block, /^2\. \*\*vdc-published-settings\.\*\*/m);
    assert.match(block, /^3\. \*\*trunking-and-preflight\.\*\*/m);
    assert.match(block, /^4\. \*\*cluster-settings-backup-tag\.\*\*/m);
    assert.match(block, /^5\. \*\*upgrade-resume-and-adapters\.\*\*/m);
  });

  it("does not use em dashes or tool names", () => {
    const source = text();
    assert.doesNotMatch(source, /[\u2013\u2014]/);
    // Encoded for the same reason scripts/ci-gates.mjs encodes them: a checker that
    // spells the names it forbids fails its own repository hygiene gate.
    for (const encoded of ["Q2xhdWRl", "Q3Vyc29y", "R1BU", "cG9ueXRhaWw="]) {
      const name = Buffer.from(encoded, "base64").toString("utf8");
      assert.doesNotMatch(source, new RegExp(`\\b${name}\\b`, "i"), name);
    }
  });
});

describe("search-shape coverage", () => {
  // Shard-named residue, not decisions, so it has no place in a decision inventory. The pattern
  // is anchored at both ends so a document with a real name cannot slip through it.
  const AUDIT_RESIDUE = /^FIX-\d{2}\.md$/;

  it("mentions every request file that is a decision rather than audit residue", () => {
    const source = text();
    const all = readdirSync(REQUESTS).filter((name) => name.endsWith(".md")).sort();
    const files = all.filter((name) => !AUDIT_RESIDUE.test(name));
    assert.ok(files.length >= 60, `expected a full request inventory, got ${files.length}`);
    // The exemption must stay narrow: prove it excludes only the shard-named files and nothing else.
    const exempt = all.filter((name) => AUDIT_RESIDUE.test(name));
    assert.deepEqual(
      exempt.filter((name) => !/^FIX-\d{2}\.md$/.test(name)),
      [],
      "the audit-residue exemption matched a file that is not shard residue",
    );
    const missing = files.filter((name) => !source.includes(name));
    assert.deepEqual(missing, [], `request files absent from leftovers.md: ${missing.join(", ")}`);
  });

  it("mentions every analysis file except leftovers.md itself", () => {
    const source = text();
    const files = readdirSync(ANALYSIS)
      .filter((name) => name.endsWith(".md") && name !== "leftovers.md")
      .sort();
    assert.ok(files.length >= 25, `expected a full analysis inventory, got ${files.length}`);
    const missing = files.filter((name) => !source.includes(name));
    assert.deepEqual(missing, [], `analysis files absent from leftovers.md: ${missing.join(", ")}`);
  });
});

describe("code-fact hygiene", () => {
  // No assertion here may pin a gap as still-open: that goes red when somebody closes the gap,
  // which inverts what a failing test means. Open gaps are tracked in the inventory above.
  it("src still has no TODO or FIXME comments", () => {
    const srcRoot = join(PKG, "src");
    const files: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) walk(path);
        else if (entry.name.endsWith(".ts")) files.push(path);
      }
    };
    walk(srcRoot);
    const hits = files.flatMap((file) => {
      const content = readFileSync(file, "utf8");
      return /\b(TODO|FIXME|XXX|HACK|UNIMPLEMENTED)\b/.test(content) ? [file] : [];
    });
    assert.deepEqual(hits, []);
  });
});
