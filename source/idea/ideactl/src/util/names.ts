import { shake256Hex } from './shake256.ts';

// Physical resource names, ported from SocaBaseConstruct (app/cdk/constructs/base.py) and
// IdeaBaseStack.get_target_group_name (app/cdk/stacks/base_stack.py). The Python prefix is the
// cluster name; `regionSuffix` is the region string when the Python passed region_suffix=True.

export function buildResourceName(cluster: string, name: string, regionSuffix?: string): string {
  const resourceName = `${cluster}-${name}`;
  return regionSuffix ? `${resourceName}-${regionSuffix}` : resourceName;
}

/** `{prefix}{suffix}-{name[:10]}-{shake256(untrimmed, (trimLength - 12 - prefix - suffix) / 2)}` */
export function buildTrimmedResourceName(
  cluster: string,
  name: string,
  regionSuffix?: string,
  trimLength = 64,
): string {
  const suffix = regionSuffix ? `-${regionSuffix}` : '';
  const resourceName = `${cluster}-${name}${suffix}`;
  // Python `int(...)` truncates toward zero; Math.trunc, not Math.floor.
  const bytes = Math.trunc((trimLength - 12 - cluster.length - suffix.length) / 2);
  return `${cluster}${suffix}-${name.slice(0, 10)}-${shake256Hex(resourceName, bytes)}`;
}

export function getTargetGroupName(cluster: string, moduleId: string, identifier: string): string {
  const suffix = shake256Hex(`${cluster}.${moduleId}`, 4);
  const targetGroupName = `${cluster}-${identifier}-${suffix}`;
  if (targetGroupName.length > 32) {
    throw new Error(`Target group name ${targetGroupName} is longer than 32 characters. Shorten the cluster name or the identifier, then synth again.`);
  }
  return targetGroupName;
}

export function buildInstanceProfileArn(
  partition: string,
  accountId: string,
  instanceProfileRef: string,
): string {
  return `arn:${partition}:iam::${accountId}:instance-profile/${instanceProfileRef}`;
}

export function getKmsKeyArn(
  keyId: string,
  partition: string,
  region: string,
  accountId: string,
): string {
  if (keyId.startsWith('arn:')) {
    return keyId;
  }
  return `arn:${partition}:kms:${region}:${accountId}:key/${keyId}`;
}
