/**
 * Deployment controls in the container stack that no other suite pins.
 *
 * Each test asserts the property by value, so a reworded setting with the wrong
 * value still fails. The controls are the ones whose loss is silent: a task that
 * inherits the host security group, a service that cannot roll itself back, a
 * host that lets a task reach instance metadata, a positional target-group list
 * that the module stacks index by number.
 */

import assert from "node:assert/strict";
import { after, test } from "node:test";

import {
  ACCOUNT,
  ECS_CLUSTER,
  REGION,
  byType,
  cleanupWorkdirs,
  onlyOne,
  resourcesOf,
  synthEcs,
  type Json,
} from "./harness.ts";

const APPLICATION_ROLES = ["cluster-manager", "vdc", "scheduler", "dcv-broker", "dcv-gateway"] as const;

const HOST_SECURITY_GROUP_NAME = `${ECS_CLUSTER}-ecs-host-security-group`;

/** The scheduler task role this stack creates, which the file-system policy names. */
const SCHEDULER_TASK_ROLE_NAME = `${ECS_CLUSTER}-ecs-scheduler-task-role-${REGION}`;

after(cleanupWorkdirs);

/** Logical id to `Name` for every target group in the template. */
function targetGroupNames(resources: Record<string, Json>): Record<string, string> {
  const names: Record<string, string> = {};
  for (const [id, resource] of Object.entries(resources)) {
    if (String(resource["Type"]).endsWith("::TargetGroup")) names[id] = resource["Properties"]["Name"] as string;
  }
  return names;
}

/** Logical id to `GroupName` for every security group in the template. */
function securityGroupNames(resources: Record<string, Json>): Record<string, string> {
  const names: Record<string, string> = {};
  for (const [id, resource] of byType(resources, "AWS::EC2::SecurityGroup")) {
    names[id] = resource["Properties"]["GroupName"] as string;
  }
  return names;
}

/** The container role a task definition carries in `IDEA_CONTAINER_ROLE`. */
function roleOfTaskDefinition(resource: Json): string | undefined {
  for (const container of resource["Properties"]["ContainerDefinitions"] as Json[]) {
    for (const variable of (container["Environment"] ?? []) as Json[]) {
      if (variable["Name"] === "IDEA_CONTAINER_ROLE") return variable["Value"] as string;
    }
  }
  return undefined;
}

/** Container role to service, resolved through each service's `TaskDefinition` ref. */
function servicesByRole(resources: Record<string, Json>): Record<string, Json> {
  const roles: Record<string, string> = {};
  for (const [id, resource] of byType(resources, "AWS::ECS::TaskDefinition")) {
    const role = roleOfTaskDefinition(resource);
    if (role !== undefined) roles[id] = role;
  }
  const services: Record<string, Json> = {};
  for (const [, service] of byType(resources, "AWS::ECS::Service")) {
    const taskDefinition = service["Properties"]["TaskDefinition"]?.Ref as string | undefined;
    const role = taskDefinition === undefined ? undefined : roles[taskDefinition];
    if (role !== undefined) services[role] = service;
  }
  return services;
}

/** The published `ecs.<role>.target_group_arns` list, resolved to target groups. */
function publishedTargetGroups(resources: Record<string, Json>, role: string): Json[] {
  const settings = onlyOne(byType(resources, "Custom::ClusterSettings"), "settings resource")[1];
  const published = settings["Properties"]["settings"][`${role}.target_group_arns`] as Json[];
  return published.map((entry) => {
    const target = resources[entry["Ref"] as string];
    if (target === undefined) throw new Error(`${role} publishes something that is not a stack target group`);
    return target;
  });
}

/**
 * The role part of a target-group name: `{cluster}-{identifier}-{8 hex}` with the
 * cluster prefix and the uniqueness suffix removed, then its last segment. This is
 * what distinguishes three otherwise identical HTTPS groups from one another.
 */
