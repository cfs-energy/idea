/**
 * Rendering for the deployed `<cluster>-bootstrap` stack.
 *
 * The static toolkit template is rendered and passed to `cdk bootstrap --template`. It is not a
 * CDK construct. The template uses `cluster_name`, `aws_dns_suffix`, `aws_elb_account_id`, and
 * `input_permissions_boundary`.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { Stack } from "aws-cdk-lib";

import { GeneralException } from '../../config/cluster-config.ts';
import { jinjaEnv, renderTemplate } from '../../config/jinja.ts';
import { resourcesDir } from '../policy.ts';
import type { StackBuildProps } from "../app.ts";

export interface BootstrapStackVars {
  clusterName: string;
  awsDnsSuffix: string;
  /** `Utils.get_value_as_string(aws_region, region_elb_account_id_config)`: undefined if the region has no entry. */
  awsElbAccountId?: string;
  /** `CdkInvoker.custom_permissions_boundary`; Python default `None`, which Jinja/nunjucks print as `''`. */
  inputPermissionsBoundary?: string;
  /**
   * Each scheduler module's `logs/<id>/` archive prefix and the days it is kept. Unset, or 0
   * for a module, keeps that archive forever, as before.
   */
  schedulerLogArchives?: { moduleId: string; retentionDays: number }[];
}

/** `resources/cdk/cdk_toolkit_stack.yml`'s directory, for the `FileSystemLoader` root. */
export function bootstrapTemplateDir(): string {
  return join(resourcesDir(), 'cdk');
}

/** `resources/config/region_elb_account_id.yml`. */
export function regionElbAccountIdPath(): string {
  return join(resourcesDir(), 'config', 'region_elb_account_id.yml');
}

/**
 * `region_elb_account_id.yml` is a flat `region: account-id` mapping, one entry per line, `#`
 * comments allowed. An account id is twelve digits kept as written: a leading zero is part of it
 * (me-south-1's starts with 0), never an octal number as the old YAML reader made it.
 */
function parseRegionElbAccountIdFile(text: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim();
    if (line === '' || line.startsWith('#')) continue;
    const separator = line.indexOf(':');
    if (separator === -1) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (key === '') continue;
    const id = value.replace(/^(['"])(.*)\1$/, '$2');
    if (!/^\d{12}$/.test(id)) throw new Error(`region_elb_account_id.yml: ${key} has ${JSON.stringify(value)}, not a twelve-digit account id`);
    result[key] = id;
  }
  return result;
}

/**
 * `Utils.get_value_as_string(aws_region, region_elb_account_id_config)`. Returns `undefined`
 * (`None`) for a region absent from the file, which makes the bucket-policy
 * `{% if aws_elb_account_id %}` branch fall through to the `logdelivery.elasticloadbalancing.*`
 * service-principal form.
 */
export function elbAccountIdForRegion(region: string, path: string = regionElbAccountIdPath()): string | undefined {
  const config = parseRegionElbAccountIdFile(readFileSync(path, 'utf-8'));
  return config[region];
}

/**
 * The scheduler uploads each day's changed logs and keeps ten days on disk (twenty after a failed
 * upload). The bucket copy must outlive the local copy; an expired file is not uploaded again.
 */
export const SCHEDULER_LOG_ARCHIVE_MIN_RETENTION_DAYS = 30;

/**
 * `<module id>.log_archive.s3_retention_days`: 0 keeps the archive forever; otherwise at least
 * `SCHEDULER_LOG_ARCHIVE_MIN_RETENTION_DAYS`. The error names the module whose value is wrong.
 */
export function schedulerLogRetentionDays(value: number, moduleId: string = 'scheduler'): number {
  if (!Number.isInteger(value) || value < 0 || (value > 0 && value < SCHEDULER_LOG_ARCHIVE_MIN_RETENTION_DAYS)) {
    throw new GeneralException(
      `${moduleId}.log_archive.s3_retention_days must be 0 (keep forever) or at least ${SCHEDULER_LOG_ARCHIVE_MIN_RETENTION_DAYS}, got ${value}`,
    );
  }
  return value;
}

/** Renders `cdk_toolkit_stack.yml`. */
export function renderBootstrapStack(vars: BootstrapStackVars, templateDir: string = bootstrapTemplateDir()): string {
  const env = jinjaEnv(templateDir);
  return renderTemplate(env, 'cdk_toolkit_stack.yml', {
    cluster_name: vars.clusterName,
    aws_dns_suffix: vars.awsDnsSuffix,
    aws_elb_account_id: vars.awsElbAccountId,
    input_permissions_boundary: vars.inputPermissionsBoundary,
    scheduler_log_archives: (vars.schedulerLogArchives ?? [])
      .filter((archive) => archive.retentionDays > 0)
      .map((archive) => ({ module_id: archive.moduleId, retention_days: archive.retentionDays })),
  });
}

/**
 * Creates the empty app target required by `cdk bootstrap --app`.
 *
 * The deployed bootstrap template is rendered separately by `renderBootstrapStack`; adding it to
 * this construct would change the CDK app contract.
 */
export function buildStack(props: StackBuildProps): void {
  const stackName = `${props.ctx.clusterName}-bootstrap`;
  new Stack(props.app, stackName, {
    env: props.env,
    stackName,
  });
}
