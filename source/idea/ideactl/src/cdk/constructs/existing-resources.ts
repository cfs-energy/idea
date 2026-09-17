/**
 * Creates no resources: it imports the VPC (a context lookup resolved from `cdk.context.json`),
 * the cluster IAM roles and the cluster security groups under the construct ids every other
 * stack's logical IDs are hashed against (`vpc`, `<name>-role`, `<name>-security-group`).
 */

import type { Construct } from 'constructs';
import * as backup from 'aws-cdk-lib/aws-backup';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as opensearch from 'aws-cdk-lib/aws-opensearchservice';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';

import type { IdeaContext } from './base.ts';
import { kmsKeyArn } from './base.ts';

/**
 * `ExistingVpc`: the looked-up VPC plus the subnets named in
 * `cluster.network.{public,private}_subnets`, ordered by the config list.
 */
export class ExistingVpc {
  readonly ctx: IdeaContext;
  readonly scope: Construct;
  readonly vpcId: string;
  vpc: ec2.IVpc;
  private cachedPrivateSubnets: ec2.ISubnet[] | undefined;
  private cachedPublicSubnets: ec2.ISubnet[] | undefined;

  constructor(ctx: IdeaContext, _name: string, scope: Construct) {
    this.ctx = ctx;
    this.scope = scope;
    this.vpcId = ctx.config.getString('cluster.network.vpc_id', undefined, { required: true }) as string;
    this.vpc = ec2.Vpc.fromLookup(this.scope, 'vpc', { vpcId: this.vpcId });
  }

  getPublicSubnetIds(): string[] {
    return this.ctx.config.getList<string>('cluster.network.public_subnets', []);
  }

  getPrivateSubnetIds(): string[] {
    return this.ctx.config.getList<string>('cluster.network.private_subnets', []);
  }

  getPublicSubnets(): ec2.ISubnet[] {
    if (this.cachedPublicSubnets !== undefined) return this.cachedPublicSubnets;
    const ids = this.getPublicSubnetIds();
    this.cachedPublicSubnets =
      ids.length === 0 ? [] : orderByConfig(this.vpc.publicSubnets ?? [], ids);
    return this.cachedPublicSubnets;
  }

  /**
   * Both `privateSubnets` and `isolatedSubnets` are searched: CDK buckets a subnet without a NAT
   * gateway as isolated, and IDEA calls both "private". Order follows the config list.
   */
  getPrivateSubnets(): ec2.ISubnet[] {
    if (this.cachedPrivateSubnets !== undefined) return this.cachedPrivateSubnets;
    const ids = this.getPrivateSubnetIds();
    if (ids.length === 0) {
      this.cachedPrivateSubnets = [];
      return this.cachedPrivateSubnets;
    }
    this.cachedPrivateSubnets = orderByConfig(
      [...(this.vpc.privateSubnets ?? []), ...(this.vpc.isolatedSubnets ?? [])],
      ids,
    );
    return this.cachedPrivateSubnets;
  }
}

function orderByConfig(subnets: ec2.ISubnet[], ids: string[]): ec2.ISubnet[] {
  return subnets
    .filter((subnet) => ids.includes(subnet.subnetId))
    .sort((a, b) => ids.indexOf(a.subnetId) - ids.indexOf(b.subnetId));
}

/** `ExistingSocaCluster`: the VPC, the `cluster.iam.roles` and the `cluster.network.security_groups`. */
export class ExistingSocaCluster {
  readonly ctx: IdeaContext;
  readonly scope: Construct;
  readonly existingVpc: ExistingVpc;
  readonly securityGroups: Record<string, ec2.ISecurityGroup>;
  readonly roles: Record<string, iam.IRole>;

  constructor(ctx: IdeaContext, scope: Construct) {
    this.ctx = ctx;
    this.scope = scope;
    this.existingVpc = new ExistingVpc(ctx, 'existing-vpc', scope);
    this.securityGroups = {};
    this.roles = {};
    this.lookupRoles();
    this.lookupSecurityGroups();
  }

  get vpc(): ec2.IVpc {
    return this.existingVpc.vpc;
  }

  get publicSubnets(): ec2.ISubnet[] {
    return this.existingVpc.getPublicSubnets();
  }

  get privateSubnets(): ec2.ISubnet[] {
    return this.existingVpc.getPrivateSubnets();
  }

  private lookupRoles(): void {
    const roles = this.ctx.config.getConfig('cluster.iam.roles', undefined, { required: true }) ?? {};
    for (const [name, arn] of Object.entries(roles)) {
      this.roles[name] = iam.Role.fromRoleArn(this.scope, `${name}-role`, String(arn));
    }
  }

