import assert from "node:assert/strict";
import test from "node:test";
import { App } from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { CostCollectorStack } from "../../src/cdk/stacks/cost-collector.ts";
import { DATADOG_AGENT_IMAGE } from "../../src/config/datadog-agent.ts";

const props = {
  env: { account: "123456789012", region: "us-east-1" },
  clusterName: "gov-cluster",
  controlPlaneImage: "123456789012.dkr.ecr.us-east-1.amazonaws.com/control:release",
  agentImage: `123456789012.dkr.ecr.us-east-1.amazonaws.com/agent@sha256:${"a".repeat(64)}`,
  datadogApiKeySecretArn: "arn:aws:secretsmanager:us-east-1:123456789012:secret:datadog-abcdef",
  vpcId: "vpc-0123456789abcdef0",
  subnetIds: ["subnet-0123456789abcdef0"],
  publicSubnets: true,
};

function template(overrides = {}): Template {
  return Template.fromStack(new CostCollectorStack(new App(), "cost-collector", { ...props, ...overrides }));
}

test("one Fargate service has two containers sharing a socket and an injected agent secret", () => {
  const result = template();
  result.resourceCountIs("AWS::ECS::Cluster", 1);
  result.resourceCountIs("AWS::Logs::LogGroup", 1);
  result.resourceCountIs("AWS::ECS::TaskDefinition", 1);
  result.resourceCountIs("AWS::ECS::Service", 1);
  const task = Object.values(result.findResources("AWS::ECS::TaskDefinition"))[0].Properties;
  assert.equal(task.Cpu, "256");
  assert.equal(task.Memory, "1024");
  assert.deepEqual(task.RequiresCompatibilities, ["FARGATE"]);
  assert.deepEqual(task.Volumes, [{ Name: "datadog" }]);
  assert.equal(task.ContainerDefinitions.length, 2);
  const [agent, collector] = task.ContainerDefinitions;
  assert.equal(agent.Image, props.agentImage);
  assert.equal(collector.Image, props.controlPlaneImage);
  assert.deepEqual(agent.Secrets, [{ Name: "DD_API_KEY", ValueFrom: props.datadogApiKeySecretArn }]);
  const env = (container: typeof agent) => Object.fromEntries(container.Environment.map((entry: { Name: string; Value: string }) => [entry.Name, entry.Value]));
  assert.deepEqual(env(agent), {
    ECS_FARGATE: "true", DD_DOGSTATSD_ORIGIN_DETECTION: "true",
    DD_DOGSTATSD_SOCKET: "/var/run/datadog/dsd.socket", DD_TAGS: "idea_cluster:gov-cluster",
  });
  assert.deepEqual(env(collector), {
    IDEA_CONTAINER_ROLE: "cost-metrics", IDEA_CLUSTER_NAME: "gov-cluster", AWS_DEFAULT_REGION: "us-east-1",
    DD_DOGSTATSD_URL: "unix:///var/run/datadog/dsd.socket", IDEA_COST_METRICS_ENABLED: "true",
    IDEA_COST_METRICS_INTERVAL_HOURS: "6", IDEA_COST_METRICS_LOOKBACK_DAYS: "3",
    IDEA_COST_METRICS_MODULE_TAG: "idea:ModuleId", IDEA_COST_METRICS_PROJECT_TAG: "idea:Project",
    IDEA_COST_METRICS_OWNER_TAG: "idea:JobOwner", IDEA_COST_METRICS_BY_ACCOUNT: "false",
  });
  assert.deepEqual(agent.MountPoints, [{ ContainerPath: "/var/run/datadog", SourceVolume: "datadog", ReadOnly: false }]);
  assert.deepEqual(collector.MountPoints, [{ ContainerPath: "/var/run/datadog", SourceVolume: "datadog", ReadOnly: true }]);
  assert.deepEqual(collector.DependsOn, [{ ContainerName: "datadog", Condition: "HEALTHY" }]);
  result.hasResourceProperties("AWS::ECS::Service", {
    DesiredCount: 1, LaunchType: "FARGATE",
    DeploymentConfiguration: { MinimumHealthyPercent: 0, MaximumPercent: 100 },
    NetworkConfiguration: { AwsvpcConfiguration: { AssignPublicIp: "ENABLED", Subnets: props.subnetIds } },
  });
});

