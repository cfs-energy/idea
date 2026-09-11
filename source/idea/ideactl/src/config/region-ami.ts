/**
 * Resolution of region_ami_config.yml, which is keyed by region, processor architecture and base OS.
 * Port of `ideaadministrator/app/region_ami_config.py`.
 *
 * Two file shapes are accepted. The flat shape lists x86_64 AMIs only:
 *
 *     us-east-2:
 *       amazonlinux2023: ami-...
 *
 * The nested shape names the architecture, and is required to serve anything other than x86_64:
 *
 *     us-east-2:
 *       x86_64:
 *         amazonlinux2023: ami-...
 *       arm64:
 *         amazonlinux2023: ami-...
 */

import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';

import { ClusterConfigError, GeneralException } from './cluster-config.ts';

export const ARCHITECTURE_X86_64 = 'x86_64';
export const ARCHITECTURE_ARM64 = 'arm64';
export const SUPPORTED_ARCHITECTURES: readonly string[] = [ARCHITECTURE_X86_64, ARCHITECTURE_ARM64];

export type RegionsConfig = Record<string, Record<string, unknown>>;

// one class per ported python exception, so `instanceof` means the same thing everywhere
export { ClusterConfigError, GeneralException };

/**
 * The shipped region_ami_config.yml. The AMI ids in it are release-pinned inputs, so the same
 * file has to be read here and by the Python administrator until `resources/` moves into this
 * package.
 */
export function regionAmiConfigPath(): string {
  const here = fileURLToPath(new URL('.', import.meta.url));
  const candidates = [
    // Package-local resource path.
    new URL('../../resources/config/region_ami_config.yml', import.meta.url),
    // Administrator resource path.
    new URL('../../../idea-administrator/resources/config/region_ami_config.yml', import.meta.url),
  ].map((url) => fileURLToPath(url));
  const found = candidates.find((candidate) => existsSync(candidate));
  if (found === undefined) {
    throw new GeneralException(`region_ami_config.yml not found (looked next to ${here})`);
  }
  return found;
}

export function loadRegionAmiConfig(file: string = regionAmiConfigPath()): RegionsConfig {
  return yaml.load(readFileSync(file, 'utf-8')) as RegionsConfig;
}

function regionConfig(regionsConfig: RegionsConfig, awsRegion: string): Record<string, unknown> {
  const amiConfig = regionsConfig[awsRegion];
  if (amiConfig === undefined || amiConfig === null || typeof amiConfig !== 'object') {
    throw new GeneralException(`aws_region: ${awsRegion} not found in region_ami_config.yml`);
  }
  return amiConfig as Record<string, unknown>;
}

/**
 * The AMI for a region / architecture / base OS. Raises when the combination is not configured,
 * naming the architecture so the failure is not mistaken for an unsupported base OS.
 */
export function resolveRegionAmi(
  regionsConfig: RegionsConfig,
  awsRegion: string,
  baseOs: string,
  architecture?: string,
): string {
  const arch = architecture === undefined || architecture === null || architecture.trim() === ''
    ? ARCHITECTURE_X86_64
    : architecture;

  const amiConfig = regionConfig(regionsConfig, awsRegion);

  const keys = Object.keys(amiConfig);
  const architectureKeys = keys.filter((key) => SUPPORTED_ARCHITECTURES.includes(key));
  const baseOsKeys = keys.filter((key) => !SUPPORTED_ARCHITECTURES.includes(key));

  if (architectureKeys.length > 0 && baseOsKeys.length > 0) {
    // adding an arm64 section without moving the existing base OS keys under x86_64 would
    // otherwise read as nested and fail every x86_64 install here.
    const listed = [
      `region: ${awsRegion} in region_ami_config.yml mixes architecture sections`,
      `(${[...architectureKeys].sort().join(', ')}) with base OS entries at the top`,
      `level (${[...baseOsKeys].sort().join(', ')}). Move the base OS entries under an`,
      'architecture section.',
    ].join(' ');
    throw new GeneralException(listed);
  }

  let architectureConfig: Record<string, unknown>;
  if (architectureKeys.length > 0) {
    const nested = amiConfig[arch];
    if (nested === undefined || nested === null || typeof nested !== 'object') {
      throw new GeneralException(
        `no AMIs configured for architecture: ${arch} in region: ${awsRegion} (region_ami_config.yml)`,
      );
    }
    architectureConfig = nested as Record<string, unknown>;
  } else if (arch === ARCHITECTURE_X86_64) {
    architectureConfig = amiConfig;
  } else {
    throw new GeneralException(
      [
        `region_ami_config.yml lists ${ARCHITECTURE_X86_64} AMIs only for region: ${awsRegion},`,
        `and architecture: ${arch} was requested. Add an ${arch} section for the region,`,
        'or set the instance_ami explicitly.',
      ].join(' '),
    );
  }

  const amiId = architectureConfig[baseOs];
  if (typeof amiId !== 'string' || amiId.trim() === '') {
    // Report an unsupported base OS selection as a configuration error.
    throw new ClusterConfigError(
      `instance_ami not found for base_os: ${baseOs}, architecture: ${arch}, region: ${awsRegion}`,
    );
  }
  return amiId;
}
