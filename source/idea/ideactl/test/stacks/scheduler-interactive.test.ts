import assert from 'node:assert/strict';
import { test } from 'node:test';
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { SchedulerStack } from '../../src/cdk/stacks/scheduler.ts';
import { ClusterConfig } from '../../src/config/cluster-config.ts';

for (const hostsPresent of [false, true]) {
  test(`interactive connect-back reaches only the bastion from compute nodes (hosts=${hostsPresent})`, () => {
    const stack = new Stack(new App(), 'scheduler');
    const vpc = ec2.Vpc.fromVpcAttributes(stack, 'vpc', {
      vpcId: 'vpc-0123456789abcdef0', vpcCidrBlock: '192.0.2.0/24', availabilityZones: ['us-east-2a'],
    });
    const groups = {
      'bastion-host': ec2.SecurityGroup.fromSecurityGroupId(stack, 'bastion', 'sg-0123456789abcdef1'),
      'external-load-balancer': ec2.SecurityGroup.fromSecurityGroupId(stack, 'alb', 'sg-0123456789abcdef2'),
    };
    // Synthesize the real networking method without private deployment fixtures or AWS lookups.
    const scheduler = Object.create(SchedulerStack.prototype) as SchedulerStack;
    Object.defineProperties(scheduler, {
      stack: { value: stack }, moduleId: { value: 'scheduler' }, hostsPresent: { value: hostsPresent },
      context: { value: { clusterName: 'example', config: new ClusterConfig([]) } },
      cluster: { value: { vpc, getSecurityGroup: (name: keyof typeof groups) => groups[name] } },
    });
    scheduler.buildSecurityGroups();
    const template = Template.fromStack(stack);
    const rules = Object.values(template.findResources('AWS::EC2::SecurityGroupIngress')).filter(
      (rule) => rule.Properties.Description === 'OpenPBS interactive sessions from compute nodes to bastion',
    );
    assert.equal(rules.length, 1);
    const rule = rules[0]!.Properties;
    assert.equal(rule.GroupId, 'sg-0123456789abcdef1');
    assert.deepEqual(rule.SourceSecurityGroupId, stack.resolve(scheduler.computeNodeSecurityGroup.securityGroupId));
    assert.equal(rule.IpProtocol, 'tcp');
    assert.equal(rule.FromPort, 1024);
    assert.equal(rule.ToPort, 65535);
    assert.equal(rule.CidrIp, undefined);
    assert.equal(rule.CidrIpv6, undefined);
  });
}
