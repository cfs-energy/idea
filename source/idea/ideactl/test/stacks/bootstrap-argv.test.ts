import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { shake256Hex } from "../../src/util/shake256.ts";
import {
  bootstrapStackName,
  bootstrapTags,
  buildBootstrapArgv,
  customTagsToKeyValuePairs,
  IDEA_TAG_CLUSTER_NAME,
} from "../../src/cli/commands/bootstrap.ts";

describe("customTagsToKeyValuePairs", () => {
  it("parses \"Key=k,Value=v\" strings", () => {
    assert.deepEqual(customTagsToKeyValuePairs(["Key=CostCenter,Value=42"]), new Map([["CostCenter", "42"]]));
  });

  it("keeps commas inside the value (split(\",\", 1) semantics)", () => {
    assert.deepEqual(customTagsToKeyValuePairs(["Key=Owners,Value=a,b,c"]), new Map([["Owners", "a,b,c"]]));
  });

  it("drops entries with an empty key or value", () => {
    assert.deepEqual(customTagsToKeyValuePairs(["Key= ,Value=x", "Key=k,Value= "]), new Map());
  });

  it("raises an IndexError for malformed entries", () => {
    for (const customTag of ["malformed", "Value=value", "Key=key"]) {
      assert.throws(
        () => customTagsToKeyValuePairs([customTag]),
        (error: Error) =>
          error.name === "IndexError" &&
          error.message === `Custom tag ${customTag} is not in Key=k,Value=v form. Fix the tag and re-run bootstrap.`,
      );
    }
  });

  it("retains arbitrary keys and insertion order", () => {
    const tags = customTagsToKeyValuePairs([
      "Key=2,Value=second",
      "Key=1,Value=first",
      "Key=__proto__,Value=retained",
      "Key=2,Value=replaced",
    ]);
    assert.deepEqual([...tags], [
      ["2", "replaced"],
      ["1", "first"],
      ["__proto__", "retained"],
    ]);
  });
});

describe("bootstrapTags", () => {
  it("is idea:ClusterName plus the custom tags, demo1 has none", () => {
    assert.deepEqual(bootstrapTags("idea-demo1", []), new Map([[IDEA_TAG_CLUSTER_NAME, "idea-demo1"]]));
  });

  it("idea:ClusterName wins if a custom tag reuses the key (merge order in cdk_invoker.py:546)", () => {
    assert.deepEqual(
      bootstrapTags("idea-demo1", ["Key=idea:ClusterName,Value=bogus"]),
      new Map([[IDEA_TAG_CLUSTER_NAME, "idea-demo1"]]),
    );
  });

  it("preserves custom-tag order when building repeated bootstrap tag flags", () => {
    const argv = buildBootstrapArgv({
      cdkBin: "cdk",
      cdkAppCmd: "app",
      clusterName: "sample-cluster",
      clusterBucket: "bucket",
      terminationProtection: true,
      qualifier: "abc",
      templatePath: "/tmp/template.yml",
      tags: bootstrapTags("sample-cluster", [
        "Key=2,Value=second",
        "Key=1,Value=first",
        "Key=__proto__,Value=retained",
      ]),
    });
    assert.deepEqual(argv.filter((value) => value.includes("=")), [
      "2=second",
      "1=first",
      "__proto__=retained",
      "idea:ClusterName=sample-cluster",
    ]);
  });
});

