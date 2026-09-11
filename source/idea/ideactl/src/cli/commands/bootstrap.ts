/**
 * The `cdk bootstrap` invocation built by `ideactl bootstrap`.
 *
 * This performs pure argv construction. The caller supplies resolved bucket, DNS,
 * ELB account, tags, and template values. It makes no child-process, AWS, or
 * cluster-config calls.
 */

/**
 * Converts `Key=k,Value=v` strings while preserving insertion order, including
 * integer-like and `__proto__` keys.
 */
export function customTagsToKeyValuePairs(customTags: readonly string[]): Map<string, string> {
  const result = new Map<string, string>();
  for (const customTag of customTags) {
    const separator = customTag.indexOf(",");
    const tokens = separator === -1
      ? [customTag]
      : [customTag.slice(0, separator), customTag.slice(separator + 1)];
    const key = indexOne(tokens[0].split("Key="), customTag).trim();
    const value = indexOne(indexOne(tokens, customTag).split("Value="), customTag).trim();
    if (key === "" || value === "") continue;
    result.set(key, value);
  }
  return result;
}

/** Returns item one, including the malformed-input exception. */
function indexOne(values: readonly string[], customTag: string): string {
  const value = values[1];
  if (value === undefined) {
    const error = new RangeError(
      `Custom tag ${customTag} is not in Key=k,Value=v form. Fix the tag and re-run bootstrap.`,
    );
    error.name = "IndexError";
    throw error;
  }
  return value;
}

/** `constants.IDEA_TAG_CLUSTER_NAME`. */
export const IDEA_TAG_CLUSTER_NAME = "idea:ClusterName";

/** Adds the cluster tag after custom tags so it wins a key collision. */
export function bootstrapTags(clusterName: string, customTags: readonly string[]): Map<string, string> {
  const tags = customTagsToKeyValuePairs(customTags);
  tags.set(IDEA_TAG_CLUSTER_NAME, clusterName);
  return tags;
}

export interface BootstrapArgvInput {
  /** The binary to execute. */
  cdkBin: string;
  /** `CdkInvoker.get_cdk_app_cmd()` output. */
  cdkAppCmd: string;
  clusterName: string;
  clusterBucket: string;
  /** Default: `true`. */
  terminationProtection: boolean;
  /** Cluster qualifier. */
  qualifier: string;
  templatePath: string;
  /** Empty values omit this flag. */
  customPermissionsBoundary?: string;
  /** Empty values omit this flag. */
  cloudformationExecutionPolicies?: string;
  /** Default: `true`. This flag is always emitted. */
  publicAccessBlockConfiguration?: boolean;
  /** Ordered bootstrap tags, including arbitrary custom-tag keys. */
  tags: ReadonlyMap<string, string>;
  awsProfile?: string;
}

/** `f'{cluster_name}-bootstrap'` (`cdk_invoker.py:510`). */
export function bootstrapStackName(clusterName: string): string {
  return `${clusterName}-bootstrap`;
}

/** Returns whether an optional string is non-empty. */
function isNotEmptyString(value: string | undefined): value is string {
  return value !== undefined && value.trim().length > 0;
}

/**
 * Returns tokenized `cdk bootstrap` arguments. Flag/value pairs are separate
 * tokens for direct execution.
 */
export function buildBootstrapArgv(input: BootstrapArgvInput): string[] {
  const argv: string[] = [input.cdkBin, "bootstrap"];
  argv.push("--app", input.cdkAppCmd);
  argv.push("--bootstrap-bucket-name", input.clusterBucket);
  argv.push("--toolkit-stack-name", bootstrapStackName(input.clusterName));
  argv.push("--termination-protection", String(input.terminationProtection));
  argv.push("--qualifier", input.qualifier);
  argv.push("--template", input.templatePath);

  if (isNotEmptyString(input.customPermissionsBoundary)) {
    argv.push("--custom-permissions-boundary", input.customPermissionsBoundary);
  }
  if (isNotEmptyString(input.cloudformationExecutionPolicies)) {
    argv.push("--cloudformation-execution-policies", input.cloudformationExecutionPolicies);
  }
  // Boolean values always emit this flag.
  argv.push("--public-access-block-configuration", String(input.publicAccessBlockConfiguration ?? true));

  for (const [key, value] of input.tags) {
    argv.push("--tags", `${key}=${value}`);
  }
  if (isNotEmptyString(input.awsProfile)) {
    argv.push("--profile", input.awsProfile);
  }
  return argv;
}
