import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import {
  ClusterConfig,
  ClusterConfigError,
  ConfigTypeError,
  GeneralException,
  isNullValue,
  type ModuleInfo,
  type ScanPage,
} from '../../src/config/cluster-config.ts';
import { requireFixtures } from '../support/fixtures.ts';

const FIXTURES = fileURLToPath(new URL('../../tools/parity/fixtures/idea-dev27/raw/', import.meta.url));
const SCAN = `${FIXTURES}cluster-settings.scan.json`;
const MODULES = `${FIXTURES}modules.scan.json`;
requireFixtures([SCAN, MODULES], "node tools/parity/capture.ts --from-raw tools/parity/fixtures/idea-dev27/raw --out tools/parity/fixtures/idea-dev27");

function dev27(): ClusterConfig {
  return ClusterConfig.fromFile(readFileSync(SCAN, 'utf-8'), readFileSync(MODULES, 'utf-8'));
}

describe('ClusterConfig (synthetic)', () => {
  const config = ClusterConfig.fromFile(
    JSON.stringify({
      Items: [
        { key: { S: 'global-settings.module_sets.default.virtual-desktop-controller.module_id' }, value: { S: 'vdc' } },
        { key: { S: 'global-settings.module_sets.default.cluster.module_id' }, value: { S: 'cluster' } },
        { key: { S: 'global-settings.module_sets.custom.virtual-desktop-controller.module_id' }, value: { S: 'vdc2' } },
        { key: { S: 'vdc.x' }, value: { S: 'hello' } },
        { key: { S: 'vdc.count' }, value: { N: '7' } },
        { key: { S: 'vdc.ratio' }, value: { N: '1.5' } },
        { key: { S: 'vdc.flag' }, value: { BOOL: true } },
        { key: { S: 'vdc.provided' }, value: { S: 'true' } },
        { key: { S: 'vdc.nothing' }, value: { NULL: true } },
        { key: { S: 'vdc.blank' }, value: { S: '   ' } },
        { key: { S: 'vdc.zero' }, value: { N: '0' } },
        { key: { S: 'vdc.off' }, value: { BOOL: false } },
        { key: { S: 'vdc.empty_list' }, value: { L: [] } },
        { key: { S: 'vdc.some_list' }, value: { L: [{ S: 'a' }, { S: 'b' }] } },
        { key: { S: 'vdc.quoted_list' }, value: { L: [{ S: "it's" }, { S: 'say "hi"' }] } },
        { key: { S: 'vdc.bool_list' }, value: { L: [{ BOOL: true }, { NULL: true }] } },
        { key: { S: 'vdc.number_list' }, value: { L: [{ N: '1' }, { N: '2' }] } },
        { key: { S: 'vdc.float_list' }, value: { L: [{ N: '1.5' }] } },
        { key: { S: 'vdc.nested' }, value: { M: { a: { S: 'x' }, n: { N: '2' } } } },
        { key: { S: 'vdc.strings_map' }, value: { M: { a: { S: 'x' }, b: { S: 'y' } } } },
        { key: { S: 'vdc.numeric_map' }, value: { M: { '0': { S: 'a' }, '1': { S: 'b' } } } },
        {
          key: { S: 'vdc.wide_map' },
          value: { M: { '0': { S: 'z' }, '1': { S: 'y' }, '10': { S: 'w' }, '2': { S: 'x' } } },
        },
        { key: { S: 'vdc.mixed_map' }, value: { M: { '0': { S: 'a' }, x: { S: 'b' } } } },
        { key: { S: 'queue-profile.compute.enabled' }, value: { BOOL: true } },
      ],
    }),
    JSON.stringify({
      Items: [
        { module_id: { S: 'vdc' }, name: { S: 'virtual-desktop-controller' }, type: { S: 'app' } },
        { module_id: { S: 'cluster' }, name: { S: 'cluster' }, type: { S: 'stack' } },
      ],
    }),
  );

  it('maps a module name prefix to its module id', () => {
    assert.equal(config.getRealKey('virtual-desktop-controller.x'), 'vdc.x');
  });

  it('passes global-settings through untouched', () => {
    assert.equal(
      config.getRealKey('global-settings.module_sets.default.virtual-desktop-controller.module_id'),
      'global-settings.module_sets.default.virtual-desktop-controller.module_id',
    );
  });

  it('falls back to the module name for a custom prefix with no module_sets row', () => {
    assert.equal(config.getRealKey('queue-profile.compute.enabled'), 'queue-profile.compute.enabled');
    assert.equal(config.getBool('queue-profile.compute.enabled'), true);
  });

  it('honours an explicit module id and the module set', () => {
    assert.equal(config.getRealKey('virtual-desktop-controller.x', 'vdc9'), 'vdc9.x');
    const custom = ClusterConfig.fromFile(
      JSON.stringify({
        Items: [
          { key: { S: 'global-settings.module_sets.custom.virtual-desktop-controller.module_id' }, value: { S: 'vdc2' } },
        ],
      }),
      undefined,
      { moduleSet: 'custom' },
    );
    assert.equal(custom.getRealKey('virtual-desktop-controller.x'), 'vdc2.x');
  });

  it('maps the current module by its own name once set', () => {
    const own = ClusterConfig.fromFile(
      JSON.stringify({ Items: [] }),
      JSON.stringify({
        Items: [{ module_id: { S: 'vdc' }, name: { S: 'virtual-desktop-controller' }, type: { S: 'app' } }],
      }),
      { moduleId: 'vdc' },
    );
    assert.equal(own.getRealKey('virtual-desktop-controller.x'), 'vdc.x');
  });

  it('reproduces Python\'s trailing dot on a single segment key', () => {
    assert.equal(config.getRealKey('virtual-desktop-controller'), 'vdc.');
  });

  it('types values the way boto3 + check_and_convert_decimal_value do', () => {
    assert.equal(config.getString('virtual-desktop-controller.x'), 'hello');
    assert.equal(config.getInt('virtual-desktop-controller.count'), 7);
    assert.equal(config.getFloat('virtual-desktop-controller.ratio'), 1.5);
    assert.equal(config.get('virtual-desktop-controller.count'), 7);
    assert.equal(config.getBool('virtual-desktop-controller.flag'), true);
    assert.deepEqual(config.get('virtual-desktop-controller.nested'), { a: 'x', n: 2 });
  });

  it('coerces the string "true" through get_bool, as pyhocon does', () => {
    assert.equal(config.getBool('virtual-desktop-controller.provided'), true);
  });

  it('stringifies a bool lowercased', () => {
    assert.equal(config.getString('virtual-desktop-controller.flag'), 'true');
    assert.equal(config.getString('virtual-desktop-controller.off'), 'false');
  });

  it('keeps 0 and false rather than treating them as empty', () => {
    assert.equal(config.getInt('virtual-desktop-controller.zero', 99), 0);
    assert.equal(config.getBool('virtual-desktop-controller.off', true), false);
  });

  it('returns the default for NULL, for a whitespace string and for a missing key', () => {
    assert.equal(config.getString('virtual-desktop-controller.nothing', 'fallback'), 'fallback');
    assert.equal(config.getString('virtual-desktop-controller.blank', 'fallback'), 'fallback');
    assert.equal(config.getString('virtual-desktop-controller.absent', 'fallback'), 'fallback');
    assert.equal(config.getList('virtual-desktop-controller.nothing', ['d']).join(), 'd');
  });

  it('keeps a real empty list', () => {
    assert.deepEqual(config.getList('virtual-desktop-controller.empty_list', ['d']), []);
    assert.deepEqual(config.get('virtual-desktop-controller.empty_list', ['d']), []);
    assert.deepEqual(config.getList('virtual-desktop-controller.some_list'), ['a', 'b']);
  });

  it('is_null_value: only an empty list survives', () => {
    assert.equal(isNullValue([]), false);
    assert.equal(isNullValue(''), true);
    assert.equal(isNullValue('  '), true);
    assert.equal(isNullValue({}), true);
    assert.equal(isNullValue(null), true);
    assert.equal(isNullValue(0), false);
    assert.equal(isNullValue(false), false);
  });

  it('returns the default rather than raising when descending through a scalar', () => {
    assert.equal(config.getString('virtual-desktop-controller.x.deeper', 'fallback'), 'fallback');
  });

  it('raises on a required key that is absent, and on a bad type', () => {
    assert.throws(() => config.getString('virtual-desktop-controller.absent', undefined, { required: true }));
    assert.throws(() => config.getBool('virtual-desktop-controller.count'));
    assert.throws(() => config.getList('virtual-desktop-controller.x'));
  });

  it('applies entries in sorted key order, so a nested key wins over a scalar prefix', () => {
    const collide = ClusterConfig.fromFile(
      JSON.stringify({
        Items: [
          { key: { S: 'cluster.a.b' }, value: { S: 'deep' } },
          { key: { S: 'cluster.a' }, value: { S: 'scalar' } },
        ],
      }),
    );
    assert.equal(collide.getString('cluster.a.b'), 'deep');
    // the scalar at the prefix is gone: pyhocon replaced it with the tree
    assert.deepEqual(collide.get('cluster.a'), { b: 'deep' });
  });

  // The fixed interface exposes `moduleId(moduleName)` and generic `get<T>(key)`.
  describe('the fixed public interface', () => {
    it('moduleId(moduleName) is a method, and resolves through the module set', () => {
      assert.equal(typeof config.moduleId, 'function');
      assert.equal(config.moduleId('virtual-desktop-controller'), 'vdc');
      assert.equal(config.moduleId('virtual-desktop-controller'), 'vdc');
      assert.equal(config.isModuleEnabled('virtual-desktop-controller'), true);
      assert.equal(config.isModuleEnabled('scheduler'), false);
      assert.throws(() => config.moduleId('scheduler'));
    });

    it('the module this process IS lives on currentModuleId, not on moduleId', () => {
      const own = ClusterConfig.fromFile(
        JSON.stringify({ Items: [] }),
        JSON.stringify({
          Items: [{ module_id: { S: 'vdc' }, name: { S: 'virtual-desktop-controller' }, type: { S: 'app' } }],
        }),
      );
      assert.equal(own.currentModuleId, undefined);
      own.setModuleId('vdc');
      assert.equal(own.currentModuleId, 'vdc');
      assert.equal(own.moduleInfo?.name, 'virtual-desktop-controller');
      assert.throws(() => own.setModuleId('nope'), GeneralException);
    });

    it('get<T>(key) is generic with a single argument', () => {
      // the assignments are the assertion: on a non-generic `get(key): unknown` they do not compile
      const text: string = config.get<string>('virtual-desktop-controller.x');
      const count: number = config.get<number>('virtual-desktop-controller.count');
      const list: string[] = config.get<string[]>('virtual-desktop-controller.some_list');
      assert.equal(text, 'hello');
      assert.equal(count, 7);
      assert.deepEqual(list, ['a', 'b']);
    });
  });

  // getConfig rejects stored non-mapping values.
  describe('getConfig', () => {
    it('returns the subtree', () => {
      assert.deepEqual(config.getConfig('virtual-desktop-controller.nested'), { a: 'x', n: 2 });
    });

    it('raises for a stored scalar rather than returning the default', () => {
      assert.throws(
        () => config.getConfig('virtual-desktop-controller.x', { fallback: true }),
        (error: Error) => {
          assert.ok(error instanceof ConfigTypeError);
          assert.equal(error.message, "vdc.x has type 'str' rather than 'config'");
          return true;
        },
      );
    });

    it('raises for a stored list too', () => {
      assert.throws(
        () => config.getConfig('virtual-desktop-controller.some_list', { fallback: true }),
        (error: Error) => {
          assert.equal(error.message, "vdc.some_list has type 'list' rather than 'config'");
          return true;
        },
      );
    });

    it('falls back only for a missing, NULL or empty value', () => {
      assert.deepEqual(config.getConfig('virtual-desktop-controller.absent', { fallback: true }), { fallback: true });
      assert.deepEqual(config.getConfig('virtual-desktop-controller.nothing', { fallback: true }), { fallback: true });
    });
  });

  // getList accepts a ConfigTree with numeric keys.
  describe('getList of a numeric-key map', () => {
    it('returns the values in sorted key order', () => {
      assert.deepEqual(config.getList('virtual-desktop-controller.numeric_map'), ['a', 'b']);
    });

    it('sorts the keys as strings, the way Python sorted() does', () => {
      // keys '0', '1', '10', '2' - not numeric order
      assert.deepEqual(config.getList('virtual-desktop-controller.wide_map'), ['z', 'y', 'w', 'x']);
    });

    it('raises when any key is not numeric', () => {
      assert.throws(
        () => config.getList('virtual-desktop-controller.mixed_map'),
        (error: Error) => {
          assert.ok(error instanceof ConfigTypeError);
          assert.equal(error.message, 'vdc.mixed_map does not translate to a list');
          return true;
        },
      );
    });
  });

  // getString formats containers with Python str(value) rules.
  describe('getString of a container', () => {
    it('reproduces Python str() for a list', () => {
      assert.equal(config.getString('virtual-desktop-controller.some_list'), "['a', 'b']");
    });

    it('reproduces Python str() for an empty list, which is not the default', () => {
      assert.equal(config.getString('virtual-desktop-controller.empty_list', 'fallback'), '[]');
    });

    it('reproduces Python str() for a map', () => {
      // every dict in the tree is a pyhocon ConfigTree, and python 3.12+ reprs an OrderedDict
      // subclass as ClassName({...})
      assert.equal(config.getString('virtual-desktop-controller.strings_map'), "ConfigTree({'a': 'x', 'b': 'y'})");
      assert.equal(config.getString('virtual-desktop-controller.numeric_map'), "ConfigTree({'0': 'a', '1': 'b'})");
    });

    it('quotes the way Python repr does', () => {
      assert.equal(config.getString('virtual-desktop-controller.quoted_list'), '["it\'s", \'say "hi"\']');
    });

    it('reprs bools and None inside a list, but lowercases a top-level bool', () => {
      assert.equal(config.getString('virtual-desktop-controller.bool_list'), '[True, None]');
      assert.equal(config.getString('virtual-desktop-controller.flag'), 'true');
    });
  });

  // Numeric top-level lists use checkAndConvertDecimalValue.
  describe('the check_and_convert_decimal_value quirk', () => {
    it('appends the whole original list after every converted element', () => {
      assert.deepEqual(config.get('virtual-desktop-controller.number_list'), [1, [1, 2], 2, [1, 2]]);
      assert.deepEqual(config.getList('virtual-desktop-controller.number_list'), [1, [1, 2], 2, [1, 2]]);
      assert.deepEqual(config.getList('virtual-desktop-controller.float_list'), [1.5, [1.5]]);
    });

    it('leaves a list that does not start with a number alone', () => {
      assert.deepEqual(config.getList('virtual-desktop-controller.some_list'), ['a', 'b']);
      assert.deepEqual(config.getList('virtual-desktop-controller.empty_list'), []);
      assert.deepEqual(config.getList('virtual-desktop-controller.bool_list'), [true, null]);
    });
  });

  // modules joins module metadata.
  it('joins title and deployment_priority onto every module row', () => {
    const modules = config.modules();
    assert.deepEqual(
      modules.map((module) => [module.module_id, module.title, module.deployment_priority]),
      [
        ['vdc', 'eVDI', 6],
        ['cluster', 'Cluster', 2],
      ],
    );
    // the table row is preserved alongside the joined fields
    assert.equal(modules[0]?.type, 'app');
    // ... and the raw row lookup is not enriched, exactly like get_module_info
    assert.equal(config.moduleInfoById('vdc')?.title, undefined);
  });

  it('raises for a module row whose name has no metadata', () => {
    const unknown = ClusterConfig.fromFile(
      JSON.stringify({ Items: [] }),
      JSON.stringify({ Items: [{ module_id: { S: 'x' }, name: { S: 'not-a-module' }, type: { S: 'app' } }] }),
    );
    assert.throws(() => unknown.modules(), GeneralException);
  });

  it('endpoint helpers raise a cluster_config_error when the dns name is missing', () => {
    assert.throws(() => config.getClusterExternalEndpoint(), ClusterConfigError);
    assert.throws(() => config.getClusterInternalEndpoint(), ClusterConfigError);
  });

  it('builds the endpoints from the load balancer dns names', () => {
    const endpoints = ClusterConfig.fromFile(
      JSON.stringify({
        Items: [
          { key: { S: 'global-settings.module_sets.default.cluster.module_id' }, value: { S: 'cluster' } },
          {
            key: { S: 'cluster.load_balancers.external_alb.load_balancer_dns_name' },
            value: { S: 'external.example.invalid' },
          },
          {
            key: { S: 'cluster.load_balancers.internal_alb.certificates.custom_dns_name' },
            value: { S: 'internal.example.invalid' },
          },
        ],
      }),
    );
    assert.equal(endpoints.getClusterExternalEndpoint(), 'https://external.example.invalid');
    assert.equal(endpoints.getClusterInternalEndpoint(), 'https://internal.example.invalid');
  });
});

