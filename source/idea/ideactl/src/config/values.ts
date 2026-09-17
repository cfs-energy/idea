/**
 * The `values.yml` getters and the jinja context they build.
 * Port of the getter half of `ideaadministrator/app/config_generator.py` (lines 33-591).
 *
 * Every lookup goes through the `Utils.get_value_as_*` helpers, so a missing key, a `~` key and a
 * blank string all behave the same, and a validation only fires when the branch that needs the
 * value is selected. The context is built in the same order as the Python dict literal, because
 * that order decides which validation error an operator sees first.
 */

import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import * as yaml from 'js-yaml';

import { ClusterConfigError, GeneralException, isEmpty } from './cluster-config.ts';
import { loadRegionAmiConfig, regionAmiConfigPath, resolveRegionAmi } from './region-ami.ts';
import { ideaVersion } from '../version.ts';

/** `exceptions.invalid_params` */
export class InvalidParams extends Error {}

export type UserValues = Record<string, unknown>;

/** `constants.SUPPORTED_OS` */
export const SUPPORTED_OS: readonly string[] = [
  'amazonlinux2023',
  'rhel8',
  'rhel9',
  'rhel10',
  'windows',
  'windows2019',
  'windows2022',
  'windows2025',
  'rocky8',
  'rocky9',
  'rocky10',
  'ubuntu2204',
  'ubuntu2404',
];

/** `constants.EOL_BASEOS`: base_os values that are gone, mapped to their replacement. */
export const EOL_BASEOS: Readonly<Record<string, string>> = { amazonlinux2: 'amazonlinux2023' };

const ARCHITECTURE_X86_64 = 'x86_64';

// ---------------------------------------------------------------------------------------------
// PyYAML scalar semantics
// ---------------------------------------------------------------------------------------------

/**
 * A scalar PyYAML resolved as `tag:yaml.org,2002:float`.
 *
 * Python's coercion rules branch on `isinstance(value, int)` vs `isinstance(value, float)`, and
 * JavaScript has one number type: without this marker `1` and `1.0` are indistinguishable, and
 * `use_existing_vpc: 1.0` would take the existing-VPC branch here while Python falls back to the
 * default. Only values.yml scalars are wrapped; the getters unwrap before anything else sees them.
 */
class PyFloat {
  readonly value: number;
  constructor(value: number) {
    this.value = value;
  }
}

/** PyYAML resolves the YAML 1.1 boolean set; every js-yaml schema resolves the YAML 1.2 one. */
const PY_TRUE_SCALARS = new Set(['yes', 'Yes', 'YES', 'true', 'True', 'TRUE', 'on', 'On', 'ON']);
const PY_FALSE_SCALARS = new Set(
  ['no', 'No', 'NO', 'false', 'False', 'FALSE', 'off', 'Off', 'OFF'],
);

/** A core-schema scalar tag, reused inside the PyYAML-shaped schema below. */
function coreScalarTag(tagName: string): yaml.ScalarTagDefinition {
  const tag = yaml.CORE_SCHEMA.lookupScalarTag(tagName);
  if (tag === undefined) throw new Error(`js-yaml core schema has no ${tagName}`);
  return tag;
}
const coreFloat = coreScalarTag('tag:yaml.org,2002:float');

const pyBoolTag = yaml.defineScalarTag<boolean>('tag:yaml.org,2002:bool', {
  implicit: true,
  resolve: (source) => (PY_TRUE_SCALARS.has(source) ? true : PY_FALSE_SCALARS.has(source) ? false : yaml.NOT_RESOLVED),
  identify: (value: unknown) => typeof value === 'boolean',
  represent: (value: boolean) => (value ? 'true' : 'false'),
  implicitFirstChars: ['y', 'Y', 'n', 'N', 't', 'T', 'f', 'F', 'o', 'O'],
});

