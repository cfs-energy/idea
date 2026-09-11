/**
 * The base infrastructure every other module stack reads out of the cluster-settings table: VPC,
 * both load balancers and their
 * listeners, the WAF trio, the self-signed certificates, the prefix list, the shared IAM policies
 * and roles, and the four custom-resource lambdas.
 *
 * Build order is load-bearing: it fixes template order, the
 * `{IndirectPeer}` counters on the security groups, and the `settings` map insertion order.
 *
 * Two behaviours are load-bearing:
 *
 * - the three DCV broker listeners read `cluster.external_alb.dcv_broker_*_listener_arn`, keys
 *   nothing writes (the stack writes `cluster.load_balancers.internal_alb.dcv_broker_*`), so they
 *   always synthesize a fixed-response and the virtual-desktop-controller stack sets the real
 *   default action out of band through the cluster-endpoints lambda;
 * - the private hosted zone's `Name` tag carries the cluster prefix twice.
 */

import { CustomResource, Duration, RemovalPolicy } from 'aws-cdk-lib';
import * as backup from 'aws-cdk-lib/aws-backup';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventsTargets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

import { ConfigKeyNotFound, isEmpty } from '../../config/cluster-config.ts';
import type { ClusterConfig } from '../../config/cluster-config.ts';
import type { StackBuildProps } from '../app.ts';
import { IdeaBaseStack } from '../base-stack.ts';
import { IdeaCodeAsset } from '../code-asset.ts';
import { kmsKeyArn } from '../constructs/base.ts';
import { BackupPlan } from '../constructs/backup.ts';
import {
  CreateTagsCustomResource,
  LambdaFunction,
  ManagedPolicy,
  Policy,
  Role,
  SNSTopic,
} from '../constructs/common.ts';
import { PrivateHostedZone } from '../constructs/dns.ts';
import { ExistingVpc, lookupClusterS3Bucket } from '../constructs/existing-resources.ts';
import {
  BastionHostSecurityGroup,
  DefaultClusterSecurityGroup,
  ExternalLoadBalancerSecurityGroup,
  InternalLoadBalancerSecurityGroup,
  SecurityGroup,
  Vpc,
  VpcEndpointSecurityGroup,
  VpcGatewayEndpoint,
  VpcInterfaceEndpoint,
  WebAcl,
} from '../constructs/network.ts';

const LOG_RETENTION_ROLE_NAME = 'log-retention';
const MODULE_VIRTUAL_DESKTOP_CONTROLLER = 'virtual-desktop-controller';
const DEFAULT_SSL_POLICY = 'ELBSecurityPolicy-FS-1-2-Res-2020-10';
/** `constants.CAVEATS['ROUTE53_CROSS_ZONE_ALIAS_RESTRICTED_REGION_LIST']`. */
const ROUTE53_CROSS_ZONE_ALIAS_RESTRICTED_REGIONS = ['us-gov-east-1', 'us-gov-west-1'];

/** `cdk.RemovalPolicy(value)` takes the member name; the TS enum values are other strings. */
function removalPolicyByName(name: string): RemovalPolicy {
  if (!Object.prototype.hasOwnProperty.call(RemovalPolicy, name)) {
    throw new Error(`'${name}' is not a valid RemovalPolicy`);
  }
  return RemovalPolicy[name as keyof typeof RemovalPolicy];
}

/** `config.get_int(key, required=True)` / `get_bool(key, required=True)`: missing or NULL raises. */
function requiredInt(config: ClusterConfig, key: string): number {
  const value = config.getInt(key);
  if (value === undefined) throw new ConfigKeyNotFound(`'${key}', key: ${key}`);
  return value;
}

function requiredBool(config: ClusterConfig, key: string): boolean {
  const value = config.getBool(key);
  if (value === undefined) throw new ConfigKeyNotFound(`'${key}', key: ${key}`);
  return value;
}

interface DescribedListener {
  DefaultActions?: { Type?: string; TargetGroupArn?: string }[];
}

/**
 * The listener ARNs this stack reads back at synth time. Both keys are optional: on a first
 * deploy neither listener exists yet, and nothing ever writes the three DCV broker keys.
 */
const LISTENER_ARN_KEYS = [
  'cluster.load_balancers.external_alb.https_listener_arn',
  'cluster.external_alb.dcv_broker_client_listener_arn',
  'cluster.external_alb.dcv_broker_agent_listener_arn',
  'cluster.external_alb.dcv_broker_gateway_listener_arn',
];

/**
 * `elbv2:DescribeListeners` for every listener ARN in the settings, before the tree is built.
 * `SynthReads` is asynchronous and a stack constructor is not, so `buildStack` does the reads and
 * hands the results in. A read that fails fails the synth: guessing here resets the external
 * listener's default action and takes the web portal offline.
 */
async function describeListeners(ctx: StackBuildProps['ctx']): Promise<Map<string, DescribedListener>> {
  const listeners = new Map<string, DescribedListener>();
  for (const key of LISTENER_ARN_KEYS) {
    const arn = ctx.config.getString(key);
    if (isEmpty(arn) || listeners.has(arn as string)) continue;
    listeners.set(arn as string, await ctx.synthReads.describeListener(arn as string));
  }
  return listeners;
}

export class ClusterStack extends IdeaBaseStack {
  /** The listeners `buildStack` read back, by ARN. */
  private readonly describedListeners: Map<string, DescribedListener>;
  private newVpc: Vpc | undefined;
  private existingVpc: ExistingVpc | undefined;

  vpcInterfaceEndpoints: Record<string, VpcInterfaceEndpoint> | undefined;

  selfSignedCertificateLambda: LambdaFunction | undefined;
  externalCertificate: CustomResource | undefined;
  internalCertificate: CustomResource | undefined;

  backupRole: Role | undefined;
  backupVault: backup.BackupVault | undefined;
  backupPlan: BackupPlan | undefined;

