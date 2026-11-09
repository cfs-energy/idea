import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, test } from 'node:test';
import { evaluateChangeSet } from '../../src/cli/cdk-invoker.ts';
import { byType, cleanupWorkdirs, onlyOne, resourcesOf, synthBastion } from '../support/ecs-harness.ts';

after(cleanupWorkdirs);

const one = (resources: ReturnType<typeof resourcesOf>, type: string) => onlyOne(byType(resources, type), type);

test('bastion uses stable public addresses and an SSH service on shared capacity', () => {
  const resources = resourcesOf(synthBastion());
  assert.equal(byType(resources, 'AWS::EC2::Instance').length, 0);
  assert.equal(byType(resources, 'AWS::EC2::LaunchTemplate').length, 0);
  assert.equal(byType(resources, 'AWS::IAM::InstanceProfile').length, 0);
  const addresses = byType(resources, 'AWS::EC2::EIP');
  assert.equal(addresses.length, 2);
  const [, nlb] = one(resources, 'AWS::ElasticLoadBalancingV2::LoadBalancer');
  assert.equal(nlb.Properties.Scheme, 'internet-facing');
  assert.deepEqual(nlb.Properties.SubnetMappings.map((entry: any) => entry.AllocationId), addresses.map(([id]) => ({ 'Fn::GetAtt': [id, 'AllocationId'] })));
  assert.equal(nlb.Properties.Subnets, undefined);
  assert.deepEqual(nlb.Properties.SecurityGroups, ['sg-0123456789abcdef8']);
  const [, listener] = one(resources, 'AWS::ElasticLoadBalancingV2::Listener');
  assert.equal(listener.Properties.Port, 22);
  assert.equal(listener.Properties.Protocol, 'TCP');
  const [, group] = one(resources, 'AWS::ElasticLoadBalancingV2::TargetGroup');
  assert.equal(group.Properties.TargetType, 'ip');
  assert.ok(group.Properties.TargetGroupAttributes.some((entry: any) => entry.Key === 'preserve_client_ip.enabled' && entry.Value === 'true'));
  const [, service] = one(resources, 'AWS::ECS::Service');
  assert.equal(service.Properties.DesiredCount, 2);
  assert.equal(service.Properties.CapacityProviderStrategy.length, 1);
  assert.ok(service.Properties.NetworkConfiguration.AwsvpcConfiguration.SecurityGroups.includes('sg-0123456789abcdef8'));
  const [, task] = one(resources, 'AWS::ECS::TaskDefinition');
  assert.equal(task.Properties.NetworkMode, 'awsvpc');
  assert.equal(task.Properties.RuntimePlatform.CpuArchitecture, 'ARM64');
  const container = task.Properties.ContainerDefinitions[0];
  assert.equal(container.Privileged, undefined);
  assert.deepEqual(container.PortMappings, [{ ContainerPort: 22, Protocol: 'tcp' }]);
  assert.ok(container.MountPoints.some((mount: any) => mount.ContainerPath === '/data'));
  assert.ok(container.MountPoints.some((mount: any) => mount.ContainerPath === '/apps'));
  assert.ok(!task.Properties.Volumes.some((volume: any) => ['/var/run/docker.sock', '/'].includes(volume.Host?.SourcePath)));
  const [settingsId, settings] = one(resources, 'Custom::ClusterSettings');
  assert.ok(service.DependsOn.includes(settingsId));
  assert.equal(settings.Properties.settings.instance_id, undefined);
  assert.equal(settings.Properties.settings.private_ip, undefined);
  assert.equal(settings.Properties.settings.instance_profile_arn, undefined);
  assert.deepEqual(settings.Properties.settings.public_ip, { Ref: addresses[0]![0] });
  assert.equal(settings.Properties.settings.public_ips.length, 2);
  const [, record] = one(resources, 'AWS::Route53::RecordSet');
  assert.ok(record.Properties.AliasTarget);
  assert.equal(record.Properties.ResourceRecords, undefined);
});

for (const overrides of [{ 'bastion-host.public': false }, { 'cluster.network.public_subnets': [] }]) {
  test(`private placement has no EIPs: ${JSON.stringify(overrides)}`, () => {
    const resources = resourcesOf(synthBastion(overrides));
    assert.equal(byType(resources, 'AWS::EC2::EIP').length, 0);
    assert.equal(one(resources, 'AWS::ElasticLoadBalancingV2::LoadBalancer')[1].Properties.Scheme, 'internal');
    const settings = one(resources, 'Custom::ClusterSettings')[1].Properties.settings;
    assert.equal(settings.public, undefined);
    assert.equal(settings.public_ip, undefined);
    assert.ok(settings.private_dns_name);
  });
}

