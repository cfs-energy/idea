/**
 * Without the container flag the bastion is a single EC2 host, built as an L1 `CfnInstance` next to an L2 `LaunchTemplate`.
 * The launch template carries the encrypted root volume and IMDSv2; the instance repeats the
 * block device mapping, the AMI, the instance type and the key name, and references the launch
 * template by id and latest version. Both carry the same user data, so the script is emitted
 * twice on purpose.
 *
 * The instance takes the instance profile by its literal **name**, not a `Ref`, so the only
 * ordering edge is the explicit `DependsOn` on the instance profile (and the profile's own
 * explicit `DependsOn` on the role).
 */

import { CfnDeletionPolicy, Duration, Fn, Tags } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import {
  addStorageMounts, adoptedLogDriver, buildEc2Service, buildExecutionRole,
  buildTaskDefinition, commonEnvironment, containerImage, dockerLabels,
  applicationContainerSettings, ecsTasksPrincipal, grantInjectedSecret, healthCheckGrace, roleSizing, taskRoleName,
  type ContainerScope,
} from '../constructs/container.ts';

import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { IDEA_TAG_NODE_TYPE } from '../constructs/base.ts';
import { InstanceProfile, Policy, Role } from '../constructs/common.ts';
import {
  ExistingSocaCluster,
  lookupClusterDns,
  lookupEbsKmsKey,
  lookupKeyPair,
} from '../constructs/existing-resources.ts';
import { buildBootstrapUserData } from '../userdata.ts';

export const MODULE_BASTION_HOST = 'bastion-host';
const NODE_TYPE_INFRA = 'infra';

/** `Utils.get_ec2_block_device_name`. */
export function ec2BlockDeviceName(baseOs: string): string {
  return baseOs === 'amazonlinux2' || baseOs === 'amazonlinux2023' ? '/dev/xvda' : '/dev/sda1';
}

