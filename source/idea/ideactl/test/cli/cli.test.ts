/**
 * The command tree, the option surface, the exit codes, and
 * the read-only status commands.
 */

import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, it } from 'node:test';
import type { Command } from 'commander';

import { ClusterConfigError, ClusterConfig } from '../../src/config/cluster-config.ts';
import { ChangeSetRefused, ExitWithCode } from '../../src/cli/cdk-invoker.ts';
import { buildProgram, run } from '../../src/cli/main.ts';
import { cdkAppArgv } from '../../src/cli/commands/cdk.ts';
import { checkClusterStatus, connectionInfo, modulesTable, sessionManagerUrl, statusEndpoints } from '../../src/cli/commands/status.ts';
import { asBoolFlag } from '../../src/cli/commands/deploy.ts';
import { fakeDeps, moduleRow, withTempIdeaHome } from '../support/deploy-harness.ts';

const CLUSTER = 'sample-cluster';
const REGION = 'us-east-2';

const home = withTempIdeaHome();
after(() => home.restore());

function findCommand(program: Command, path: readonly string[]): Command {
  let node = program;
  for (const name of path) {
    const child = node.commands.find((candidate) => candidate.name() === name);
    assert.ok(child !== undefined, `command not found: ${path.join(' ')}`);
    node = child;
  }
  return node;
}

function optionFlags(command: Command): string[] {
  return command.options.flatMap((option) => [option.short, option.long].filter((flag): flag is string => flag !== null && flag !== undefined)).sort();
}

/**
 * Every command in scope, with the options `app_main.py` declares for it. `-h/--help`
 * is on every command through `CLICK_SETTINGS` and is not repeated here.
 */
