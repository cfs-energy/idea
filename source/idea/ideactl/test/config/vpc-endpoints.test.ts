import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { flattenConfigDir, generateConfigFromTemplates } from '../../src/config/generator.ts';
import { loadValuesFile } from '../../src/config/values.ts';
import { configVpcEndpoints, regionVpcEndpoints, settingsVpcEndpoints, vpcEndpointsFor, type VpcEndpointServices } from '../../src/config/vpc-endpoints.ts';

const NEW_VPC = join(import.meta.dirname, '../parity/fixtures-synthetic/network-new.yml');

function region(offered: { Gateway: string[]; Interface: string[] }) {
  const calls: string[] = [];
  const api: VpcEndpointServices = {
    async serviceNames({ serviceType }) {
      calls.push(serviceType);
      return offered[serviceType].map((name) => `com.amazonaws.us-east-2.${name}`);
    },
  };
  return { api, calls };
}

const NO_TABLE = async () => { throw Object.assign(new Error('no table'), { name: 'ResourceNotFoundException' }); };

test('a new VPC gets every known endpoint the region offers, fips and pca off', async () => {
  const { api } = region({ Gateway: ['s3', 'dynamodb'], Interface: ['ec2', 'fsx-fips', 'acm-pca', 'not-an-idea-service'] });
  const lookup = await regionVpcEndpoints(api, { awsRegion: 'us-east-2', dnsSuffix: 'amazonaws.com' });
  assert.deepEqual(lookup.gatewayEndpoints(), ['s3', 'dynamodb']);
  assert.deepEqual(lookup.interfaceEndpoints(), {
    ec2: { enabled: true, endpoint_url: null },
    'fsx-fips': { enabled: false, endpoint_url: null },
    'acm-pca': { enabled: false, endpoint_url: null },
  });
});

test('an existing cluster keeps its own lists, endpoint urls included', () => {
  const lookup = settingsVpcEndpoints([
    { key: 'cluster.network.vpc_gateway_endpoints', value: ['s3'] },
    { key: 'cluster.network.vpc_interface_endpoints.ec2.enabled', value: true },
    { key: 'cluster.network.vpc_interface_endpoints.ec2.endpoint_url', value: 'https://vpce-1.ec2.example.invalid' },
    { key: 'cluster.network.vpc_id', value: 'vpc-1' },
  ]);
  assert.deepEqual(lookup.gatewayEndpoints(), ['s3']);
  assert.deepEqual(lookup.interfaceEndpoints(), { ec2: { enabled: true, endpoint_url: 'https://vpce-1.ec2.example.invalid' } });
});

test('regenerating an existing cluster never asks EC2, so an upgrade adds no endpoint', async () => {
  const { api, calls } = region({ Gateway: ['s3', 'dynamodb'], Interface: ['ec2', 'sqs'] });
  const scan = async () => ({ Items: [{ key: 'cluster.network.vpc_gateway_endpoints', value: ['s3'] }] });
  const values = { ...loadValuesFile(NEW_VPC), use_vpc_endpoints: true };
  const lookup = await vpcEndpointsFor({ scan, vpcEndpointServices: api }, values);
  assert.deepEqual(lookup?.gatewayEndpoints(), ['s3']);
  assert.deepEqual(lookup?.interfaceEndpoints(), {});
  assert.deepEqual(calls, []);
});

test('no lookup at all when endpoints are off or the VPC is the customer\'s', async () => {
  const { api, calls } = region({ Gateway: ['s3'], Interface: [] });
  const base = loadValuesFile(NEW_VPC);
  assert.equal(await vpcEndpointsFor({ scan: NO_TABLE, vpcEndpointServices: api }, base), undefined);
  assert.equal(await vpcEndpointsFor({ scan: NO_TABLE, vpcEndpointServices: api }, { ...base, use_vpc_endpoints: true, use_existing_vpc: true }), undefined);
  assert.deepEqual(calls, []);
});

test('a new VPC with endpoints renders its lists instead of failing config generate', async () => {
  const { api } = region({ Gateway: ['s3', 'dynamodb'], Interface: ['ec2', 'sqs'] });
  const values = { ...loadValuesFile(NEW_VPC), use_vpc_endpoints: true };
  assert.throws(() => generateConfigFromTemplates(values, mkdtempSync(join(tmpdir(), 'vpce-before-'))), /requires an EC2 lookup/);
  const out = mkdtempSync(join(tmpdir(), 'vpce-'));
  try {
    generateConfigFromTemplates(values, out, { vpcEndpoints: await vpcEndpointsFor({ scan: NO_TABLE, vpcEndpointServices: api }, values) });
    const flat = flattenConfigDir(out);
    assert.deepEqual(flat['cluster.network.vpc_gateway_endpoints'], ['s3', 'dynamodb']);
    assert.equal(flat['cluster.network.vpc_interface_endpoints.sqs.enabled'], true);
    assert.equal(flat['cluster.network.vpc_interface_endpoints.ec2.endpoint_url'], null);
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});

test('a new VPC with endpoints and no EC2 adapter says what is missing', async () => {
  const values = { ...loadValuesFile(NEW_VPC), use_vpc_endpoints: true };
  await assert.rejects(vpcEndpointsFor({ scan: NO_TABLE }, values), /region endpoint service lookup/);
});

test('string and integer flags parse as the values builder parses them', async () => {
  const { api, calls } = region({ Gateway: ['s3'], Interface: [] });
  const base = loadValuesFile(NEW_VPC);
  for (const on of ['true', 'True', 1]) {
    assert.deepEqual((await vpcEndpointsFor({ scan: NO_TABLE, vpcEndpointServices: api }, { ...base, use_vpc_endpoints: on }))?.gatewayEndpoints(), ['s3']);
  }
  assert.equal(await vpcEndpointsFor({ scan: NO_TABLE, vpcEndpointServices: api }, { ...base, use_vpc_endpoints: true, use_existing_vpc: 'true' }), undefined);
  assert.equal(calls.length, 6);
});

test('the existing-cluster probe reads the values file region and profile, not the environment', async () => {
  const { api, calls } = region({ Gateway: ['s3', 'dynamodb'], Interface: ['ec2'] });
  const targets: string[] = [];
  const scanIn = (target: { awsRegion: string; awsProfile?: string }) => {
    targets.push(`${target.awsRegion}/${target.awsProfile}`);
    return async () => ({ Items: [{ key: 'cluster.network.vpc_gateway_endpoints', value: ['s3'] }] });
  };
  const values = { ...loadValuesFile(NEW_VPC), use_vpc_endpoints: true };
  const lookup = await vpcEndpointsFor({ scan: NO_TABLE, scanIn, vpcEndpointServices: api }, values, 'fixture-profile');
  assert.deepEqual(targets, ['us-east-2/fixture-profile']);
  assert.deepEqual(lookup?.gatewayEndpoints(), ['s3']);
  assert.deepEqual(calls, []);
});

test('a loaded cluster configuration supplies its nested lists unchanged', () => {
  const settings: Record<string, unknown> = {
    'cluster.network.vpc_gateway_endpoints': ['s3', 'dynamodb'],
    'cluster.network.vpc_interface_endpoints': { ec2: { enabled: true, endpoint_url: 'https://vpce-1.ec2.example.invalid' } },
  };
  const lookup = configVpcEndpoints((key) => settings[key] ?? null);
  assert.deepEqual(lookup.gatewayEndpoints(), ['s3', 'dynamodb']);
  assert.deepEqual(lookup.interfaceEndpoints(), settings['cluster.network.vpc_interface_endpoints']);
});