const pyFloatTag = yaml.defineScalarTag<PyFloat>('tag:yaml.org,2002:float', {
  implicit: true,
  resolve: (source, isExplicit, tagName) => {
    const value = coreFloat.resolve(source, isExplicit, tagName);
    return value === yaml.NOT_RESOLVED ? yaml.NOT_RESOLVED : new PyFloat(value as number);
  },
  identify: (value: unknown) => value instanceof PyFloat,
  represent: (value: PyFloat) => pyFloatRepr(value.value),
  implicitFirstChars: coreFloat.implicitFirstChars,
});

/**
 * `yaml.safe_load`: the core schema's scalar set with PyYAML's boolean list and a float type that
 * stays distinguishable from an int. A plain scalar tries these tags in this order.
 */
const PY_SAFE_SCHEMA = yaml.FAILSAFE_SCHEMA.withTags(
  coreScalarTag('tag:yaml.org,2002:null'),
  pyBoolTag,
  coreScalarTag('tag:yaml.org,2002:int'),
  pyFloatTag,
);

/** `repr(float)`: Python always prints a decimal point or an exponent, `String()` does not. */
function pyFloatRepr(value: number): string {
  if (Number.isNaN(value)) return 'nan';
  if (value === Infinity) return 'inf';
  if (value === -Infinity) return '-inf';
  const text = String(value);
  return /[.e]/.test(text) ? text : `${text}.0`;
}

/** `repr()` for the values a YAML document can hold. `str()` equals it for everything but `str`. */
function pyRepr(value: unknown): string {
  if (value === null || value === undefined) return 'None';
  if (value instanceof PyFloat) return pyFloatRepr(value.value);
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'string') {
    return `'${value.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
  }
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(', ')}]`;
  if (typeof value === 'object') {
    const items = Object.entries(value).map(([k, v]) => `${pyRepr(k)}: ${pyRepr(v)}`);
    return `{${items.join(', ')}}`;
  }
  return String(value);
}

/** `int(str)`: decimal digits with an optional sign and `_` separators, nothing else. */
function pyIntFromString(text: string): number | undefined {
  const trimmed = text.trim();
  if (!/^[+-]?[0-9](?:_?[0-9])*$/.test(trimmed)) return undefined;
  return Number(trimmed.replace(/_/g, ''));
}

/** `float(str)`: decimal or exponent notation, `inf`/`nan`, `_` separators. Not hexadecimal. */
function pyFloatFromString(text: string): number | undefined {
  const trimmed = text.trim();
  if (/^[+-]?(?:inf|infinity)$/i.test(trimmed)) {
    return trimmed.startsWith('-') ? -Infinity : Infinity;
  }
  if (/^[+-]?nan$/i.test(trimmed)) return NaN;
  const digits = String.raw`[0-9](?:_?[0-9])*`;
  const pattern = new RegExp(
    `^[+-]?(?:${digits}(?:\\.(?:${digits})?)?|\\.${digits})(?:[eE][+-]?${digits})?$`,
  );
  if (!pattern.test(trimmed)) return undefined;
  return Number(trimmed.replace(/_/g, ''));
}

/** PyFloat never escapes the getters; a list can still carry one. */
function unwrapPyFloat(value: unknown): unknown {
  if (value instanceof PyFloat) return value.value;
  if (Array.isArray(value)) return value.map(unwrapPyFloat);
  return value;
}

// ---------------------------------------------------------------------------------------------
// ModelUtils value getters (idea-data-model/model_utils.py)
// ---------------------------------------------------------------------------------------------

/** `ModelUtils.value_exists`: present, not None, and not an empty string/list/dict. */
function valueExists(key: string, obj: UserValues): boolean {
  if (obj === null || obj === undefined) return false;
  if (!Object.hasOwn(obj, key)) return false;
  const value = obj[key];
  if (value === null || value === undefined) return false;
  if (value instanceof PyFloat) return true;
  if (Array.isArray(value) || typeof value === 'object' || typeof value === 'string') {
    return !isEmpty(value);
  }
  return true;
}

/**
 * `ModelUtils.is_true`: bool, int truthiness, or one of the recognised strings. A float reaches
 * `value.strip()` in Python and raises; nothing calls it with one, so the fallback stands in.
 */
