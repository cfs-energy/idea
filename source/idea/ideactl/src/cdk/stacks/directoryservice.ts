/**
 * `directoryservice.provider` picks one of three disjoint resource sets:
 *
 * - `openldap`: the credentials pair, an IAM role/profile, the OpenLDAP security group, a
 *   self-signed certificate custom resource, a launch template + `AWS::EC2::Instance`, and an A
 *   record. No AD-automation queue.
 * - `aws_managed_activedirectory`: the `ActiveDirectory` construct (secrets, MicrosoftAD,
 *   `Custom::ADSecurityGroupId`, resolver endpoint/rule/association) unless
 *   `directoryservice.use_existing` is set, plus the AD-automation queue pair.
 * - `activedirectory` (self managed): only the AD-automation queue pair; every credential is a
 *   config-supplied secret ARN, checked non-empty at synth.
 *
 * An unrecognised provider builds nothing and writes no cluster settings.
 */

import { CustomResource, Duration, Fn, RemovalPolicy, Tags } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import type * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';

import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { buildBootstrapUserData } from '../userdata.ts';
import { isEmpty } from '../../config/cluster-config.ts';
import {
  DIRECTORYSERVICE_ACTIVE_DIRECTORY,
  DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY,
  DIRECTORYSERVICE_OPENLDAP,
  IDEA_TAG_NAME,
  IDEA_TAG_NODE_TYPE,
} from '../constructs/base.ts';
import { InstanceProfile, Policy, Role, SQSQueue } from '../constructs/common.ts';
import { ActiveDirectory, DirectoryServiceCredentials, MODULE_DIRECTORYSERVICE } from '../constructs/directory-service.ts';
import { ExistingSocaCluster, lookupClusterDns, lookupEbsKmsKey } from '../constructs/existing-resources.ts';
import { OpenLDAPServerSecurityGroup } from '../constructs/network.ts';

/** `constants.NODE_TYPE_INFRA`. */
const NODE_TYPE_INFRA = 'infra';
/** `constants.SQS_VISIBILITY_TIMEOUT_AD_AUTOMATION`. */
const SQS_VISIBILITY_TIMEOUT_AD_AUTOMATION = 30;
/** `constants.SQS_MAX_RECEIVE_COUNT_AD_AUTOMATION`. */
const SQS_MAX_RECEIVE_COUNT_AD_AUTOMATION = 16;

/** `Utils.get_ec2_block_device_name`. */
export function ec2BlockDeviceName(baseOs: string): string {
  return baseOs === 'amazonlinux2' || baseOs === 'amazonlinux2023' ? '/dev/xvda' : '/dev/sda1';
}

export class DirectoryServiceStack extends IdeaBaseStack {
  readonly cluster: ExistingSocaCluster;

  bootstrapPackageUri: string | undefined;
  openldapRole: Role | undefined;
  openldapInstanceProfile: InstanceProfile | undefined;
  openldapSecurityGroup: OpenLDAPServerSecurityGroup | undefined;
  openldapCerts: CustomResource | undefined;
  openldapEc2Instance: ec2.CfnInstance | undefined;
  openldapClusterDnsRecordSet: route53.RecordSet | undefined;
  openldapCredentials: DirectoryServiceCredentials | undefined;

  activedirectory: ActiveDirectory | undefined;

  adAutomationSqsQueue: SQSQueue | undefined;

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

    const provider = this.context.config.getString('directoryservice.provider', undefined, {
      required: true,
    }) as string;

