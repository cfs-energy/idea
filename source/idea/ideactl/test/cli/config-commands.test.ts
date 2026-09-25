/**
 * The `config` group: entry parsing, the DB scan with its regex filter, diff, export, and the two
 * values-file commands.
 */

import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, describe, it } from 'node:test';

import * as yaml from 'js-yaml';

import {
  buildTree,
  clusterBucketName,
  configDiff,
  configExport,
  configGenerate,
  configUpdate,
  parseSetEntries,
  pyStr,
  renderTable,
  scanSettings,
} from '../../src/cli/commands/config.ts';
import { compareUpgradeDrift } from '../../src/config/upgrade-drift.ts';
import { VALUES_FILE_S3_KEY, clusterRegionDir } from '../../src/cli/cdk-invoker.ts';
import { fakeDeps, moduleRow, withTempIdeaHome } from '../support/deploy-harness.ts';
import { requireCapture } from '../support/fixtures.ts';

const CLUSTER = 'sample-cluster';
const REGION = 'us-east-2';

const home = withTempIdeaHome();
after(() => home.restore());

describe('parseSetEntries', () => {
  it('parses each scalar type', () => {
    assert.deepEqual(
      parseSetEntries([
        'Key=global-settings.string_val,Type=string,Value=stringcontent',
        'Key=global-settings.int_val,Type=int,Value=12',
        'Key=global-settings.float_val,Type=decimal,Value=1.5',
        'Key=global-settings.bool_val,Type=boolean,Value=true',
      ]),
      [
        { key: 'global-settings.string_val', value: 'stringcontent' },
        { key: 'global-settings.int_val', value: 12 },
        { key: 'global-settings.float_val', value: 1.5 },
        { key: 'global-settings.bool_val', value: true },
      ],
    );
  });

  it('splits a list on the third comma onward, so the value keeps its own commas', () => {
    assert.deepEqual(parseSetEntries(['Key=my_config.string_list,Type=list<str>,Value=value1,value2']), [
      { key: 'my_config.string_list', value: ['value1', 'value2'] },
    ]);
    assert.deepEqual(parseSetEntries(['Key=k,Type=list<int>,Value=1,2']), [{ key: 'k', value: [1, 2] }]);
  });

  it('rejects a key holding a comma or a colon', () => {
    assert.throws(
      () => parseSetEntries(['Key=a:b,Type=str,Value=x']),
      /comma\(,\) and colon\(:\) are not allowed in key names/,
    );
  });

  it('rejects an unsupported type and a non-numeric numeric value', () => {
    assert.throws(() => parseSetEntries(['Key=k,Type=dict,Value=x']), /Type: dict not supported/);
    assert.throws(() => parseSetEntries(['Key=k,Type=int,Value=x']), /is not a valid int/);
    assert.throws(() => parseSetEntries(['Key=k,Type=list<int>,Value=1,x']), /is not a valid list<int>/);
  });

  it('requires each of Key, Type and Value', () => {
    assert.throws(() => parseSetEntries(['Key= ,Type=str,Value=x']), /\[0\] Key is required/);
    assert.throws(() => parseSetEntries(['Key=k,Type= ,Value=x']), /\[0\] Type is required/);
    assert.throws(() => parseSetEntries(['Key=k,Type=str,Value= ']), /\[0\] Value is required/);
  });
});

describe('pyStr and renderTable', () => {
  it('renders values the way the Python table did', () => {
    assert.equal(pyStr(undefined), '-');
    assert.equal(pyStr(null), '-');
    assert.equal(pyStr(true), 'True');
    assert.equal(pyStr(['a', 'b']), "['a', 'b']");
    assert.equal(pyStr(12), '12');
  });

  it('aligns columns left and pads to the widest cell', () => {
    const table = renderTable(['Key', 'Value'], [['a', 'longer']]);
    assert.deepEqual(table.split('\n'), [
      '+-----+--------+',
      '| Key | Value  |',
      '+-----+--------+',
      '| a   | longer |',
      '+-----+--------+',
    ]);
  });
});

describe('buildTree', () => {
  it('nests dotted keys and lets a deeper key replace a scalar', () => {
    assert.deepEqual(
      buildTree([
        { key: 'cluster.aws.region', value: REGION },
        { key: 'cluster.name', value: CLUSTER },
        { key: 'cluster.list', value: [] },
      ]),
      { cluster: { aws: { region: REGION }, list: [], name: CLUSTER } },
    );
  });

  it('writes a missing value as null, not as undefined', () => {
    assert.deepEqual(buildTree([{ key: 'a.b' }]), { a: { b: null } });
  });
});

