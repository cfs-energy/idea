import assert from "node:assert/strict";
import { after, test } from "node:test";
import { DirectoryServiceStack } from "../../src/cdk/stacks/directoryservice.ts";
import { ACCOUNT, byType, cleanupWorkdirs, resourcesOf, synthBastion } from "../support/ecs-harness.ts";

after(cleanupWorkdirs);

test("the automation queue permits events from this account only", () => {
  const resources = resourcesOf(synthBastion({
    "directoryservice.provider": "aws_managed_activedirectory",
    "directoryservice.use_existing": true,
    "directoryservice.directory_id": "d-synthetic",
  }, DirectoryServiceStack, "directoryservice"));
  const policies = byType(resources, "AWS::SQS::QueuePolicy");
  const statements = policies.flatMap(([, policy]) => policy.Properties.PolicyDocument.Statement);
  const eventStatements = statements.filter(statement => statement.Principal?.Service === "events.amazonaws.com");
  assert.equal(eventStatements.length, 1);
  assert.equal(eventStatements[0].Action, "sqs:SendMessage");
  assert.deepEqual(eventStatements[0].Condition, { StringEquals: { "aws:SourceAccount": ACCOUNT } });
  const queue = byType(resources, "AWS::SQS::Queue").find(([id]) => !id.includes("dlq"))!;
  assert.deepEqual(eventStatements[0].Resource, { "Fn::GetAtt": [queue[0], "Arn"] });
});

for (const ecsEnabled of [true, false]) {
  test(`automation queue encryption with ECS ${ecsEnabled}`, () => {
    const resources = resourcesOf(synthBastion({
      "ecs.enabled": ecsEnabled,
      "directoryservice.provider": "aws_managed_activedirectory",
      "directoryservice.use_existing": true,
      "directoryservice.directory_id": "d-synthetic",
    }, DirectoryServiceStack, "directoryservice"));
    const queue = byType(resources, "AWS::SQS::Queue").find(([id]) => !id.includes("dlq"))![1];
    if (ecsEnabled) {
      assert.equal(queue.Properties.SqsManagedSseEnabled, true);
      assert.equal(queue.Properties.KmsMasterKeyId, undefined);
    } else {
      assert.ok(JSON.stringify(queue.Properties.KmsMasterKeyId).includes("alias/aws/sqs"));
    }
  });
}

test("automation queue preserves a configured customer key", () => {
  const resources = resourcesOf(synthBastion({
    "cluster.sqs.kms_key_id": "synthetic-key",
    "directoryservice.provider": "aws_managed_activedirectory",
    "directoryservice.use_existing": true,
    "directoryservice.directory_id": "d-synthetic",
  }, DirectoryServiceStack, "directoryservice"));
  const queue = byType(resources, "AWS::SQS::Queue").find(([id]) => !id.includes("dlq"))![1];
  assert.match(JSON.stringify(queue.Properties.KmsMasterKeyId), /key\/synthetic-key/);
  assert.notEqual(queue.Properties.SqsManagedSseEnabled, true);
});
