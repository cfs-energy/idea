/**
 * Deployment controls on the container services that no other suite pins.
 *
 * Each test asserts the property by value, so a reworded setting with the wrong value still fails.
 * The controls are the ones whose loss is silent: a task that inherits the container host security
 * group, a service that cannot roll itself back, a host that lets a task reach instance metadata,
 * and an endpoint that forwards to the wrong one of several identical target groups.
 *
 * The five services live in the three module stacks, because each starts only after its own
 * module has published the rows its application reads at boot. The shared capacity controls are
 * asserted against the container stack.
 */

import assert from "node:assert/strict";
import { after, test } from "node:test";

import { requireCapture } from "../support/fixtures.ts";
import {
  CONFIG_FILE,
  CONTEXT_FILE,
  ECS_CLUSTER,
  FIXTURE_CLUSTER,
  REGION,
  SYNTH_READS_FILE,
  TARGET_GROUP_HASH,
  byType,
  cleanupWorkdirs,
  onlyOne,
  resourcesOf,
  synthContainerStacks,
  synthEcs,
  type ContainerTemplates,
  type Json,
} from "../support/ecs-harness.ts";

requireCapture(
  [CONFIG_FILE, SYNTH_READS_FILE, CONTEXT_FILE],
  "node tools/parity/capture.ts --live --cluster idea-dev27 --region us-east-2",
);

after(cleanupWorkdirs);

/** Container role to the module stack that owns its service, and the group the task ENI carries. */
const SERVICES = [
  { role: "cluster-manager", stack: "clusterManager", securityGroup: "cluster-manager-security-group" },
  { role: "scheduler", stack: "scheduler", securityGroup: "scheduler-security-group" },
  { role: "vdc", stack: "vdc", securityGroup: "vdc-controller-security-group" },
  { role: "dcv-broker", stack: "vdc", securityGroup: "vdc-broker-security-group" },
  { role: "dcv-gateway", stack: "vdc", securityGroup: "vdc-gateway-security-group" },
] as const;

const CONTAINER_HOST_SECURITY_GROUP_NAME = `${ECS_CLUSTER}-ecs-host-security-group`;

/** The scheduler task role the PBS file-system policy names. */
const SCHEDULER_TASK_ROLE_NAME = `${FIXTURE_CLUSTER}-scheduler-task-role-${REGION}`;

let templates: ContainerTemplates | undefined;
async function stacks(): Promise<ContainerTemplates> {
  templates ??= await synthContainerStacks();
  return templates;
}

/** Logical id to `GroupName` for every security group in a template. */
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

/** The task definition of one container role. */
function taskDefinitionOf(resources: Record<string, Json>, role: string): Json {
  const found = byType(resources, "AWS::ECS::TaskDefinition").find(
    ([, resource]) => roleOfTaskDefinition(resource) === role,
  );
  assert.ok(found !== undefined, `${role} task definition`);
  return found[1];
}

test("the broker service registers each target group on the port that target group fronts", async () => {
  // Three listeners front the broker (client, agent, gateway) on three ports. Registering every
  // target group on the container's default port sends the session-manager agent to the client
  // port, where it reads "JSON Error: EOF" forever and no desktop reaches READY.
  const resources = resourcesOf((await stacks())["vdc"]);
  const service = servicesByRole(resources)["dcv-broker"];
  const groupPorts = new Map(
    byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup").map(([id, group]) => [id, group["Properties"]["Port"] as number]),
  );
  const registered = (service["Properties"]["LoadBalancers"] as Json[]).map((entry) => ({
    containerPort: entry["ContainerPort"] as number,
    groupPort: groupPorts.get((entry["TargetGroupArn"] as { Ref: string })["Ref"]),
  }));
  assert.equal(registered.length, 3, JSON.stringify(registered));
  for (const entry of registered) assert.equal(entry.containerPort, entry.groupPort, JSON.stringify(entry));
  assert.deepEqual(registered.map((entry) => entry.containerPort).sort(), [8444, 8445, 8446]);
});

