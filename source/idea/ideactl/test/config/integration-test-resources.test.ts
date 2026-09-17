import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { load } from "js-yaml";

/**
 * Checks that the shipped integration-test data remains valid YAML with the
 * top-level structure consumed by the administrator integration-test suites.
 */
function assertIntegrationTestData(
  filename: string,
  requiredRootKey: string,
  caseListKey: string,
): void {
  const filePath = fileURLToPath(
    new URL(
      `../../resources/integration_tests/${filename}`,
      import.meta.url,
    ),
  );
  const document = load(readFileSync(filePath, "utf8"));

  assert.ok(isRecord(document), `${filename} must contain a mapping`);
  assert.ok(requiredRootKey in document, `${filename} is missing ${requiredRootKey}`);
  assert.ok(caseListKey in document, `${filename} is missing ${caseListKey}`);
  assert.ok(
    Array.isArray(document[caseListKey]),
    `${filename}.${caseListKey} must be a list`,
  );
  assert.ok(
    document[caseListKey].length > 0,
    `${filename}.${caseListKey} must not be empty`,
  );

  for (const testCase of document[caseListKey]) {
    assert.ok(isRecord(testCase), `${filename} contains a non-mapping test case`);
    const name = testCase.name;
    assert.ok(typeof name === "string", `${filename} test case lacks a name`);
    assert.ok(name.length > 0, `${filename} test case name must not be empty`);
  }
}

/**
 * Narrows a parsed YAML value to a string-keyed mapping.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

test("scheduler integration-test cases parse", () => {
  assertIntegrationTestData("job_test_cases.yml", "configuration", "test_cases");
});

test("virtual desktop integration-test cases parse", () => {
  assertIntegrationTestData("session_test_cases.yml", "testcases", "testcases");
});