function targetGroupRole(targetGroup: Json): string {
  const name = String(targetGroup["Properties"]["Name"]);
  const identifier = name.slice(`${ECS_CLUSTER}-`.length, -"-00000000".length);
  return identifier.slice(identifier.lastIndexOf("-") + 1);
}

test("each task runs with its own module security group, never the host group", () => {
  const resources = resourcesOf(synthEcs());
  const groupNames = securityGroupNames(resources);
  const services = servicesByRole(resources);

  for (const role of APPLICATION_ROLES) {
    const service = services[role];
    assert.ok(service !== undefined, `${role} service`);
    const attached = service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"] as Json[];
    assert.deepEqual(
      attached.map((entry) => groupNames[(entry["Fn::GetAtt"] as string[])[0] as string]),
      [`${ECS_CLUSTER}-ecs-${role}-task-security-group`],
      `${role} task ENI carries only its own task security group`,
    );
  }

  const hostGroupIds = Object.entries(groupNames)
    .filter(([, name]) => name === HOST_SECURITY_GROUP_NAME)
    .map(([id]) => id);
  assert.equal(hostGroupIds.length, 1, "one host security group");
  assert.equal(
    JSON.stringify(byType(resources, "AWS::ECS::Service")).includes(hostGroupIds[0] as string),
    false,
    "no service attaches the container-host security group to a task ENI",
  );
});

/**
 * Seconds of load balancer grace per role, and the container start period where one exists.
 *
 * A role's start allowance is its own documented wait plus the application allowance: the batch
 * server wait of 150 seconds in the image's scheduler script, the service discovery wait of 60
 * seconds in its broker script, and 120 seconds for the module process itself. The grace adds 150
 * seconds for the target to register as healthy, because a target mid-registration is not healthy
 * and the grace is what stops the platform acting on that. The library default of 60 seconds is
 * shorter than every one of these.
 */
const START_ALLOWANCE_SECONDS: Record<string, number> = {
  "cluster-manager": 120,
  vdc: 120,
  "dcv-gateway": 120,
  "dcv-broker": 180,
  scheduler: 270,
};
const REGISTRATION_SECONDS = 150;

test("each service gets a start allowance sized from its own start, not the library default", () => {
  const resources = resourcesOf(synthEcs(true));
  const services = servicesByRole(resources);

  for (const role of APPLICATION_ROLES) {
    assert.equal(
      services[role]?.["Properties"]["HealthCheckGracePeriodSeconds"],
      (START_ALLOWANCE_SECONDS[role] as number) + REGISTRATION_SECONDS,
      `${role} service start allowance`,
    );
  }

  // The container health check has no allowance but its own start period, on a first start and on
  // every replacement, so it covers the start without the registration.
  const [, scheduler] = byType(resources, "AWS::ECS::TaskDefinition").find(
    ([, resource]) => roleOfTaskDefinition(resource) === "scheduler",
  ) as [string, Json];
  const containers = scheduler["Properties"]["ContainerDefinitions"] as Json[];
  const checked = containers.filter((container) => container["HealthCheck"] !== undefined);
  assert.equal(checked.length, 1, "one container carries the scheduler health check");
  assert.equal(
    checked[0]?.["HealthCheck"]["StartPeriod"],
    START_ALLOWANCE_SECONDS["scheduler"],
    "the scheduler container start period covers its own batch server wait",
  );
  assert.equal(checked[0]?.["HealthCheck"]["Interval"], 30, "container check interval");
  assert.equal(checked[0]?.["HealthCheck"]["Retries"], 3, "container check retries");

  // The observability daemon has no load balancer, and a grace without one is rejected on update.
  const daemon = byType(resources, "AWS::ECS::Service").find(
    ([, service]) => service["Properties"]["SchedulingStrategy"] === "DAEMON",
  );
  assert.ok(daemon !== undefined, "the observability daemon service");
  assert.equal(
    daemon[1]["Properties"]["HealthCheckGracePeriodSeconds"],
    undefined,
    "a service with no load balancer carries no grace",
  );
});