  getRole(name: string): iam.IRole | undefined {
    return this.roles[name];
  }

  private lookupSecurityGroups(): void {
    const securityGroups =
      this.ctx.config.getConfig('cluster.network.security_groups', undefined, { required: true }) ?? {};
    for (const [name, id] of Object.entries(securityGroups)) {
      this.securityGroups[name] = ec2.SecurityGroup.fromSecurityGroupId(
        this.scope,
        `${name}-security-group`,
        String(id),
      );
    }
  }

  getSecurityGroup(name: string): ec2.ISecurityGroup | undefined {
    return this.securityGroups[name];
  }
}

// --- Per-stack imports --------------------------------------------------------------------------
// These create no resources, but their construct ids feed `Names.uniqueId` for dependent resources.

function required(ctx: IdeaContext, key: string): string {
  return ctx.config.getString(key, undefined, { required: true }) as string;
}

/** `route53.HostedZone.from_hosted_zone_attributes(stack, 'cluster-dns', ...)`. */
export function lookupClusterDns(ctx: IdeaContext, scope: Construct): route53.IHostedZone {
  return route53.HostedZone.fromHostedZoneAttributes(scope, 'cluster-dns', {
    hostedZoneId: required(ctx, 'cluster.route53.private_hosted_zone_id'),
    zoneName: required(ctx, 'cluster.route53.private_hosted_zone_name'),
  });
}

/**
 * `cluster.ebs.kms_key_id` if set, else the account's `alias/aws/ebs`. VDC prefixes the id with
 * the component name (`<component>-ebs-kms-key(-default)`), every other stack does not.
 */
export function lookupEbsKmsKey(ctx: IdeaContext, scope: Construct, componentName?: string): kms.IKey {
  const prefix = componentName === undefined ? '' : `${componentName}-`;
  const kmsKeyId = ctx.config.getString('cluster.ebs.kms_key_id');
  return kmsKeyId === undefined
    ? kms.Alias.fromAliasName(scope, `${prefix}ebs-kms-key-default`, 'alias/aws/ebs')
    : kms.Key.fromKeyArn(scope, `${prefix}ebs-kms-key`, kmsKeyArn(ctx, kmsKeyId));
}

/** `ec2.KeyPair.from_key_pair_name(stack, f'{module_id}-key-pair' | f'{component}-key-pair', ...)`. */
export function lookupKeyPair(ctx: IdeaContext, scope: Construct, id?: string): ec2.IKeyPair {
  return ec2.KeyPair.fromKeyPairName(
    scope,
    id ?? `${ctx.moduleId}-key-pair`,
    required(ctx, 'cluster.network.ssh_key_pair'),
  );
}

/** `s3.Bucket.from_bucket_name(stack, 'cluster-s3-bucket', cluster.cluster_s3_bucket)`. */
export function lookupClusterS3Bucket(ctx: IdeaContext, scope: Construct): s3.IBucket {
  return s3.Bucket.fromBucketName(scope, 'cluster-s3-bucket', required(ctx, 'cluster.cluster_s3_bucket'));
}

/** `opensearch.Domain.from_domain_endpoint(stack, 'existing-opensearch', https://<endpoint>)`. */
export function lookupExistingOpensearch(ctx: IdeaContext, scope: Construct): opensearch.IDomain {
  return opensearch.Domain.fromDomainEndpoint(
    scope,
    'existing-opensearch',
    `https://${required(ctx, 'analytics.opensearch.domain_vpc_endpoint_url')}`,
  );
}

/** `sns.Topic.from_topic_arn(stack, f'{cluster}-{module_id}-ec2-state-change-topic', ...)`. */
export function lookupEc2StateChangeTopic(ctx: IdeaContext, scope: Construct): sns.ITopic {
  return sns.Topic.fromTopicArn(
    scope,
    `${ctx.clusterName}-${ctx.moduleId}-ec2-state-change-topic`,
    required(ctx, 'cluster.ec2.state_change_notifications_sns_topic_arn'),
  );
}

/** `iam.Role.from_role_arn(stack, 'backup-role', cluster.backups.role_arn)`. */
export function lookupBackupRole(ctx: IdeaContext, scope: Construct): iam.IRole {
  return iam.Role.fromRoleArn(scope, 'backup-role', required(ctx, 'cluster.backups.role_arn'));
}

/** `backup.BackupVault.from_backup_vault_arn(stack, 'cluster-backup-vault', ...)`. */
export function lookupClusterBackupVault(ctx: IdeaContext, scope: Construct): backup.IBackupVault {
  return backup.BackupVault.fromBackupVaultArn(
    scope,
    'cluster-backup-vault',
    required(ctx, 'cluster.backups.backup_vault.arn'),
  );
}