    if (provider === DIRECTORYSERVICE_OPENLDAP) {
      this.assertRootCredentialsWhenProvided();

      // Missing `bootstrap_package_uri` is represented by the literal `None`.
      const contextUri: unknown = this.stack.node.tryGetContext('bootstrap_package_uri');
      this.bootstrapPackageUri = contextUri === undefined || contextUri === null ? 'None' : String(contextUri);

      this.openldapCredentials = new DirectoryServiceCredentials(
        this.context,
        `${this.moduleId}-openldap-credentials`,
        this.stack,
        'Admin',
      );
      this.buildIamRoles();
      this.buildSecurityGroups();
      this.buildOpenldapCerts();
      this.buildEc2Instance();
      this.buildRoute53RecordSet();
      this.buildOpenldapClusterSettings();
    } else if (provider === DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY) {
      this.assertRootCredentialsWhenProvided();

      if (this.context.config.getBool('directoryservice.use_existing', false)) {
        assertNotEmpty(
          this.context.config.getString('directoryservice.directory_id'),
          'directoryservice.directory_id',
        );
      } else {
        this.activedirectory = new ActiveDirectory(this.context, 'active-directory', this.stack, {
          cluster: this.cluster,
          enableSso: false,
        });
      }
      this.buildAdAutomationSqsQueue();
      this.buildAwsManagedAdClusterSettings();
    } else if (provider === DIRECTORYSERVICE_ACTIVE_DIRECTORY) {
      // Self managed AD: IDEA has no write access, so a clusteradmin is bootstrapped from
      // config-supplied secrets by the directoryservice helper after installation.
      if (this.context.config.getBool('directoryservice.root_credentials_provided', false) !== true) {
        throw new Error('directoryservice.root_credentials_provided must be true');
      }
      for (const key of [
        'directoryservice.root_username_secret_arn',
        'directoryservice.root_password_secret_arn',
        'directoryservice.clusteradmin.clusteradmin_username_secret_arn',
        'directoryservice.clusteradmin.clusteradmin_password_secret_arn',
      ]) {
        assertNotEmpty(this.context.config.getString(key), key);
      }

      this.buildAdAutomationSqsQueue();
      this.buildActivedirectoryClusterSettings();
    }
  }

  /** Both AD-backed providers and openldap check the pair only when the flag is set. */
  private assertRootCredentialsWhenProvided(): void {
    if (!this.context.config.getBool('directoryservice.root_credentials_provided', false)) return;
    for (const key of ['directoryservice.root_username_secret_arn', 'directoryservice.root_password_secret_arn']) {
      assertNotEmpty(this.context.config.getString(key), key);
    }
  }

  buildIamRoles(): void {
    this.openldapRole = new Role(this.context, `${this.moduleId}-openldap-role`, this.stack, {
      description: 'IAM role assigned to the OpenLDAP server',
      assumedBy: ['ssm', 'ec2'],
      managedPolicies: this.getEc2InstanceManagedPolicies(),
    });
    this.openldapRole.attachInlinePolicy(
      new Policy(this.context, 'openldap-server-policy', this.stack, {
        policyTemplateName: 'openldap-server.yml',
      }),
    );
    this.openldapInstanceProfile = new InstanceProfile(
      this.context,
      `${this.moduleId}-openldap-instance-profile`,
      this.stack,
      [this.openldapRole],
    );
  }

  buildSecurityGroups(): void {
    this.openldapSecurityGroup = new OpenLDAPServerSecurityGroup(
      this.context,
      `${this.moduleId}-security-group`,
      this.stack,
      this.cluster.vpc,
      this.cluster.getSecurityGroup('bastion-host') as ec2.ISecurityGroup,
    );
  }

  /**
   * OpenLDAP TLS certificates, saved to Secrets Manager. Only the server can read the private key;
   * every cluster node reads the certificate so it can join the directory.
   *
   * The deploy tool generates the pair now (`src/cli/certificates.ts`) and publishes the two ARNs
   * as settings rows; this resource stays for one release so a cluster that has it can update it
   * in place to the Node handler, and it carries `Retain` so nothing here can destroy the pair the
   * running server is serving. Remove it in the release after this one, once every cluster has
   * deployed this one.
   */
  buildOpenldapCerts(): void {
    const hostname = this.context.config.getString('directoryservice.hostname', undefined, {
      required: true,
    }) as string;
    const serviceToken = this.context.config.getString('cluster.self_signed_certificate_lambda_arn', undefined, {
      required: true,
    }) as string;
    this.openldapCerts = new CustomResource(this.stack, 'openldap-server-certs', {
      serviceToken,
      properties: {
        domain_name: hostname,
        certificate_name: `${this.clusterName}-${this.moduleId}`,
        create_acm_certificate: false,
        kms_key_id: this.context.config.getString('cluster.secretsmanager.kms_key_id'),
        tags: {
          Name: `${this.clusterName}-${this.moduleId}`,
          'idea:ClusterName': this.clusterName,
          'idea:ModuleName': MODULE_DIRECTORYSERVICE,
        },
      },
      removalPolicy: RemovalPolicy.RETAIN,
      resourceType: 'Custom::SelfSignedCertificateOpenLDAPServer',
    });
  }

  /** The certificate the deploy tool generated or adopted, by the row it published. */
  private tlsCertificateSecretArn(): string {
    return this.context.config.getString('directoryservice.tls_certificate_secret_arn', undefined, {
      required: true,
    }) as string;
  }

  private tlsPrivateKeySecretArn(): string {
    return this.context.config.getString('directoryservice.tls_private_key_secret_arn', undefined, {
      required: true,
    }) as string;
  }

  buildEc2Instance(): void {
    const config = this.context.config;
    const isPublic = config.getBool('directoryservice.public', false);
    const baseOs = config.getString('directoryservice.base_os', undefined, { required: true }) as string;
    const instanceAmi = config.getString('directoryservice.instance_ami', undefined, { required: true }) as string;
    const instanceType = config.getString('directoryservice.instance_type', undefined, { required: true }) as string;
    const volumeSize = config.getInt('directoryservice.volume_size', 200);
    const keyPairName = config.getString('cluster.network.ssh_key_pair', undefined, { required: true }) as string;
    const enableDetailedMonitoring = config.getBool('directoryservice.ec2.enable_detailed_monitoring', false);
    const enableTerminationProtection = config.getBool('directoryservice.ec2.enable_termination_protection', false);
    const metadataHttpTokens = config.getString('directoryservice.ec2.metadata_http_tokens', undefined, {
      required: true,
    }) as string;
    const httpsProxy = config.getString('cluster.network.https_proxy', '');
    const noProxy = config.getString('cluster.network.no_proxy', '');
    const proxyConfig: Record<string, string> = isEmpty(httpsProxy)
      ? {}
      : { http_proxy: httpsProxy, https_proxy: httpsProxy, no_proxy: noProxy };

    const ebsKmsKey = lookupEbsKmsKey(this.context, this.stack);

    const subnetIds =
      isPublic && this.cluster.publicSubnets.length > 0
        ? this.cluster.existingVpc.getPublicSubnetIds()
        : this.cluster.existingVpc.getPrivateSubnetIds();

    const blockDeviceName = ec2BlockDeviceName(baseOs);
    const blockDeviceTypeString = config.getString('directoryservice.volume_type', 'gp3');
    const blockDeviceVolumeType =
      blockDeviceTypeString === 'gp3' ? ec2.EbsDeviceVolumeType.GP3 : ec2.EbsDeviceVolumeType.GP2;

    const userData = buildBootstrapUserData({
      awsRegion: this.awsRegion,
      bootstrapPackageUri: this.bootstrapPackageUri as string,
      installCommands: ['/bin/bash openldap-server/setup.sh'],
      baseOs,
      infraConfig: {
        LDAP_ROOT_USERNAME_SECRET_ARN: '${__LDAP_ROOT_USERNAME_SECRET_ARN__}',
        LDAP_ROOT_PASSWORD_SECRET_ARN: '${__LDAP_ROOT_PASSWORD_SECRET_ARN__}',
        LDAP_TLS_CERTIFICATE_SECRET_ARN: '${__LDAP_TLS_CERTIFICATE_SECRET_ARN__}',
        LDAP_TLS_PRIVATE_KEY_SECRET_ARN: '${__LDAP_TLS_PRIVATE_KEY_SECRET_ARN__}',
      },
      proxyConfig,
    });

    const credentials = this.openldapCredentials as DirectoryServiceCredentials;
    const substitutedUserdata = Fn.sub(userData, {
      __LDAP_ROOT_USERNAME_SECRET_ARN__: credentials.getUsernameSecretArn(),
      __LDAP_ROOT_PASSWORD_SECRET_ARN__: credentials.getPasswordSecretArn(),
      __LDAP_TLS_CERTIFICATE_SECRET_ARN__: this.tlsCertificateSecretArn(),
      __LDAP_TLS_PRIVATE_KEY_SECRET_ARN__: this.tlsPrivateKeySecretArn(),
    });

    const launchTemplate = new ec2.LaunchTemplate(this.stack, `${this.moduleId}-lt`, {
      instanceType: new ec2.InstanceType(instanceType),
      machineImage: ec2.MachineImage.genericLinux({ [this.awsRegion]: instanceAmi }),
      userData: ec2.UserData.custom(substitutedUserdata),
      keyName: keyPairName,
      blockDevices: [
        {
          deviceName: blockDeviceName,
          volume: {
            ebsDevice: {
              encrypted: true,
              kmsKey: ebsKmsKey,
              volumeSize,
              volumeType: blockDeviceVolumeType,
            },
          },
        },
      ],
      requireImdsv2: metadataHttpTokens === 'required',
    });

    this.openldapEc2Instance = new ec2.CfnInstance(this.stack, `${this.moduleId}-instance`, {
      blockDeviceMappings: [
        { deviceName: blockDeviceName, ebs: { volumeSize, volumeType: blockDeviceTypeString } },
      ],
      disableApiTermination: enableTerminationProtection,
      iamInstanceProfile: (this.openldapInstanceProfile as InstanceProfile).instanceProfileName,
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
          groupSet: [(this.openldapSecurityGroup as OpenLDAPServerSecurityGroup).securityGroupId],
          subnetId: subnetIds[0] as string,
        },
      ],
      userData: Fn.base64(substitutedUserdata),
      monitoring: enableDetailedMonitoring,
    });
    Tags.of(this.openldapEc2Instance).add(IDEA_TAG_NAME, this.buildResourceName(this.moduleId));
    Tags.of(this.openldapEc2Instance).add(IDEA_TAG_NODE_TYPE, NODE_TYPE_INFRA);
    this.addBackupTags(this.openldapEc2Instance);

    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-EC26', reason: 'EBS Encryption is enforced via Launch Template' }],
      this.openldapEc2Instance,
    );

    if (!enableDetailedMonitoring) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC28',
            reason: 'detailed monitoring is a configurable option to save costs',
          },
        ],
        this.openldapEc2Instance,
      );
    }

    if (!enableTerminationProtection) {
      this.addNagSuppression(
        [
          {
            rule_id: 'AwsSolutions-EC29',
            reason:
              'termination protection not supported in CDK L2 construct. enable termination protection via AWS EC2 console after deploying the cluster.',
          },
        ],
        this.openldapEc2Instance,
      );
    }
  }

  buildRoute53RecordSet(): void {
    const hostname = this.context.config.getString('directoryservice.hostname', undefined, {
      required: true,
    }) as string;
    this.openldapClusterDnsRecordSet = new route53.RecordSet(this.stack, `${this.moduleId}-dns-record`, {
      recordType: route53.RecordType.A,
      target: route53.RecordTarget.fromIpAddresses(
        (this.openldapEc2Instance as ec2.CfnInstance).attrPrivateIp,
      ),
      ttl: Duration.minutes(5),
      recordName: hostname,
      zone: lookupClusterDns(this.context, this.stack),
    });
  }

  buildAdAutomationSqsQueue(): void {
    const kmsKeyId = this.context.config.getString('cluster.sqs.kms_key_id');

    this.adAutomationSqsQueue = new SQSQueue(this.context, 'ad-automation-sqs-queue', this.stack, {
      queueName: `${this.clusterName}-${this.moduleId}-ad-automation.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      encryptionMasterKey: kmsKeyId,
      visibilityTimeout: Duration.seconds(SQS_VISIBILITY_TIMEOUT_AD_AUTOMATION),
      deadLetterQueue: {
        maxReceiveCount: SQS_MAX_RECEIVE_COUNT_AD_AUTOMATION,
        queue: new SQSQueue(this.context, 'ad-automation-sqs-queue-dlq', this.stack, {
          queueName: `${this.clusterName}-${this.moduleId}-ad-automation-dlq.fifo`,
          fifo: true,
          contentBasedDeduplication: true,
          encryptionMasterKey: kmsKeyId,
          isDeadLetterQueue: true,
        }),
      },
    });
    // Both queues use `Name=<cluster>-<moduleId>`.
    this.addCommonTags(this.adAutomationSqsQueue);
    this.addCommonTags((this.adAutomationSqsQueue.deadLetterQueue as sqs.DeadLetterQueue).queue);
  }

  buildOpenldapClusterSettings(): void {
    const instance = this.openldapEc2Instance as ec2.CfnInstance;
    const credentials = this.openldapCredentials as DirectoryServiceCredentials;
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      private_ip: instance.attrPrivateIp,
      private_dns_name: instance.attrPrivateDnsName,
      instance_id: instance.ref,
      security_group_id: (this.openldapSecurityGroup as OpenLDAPServerSecurityGroup).securityGroupId,
      iam_role_arn: (this.openldapRole as Role).roleArn,
      instance_profile_arn: (this.openldapInstanceProfile as InstanceProfile).ref,
      // This path reads raw secret resources. With supplied root credentials those resources are
      // absent, so the dereference fails.
      root_username_secret_arn: (credentials.adminUsername as secretsmanager.CfnSecret).ref,
      root_password_secret_arn: (credentials.adminPassword as secretsmanager.CfnSecret).ref,
      tls_certificate_secret_arn: this.tlsCertificateSecretArn(),
      tls_private_key_secret_arn: this.tlsPrivateKeySecretArn(),
    };

    if (this.context.config.getBool('directoryservice.public', false)) {
      clusterSettings['public_ip'] = instance.attrPublicIp;
    }

    this.updateClusterSettings(clusterSettings);
  }

  buildAwsManagedAdClusterSettings(): void {
    const queue = this.adAutomationSqsQueue as SQSQueue;
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      'ad_automation.sqs_queue_url': queue.queueUrl,
      'ad_automation.sqs_queue_arn': queue.queueArn,
    };
    if (this.activedirectory !== undefined) {
      clusterSettings['directory_id'] = this.activedirectory.ad.ref;
      clusterSettings['root_username_secret_arn'] = this.activedirectory.credentials.getUsernameSecretArn();
      clusterSettings['root_password_secret_arn'] = this.activedirectory.credentials.getPasswordSecretArn();
    }
    this.updateClusterSettings(clusterSettings);
  }

  buildActivedirectoryClusterSettings(): void {
    const queue = this.adAutomationSqsQueue as SQSQueue;
    this.updateClusterSettings({
      deployment_id: this.deploymentId,
      'ad_automation.sqs_queue_url': queue.queueUrl,
      'ad_automation.sqs_queue_arn': queue.queueArn,
    });
  }
}

/** Throws with the key when a required value is empty. */
function assertNotEmpty(value: string | undefined, key: string): void {
  if (isEmpty(value)) throw new Error(`${key} is required`);
}

export function buildStack(props: StackBuildProps): void {
  new DirectoryServiceStack(props);
}
