/**
 * Deployment ordering, `--optimize-deployment` grouping, the 10 s stagger and the
 * post-group status re-check (`deployment_helper.py`).
 */

import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import { after, before, describe, it } from 'node:test';

import {
  deploymentOrder,
  DeploymentHelper,
  OPTIMIZED_DEPLOYMENT_STAGGER_MS,
  optimizedDeploymentOrder,
} from '../../src/cli/deployment-helper.ts';
import { resolveRequestedModules } from '../../src/cli/commands/deploy.ts';
import type { Deps } from '../../src/cli/cdk-invoker.ts';
import { fakeDeps, moduleRow, withTempIdeaHome, type FakeDeps } from '../support/deploy-harness.ts';

const CLUSTER = 'sample-cluster';
const REGION = 'us-east-2';

/** Deliberately out of priority order, so a passing test proves the sort and not the input. */
const MODULES = [
  moduleRow('bastion-host', 'bastion-host', 'stack'),
  moduleRow('vdc', 'virtual-desktop-controller', 'app'),
  moduleRow('global-settings', 'global-settings', 'config'),
  moduleRow('cluster-manager', 'cluster-manager', 'app'),
  moduleRow('analytics', 'analytics', 'stack'),
  moduleRow('cluster', 'cluster', 'stack'),
  moduleRow('scheduler', 'scheduler', 'app'),
  moduleRow('identity-provider', 'identity-provider', 'stack'),
  moduleRow('shared-storage', 'shared-storage', 'stack'),
  moduleRow('metrics', 'metrics', 'stack'),
  moduleRow('directoryservice', 'directoryservice', 'stack'),
];

const ALL_IDS = MODULES.map((module) => module['module_id'] as string);

const home = withTempIdeaHome();
const previousCdkBin = process.env.IDEA_CDK_BIN;
before(() => {
  process.env.IDEA_CDK_BIN = '/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk';
});
after(() => {
  if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
  else process.env.IDEA_CDK_BIN = previousCdkBin;
  home.restore();
});

describe('deploymentOrder', () => {
  it('sorts by module deployment priority and drops config modules', () => {
    assert.deepEqual(deploymentOrder(MODULES as never, ALL_IDS, false), [
      'cluster',
      'analytics',
      'identity-provider',
      'metrics',
      'directoryservice',
      'shared-storage',
      'cluster-manager',
      'vdc',
      'scheduler',
      'bastion-host',
    ]);
  });

  it('skips modules already deployed unless --upgrade', () => {
    const modules = MODULES.map((module) =>
      module['module_id'] === 'cluster' ? { ...module, status: 'deployed' } : module,
    );
    assert.ok(!deploymentOrder(modules as never, ALL_IDS, false).includes('cluster'));
    assert.ok(deploymentOrder(modules as never, ALL_IDS, true).includes('cluster'));
  });

  it('ignores a module id the modules table does not hold', () => {
    assert.deepEqual(deploymentOrder(MODULES as never, ['nosuchmodule'], false), []);
  });

  it('ecs deploys after shared-storage and before cluster-manager', () => {
    const modules = [
      ...MODULES,
      moduleRow('ecs', 'ecs', 'stack'),
    ];
    const ids = modules.map((module) => module['module_id'] as string);
    const order = deploymentOrder(modules as never, ids, false);
    const ecs = order.indexOf('ecs');
    const storage = order.indexOf('shared-storage');
    const manager = order.indexOf('cluster-manager');
    assert.ok(ecs > storage);
    assert.ok(ecs < manager);
  });
});

describe('optimizedDeploymentOrder', () => {
  it('groups equal priorities and keeps the groups in priority order', () => {
    assert.deepEqual(optimizedDeploymentOrder(MODULES as never, ALL_IDS, false), [
      ['cluster'],
      ['analytics', 'identity-provider', 'metrics', 'directoryservice'],
      ['shared-storage'],
      ['cluster-manager'],
      ['vdc', 'scheduler'],
      ['bastion-host'],
    ]);
  });
});

describe('resolveRequestedModules', () => {
  it('dedupes while keeping the first occurrence', () => {
    assert.deepEqual(resolveRequestedModules(['cluster', 'analytics', 'cluster']), {
      allModules: false,
      moduleIds: ['cluster', 'analytics'],
    });
  });

  it('`all` on its own selects every module', () => {
    assert.deepEqual(resolveRequestedModules(['all']), { allModules: true, moduleIds: undefined });
  });

  it('`all` mixed with a module id is a hard error', () => {
    assert.throws(() => resolveRequestedModules(['all', 'cluster']), /"all" deployment must be the only requested module/);
  });
});