for (const provider of ['openldap', 'activedirectory', 'aws_managed_activedirectory']) {
  test(`secret permissions and task identity are constrained for ${provider}`, () => {
    const resources = resourcesOf(synthBastion({ 'directoryservice.provider': provider }));
    const [secretId, secret] = one(resources, 'AWS::SecretsManager::Secret');
    assert.ok(secret.Properties.Tags.some((tag: any) => tag.Key === 'idea:ModuleName' && tag.Value === 'bastion-host'));
    assert.ok(secret.Properties.Tags.some((tag: any) => tag.Key === 'idea:ClusterName'));
    const role = byType(resources, 'AWS::IAM::Role').find(([, resource]) => resource.Properties.RoleName.includes('bastion-host-task-role'))!;
    assert.ok(role[1].Properties.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals['aws:SourceAccount']);
    assert.equal(role[1].Properties.ManagedPolicyArns, undefined);
    const policies = byType(resources, 'AWS::IAM::Policy').filter(([, policy]) => policy.Properties.Roles.some((entry: any) => entry.Ref === role[0]));
    const statements = policies.flatMap(([, policy]) => policy.Properties.PolicyDocument.Statement);
    const secretStatements = statements.filter((entry: any) => JSON.stringify(entry.Action).includes('secretsmanager:'));
    assert.equal(secretStatements.length, 1);
    assert.deepEqual(secretStatements[0].Resource, { Ref: secretId });
    assert.deepEqual(secretStatements[0].Action, ['secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue']);
    if (provider !== 'openldap') {
      assert.ok(statements.some((entry: any) => entry.Condition?.['ForAllValues:StringEquals']?.['dynamodb:LeadingKeys']?.[0] === '${aws:userid}'));
    }
    const container = one(resources, 'AWS::ECS::TaskDefinition')[1].Properties.ContainerDefinitions[0];
    assert.ok(!JSON.stringify(container.Secrets ?? []).includes('password'));
  });
}

test('unsupported directories and zero desired tasks fail synthesis', () => {
  assert.throws(() => synthBastion({ 'directoryservice.provider': 'unsupported' }), /requires an OpenLDAP or Active Directory/);
  assert.throws(() => synthBastion({ 'ecs.tasks.bastion-host.desired': 0 }), /at least 1/);
});

test('host retirement and subsequent task revisions need no replacement override', () => {
  const verdict = evaluateChangeSet({
    Changes: [
      { ResourceChange: { Action: 'Remove', LogicalResourceId: 'bastionhostinstance', ResourceType: 'AWS::EC2::Instance' } },
      { ResourceChange: { Action: 'Modify', LogicalResourceId: 'bastionTask', ResourceType: 'AWS::ECS::TaskDefinition', Replacement: 'True' } },
      { ResourceChange: { Action: 'Modify', LogicalResourceId: 'bastionRecord', ResourceType: 'AWS::Route53::RecordSet', Replacement: 'False' } },
    ]
  });
  assert.deepEqual(verdict.refusals, []);
});

test('deletion releases bastion endpoints and keys', () => {
  const container = resourcesOf(synthBastion());
  for (const type of ['AWS::EC2::EIP', 'AWS::SecretsManager::Secret', 'AWS::ElasticLoadBalancingV2::LoadBalancer', 'AWS::ECS::Service']) {
    for (const [, resource] of byType(container, type)) assert.notEqual(resource.DeletionPolicy, 'Retain', type);
  }
});

test('the container bastion publishes no boolean and never its own public flag', () => {
  // Custom resource properties reach the handler as strings; a published boolean overwrote the
  // typed values row on the first live run and failed the completion read-back.
  const settings = one(resourcesOf(synthBastion()), 'Custom::ClusterSettings')[1] as { Properties: { settings: Record<string, unknown> } };
  const published = settings.Properties.settings;
  assert.equal(published['public'], undefined);
  for (const [key, value] of Object.entries(published)) assert.notEqual(typeof value, 'boolean', key);
});

test("bastion placement requires a joined directory client on the host", () => {
  const [, service] = one(resourcesOf(synthBastion()), "AWS::ECS::Service");
  assert.ok(service.Properties.PlacementConstraints.some((constraint: any) => constraint.Type === "memberOf" && constraint.Expression === "attribute:idea.directory == joined"));
});

test('sshd exempts the VPC and the cluster subnets from per-source penalties', () => {
  const [, task] = one(resourcesOf(synthBastion()), 'AWS::ECS::TaskDefinition');
  const environment = task.Properties.ContainerDefinitions[0].Environment;
  const cidrs = environment.find((entry: any) => entry.Name === 'IDEA_VPC_CIDRS')?.Value;
  // the public subnets carry the load balancer's health checks and may sit outside the primary CIDR
  assert.deepEqual(cidrs.split(','), ['192.0.2.0/24', '198.51.100.0/25', '198.51.100.128/25', '192.0.2.0/25', '192.0.2.128/25']);
});

