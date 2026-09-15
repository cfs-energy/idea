/**
 * Both classes are plain holders. The L1 file systems are created directly under the caller's
 * scope and use `name` as the construct id.
 */

import { CfnDeletionPolicy } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import type * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as efs from 'aws-cdk-lib/aws-efs';
import * as fsx from 'aws-cdk-lib/aws-fsx';

import { isEmpty } from '../../config/cluster-config.ts';
import type { IdeaContext } from './base.ts';
import { addBackupTags, addCommonTags, resourceName } from './base.ts';

// --- `Utils.get_value_as_*` over a config subtree ----------------------------------------------
//
// A DynamoDB NULL arrives as `null`. Missing, null, and empty string, list, or object values use
// the default instead of JavaScript's `??`.

/** `ModelUtils.value_exists`. */
export function valueExists(key: string, obj: Record<string, unknown> | undefined): boolean {
  if (obj === undefined || obj === null) return false;
  if (!(key in obj)) return false;
  const value = obj[key];
  if (value === null || value === undefined) return false;
  if (typeof value === 'string' || typeof value === 'object') return !isEmpty(value);
  return true;
}

/** `Utils.get_value_as_string`: strings are stripped, everything else goes through `str()`. */
export function valueAsString(key: string, obj: Record<string, unknown> | undefined): string | undefined {
  if (!valueExists(key, obj)) return undefined;
  const value = (obj as Record<string, unknown>)[key];
  if (typeof value === 'string') {
    const stripped = value.trim();
    return stripped.length === 0 ? undefined : stripped;
  }
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  return String(value);
}

/** Booleans are returned unchanged. */
export function valueAsInt(key: string, obj: Record<string, unknown> | undefined): number | boolean | undefined {
  if (!valueExists(key, obj)) return undefined;
  const value = (obj as Record<string, unknown>)[key];
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return Math.trunc(value);
  if (typeof value === 'string') {
    const text = value.trim();
    // `int()`/`float()` parse neither a base prefix nor an empty string, so neither does this
    if (text.length > 0 && !/^[+-]?0[xXoObB]/.test(text)) {
      const parsed = Number(text);
      if (Number.isFinite(parsed)) return Math.trunc(parsed);
    }
  }
  return undefined;
}

/** `Utils.get_value_as_bool`. */
export function valueAsBool(
  key: string,
  obj: Record<string, unknown> | undefined,
  defaultValue: boolean,
): boolean {
  if (!valueExists(key, obj)) return defaultValue;
  const value = (obj as Record<string, unknown>)[key];
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return Boolean(value);
  if (typeof value === 'string') {
    const lowered = value.trim().toLowerCase();
    if (['true', 'yes', 'y', '1'].includes(lowered)) return true;
    if (['false', 'no', 'n', '0'].includes(lowered)) return false;
  }
  return defaultValue;
}

