import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { App, Stack } from "aws-cdk-lib";
import { load } from "js-yaml";

import {
  buildStack,
  elbAccountIdForRegion,
  renderBootstrapStack,
  regionElbAccountIdPath,
} from "../../src/cdk/stacks/bootstrap.ts";
import { DEFAULT_STACK_REGISTRY, type StackBuildProps } from "../../src/cdk/app.ts";
import { requireCapture } from "../support/fixtures.ts";

const LIVE = fileURLToPath(new URL("../../tools/parity/live/idea-dev27-bootstrap.json", import.meta.url));
const PYTHON_RENDERED = fileURLToPath(
  new URL("../../tools/parity/fixtures/idea-dev27/python/_cdk/cdk_toolkit_stack.yml", import.meta.url),
);
const BOOTSTRAP_SOURCE = fileURLToPath(new URL("../../src/cdk/stacks/bootstrap.ts", import.meta.url));
const BOOTSTRAP_STACK_TEST = fileURLToPath(new URL("./bootstrap-stack.test.ts", import.meta.url));
const BOOTSTRAP_ARGV_TEST = fileURLToPath(new URL("./bootstrap-argv.test.ts", import.meta.url));
const HYGIENE_SCANNED_FILES = [
  BOOTSTRAP_STACK_TEST,
  BOOTSTRAP_ARGV_TEST,
];

