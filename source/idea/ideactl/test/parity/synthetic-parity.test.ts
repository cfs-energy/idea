/**
 * Synthesizes a credential-free metrics stack and compares its complete template
 * with the committed synthetic oracle.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { App } from "aws-cdk-lib";

import { makeContext } from "../../src/cdk/constructs/base.ts";
import { MetricsStack } from "../../src/cdk/stacks/metrics.ts";
import type { SynthReads } from "../../src/cdk/synth-reads.ts";
import { ClusterConfig } from "../../src/config/cluster-config.ts";

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = resolve(TEST_DIRECTORY, "../..");
const EXPECTED_TEMPLATE = join(
  TEST_DIRECTORY,
  "fixtures/metrics.template.json",
);
const PARITY_PROGRAM = join(PACKAGE_ROOT, "tools/parity/parity.ts");
const ACCOUNT = "123456789012";
const CLUSTER = "sample-cluster";
const REGION = "us-east-2";
const VPC_ID = "vpc-0123456789abcdef0";

const SYNTH_READS: SynthReads = {
  callerIdentity: async () => ({ account: ACCOUNT, arn: "synthetic-caller" }),
  describeDomain: async () => {
    throw new Error("synthetic metrics synthesis does not read domain settings");
  },
  describeListener: async () => ({}),
  describeUserPool: async () => ({}),
  listServiceLinkedRoles: async () => [],
};

/**
 * Builds the context entry consumed by the VPC lookup.
 */
function vpcContext(): Record<string, unknown> {
  const key = [
    "vpc-provider:account=",
    ACCOUNT,
    `:filter.vpc-id=${VPC_ID}:region=${REGION}`,
    ":returnAsymmetricSubnets=true",
  ].join("");
  return {
    [`availability-zones:account=${ACCOUNT}:region=${REGION}`]: [
      `${REGION}a`,
      `${REGION}b`,
    ],
    [key]: {
      availabilityZones: [],
      ownerAccountId: ACCOUNT,
      subnetGroups: [],
      vpcCidrBlock: "192.0.2.0/24",
      vpcId: VPC_ID,
    },
  };
}

/**
 * Synthesizes the current metrics template using only synthetic inputs.
 */
function synthesizeTemplate(): Record<string, unknown> {
  const entries: Array<{ key: string; value: unknown }> = [
    { key: "cluster.cluster_name", value: CLUSTER },
    {
      key: "cluster.cluster_s3_bucket",
      value: "sample-cluster-bucket",
    },
    {
      key: "cluster.cluster_settings_lambda_arn",
      value:
        "arn:aws:lambda:us-east-2:123456789012:function:sample-cluster-settings",
    },
    { key: "cluster.iam.roles", value: {} },
    { key: "cluster.network.security_groups", value: {} },
    { key: "cluster.network.vpc_id", value: VPC_ID },
    {
      key: "metrics.cloudwatch.dashboard_name",
      value: "sample-cluster_us-east-2",
    },
    { key: "metrics.provider", value: "cloudwatch" },
  ];
  const config = new ClusterConfig(entries);
  const app = new App({ context: vpcContext() });
  const context = makeContext({
    awsRegion: REGION,
    config,
    moduleId: "metrics",
    releaseVersion: "26.09.0",
    synthReads: SYNTH_READS,
  });
  new MetricsStack({
    app,
    ctx: context,
    deploymentId: "sample-deployment",
    env: { account: ACCOUNT, region: REGION },
    moduleName: "metrics",
    terminationProtection: true,
  });
  return app.synth().getStackByName(`${CLUSTER}-metrics`).template;
}

test("current synthetic metrics template matches the committed oracle", () => {
  const temporaryDirectory = mkdtempSync(
    join(tmpdir(), "ideactl-ci-parity-"),
  );
  const actualTemplate = join(temporaryDirectory, "metrics.template.json");
  try {
    writeFileSync(
      actualTemplate,
      `${JSON.stringify(synthesizeTemplate(), undefined, 2)}\n`,
    );
    const result = spawnSync(
      process.execPath,
      [PARITY_PROGRAM, "diff", EXPECTED_TEMPLATE, actualTemplate],
      { encoding: "utf8" },
    );
    assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
    assert.match(
      result.stdout,
      /^PARITY  2 live resources, 0 missing, 0 extra, 0 property diffs, 0 soft$/mu,
    );
  } finally {
    rmSync(temporaryDirectory, { force: true, recursive: true });
  }
});