/** `Utils.get_value_as_dict`: an empty dict reads as absent. */
export function valueAsDict(
  key: string,
  obj: Record<string, unknown> | undefined,
): Record<string, unknown> | undefined {
  if (!valueExists(key, obj)) return undefined;
  const value = (obj as Record<string, unknown>)[key];
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/** `Utils.get_value_as_list`: an empty list reads as absent. */
export function valueAsList(key: string, obj: Record<string, unknown> | undefined): unknown[] | undefined {
  if (!valueExists(key, obj)) return undefined;
  const value = (obj as Record<string, unknown>)[key];
  return Array.isArray(value) ? value : undefined;
}

/**
 * `CfnDeletionPolicy` lookup uses member names. `DESTROY` is rewritten to `DELETE`, and an
 * unknown value raises instead of silently emitting no policy.
 */
export function cfnDeletionPolicy(memberName: string | undefined): CfnDeletionPolicy {
  const policy = (CfnDeletionPolicy as unknown as Record<string, CfnDeletionPolicy | undefined>)[
    memberName ?? ''
  ];
  if (policy === undefined) {
    throw new Error(`${memberName} is not a valid CfnDeletionPolicy`);
  }
  return policy;
}

// --- EFS --------------------------------------------------------------------------------------

export interface AmazonEFSProps {
  vpc: ec2.IVpc;
  securityGroup: ec2.ISecurityGroup;
  /** The `shared-storage.<name>.efs` subtree. */
  efsConfig: Record<string, unknown> | undefined;
  subnets?: ec2.ISubnet[];
}

/**
 * `storage.py:AmazonEFS`. One `CfnFileSystem` (construct id = `name`) plus one `CfnMountTarget`
 * per subnet, **scoped to the file system**, so the logical ID repeats the file-system segment.
 *
 * `DeletionPolicy` comes from `<efs>.removal_policy` and no `UpdateReplacePolicy` is set.
 */
export class AmazonEFS {
  readonly ctx: IdeaContext;
  readonly name: string;
  readonly fileSystem: efs.CfnFileSystem;
  readonly mountTargets: efs.CfnMountTarget[];

  constructor(ctx: IdeaContext, name: string, scope: Construct, props: AmazonEFSProps) {
    this.ctx = ctx;
    this.name = name;

    const config = props.efsConfig;
    const kmsKeyId = valueAsString('kms_key_id', config);
    const transitionToIa = valueAsString('transition_to_ia', config);
    const encrypted = valueAsBool('encrypted', config, true);
    const throughputMode = valueAsString('throughput_mode', config) ?? 'bursting';
    const performanceMode = valueAsString('performance_mode', config) ?? 'generalPurpose';
    let removalPolicy = valueAsString('removal_policy', config);
    if (removalPolicy === 'DESTROY') removalPolicy = 'DELETE';
    const deletionPolicy = cfnDeletionPolicy(removalPolicy);

    this.fileSystem = new efs.CfnFileSystem(scope, name, {
      encrypted,
      fileSystemTags: [{ key: 'Name', value: resourceName(ctx, name) }],
      kmsKeyId,
      throughputMode,
      performanceMode,
      lifecyclePolicies: isEmpty(transitionToIa) ? undefined : [{ transitionToIa }],
      fileSystemPolicy: {
        Version: '2012-10-17',
        Id: 'efs-prevent-anonymous-access-policy',
        Statement: [
          {
            Sid: 'efs-statement',
            Effect: 'Allow',
            Principal: { AWS: '*' },
            Action: [
              'elasticfilesystem:ClientRootAccess',
              'elasticfilesystem:ClientWrite',
              'elasticfilesystem:ClientMount',
            ],
            Condition: { Bool: { 'elasticfilesystem:AccessedViaMountTarget': 'true' } },
          },
        ],
      },
    });
    addCommonTags(ctx, this.fileSystem, name);
    addBackupTags(ctx, this.fileSystem);
    this.fileSystem.cfnOptions.deletionPolicy = deletionPolicy;

    const subnets = props.subnets ?? props.vpc.privateSubnets;
    this.mountTargets = subnets.map((subnet, index) => {
      const mountTarget = new efs.CfnMountTarget(this.fileSystem, `${name}-mount-target-${index + 1}`, {
        fileSystemId: this.fileSystem.ref,
        securityGroups: [props.securityGroup.securityGroupId],
        subnetId: subnet.subnetId,
      });
      addCommonTags(ctx, mountTarget, name);
      return mountTarget;
    });
  }
}

// --- FSx for Lustre ---------------------------------------------------------------------------

export interface FSxForLustreProps {
  vpc: ec2.IVpc;
  securityGroup: ec2.ISecurityGroup;
  /** The `shared-storage.<name>.fsx_lustre` subtree. */
  fsxLustreConfig: Record<string, unknown> | undefined;
  subnets?: ec2.ISubnet[];
}

/**
 * `storage.py:FSxForLustre`. A single `AWS::FSx::FileSystem` in the **first** subnet only; no
 * deletion policy is set, so it inherits CloudFormation's default (Delete).
 */
export class FSxForLustre {
  readonly fileSystem: fsx.CfnFileSystem;

  constructor(ctx: IdeaContext, name: string, scope: Construct, props: FSxForLustreProps) {
    const config = props.fsxLustreConfig;
    const deploymentType = valueAsString('deployment_type', config);
    const storageType = valueAsString('storage_type', config);
    // `drive_cache_type` accepts numeric values, so string values are omitted.
    const driveCacheTypeAsInt = valueAsInt('drive_cache_type', config);

    let perUnitStorageThroughput: number | boolean | undefined;
    let driveCacheType: number | boolean | undefined;
    if (storageType === 'SSD') {
      if (deploymentType === 'PERSISTENT_1') {
        perUnitStorageThroughput = valueAsInt('per_unit_storage_throughput', config);
      }
    } else {
      driveCacheType = driveCacheTypeAsInt;
    }

    const subnets = props.subnets ?? props.vpc.privateSubnets;
    this.fileSystem = new fsx.CfnFileSystem(scope, name, {
      fileSystemType: 'LUSTRE',
      subnetIds: [(subnets[0] as ec2.ISubnet).subnetId],
      lustreConfiguration: {
        deploymentType,
        perUnitStorageThroughput: perUnitStorageThroughput as number | undefined,
        driveCacheType: driveCacheType as unknown as string | undefined,
      },
      securityGroupIds: [props.securityGroup.securityGroupId],
      kmsKeyId: valueAsString('kms_key_id', config),
      storageCapacity: valueAsInt('storage_capacity', config) as number | undefined,
    });
    addCommonTags(ctx, this.fileSystem, name);
    addBackupTags(ctx, this.fileSystem);
  }
}
