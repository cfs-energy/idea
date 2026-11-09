import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { load } from "js-yaml";

import { renderBootstrapStack, schedulerLogRetentionDays } from "../../src/cdk/stacks/bootstrap.ts";

describe("scheduler log archive expiry", () => {
  const base = {
    clusterName: "sample-cluster",
    awsDnsSuffix: "amazonaws.com",
    awsElbAccountId: "111111111111",
    inputPermissionsBoundary: "",
  };
  type Rule = Record<string, unknown>;
  const rules = (rendered: string): Rule[] =>
    (load(rendered) as { Resources: { StagingBucket: { Properties: { LifecycleConfiguration: { Rules: Rule[] } } } } })
      .Resources.StagingBucket.Properties.LifecycleConfiguration.Rules;

  it("adds no rule unless a retention is set, so an unconfigured bucket renders as before", () => {
    assert.deepEqual(rules(renderBootstrapStack(base)).map((rule) => rule.Id), ["CleanupOldVersions"]);
    assert.deepEqual(
      rules(renderBootstrapStack({ ...base, schedulerLogArchives: [{ moduleId: "scheduler", retentionDays: 0 }] })).map(
        (rule) => rule.Id,
      ),
      ["CleanupOldVersions"],
    );
  });

  it("expires only the scheduler module's prefix, in place on the existing bucket", () => {
    const rendered = renderBootstrapStack({
      ...base,
      schedulerLogArchives: [{ moduleId: "scheduler", retentionDays: 400 }],
    });
    const found = rules(rendered);
    const expire = found.find((rule) => rule.Id === "ExpireSchedulerLogArchive1");
    assert.equal(expire?.Prefix, "logs/scheduler/");
    assert.equal(expire?.ExpirationInDays, 400);
    assert.equal(expire?.Status, "Enabled");
    assert.deepEqual(expire?.NoncurrentVersionExpiration, { NoncurrentDays: 1 });
    const markers = found.find((rule) => rule.Id === "ExpireSchedulerLogDeleteMarkers1");
    assert.equal(markers?.ExpiredObjectDeleteMarker, true);
    // the load balancer access logs are under logs/ too and are left alone
    assert.ok(found.every((rule) => rule.Prefix === undefined || String(rule.Prefix).startsWith("logs/scheduler/")));
    // a lifecycle rule changes the bucket in place: its name, and so its identity, are untouched
    const doc = load(rendered) as { Resources: { StagingBucket: { DeletionPolicy: string; UpdateReplacePolicy: string } } };
    assert.equal(doc.Resources.StagingBucket.DeletionPolicy, "Retain");
    assert.equal(doc.Resources.StagingBucket.UpdateReplacePolicy, "Retain");
  });

  it("gives each scheduler module its own rules", () => {
    const found = rules(
      renderBootstrapStack({
        ...base,
        schedulerLogArchives: [
          { moduleId: "scheduler", retentionDays: 45 },
          { moduleId: "scheduler-b", retentionDays: 90 },
        ],
      }),
    );
    assert.deepEqual(
      found.filter((rule) => rule.ExpirationInDays !== undefined).map((rule) => [rule.Prefix, rule.ExpirationInDays]),
      [
        ["logs/scheduler/", 45],
        ["logs/scheduler-b/", 90],
      ],
    );
  });

  it("accepts 0 or at least 30 days, and refuses a retention shorter than the sync would undo", () => {
    assert.equal(schedulerLogRetentionDays(0), 0);
    assert.equal(schedulerLogRetentionDays(30), 30);
    for (const bad of [1, 29, -1, 1.5]) assert.throws(() => schedulerLogRetentionDays(bad), /s3_retention_days/);
  });
});