  clusterEndpointsLambda: LambdaFunction | undefined;
  externalAlb: elbv2.ApplicationLoadBalancer | undefined;
  externalAlbHttpsListener: elbv2.CfnListener | undefined;
  externalAlbWafWebAcl: WebAcl | undefined;
  internalAlb: elbv2.ApplicationLoadBalancer | undefined;
  internalAlbHttpsListener: elbv2.CfnListener | undefined;
  internalAlbDcvBrokerClientListener: elbv2.CfnListener | undefined;
  internalAlbDcvBrokerAgentListener: elbv2.CfnListener | undefined;
  internalAlbDcvBrokerGatewayListener: elbv2.CfnListener | undefined;
  internalAlbDnsRecordSet: route53.RecordSet | route53.CnameRecord | undefined;

  privateHostedZone: PrivateHostedZone | undefined;
  clusterPrefixList: ec2.CfnPrefixList | undefined;
  readonly securityGroups: Record<string, SecurityGroup> = {};
  readonly roles: Record<string, Role> = {};
  amazonSsmManagedInstanceCorePolicy: ManagedPolicy | undefined;
  cloudWatchAgentServerPolicy: ManagedPolicy | undefined;
  amazonPrometheusRemoteWritePolicy: ManagedPolicy | undefined;

  solutionMetricsLambda: LambdaFunction | undefined;
  clusterSettingsLambda: LambdaFunction | undefined;

  ec2EventsSnsTopic: SNSTopic | undefined;

  constructor(props: StackBuildProps, describedListeners: Map<string, DescribedListener> = new Map()) {
    super({
      scope: props.app,
      ctx: props.ctx,
      moduleName: props.moduleName,
      deploymentId: props.deploymentId,
      terminationProtection: props.terminationProtection,
      env: props.env,
    });

    this.describedListeners = describedListeners;
    this.buildBackups();
    this.buildPolicies();
    this.buildRoles();
    this.buildSelfSignedCertificatesLambda();
    this.buildSelfSignedCertificates();
    this.buildClusterSettingsLambda();
    this.buildVpc();
    this.buildClusterPrefixList();
    this.buildSecurityGroups();
    this.buildPrivateHostedZone();
    this.buildEc2NotificationModule();
    this.buildClusterEndpoints();
    this.buildVpcEndpoints();
    this.buildSolutionMetricsLambda();
    this.buildClusterSettings();
  }

  private get config() {
    return this.context.config;
  }

  private useExistingVpc(): boolean {
    return this.config.getBool('cluster.network.use_existing_vpc', false);
  }

  get vpc(): ec2.IVpc {
    return this.useExistingVpc() ? (this.existingVpc as ExistingVpc).vpc : (this.newVpc as Vpc);
  }

  publicSubnets(): ec2.ISubnet[] {
    return this.useExistingVpc() ? (this.existingVpc as ExistingVpc).getPublicSubnets() : this.vpc.publicSubnets;
  }

  privateSubnets(): ec2.ISubnet[] {
    return this.useExistingVpc() ? (this.existingVpc as ExistingVpc).getPrivateSubnets() : this.vpc.privateSubnets;
  }

  // --- backups ---------------------------------------------------------------------------------

  buildBackups(): void {
    if (!this.config.getBool('cluster.backups.enabled', false)) return;

    const enableRestore = this.config.getBool('cluster.backups.enable_restore', true);

    // Backup policies have to be managed policies: as inline policies they exceed the 10240 byte
    // limit on the role.
    const backupCreatePolicy = new ManagedPolicy(this.context, 'backup-create-policy', this.stack, {
      managedPolicyName: `${this.clusterName}-${this.awsRegion}-backup-create`,
      description:
        'Provides AWS Backup permission to create backups on your behalf across AWS services',
      policyTemplateName: 'backup-create.yml',
    });
    const backupS3CreatePolicy = new ManagedPolicy(this.context, 'backup-s3-create-policy', this.stack, {
      managedPolicyName: `${this.clusterName}-${this.awsRegion}-backup-s3-create`,
      description:
        'Policy containing permissions necessary for AWS Backup to backup data in any S3 bucket. ' +
        'This includes read access to all S3 objects and any decrypt access for all KMS keys.',
      policyTemplateName: 'backup-s3-create.yml',
    });

    let backupRestorePolicy: ManagedPolicy | undefined;
    let backupS3RestorePolicy: ManagedPolicy | undefined;
    if (enableRestore) {
      backupRestorePolicy = new ManagedPolicy(this.context, 'backup-restore-policy', this.stack, {
        managedPolicyName: `${this.clusterName}-${this.awsRegion}-backup-restore`,
        description:
          'Provides AWS Backup permission to perform restores on your behalf across AWS services. ' +
          'This policy includes permissions to create and delete AWS resources, such as EBS volumes, RDS instances, and EFS file systems, which are part of the restore process.',
        policyTemplateName: 'backup-restore.yml',
      });
      backupS3RestorePolicy = new ManagedPolicy(this.context, 'backup-s3-restore-policy', this.stack, {
        managedPolicyName: `${this.clusterName}-${this.awsRegion}-backup-s3-restore`,
        description:
          'Policy containing permissions necessary for AWS Backup to restore a S3 backup to a bucket. ' +
          'This includes read/write permissions to all S3 buckets, and permissions to GenerateDataKey and DescribeKey for all KMS keys.',
        policyTemplateName: 'backup-s3-restore.yml',
      });
    }

    const backupRole = new Role(this.context, `${this.moduleId}-backup-role`, this.stack, {
      description: 'Role used by AWS Backup to authenticate when backing or restoring the resources',
      assumedBy: ['backup'],
    });
    backupRole.addManagedPolicy(backupCreatePolicy);
    backupRole.addManagedPolicy(backupS3CreatePolicy);
    if (enableRestore) {
      backupRole.addManagedPolicy(backupRestorePolicy as ManagedPolicy);
      backupRole.addManagedPolicy(backupS3RestorePolicy as ManagedPolicy);
    }

    const backupVaultRemovalPolicy = this.config.getString(
      'cluster.backups.backup_vault.removal_policy',
      'DESTROY',
    );
    const backupVaultKmsKeyId = this.config.getString('cluster.backups.backup_vault.kms_key_id');
    const backupVaultEncryptionKey = isEmpty(backupVaultKmsKeyId)
      ? undefined
      : kms.Key.fromKeyArn(this.stack, 'backup-vault-kms-key', kmsKeyArn(this.context, backupVaultKmsKeyId as string));
    const backupVault = new backup.BackupVault(this.stack, 'backup-vault', {
      backupVaultName: `${this.clusterName}-${this.moduleId}-backup-vault`,
      encryptionKey: backupVaultEncryptionKey,
      removalPolicy: removalPolicyByName(backupVaultRemovalPolicy),
    });

    // The immutable reference keeps BackupSelection from attaching the AWS Backup managed
    // policies to the role; the copied policies above are what the role carries.
    const backupPlan = new BackupPlan(this.stack, {
      backupPlanName: `${this.clusterName}-${this.moduleId}`,
      backupPlanConfig: this.config.getConfig('cluster.backups.backup_plan'),
      backupVault,
      backupRole: backupRole.withoutPolicyUpdates(),
    });

    backupPlan.backupSelection.node.addDependency(backupCreatePolicy);
    backupPlan.backupSelection.node.addDependency(backupS3CreatePolicy);
    if (enableRestore) {
      backupPlan.backupSelection.node.addDependency(backupRestorePolicy as ManagedPolicy);
      backupPlan.backupSelection.node.addDependency(backupS3RestorePolicy as ManagedPolicy);
    }
    backupPlan.backupSelection.node.addDependency(backupRole);

    this.backupRole = backupRole;
    this.backupVault = backupVault;
    this.backupPlan = backupPlan;
  }