function isTrue(value: unknown, fallback = false): boolean {
  if (value === null || value === undefined) return fallback;
  if (typeof value === 'boolean') return value;
  if (value instanceof PyFloat) return fallback;
  if (typeof value === 'number') return Boolean(value);
  if (typeof value !== 'string') return fallback;
  if (isEmpty(value)) return fallback;
  return ['true', 'yes', 'y', '1'].includes(value.trim().toLowerCase());
}

function isFalse(value: unknown, fallback = false): boolean {
  if (value === null || value === undefined) return fallback;
  if (typeof value === 'boolean') return !value;
  if (value instanceof PyFloat) return fallback;
  if (typeof value === 'number') return !value;
  if (typeof value !== 'string') return fallback;
  if (isEmpty(value)) return fallback;
  return ['false', 'no', 'n', '0'].includes(value.trim().toLowerCase());
}

/** `ModelUtils.get_value_as_string`: strings are stripped, everything else is `str()`-ed. */
function getString(key: string, obj: UserValues, def: string | null = null): string | null {
  if (!valueExists(key, obj)) return def;
  const value = obj[key];
  if (typeof value === 'string') {
    const stripped = value.trim();
    return stripped.length === 0 ? def : stripped;
  }
  return pyRepr(value);
}

/**
 * `ModelUtils.get_as_bool`: `bool`, then `int` truthiness, then the recognised strings. A `float`
 * matches neither `isinstance` test and falls through to the default.
 */
function getBool(key: string, obj: UserValues, def: boolean): boolean {
  if (!valueExists(key, obj)) return def;
  const value = obj[key];
  if (typeof value === 'boolean') return value;
  // A PyFloat is a float whatever its value; a bare JavaScript number carries no YAML provenance,
  // so an integral one stands in for `int` and everything else for `float`.
  if (value instanceof PyFloat) return def;
  if (typeof value === 'number') return Number.isInteger(value) ? Boolean(value) : def;
  if (typeof value === 'string') {
    if (isTrue(value)) return true;
    if (isFalse(value)) return false;
  }
  return def;
}

/**
 * `ModelUtils.get_as_int`. `bool` is a subclass of `int` in Python, so the `isinstance(value, int)`
 * branch returns the bool unchanged - `volume_size: true` reaches the template as `True`.
 * A string is parsed the way `int()` then `float()` parse it: `0x10` is neither, so it is dropped.
 */
function getInt(key: string, obj: UserValues, def: number): number | boolean {
  if (!valueExists(key, obj)) return def;
  const value = obj[key];
  if (typeof value === 'boolean') return value;
  if (value instanceof PyFloat) return Math.trunc(value.value);
  if (typeof value === 'number') return Math.trunc(value);
  if (typeof value === 'string') {
    const asInt = pyIntFromString(value);
    if (asInt !== undefined) return asInt;
    const asFloat = pyFloatFromString(value);
    if (asFloat !== undefined && Number.isFinite(asFloat)) return Math.trunc(asFloat);
  }
  return def;
}

/**
 * `ModelUtils.get_value_as_list`. An empty list is treated as absent and yields the default -
 * that is why `enabled_modules: []` and a missing `enabled_modules` are indistinguishable.
 */
function getList(key: string, obj: UserValues, def: unknown[] | null = null): unknown[] | null {
  if (!valueExists(key, obj)) return def;
  const value = obj[key];
  if (!Array.isArray(value)) return def;
  return value.length === 0 ? def : (unwrapPyFloat(value) as unknown[]);
}

// ---------------------------------------------------------------------------------------------
// resource files
// ---------------------------------------------------------------------------------------------

/** A file under the package's `resources/`: release-pinned inputs, not code. */
export function resourcePath(relative: string): string {
  const file = fileURLToPath(new URL(`../../resources/${relative}`, import.meta.url));
  if (!existsSync(file)) throw new GeneralException(`resource not found: ${relative}`);
  return file;
}

export function configTemplatesDir(): string {
  return resourcePath('config/templates');
}

export function regionTimezoneConfigPath(): string {
  return resourcePath('config/region_timezone_config.yml');
}