describe('scanSettings', () => {
  const tables = {
    [`${CLUSTER}.cluster-settings`]: [
      { key: 'cluster.cluster_s3_bucket', value: 'sample-bucket', version: 3 },
      { key: 'analytics.enabled', value: true, version: 1 },
      { key: 'global-settings.x', value: 1, version: 1 },
    ],
  };

  it('sorts by key and keeps the version', async () => {
    const rows = await scanSettings(fakeDeps({ tables }), CLUSTER);
    assert.deepEqual(
      rows.map((row) => [row.key, row.version]),
      [
        ['analytics.enabled', 1],
        ['cluster.cluster_s3_bucket', 3],
        ['global-settings.x', 1],
      ],
    );
  });

  it('filters with a start-anchored regex, as re.match does', async () => {
    const rows = await scanSettings(fakeDeps({ tables }), CLUSTER, 'cluster\\.');
    assert.deepEqual(rows.map((row) => row.key), ['cluster.cluster_s3_bucket']);
    // `re.match` anchors at the start, so a mid-key match returns nothing.
    assert.deepEqual(await scanSettings(fakeDeps({ tables }), CLUSTER, 'enabled'), []);
  });

  it('rejects an invalid regex rather than scanning', async () => {
    await assert.rejects(() => scanSettings(fakeDeps({ tables }), CLUSTER, '('), /invalid search regex/);
  });
});

describe('clusterBucketName', () => {
  it('prefers the recorded bucket', async () => {
    const deps = fakeDeps({
      tables: { [`${CLUSTER}.cluster-settings`]: [{ key: 'cluster.cluster_s3_bucket', value: 'recorded-bucket' }] },
    });
    assert.equal(await clusterBucketName(deps, CLUSTER, REGION), 'recorded-bucket');
  });

  it('falls back to the conventional name when the setting is absent', async () => {
    const deps = fakeDeps({ tables: { [`${CLUSTER}.cluster-settings`]: [] } });
    assert.equal(await clusterBucketName(deps, CLUSTER, REGION), `${CLUSTER}-cluster-${REGION}-123456789012`);
  });
});

describe('config export', () => {
  it('writes one settings.yml per module plus idea.yml', async () => {
    const exportDir = mkdtempSync(join(tmpdir(), 'ideactl-export-'));
    rmSync(exportDir, { recursive: true, force: true });
    const deps = fakeDeps({
      tables: {
        [`${CLUSTER}.cluster-settings`]: [
          { key: 'analytics.enabled', value: true },
          { key: 'cluster.cluster_name', value: CLUSTER },
        ],
        [`${CLUSTER}.modules`]: [
          moduleRow('analytics', 'analytics', 'stack'),
          moduleRow('cluster', 'cluster', 'stack'),
        ],
      },
    });
    await configExport(deps, { clusterName: CLUSTER, awsRegion: REGION, exportDir });
    assert.deepEqual(yaml.load(readFileSync(join(exportDir, 'analytics', 'settings.yml'), 'utf-8')), { enabled: true });
    assert.deepEqual(yaml.load(readFileSync(join(exportDir, 'idea.yml'), 'utf-8')), {
      modules: [
        { name: 'analytics', id: 'analytics', type: 'stack', config_files: ['settings.yml'] },
        { name: 'cluster', id: 'cluster', type: 'stack', config_files: ['settings.yml'] },
      ],
    });
    rmSync(exportDir, { recursive: true, force: true });
  });

  it('refuses a non-empty export directory', async () => {
    const exportDir = mkdtempSync(join(tmpdir(), 'ideactl-export-'));
    writeFileSync(join(exportDir, 'idea.yml'), 'modules: []\n');
    await assert.rejects(
      () => configExport(fakeDeps(), { clusterName: CLUSTER, awsRegion: REGION, exportDir }),
      /already exists and can cause merge conflicts/,
    );
    rmSync(exportDir, { recursive: true, force: true });
  });
});

describe('config diff', () => {
  it('classifies MODIFIED, DELETED and ADDED', async () => {
    const configDir = mkdtempSync(join(tmpdir(), 'ideactl-diff-'));
    mkdirSync(join(configDir, 'analytics'), { recursive: true });
    writeFileSync(
      join(configDir, 'idea.yml'),
      'modules:\n  - name: analytics\n    id: analytics\n    type: stack\n    config_files: [settings.yml]\n',
    );
    writeFileSync(join(configDir, 'analytics', 'settings.yml'), 'enabled: true\nnew_key: added\n');

    const deps = fakeDeps({
      tables: {
        [`${CLUSTER}.cluster-settings`]: [
          { key: 'analytics.enabled', value: false },
          { key: 'analytics.gone', value: 'x' },
        ],
      },
    });
    assert.deepEqual(await configDiff(deps, { clusterName: CLUSTER, awsRegion: REGION, configDir }), [
      ['analytics.enabled', 'False', 'True', 'MODIFIED'],
      ['analytics.gone', 'x', 'n/a', 'DELETED'],
      ['analytics.new_key', 'n/a', 'added', 'ADDED'],
    ]);
    rmSync(configDir, { recursive: true, force: true });
  });
});