  // --- policies and roles ----------------------------------------------------------------------

  buildPolicies(): void {
    this.amazonSsmManagedInstanceCorePolicy = new ManagedPolicy(
      this.context,
      'amazon-ssm-managed-instance-core',
      this.stack,
      {
        managedPolicyName: `${this.clusterName}-${this.awsRegion}-amazon-ssm-managed-instance-core`,
        description:
          'The policy for Amazon EC2 Role to enable AWS Systems Manager service core functionality.',
        policyTemplateName: 'amazon-ssm-managed-instance-core.yml',
      },
    );

    this.cloudWatchAgentServerPolicy = new ManagedPolicy(
      this.context,
      'cloud-watch-agent-server-policy',
      this.stack,
      {
        managedPolicyName: `${this.clusterName}-${this.awsRegion}-cloud-watch-agent-server-policy`,
        description: 'Permissions required to use AmazonCloudWatchAgent on servers',
        policyTemplateName: 'cloud-watch-agent-server-policy.yml',
      },
    );

    if (this.isMetricsProviderAmazonManagedPrometheus()) {
      this.amazonPrometheusRemoteWritePolicy = new ManagedPolicy(
        this.context,
        'amazon-prometheus-remote-write-access',
        this.stack,
        {
          managedPolicyName: `${this.clusterName}-${this.awsRegion}-amazon-prometheus-remote-write-access`,
          description: 'Grants write only access to AWS Managed Prometheus workspaces',
          policyTemplateName: 'amazon-prometheus-remote-write-access.yml',
        },
      );
    }
  }

  buildRoles(): void {
    // The policy is a constructor argument, so it is created before the role and precedes it in
    // the template.
    this.roles[LOG_RETENTION_ROLE_NAME] = new Role(this.context, LOG_RETENTION_ROLE_NAME, this.stack, {
      description: 'log retention role for CDK custom resources',
      assumedBy: ['lambda'],
      inlinePolicies: [
        new Policy(this.context, 'LogRetention', this.stack, { policyTemplateName: 'log-retention.yml' }),
      ],
    });
  }

  private get logRetentionRole(): iam.IRole {
    return this.roles[LOG_RETENTION_ROLE_NAME] as Role;
  }

  private addPythonRuntimeSuppression(lambdaFunction: LambdaFunction): void {
    this.addNagSuppression(
      [{ rule_id: 'AwsSolutions-L1', reason: 'Python Runtime is selected for stability.' }],
      lambdaFunction,
    );
  }

  // --- certificates ----------------------------------------------------------------------------

