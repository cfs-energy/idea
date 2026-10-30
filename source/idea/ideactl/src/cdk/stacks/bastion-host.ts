/** SSH bastion tasks, persistent host keys, stable load-balancer addresses, and directory access. */

import { CfnDeletionPolicy, Duration } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import {
  addStorageMounts, adoptedLogDriver,
  applicationContainerSettings,
  buildEc2Service, buildExecutionRole,
  buildTaskDefinition, commonEnvironment, containerImage, dockerLabels,
  ecsTasksPrincipal, grantInjectedSecret, healthCheckGrace, roleSizing, taskRoleName,
  type ContainerScope,
} from '../constructs/container.ts';

import { ArnBuilder } from '../../config/arn-builder.ts';

import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import {
  ExistingSocaCluster,
  lookupClusterDns
} from '../constructs/existing-resources.ts';

export const MODULE_BASTION_HOST = 'bastion-host';

export class BastionHostStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  clusterDnsRecordSet!: route53.RecordSet;

  constructor(props: StackBuildProps) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.cluster = new ExistingSocaCluster(this.context, this.stack);
    this.buildContainer();
  }

  private buildContainer(): void {
    const config = this.context.config;
    const scope: ContainerScope = {
      ctx: this.context, stack: this.stack, vpc: this.cluster.vpc,
      privateSubnets: this.cluster.privateSubnets,
    };
    const sizing = roleSizing(scope, 'bastion-host');
    if (sizing.desired < 1) throw new Error('ecs.tasks.bastion-host.desired must be at least 1');
    const provider = config.getString('directoryservice.provider', undefined, { required: true });
    if (!['openldap', 'activedirectory', 'aws_managed_activedirectory'].includes(provider as string)) {
      throw new Error('The bastion task requires an OpenLDAP or Active Directory provider');
    }
    const isPublic = config.getBool('bastion-host.public', false) && this.cluster.publicSubnets.length > 0;
    const subnets = isPublic ? this.cluster.publicSubnets : this.cluster.privateSubnets;
    const bastionSecurityGroup = this.cluster.getSecurityGroup(MODULE_BASTION_HOST) as ec2.ISecurityGroup;
    // SSH owns its address and access rules independently of desktop sessions. A separate NLB
    // avoids coupling bastion availability and public/private placement to the optional gateway.
    const nlb = new elbv2.NetworkLoadBalancer(this.stack, 'bastion-nlb', {
      vpc: this.cluster.vpc, internetFacing: isPublic,
      vpcSubnets: { subnets }, crossZoneEnabled: true,
      securityGroups: [bastionSecurityGroup],
    });
    const addresses = isPublic ? subnets.map((subnet, index) => {
      const address = new ec2.CfnEIP(this.stack, `bastion-eip-${index}`, { domain: 'vpc' });
      address.cfnOptions.updateReplacePolicy = CfnDeletionPolicy.RETAIN;
      return { address, subnet };
    }) : [];
    if (isPublic) {
      const resource = nlb.node.defaultChild as elbv2.CfnLoadBalancer;
      resource.subnets = undefined;
      resource.subnetMappings = addresses.map(({ address, subnet }) => ({
        subnetId: subnet.subnetId, allocationId: address.attrAllocationId,
      }));
    }
    const targetGroup = new elbv2.NetworkTargetGroup(this.stack, 'bastion-target-group', {
      vpc: this.cluster.vpc, port: 22, protocol: elbv2.Protocol.TCP,
      targetType: elbv2.TargetType.IP, preserveClientIp: true,
      healthCheck: { protocol: elbv2.Protocol.TCP, port: '22', interval: Duration.seconds(30), timeout: Duration.seconds(10), healthyThresholdCount: 2, unhealthyThresholdCount: 2 },
      deregistrationDelay: Duration.seconds(30), connectionTermination: true,
    });
    const listener = nlb.addListener('ssh', { port: 22, protocol: elbv2.Protocol.TCP, defaultTargetGroups: [targetGroup] });
    const healthSecurityGroup = new ec2.SecurityGroup(this.stack, 'bastion-health-security-group', {
      vpc: this.cluster.vpc, allowAllOutbound: false, description: 'Allow SSH and health checks from the bastion NLB',
    });
    healthSecurityGroup.addIngressRule(bastionSecurityGroup, ec2.Port.tcp(22));
    const managerSecurityGroup = ec2.SecurityGroup.fromSecurityGroupId(this.stack, 'terminal-cluster-manager-sg', config.getString('cluster-manager.security_group_id', undefined, { required: true }) as string);
    healthSecurityGroup.addIngressRule(managerSecurityGroup, ec2.Port.tcp(22), 'Browser terminals');

    const secret = new secretsmanager.CfnSecret(this.stack, 'bastion-host-keys', {
      description: 'Persistent SSH host keys for the bastion service',
      secretString: '{"schema":1}',
      tags: [
        { key: 'idea:ClusterName', value: this.clusterName },
        { key: 'idea:ModuleName', value: MODULE_BASTION_HOST },
      ],
    });
    const taskRole = new iam.Role(this.stack, 'bastion-task-role', {
      assumedBy: ecsTasksPrincipal(scope), roleName: taskRoleName(scope, `${this.moduleId}-task-role`),
    });
    if (this.context.config.getString('cluster.dynamodb.kms_key_id')) {
      taskRole.addToPolicy(new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: [new ArnBuilder(this.context.config).kmsDynamodbKeyArn],
      }));
    }
    const terminalTableArn = this.stack.formatArn({ service: 'dynamodb', resource: 'table', resourceName: `${this.clusterName}.cluster-manager.terminal` });
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem'], resources: [terminalTableArn],
      conditions: { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['host:bastion'] } },
    }));
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem'], resources: [this.stack.formatArn({ service: 'dynamodb', resource: 'table', resourceName: `${this.clusterName}.cluster-settings` })],
      conditions: { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['cluster-manager.web_terminal.enabled', 'cluster-manager.web_terminal.ca_public_key'] } },
    }));
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue'], resources: [secret.ref],
    }));
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssmmessages:CreateControlChannel', 'ssmmessages:CreateDataChannel', 'ssmmessages:OpenControlChannel', 'ssmmessages:OpenDataChannel'],
      resources: ['*'],
    }));
    taskRole.addToPolicy(new iam.PolicyStatement({ actions: ['ec2:DescribeKeyPairs', 'logs:DescribeLogGroups'], resources: ['*'] }));
    // The ecs module's id is always its name: an upgrade holds its module-set row until the last
    // stack has deployed, so a lookup through that row would fail exactly when this stack synthesizes.
    logs.LogGroup.fromLogGroupName(this.stack, 'bastion-exec-log-group',
      `/${this.clusterName}/ecs/exec`).grantWrite(taskRole);
    this.addNagSuppression([
      { rule_id: 'AwsSolutions-IAM5', reason: 'ECS Exec channels, log discovery and EC2 public key lookup require wildcard resources; secret access is restricted to the host key secret.' },
    ], taskRole, true);
    this.addNagSuppression([
      { rule_id: 'AwsSolutions-SMG4', reason: 'SSH host identity must survive task replacement; automatic rotation would change the trusted fingerprint.' },
    ], secret);
    const executionRole = buildExecutionRole(scope, 'bastion-execution-role');
    const environment = commonEnvironment(scope, { role: 'bastion-host', moduleId: this.moduleId, moduleName: MODULE_BASTION_HOST });
    environment['IDEA_HOST_KEYS_SECRET_ARN'] = secret.ref;
    environment['IDEA_DIRECTORY_PROVIDER'] = provider as string;
    environment['IDEA_SSH_KEY_PAIR'] = config.getString('cluster.network.ssh_key_pair', undefined, { required: true }) as string;
    environment['IDEA_PBS_SERVER'] = config.getString('scheduler.private_dns_name', '');
    environment['IDEA_DATA_DIR'] = config.getString('shared-storage.data.mount_dir', undefined, { required: true }) as string;
    // the site hook lives under the cluster home on /apps, as the compute and desktop hooks do
    environment['IDEA_CLUSTER_HOME'] = config.getString('cluster.home_dir', undefined, { required: true }) as string;
    const secrets: Record<string, ecs.Secret> = {};
    if (provider === 'openldap') {
      environment['IDEA_LDAP_HOST'] = config.getString('directoryservice.hostname', undefined, { required: true }) as string;
      environment['IDEA_LDAP_BASE'] = config.getString('directoryservice.ldap_base', undefined, { required: true }) as string;
      const certificateArn = config.getString('directoryservice.tls_certificate_secret_arn', undefined, { required: true }) as string;
      secrets['IDEA_LDAP_CERTIFICATE'] = ecs.Secret.fromSecretsManager(
        secretsmanager.Secret.fromSecretCompleteArn(this.stack, 'directory-certificate', certificateArn),
      );
      grantInjectedSecret(scope, executionRole, certificateArn);
    } else {
      environment['IDEA_AD_DOMAIN'] = config.getString('directoryservice.name', undefined, { required: true }) as string;
      environment['IDEA_AD_ID_MAPPING'] = String(config.getBool('directoryservice.sssd.ldap_id_mapping', false));
      environment['IDEA_AD_SUDOERS_GROUP'] = config.getString('directoryservice.sudoers.group_name', undefined, { required: true }) as string;
      environment['IDEA_AD_QUEUE_URL'] = config.getString('directoryservice.ad_automation.sqs_queue_url', undefined, { required: true }) as string;
      environment['IDEA_AD_TABLE'] = `${this.clusterName}.ad-automation`;
      taskRole.addToPolicy(new iam.PolicyStatement({
        actions: ['sqs:SendMessage'],
        resources: [config.getString('directoryservice.ad_automation.sqs_queue_arn', undefined, { required: true }) as string],
      }));
      taskRole.addToPolicy(new iam.PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [this.stack.formatArn({ service: 'dynamodb', resource: 'table', resourceName: `${this.clusterName}.ad-automation` })],
        conditions: { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['${aws:userid}'] } },
      }));
    }
    const task = buildTaskDefinition(scope, 'bastion-task-definition', { executionRole, taskRole });
    const container = task.addContainer('bastion', {
      ...applicationContainerSettings('bastion-host'),
      image: containerImage(scope), cpu: sizing.cpu, memoryLimitMiB: sizing.memory,
      environment, secrets, dockerLabels: dockerLabels(scope, 'bastion-host'),
      logging: adoptedLogDriver(scope, 'bastion-log-group', `/${this.clusterName}/${this.moduleId}`, 'sshd'),
      linuxParameters: new ecs.LinuxParameters(this.stack, 'bastion-linux-parameters', { initProcessEnabled: true }),
      systemControls: [
        { namespace: 'net.ipv4.tcp_rmem', value: '4096 87380 16777216' },
        { namespace: 'net.ipv4.tcp_wmem', value: '4096 65536 16777216' },
      ],
    });
    container.addPortMappings({ containerPort: 22, protocol: ecs.Protocol.TCP });
    addStorageMounts(scope, task, container);
    const service = buildEc2Service(scope, {
      constructId: 'bastion-service', serviceName: `${this.clusterName}-${this.moduleId}`,
      taskDefinition: task, desiredCount: sizing.desired,
      securityGroups: [bastionSecurityGroup, healthSecurityGroup],
      healthCheckGracePeriod: healthCheckGrace('bastion-host'),
      placementConstraints: [ecs.PlacementConstraint.memberOf('attribute:idea.directory == joined')],
      dependencies: [taskRole, executionRole, listener],
    });
    service.attachToNetworkTargetGroup(targetGroup);
    // Reuse the host record's construct id: its A record becomes an alias in place.
    this.clusterDnsRecordSet = new route53.RecordSet(this.stack, `${this.moduleId}-dns-record`, {
      recordType: route53.RecordType.A, target: route53.RecordTarget.fromAlias(new targets.LoadBalancerTarget(nlb)),
      recordName: config.getString('bastion-host.hostname', undefined, { required: true }) as string,
      zone: lookupClusterDns(this.context, this.stack),
    });
    // `bastion-host.public` stays the values-generated row this stack reads: a custom resource
    // property is a string by the time it reaches the handler, and a published boolean would
    // overwrite the typed row and be deleted with the map the day it is dropped.
    const settings = this.updateClusterSettings({
      deployment_id: this.deploymentId, private_dns_name: nlb.loadBalancerDnsName,
      ...(isPublic ? { public_ip: addresses[0]!.address.ref, public_ips: addresses.map(({ address }) => address.ref) } : {}),
      iam_role_arn: taskRole.roleArn, task_role_id: (taskRole.node.defaultChild as iam.CfnRole).attrRoleId,
      service_name: `${this.clusterName}-${this.moduleId}`, host_keys_secret_arn: secret.ref,
    });
    service.node.addDependency(settings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new BastionHostStack(props);
}
