import assert from "node:assert/strict";
import test from "node:test";
import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";

import { ClusterConfig } from "../../src/config/cluster-config.ts";
import type { IdeaContext } from "../../src/cdk/constructs/base.ts";
import { buildExecutionRole, grantInjectedSecret } from "../../src/cdk/constructs/container.ts";

for (const customerManaged of [true, false]) {
  test(`gateway certificate injection ${customerManaged ? "custom cluster key" : "default cluster key"} grants secret-constrained KMS decryption`, () => {
    const stack = new Stack(new App(), "gateway", { env: { account: "123456789012", region: "us-east-2" } });
    const config = new ClusterConfig(Object.entries({
      "cluster.aws.partition": "aws",
      "cluster.aws.account_id": "123456789012",
      "cluster.aws.region": "us-east-2",
      "cluster.kms.key_type": customerManaged ? "customer-managed" : "aws-managed",
      "cluster.secretsmanager.kms_key_id": customerManaged ? "arn:aws:kms:us-east-2:123456789012:key/cert-key" : null,
    }).map(([key, value]) => ({ key, value })));
    const ctx = { config, clusterName: "sample", awsRegion: "us-east-2" } as IdeaContext;
    const vpc = new ec2.Vpc(stack, "vpc", { maxAzs: 1, natGateways: 0 });
    const executionRole = buildExecutionRole({ ctx, stack, vpc, privateSubnets: [] }, "gateway-execution");
    const task = new ecs.Ec2TaskDefinition(stack, "gateway-task", { executionRole });
    const certificate = secretsmanager.Secret.fromSecretCompleteArn(stack, "certificate", "arn:aws:secretsmanager:us-east-2:123456789012:secret:certificate-abcdef");
    const privateKey = secretsmanager.Secret.fromSecretCompleteArn(stack, "private-key", "arn:aws:secretsmanager:us-east-2:123456789012:secret:private-key-abcdef");
    for (const secret of [certificate, privateKey]) grantInjectedSecret({ ctx, stack, vpc, privateSubnets: [] }, executionRole, secret.secretArn);
    task.addContainer("gateway", {
      image: ecs.ContainerImage.fromRegistry("example.invalid/gateway"),
      memoryLimitMiB: 128,
      secrets: { CERTIFICATE: ecs.Secret.fromSecretsManager(certificate), PRIVATE_KEY: ecs.Secret.fromSecretsManager(privateKey) },
    });
    const template = Template.fromStack(stack);
    const policies = template.findResources("AWS::IAM::Policy");
    const statements = Object.values(policies)
      .filter((policy) => policy.Properties.Roles.some((role: { Ref: string }) => role.Ref === stack.getLogicalId(executionRole.node.defaultChild as import("aws-cdk-lib").CfnResource)))
      .flatMap((policy) => policy.Properties.PolicyDocument.Statement);
    for (const secret of [certificate, privateKey]) {
      assert.ok(statements.some((statement) => JSON.stringify(statement.Action).includes("secretsmanager:GetSecretValue") && statement.Resource === secret.secretArn));
    }
    const decrypt = statements.filter((statement) => statement.Action === "kms:Decrypt");
    assert.deepEqual(decrypt, [certificate, privateKey].map((secret) => ({
      Action: "kms:Decrypt", Effect: "Allow", Resource: "*", Condition: { StringEquals: {
        "kms:ViaService": { "Fn::Join": ["", ["secretsmanager.us-east-2.", { Ref: "AWS::URLSuffix" }]] },
        "kms:EncryptionContext:SecretARN": secret.secretArn,
      } },
    })));
  });
}
