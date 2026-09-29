/**
 * The VPC endpoint lists the cluster settings template renders when `use_vpc_endpoints` is on.
 *
 * A new VPC gets every endpoint below that the region offers, as the Python generator did. An
 * existing cluster keeps the lists its settings already hold, so regenerating its configuration
 * (upgrades, `config generate --regenerate`) never adds an endpoint it did not have.
 */
import type { TableScanner } from './cluster-config.ts';
import { getBool, type VpcEndpointsLookup } from './values.ts';

export const GATEWAY_ENDPOINTS = ['s3', 'dynamodb'];

/** Interface endpoint services, and whether clients use each by default. */
export const INTERFACE_ENDPOINTS: Record<string, boolean> = {
  'application-autoscaling': true,
  autoscaling: true,
  cloudformation: true,
  ec2: true,
  ec2messages: true,
  ebs: true,
  elasticfilesystem: true,
  'elasticfilesystem-fips': false,
  elasticloadbalancing: true,
  logs: true,
  monitoring: true,
  secretsmanager: true,
  sns: true,
  sqs: true,
  events: true,
  ssm: true,
  ssmmessages: true,
  fsx: true,
  'fsx-fips': false,
  backup: true,
  grafana: true,
  'acm-pca': false,
  'kinesis-streams': true,
};

/** Service names the region offers for one endpoint type. */
export interface VpcEndpointServices {
  serviceNames(input: { awsRegion: string; awsProfile?: string; serviceType: 'Gateway' | 'Interface' }): Promise<string[]>;
}

/** True when the settings template needs the endpoint lists at all. */
export function needsVpcEndpoints(values: Record<string, unknown>): boolean {
  // Same parsing as the values builder, so "true" and 1 mean what they mean there.
  return getBool('use_vpc_endpoints', values, false) && !getBool('use_existing_vpc', values, false);
}

/** The lists for a new VPC: every known endpoint the region offers. */
export async function regionVpcEndpoints(
  api: VpcEndpointServices,
  input: { awsRegion: string; awsProfile?: string; dnsSuffix: string },
): Promise<VpcEndpointsLookup> {
  const prefix = `${input.dnsSuffix.split('.').reverse().join('.')}.${input.awsRegion}.`;
  const offered = async (serviceType: 'Gateway' | 'Interface') =>
    new Set(await api.serviceNames({ awsRegion: input.awsRegion, awsProfile: input.awsProfile, serviceType }));
  const gateway = await offered('Gateway');
  const iface = await offered('Interface');
  const gateways = GATEWAY_ENDPOINTS.filter((name) => gateway.has(prefix + name));
  const interfaces = Object.fromEntries(
    Object.entries(INTERFACE_ENDPOINTS)
      .filter(([name]) => iface.has(prefix + name))
      .map(([name, enabled]) => [name, { enabled, endpoint_url: null }]),
  );
  return { gatewayEndpoints: () => gateways, interfaceEndpoints: () => interfaces };
}

/** The lists from a loaded cluster configuration, where the interface map is already nested. */
export function configVpcEndpoints(get: (key: string) => unknown): VpcEndpointsLookup {
  const gateways = get('cluster.network.vpc_gateway_endpoints');
  const interfaces = get('cluster.network.vpc_interface_endpoints');
  return {
    gatewayEndpoints: () => (Array.isArray(gateways) ? gateways.map(String) : []),
    interfaceEndpoints: () => (interfaces !== null && typeof interfaces === 'object' ? interfaces as Record<string, unknown> : {}),
  };
}

const GATEWAY_KEY = 'cluster.network.vpc_gateway_endpoints';
const INTERFACE_PREFIX = 'cluster.network.vpc_interface_endpoints.';

/** The lists an existing cluster already holds, rebuilt from its flattened settings rows. */
export function settingsVpcEndpoints(rows: ReadonlyArray<Record<string, unknown>>): VpcEndpointsLookup {
  let gateways: string[] = [];
  const interfaces: Record<string, Record<string, unknown>> = {};
  for (const row of rows) {
    const key = String(row['key'] ?? '');
    if (key === GATEWAY_KEY && Array.isArray(row['value'])) gateways = row['value'].map(String);
    if (!key.startsWith(INTERFACE_PREFIX)) continue;
    const [service, field] = key.slice(INTERFACE_PREFIX.length).split('.');
    if (service === undefined || field === undefined) continue;
    (interfaces[service] ??= {})[field] = row['value'] ?? null;
  }
  return { gatewayEndpoints: () => gateways, interfaceEndpoints: () => interfaces };
}

/**
 * The lists for rendering `values`: none when the template does not use them, the cluster's own
 * rows when its settings table exists, otherwise a region lookup (a new install).
 */
export async function vpcEndpointsFor(
  deps: { scan: TableScanner; scanIn?: (input: { awsRegion: string; awsProfile?: string }) => TableScanner; vpcEndpointServices?: VpcEndpointServices },
  values: Record<string, unknown>,
  awsProfile?: string,
): Promise<VpcEndpointsLookup | undefined> {
  if (!needsVpcEndpoints(values)) return undefined;
  const awsRegion = String(values['aws_region'] ?? '');
  // The table lives in the values file's region, which need not be the environment's.
  const scan = deps.scanIn?.({ awsRegion, awsProfile }) ?? deps.scan;
  const rows = await clusterSettingsRows(scan, String(values['cluster_name'] ?? ''));
  if (rows.length > 0) return settingsVpcEndpoints(rows);
  if (deps.vpcEndpointServices === undefined) {
    throw new Error('A new VPC with use_vpc_endpoints needs the region endpoint service lookup');
  }
  return regionVpcEndpoints(deps.vpcEndpointServices, {
    awsRegion,
    awsProfile,
    dnsSuffix: String(values['aws_dns_suffix'] ?? 'amazonaws.com'),
  });
}

async function clusterSettingsRows(scan: TableScanner, clusterName: string): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  let startKey: Record<string, unknown> | undefined;
  try {
    do {
      const page = await scan({ TableName: `${clusterName}.cluster-settings`, ExclusiveStartKey: startKey });
      rows.push(...(page.Items ?? []));
      startKey = page.LastEvaluatedKey;
    } while (startKey !== undefined);
  } catch (error) {
    if ((error as Error).name === 'ResourceNotFoundException') return [];
    throw error;
  }
  return rows;
}