describe("buildBootstrapArgv", () => {
  // demo1 inputs use default termination protection and public-access blocking.
  const qualifier = shake256Hex("idea-demo1", 5);

  it("matches CdkInvoker.bootstrap_cluster for demo1-shaped inputs", () => {
    const argv = buildBootstrapArgv({
      cdkBin: "cdk",
      cdkAppCmd: "idea-admin cdk cdk-app --cluster-name idea-demo1 --aws-region us-east-2 --module-id bootstrap --module-name bootstrap --deployment-id 700f4b2c-110b-4395-a208-b13874d20ec5 --termination-protection true",
      clusterName: "idea-demo1",
      clusterBucket: "idea-demo1-cluster-us-east-2-123456789012",
      terminationProtection: true,
      qualifier,
      templatePath: "/home/example/.idea/clusters/idea-demo1/us-east-2/_cdk/cdk_toolkit_stack.yml",
      tags: bootstrapTags("idea-demo1", []),
    });

    assert.equal(qualifier, "d98389644e");
    assert.deepEqual(argv, [
      "cdk",
      "bootstrap",
      "--app",
      "idea-admin cdk cdk-app --cluster-name idea-demo1 --aws-region us-east-2 --module-id bootstrap --module-name bootstrap --deployment-id 700f4b2c-110b-4395-a208-b13874d20ec5 --termination-protection true",
      "--bootstrap-bucket-name",
      "idea-demo1-cluster-us-east-2-123456789012",
      "--toolkit-stack-name",
      "idea-demo1-bootstrap",
      "--termination-protection",
      "true",
      "--qualifier",
      "d98389644e",
      "--template",
      "/home/example/.idea/clusters/idea-demo1/us-east-2/_cdk/cdk_toolkit_stack.yml",
      "--public-access-block-configuration",
      "true",
      "--tags",
      "idea:ClusterName=idea-demo1",
    ]);
  });

  it("omits optional string flags when empty or whitespace-only (Utils.is_not_empty)", () => {
    const argv = buildBootstrapArgv({
      cdkBin: "cdk",
      cdkAppCmd: "idea-admin cdk cdk-app ...",
      clusterName: "idea-demo1",
      clusterBucket: "bucket",
      terminationProtection: true,
      qualifier: "abc",
      templatePath: "/tmp/cdk_toolkit_stack.yml",
      customPermissionsBoundary: "   ",
      cloudformationExecutionPolicies: "\t",
      awsProfile: "\n",
      tags: new Map(),
    });
    assert.equal(argv.includes("--custom-permissions-boundary"), false);
    assert.equal(argv.includes("--cloudformation-execution-policies"), false);
    assert.equal(argv.includes("--profile"), false);
  });

  it("emits every optional flag when set, and always emits --public-access-block-configuration even when false", () => {
    const argv = buildBootstrapArgv({
      cdkBin: "cdk",
      cdkAppCmd: "idea-admin cdk cdk-app ...",
      clusterName: "idea-test1",
      clusterBucket: "bucket",
      terminationProtection: false,
      qualifier: "abc123",
      templatePath: "/tmp/cdk_toolkit_stack.yml",
      customPermissionsBoundary: "my-boundary",
      cloudformationExecutionPolicies: "managed-policy-value",
      publicAccessBlockConfiguration: false,
      tags: new Map([[IDEA_TAG_CLUSTER_NAME, "idea-test1"], ["CostCenter", "42"]]),
      awsProfile: "idea-test1-profile",
    });

    assert.deepEqual(argv, [
      "cdk",
      "bootstrap",
      "--app",
      "idea-admin cdk cdk-app ...",
      "--bootstrap-bucket-name",
      "bucket",
      "--toolkit-stack-name",
      "idea-test1-bootstrap",
      "--termination-protection",
      "false",
      "--qualifier",
      "abc123",
      "--template",
      "/tmp/cdk_toolkit_stack.yml",
      "--custom-permissions-boundary",
      "my-boundary",
      "--cloudformation-execution-policies",
      "managed-policy-value",
      "--public-access-block-configuration",
      "false",
      "--tags",
      "idea:ClusterName=idea-test1",
      "--tags",
      "CostCenter=42",
      "--profile",
      "idea-test1-profile",
    ]);
  });

  it("bootstrapStackName is \"<cluster>-bootstrap\"", () => {
    assert.equal(bootstrapStackName("idea-demo1"), "idea-demo1-bootstrap");
  });
});
