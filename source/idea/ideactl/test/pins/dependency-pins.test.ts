/**
 * Verifies every direct package declaration is an exact resolved version.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

type StringRecord = Record<string, string>;
type JsonRecord = Record<string, unknown>;

interface PackageManifest {
  dependencies: StringRecord;
  devDependencies: StringRecord;
}

interface PackageLock {
  packages: JsonRecord;
}

interface DirectPackage {
  name: string;
  declared: string;
  lockfileDeclared: string;
  resolved: string;
}

const exactVersion = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/u;
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

/** Reads an object from JSON while validating the expected top-level shape. */
function readJsonRecord(file: string): JsonRecord {
  const value: unknown = JSON.parse(readFileSync(file, "utf8"));
  assert.ok(isJsonRecord(value), `${file} must contain a JSON object`);
  return value;
}

/** Distinguishes a JSON object from null, arrays, and scalar values. */
function isJsonRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Reads a map whose values must all be strings. */
function readStringRecord(record: JsonRecord, key: string): StringRecord {
  const value = record[key];
  assert.ok(isJsonRecord(value), `${key} must be an object`);

  const strings: StringRecord = {};
  for (const [name, version] of Object.entries(value)) {
    if (typeof version !== "string") {
      throw new TypeError(`${key}.${name} must be a string`);
    }
    strings[name] = version;
  }
  return strings;
}

/** Parses the package manifest's dependency maps. */
function readManifest(): PackageManifest {
  const record = readJsonRecord(join(packageRoot, "package.json"));
  return {
    dependencies: readStringRecord(record, "dependencies"),
    devDependencies: readStringRecord(record, "devDependencies"),
  };
}

/** Parses the lockfile package map used for installed resolutions. */
function readLockfile(): PackageLock {
  const record = readJsonRecord(join(packageRoot, "package-lock.json"));
  return { packages: readJsonRecordValue(record, "packages") };
}

/** Reads an object-valued member from another validated JSON object. */
function readJsonRecordValue(record: JsonRecord, key: string): JsonRecord {
  const value = record[key];
  assert.ok(isJsonRecord(value), `${key} must be an object`);
  return value;
}

/** Audits direct dependencies against their manifest and lockfile declarations. */
function auditDirectPackages(manifest: PackageManifest, lockfile: PackageLock): DirectPackage[] {
  const lockRoot = readJsonRecordValue(lockfile.packages, "");
  const lockDependencies = readStringRecord(lockRoot, "dependencies");
  const lockDevDependencies = readStringRecord(lockRoot, "devDependencies");
  const result: DirectPackage[] = [];

  // Check both manifest sections so development tools cannot silently retain ranges.
  for (const [manifestEntries, lockEntries] of [
    [manifest.dependencies, lockDependencies],
    [manifest.devDependencies, lockDevDependencies],
  ] as const) {
    for (const [name, declared] of Object.entries(manifestEntries)) {
      const lockfileDeclared = lockEntries[name];
      assert.equal(lockfileDeclared, declared, `${name} differs between manifest and lockfile`);

      const installed = readJsonRecordValue(lockfile.packages, `node_modules/${name}`);
      const resolved = installed["version"];
      if (typeof resolved !== "string") {
        throw new TypeError(`${name} has no resolved lockfile version`);
      }
      result.push({ name, declared, lockfileDeclared, resolved });
    }
  }
  return result;
}

/** Rejects version ranges and pins that do not reproduce the locked resolution. */
function assertExactResolvedPackages(packages: readonly DirectPackage[]): void {
  for (const dependency of packages) {
    assert.match(dependency.declared, exactVersion, `${dependency.name} is not exactly pinned`);
    assert.equal(
      dependency.declared,
      dependency.resolved,
      `${dependency.name} does not reproduce its lockfile resolution`,
    );
  }
}

test("every dependency and development dependency has an exact resolved pin", () => {
  const packages = auditDirectPackages(readManifest(), readLockfile());
  assert.ok(packages.length > 0, "the manifest must declare at least one package");
  assertExactResolvedPackages(packages);
});

test("a dependency range is rejected", () => {
  assert.throws(
    () =>
      assertExactResolvedPackages([
        {
          name: "sample-package",
          declared: "^1.2.3",
          lockfileDeclared: "^1.2.3",
          resolved: "1.2.3",
        },
      ]),
    /sample-package is not exactly pinned/u,
  );
});