describe('DeploymentHelper.invoke', () => {
  const tablesWith = (modules: Array<Record<string, unknown>>): Record<string, Array<Record<string, unknown>>> => ({
    [`${CLUSTER}.modules`]: modules,
    [`${CLUSTER}.cluster-settings`]: [
      { key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' },
      { key: 'global-settings.module_sets.default.cluster.module_id', value: 'cluster' },
    ],
  });

  const openHelper = async (
    modules: Array<Record<string, unknown>>,
    options: { optimizeDeployment?: boolean; moduleIds?: string[]; allModules?: boolean } = {},
  ) => {
    const deps = fakeDeps({ tables: tablesWith(modules) });
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      deploymentId: 'deployment-1',
      deps,
      ...options,
    });
    return { deps, helper };
  };

  it('deploys sequentially in priority order', async () => {
    const { deps, helper } = await openHelper(
      [moduleRow('cluster', 'cluster', 'stack'), moduleRow('metrics', 'metrics', 'stack')],
      { allModules: true },
    );
    await helper.invoke();
    assert.deepEqual(
      deps.spawns.map((argv) => argv[argv.length - 1]),
      ['cdk.out.cluster', 'cdk.out.metrics'],
    );
    assert.deepEqual(deps.sleeps, []);
  });

  it('staggers each module of a group by 10 seconds', async () => {
    const modules = [
      moduleRow('analytics', 'analytics', 'stack'),
      moduleRow('metrics', 'metrics', 'stack'),
      moduleRow('identity-provider', 'identity-provider', 'stack'),
    ].map((module) => ({ ...module, status: 'not-deployed' }));
    const { deps, helper } = await openHelper(modules, { optimizeDeployment: true, allModules: true });
    // Every module reads `deployed` on the re-check, so the group passes.
    deps.scan = async (input) => ({
      Items:
        input.TableName === `${CLUSTER}.modules`
          ? modules.map((module) => ({ ...module, status: 'deployed' }))
          : tablesWith(modules)[input.TableName],
    });
    await helper.invoke();
    assert.deepEqual(deps.sleeps, [
      OPTIMIZED_DEPLOYMENT_STAGGER_MS,
      OPTIMIZED_DEPLOYMENT_STAGGER_MS,
      OPTIMIZED_DEPLOYMENT_STAGGER_MS,
    ]);
    assert.equal(deps.spawns.length, 3);
  });

  it('fails the group when one module fails, naming it', async () => {
    const modules = [moduleRow('analytics', 'analytics', 'stack'), moduleRow('metrics', 'metrics', 'stack')];
    const deps = fakeDeps({ tables: tablesWith(modules), spawnExitCodes: [0, 1] });
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      optimizeDeployment: true,
      allModules: true,
      deps,
    });
    await assert.rejects(() => helper.invoke(), /deployment failed\. could not deploy module\(s\): metrics/);
  });

  it('fails when a module in the group did not reach status deployed', async () => {
    const modules = [moduleRow('analytics', 'analytics', 'stack'), moduleRow('metrics', 'metrics', 'stack')];
    const { helper } = await openHelper(modules, { optimizeDeployment: true, allModules: true });
    await assert.rejects(
      () => helper.invoke(),
      /Module analytics on sample-cluster is not deployed after its stack run/,
    );
  });

  it('says so instead of deploying when everything is already deployed', async () => {
    const { deps, helper } = await openHelper(
      [moduleRow('cluster', 'cluster', 'stack', 'deployed')],
      { allModules: true },
    );
    await assert.rejects(() => helper.invoke(), { name: 'ExitWithCode', code: 1 });
    assert.deepEqual(deps.spawns, []);
    assert.ok(deps.stdout.some((line) => line.includes('is already deployed. use the --upgrade flag')));
  });

  for (const optimizeDeployment of [false, true]) {
    it(`refuses an already deployed group with optimizeDeployment=${optimizeDeployment}`, async () => {
      const { deps, helper } = await openHelper(
        [moduleRow('analytics', 'analytics', 'stack', 'deployed'), moduleRow('metrics', 'metrics', 'stack', 'deployed')],
        { allModules: true, optimizeDeployment },
      );
      await assert.rejects(() => helper.invoke(), { name: 'ExitWithCode', code: 1 });
      assert.deepEqual(deps.spawns, []);
      assert.ok(deps.stdout.includes('[analytics, metrics] are already deployed. use the --upgrade flag to re-deploy these modules.'));
    });
  }

  it('re-deploys a deployed module with --upgrade', async () => {
    const deps = fakeDeps({ tables: tablesWith([moduleRow('cluster', 'cluster', 'stack', 'deployed')]) });
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      allModules: true,
      upgrade: true,
      deps,
    });
    await helper.invoke();
    assert.equal(deps.spawns.length, 1);
  });

  /**
   * A prefix list already holding one of the configured addresses. `spawnsWhenModified` records
   * how many module stacks had run at the moment of each write, which is how the ordering is
   * checked without reaching into the helper.
   */
  function fakePrefixList(spawns: string[][]): {
    api: NonNullable<Deps['prefixList']>;
    modifications: Array<Record<string, unknown>>;
    spawnsWhenModified: number[];
  } {
    const modifications: Array<Record<string, unknown>> = [];
    const spawnsWhenModified: number[] = [];
    return {
      modifications,
      spawnsWhenModified,
      api: {
        getManagedPrefixListEntries: async () => ({
          Entries: [{ Cidr: '198.51.100.0/24', Description: 'Allow access to cluster from Client IP' }],
        }),
        describeManagedPrefixLists: async () => ({ PrefixLists: [{ Version: 4 }] }),
        modifyManagedPrefixList: async (input) => {
          modifications.push(input as unknown as Record<string, unknown>);
          spawnsWhenModified.push(spawns.length);
        },
      },
    };
  }

  const PREFIX_LIST_SETTINGS = [
    { key: 'cluster.network.cluster_prefix_list_id', value: 'pl-0123456789abcdef0' },
    // one already in the list, one bare address that needs the /32, one duplicate of the first
    { key: 'cluster.network.client_ip', value: ['198.51.100.0/24', '192.0.2.7', '198.51.100.0/24'] },
  ];

  it('merges only the missing client addresses into the prefix list, after the cluster module', async () => {
    const modules = [moduleRow('cluster', 'cluster', 'stack'), moduleRow('metrics', 'metrics', 'stack')];
    const tables = tablesWith(modules);
    const deps = fakeDeps({
      tables: {
        ...tables,
        [`${CLUSTER}.cluster-settings`]: [...(tables[`${CLUSTER}.cluster-settings`] ?? []), ...PREFIX_LIST_SETTINGS],
      },
    });
    const prefixList = fakePrefixList(deps.spawns);
    deps.prefixList = prefixList.api;
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      deploymentId: 'deployment-1',
      allModules: true,
      deps,
    });

    await helper.invoke();

    assert.equal(prefixList.modifications.length, 1, 'one merge, not one per module');
    assert.deepEqual(prefixList.modifications[0], {
      PrefixListId: 'pl-0123456789abcdef0',
      CurrentVersion: 4,
      AddEntries: [{ Cidr: '192.0.2.7/32', Description: 'Allow access to cluster from Client IP' }],
    });
    assert.equal(
      (prefixList.modifications[0] as Record<string, unknown>)['RemoveEntries'],
      undefined,
      'the merge never removes an entry',
    );
    // Two modules deploy; the merge happens with the cluster module's stack run behind it and the
    // next module's still to come.
    assert.deepEqual(prefixList.spawnsWhenModified, [1]);
    assert.equal(deps.spawns.length, 2);
  });

  it('does not touch the prefix list when no client address is configured', async () => {
    const { deps, helper } = await openHelper([moduleRow('cluster', 'cluster', 'stack')], { allModules: true });
    const prefixList = fakePrefixList(deps.spawns);
    deps.prefixList = prefixList.api;
    await helper.invoke();
    assert.deepEqual(prefixList.modifications, []);
  });

  it('refreshModules retries once on ExpiredTokenException', async () => {
    const modules = [moduleRow('analytics', 'analytics', 'stack'), moduleRow('metrics', 'metrics', 'stack')]
      .map((module) => ({ ...module, status: 'not-deployed' }));
    const { deps, helper } = await openHelper(modules, { optimizeDeployment: true, allModules: true });
    let scans = 0;
    let refreshAttempts = 0;
    deps.scan = async (input) => {
      if (input.TableName === `${CLUSTER}.modules`) {
        scans += 1;
        if (deps.spawns.length >= 2) {
          refreshAttempts += 1;
          if (refreshAttempts === 1) {
            const error = new Error('expired');
            error.name = 'ExpiredTokenException';
            throw error;
          }
          return { Items: modules.map((module) => ({ ...module, status: 'deployed' })) };
        }
        return { Items: modules };
      }
      return { Items: tablesWith(modules)[input.TableName] ?? [] };
    };
    await helper.invoke();
    assert.ok(scans >= 2);
    assert.equal(refreshAttempts, 2);
  });
});