test("every application service rolls its own failed deployment back", () => {
  const resources = resourcesOf(synthEcs());
  const services = servicesByRole(resources);

  for (const role of APPLICATION_ROLES) {
    const deployment = services[role]?.["Properties"]["DeploymentConfiguration"] as Json | undefined;
    assert.deepEqual(
      deployment?.["DeploymentCircuitBreaker"],
      { Enable: true, Rollback: true },
      `${role} service deployment circuit breaker`,
    );
  }
});

test("the capacity provider keeps managed termination protection and managed scaling", () => {
  const resources = resourcesOf(synthEcs());
  const provider = onlyOne(byType(resources, "AWS::ECS::CapacityProvider"), "capacity provider")[1];
  const asgProvider = provider["Properties"]["AutoScalingGroupProvider"] as Json;

  assert.equal(asgProvider["ManagedTerminationProtection"], "ENABLED", "managed termination protection");
  assert.deepEqual(asgProvider["ManagedScaling"], { Status: "ENABLED", TargetCapacity: 100 }, "managed scaling");
});

test("new container hosts are protected from scale-in", () => {
  const resources = resourcesOf(synthEcs());
  const group = onlyOne(byType(resources, "AWS::AutoScaling::AutoScalingGroup"), "host auto scaling group")[1];

  assert.equal(
    group["Properties"]["NewInstancesProtectedFromScaleIn"],
    true,
    "a host running tasks is not terminated by a scale-in",
  );
});

test("the host launch template keeps the instance metadata hop limit at one", () => {
  const resources = resourcesOf(synthEcs());
  const launchTemplate = onlyOne(byType(resources, "AWS::EC2::LaunchTemplate"), "host launch template")[1];

  assert.deepEqual(
    launchTemplate["Properties"]["LaunchTemplateData"]["MetadataOptions"],
    { HttpPutResponseHopLimit: 1, HttpTokens: "required" },
    "a hop limit above one lets a container reach the instance role through the host",
  );
});

test("every role that trusts the task service is scoped to this account", () => {
  const resources = resourcesOf(synthEcs(true));
  const executionRoleIds = new Set<string>();
  for (const [, taskDefinition] of byType(resources, "AWS::ECS::TaskDefinition")) {
    const arn = taskDefinition["Properties"]["ExecutionRoleArn"] as Json | undefined;
    const id = (arn?.["Fn::GetAtt"] as string[] | undefined)?.[0];
    if (id !== undefined) executionRoleIds.add(id);
  }
  assert.equal(executionRoleIds.size, 3, "one shared, one gateway and one observability execution role");

  // Every role in the template with this trust, not only the ones a task definition names as its
  // execution role. A task definition built without a task role gets a generated one, and that
  // generated trust is the one that carried no condition.
  const trustIds = new Set(
    byType(resources, "AWS::IAM::Role")
      .filter(([, role]) =>
        JSON.stringify(role["Properties"]["AssumeRolePolicyDocument"]).includes("ecs-tasks.amazonaws.com"),
      )
      .map(([id]) => id),
  );
  assert.deepEqual([...executionRoleIds].filter((id) => !trustIds.has(id)), [], "every execution role is counted");
  // Named rather than counted, so a role that gains or loses this trust fails here by name. The
  // five task roles are this stack's own task identities; the module instance roles they replace
  // are not in this stack and keep existing until the module hosts retire.
  assert.deepEqual(
    [...trustIds].map((id) => resources[id]?.["Properties"]["RoleName"]).sort(),
    [
      `${ECS_CLUSTER}-datadog-task-execution-role-${REGION}`,
      `${ECS_CLUSTER}-datadog-task-role-${REGION}`,
      `${ECS_CLUSTER}-ecs-cluster-manager-task-role-${REGION}`,
      `${ECS_CLUSTER}-ecs-dcv-broker-task-role-${REGION}`,
      `${ECS_CLUSTER}-ecs-dcv-gateway-task-role-${REGION}`,
      `${ECS_CLUSTER}-ecs-scheduler-task-role-${REGION}`,
      `${ECS_CLUSTER}-ecs-task-execution-role-${REGION}`,
      `${ECS_CLUSTER}-ecs-vdc-task-role-${REGION}`,
      `${ECS_CLUSTER}-gateway-task-execution-role-${REGION}`,
    ],
    "every role trusting the task service, by name",
  );

  for (const id of trustIds) {
    assert.deepEqual(
      resources[id]?.["Properties"]["AssumeRolePolicyDocument"]["Statement"],
      [
        {
          Action: "sts:AssumeRole",
          Condition: { StringEquals: { "aws:SourceAccount": ACCOUNT } },
          Effect: "Allow",
          Principal: { Service: "ecs-tasks.amazonaws.com" },
        },
      ],
      `${id} trusts ecs-tasks only from this account`,
    );
  }
});