export class BastionHostStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;
  readonly bootstrapPackageUri!: string;
  bastionHostRole!: Role;
  bastionHostInstanceProfile!: InstanceProfile;
  ec2Instance!: ec2.CfnInstance;
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

    if (this.context.config.getBool('ecs.enabled', false)) {
      this.buildContainer();
      return;
    }
    this.bootstrapPackageUri = this.getBootstrapPackageUri();
    this.buildIamRoles();
    this.buildEc2Instance();
    this.buildRoute53RecordSet();
    this.buildClusterSettings();
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
      healthCheck: { protocol: elbv2.Protocol.TCP, port: '22', interval: Duration.seconds(5), timeout: Duration.seconds(4), healthyThresholdCount: 2, unhealthyThresholdCount: 2 },
      deregistrationDelay: Duration.seconds(30), connectionTermination: true,
    });
    const listener = nlb.addListener('ssh', { port: 22, protocol: elbv2.Protocol.TCP, defaultTargetGroups: [targetGroup] });
    const healthSecurityGroup = new ec2.SecurityGroup(this.stack, 'bastion-health-security-group', {
      vpc: this.cluster.vpc, allowAllOutbound: false, description: 'Allow SSH and health checks from the bastion NLB',
    });
    healthSecurityGroup.addIngressRule(bastionSecurityGroup, ec2.Port.tcp(22));

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

  /**
   * The bootstrap package is not a CDK asset: the CLI uploads it and passes its location as the
   * `bootstrap_package_uri` context parameter. When absent, the deployment identifier derives
   * the package name for a standalone synth, the same rule `bootstrapPackageBasenames` uses.
   *
   * The naming rule is local so stack synthesis does not load an S3 client.
   */
  getBootstrapPackageUri(): string {
    const fromContext: unknown = this.stack.node.tryGetContext('bootstrap_package_uri');
    if (typeof fromContext === 'string' && fromContext !== '') return fromContext;
    const bucket = this.context.config.getString('cluster.cluster_s3_bucket', undefined, {
      required: true,
    }) as string;
    return `s3://${bucket}/idea/bootstrap/bootstrap-${this.moduleId}-${this.deploymentId}.tar.gz`;
  }

  buildIamRoles(): void {
    this.bastionHostRole = new Role(this.context, `${this.moduleId}-role`, this.stack, {
      description: 'IAM role assigned to the bastion-host',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: this.getEc2InstanceManagedPolicies(),
    });
    this.bastionHostRole.attachInlinePolicy(
      new Policy(this.context, 'bastion-host-policy', this.stack, {
        policyTemplateName: 'bastion-host.yml',
      }),
    );
    this.bastionHostInstanceProfile = new InstanceProfile(
      this.context,
      `${this.moduleId}-instance-profile`,
      this.stack,
      [this.bastionHostRole],
    );
    this.bastionHostInstanceProfile.node.addDependency(this.bastionHostRole);
  }

  buildEc2Instance(): void {
    const config = this.context.config;
    const isPublic = config.getBool('bastion-host.public', false);
    const baseOs = config.getString('bastion-host.base_os', undefined, { required: true }) as string;
    const instanceAmi = config.getString('bastion-host.instance_ami', undefined, { required: true }) as string;
    const instanceType = config.getString('bastion-host.instance_type', undefined, { required: true }) as string;
    const volumeSize = config.getInt('bastion-host.volume_size', 200);
    const keyPairName = config.getString('cluster.network.ssh_key_pair', undefined, { required: true }) as string;
    const enableDetailedMonitoring = config.getBool('bastion-host.ec2.enable_detailed_monitoring', false);
    const enableTerminationProtection = config.getBool('bastion-host.ec2.enable_termination_protection', false);
    const metadataHttpTokens = config.getString('bastion-host.ec2.metadata_http_tokens', undefined, {
      required: true,
    }) as string;

    const httpsProxy = config.getString('cluster.network.https_proxy', '');
    const noProxy = config.getString('cluster.network.no_proxy', '');
    const proxyConfig: Record<string, string> =
      httpsProxy === '' ? {} : { http_proxy: httpsProxy, https_proxy: httpsProxy, no_proxy: noProxy };

    const ebsKmsKey = lookupEbsKmsKey(this.context, this.stack);
    const instanceProfileName = this.bastionHostInstanceProfile.instanceProfileName as string;
    const securityGroup = this.cluster.getSecurityGroup(MODULE_BASTION_HOST) as ec2.ISecurityGroup;

    const subnetIds =
      isPublic && this.cluster.publicSubnets.length > 0
        ? this.cluster.existingVpc.getPublicSubnetIds()
        : this.cluster.existingVpc.getPrivateSubnetIds();

    const blockDeviceName = ec2BlockDeviceName(baseOs);
    const blockDeviceTypeString = config.getString('bastion-host.volume_type', 'gp3');
    // Anything other than gp3 becomes gp2 in the launch template, including a typo.
    const blockDeviceVolumeType =
      blockDeviceTypeString === 'gp3' ? ec2.EbsDeviceVolumeType.GP3 : ec2.EbsDeviceVolumeType.GP2;

    const userData = buildBootstrapUserData({
      awsRegion: this.awsRegion,
      bootstrapPackageUri: this.bootstrapPackageUri,
      installCommands: ['/bin/bash bastion-host/setup.sh'],
      proxyConfig,
      baseOs,
    });

    const launchTemplate = new ec2.LaunchTemplate(this.stack, `${this.moduleId}-lt`, {
      instanceType: new ec2.InstanceType(instanceType),
      machineImage: ec2.MachineImage.genericLinux({ [this.awsRegion]: instanceAmi }),
      userData: ec2.UserData.custom(Fn.sub(userData)),
      keyPair: lookupKeyPair(this.context, this.stack),
      blockDevices: [
        {
          deviceName: blockDeviceName,
          volume: ec2.BlockDeviceVolume.ebs(volumeSize, {
            encrypted: true,
            kmsKey: ebsKmsKey,
            volumeType: blockDeviceVolumeType,
          }),
        },
      ],
      requireImdsv2: metadataHttpTokens === 'required',
    });

    this.ec2Instance = new ec2.CfnInstance(this.stack, `${this.moduleId}-instance`, {
      blockDeviceMappings: [
        {
          deviceName: blockDeviceName,
          ebs: { encrypted: true, volumeSize, volumeType: blockDeviceTypeString },
        },
      ],
      disableApiTermination: enableTerminationProtection,
      iamInstanceProfile: instanceProfileName,
      instanceType,
      imageId: instanceAmi,
      keyName: keyPairName,
      launchTemplate: {
        version: launchTemplate.latestVersionNumber,
        launchTemplateId: launchTemplate.launchTemplateId,
      },
      networkInterfaces: [
        {
          deviceIndex: '0',
          associatePublicIpAddress: isPublic,
          groupSet: [securityGroup.securityGroupId],
          subnetId: subnetIds[0] as string,
        },
      ],
      userData: Fn.base64(Fn.sub(userData)),
      monitoring: enableDetailedMonitoring,
    });
    Tags.of(this.ec2Instance).add('Name', this.buildResourceName(this.moduleId));
    Tags.of(this.ec2Instance).add(IDEA_TAG_NODE_TYPE, NODE_TYPE_INFRA);
    this.addBackupTags(this.ec2Instance);
    this.ec2Instance.node.addDependency(this.bastionHostInstanceProfile);

    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-EC26', reason: 'EBS Encryption is enforced via Launch Template' }],
      this.ec2Instance,
    );

    if (!enableDetailedMonitoring) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC28',
            reason: 'Detailed monitoring is a configurable option to save costs.',
          },
        ],
        this.ec2Instance,
      );
    }

    if (!enableTerminationProtection) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC29',
            reason:
              'termination protection is a configurable option. Enable termination protection via AWS EC2 console after deploying the cluster if required.',
          },
        ],
        this.ec2Instance,
      );
    }
  }

  buildRoute53RecordSet(): void {
    const hostname = this.context.config.getString('bastion-host.hostname', undefined, {
      required: true,
    }) as string;
    this.clusterDnsRecordSet = new route53.RecordSet(this.stack, `${this.moduleId}-dns-record`, {
      recordType: route53.RecordType.A,
      target: route53.RecordTarget.fromIpAddresses(this.ec2Instance.attrPrivateIp),
      ttl: Duration.minutes(5),
      recordName: hostname,
      zone: lookupClusterDns(this.context, this.stack),
    });
  }

  buildClusterSettings(): void {
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      private_ip: this.ec2Instance.attrPrivateIp,
      private_dns_name: this.ec2Instance.attrPrivateDnsName,
    };
    if (this.context.config.getBool('bastion-host.public', false)) {
      clusterSettings['public_ip'] = this.ec2Instance.attrPublicIp;
    }
    clusterSettings['instance_id'] = this.ec2Instance.ref;
    clusterSettings['iam_role_arn'] = this.bastionHostRole.roleArn;
    clusterSettings['instance_profile_arn'] = this.bastionHostInstanceProfile.ref;
    this.updateClusterSettings(clusterSettings);
  }
}

export function buildStack(props: StackBuildProps): void {
  new BastionHostStack(props);
}