function loadYamlFile(file: string): Record<string, unknown> {
  return (yaml.loadAll(readFileSync(file, 'utf-8'), { schema: PY_SAFE_SCHEMA })[0] ?? {}) as Record<
    string,
    unknown
  >;
}

export function loadValuesFile(file: string): UserValues {
  return loadYamlFile(file);
}

// ---------------------------------------------------------------------------------------------
// context
// ---------------------------------------------------------------------------------------------

/** Supplies the VPC endpoint service lists, which the generator otherwise reads from EC2. */
export interface VpcEndpointsLookup {
  gatewayEndpoints(): string[];
  interfaceEndpoints(): Record<string, unknown>;
}

export interface BuildContextOptions {
  /** Only consulted for a NEW VPC with `use_vpc_endpoints: true`; the Python path calls EC2 there. */
  vpcEndpoints?: VpcEndpointsLookup;
  regionAmiConfigFile?: string;
  regionTimezoneConfigFile?: string;
}

/**
 * The getters, as one closure over `values` so each one can call the others exactly as the Python
 * methods do (they recompute rather than cache, and the validations ride along with the reads).
 */
function getters(values: UserValues, options: BuildContextOptions) {
  const g = {
    clusterName(): string {
      const value = getString('cluster_name', values);
      if (isEmpty(value)) throw new InvalidParams('cluster_name is required');
      return value as string;
    },

    /** values.yml's own `cluster_timezone` is never read: the region table wins. */
    clusterTimezone(): string {
      const file = options.regionTimezoneConfigFile ?? regionTimezoneConfigPath();
      const config = loadYamlFile(file);
      const fallback = getString('default', config, 'America/Los_Angeles') as string;
      return getString(g.awsRegion(), config, fallback) as string;
    },

    clusterLocale: () => getString('cluster_locale', values, 'en_US') as string,

    administratorEmail(): string {
      const value = getString('administrator_email', values, null);
      if (isEmpty(value)) throw new InvalidParams('administrator_email is required');
      return value as string;
    },

    administratorUsername: () =>
      getString('administrator_username', values, 'clusteradmin') as string,

    awsAccountId(): string {
      const value = getString('aws_account_id', values, null);
      if (isEmpty(value)) throw new InvalidParams('aws_account_id is required');
      return value as string;
    },

    awsDnsSuffix(): string {
      const value = getString('aws_dns_suffix', values, null);
      if (isEmpty(value)) throw new InvalidParams('aws_dns_suffix is required');
      return value as string;
    },

    awsPartition(): string {
      const value = getString('aws_partition', values, null);
      if (isEmpty(value)) throw new InvalidParams('aws_partition is required');
      return value as string;
    },

    awsRegion(): string {
      const value = getString('aws_region', values);
      if (isEmpty(value)) throw new InvalidParams('aws_region is required');
      return value as string;
    },

    storageAppsProvider: () => getString('storage_apps_provider', values, 'efs') as string,
    appsMountDir: () => getString('apps_mount_dir', values, '/apps') as string,
    storageDataProvider: () => getString('storage_data_provider', values, 'efs') as string,
    dataMountDir: () => getString('data_mount_dir', values, '/data') as string,
    prefixListIds: () => getList('prefix_list_ids', values, []) as unknown[],
    clientIp: () => getList('client_ip', values, []) as unknown[],

    sshKeyPairName(): string {
      const value = getString('ssh_key_pair_name', values);
      if (isEmpty(value)) throw new InvalidParams('ssh_key_pair_name is required');
      return value as string;
    },

    vpcCidrBlock(): string | null {
      if (g.useExistingVpc()) return null;
      const value = getString('vpc_cidr_block', values);
      if (isEmpty(value)) throw new InvalidParams('vpc_cidr_block is required');
      return value;
    },

    useVpcEndpoints: () => getBool('use_vpc_endpoints', values, false),
    albPublic: () => getBool('alb_public', values, true),

    directoryServiceProvider: () =>
      getString('directory_service_provider', values, 'openldap') as string,

    useExistingDirectoryService(): boolean {
      const value = getBool('use_existing_directory_service', values, false);
      if (value && !g.useExistingVpc()) {
        throw new InvalidParams(
          'use_existing_directory_service cannot be True if use_existing_vpc = False',
        );
      }
      return value;
    },

    directoryId(): string | null {
      const value = getString('directory_id', values);
      if (isEmpty(value) && g.useExistingDirectoryService()) {
        throw new InvalidParams(
          'directory_id is required when use_existing_directory_service = True',
        );
      }
      return value;
    },

    directoryServiceRootUsernameSecretArn(): string | null {
      const value = getString('directory_service_root_username_secret_arn', values);
      if (isEmpty(value) && g.useExistingDirectoryService()) {
        throw new InvalidParams(
          'directory_service_root_username_secret_arn is required when use_existing_directory_service = True. Set that key in values.yml, then re-run.',
        );
      }
      return value;
    },

    directoryServiceRootPasswordSecretArn(): string | null {
      const value = getString('directory_service_root_password_secret_arn', values);
      if (isEmpty(value) && g.useExistingDirectoryService()) {
        throw new InvalidParams(
          'directory_service_root_password_secret_arn is required when use_existing_directory_service = True',
        );
      }
      return value;
    },

    identityProvider: () => getString('identity_provider', values, 'cognito-idp') as string,
    enableAwsBackup: () => getBool('enable_aws_backup', values, true),
    // The ecs settings template writes `ecs.enabled` from this. The migration stages it false and
    // flips it at its activation boundary, so regenerating values must not turn it on by itself.
    enableEcs: () => getBool('enable_ecs', values, false),
    kmsKeyType: () => getString('kms_key_type', values, null),
    kmsKeyId: () => getString('kms_key_id', values, null),
    instanceType: () => getString('instance_type', values, 'm7i.large') as string,

    baseOs(): string {
      const baseOs = getString('base_os', values, 'amazonlinux2023') as string;
      // Validate end-of-life base OS values before AMI resolution.
      if (Object.hasOwn(EOL_BASEOS, baseOs)) {
        throw new ClusterConfigError(
          `base_os: ${baseOs} has reached end-of-life and is no longer supported by IDEA. ` +
            `Update base_os to ${EOL_BASEOS[baseOs]} in values.yml and re-run.`,
        );
      }
      // EL10 has no Amazon DCV packages: an eVDI-enabled install on rhel10/rocky10 would deploy
      // broker/gateway/controller nodes with no DCV installed.
      if ((baseOs === 'rhel10' || baseOs === 'rocky10') &&
        g.enabledModules().includes('virtual-desktop-controller')) {
        throw new GeneralException(
          `base_os ${baseOs} is not supported with the virtual-desktop-controller module: ` +
            'Amazon DCV publishes no EL10 packages. Choose rhel9/rocky9, or install without eVDI.',
        );
      }
      return baseOs;
    },

    dcvConnectionGatewayVolumeSize: () =>
      getInt('dcv_connection_gateway_volume_size', values, 200),

    instanceArchitecture: () =>
      getString('instance_architecture', values, ARCHITECTURE_X86_64) as string,

    regionAmi(): string {
      const file = options.regionAmiConfigFile ?? regionAmiConfigPath();
      return resolveRegionAmi(
        loadRegionAmiConfig(file),
        g.awsRegion(),
        g.baseOs(),
        g.instanceArchitecture(),
      );
    },

    dcvConnectionGatewayInstanceAmi(): string {
      const value = getString('dcv_connection_gateway_instance_ami', values);
      return isEmpty(value) ? g.regionAmi() : (value as string);
    },

    dcvBrokerVolumeSize: () => getInt('dcv_broker_volume_size', values, 200),

    dcvBrokerInstanceAmi(): string {
      const value = getString('dcv_broker_instance_ami', values);
      return isEmpty(value) ? g.regionAmi() : (value as string);
    },

    instanceAmi(): string {
      const value = getString('instance_ami', values);
      return isEmpty(value) ? g.regionAmi() : (value as string);
    },

    volumeSize: () => getInt('volume_size', values, 200),
    volumeType: () => getString('volume_type', values, 'gp3') as string,

    enabledModules(): string[] {
      const modules = getList('enabled_modules', values);
      return isEmpty(modules) ? [] : (modules as string[]);
    },

    metricsProvider: () => getString('metrics_provider', values, 'cloudwatch'),

    prometheusRemoteWriteUrl(): string | null {
      if (g.metricsProvider() !== 'prometheus') return null;
      const url = getString('prometheus_remote_write_url', values);
      if (isEmpty(url)) {
        throw new GeneralException(
          'prometheus_remote_write_url is required when metrics_provider = prometheus',
        );
      }
      return url;
    },

    /** The container host pool runs the Datadog agent when the modules send to it. */
    datadogAgent: () => g.enableEcs() && g.metricsProvider() === 'dogstatsd',

    datadogApiKeySecretArn(): string | null {
      const value = getString('datadog_api_key_secret_arn', values);
      if (isEmpty(value) && g.datadogAgent()) {
        throw new GeneralException(
          'datadog_api_key_secret_arn is required when metrics_provider = dogstatsd and enable_ecs = true',
        );
      }
      return value;
    },

    datadogAgentImage(): string | null {
      const value = getString('datadog_agent_image', values);
      if (isEmpty(value) && g.datadogAgent()) {
        throw new GeneralException(
          'datadog_agent_image is required when metrics_provider = dogstatsd and enable_ecs = true',
        );
      }
      return value;
    },

    useExistingVpc: () => getBool('use_existing_vpc', values, false),

    vpcId(): string | null {
      const value = getString('vpc_id', values);
      if (isEmpty(value) && g.useExistingVpc()) {
        throw new InvalidParams('vpc_id is required when use_existing_vpc = True');
      }
      return value;
    },

    privateSubnetIds(): unknown[] {
      const privateIds = getList('private_subnet_ids', values, []) as unknown[];
      const publicIds = getList('public_subnet_ids', values, []) as unknown[];
      if (g.useExistingVpc() && isEmpty(privateIds) && isEmpty(publicIds)) {
        throw new InvalidParams(
          'use_existing_vpc is True, but both private_subnet_ids and public_subnet_ids are empty in values.yml. Set at least one list, then re-run.',
        );
      }
      return privateIds;
    },

    publicSubnetIds(): unknown[] {
      const privateIds = getList('private_subnet_ids', values, []) as unknown[];
      const publicIds = getList('public_subnet_ids', values, []) as unknown[];
      if (g.useExistingVpc() && isEmpty(privateIds) && isEmpty(publicIds)) {
        throw new InvalidParams(
          'use_existing_vpc is True, but both private_subnet_ids and public_subnet_ids are empty in values.yml. Set at least one list, then re-run.',
        );
      }
      return publicIds;
    },

    useExistingAppsFs(): boolean {
      const value = getBool('use_existing_apps_fs', values, false);
      if (value && !g.useExistingVpc()) {
        throw new InvalidParams('use_existing_apps_fs cannot be True if use_existing_vpc = False');
      }
      return value;
    },

    existingAppsFsId(): string | null {
      const value = getString('existing_apps_fs_id', values);
      if (isEmpty(value) && g.useExistingAppsFs()) {
        throw new InvalidParams(
          'existing_apps_fs_id is required when use_existing_apps_fs = True. Set the file-system id in values.yml, then re-run.',
        );
      }
      return value;
    },

    useExistingDataFs(): boolean {
      const value = getBool('use_existing_data_fs', values, false);
      if (value && !g.useExistingVpc()) {
        throw new InvalidParams('use_existing_data_fs cannot be True if use_existing_vpc = False');
      }
      return value;
    },

    existingDataFsId(): string | null {
      const value = getString('existing_data_fs_id', values);
      if (isEmpty(value) && g.useExistingDataFs()) {
        throw new InvalidParams(
          'existing_data_fs_id is required when use_existing_data_fs = True. Set the file-system id in values.yml, then re-run.',
        );
      }
      return value;
    },

    useExistingOpensearchCluster(): boolean {
      const value = getBool('use_existing_opensearch_cluster', values, false);
      if (value && !g.useExistingVpc()) {
        throw new InvalidParams(
          'use_existing_opensearch_cluster cannot be True if use_existing_vpc = False',
        );
      }
      return value;
    },

    opensearchDomainEndpoint(): string | null {
      const value = getString('opensearch_domain_endpoint', values);
      if (isEmpty(value) && g.useExistingOpensearchCluster()) {
        throw new InvalidParams(
          'opensearch_domain_endpoint is required when use_existing_opensearch_cluster = True',
        );
      }
      return value;
    },

    albCustomCertificateProvided: () =>
      getBool('alb_custom_certificate_provided', values, false),

    albCustomCertificateAcmCertificateArn(): string | null {
      const value = getString('alb_custom_certificate_acm_certificate_arn', values);
      if (g.albCustomCertificateProvided() && isEmpty(value)) {
        throw new ClusterConfigError(
          'Need to provide alb_custom_certificate_acm_certificate_arn if alb_custom_certificate_provided is true',
        );
      }
      return value;
    },

    albCustomDnsName(): string | null {
      const value = getString('alb_custom_dns_name', values);
      if (g.albCustomCertificateProvided() && isEmpty(value)) {
        throw new ClusterConfigError(
          'Need to provide alb_custom_dns_name if alb_custom_certificate_provided is true',
        );
      }
      return value;
    },

    dcvSessionQuicSupport: () => getBool('dcv_session_quic_support', values, false),

    dcvConnectionGatewayCustomCertificateProvided: () =>
      getBool('dcv_connection_gateway_custom_certificate_provided', values, false),

    dcvConnectionGatewayCustomDnsHostname: () =>
      g.requiredWhenGatewayCertificateProvided(
        'dcv_connection_gateway_custom_dns_hostname',
      ),

    dcvConnectionGatewayCustomCertificateCertificateSecretArn: () =>
      g.requiredWhenGatewayCertificateProvided(
        'dcv_connection_gateway_custom_certificate_certificate_secret_arn',
      ),

    dcvConnectionGatewayCustomCertificatePrivateKeySecretArn: () =>
      g.requiredWhenGatewayCertificateProvided(
        'dcv_connection_gateway_custom_certificate_private_key_secret_arn',
      ),

    requiredWhenGatewayCertificateProvided(key: string): string | null {
      const value = getString(key, values);
      if (g.dcvConnectionGatewayCustomCertificateProvided() && isEmpty(value)) {
        throw new ClusterConfigError(
          `Need to provide ${key} if dcv_connection_gateway_custom_certificate_provided is true`,
        );
      }
      return value;
    },

    vpcGatewayEndpoints(): string[] | null {
      if (!g.useVpcEndpoints()) return null;
      if (g.useExistingVpc()) return [];
      if (options.vpcEndpoints === undefined) {
        throw new GeneralException(
          'vpc_gateway_endpoints requires an EC2 lookup: pass vpcEndpoints for a new VPC with use_vpc_endpoints',
        );
      }
      return options.vpcEndpoints.gatewayEndpoints();
    },

    vpcInterfaceEndpoints(): Record<string, unknown> | null {
      if (!g.useVpcEndpoints()) return null;
      if (g.useExistingVpc()) return {};
      if (options.vpcEndpoints === undefined) {
        throw new GeneralException(
          'vpc_interface_endpoints requires an EC2 lookup: pass vpcEndpoints for a new VPC with use_vpc_endpoints',
        );
      }
      return options.vpcEndpoints.interfaceEndpoints();
    },
  };
  return g;
}