describe('save-values and download-values', () => {
  it('round-trips values.yml through the cluster bucket key', async () => {
    const valuesFile = valuesFileFor('save');
    writeFileSync(valuesFile, 'cluster_name: sample-cluster\naws_region: us-east-2\n');
    const deps = fakeDeps({
      tables: { [`${CLUSTER}.cluster-settings`]: [{ key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' }] },
      getObject: () => 'cluster_name: sample-cluster\naws_region: us-east-2\n',
    });
    const { run } = await import('../../src/cli/main.ts');
    assert.equal(
      await run(
        ['config', 'save-values', '--cluster-name', CLUSTER, '--aws-region', REGION, '--values-file', valuesFile],
        deps,
      ),
      0,
    );
    assert.ok(deps.puts.has(`sample-bucket/${VALUES_FILE_S3_KEY}`));

    const downloadDir = mkdtempSync(join(tmpdir(), 'ideactl-values-'));
    assert.equal(
      await run(
        ['config', 'download-values', '--cluster-name', CLUSTER, '--aws-region', REGION, '--values-dir', downloadDir],
        deps,
      ),
      0,
    );
    assert.match(readFileSync(join(downloadDir, 'values.yml'), 'utf-8'), /cluster_name: sample-cluster/);
    rmSync(downloadDir, { recursive: true, force: true });
  });

  it('exits 1 and names both locations when the bucket has no copy', async () => {
    const deps = fakeDeps({
      tables: { [`${CLUSTER}.cluster-settings`]: [{ key: 'cluster.cluster_s3_bucket', value: 'sample-bucket' }] },
    });
    const { run } = await import('../../src/cli/main.ts');
    assert.equal(
      await run(['config', 'download-values', '--cluster-name', CLUSTER, '--aws-region', REGION], deps),
      1,
    );
    assert.ok(deps.stderr.some((line) => line.includes('could not be downloaded from s3://sample-bucket/')));
  });
});

function valuesFileFor(name: string): string {
  const dir = mkdtempSync(join(tmpdir(), `ideactl-${name}-`));
  return join(dir, 'values.yml');
}

// -------------------------------------------------------------------------------------------
// generate and update, driven by the dev cluster fixture when it is present
// -------------------------------------------------------------------------------------------

const DEV27_VALUES = new URL('../../tools/parity/fixtures/idea-dev27/values.yml', import.meta.url).pathname;
requireCapture(
  [DEV27_VALUES],
  "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27",
);

describe('config generate', () => {
  it("starts the interactive installer when --values-file is omitted", async () => {
    const questions: string[] = [];
    const stopAfterAccountQuestions = new Error("stop after account questions");

    await assert.rejects(
      () =>
        configGenerate(fakeDeps(), {
          // Record the real question sequence while accepting each declared default.
          installerDriver: {
            ask: async (question) => {
              questions.push(question.name);
              return question.defaultValue;
            },
            report: (message) => assert.fail(message),
          },
          installerIdentity: async () => {
            throw stopAfterAccountQuestions;
          },
        }),
      (error: Error) => error === stopAfterAccountQuestions,
    );
    assert.deepEqual(questions, ["aws_profile", "aws_partition", "aws_region"]);
  });

  it('rejects a values file that is not there', async () => {
    await assert.rejects(
      () => configGenerate(fakeDeps(), { valuesFile: '/nonexistent/values.yml' }),
      /file not found/,
    );
  });

  it('writes values.yml and a config tree from the dev cluster fixture', async () => {
    const configDir = mkdtempSync(join(tmpdir(), 'ideactl-generate-'));
    const deps = fakeDeps();
    // The captured file is an existing cluster's own values, which predates the container
    // control plane, so this is the regenerate path rather than a new install.
    const values = await configGenerate(deps, { valuesFile: DEV27_VALUES, configDir, force: true, regenerate: true });
    assert.equal(typeof values['cluster_name'], 'string');
    assert.ok(existsSync(join(configDir, 'values.yml')));
    assert.ok(existsSync(join(configDir, 'config', 'idea.yml')));
    assert.ok(deps.stdout.some((line) => line.startsWith('saving values to: ')));
    rmSync(configDir, { recursive: true, force: true });
  });
});

describe('config update', () => {
  /** A minimal but structurally real config tree: `update`'s sanity checks read these keys. */
  function writeConfigTree(clusterName: string, awsRegion: string): string {
    const root = mkdtempSync(join(tmpdir(), 'ideactl-update-'));
    const configDir = join(root, 'config');
    mkdirSync(join(configDir, 'cluster'), { recursive: true });
    mkdirSync(join(configDir, 'global-settings'), { recursive: true });
    writeFileSync(
      join(configDir, 'idea.yml'),
      'modules:\n' +
        '  - name: global-settings\n    id: global-settings\n    type: config\n    config_files: [settings.yml]\n' +
        '  - name: cluster\n    id: cluster\n    type: stack\n    config_files: [settings.yml]\n',
    );
    writeFileSync(
      join(configDir, 'global-settings', 'settings.yml'),
      `module_sets:\n  default:\n    cluster:\n      module_id: cluster\n`,
    );
    writeFileSync(
      join(configDir, 'cluster', 'settings.yml'),
      `cluster_name: ${clusterName}\naws:\n  region: ${awsRegion}\ndynamodb:\n  kms_key_id: ~\n`,
    );
    return root;
  }

  it('syncs modules then settings, honouring --overwrite', async () => {
    const root = writeConfigTree(CLUSTER, REGION);
    const deps = fakeDeps();
    await configUpdate(deps, {
      clusterName: CLUSTER,
      awsRegion: REGION,
      moduleSet: 'default',
      configDir: root,
      force: true,
      overwrite: true,
    });
    assert.deepEqual(deps.writes.map((write) => write.op), ['syncModulesInDb', 'syncClusterSettingsInDb']);
    assert.equal((deps.writes[1]?.payload as { overwrite: boolean }).overwrite, true);
    rmSync(root, { recursive: true, force: true });
  });

  it('operator file overwrite retains provenance for the next drift approval', async () => {
    const root = writeConfigTree(CLUSTER, REGION);
    const deps = fakeDeps();
    let source: string | undefined;
    const writer = await deps.configWriter({clusterName: CLUSTER, awsRegion: REGION});
    writer.syncClusterSettingsInDb = async (_entries, _overwrite, suppliedSource) => { source = suppliedSource; };
    deps.configWriter = async () => writer;
    try {
      await configUpdate(deps, {clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default', configDir: root, force: true, overwrite: true});
      assert.equal(source, 'cli');
      const key = 'global-settings.gpu_settings.fail_on_missing_driver';
      const report = compareUpgradeDrift({current: [{key, value: false, source}], generated: [{key, value: true}], replaceGlobalSettings: true});
      assert.deepEqual(report.changedRowsDifferingFromGenerated, [key]);
    } finally { rmSync(root, {recursive: true, force: true}); }
  });

  it('refuses a config tree belonging to another cluster or region', async () => {
    const root = writeConfigTree('other-cluster', REGION);
    await assert.rejects(
      () => configUpdate(fakeDeps(), { clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default', configDir: root, force: true }),
      /does not match the given cluster name: sample-cluster/,
    );
    const root2 = writeConfigTree(CLUSTER, 'eu-west-1');
    await assert.rejects(
      () => configUpdate(fakeDeps(), { clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default', configDir: root2, force: true }),
      /does not match the given aws region: us-east-2/,
    );
    rmSync(root, { recursive: true, force: true });
    rmSync(root2, { recursive: true, force: true });
  });

  it('writes nothing when the operator picks Exit at the confirmation', async () => {
    const root = writeConfigTree(CLUSTER, REGION);
    const deps = fakeDeps({ answers: ['Exit'] });
    await assert.rejects(
      () => configUpdate(deps, { clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default', configDir: root }),
      (error: Error) => error.name === 'ExitWithCode',
    );
    assert.deepEqual(deps.writes, []);
    rmSync(root, { recursive: true, force: true });
  });

  it('re-reads the tree when the operator picks Reload Changes', async () => {
    const root = writeConfigTree(CLUSTER, REGION);
    const deps = fakeDeps({ answers: ['Reload Changes', 'Yes'] });
    await configUpdate(deps, { clusterName: CLUSTER, awsRegion: REGION, moduleSet: 'default', configDir: root });
    assert.equal(deps.stdout.filter((line) => line.startsWith('reading cluster settings from')).length, 2);
    rmSync(root, { recursive: true, force: true });
  });

  it('reports a missing config directory rather than scanning the default one', async () => {
    const deps = fakeDeps();
    await assert.rejects(
      () =>
        configUpdate(deps, {
          clusterName: CLUSTER,
          awsRegion: REGION,
          moduleSet: 'default',
          configDir: '/nonexistent',
          force: true,
        }),
      (error: Error) => error.name === 'ExitWithCode',
    );
    assert.ok(deps.stderr.some((line) => line.includes('/nonexistent/config does not exist')));
  });
});

describe('cluster directory layout', () => {
  it('is rooted at IDEA_USER_HOME so nothing here touches a real cluster directory', () => {
    assert.ok(clusterRegionDir(CLUSTER, REGION).startsWith(home.home));
  });
});