requireCapture(
  [LIVE, PYTHON_RENDERED],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

/** Compare the exact template sections that form the bootstrap parity gate. */
function assertBootstrapSectionsEqual(got: Record<string, unknown>, want: Record<string, unknown>): void {
  assert.equal(got.Description, want.Description);
  assert.deepEqual(got.Parameters, want.Parameters);
  assert.deepEqual(got.Conditions, want.Conditions);
  assert.deepEqual(got.Resources, want.Resources);
  assert.deepEqual(got.Outputs, want.Outputs);
}

/**
 * The live parity path deliberately parses and compares the unmodified live resource objects.
 * Keeping this operation here lets the metadata-sabotage test exercise the same path as the gate.
 */
function assertLiveBootstrapTemplateEqual(rendered: string, liveTemplate: string): void {
  const got = load(rendered) as Record<string, unknown>;
  const want = JSON.parse(liveTemplate) as Record<string, unknown>;
  assertBootstrapSectionsEqual(got, want);
}

/** Deliberately removes resource metadata for this test. */
function stripResourceMetadata(template: Record<string, unknown>): Record<string, unknown> {
  const resources = template.Resources;
  assert.ok(resources !== null && typeof resources === "object" && !Array.isArray(resources));
  const strippedResources = Object.fromEntries(
    Object.entries(resources).map(([logicalId, resource]) => {
      assert.ok(resource !== null && typeof resource === "object" && !Array.isArray(resource));
      const { Metadata: ignoredMetadata, ...withoutMetadata } = resource;
      return [logicalId, withoutMetadata];
    }),
  );
  return { ...template, Resources: strippedResources };
}

describe("repository hygiene", () => {
  it("contains no non-synthetic account identifiers in the bootstrap source or tests", () => {
    const allowedAccountIds = new Set(["111111111111", "123456789012"]);
    for (const path of [BOOTSTRAP_SOURCE, ...HYGIENE_SCANNED_FILES]) {
      const source = readFileSync(path, "utf-8");
      const accountIds = source.match(/\b[0-9]{12}\b/g) ?? [];
      assert.ok(accountIds.every((accountId) => allowedAccountIds.has(accountId)), path);
    }
    const arnPattern = /[a]rn:/;
    assert.doesNotMatch(readFileSync(BOOTSTRAP_SOURCE, "utf-8"), arnPattern);
    assert.doesNotMatch(readFileSync(BOOTSTRAP_ARGV_TEST, "utf-8"), arnPattern);
    assert.doesNotMatch(readFileSync(BOOTSTRAP_STACK_TEST, "utf-8"), arnPattern);
  });
});

describe("elbAccountIdForRegion", () => {
  it("reads PyYAML-compatible scalar values from a supplied mapping", () => {
    assert.equal(existsSync(regionElbAccountIdPath()), true);
    const directory = mkdtempSync(join(tmpdir(), "ideactl-bootstrap-stack-"));
    const path = join(directory, "region-elb-account-id.yml");
    try {
      writeFileSync(path, "sample-region: 123456789012\noctal-scalar: 0123\nstring-scalar: 0890\n", "utf-8");
      assert.equal(elbAccountIdForRegion("sample-region", path), "123456789012");
      assert.equal(elbAccountIdForRegion("octal-scalar", path), "83");
      assert.equal(elbAccountIdForRegion("string-scalar", path), "0890");
      assert.equal(elbAccountIdForRegion("missing-region", path), undefined);
    } finally {
      rmSync(directory, { force: true, recursive: true });
    }
  });
});

describe("renderBootstrapStack", () => {
  it("matches the dev27 live CloudFormation template (Parameters/Conditions/Resources/Outputs/Description)", () => {
    const rendered = renderBootstrapStack({
      clusterName: "idea-dev27",
      awsDnsSuffix: "amazonaws.com",
      awsElbAccountId: elbAccountIdForRegion("us-east-2"),
      inputPermissionsBoundary: "",
    });
    assertLiveBootstrapTemplateEqual(rendered, readFileSync(LIVE, "utf-8"));
  });

  it("matches the rendered dev27 template after parsed-YAML comparison", () => {
    const rendered = renderBootstrapStack({
      clusterName: "idea-dev27",
      awsDnsSuffix: "amazonaws.com",
      awsElbAccountId: elbAccountIdForRegion("us-east-2"),
      inputPermissionsBoundary: "",
    });
    const got = load(rendered);
    const want = load(readFileSync(PYTHON_RENDERED, "utf-8"));
    assert.deepEqual(got, want);
  });

  it("fails when a metadata-stripping sabotage modifies the live oracle", () => {
    const intactTemplate = {
      Description: "sample",
      Parameters: {},
      Conditions: {},
      Resources: {
        SampleResource: {
          Type: "Sample::Resource",
          Metadata: { "aws:cdk:path": "sample/SampleResource" },
        },
      },
      Outputs: {},
    };
    const rendered = JSON.stringify(intactTemplate);
    assert.doesNotThrow(() => assertLiveBootstrapTemplateEqual(rendered, JSON.stringify(intactTemplate)));
    assert.throws(() =>
      assertLiveBootstrapTemplateEqual(rendered, JSON.stringify(stripResourceMetadata(intactTemplate))),
    );
  });

  it("rendered template equals both bootstrap parity fixtures", () => {
    const rendered = renderBootstrapStack({
      clusterName: "idea-dev27",
      awsDnsSuffix: "amazonaws.com",
      awsElbAccountId: elbAccountIdForRegion("us-east-2"),
      inputPermissionsBoundary: "",
    });
    const got = load(rendered) as Record<string, unknown>;
    const live = JSON.parse(readFileSync(LIVE, "utf-8")) as Record<string, unknown>;
    const python = load(readFileSync(PYTHON_RENDERED, "utf-8")) as Record<string, unknown>;
    assertBootstrapSectionsEqual(got, live);
    assert.deepEqual(got, python);
  });

  it("falls back to the log-delivery service principal when the region has no ELB account id", () => {
    const rendered = renderBootstrapStack({
      clusterName: "sample-cluster",
      awsDnsSuffix: "amazonaws.com",
      awsElbAccountId: undefined,
      inputPermissionsBoundary: "",
    });
    assert.match(rendered, /Service: logdelivery\.elasticloadbalancing\.amazonaws\.com/);
    assert.doesNotMatch(rendered, /iam::undefined:root/);
  });

  it("renders a non-empty custom permissions boundary into the parameter default", () => {
    const rendered = renderBootstrapStack({
      clusterName: "sample-cluster",
      awsDnsSuffix: "amazonaws.com",
      awsElbAccountId: "111111111111",
      inputPermissionsBoundary: "my-boundary-policy",
    });
    const doc = load(rendered) as { Parameters: { InputPermissionsBoundary: { Default: string } } };
    assert.equal(doc.Parameters.InputPermissionsBoundary.Default, "my-boundary-policy");
  });
});

describe("buildStack", () => {
  it("is registered and creates exactly one empty CDK stack target", async () => {
    const registryBuilder = await DEFAULT_STACK_REGISTRY.bootstrap();
    assert.equal(registryBuilder, buildStack);

    const app = new App();
    const props: StackBuildProps = {
      app,
      ctx: { clusterName: "sample-cluster" } as StackBuildProps["ctx"],
      moduleName: "bootstrap",
      deploymentId: "sample-deployment",
      terminationProtection: true,
      env: { account: "123456789012", region: "us-east-2" },
    };
    buildStack(props);

    const stack = Stack.of(app.node.findChild("sample-cluster-bootstrap"));
    assert.equal(app.node.children.length, 1);
    assert.equal(stack.stackName, "sample-cluster-bootstrap");
    assert.deepEqual(app.synth().getStackArtifact(stack.artifactId).template.Resources ?? {}, {});
  });
});