// The DynamoDB loader uses the supplied scanner.
describe('ClusterConfig.fromDynamoDb', () => {
  interface ScanCall {
    TableName: string;
    ExclusiveStartKey?: Record<string, unknown>;
  }

  const settingsPage1: ScanPage = {
    Items: [
      { key: 'global-settings.module_sets.default.virtual-desktop-controller.module_id', value: 'vdc', version: 3 },
      { key: 'vdc.count', value: 7 },
      { key: 'vdc.numbers', value: [1, 2] },
    ],
    LastEvaluatedKey: { key: 'vdc.numbers' },
  };
  const settingsPage2: ScanPage = {
    Items: [
      { key: 'vdc.late', value: 'from page two' },
      { key: 'vdc.nothing', value: null },
      { key: 'vdc.enabled', value: true },
    ],
  };
  const modulesPage: ScanPage = {
    Items: [
      { module_id: 'vdc', name: 'virtual-desktop-controller', type: 'app', status: 'deployed', stack_name: null },
      { module_id: 'cluster', name: 'cluster', type: 'stack', status: 'deployed' },
    ],
  };

  function fakeScanner(): { calls: ScanCall[]; scan: (input: ScanCall) => Promise<ScanPage> } {
    const calls: ScanCall[] = [];
    const pages = new Map<string, ScanPage[]>([
      ['sample-cluster.cluster-settings', [settingsPage1, settingsPage2]],
      ['sample-cluster.modules', [modulesPage]],
    ]);
    return {
      calls,
      scan: (input: ScanCall) => {
        calls.push(input);
        const remaining = pages.get(input.TableName);
        if (remaining === undefined) throw new Error(`unexpected table: ${input.TableName}`);
        const page = remaining.shift();
        if (page === undefined) throw new Error(`too many scans of ${input.TableName}`);
        return Promise.resolve(page);
      },
    };
  }

  it('scans both tables, follows LastEvaluatedKey, and loads the modules', async () => {
    const fake = fakeScanner();
    const config = await ClusterConfig.fromDynamoDb('sample-cluster', 'us-east-2', { scan: fake.scan });

    assert.deepEqual(fake.calls, [
      { TableName: 'sample-cluster.cluster-settings', ExclusiveStartKey: undefined },
      { TableName: 'sample-cluster.cluster-settings', ExclusiveStartKey: { key: 'vdc.numbers' } },
      { TableName: 'sample-cluster.modules', ExclusiveStartKey: undefined },
    ]);

    // rows from the second page are in the tree, so pagination is not silently dropping them
    assert.equal(config.getString('virtual-desktop-controller.late'), 'from page two');
    // document-client values keep their types, and NULL still falls back
    assert.equal(config.getInt('virtual-desktop-controller.count'), 7);
    assert.equal(config.getBool('virtual-desktop-controller.enabled'), true);
    assert.equal(config.getString('virtual-desktop-controller.nothing', 'fallback'), 'fallback');
    // the module-name -> module-id rewrite came from the scanned rows
    assert.equal(config.getRealKey('virtual-desktop-controller.late'), 'vdc.late');
    // the decimal quirk applies to this read path as well
    assert.deepEqual(config.getList('virtual-desktop-controller.numbers'), [1, [1, 2], 2, [1, 2]]);

    assert.deepEqual(
      config.modules().map((module: ModuleInfo) => [module.module_id, module.title, module.deployment_priority]),
      [
        ['vdc', 'eVDI', 6],
        ['cluster', 'Cluster', 2],
      ],
    );
    assert.equal(config.moduleInfoById('vdc')?.status, 'deployed');
  });

  it('takes the module set and the current module id like fromFile', async () => {
    const fake = fakeScanner();
    const config = await ClusterConfig.fromDynamoDb('sample-cluster', 'us-east-2', {
      scan: fake.scan,
      moduleId: 'vdc',
    });
    assert.equal(config.currentModuleId, 'vdc');
    assert.equal(config.moduleSet, 'default');
  });
});

