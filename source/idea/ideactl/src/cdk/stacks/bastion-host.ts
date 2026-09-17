/**
 * The bastion is a single EC2 host, built as an L1 `CfnInstance` next to an L2 `LaunchTemplate`.
 * The launch template carries the encrypted root volume and IMDSv2; the instance repeats the
 * block device mapping, the AMI, the instance type and the key name, and references the launch
 * template by id and latest version. Both carry the same user data, so the script is emitted
 * twice on purpose.
 *
 * The instance takes the instance profile by its literal **name**, not a `Ref`, so the only
 * ordering edge is the explicit `DependsOn` on the instance profile (and the profile's own
 * explicit `DependsOn` on the role).
 */

import { Duration, Fn, Tags } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';

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
  readonly bootstrapPackageUri: string;
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

    this.bootstrapPackageUri = this.getBootstrapPackageUri();
    this.cluster = new ExistingSocaCluster(this.context, this.stack);

    this.buildIamRoles();
    this.buildEc2Instance();
    this.buildRoute53RecordSet();
    this.buildClusterSettings();
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