  buildSelfSignedCertificatesLambda(): void {
    const lambdaName = 'self-signed-certificate';

    const policy = new Policy(this.context, `${lambdaName}-policy`, this.stack, {
      policyTemplateName: 'custom-resource-self-signed-certificate.yml',
    });
    const role = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `Role for generating self-signed certificates Lambda function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda'],
    });
    role.attachInlinePolicy(policy);

    this.selfSignedCertificateLambda = new LambdaFunction(this.context, lambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_self_signed_certificate'),
      description: 'Manage self-signed certificates for IDEA cluster infrastructure',
      timeoutSeconds: 180,
      role,
      logRetentionRole: this.logRetentionRole,
    });
    // Without the explicit dependencies, stack deletion races the policy against the function.
    this.selfSignedCertificateLambda.node.addDependency(policy);
    this.selfSignedCertificateLambda.node.addDependency(role);
    this.addPythonRuntimeSuppression(this.selfSignedCertificateLambda);
  }

  buildSelfSignedCertificates(): void {
    const serviceToken = (this.selfSignedCertificateLambda as LambdaFunction).functionArn;
    const kmsKeyId = this.config.getString('cluster.secretsmanager.kms_key_id');

    if (!this.config.getBool('cluster.load_balancers.external_alb.certificates.provided', false)) {
      this.externalCertificate = new CustomResource(
        this.stack,
        `${this.clusterName}-${this.moduleId}-external-cert`,
        {
          serviceToken,
          properties: {
            domain_name: `${this.clusterName}.idea.default`,
            certificate_name: `${this.clusterName}-external`,
            create_acm_certificate: true,
            kms_key_id: kmsKeyId,
            tags: {
              Name: `${this.clusterName} external alb certs`,
              'idea:ClusterName': this.clusterName,
            },
          },
          resourceType: 'Custom::SelfSignedCertificateExternal',
        },
      );
      this.externalCertificate.node.addDependency(this.selfSignedCertificateLambda as LambdaFunction);
    }

    const privateHostedZoneName = this.config.getString('cluster.route53.private_hosted_zone_name', undefined, {
      required: true,
    }) as string;
    this.internalCertificate = new CustomResource(
      this.stack,
      `${this.clusterName}-${this.moduleId}-internal-cert`,
      {
        serviceToken,
        properties: {
          domain_name: `*.${privateHostedZoneName}`,
          certificate_name: `${this.clusterName}-internal`,
          create_acm_certificate: true,
          kms_key_id: kmsKeyId,
          tags: {
            Name: `${this.clusterName} internal alb certs`,
            'idea:ClusterName': this.clusterName,
          },
        },
        resourceType: 'Custom::SelfSignedCertificateInternal',
      },
    );
    this.internalCertificate.node.addDependency(this.selfSignedCertificateLambda as LambdaFunction);
  }

  // --- lambdas ---------------------------------------------------------------------------------

  buildClusterSettingsLambda(): void {
    const lambdaName = 'cluster-settings';

    const role = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `Role for cluster-settings lambda function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda'],
    });
    const policy = new Policy(this.context, `${lambdaName}-policy`, this.stack, {
      policyTemplateName: 'custom-resource-update-cluster-settings.yml',
    });

    this.clusterSettingsLambda = new LambdaFunction(this.context, lambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_update_cluster_settings'),
      description: 'Update cluster settings during cluster module deployment',
      timeoutSeconds: 180,
      role,
      logRetentionRole: this.logRetentionRole,
    });
    role.attachInlinePolicy(policy);
    this.clusterSettingsLambda.node.addDependency(policy);
    this.clusterSettingsLambda.node.addDependency(role);
    this.addPythonRuntimeSuppression(this.clusterSettingsLambda);
  }

  buildSolutionMetricsLambda(): void {
    const lambdaName = 'solution-metrics';

    const role = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `Role for solution-metrics metrics Lambda function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda'],
    });
    const policy = new Policy(this.context, `${lambdaName}-policy`, this.stack, {
      policyTemplateName: 'solution-metrics-lambda-function.yml',
    });

    this.solutionMetricsLambda = new LambdaFunction(this.context, lambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_solution_metrics'),
      description: 'Send anonymous Metrics to AWS',
      timeoutSeconds: 180,
      role,
      logRetentionRole: this.logRetentionRole,
    });
    role.attachInlinePolicy(policy);
    this.solutionMetricsLambda.node.addDependency(policy);
    this.solutionMetricsLambda.node.addDependency(role);
    this.addPythonRuntimeSuppression(this.solutionMetricsLambda);
  }

  // --- network ---------------------------------------------------------------------------------

  buildVpc(): void {
    // An existing VPC is looked up and never modified: no VPC resources are created and the
    // administrator carries the same configuration into every upgrade.
    if (this.useExistingVpc()) {
      this.existingVpc = new ExistingVpc(this.context, 'existing-vpc', this.stack);
    } else {
      this.newVpc = new Vpc(this.context, 'vpc', this.stack);
    }
  }

  buildPrivateHostedZone(): void {
    this.privateHostedZone = new PrivateHostedZone(this.context, this.stack, this.vpc);
  }

  /**
   * The cluster prefix list is the one place administrators manage external access from. Entries
   * are added out of band by `idea-admin utils cluster-prefix-list`, so the stack only ever
   * creates the list and adds the configured client IPs; it never removes an entry.
   */
  buildClusterPrefixList(): void {
    const maxEntries = this.config.getInt('cluster.network.cluster_prefix_list_max_entries', 10);
    this.clusterPrefixList = new ec2.CfnPrefixList(this.stack, 'cluster-prefix-list', {
      addressFamily: 'IPv4',
      maxEntries,
      prefixListName: `${this.clusterName}-prefix-list`,
    });
    this.addCommonTags(this.clusterPrefixList);

    const clientIps = this.config.getList<string>('cluster.network.client_ip', []);
    if (isEmpty(clientIps)) return;

    const lambdaName = 'update-cluster-prefix-list';
    const policy = new Policy(this.context, `${lambdaName}-policy`, this.stack, {
      policyTemplateName: 'custom-resource-update-cluster-prefix-list.yml',
    });
    const role = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `Role to manage cluster prefix list Lambda function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda'],
    });
    role.attachInlinePolicy(policy);

    const lambdaFunction = new LambdaFunction(this.context, lambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_update_cluster_prefix_list'),
      description: 'Manage Cluster Prefix List',
      timeoutSeconds: 180,
      role,
      logRetentionRole: this.logRetentionRole,
    });
    lambdaFunction.node.addDependency(policy);
    lambdaFunction.node.addDependency(role);
    lambdaFunction.node.addDependency(this.clusterPrefixList);
    this.addPythonRuntimeSuppression(lambdaFunction);

    const entries = clientIps.map((clientIp) => ({
      Cidr: clientIp.includes('/') ? clientIp : `${clientIp}/32`,
      Description: 'Allow access to cluster from Client IP',
    }));

    const customResource = new CustomResource(
      this.stack,
      `${this.clusterName}-${this.moduleId}-cluster-prefix-list`,
      {
        serviceToken: lambdaFunction.functionArn,
        properties: {
          prefix_list_id: this.clusterPrefixList.attrPrefixListId,
          add_entries: entries,
        },
        resourceType: 'Custom::ClusterPrefixList',
      },
    );
    customResource.node.addDependency(lambdaFunction);
  }

  buildSecurityGroups(): void {
    const prefixList = this.clusterPrefixList as ec2.CfnPrefixList;

    this.securityGroups['cluster'] = new DefaultClusterSecurityGroup(
      this.context,
      'default-security-group',
      this.stack,
      this.vpc,
    );

    const bastionHostSecurityGroup = new BastionHostSecurityGroup(
      this.context,
      'bastion-host-security-group',
      this.stack,
      this.vpc,
      prefixList.attrPrefixListId,
    );
    this.securityGroups['bastion-host'] = bastionHostSecurityGroup;

    const externalLoadBalancerSecurityGroup = new ExternalLoadBalancerSecurityGroup(
      this.context,
      'external-load-balancer-security-group',
      this.stack,
      this.vpc,
      prefixList.attrPrefixListId,
      bastionHostSecurityGroup,
    );
    this.securityGroups['external-load-balancer'] = externalLoadBalancerSecurityGroup;

    this.securityGroups['internal-load-balancer'] = new InternalLoadBalancerSecurityGroup(
      this.context,
      'internal-load-balancer-security-group',
      this.stack,
      this.vpc,
    );

    const natEips: ec2.CfnEIP[] = [];
    for (const subnet of this.vpc.publicSubnets) {
      const eip = subnet.node.tryFindChild('EIP');
      if (eip !== undefined) natEips.push(eip as ec2.CfnEIP);
    }
    if (natEips.length > 0) {
      externalLoadBalancerSecurityGroup.addNatGatewayIpsIngressRule(natEips);
    }

    if (!this.useExistingVpc() && this.config.getBool('cluster.network.use_vpc_endpoints', false)) {
      this.securityGroups['vpc-endpoint'] = new VpcEndpointSecurityGroup(
        this.context,
        'vpc-endpoint-security-group',
        this.stack,
        this.vpc,
      );
    }
  }

  buildVpcEndpoints(): void {
    if (!this.config.getBool('cluster.network.use_vpc_endpoints', false)) return;
    if (this.useExistingVpc()) return;

    const gatewayEndpoints: Record<string, VpcGatewayEndpoint> = {};
    const interfaceEndpoints: Record<string, VpcInterfaceEndpoint> = {};

    const createTags = new CreateTagsCustomResource(this.context, this.stack, this.logRetentionRole);

    for (const service of this.config.getList<string>('cluster.network.vpc_gateway_endpoints', [])) {
      gatewayEndpoints[service] = new VpcGatewayEndpoint(this.context, this.stack, service, this.vpc, createTags);
    }

    const configured = this.config.getConfig('cluster.network.vpc_interface_endpoints', {}) ?? {};
    for (const [service, endpointConfig] of Object.entries(configured)) {
      const enabled = (endpointConfig as Record<string, unknown> | undefined)?.['enabled'];
      if (enabled !== true) continue;
      interfaceEndpoints[service] = new VpcInterfaceEndpoint(
        this.context,
        this.stack,
        service,
        this.vpc,
        this.securityGroups['vpc-endpoint'] as SecurityGroup,
        createTags,
      );
    }

    this.vpcInterfaceEndpoints = interfaceEndpoints;
  }

  // --- ec2 state change notifications ------------------------------------------------------------

  buildEc2NotificationModule(): void {
    this.ec2EventsSnsTopic = new SNSTopic(this.context, 'cluster-ec2-state-change-sns-topic', this.stack, {
      displayName: `${this.clusterName}-${this.moduleId}-ec2-state-change-sns-topic`,
      topicName: `${this.clusterName}-${this.moduleId}-ec2-state-change-sns-topic`,
      masterKey: this.config.getString('cluster.sns.kms_key_id'),
    });
    this.addCommonTags(this.ec2EventsSnsTopic);

    const lambdaName = `${this.moduleId}-ec2state-event-transformer`;
    const role = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `${lambdaName}-role`,
      assumedBy: ['lambda'],
    });
    role.attachInlinePolicy(
      new Policy(this.context, `${lambdaName}-policy`, this.stack, {
        policyTemplateName: 'ec2state-event-transformer.yml',
      }),
    );

    const transformer = new LambdaFunction(this.context, lambdaName, this.stack, {
      description: `${this.moduleId} lambda to intercept all ec2 state change events and transform to the required event object`,
      environment: {
        IDEA_EC2_STATE_SNS_TOPIC_ARN: this.ec2EventsSnsTopic.topicArn,
        IDEA_CLUSTER_NAME_TAG_KEY: 'idea:ClusterName',
        IDEA_CLUSTER_NAME_TAG_VALUE: this.clusterName,
        IDEA_TAG_PREFIX: 'idea:',
      },
      timeoutSeconds: 180,
      role,
      ideaCodeAsset: new IdeaCodeAsset('idea_ec2_state_event_transformation_lambda'),
    });
    this.addPythonRuntimeSuppression(transformer);

    const rule = new events.Rule(this.stack, `${this.clusterName}-ec2-state-monitoring-rule`, {
      enabled: true,
      ruleName: `${this.clusterName}-${this.moduleId}-ec2-state-monitoring-rule`,
      description: 'Event Rule to monitor state changes on EC2 Instances',
      eventPattern: {
        source: ['aws.ec2'],
        detailType: ['EC2 Instance State-change Notification'],
        region: [this.awsRegion],
      },
    });
    rule.addTarget(new eventsTargets.LambdaFunction(transformer));
  }

  // --- load balancers --------------------------------------------------------------------------

  /**
   * A listener needs a default action at create time, but the cluster-manager and the
   * virtual-desktop-controller stacks repoint it afterwards through the cluster-endpoints lambda.
   * Re-reading the live listener keeps this stack from resetting their target group. Only
 * `forward` is carried over.
   */
  getAlbListenerDefaultActions(listenerArn?: string): elbv2.CfnListener.ActionProperty[] {
    if (!isEmpty(listenerArn)) {
      const listener = this.describedListeners.get(listenerArn as string);
      if (listener === undefined) {
        throw new Error(
          `listener ${listenerArn as string} was not read at synth time: add its setting key to LISTENER_ARN_KEYS`,
        );
      }
      const existingAction = listener.DefaultActions?.[0];
      if (existingAction?.Type === 'forward') {
        return [
          {
            type: 'forward',
            forwardConfig: {
              targetGroups: [{ targetGroupArn: existingAction.TargetGroupArn }],
            },
          },
        ];
      }
    }
    return [
      {
        type: 'fixed-response',
        fixedResponseConfig: {
          statusCode: '200',
          contentType: 'application/json',
          messageBody: JSON.stringify({ success: true, message: 'OK' }),
        },
      },
    ];
  }

  buildClusterEndpoints(): void {
    const lambdaName = 'cluster-endpoints';

    const clusterEndpointsPolicy = new Policy(this.context, `${lambdaName}-policy`, this.stack, {
      policyTemplateName: 'custom-resource-cluster-endpoints.yml',
    });
    const clusterEndpointsRole = new Role(this.context, `${lambdaName}-role`, this.stack, {
      description: `Role for cluster endpoints lambda function for Cluster: ${this.clusterName}`,
      assumedBy: ['lambda'],
    });
    clusterEndpointsRole.attachInlinePolicy(clusterEndpointsPolicy);

    this.clusterEndpointsLambda = new LambdaFunction(this.context, lambdaName, this.stack, {
      ideaCodeAsset: new IdeaCodeAsset('idea_custom_resource_cluster_endpoints'),
      description: 'Manage cluster endpoints exposed via internal and external ALB',
      timeoutSeconds: 600,
      role: clusterEndpointsRole,
      logRetentionRole: this.logRetentionRole,
    });
    this.clusterEndpointsLambda.node.addDependency(clusterEndpointsPolicy);
    this.clusterEndpointsLambda.node.addDependency(clusterEndpointsRole);
    this.addPythonRuntimeSuppression(this.clusterEndpointsLambda);

    // external ALB: public or private subnets
    const isPublic = this.config.getBool('cluster.load_balancers.external_alb.public', true);
    this.externalAlb = new elbv2.ApplicationLoadBalancer(this.stack, `${this.clusterName}-external-alb`, {
      loadBalancerName: `${this.clusterName}-external-alb`,
      securityGroup: this.securityGroups['external-load-balancer'] as SecurityGroup,
      http2Enabled: true,
      vpc: this.vpc,
      vpcSubnets: { subnets: isPublic ? this.publicSubnets() : this.privateSubnets() },
      internetFacing: isPublic,
      dropInvalidHeaderFields: true,
    });
    // large file downloads need more than the 60 second default
    this.externalAlb.setAttribute(
      'idle_timeout.timeout_seconds',
      String(this.config.getInt('cluster.load_balancers.external_alb.idle_timeout_seconds', 600)),
    );
    if (this.externalCertificate !== undefined) {
      this.externalAlb.node.addDependency(this.externalCertificate);
    }

    if (this.config.getBool('cluster.load_balancers.external_alb.waf.enabled', false)) {
      this.externalAlbWafWebAcl = new WebAcl(this.context, 'external-alb', this.stack);

      const wafAssociation = new wafv2.CfnWebACLAssociation(
        this.stack,
        `${this.clusterName}-external-alb-waf-association`,
        {
          resourceArn: this.externalAlb.loadBalancerArn,
          webAclArn: this.externalAlbWafWebAcl.webAclArn,
        },
      );
      wafAssociation.node.addDependency(this.externalAlb);
      wafAssociation.node.addDependency(this.externalAlbWafWebAcl.webAcl);
    }

    // internal ALB: always private subnets
    this.internalAlb = new elbv2.ApplicationLoadBalancer(this.stack, `${this.clusterName}-internal-alb`, {
      loadBalancerName: `${this.clusterName}-internal-alb`,
      securityGroup: this.securityGroups['internal-load-balancer'] as SecurityGroup,
      http2Enabled: true,
      vpc: this.vpc,
      vpcSubnets: { subnets: this.privateSubnets() },
      internetFacing: false,
      dropInvalidHeaderFields: true,
    });
    this.internalAlb.setAttribute(
      'idle_timeout.timeout_seconds',
      String(this.config.getInt('cluster.load_balancers.internal_alb.idle_timeout_seconds', 600)),
    );

    const externalAccessLogs = this.config.getBool('cluster.load_balancers.external_alb.access_logs', false);
    const internalAccessLogs = this.config.getBool('cluster.load_balancers.internal_alb.access_logs', false);
    if (externalAccessLogs || internalAccessLogs) {
      const accessLogDestination = lookupClusterS3Bucket(this.context, this.stack);
      if (externalAccessLogs) {
        this.externalAlb.logAccessLogs(
          accessLogDestination,
          `logs/${this.moduleId}/alb-access-logs/external-alb`,
        );
      }
      if (internalAccessLogs) {
        this.internalAlb.logAccessLogs(
          accessLogDestination,
          `logs/${this.moduleId}/alb-access-logs/internal-alb`,
        );
      }
    }

    new elbv2.CfnListener(this.externalAlb, 'http-listener', {
      port: 80,
      loadBalancerArn: this.externalAlb.loadBalancerArn,
      protocol: 'HTTP',
      defaultActions: [
        {
          type: 'redirect',
          redirectConfig: {
            host: '#{host}',
            path: '/#{path}',
            port: '443',
            protocol: 'HTTPS',
            query: '#{query}',
            statusCode: 'HTTP_301',
          },
        },
      ],
    });

    const externalAlbDefaultActions = this.getAlbListenerDefaultActions(
      this.config.getString('cluster.load_balancers.external_alb.https_listener_arn'),
    );

    const externalAcmCertificateArn =
      this.externalCertificate === undefined
        ? (this.config.getString('cluster.load_balancers.external_alb.certificates.acm_certificate_arn', undefined, {
            required: true,
          }) as string)
        : this.externalCertificate.getAttString('acm_certificate_arn');

    this.externalAlbHttpsListener = new elbv2.CfnListener(this.externalAlb, 'https-listener', {
      port: 443,
      sslPolicy: this.config.getString('cluster.load_balancers.external_alb.ssl_policy', DEFAULT_SSL_POLICY),
      loadBalancerArn: this.externalAlb.loadBalancerArn,
      protocol: 'HTTPS',
      certificates: [{ certificateArn: externalAcmCertificateArn }],
      defaultActions: externalAlbDefaultActions,
    });
    if (this.externalCertificate !== undefined) {
      this.externalAlbHttpsListener.node.addDependency(this.externalCertificate);
    }

    const internalCertificate = this.internalCertificate as CustomResource;
    this.internalAlb.node.addDependency(internalCertificate);

    const internalAcmCertificateArn = internalCertificate.getAttString('acm_certificate_arn');
    this.internalAlbHttpsListener = new elbv2.CfnListener(this.internalAlb, 'https-listener', {
      port: 443,
      sslPolicy: this.config.getString('cluster.load_balancers.internal_alb.ssl_policy', DEFAULT_SSL_POLICY),
      loadBalancerArn: this.internalAlb.loadBalancerArn,
      protocol: 'HTTPS',
      certificates: [{ certificateArn: internalAcmCertificateArn }],
      defaultActions: [
        {
          type: 'fixed-response',
          fixedResponseConfig: {
            statusCode: '200',
            contentType: 'application/json',
            messageBody: JSON.stringify({ success: true, message: 'OK' }),
          },
        },
      ],
    });
    this.internalAlbHttpsListener.node.addDependency(internalCertificate);

    const privateHostedZone = this.privateHostedZone as PrivateHostedZone;
    const recordName = `internal-alb.${privateHostedZone.zoneName}`;
    if (ROUTE53_CROSS_ZONE_ALIAS_RESTRICTED_REGIONS.includes(this.awsRegion)) {
      this.internalAlbDnsRecordSet = new route53.CnameRecord(this.stack, 'internal-alb-dns-record', {
        recordName,
        zone: privateHostedZone,
        domainName: this.internalAlb.loadBalancerDnsName,
        ttl: Duration.minutes(5),
      });
    } else {
      this.internalAlbDnsRecordSet = new route53.RecordSet(this.stack, 'internal-alb-dns-record', {
        recordType: route53.RecordType.A,
        target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(this.internalAlb)),
        recordName,
        zone: privateHostedZone,
      });
    }

    if (this.config.isModuleEnabled(MODULE_VIRTUAL_DESKTOP_CONTROLLER)) {
      this.internalAlbDcvBrokerClientListener = this.buildDcvBrokerListener(
        'dcv-broker-client-listener',
        'client',
        internalAcmCertificateArn,
        'Allow HTTPS traffic from DCV Clients to DCV Broker',
      );
      this.internalAlbDcvBrokerAgentListener = this.buildDcvBrokerListener(
        'dcv-broker-agent-listener',
        'agent',
        internalAcmCertificateArn,
        'Allow HTTPS traffic from DCV Agents to DCV Broker',
      );
      this.internalAlbDcvBrokerGatewayListener = this.buildDcvBrokerListener(
        'dcv-broker-gateway-listener',
        'gateway',
        internalAcmCertificateArn,
        'Allow HTTPS traffic from DCV Connection Gateway to DCV Broker',
      );
    }
  }

  /**
   * The listener ARN is read from `cluster.external_alb.dcv_broker_<kind>_listener_arn`, which
   * nothing writes, so the default action is always the fixed response. Reading the key the stack
   * actually writes would embed the broker target group and change the property on every cluster.
   */
  private buildDcvBrokerListener(
    listenerId: string,
    kind: 'client' | 'agent' | 'gateway',
    certificateArn: string,
    ingressDescription: string,
  ): elbv2.CfnListener {
    const defaultActions = this.getAlbListenerDefaultActions(
      this.config.getString(`cluster.external_alb.dcv_broker_${kind}_listener_arn`),
    );
    const port = requiredInt(this.config, `virtual-desktop-controller.dcv_broker.${kind}_communication_port`);
    const internalAlb = this.internalAlb as elbv2.ApplicationLoadBalancer;
    const listener = new elbv2.CfnListener(internalAlb, listenerId, {
      port,
      sslPolicy: this.config.getString('virtual-desktop-controller.dcv_broker.ssl_policy', DEFAULT_SSL_POLICY),
      loadBalancerArn: internalAlb.loadBalancerArn,
      protocol: 'HTTPS',
      certificates: [{ certificateArn }],
      defaultActions,
    });
    listener.node.addDependency(this.internalCertificate as CustomResource);
    (this.securityGroups['internal-load-balancer'] as SecurityGroup).addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(port),
      ingressDescription,
    );
    return listener;
  }

  // --- cluster settings --------------------------------------------------------------------------

  buildClusterSettings(): void {
    // settings are written in this module's scope, so no key carries the module id
    const clusterSettings: Record<string, unknown> = {
      deployment_id: this.deploymentId,
      'network.vpc_id': this.vpc.vpcId,
      'network.cluster_prefix_list_id': (this.clusterPrefixList as ec2.CfnPrefixList).attrPrefixListId,
    };

    const publicSubnets = this.config.getList<string>('cluster.network.public_subnets', []);
    const isExternalAlbPublic = this.config.getBool('cluster.load_balancers.external_alb.public', true);
    if (isEmpty(publicSubnets) && isExternalAlbPublic) {
      for (const subnet of this.vpc.publicSubnets) publicSubnets.push(subnet.subnetId);
    }
    clusterSettings['network.public_subnets'] = publicSubnets;

    const privateSubnets = this.config.getList<string>('cluster.network.private_subnets', []);
    if (isEmpty(privateSubnets)) {
      for (const subnet of this.vpc.privateSubnets) privateSubnets.push(subnet.subnetId);
    }
    clusterSettings['network.private_subnets'] = privateSubnets;

    if (!this.useExistingVpc()) {
      clusterSettings['network.nat_gateway_ips'] = (this.newVpc as Vpc).natGatewayIps.map((eip) => eip.ref);
    }

    for (const [name, securityGroup] of Object.entries(this.securityGroups)) {
      clusterSettings[`network.security_groups.${name}`] = securityGroup.securityGroupId;
    }

    for (const [name, role] of Object.entries(this.roles)) {
      clusterSettings[`iam.roles.${name}`] = role.roleArn;
    }
    clusterSettings['iam.policies.amazon_ssm_managed_instance_core_arn'] = (
      this.amazonSsmManagedInstanceCorePolicy as ManagedPolicy
    ).managedPolicyArn;
    clusterSettings['iam.policies.cloud_watch_agent_server_arn'] = (
      this.cloudWatchAgentServerPolicy as ManagedPolicy
    ).managedPolicyArn;
    if (this.amazonPrometheusRemoteWritePolicy !== undefined) {
      clusterSettings['iam.policies.amazon_prometheus_remote_write_arn'] =
        this.amazonPrometheusRemoteWritePolicy.managedPolicyArn;
    }

    clusterSettings['solution.solution_metrics_lambda_arn'] = (
      this.solutionMetricsLambda as LambdaFunction
    ).functionArn;
    clusterSettings['cluster_settings_lambda_arn'] = (this.clusterSettingsLambda as LambdaFunction).functionArn;
    clusterSettings['self_signed_certificate_lambda_arn'] = (
      this.selfSignedCertificateLambda as LambdaFunction
    ).functionArn;

    const privateHostedZone = this.privateHostedZone as PrivateHostedZone;
    clusterSettings['route53.private_hosted_zone_id'] = privateHostedZone.hostedZoneId;
    clusterSettings['route53.private_hosted_zone_arn'] = privateHostedZone.hostedZoneArn;

    if (!requiredBool(this.config, 'cluster.load_balancers.external_alb.certificates.provided')) {
      const externalCertificate = this.externalCertificate as CustomResource;
      clusterSettings['load_balancers.external_alb.certificates.certificate_secret_arn'] =
        externalCertificate.getAttString('certificate_secret_arn');
      clusterSettings['load_balancers.external_alb.certificates.private_key_secret_arn'] =
        externalCertificate.getAttString('private_key_secret_arn');
      clusterSettings['load_balancers.external_alb.certificates.acm_certificate_arn'] =
        externalCertificate.getAttString('acm_certificate_arn');
    } else {
      clusterSettings['load_balancers.external_alb.certificates.provided'] = this.config.getString(
        'cluster.load_balancers.external_alb.certificates.provided',
        undefined,
        { required: true },
      );
      clusterSettings['load_balancers.external_alb.certificates.acm_certificate_arn'] = this.config.getString(
        'cluster.load_balancers.external_alb.certificates.acm_certificate_arn',
        undefined,
        { required: true },
      );
    }

    const internalCertificate = this.internalCertificate as CustomResource;
    clusterSettings['load_balancers.internal_alb.certificates.certificate_secret_arn'] =
      internalCertificate.getAttString('certificate_secret_arn');
    clusterSettings['load_balancers.internal_alb.certificates.private_key_secret_arn'] =
      internalCertificate.getAttString('private_key_secret_arn');
    clusterSettings['load_balancers.internal_alb.certificates.acm_certificate_arn'] =
      internalCertificate.getAttString('acm_certificate_arn');
    clusterSettings['load_balancers.internal_alb.certificates.custom_dns_name'] =
      `internal-alb.${privateHostedZone.zoneName}`;

    const externalAlb = this.externalAlb as elbv2.ApplicationLoadBalancer;
    const internalAlb = this.internalAlb as elbv2.ApplicationLoadBalancer;
    clusterSettings['cluster_endpoints_lambda_arn'] = (this.clusterEndpointsLambda as LambdaFunction).functionArn;
    clusterSettings['load_balancers.external_alb.load_balancer_arn'] = externalAlb.loadBalancerArn;
    clusterSettings['load_balancers.external_alb.load_balancer_dns_name'] = externalAlb.loadBalancerDnsName;
    clusterSettings['load_balancers.external_alb.https_listener_arn'] = (
      this.externalAlbHttpsListener as elbv2.CfnListener
    ).attrListenerArn;

    clusterSettings['load_balancers.internal_alb.load_balancer_arn'] = internalAlb.loadBalancerArn;
    clusterSettings['load_balancers.internal_alb.load_balancer_dns_name'] = internalAlb.loadBalancerDnsName;
    clusterSettings['load_balancers.internal_alb.https_listener_arn'] = (
      this.internalAlbHttpsListener as elbv2.CfnListener
    ).attrListenerArn;

    const ec2EventsSnsTopic = this.ec2EventsSnsTopic as SNSTopic;
    clusterSettings['ec2.state_change_notifications_sns_topic_arn'] = ec2EventsSnsTopic.topicArn;
    clusterSettings['ec2.state_change_notifications_sns_topic_name'] = ec2EventsSnsTopic.topicName;

    if (this.internalAlbDcvBrokerClientListener !== undefined) {
      clusterSettings['load_balancers.internal_alb.dcv_broker_client_listener_arn'] =
        this.internalAlbDcvBrokerClientListener.attrListenerArn;
    }
    if (this.internalAlbDcvBrokerAgentListener !== undefined) {
      clusterSettings['load_balancers.internal_alb.dcv_broker_agent_listener_arn'] =
        this.internalAlbDcvBrokerAgentListener.attrListenerArn;
    }
    if (this.internalAlbDcvBrokerGatewayListener !== undefined) {
      clusterSettings['load_balancers.internal_alb.dcv_broker_gateway_listener_arn'] =
        this.internalAlbDcvBrokerGatewayListener.attrListenerArn;
    }

    // An interface endpoint's url is written once, at provisioning: an administrator who edited
    // the configuration keeps their value.
    for (const [service, endpoint] of Object.entries(this.vpcInterfaceEndpoints ?? {})) {
      const endpointConfigKey = `network.vpc_interface_endpoints.${service}.endpoint_url`;
      const existingEndpointUrl = this.config.getString(`cluster.${endpointConfigKey}`);
      clusterSettings[endpointConfigKey] = isEmpty(existingEndpointUrl)
        ? endpoint.getEndpointUrl()
        : existingEndpointUrl;
    }

    if (this.config.getBool('cluster.backups.enabled', false)) {
      clusterSettings['backups.role_arn'] = (this.backupRole as Role).roleArn;
      clusterSettings['backups.backup_vault.arn'] = (this.backupVault as backup.BackupVault).backupVaultArn;
      clusterSettings['backups.backup_plan.arn'] = (this.backupPlan as BackupPlan).getBackupPlanArn();
    }

    // This stack owns the cluster-settings lambda, so the service token is a GetAtt rather than
    // the literal ARN every other module stack reads from the configuration.
    new CustomResource(this.stack, `${this.clusterName}-${this.moduleId}-settings`, {
      serviceToken: (this.clusterSettingsLambda as LambdaFunction).functionArn,
      properties: {
        cluster_name: this.clusterName,
        module_id: this.moduleId,
        version: this.releaseVersion,
        settings: clusterSettings,
      },
      resourceType: 'Custom::ClusterSettings',
    });
  }
}

export async function buildStack(props: StackBuildProps): Promise<void> {
  new ClusterStack(props, await describeListeners(props.ctx));
}