const EXPECTED_OPTIONS: Array<{ path: string[]; options: string[] }> = [
  { path: ['about'], options: ['--no-banner'] },
  { path: ['quick-setup-help'], options: [] },
  {
    path: ['quick-setup'],
    options: [
      '--allow-replacement',
      '--deployment-id',
      '--existing-resources',
      '--force',
      '--module-set',
      '--no-rollback',
      '--optimize-deployment',
      '--rollback',
      '--skip-config',
      '--termination-protection',
      '--values-file',
    ],
  },
  { path: ['config', 'generate'], options: ['--config-dir', '--existing-resources', '--force', '--regenerate', '--values-file'] },
  {
    path: ['config', 'update'],
    options: ['--aws-profile', '--aws-region', '--cluster-name', '--config-dir', '--force', '--key-prefix', '--module-set', '--overwrite'],
  },
  { path: ['config', 'set'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--force'] },
  { path: ['config', 'show'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--format', '--query', '-q'] },
  { path: ['config', 'export'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--export-dir'] },
  { path: ['config', 'delete'], options: ['--aws-profile', '--aws-region', '--cluster-name'] },
  { path: ['config', 'diff'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--config-dir'] },
  { path: ['config', 'save-values'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--values-file'] },
  { path: ['config', 'download-values'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--values-dir'] },
  {
    path: ['bootstrap'],
    options: [
      '--aws-profile',
      '--aws-region',
      '--cloudformation-execution-policies',
      '--cluster-name',
      '--custom-permissions-boundary',
      '--module-set',
      '--public-access-block-configuration',
      '--termination-protection',
    ],
  },
  {
    path: ['deploy'],
    options: [
      '--allow-replacement',
      '--aws-profile',
      '--aws-region',
      '--cluster-name',
      '--deployment-id',
      '--force-build-bootstrap',
      '--module-set',
      '--no-rollback',
      '--optimize-deployment',
      '--rollback',
      '--termination-protection',
      '--upgrade',
    ],
  },
  { path: ['cdk', 'synth'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--deployment-id', '--module-set'] },
  { path: ['cdk', 'diff'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--deployment-id', '--module-set'] },
  {
    path: ['cdk', 'cdk-app'],
    options: [
      '--aws-profile',
      '--aws-region',
      '--cluster-name',
      '--config-file',
      '--deployment-id',
      '--module-id',
      '--module-name',
      '--synth-reads',
      '--termination-protection',
    ],
  },
  {
    path: ['check-cluster-status'],
    options: ['--aws-profile', '--aws-region', '--cluster-name', '--debug', '--module-set', '--wait', '--wait-timeout'],
  },
  { path: ['list-modules'], options: ['--aws-profile', '--aws-region', '--cluster-name'] },
  { path: ['show-connection-info'], options: ['--aws-profile', '--aws-region', '--cluster-name', '--module-set'] },
];

describe('the command tree', () => {
  const program = buildProgram(fakeDeps());

  it('registers every command in this group', () => {
    // Other command groups register onto the same program, so this asserts the commands this
    // group owns are present rather than pinning the whole tree.
    const registered = new Set(program.commands.map((command) => command.name()));
    for (const name of [
      'about',
      'bootstrap',
      'cdk',
      'check-cluster-status',
      'config',
      'deploy',
      'list-modules',
      'quick-setup',
      'quick-setup-help',
      'show-connection-info',
    ]) {
      assert.ok(registered.has(name), `command not registered: ${name}`);
    }
  });

  it('holds the config sub-group in full', () => {
    assert.deepEqual(findCommand(program, ['config']).commands.map((command) => command.name()).sort(), [
      'delete',
      'diff',
      'download-values',
      'export',
      'generate',
      // registered by the drift preview, not by this group
      'preview-upgrade',
      'save-values',
      'set',
      'show',
      'update',
    ]);
  });

  it('holds the cdk sub-group in full', () => {
    assert.deepEqual(findCommand(program, ['cdk']).commands.map((command) => command.name()).sort(), [
      'cdk-app',
      'diff',
      'synth',
    ]);
  });

  for (const expected of EXPECTED_OPTIONS) {
    it(`\`${expected.path.join(' ')}\` declares the same options as the Python command`, () => {
      assert.deepEqual(optionFlags(findCommand(program, expected.path)), [...expected.options].sort());
    });
  }

  it('requires --cluster-name and --aws-region wherever Python does', () => {
    for (const path of [['config', 'update'], ['deploy'], ['bootstrap'], ['cdk', 'synth'], ['list-modules']]) {
      const command = findCommand(program, path);
      const required = command.options.filter((option) => option.mandatory).map((option) => option.long).sort();
      assert.deepEqual(required, ['--aws-region', '--cluster-name'], `${path.join(' ')} required options`);
    }
  });

  it('takes MODULES as a variadic argument on deploy', () => {
    const deploy = findCommand(program, ['deploy']);
    assert.equal(deploy.registeredArguments.length, 1);
    assert.equal(deploy.registeredArguments[0]?.variadic, true);
    assert.equal(deploy.registeredArguments[0]?.required, true);
  });

  it('accepts --allow-replacement more than once', async () => {
    const collected: string[][] = [];
    const spy = buildProgram(fakeDeps());
    const deployCommand = findCommand(spy, ['deploy']);
    deployCommand.action((_modules: string[], options: { allowReplacement?: string[] }) => {
      collected.push(options.allowReplacement ?? []);
    });
    await spy.parseAsync(
      ['deploy', 'analytics', '--cluster-name', CLUSTER, '--aws-region', REGION, '--allow-replacement', 'a', '--allow-replacement', 'b'],
      { from: 'user' },
    );
    assert.deepEqual(collected, [['a', 'b']]);
  });
});

describe('cdkAppArgv', () => {
  it('rebuilds the argv the CDK app entry point parses', () => {
    assert.deepEqual(
      cdkAppArgv({
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleId: 'vdc',
        moduleName: 'virtual-desktop-controller',
        deploymentId: 'deployment-1',
        terminationProtection: 'false',
        awsProfile: 'admin',
        configFile: '/tmp/cluster-settings.json',
      }),
      [
        '--cluster-name',
        CLUSTER,
        '--aws-region',
        REGION,
        '--module-id',
        'vdc',
        '--module-name',
        'virtual-desktop-controller',
        '--deployment-id',
        'deployment-1',
        '--termination-protection',
        'false',
        '--aws-profile',
        'admin',
        '--config-file',
        '/tmp/cluster-settings.json',
      ],
    );
  });
});

describe('exit behaviour', () => {
  it('with no arguments prints help and exits 0', async () => {
    assert.equal(await run([], fakeDeps()), 0);
  });

  it('a configuration error is one red line and a non-zero exit', async () => {
    const deps = fakeDeps();
    const program = buildProgram(deps);
    program.exitOverride();
    // `run` maps the error class; drive it through a command that raises one.
    const failing = fakeDeps();
    failing.scan = async () => {
      throw new ClusterConfigError('cluster configuration error: cluster.cluster_s3_bucket not found');
    };
    assert.equal(await run(['list-modules', '--cluster-name', CLUSTER, '--aws-region', REGION], failing), 1);
    assert.ok(failing.stderr.some((line) => line.includes('cluster configuration error')));
  });

  it('an uninitialised cluster configuration says so', async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      const error = new Error(`Configuration tables not found for cluster: ${CLUSTER}`);
      error.name = 'ClusterConfigDbError';
      throw error;
    };
    assert.equal(await run(['list-modules', '--cluster-name', CLUSTER, '--aws-region', REGION], deps), 1);
    assert.ok(
      deps.stderr.some(
        (line) =>
          line ===
          `Configuration tables not found for cluster ${CLUSTER} in ${REGION}. Create them with ideactl config update --cluster-name ${CLUSTER} --aws-region ${REGION}, or confirm the cluster was installed in this account and region.`,
      ),
    );
  });

  it('a refused change set exits non-zero without a traceback', async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new ChangeSetRefused('change-set guard refused 1 change(s)', { refusals: [], allowed: [], empty: false });
    };
    assert.equal(await run(['list-modules', '--cluster-name', CLUSTER, '--aws-region', REGION], deps), 1);
  });

  it('an ExitWithCode(0) exits 0 silently', async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new ExitWithCode(0);
    };
    assert.equal(await run(['list-modules', '--cluster-name', CLUSTER, '--aws-region', REGION], deps), 0);
  });

  it('an unexpected error prints one line and exits 1', async () => {
    const deps = fakeDeps();
    deps.scan = async () => {
      throw new TypeError('a real bug');
    };
    assert.equal(await run(['list-modules', '--cluster-name', CLUSTER, '--aws-region', REGION], deps), 1);
    assert.ok(deps.stderr.some((line) => line === 'a real bug'));
    assert.equal(
      deps.stderr.some((line) => line.startsWith('Command failed with error:')),
      false,
    );
  });

  it('a missing required option exits non-zero', async () => {
    assert.notEqual(await run(['list-modules'], fakeDeps()), 0);
  });
});

// -------------------------------------------------------------------------------------------
// status commands
// -------------------------------------------------------------------------------------------

const STATUS_TABLES = {
  [`${CLUSTER}.modules`]: [
    moduleRow('cluster', 'cluster', 'stack', 'deployed'),
    moduleRow('analytics', 'analytics', 'stack', 'deployed'),
    moduleRow('cluster-manager', 'cluster-manager', 'app', 'deployed'),
    moduleRow('scheduler', 'scheduler', 'app', 'deployed'),
    moduleRow('bastion-host', 'bastion-host', 'stack', 'deployed'),
  ],
  [`${CLUSTER}.cluster-settings`]: [
    { key: 'global-settings.module_sets.default.cluster.module_id', value: 'cluster' },
    { key: 'cluster.load_balancers.external_alb.certificates.provided', value: false },
    { key: 'cluster.cluster_name', value: CLUSTER },
    { key: 'cluster.aws.partition', value: 'aws' },
    { key: 'cluster.network.ssh_key_pair', value: 'sample-keypair' },
    { key: 'cluster.load_balancers.external_alb.load_balancer_dns_name', value: 'alb.example.invalid' },
    { key: 'cluster.webportal_domain_name', value: 'portal.example.invalid' },
    { key: 'bastion-host.public_ip', value: '192.0.2.10' },
    { key: 'bastion-host.base_os', value: 'amazonlinux2023' },
    { key: 'bastion-host.instance_id', value: 'i-0123456789abcdef0' },
  ],
};

async function statusConfig(): Promise<ClusterConfig> {
  return ClusterConfig.fromDynamoDb(CLUSTER, REGION, { scan: fakeDeps({ tables: STATUS_TABLES }).scan });
}

describe('check-cluster-status', () => {
  it('checks the analytics dashboard and one healthcheck per app module', async () => {
    const endpoints = statusEndpoints(await statusConfig());
    assert.deepEqual(
      endpoints.map((endpoint) => endpoint.name),
      ['OpenSearch Service Dashboard', 'Cluster Manager', 'Scale-Out Computing'],
    );
    assert.ok(endpoints[0]?.endpoint.endsWith('/_dashboards/'));
    assert.ok(endpoints[1]?.endpoint.endsWith('/cluster-manager/healthcheck'));
  });

  it('exits 1 when an endpoint fails', async () => {
    const deps = fakeDeps({ tables: STATUS_TABLES, httpStatus: (url) => (url.includes('scheduler') ? 503 : 200) });
    await assert.rejects(
      () => checkClusterStatus(deps, { clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default' }),
      (error: Error) => error.name === 'ExitWithCode' && (error as ExitWithCode).code === 1,
    );
    assert.ok(deps.stdout.some((line) => line.includes('FAIL')));
  });

  it('polls every 60 seconds while --wait, then times out', async () => {
    const deps = fakeDeps({ tables: STATUS_TABLES, httpStatus: () => 503 });
    await assert.rejects(() =>
      checkClusterStatus(deps, {
        clusterName: CLUSTER,
        awsRegion: REGION,
        moduleSet: 'default',
        wait: true,
        waitTimeout: 120,
      }),
    );
    assert.deepEqual(deps.sleeps, [60_000, 60_000]);
    assert.ok(deps.stderr.some((line) => line.includes('check endpoint status timed-out')));
  });

  it('returns without waiting once every endpoint is healthy', async () => {
    const deps = fakeDeps({ tables: STATUS_TABLES });
    assert.equal(
      await checkClusterStatus(deps, { clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default', wait: true }),
      0,
    );
    assert.deepEqual(deps.sleeps, []);
  });
});

describe('list-modules', () => {
  it('prints the seven Python columns', async () => {
    const table = modulesTable((await statusConfig()).modules());
    assert.ok(table.includes('| Title'));
    for (const header of ['Name', 'Module ID', 'Type', 'Stack Name', 'Version', 'Status']) {
      assert.ok(table.includes(header), `missing column ${header}`);
    }
    assert.ok(table.includes('Scale-Out Computing'));
  });
});

describe('show-connection-info', () => {
  it('sorts by the hardcoded weights', async () => {
    const entries = connectionInfo(await statusConfig(), REGION);
    assert.deepEqual(entries.map((entry) => entry.key), [
      'Web Portal',
      'Bastion Host (SSH Access)',
      'Bastion Host (Session Manager URL)',
      'Analytics Dashboard',
    ]);
    assert.ok(entries[1]?.value.startsWith('ssh -i ~/.ssh/sample-keypair.pem ec2-user@192.0.2.10'));
  });

  it('skips modules that are not deployed', async () => {
    const deps = fakeDeps({
      tables: {
        ...STATUS_TABLES,
        [`${CLUSTER}.modules`]: [moduleRow('cluster-manager', 'cluster-manager', 'app', 'not-deployed')],
      },
    });
    const config = await ClusterConfig.fromDynamoDb(CLUSTER, REGION, { scan: deps.scan });
    assert.deepEqual(connectionInfo(config, REGION), []);
  });

  it('builds the session-manager URL per partition', () => {
    assert.equal(
      sessionManagerUrl('aws', REGION, 'i-0123456789abcdef0'),
      `https://${REGION}.console.aws.amazon.com/systems-manager/session-manager/i-0123456789abcdef0?region=${REGION}`,
    );
    assert.ok(sessionManagerUrl('aws-us-gov', 'us-gov-west-1', 'i-1').startsWith('https://console.amazonaws-us-gov.com/'));
    assert.ok(sessionManagerUrl('aws-cn', 'cn-north-1', 'i-1').startsWith('https://console.amazonaws.cn/'));
  });
});

describe('the commander wiring reaches each action', () => {
  const previousCdkBin = process.env.IDEA_CDK_BIN;
  before(() => {
    process.env.IDEA_CDK_BIN = '/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk';
  });
  after(() => {
    if (previousCdkBin === undefined) delete process.env.IDEA_CDK_BIN;
    else process.env.IDEA_CDK_BIN = previousCdkBin;
  });

  const args = (...rest: string[]): string[] => [...rest, '--cluster-name', CLUSTER, '--aws-region', REGION];

  it('about prints the version', async () => {
    const deps = fakeDeps();
    assert.equal(await run(['about'], deps), 0);
    assert.match(deps.stdout[0] ?? '', /^ideactl \S+$/);
  });

  it('config show renders a table, yaml or raw values', async () => {
    for (const [format, matcher] of [
      ['table', /\| analytics\.enabled/],
      ['yaml', /^analytics:/m],
      ['raw', /^True$/m],
    ] as const) {
      const deps = fakeDeps({ tables: STATUS_TABLES });
      deps.scan = async () => ({ Items: [{ key: 'analytics.enabled', value: true, version: 1 }] });
      assert.equal(await run(args('config', 'show', '--format', format), deps), 0);
      assert.match(deps.stdout.join('\n'), matcher, `format ${format}`);
    }
  });

  it('config set writes each parsed entry', async () => {
    const deps = fakeDeps();
    assert.equal(
      await run(args('config', 'set', 'Key=global-settings.x,Type=int,Value=7', '--force'), deps),
      0,
    );
    assert.deepEqual(deps.writes, [{ op: 'setConfigEntry', payload: { key: 'global-settings.x', value: 7 } }]);
  });

  it('config delete deletes each prefix, trimmed', async () => {
    const deps = fakeDeps();
    assert.equal(await run(args('config', 'delete', ' analytics. ', 'alb.listener_rules.'), deps), 0);
    assert.deepEqual(deps.writes.map((write) => write.payload), ['analytics.', 'alb.listener_rules.']);
  });

  it('config diff prints the four-column table', async () => {
    const configDir = mkdtempSync(join(tmpdir(), 'ideactl-clidiff-'));
    mkdirSync(join(configDir, 'analytics'), { recursive: true });
    writeFileSync(
      join(configDir, 'idea.yml'),
      'modules:\n  - name: analytics\n    id: analytics\n    type: stack\n    config_files: [settings.yml]\n',
    );
    writeFileSync(join(configDir, 'analytics', 'settings.yml'), 'enabled: true\n');
    const deps = fakeDeps();
    deps.scan = async () => ({ Items: [{ key: 'analytics.enabled', value: false }] });
    assert.equal(await run(args('config', 'diff', '--config-dir', configDir), deps), 0);
    assert.match(deps.stdout.join('\n'), /\| analytics\.enabled \| False\s+\| True\s+\| MODIFIED \|/);
    rmSync(configDir, { recursive: true, force: true });
  });

  it('list-modules and show-connection-info print', async () => {
    const deps = fakeDeps({ tables: STATUS_TABLES });
    assert.equal(await run(args('list-modules'), deps), 0);
    assert.ok(deps.stdout.join('\n').includes('cluster-manager'));
    const info = fakeDeps({ tables: STATUS_TABLES });
    assert.equal(await run(args('show-connection-info'), info), 0);
    assert.ok(info.stdout.some((line) => line.startsWith('Web Portal: https://')));
  });

  it('cdk synth spawns the synth argv for the named module', async () => {
    const deps = fakeDeps({ tables: STATUS_TABLES });
    assert.equal(await run(args('cdk', 'synth', 'analytics'), deps), 0);
    assert.deepEqual(deps.spawns[0]?.slice(0, 2), ['/opt/idea/lib/idea-cdk/node_modules/aws-cdk/bin/cdk', 'synth']);
    assert.deepEqual(deps.spawns[0]?.slice(-2), ['--output', 'cdk.out.analytics']);
  });

  it('deploy reaches the change-set guard', async () => {
    const deps = fakeDeps({ tables: STATUS_TABLES });
    assert.equal(await run(args('deploy', 'analytics', '--upgrade'), deps), 0);
    assert.ok(deps.spawns[0]?.includes('--method=prepare-change-set'));
  });
});

it('asBoolFlag maps commander strings the way deploy and upgrade pass them', () => {
  assert.equal(asBoolFlag(undefined, true), true);
  assert.equal(asBoolFlag('true', true), true);
  assert.equal(asBoolFlag('false', true), false);
  assert.equal(asBoolFlag('no', true), false);
  assert.equal(asBoolFlag(false, true), false);
});
