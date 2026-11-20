/** The cluster bucket accepts load balancer access logs only for this account's own logs. */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { load } from "js-yaml";

import { renderBootstrapStack } from "../../src/cdk/stacks/bootstrap.ts";

type Statement = { Sid?: string; Principal?: unknown; Action: string[]; Resource: Array<{ "Fn::Sub": string }>; Condition?: Record<string, Record<string, unknown>> };

const statements = (awsElbAccountId: string): Statement[] =>
  (load(renderBootstrapStack({ clusterName: "sample-cluster", awsDnsSuffix: "amazonaws.com", awsElbAccountId, inputPermissionsBoundary: "" })) as {
    Resources: { StagingBucketPolicy: { Properties: { PolicyDocument: { Statement: Statement[] } } } };
  }).Resources.StagingBucketPolicy.Properties.PolicyDocument.Statement;

describe("cluster bucket load balancer log delivery", () => {
  for (const [label, elbAccount] of [["legacy region account", "111111111111"], ["service principal region", ""]] as const) {
    it(`${label}: every log write is limited to this account's AWSLogs path`, () => {
      const writes = statements(elbAccount).filter((statement) => statement.Sid?.startsWith("Idea") && statement.Action.includes("s3:PutObject"));
      assert.equal(writes.length, 3);
      for (const statement of writes) {
        assert.deepEqual(statement.Resource, [{ "Fn::Sub": "${StagingBucket.Arn}/logs/*/AWSLogs/${AWS::AccountId}/*" }], String(statement.Sid));
      }
    });
  }

  it("the log delivery service writes and checks the bucket only on behalf of this account", () => {
    const delivery = statements("").filter((statement) => JSON.stringify(statement.Principal).includes("delivery.logs."));
    assert.equal(delivery.length, 2);
    for (const statement of delivery) {
      assert.deepEqual(statement.Condition?.StringEquals?.["aws:SourceAccount"], { Ref: "AWS::AccountId" }, String(statement.Sid));
    }
  });
});