test("task role has only Cost Explorer reads and execution role owns image, log and secret access", () => {
  const result = template();
  const policies = Object.values(result.findResources("AWS::IAM::Policy"));
  const taskPolicy = policies.find((policy) => policy.Properties.PolicyDocument.Statement.some((s: { Action: unknown }) => JSON.stringify(s.Action).includes("ce:GetCostAndUsage")))!;
  assert.deepEqual(taskPolicy.Properties.PolicyDocument.Statement, [{
    Action: ["ce:GetCostAndUsage", "ce:GetTags", "ce:GetDimensionValues"], Effect: "Allow", Resource: "*",
  }]);
  result.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Action: ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource: props.datadogApiKeySecretArn,
    })]) },
  });
  result.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Action: "kms:Decrypt", Resource: "*",
      Condition: { StringEquals: {
        "kms:ViaService": { "Fn::Join": ["", ["secretsmanager.us-east-1.", { Ref: "AWS::URLSuffix" }]] },
        "kms:EncryptionContext:SecretARN": props.datadogApiKeySecretArn,
      } },
    })]) },
  });
  result.hasResourceProperties("AWS::IAM::Role", {
    ManagedPolicyArns: Match.arrayWith([Match.objectLike({ "Fn::Join": Match.anyValue() })]),
  });
  const roles = Object.values(result.findResources("AWS::IAM::Role"));
  assert.equal(roles.length, 2);
  for (const role of roles) {
    assert.deepEqual(role.Properties.AssumeRolePolicyDocument.Statement[0].Condition, { StringEquals: { "aws:SourceAccount": "123456789012" } });
  }
  const execution = roles.find((role) => role.Properties.ManagedPolicyArns !== undefined)!;
  assert.match(JSON.stringify(execution), /AmazonECSTaskExecutionRolePolicy/);
  const service = Object.values(result.findResources("AWS::ECS::Service"))[0];
  for (const id of Object.keys(result.findResources("AWS::IAM::Policy"))) {
    assert.ok(service.DependsOn.includes(id));
  }
  assert.notDeepEqual(policies.find((p) => p !== taskPolicy)!.Properties.Roles, taskPolicy.Properties.Roles);
});

test("private subnets do not get public IPs and overrides reach the collector", () => {
  const result = template({ publicSubnets: false, intervalHours: 12, lookbackDays: 7, moduleTag: "m", projectTag: "p", ownerTag: "o", byAccount: true });
  result.hasResourceProperties("AWS::ECS::Service", {
    NetworkConfiguration: { AwsvpcConfiguration: { AssignPublicIp: "DISABLED" } },
  });
  const task = Object.values(result.findResources("AWS::ECS::TaskDefinition"))[0].Properties;
  const env = Object.fromEntries(task.ContainerDefinitions[1].Environment.map((entry: { Name: string; Value: string }) => [entry.Name, entry.Value]));
  for (const [key, value] of Object.entries({ INTERVAL_HOURS: "12", LOOKBACK_DAYS: "7", MODULE_TAG: "m", PROJECT_TAG: "p", OWNER_TAG: "o", BY_ACCOUNT: "true" })) {
    assert.equal(env[`IDEA_COST_METRICS_${key}`], value);
  }
});

for (const agentImage of ["datadog/agent:latest", "public.ecr.aws/datadog/agent:latest", props.agentImage.replace(/@.*/, ":latest"), props.agentImage.slice(0, -1)]) {
  test(`rejects unpinned agent image ${agentImage}`, () => {
    assert.throws(() => template({ agentImage }), /digest-pinned image reference/);
  });
}

test("accepts Datadog's official public ECR image when it is digest-pinned", () => {
  const rendered = template({ agentImage: DATADOG_AGENT_IMAGE });
  rendered.hasResourceProperties("AWS::ECS::TaskDefinition", {
    ContainerDefinitions: Match.arrayWith([Match.objectLike({ Image: DATADOG_AGENT_IMAGE })]),
  });
});

test("invalid schedules and empty subnet lists fail synthesis", () => {
  for (const intervalHours of [0, -1, 1.5, NaN]) assert.throws(() => template({ intervalHours }), /positive integers/);
  assert.throws(() => template({ lookbackDays: 0 }), /positive integers/);
  assert.throws(() => template({ subnetIds: [] }), /at least one subnet/);
});