/** The `RoleName` of the role a `Fn::GetAtt` refers to, so a policy can be checked by value. */
function roleNameOf(resources: Record<string, Json>, value: unknown): string | undefined {
  const getAtt = (value as Json | undefined)?.["Fn::GetAtt"];
  const id = Array.isArray(getAtt) ? (getAtt[0] as string) : undefined;
  if (id === undefined) return undefined;
  const role = resources[id];
  return role?.["Type"] === "AWS::IAM::Role" ? (role["Properties"]["RoleName"] as string) : undefined;
}

test("the PBS file system denies every principal but the scheduler task role", () => {
  const resources = resourcesOf(synthEcs());
  const fileSystem = onlyOne(byType(resources, "AWS::EFS::FileSystem"), "PBS file system")[1];
  const statements = fileSystem["Properties"]["FileSystemPolicy"]["Statement"] as Json[];
  const clientActions = [
    "elasticfilesystem:ClientMount",
    "elasticfilesystem:ClientWrite",
    "elasticfilesystem:ClientRootAccess",
  ];

  const principalDeny = statements.find((statement) => statement["Condition"]?.["ArnNotEquals"] !== undefined);
  assert.ok(principalDeny !== undefined, "a statement conditioned on the calling principal");
  assert.equal(principalDeny["Effect"], "Deny", "the principal statement denies");
  assert.deepEqual(principalDeny["Principal"], { AWS: "*" }, "the deny applies to every principal");
  assert.deepEqual(principalDeny["Action"], clientActions, "the deny covers mount, write and root access");
  // Resolved to the role resource rather than matched as text: the policy names the task role this
  // stack creates, so the exempt principal has to be that role and not a role of the same shape.
  assert.equal(
    roleNameOf(resources, principalDeny["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]),
    SCHEDULER_TASK_ROLE_NAME,
    "the only exempt principal is the scheduler task role",
  );

  const allow = statements.find((statement) => statement["Effect"] === "Allow");
  assert.ok(allow !== undefined, "an allow statement");
  assert.equal(
    roleNameOf(resources, allow["Principal"]["AWS"]),
    SCHEDULER_TASK_ROLE_NAME,
    "the allow names the same task role",
  );

  // The condition matches the shape of an access point rather than naming one. Naming it is a
  // reference from the file system policy to the access point, and the access point already
  // refers to the file system, which CloudFormation refuses as a circular dependency. A mount can
  // only present an access point of the file system it is mounting, and this file system has one,
  // so the restriction is unchanged. A negated string condition is also true when the key is
  // absent, so a mount presenting no access point at all is still denied.
  const accessPointDeny = statements.find((statement) => statement["Condition"]?.["StringNotLike"] !== undefined);
  assert.ok(accessPointDeny !== undefined, "a statement conditioned on the access point");
  assert.equal(accessPointDeny["Effect"], "Deny", "the access-point statement denies");
  assert.deepEqual(accessPointDeny["Principal"], { AWS: "*" }, "the deny applies to every principal");
  assert.match(
    JSON.stringify(accessPointDeny["Condition"]["StringNotLike"]["elasticfilesystem:AccessPointArn"]),
    /:access-point\/\*/,
    "the exemption is keyed on the access point arn",
  );
  // The policy is a property of the file system, so naming the file system in it is a self
  // reference and the same refusal. Every statement therefore leaves the resource unqualified.
  for (const statement of statements) {
    assert.equal(statement["Resource"], "*", "no statement names the file system it belongs to");
  }

  assert.equal(
    statements.filter((statement) => statement["Effect"] === "Allow").length,
    1,
    "exactly one allow statement",
  );
});

test("the published target-group lists are in the order the module stacks index", () => {
  const resources = resourcesOf(synthEcs());
  const roles = (role: string): string[] => publishedTargetGroups(resources, role).map(targetGroupRole);

  // cluster-manager.ts reads 0 external, 1 internal, 2 web portal.
  assert.deepEqual(roles("cluster-manager"), ["e", "i", "w"], "cluster-manager external, internal, web portal");
  // scheduler.ts reads 0 external, 1 internal.
  assert.deepEqual(roles("scheduler"), ["e", "i"], "scheduler external, internal");
  // vdc.ts reads 0 external, 1 internal for the controller.
  assert.deepEqual(roles("vdc"), ["e", "i"], "vdc controller external, internal");
  // vdc.ts reads 0 client, 1 agent, 2 gateway for the broker.
  assert.deepEqual(roles("dcv-broker"), ["c", "a", "g"], "broker client, agent, gateway");
  // One gateway target group, the one matching the session protocol setting. The desktop stack
  // attaches only that one to its network load balancer, so publishing both left the other with
  // no load balancer for ever, and a service may not name a target group that has none. vdc.ts
  // reads entry 0 whichever protocol is configured.
  assert.deepEqual(roles("dcv-gateway"), ["TN"], "gateway without QUIC is plain tcp");

  // Every published entry is a target group this stack owns, and each appears once.
  const names = Object.values(targetGroupNames(resources));
  assert.equal(new Set(names).size, names.length, "target-group names are distinct");
});

test("each published broker and gateway target group listens on the port its listener forwards to", () => {
  const resources = resourcesOf(synthEcs());

  // The broker listeners on the internal load balancer are client, agent, gateway in that order.
  const brokerPorts: Array<[number, string]> = [
    [8444, "HTTPS"],
    [8445, "HTTPS"],
    [8446, "HTTPS"],
  ];
  const broker = publishedTargetGroups(resources, "dcv-broker");
  assert.equal(broker.length, brokerPorts.length, "three broker target groups");
  brokerPorts.forEach(([port, protocol], index) => {
    const properties = broker[index]?.["Properties"] as Json;
    assert.equal(properties["Port"], port, `dcv-broker.target_group_arns[${index}] port`);
    assert.equal(properties["Protocol"], protocol, `dcv-broker.target_group_arns[${index}] protocol`);
  });

  // One gateway target group, matching the configured session protocol. The synthetic settings
  // leave QUIC off, so it is the plain TCP one.
  const gateway = publishedTargetGroups(resources, "dcv-gateway");
  assert.equal(gateway.length, 1, "one gateway target group");
  const gatewayProperties = gateway[0]?.["Properties"] as Json;
  assert.equal(gatewayProperties["Protocol"], "TCP", "dcv-gateway.target_group_arns[0] protocol");
  assert.equal(gatewayProperties["Port"], 8443, "dcv-gateway.target_group_arns[0] port");
  assert.equal(gatewayProperties["HealthCheckPort"], "8989", "dcv-gateway.target_group_arns[0] health check port");
});

test("every task definition the stack creates is retained, observability included", () => {
  const resources = resourcesOf(synthEcs(true));
  const taskDefinitions = byType(resources, "AWS::ECS::TaskDefinition");

  assert.equal(taskDefinitions.length, 6, "five application task definitions and the observability daemon");
  for (const [id, taskDefinition] of taskDefinitions) {
    assert.equal(taskDefinition["DeletionPolicy"], "Retain", `${id} keeps its revision for a rollback`);
    assert.equal(taskDefinition["UpdateReplacePolicy"], "Retain", `${id} keeps its revision on replacement`);
  }
});