test('sshd exempts the SSH allowlist the bastion prefix list holds', () => {
  const environmentOf = (overrides: Record<string, unknown>) => {
    const [, task] = one(resourcesOf(synthBastion(overrides)), 'AWS::ECS::TaskDefinition');
    return task.Properties.ContainerDefinitions[0].Environment.find((entry: any) => entry.Name === 'IDEA_SSH_CLIENT_CIDRS')?.Value;
  };
  // bare addresses become /32 as in the prefix list; the script, not synthesis, drops 0.0.0.0/0
  assert.equal(environmentOf({ 'cluster.network.ssh_client_ip': ['23.23.20.238', '198.51.100.0/24', '23.23.20.238/32', '0.0.0.0/0'] }),
    '23.23.20.238/32,198.51.100.0/24,0.0.0.0/0');
  assert.equal(environmentOf({}), '');
});

test('the image takes the newest OpenSSH past its base release, and the release proves it', () => {
  const root = new URL('../../../../../', import.meta.url);
  const dockerfile = readFileSync(new URL('deployment/ecr/idea-control-plane/Dockerfile', root), 'utf8');
  const workflow = readFileSync(new URL('.github/workflows/build_push.yaml', root), 'utf8');
  const finalStage = dockerfile.slice(dockerfile.lastIndexOf('\nFROM '));
  assert.match(finalStage, /ARG IDEA_PACKAGE_REFRESH=[^]*?\$\{IDEA_PACKAGE_REFRESH\}[^]*?dnf upgrade -y --releasever=latest 'openssh\*'/);
  assert.match(finalStage, /dnf upgrade -y --releasever=latest --security/);
  // the layer has to run on each release build, or the cache keeps the first answer
  assert.match(workflow, /--build-arg IDEA_PACKAGE_REFRESH="\$\{GITHUB_RUN_ID\}-\$\{GITHUB_RUN_ATTEMPT\}"/);
  assert.match(workflow, /dnf -q check-update --releasever=latest 'openssh\*'/);
});

test('ideactl in a task shell is the documented CLI, and the scheduler commands still work', () => {
  const root = new URL('../../../../../', import.meta.url);
  const dockerfile = readFileSync(new URL('deployment/ecr/idea-control-plane/Dockerfile', root), 'utf8');
  const finalStage = dockerfile.slice(dockerfile.lastIndexOf('\nFROM '));
  // after the module install, whose console script of the same name it replaces
  assert.ok(finalStage.indexOf('COPY deployment/ecr/idea-control-plane/ideactl.sh /usr/local/bin/ideactl') > finalStage.indexOf('pip install --no-cache-dir "${PKG}"'));
  const bin = mkdtempSync(join(tmpdir(), 'ideactl-'));
  try {
    for (const name of ['node', 'idea-scheduler-cli']) {
      writeFileSync(join(bin, name), `#!/bin/bash\necho ${name} "$@"\n`);
      chmodSync(join(bin, name), 0o755);
    }
    const run = (...args: string[]) => execFileSync('bash', [new URL('deployment/ecr/idea-control-plane/ideactl.sh', root).pathname, ...args], {
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, IDEACTL_MAIN: 'main.js' },
      encoding: 'utf8',
    }).trim();
    assert.equal(run('about'), 'node main.js about');
    assert.equal(run(), 'node main.js');
    assert.equal(run('jobs', 'list'), 'idea-scheduler-cli jobs list');
    assert.equal(run('ami-builder'), 'idea-scheduler-cli ami-builder');
    // they reach the scheduler running in its own task; anywhere else they say where to go
    const inTask = (role: string, ...args: string[]) => spawnSync('bash', [new URL('deployment/ecr/idea-control-plane/ideactl.sh', root).pathname, ...args], {
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, IDEACTL_MAIN: 'main.js', IDEA_CONTAINER_ROLE: role },
      encoding: 'utf8',
    });
    assert.equal(inTask('scheduler', 'jobs', 'list').stdout.trim(), 'idea-scheduler-cli jobs list');
    for (const role of ['cluster-manager', 'bastion-host']) {
      const refused = inTask(role, 'jobs', 'list');
      assert.equal(refused.status, 2);
      assert.equal(refused.stdout, '');
      assert.equal(refused.stderr.trim(), `ideactl jobs runs in the scheduler task's shell (container scheduler-container); this is the ${role} task`);
      assert.equal(inTask(role, 'about').stdout.trim(), 'node main.js about');
    }
  } finally {
    rmSync(bin, { recursive: true, force: true });
  }
});