test("each task runs in the host security group of the component it replaces", async () => {
  const all = await stacks();

  for (const { role, stack, securityGroup } of SERVICES) {
    const resources = resourcesOf(all[stack]);
    const groupNames = securityGroupNames(resources);
    const service = servicesByRole(resources)[role];
    assert.ok(service !== undefined, `${role} service`);
    const attached = service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"] as Json[];
    assert.deepEqual(
      attached.map((entry) => groupNames[(entry["Fn::GetAtt"] as string[])[0] as string]),
      [`${FIXTURE_CLUSTER}-${securityGroup}`],
      `${role} task ENI carries the host group of its own component`,
    );
  }

  // The container host group belongs to the container stack and is never on a task interface. It
  // is not in any module template at all, in either spelling.
  for (const stack of ["clusterManager", "scheduler", "vdc"] as const) {
    const resources = resourcesOf(all[stack]);
    assert.equal(
      Object.values(securityGroupNames(resources)).includes(CONTAINER_HOST_SECURITY_GROUP_NAME),
      false,
      `${stack} creates no container host group`,
    );
  }
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

test("each service gets a start allowance sized from its own start, not the library default", async () => {
  const all = await stacks();

  for (const { role, stack } of SERVICES) {
    const services = servicesByRole(resourcesOf(all[stack]));
    assert.equal(
      services[role]?.["Properties"]["HealthCheckGracePeriodSeconds"],
      (START_ALLOWANCE_SECONDS[role] as number) + REGISTRATION_SECONDS,
      `${role} service start allowance`,
    );
  }

  // The container health check has no allowance but its own start period, on a first start and on
  // every replacement, so it covers the start without the registration.
  const scheduler = taskDefinitionOf(resourcesOf(all.scheduler), "scheduler");
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
  assert.ok(
    JSON.stringify(checked[0]).includes("qstat -B && curl"),
    "the scheduler health check calls qstat before the module API",
  );
  assert.ok(
    JSON.stringify(checked[0]).includes("--unix-socket /run/idea.sock"),
    "the scheduler health check uses the local socket",
  );
  assert.ok(
    JSON.stringify(checked[0]).includes("Scheduler.ListActiveJobs"),
    "the scheduler health check names Scheduler.ListActiveJobs",
  );

  // The observability daemon has no load balancer, and a grace without one is rejected on update.
  const daemon = byType(resourcesOf(synthEcs(true)), "AWS::ECS::Service").find(
    ([, service]) => service["Properties"]["SchedulingStrategy"] === "DAEMON",
  );
  assert.ok(daemon !== undefined, "the observability daemon service");
  assert.equal(
    daemon[1]["Properties"]["HealthCheckGracePeriodSeconds"],
    undefined,
    "a service with no load balancer carries no grace",
  );
});

test("every application service rolls its own failed deployment back", async () => {
  const all = await stacks();

  for (const { role, stack } of SERVICES) {
    const deployment = servicesByRole(resourcesOf(all[stack]))[role]?.["Properties"]["DeploymentConfiguration"] as
      | Json
      | undefined;
    assert.deepEqual(
      deployment?.["DeploymentCircuitBreaker"],
      { Enable: true, Rollback: true },
      `${role} service deployment circuit breaker`,
    );
  }

  // The batch server holds a single-writer lock on its state directory, so two scheduler tasks
  // cannot run at once: the second to start fails to take the lock. A maximum of one hundred
  // leaves no room for a replacement to start before the running task stops.
  const scheduler = servicesByRole(resourcesOf(all.scheduler))["scheduler"];
  const schedulerDeployment = scheduler?.["Properties"]["DeploymentConfiguration"] as Json;
  assert.equal(schedulerDeployment["MinimumHealthyPercent"], 0, "scheduler minimum healthy percent");
  assert.equal(schedulerDeployment["MaximumPercent"], 100, "scheduler replacement stops before it starts");
  assert.equal(
    scheduler?.["Properties"]["PlacementConstraints"],
    undefined,
    "the one scheduler task carries no distinct-instance constraint",
  );

  // Every other service spreads across hosts, so one host loss never takes a whole role.
  for (const { role, stack } of SERVICES.filter((entry) => entry.role !== "scheduler")) {
    const service = servicesByRole(resourcesOf(all[stack]))[role];
    assert.deepEqual(
      service?.["Properties"]["PlacementConstraints"],
      [{ Type: "distinctInstance" }],
      `${role} spreads across hosts`,
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

/** Every role that trusts the task service, by physical name, per stack. */
const TASK_TRUSTING_ROLE_NAMES: Record<"clusterManager" | "scheduler" | "vdc", string[]> = {
  clusterManager: [
    `${FIXTURE_CLUSTER}-cluster-manager-task-execution-role-${REGION}`,
    `${FIXTURE_CLUSTER}-cluster-manager-task-role-${REGION}`,
  ],
  scheduler: [
    `${FIXTURE_CLUSTER}-scheduler-task-execution-role-${REGION}`,
    `${FIXTURE_CLUSTER}-scheduler-task-role-${REGION}`,
  ],
  vdc: [
    `${FIXTURE_CLUSTER}-vdc-broker-task-execution-role-${REGION}`,
    `${FIXTURE_CLUSTER}-vdc-broker-task-role-${REGION}`,
    `${FIXTURE_CLUSTER}-vdc-controller-task-execution-role-${REGION}`,
    `${FIXTURE_CLUSTER}-vdc-controller-task-role-${REGION}`,
    `${FIXTURE_CLUSTER}-vdc-gateway-task-execution-role-${REGION}`,
    `${FIXTURE_CLUSTER}-vdc-gateway-task-role-${REGION}`,
  ],
};

test("every role that trusts the task service is scoped to this account", async () => {
  const all = await stacks();

  for (const stack of ["clusterManager", "scheduler", "vdc"] as const) {
    const resources = resourcesOf(all[stack]);
    const trusting = byType(resources, "AWS::IAM::Role").filter(([, role]) =>
      JSON.stringify(role["Properties"]["AssumeRolePolicyDocument"]).includes("ecs-tasks.amazonaws.com"),
    );
    // Named rather than counted, so a role that gains or loses this trust fails here by name. The
    // module instance roles are in the same stack and must not be among them.
    assert.deepEqual(
      trusting.map(([, role]) => role["Properties"]["RoleName"]).sort(),
      TASK_TRUSTING_ROLE_NAMES[stack],
      `${stack} roles trusting the task service, by name`,
    );
    for (const [id, role] of trusting) {
      // The account is the replayed fixture's own, so it is read off the template rather than
      // written here. The shape is what matters: one statement, and a source-account condition on
      // it, which is the confused-deputy guard a generated trust policy would not carry.
      const statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"] as Json[];
      assert.equal(statements.length, 1, `${id} has one trust statement`);
      const account = statements[0]?.["Condition"]?.["StringEquals"]?.["aws:SourceAccount"] as unknown;
      assert.match(String(account), /^\d{12}$/, `${id} trust names an account`);
      assert.deepEqual(
        statements,
        [
          {
            Action: "sts:AssumeRole",
            Condition: { StringEquals: { "aws:SourceAccount": account } },
            Effect: "Allow",
            Principal: { Service: "ecs-tasks.amazonaws.com" },
          },
        ],
        `${id} trusts ecs-tasks only from this account`,
      );
    }

    // Every execution role a task definition names is one of those roles.
    for (const [id, taskDefinition] of byType(resources, "AWS::ECS::TaskDefinition")) {
      const arn = taskDefinition["Properties"]["ExecutionRoleArn"] as Json | undefined;
      const executionRoleId = (arn?.["Fn::GetAtt"] as string[] | undefined)?.[0];
      assert.ok(
        executionRoleId !== undefined && trusting.some(([roleId]) => roleId === executionRoleId),
        `${id} names an execution role that trusts the task service`,
      );
    }
  }

  // With the observability daemon off the container stack creates no task identity at all.
  const containerRoles = byType(resourcesOf(synthEcs()), "AWS::IAM::Role").filter(([, role]) =>
    JSON.stringify(role["Properties"]["AssumeRolePolicyDocument"]).includes("ecs-tasks.amazonaws.com"),
  );
  assert.deepEqual(containerRoles, [], "the container stack creates no task identity");
});

/** The `RoleName` of the role a `Fn::GetAtt` refers to, so a policy can be checked by value. */
function roleNameOf(resources: Record<string, Json>, value: unknown): string | undefined {
  const getAtt = (value as Json | undefined)?.["Fn::GetAtt"];
  const id = Array.isArray(getAtt) ? (getAtt[0] as string) : undefined;
  if (id === undefined) return undefined;
  const role = resources[id];
  return role?.["Type"] === "AWS::IAM::Role" ? (role["Properties"]["RoleName"] as string) : undefined;
}

test("the broker task role can read the settings rows its role script renders", async () => {
  const { vdc } = await stacks();
  const resources = resourcesOf(vdc);
  const policies = byType(resources, "AWS::IAM::Policy").filter(([, policy]) =>
    JSON.stringify(policy["Properties"]["Roles"]).includes("dcvbrokertaskrole"),
  );
  const statements = policies.flatMap(([, policy]) => policy["Properties"]["PolicyDocument"]["Statement"] as Array<Record<string, unknown>>);
  const grant = statements.find((statement) => JSON.stringify(statement["Action"]).includes("dynamodb:GetItem") && JSON.stringify(statement["Resource"]).includes(`${FIXTURE_CLUSTER}.cluster-settings`));
  assert.ok(grant, "the broker role script reads its ports with dynamodb get-item; without this grant the task exits and the deployment circuit breaker rolls the stack back");
});

test("the controller task can pass the desktop host role and the SSM command role", async () => {
  const { vdc } = await stacks();
  const resources = resourcesOf(vdc);
  const statements = byType(resources, "AWS::IAM::Policy")
    .filter(([, policy]) => JSON.stringify(policy["Properties"]["Roles"]).includes("controllertaskrole"))
    .flatMap(([, policy]) => policy["Properties"]["PolicyDocument"]["Statement"] as Array<Record<string, unknown>>)
    .filter((statement) => JSON.stringify(statement["Action"]).includes("iam:PassRole"));
  const passed = JSON.stringify(statements.map((statement) => statement["Resource"]));
  for (const role of ["vdchostrole", "ssmcommandssnstopicrole"]) {
    assert.ok(passed.toLowerCase().includes(role), `${role}: without this pass the controller task cannot launch a desktop on a cluster without bedrock project roles`);
  }
});

test("the PBS file system denies every principal but the scheduler task role", async () => {
  const resources = resourcesOf((await stacks()).scheduler);
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

/** Endpoint logical id to the target-group identifier its forward action must name. */
const ENDPOINT_TARGETS: Record<"clusterManager" | "scheduler" | "vdc", Record<string, string>> = {
  clusterManager: {
    externalendpoint: `${FIXTURE_CLUSTER}-cm-ecs-e-${TARGET_GROUP_HASH["cluster-manager"]}`,
    internalendpoint: `${FIXTURE_CLUSTER}-cm-ecs-i-${TARGET_GROUP_HASH["cluster-manager"]}`,
    webportalendpoint: `${FIXTURE_CLUSTER}-cm-ecs-w-${TARGET_GROUP_HASH["cluster-manager"]}`,
  },
  scheduler: {
    externalendpoint: `${FIXTURE_CLUSTER}-sched-ecs-e-${TARGET_GROUP_HASH["scheduler"]}`,
    internalendpoint: `${FIXTURE_CLUSTER}-sched-ecs-i-${TARGET_GROUP_HASH["scheduler"]}`,
  },
  vdc: {
    controllerendpointext: `${FIXTURE_CLUSTER}-vdc-ecs-e-${TARGET_GROUP_HASH["vdc"]}`,
    controllerendpointint: `${FIXTURE_CLUSTER}-vdc-ecs-i-${TARGET_GROUP_HASH["vdc"]}`,
    dcvbrokerclientendpoint: `${FIXTURE_CLUSTER}-brk-ecs-c-${TARGET_GROUP_HASH["vdc"]}`,
    dcvbrokeragentendpoint: `${FIXTURE_CLUSTER}-brk-ecs-a-${TARGET_GROUP_HASH["vdc"]}`,
    dcvbrokergatewayendpoint: `${FIXTURE_CLUSTER}-brk-ecs-g-${TARGET_GROUP_HASH["vdc"]}`,
  },
};

test("each endpoint forwards to the target group its own listener serves", async () => {
  const all = await stacks();

  for (const stack of ["clusterManager", "scheduler", "vdc"] as const) {
    const resources = resourcesOf(all[stack]);
    const names = Object.fromEntries(
      byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup").map(([id, resource]) => [
        id,
        resource["Properties"]["Name"] as string,
      ]),
    );
    for (const [endpointId, expected] of Object.entries(ENDPOINT_TARGETS[stack])) {
      const endpoint = resources[endpointId];
      assert.ok(endpoint !== undefined, `${stack} has ${endpointId}`);
      const actions = endpoint["Properties"]["actions"] as Json[];
      const arn = actions[0]?.["TargetGroupArn"] as Json;
      const referenced = arn["Ref"] as string | undefined;
      assert.ok(referenced !== undefined, `${endpointId} forwards to a target group of this stack`);
      assert.equal(names[referenced], expected, `${endpointId} forwards to ${expected}`);
    }

    // Target-group names are distinct within the stack, so two endpoints cannot silently share one.
    const values = Object.values(names);
    assert.equal(new Set(values).size, values.length, `${stack} target-group names are distinct`);
  }
});

test("each broker target group listens on the port its listener forwards to", async () => {
  const resources = resourcesOf((await stacks()).vdc);
  const byName = new Map(
    byType(resources, "AWS::ElasticLoadBalancingV2::TargetGroup").map(([, resource]) => [
      resource["Properties"]["Name"] as string,
      resource["Properties"] as Json,
    ]),
  );

  // The broker listeners on the internal load balancer are client, agent, gateway in that order.
  const brokerPorts: Array<[string, number]> = [
    ["c", 8444],
    ["a", 8445],
    ["g", 8446],
  ];
  for (const [suffix, port] of brokerPorts) {
    const properties = byName.get(`${FIXTURE_CLUSTER}-brk-ecs-${suffix}-${TARGET_GROUP_HASH["vdc"]}`);
    assert.ok(properties !== undefined, `brk-ecs-${suffix} target group`);
    assert.equal(properties["Port"], port, `brk-ecs-${suffix} port`);
    assert.equal(properties["Protocol"], "HTTPS", `brk-ecs-${suffix} protocol`);
  }
});

test("every task definition is retained, observability included", async () => {
  const all = await stacks();
  const counts: Record<string, number> = { clusterManager: 1, scheduler: 1, vdc: 3 };

  for (const stack of ["clusterManager", "scheduler", "vdc"] as const) {
    const taskDefinitions = byType(resourcesOf(all[stack]), "AWS::ECS::TaskDefinition");
    assert.equal(taskDefinitions.length, counts[stack], `${stack} task definitions`);
    for (const [id, taskDefinition] of taskDefinitions) {
      assert.equal(taskDefinition["DeletionPolicy"], "Retain", `${id} keeps its revision for a rollback`);
      assert.equal(taskDefinition["UpdateReplacePolicy"], "Retain", `${id} keeps its revision on replacement`);
      assert.deepEqual(
        taskDefinition["Properties"]["RuntimePlatform"],
        { CpuArchitecture: "ARM64", OperatingSystemFamily: "LINUX" },
        `${id} follows the architecture the container stack published`,
      );
    }
  }

  const observability = byType(resourcesOf(synthEcs(true)), "AWS::ECS::TaskDefinition");
  assert.equal(observability.length, 1, "the observability daemon task definition");
  assert.equal(observability[0]?.[1]["DeletionPolicy"], "Retain", "the daemon keeps its revision");
  assert.equal(observability[0]?.[1]["UpdateReplacePolicy"], "Retain", "the daemon keeps it on replacement");
});
