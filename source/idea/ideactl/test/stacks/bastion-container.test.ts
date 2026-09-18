import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { byType, cleanupWorkdirs, onlyOne, resourcesOf, synthBastion } from '../support/ecs-harness.ts';
import { evaluateChangeSet } from '../../src/cli/cdk-invoker.ts';

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
  const verdict = evaluateChangeSet({ Changes: [
    { ResourceChange: { Action: 'Remove', LogicalResourceId: 'bastionhostinstance', ResourceType: 'AWS::EC2::Instance' } },
    { ResourceChange: { Action: 'Modify', LogicalResourceId: 'bastionTask', ResourceType: 'AWS::ECS::TaskDefinition', Replacement: 'True' } },
    { ResourceChange: { Action: 'Modify', LogicalResourceId: 'bastionRecord', ResourceType: 'AWS::Route53::RecordSet', Replacement: 'False' } },
  ] });
  assert.deepEqual(verdict.refusals, []);
});

test('cutover updates the existing DNS and settings resources and deletion releases endpoints and keys', () => {
  const host = resourcesOf(synthBastion({
    'ecs.enabled': false,
    'bastion-host.base_os': 'amazonlinux2023',
    'bastion-host.instance_ami': 'ami-0123456789abcdef0',
    'bastion-host.instance_type': 'm7i.large',
    'bastion-host.ec2.metadata_http_tokens': 'required',
    'cluster.network.ssh_key_pair': 'sample-key',
    'global-settings.module_sets.default.bastion-host.module_id': 'bastion-host',
  }));
  const container = resourcesOf(synthBastion());
  for (const type of ['AWS::Route53::RecordSet', 'Custom::ClusterSettings']) {
    assert.equal(one(host, type)[0], one(container, type)[0]);
  }
  for (const type of ['AWS::EC2::EIP', 'AWS::SecretsManager::Secret', 'AWS::ElasticLoadBalancingV2::LoadBalancer', 'AWS::ECS::Service']) {
    for (const [, resource] of byType(container, type)) assert.notEqual(resource.DeletionPolicy, 'Retain', type);
  }
  const removals = Object.entries(host).filter(([id]) => container[id] === undefined).map(([id, resource]) => ({
    ResourceChange: { Action: 'Remove', LogicalResourceId: id, ResourceType: resource.Type },
  }));
  assert.deepEqual(evaluateChangeSet({ Changes: removals }).refusals, []);
});

test('the container bastion synthesizes while the upgrade holds the ecs module-set row', () => {
  // An upgrade deletes global-settings.module_sets.*.ecs.module_id in Phase 2 and writes it back
  // only after the last stack has deployed, so the old portal keeps working through the window.
  // The bastion stack must not resolve the ecs module through that row: the first live run of this
  // stack stopped at synthesis on exactly that lookup.
  const template = synthBastion({ 'global-settings.module_sets.default.ecs.module_id': null });
  const service = one(resourcesOf(template), 'AWS::ECS::Service');
  assert.ok(service, 'the service still synthesizes');
  assert.match(JSON.stringify(template), /\/ecs\/exec/);
});

test('the container bastion publishes no boolean and never its own public flag', () => {
  // Custom resource properties reach the handler as strings; a published boolean overwrote the
  // typed values row on the first live run and failed the completion read-back.
  const settings = one(resourcesOf(synthBastion()), 'Custom::ClusterSettings')[1] as { Properties: { settings: Record<string, unknown> } };
  const published = settings.Properties.settings;
  assert.equal(published['public'], undefined);
  for (const [key, value] of Object.entries(published)) assert.notEqual(typeof value, 'boolean', key);
});