/**
 * The full jinja variable set (`config_generator.py:529-591`), in the Python dict's key order.
 * `utils` is added by the generator, which owns the nunjucks environment.
 */
export function buildContext(
  values: UserValues,
  options: BuildContextOptions = {},
): Record<string, unknown> {
  const g = getters(values, options);
  return {
    cluster_name: g.clusterName(),
    cluster_timezone: g.clusterTimezone(),
    cluster_locale: g.clusterLocale(),
    administrator_email: g.administratorEmail(),
    administrator_username: g.administratorUsername(),
    aws_account_id: g.awsAccountId(),
    aws_dns_suffix: g.awsDnsSuffix(),
    aws_partition: g.awsPartition(),
    idea_release_version: ideaVersion(),
    aws_region: g.awsRegion(),
    storage_apps_provider: g.storageAppsProvider(),
    storage_data_provider: g.storageDataProvider(),
    apps_mount_dir: g.appsMountDir(),
    data_mount_dir: g.dataMountDir(),
    prefix_list_ids: g.prefixListIds(),
    client_ip: g.clientIp(),
    ssh_key_pair_name: g.sshKeyPairName(),
    vpc_cidr_block: g.vpcCidrBlock(),
    use_vpc_endpoints: g.useVpcEndpoints(),
    vpc_gateway_endpoints: g.vpcGatewayEndpoints(),
    vpc_interface_endpoints: g.vpcInterfaceEndpoints(),
    identity_provider: g.identityProvider(),
    directory_service_provider: g.directoryServiceProvider(),
    use_existing_directory_service: g.useExistingDirectoryService(),
    directory_id: g.directoryId(),
    directory_service_root_username_secret_arn: g.directoryServiceRootUsernameSecretArn(),
    directory_service_root_password_secret_arn: g.directoryServiceRootPasswordSecretArn(),
    enable_aws_backup: g.enableAwsBackup(),
    enable_ecs: g.enableEcs(),
    kms_key_type: g.kmsKeyType(),
    kms_key_id: g.kmsKeyId(),
    instance_type: g.instanceType(),
    base_os: g.baseOs(),
    instance_ami: g.instanceAmi(),
    volume_size: g.volumeSize(),
    volume_type: g.volumeType(),
    enabled_modules: g.enabledModules(),
    metrics_provider: g.metricsProvider(),
    prometheus_remote_write_url: g.prometheusRemoteWriteUrl(),
    datadog_agent: g.datadogAgent(),
    datadog_api_key_secret_arn: g.datadogApiKeySecretArn(),
    datadog_agent_image: g.datadogAgentImage(),
    use_existing_vpc: g.useExistingVpc(),
    vpc_id: g.vpcId(),
    private_subnet_ids: g.privateSubnetIds(),
    public_subnet_ids: g.publicSubnetIds(),
    use_existing_apps_fs: g.useExistingAppsFs(),
    existing_apps_fs_id: g.existingAppsFsId(),
    use_existing_data_fs: g.useExistingDataFs(),
    existing_data_fs_id: g.existingDataFsId(),
    use_existing_opensearch_cluster: g.useExistingOpensearchCluster(),
    opensearch_domain_endpoint: g.opensearchDomainEndpoint(),
    alb_public: g.albPublic(),
    alb_custom_certificate_provided: g.albCustomCertificateProvided(),
    alb_custom_certificate_acm_certificate_arn: g.albCustomCertificateAcmCertificateArn(),
    alb_custom_dns_name: g.albCustomDnsName(),
    dcv_session_quic_support: g.dcvSessionQuicSupport(),
    dcv_connection_gateway_custom_certificate_provided:
      g.dcvConnectionGatewayCustomCertificateProvided(),
    dcv_connection_gateway_custom_dns_hostname: g.dcvConnectionGatewayCustomDnsHostname(),
    dcv_connection_gateway_custom_certificate_certificate_secret_arn:
      g.dcvConnectionGatewayCustomCertificateCertificateSecretArn(),
    dcv_connection_gateway_custom_certificate_private_key_secret_arn:
      g.dcvConnectionGatewayCustomCertificatePrivateKeySecretArn(),
    dcv_connection_gateway_instance_ami: g.dcvConnectionGatewayInstanceAmi(),
    dcv_connection_gateway_volume_size: g.dcvConnectionGatewayVolumeSize(),
    dcv_broker_instance_ami: g.dcvBrokerInstanceAmi(),
    dcv_broker_volume_size: g.dcvBrokerVolumeSize(),
  };
}