/** Dotted paths of every leaf in a loaded config tree, including null and empty-list leaves. */
function dottedKeys(node: unknown, prefix = ''): string[] {
  if (node === null || typeof node !== 'object' || Array.isArray(node)) {
    return prefix === '' ? [] : [prefix];
  }
  const entries = Object.entries(node as Record<string, unknown>);
  if (entries.length === 0) return prefix === '' ? [] : [prefix];
  return entries.flatMap(([key, value]) => dottedKeys(value, prefix === '' ? key : `${prefix}.${key}`));
}

describe('ClusterConfig against the dev27 cluster-settings scan', () => {
  it('getRealKey(virtual-desktop-controller.x) === vdc.x', () => {
    assert.equal(dev27().getRealKey('virtual-desktop-controller.x'), 'vdc.x');
  });

  it('getString(cluster.cluster_name) === idea-dev27', () => {
    assert.equal(dev27().getString('cluster.cluster_name'), 'idea-dev27');
  });

  it('a NULL row returns the default, a real [] stays []', () => {
    const config = dev27();
    // vdc.dcv_session.network.private_subnets is stored NULL
    assert.equal(config.get('virtual-desktop-controller.dcv_session.network.private_subnets'), undefined);
    assert.deepEqual(
      config.getList('virtual-desktop-controller.dcv_session.network.private_subnets', ['fallback']),
      ['fallback'],
    );
    assert.equal(config.getString('cluster.secretsmanager.kms_key_id', 'fallback'), 'fallback');
    // global-settings.custom_tags is stored as an empty L
    assert.deepEqual(config.getList('global-settings.custom_tags', ['fallback']), []);
  });

  it('reads every row without an unmarshalling error, and keeps the row count', () => {
    const scan = JSON.parse(readFileSync(SCAN, 'utf-8')) as {
      Items: Array<{ key: { S: string }; value: Record<string, unknown> }>;
    };
    assert.equal(scan.Items.length, 858);
    const config = dev27();
    const tree = config.get('.') as Record<string, unknown>;
    const loaded = new Set(dottedKeys(tree));
    let kept = 0;
    for (const item of scan.Items) {
      const key = item.key.S;
      const inTree = loaded.has(key) || [...loaded].some((leaf) => leaf.startsWith(`${key}.`));
      assert.ok(inTree, `scan row ${key} is missing from the loaded tree`);
      kept += 1;
    }
    assert.equal(kept, scan.Items.length);
    assert.equal(config.getString('cluster.aws.region'), 'us-east-2');
    assert.equal(config.getInt('virtual-desktop-controller.controller.autoscaling.volume_size'), 200);
    assert.equal(config.getBool('bastion-host.public'), true);
    assert.deepEqual(config.getList('cluster-manager.endpoints.external.path_patterns'), ['/cluster-manager/*']);
  });

  it('lists the modules table, enriched from the module metadata', () => {
    const modules = dev27().modules();
    assert.equal(modules.length, 11);
    const vdc = modules.find((module) => module.name === 'virtual-desktop-controller');
    assert.equal(vdc?.module_id, 'vdc');
    assert.equal(vdc?.title, 'eVDI');
    assert.equal(vdc?.deployment_priority, 6);
    // every row is joined, so nothing in this cluster is missing from MODULE_METADATA
    for (const module of modules) {
      assert.equal(typeof module.title, 'string');
      assert.equal(typeof module.deployment_priority, 'number');
    }
  });
});
