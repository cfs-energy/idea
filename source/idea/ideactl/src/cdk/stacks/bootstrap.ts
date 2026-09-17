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
 * comments allowed. Values with leading-zero octal syntax normalize to decimal. Other values,
 * including leading-zero values containing an 8 or 9, remain strings.
 */
function pyyamlOctalNormalize(value: string): string {
  if (/^[-+]?0[0-7_]+$/.test(value)) {
    const negative = value.startsWith('-');
    const digits = value.replace(/^[-+]/, '').replace(/_/g, '');
    return String(parseInt(digits, 8) * (negative ? -1 : 1));
  }
  return value;
}

function parseRegionElbAccountIdFile(text: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim();
    if (line === '' || line.startsWith('#')) continue;
    const separator = line.indexOf(':');
    if (separator === -1) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (key !== '') result[key] = pyyamlOctalNormalize(value);
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

/** Renders `cdk_toolkit_stack.yml`. */
export function renderBootstrapStack(vars: BootstrapStackVars, templateDir: string = bootstrapTemplateDir()): string {
  const env = jinjaEnv(templateDir);
  return renderTemplate(env, 'cdk_toolkit_stack.yml', {
    cluster_name: vars.clusterName,
    aws_dns_suffix: vars.awsDnsSuffix,
    aws_elb_account_id: vars.awsElbAccountId,
    input_permissions_boundary: vars.inputPermissionsBoundary,
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