/**
 * The certificates the deploy tool generates before the stacks that read their ARNs synthesize.
 *
 * What is proved here is the ordering and the published rows, not the generation: the generation
 * is `test/certificates`. A hook that ran after the stack deploy, or wrote the row under the
 * module name rather than the module id, would leave the synthesis reading a key nothing wrote.
 */
describe('DeploymentHelper certificate hook', () => {
  const SETTINGS = [
    { key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' },
    { key: 'cluster.route53.private_hosted_zone_name', value: `${CLUSTER}.${REGION}.local` },
    { key: 'global-settings.module_sets.default.cluster.module_id', value: 'cluster' },
    { key: 'global-settings.module_sets.default.directoryservice.module_id', value: 'directoryservice' },
    { key: 'global-settings.module_sets.default.virtual-desktop-controller.module_id', value: 'vdc' },
  ];

  /** Records every request, with the number of stack runs that had happened when it was made. */
  function fakeCertificates(): {
    certificates: NonNullable<Deps['certificates']>;
    requests: Array<Record<string, unknown>>;
    spawnsWhenRequested: number[];
  } {
    const requests: Array<Record<string, unknown>> = [];
    const spawnsWhenRequested: number[] = [];
    return {
      requests,
      spawnsWhenRequested,
      certificates: {
        secrets: {
          // Nothing exists yet, so every request takes the generate-and-create path.
          listSecretsByTagValue: async () => [],
          createSecret: async (input) => `arn:aws:secretsmanager:${REGION}:1:secret:${input.Name}`,
        },
        acm: {
          listIssuedCertificates: async () => [],
          importCertificate: async () => 'arn:aws:acm:us-east-2:1:certificate/imported',
        },
        openssl: (args) => {
          const file = (flag: string): string => args[args.indexOf(flag) + 1] as string;
          writeFileSync(file('-out'), 'certificate-pem');
          writeFileSync(file('-keyout'), 'private-key-pem');
          return { status: 0, stderr: '' };
        },
      },
    };
  }

  /** Wraps the fake so each request is recorded with the stack-run count at that moment. */
  function recordingCertificates(deps: FakeDeps): {
    requests: Array<Record<string, unknown>>;
    spawnsWhenRequested: number[];
  } {
    const fake = fakeCertificates();
    const inner = fake.certificates.secrets.listSecretsByTagValue;
    fake.certificates.secrets.listSecretsByTagValue = async (tagKey, tagValues) => {
      fake.requests.push({ certificateName: (tagValues[0] as string).replace(/-certificate$/, '') });
      fake.spawnsWhenRequested.push(deps.spawns.length);
      return inner(tagKey, tagValues);
    };
    deps.certificates = fake.certificates;
    return fake;
  }

  const openWith = async (
    modules: Array<Record<string, unknown>>,
    settings: Array<Record<string, unknown>>,
  ) => {
    const deps = fakeDeps({
      tables: {
        [`${CLUSTER}.modules`]: modules,
        [`${CLUSTER}.cluster-settings`]: [...SETTINGS, ...settings],
      },
    });
    const helper = await DeploymentHelper.open({
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      deploymentId: 'deployment-1',
      allModules: true,
      deps,
    });
    return { deps, helper };
  };

  const settingWrites = (deps: FakeDeps): Array<{ key: string; value: unknown }> =>
    deps.writes
      .filter((write) => write.op === 'setConfigEntry')
      .map((write) => write.payload as { key: string; value: unknown });

  it('generates both cluster certificates before the cluster stack runs, and publishes six rows', async () => {
    const { deps, helper } = await openWith(
      [moduleRow('cluster', 'cluster', 'stack')],
      [{ key: 'cluster.load_balancers.external_alb.certificates.provided', value: false }],
    );
    const recorded = recordingCertificates(deps);

    await helper.invoke();

    assert.deepEqual(
      recorded.requests.map((request) => request['certificateName']),
      [`${CLUSTER}-external`, `${CLUSTER}-internal`],
    );
    // Both requests happen with no stack run behind them: the synthesis reads the rows they write.
    assert.deepEqual(recorded.spawnsWhenRequested, [0, 0]);
    assert.equal(deps.spawns.length, 1);

    assert.deepEqual(settingWrites(deps).map((write) => write.key), [
      'cluster.load_balancers.external_alb.certificates.certificate_secret_arn',
      'cluster.load_balancers.external_alb.certificates.private_key_secret_arn',
      'cluster.load_balancers.external_alb.certificates.acm_certificate_arn',
      'cluster.load_balancers.internal_alb.certificates.certificate_secret_arn',
      'cluster.load_balancers.internal_alb.certificates.private_key_secret_arn',
      'cluster.load_balancers.internal_alb.certificates.acm_certificate_arn',
    ]);
  });

  it('carries the names, domains and tags the certificate resources passed', async () => {
    const { deps, helper } = await openWith(
      [moduleRow('cluster', 'cluster', 'stack')],
      [
        { key: 'cluster.load_balancers.external_alb.certificates.provided', value: false },
        { key: 'cluster.secretsmanager.kms_key_id', value: 'alias/sample' },
      ],
    );
    const hooks = helper.certificateHooks({
      module_id: 'cluster',
      name: 'cluster',
      type: 'stack',
    } as never);

    assert.deepEqual(
      hooks.map((hook) => hook.request),
      [
        {
          certificateName: `${CLUSTER}-external`,
          domainName: `${CLUSTER}.idea.default`,
          tags: { Name: `${CLUSTER} external alb certs`, 'idea:ClusterName': CLUSTER },
          kmsKeyId: 'alias/sample',
          importToAcm: true,
        },
        {
          certificateName: `${CLUSTER}-internal`,
          domainName: `*.${CLUSTER}.${REGION}.local`,
          tags: { Name: `${CLUSTER} internal alb certs`, 'idea:ClusterName': CLUSTER },
          kmsKeyId: 'alias/sample',
          importToAcm: true,
        },
      ],
    );
    assert.equal(deps.spawns.length, 0);
  });

  it('a provided external certificate is the operator\'s, so only the internal one is generated', async () => {
    const { deps, helper } = await openWith(
      [moduleRow('cluster', 'cluster', 'stack')],
      [{ key: 'cluster.load_balancers.external_alb.certificates.provided', value: true }],
    );
    const recorded = recordingCertificates(deps);

    await helper.invoke();

    assert.deepEqual(
      recorded.requests.map((request) => request['certificateName']),
      [`${CLUSTER}-internal`],
    );
    assert.deepEqual(settingWrites(deps).map((write) => write.key), [
      'cluster.load_balancers.internal_alb.certificates.certificate_secret_arn',
      'cluster.load_balancers.internal_alb.certificates.private_key_secret_arn',
      'cluster.load_balancers.internal_alb.certificates.acm_certificate_arn',
    ]);
  });

  it('an ISSUED ACM certificate for the domain is the row that is published', async () => {
    const { deps, helper } = await openWith(
      [moduleRow('cluster', 'cluster', 'stack')],
      [{ key: 'cluster.load_balancers.external_alb.certificates.provided', value: true }],
    );
    const fake = fakeCertificates();
    fake.certificates.acm.listIssuedCertificates = async () => [
      { DomainName: `*.${CLUSTER}.${REGION}.local`, CertificateArn: 'arn:aws:acm:us-east-2:1:certificate/internal' },
    ];
    deps.certificates = fake.certificates;

    await helper.invoke();

    assert.deepEqual(
      settingWrites(deps).find((write) => write.key.endsWith('internal_alb.certificates.acm_certificate_arn')),
      {
        key: 'cluster.load_balancers.internal_alb.certificates.acm_certificate_arn',
        value: 'arn:aws:acm:us-east-2:1:certificate/internal',
      },
    );
  });

  it('openldap gets a certificate and a managed directory does not', async () => {
    const info = { module_id: 'directoryservice', name: 'directoryservice', type: 'stack' };
    for (const [provider, expected] of [
      ['openldap', [`${CLUSTER}-directoryservice`]],
      ['aws_managed_activedirectory', []],
    ] as const) {
      const { helper } = await openWith(
        [moduleRow('directoryservice', 'directoryservice', 'stack')],
        [
          { key: 'directoryservice.provider', value: provider },
          { key: 'directoryservice.hostname', value: `directoryservice.${CLUSTER}.${REGION}.local` },
        ],
      );
      const hooks = helper.certificateHooks(info as never);
      assert.deepEqual(hooks.map((hook) => hook.request.certificateName), expected);
      assert.deepEqual(
        hooks.flatMap((hook) => [hook.certificateKey, hook.privateKeyKey]),
        provider === 'openldap'
          ? ['directoryservice.tls_certificate_secret_arn', 'directoryservice.tls_private_key_secret_arn']
          : [],
      );
    }
  });

  it('the openldap request has no ACM import and the hostname as its domain', async () => {
    const { helper } = await openWith(
      [moduleRow('directoryservice', 'directoryservice', 'stack')],
      [
        { key: 'directoryservice.provider', value: 'openldap' },
        { key: 'directoryservice.hostname', value: `directoryservice.${CLUSTER}.${REGION}.local` },
      ],
    );
    assert.deepEqual(
      helper.certificateHooks({ module_id: 'directoryservice', name: 'directoryservice', type: 'stack' } as never)[0]
        ?.request,
      {
        certificateName: `${CLUSTER}-directoryservice`,
        domainName: `directoryservice.${CLUSTER}.${REGION}.local`,
        tags: {
          Name: `${CLUSTER}-directoryservice`,
          'idea:ClusterName': CLUSTER,
          'idea:ModuleName': 'directoryservice',
        },
        kmsKeyId: undefined,
        importToAcm: false,
      },
    );
  });

  it('the desktop module publishes its gateway rows under the module id', async () => {
    const { deps, helper } = await openWith(
      [moduleRow('vdc', 'virtual-desktop-controller', 'app')],
      [
        { key: 'vdc.dcv_connection_gateway.certificate.provided', value: false },
        { key: 'ecs.enabled', value: true },
      ],
    );
    const recorded = recordingCertificates(deps);

    await helper.invoke();

    assert.deepEqual(
      recorded.requests.map((request) => request['certificateName']),
      [`${CLUSTER}-vdc-gateway-certificate`],
    );
    assert.deepEqual(settingWrites(deps).map((write) => write.key), [
      'vdc.dcv_connection_gateway.certificate.certificate_secret_arn',
      'vdc.dcv_connection_gateway.certificate.private_key_secret_arn',
    ]);
    assert.deepEqual(
      helper.certificateHooks({ module_id: 'vdc', name: 'virtual-desktop-controller', type: 'app' } as never)[0]
        ?.request,
      {
        certificateName: `${CLUSTER}-vdc-gateway-certificate`,
        domainName: `vdc.${CLUSTER}.idea.default`,
        tags: {
          Name: `${CLUSTER}-vdc-gateway Self Signed Certificate`,
          'idea:ClusterName': CLUSTER,
          'idea:ModuleName': 'virtual-desktop-controller',
        },
        kmsKeyId: undefined,
        importToAcm: false,
      },
    );
  });

  it('a desktop module with a provided certificate is left alone', async () => {
    const { deps, helper } = await openWith(
      [moduleRow('vdc', 'virtual-desktop-controller', 'app')],
      [
        { key: 'vdc.dcv_connection_gateway.certificate.provided', value: true },
        { key: 'ecs.enabled', value: true },
      ],
    );
    const recorded = recordingCertificates(deps);

    await helper.invoke();

    assert.deepEqual(recorded.requests, []);
    assert.deepEqual(settingWrites(deps), []);
    assert.equal(deps.spawns.length, 1);
  });

  it('no module outside the three carries a certificate', async () => {
    const { deps, helper } = await openWith([moduleRow('analytics', 'analytics', 'stack')], []);
    const recorded = recordingCertificates(deps);
    await helper.invoke();
    assert.deepEqual(recorded.requests, []);
  });

  it('the published rows are readable from the configuration this process holds', async () => {
    const { deps, helper } = await openWith(
      [moduleRow('vdc', 'virtual-desktop-controller', 'app')],
      [
        { key: 'vdc.dcv_connection_gateway.certificate.provided', value: false },
        { key: 'ecs.enabled', value: true },
      ],
    );
    recordingCertificates(deps);

    await helper.invoke();

    // `certificateHooks` reads the same configuration object the hook writes into, so a row that
    // reached only the table would leave this undefined.
    assert.equal(
      (helper as unknown as { config: { getString(key: string): string | undefined } }).config.getString(
        'virtual-desktop-controller.dcv_connection_gateway.certificate.certificate_secret_arn',
      ),
      `arn:aws:secretsmanager:${REGION}:1:secret:${CLUSTER}-vdc-gateway-certificate-certificate`,
    );
  });
});
