/**
 * Every Python-only construct the four rendered template trees use, pinned to the bytes real
 * Jinja2 3.1.6 produces for the same template and the same context.
 *
 * Each expectation below was taken from a Jinja2 3.1.6 run, not from what nunjucks happens to do.
 * Every case fails if its shim is taken out of `src/config/jinja.ts`: the ones that do not throw
 * without it are the dangerous ones, because that is the failure mode this suite exists to catch.
 *
 * The template snippets are the shapes the real templates use, with synthetic values.
 */

import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { jinjaEnv, normalizePythonSyntax, pythonTruthy, renderTemplate, toJson } from '../../src/config/jinja.ts';

interface Case {
  /** What the construct is, as it appears in a template. */
  readonly name: string;
  readonly template: string;
  readonly context: Record<string, unknown>;
  /** Rendered by Jinja2 3.1.6. */
  readonly python: string;
}

const context = {
  base_os: 'rhel8',
  provider: 'efs',
  probe: 'lustre',
  url: 'https://example.invalid/dl/openmpi-5.0.6.tar.gz',
  host: 'scheduler.sample-cluster.local',
  packages: ['curl', 'jq'],
  extra: ['htop'],
  mapping: { z: '1', a: '2' },
  text: 'MiXeD',
  padded: '  ABCDEF  ',
  group_name: 'IDEA Admins',
  dotted: 'a.b.c',
  storage: { provider: 'fsx_lustre', mount_drive: 'Z', fsx_lustre: { dns: 'fs.example.invalid' } },
  flag: true,
  nothing: null,
  block: 'l1\nl2\n\nl4',
  special: "<a>&'\"é",
  items: ['one', 'two', 'three'],
};

const cases: readonly Case[] = [
  // `x in ('a', 'b')`: nunjucks reads the tuple as a group whose value is its last element, so
  // without the rewrite `in` becomes a substring test and the gated block disappears in silence.
  {
    name: "tuple membership matches the first element: {% if base_os in ('rhel8', 'rocky8') %}",
    template: "{% if base_os in ('rhel8', 'rocky8') %}HIT{% else %}MISS{% endif %}",
    context,
    python: 'HIT',
  },
  {
    name: 'tuple membership is not a substring test',
    template: "{% if probe in ('alpha', 'fsx_lustre_extra') %}HIT{% else %}MISS{% endif %}",
    context,
    python: 'MISS',
  },
  {
    name: "single element tuple with a trailing comma: in ('amazonlinux2023',)",
    template: "{% if provider in ('efs',) %}HIT{% else %}MISS{% endif %}",
    context,
    python: 'HIT',
  },
  {
    name: 'negated tuple membership: not in (...)',
    template: "{% if base_os not in ('rhel8', 'rocky8') %}HIT{% else %}MISS{% endif %}",
    context,
    python: 'MISS',
  },
  // `list + list`: JavaScript `+` stringifies both sides.
  {
    name: 'list + list concatenates, it does not stringify',
    template: "{{ ' '.join(packages + extra) }}|{{ (packages + extra) | length }}",
    context,
    python: 'curl jq htop|3',
  },
  {
    name: 'list literal + list concatenates',
    template: "{% set all = ['first'] + packages %}{{ all | join(',') }}",
    context,
    python: 'first,curl,jq',
  },
  {
    name: 'list + list literal concatenates',
    template: "{% set all = packages + ['last'] %}{{ all | join(',') }}",
    context,
    python: 'curl,jq,last',
  },
  {
    name: 'a string operand keeps + as concatenation',
    template: "{{ provider + '-suffix' }}",
    context,
    python: 'efs-suffix',
  },
  // `seq[-1]`: JavaScript has no negative indexing, it returns undefined and renders nothing.
  {
    name: "final element: url.split('/')[-1]",
    template: "{{ url.split('/')[-1] }}",
    context,
    python: 'openmpi-5.0.6.tar.gz',
  },
  {
    name: "positive index still works: host.split('.')[0]",
    template: "{{ host.split('.')[0] }}",
    context,
    python: 'scheduler',
  },
  // `dict.items()`: nunjucks throws when it reaches the call.
  {
    name: 'dict.items() in a two target loop',
    template: '{% for key, value in mapping.items() %}{{ key }}={{ value }};{% endfor %}',
    context,
    python: 'z=1;a=2;',
  },
  // Python string methods: nunjucks throws, JavaScript has different names.
  { name: 'str.lower()', template: '{{ text.lower() }}', context, python: 'mixed' },
  { name: 'str.upper()', template: '{{ text.upper() }}', context, python: 'MIXED' },
  { name: 'str.lower().strip() chained', template: '{{ padded.lower().strip() }}', context, python: 'abcdef' },
  {
    name: 'str.replace() replaces every occurrence, not just the first',
    template: "{{ dotted.replace('.', '-') }}",
    context,
    python: 'a-b-c',
  },
  // Python keeps the backslash on an escape it does not know, nunjucks drops it.
  {
    name: "an unknown escape in a string literal keeps its backslash: replace(' ', '\\ ')",
    template: "{{ group_name.replace(' ', '\\ ') }}",
    context,
    python: 'IDEA\\ Admins',
  },
  {
    name: 'a known escape in a string literal still decodes',
    template: "{{ dotted.replace('.', '\\n') }}",
    context,
    python: 'a\nb\nc',
  },
  // `'sep'.join(seq)`: a method on a string literal, which nunjucks cannot parse.
  {
    name: "' '.join(list)",
    template: "({{ ' '.join(packages) }})",
    context,
    python: '(curl jq)',
  },
  // `tojson`: nunjucks has no such filter at all.
  { name: 'tojson on a string', template: '{{ text | tojson }}', context, python: '"MiXeD"' },
  {
    name: 'tojson escapes the four characters Jinja2 escapes, and everything non ASCII',
    template: '{{ special | tojson }}',
    context,
    python: '"\\u003ca\\u003e\\u0026\\u0027\\"\\u00e9"',
  },
  {
    name: 'tojson sorts object keys, as Jinja2 policy does',
    template: '{{ mapping | tojson }}',
    context,
    python: '{"a": "2", "z": "1"}',
  },
  { name: 'tojson on a list separates with a space', template: '{{ packages | tojson }}', context, python: '["curl", "jq"]' },
  {
    name: 'tojson on the literals',
    template: '{{ True | tojson }} {{ False | tojson }} {{ None | tojson }}',
    context,
    python: 'true false null',
  },
  // `{% with %}`: nunjucks has no such tag.
  { name: '{% with %} binds a name', template: "{% with n = 'x' %}{{ n }}{% endwith %}", context, python: 'x' },
  {
    name: '{% with %} binds several names',
    template: "{% with prefix = 'p', names = packages %}{{ prefix }}{{ names | join('-') }}{% endwith %}",
    context,
    python: 'pcurl-jq',
  },
  {
    name: '{% with %} spanning lines, with a list concatenation in the value',
    template: "{% with messages = [\n  'head'\n] + packages %}{{ messages | join(',') }}{% endwith %}",
    context,
    python: 'head,curl,jq',
  },
  // Bare output of a Python bool.
  { name: '{{ bool }} prints True, not true', template: '{{ flag }}', context, python: 'True' },
  { name: '{{ expr }} that yields a bool prints True', template: '{{ 1 == 1 }}', context, python: 'True' },
  { name: 'lower on a bool', template: '{{ flag | lower }}', context, python: 'true' },
  { name: 'lower on None', template: '{{ nothing | lower }}', context, python: 'none' },
  { name: 'an undefined name renders empty', template: '[{{ not_provided }}]', context, python: '[]' },
  // Filters Jinja2 and nunjucks share but implement differently.
  {
    name: 'indent leaves the first line and blank lines alone',
    template: 'A\n{{ block | indent(4) }}\nB',
    context,
    python: 'A\nl1\n    l2\n\n    l4\nB',
  },
  // Constructs that need no shim, pinned so a future change to the environment cannot break them.
  {
    name: "dict key membership: 'mount_drive' in storage",
    template: "{% if 'mount_drive' in storage %}HIT{% else %}MISS{% endif %}",
    context,
    python: 'HIT',
  },
  {
    name: 'dynamic subscript on a dict',
    template: "{{ storage['provider'] }}/{{ storage[storage['provider']]['dns'] }}",
    context,
    python: 'fsx_lustre/fs.example.invalid',
  },
  {
    name: 'an inline if with no else renders empty when false',
    template: "{% for i in items %}{{ i }}{{ ',' if not loop.last }}{% endfor %}",
    context,
    python: 'one,two,three',
  },
  {
    name: 'substring membership on a string still works',
    template: "{% if 'prom' in 'amazon_managed_prometheus' %}HIT{% else %}MISS{% endif %}",
    context,
    python: 'HIT',
  },
  {
    name: 'a single trailing newline is stripped from the template source',
    template: '{{ provider }}\n',
    context,
    python: 'efs',
  },
];

const dir = mkdtempSync(join(tmpdir(), 'jinja-audit-'));
cases.forEach((testCase, index) => writeFileSync(join(dir, `case-${index}.jinja2`), testCase.template));
const env = jinjaEnv(dir);

describe('nunjucks renders what Jinja2 3.1.6 renders', () => {
  cases.forEach((testCase, index) => {
    it(testCase.name, () => {
      assert.equal(renderTemplate(env, `case-${index}.jinja2`, testCase.context), testCase.python);
    });
  });
});

describe('the rewrite only touches template tags', () => {
  it('leaves text outside a tag byte for byte alone', () => {
    const source = "packages=('a' 'b')\nif [[ x in ('y') ]]; then echo x.lower(); fi\n";
    assert.equal(normalizePythonSyntax(source), source);
  });

  it('leaves a raw block alone, tuples and method calls included', () => {
    const source = "{% raw %}\nDEVICES=$(ls /dev/nvme[0-9]n1)\nx in ('a', 'b')\n{% endraw %}";
    assert.equal(normalizePythonSyntax(source), source);
  });

  // A pipe is not proof that the result is already a string: `default(True)` and `first` both
  // hand back a boolean, so every output tag goes through `pystr`, which passes a string through.
  it('wraps an output tag that already has a filter', () => {
    assert.equal(normalizePythonSyntax('{{ value | indent(2) }}'), '{{ (value | indent(2)) | pystr }}');
  });

  it('leaves a string literal with no backslash in it untouched', () => {
    assert.equal(normalizePythonSyntax("{% if p == 'a.b/c' %}x{% endif %}"), "{% if p == 'a.b/c' %}x{% endif %}");
  });
});

describe('toJson matches Jinja2 htmlsafe_json_dumps', () => {
  // Values Jinja2 3.1.6 produced for the same inputs.
  it('sorts keys and uses Python separators', () => {
    assert.equal(toJson({ z: 1, a: [1, 2] }), '{"a": [1, 2], "z": 1}');
  });

  it('escapes the script unsafe characters', () => {
    assert.equal(toJson("<'&>"), '"\\u003c\\u0027\\u0026\\u003e"');
  });

  it('escapes non ASCII', () => {
    assert.equal(toJson('café'), '"caf\\u00e9"');
  });

  // Python sorts by code point, JavaScript `<` by UTF-16 code unit, so a key in the private-use
  // area and a key above the basic plane come out in opposite orders.
  it('sorts keys by code point, not by UTF-16 code unit', () => {
    assert.equal(toJson({ '\u{e000}': 2, '\u{10000}': 1 }), '{"\\ue000": 2, "\\ud800\\udc00": 1}');
    assert.equal(toJson({ '\u{1f600}': 1, '\u{ffff}': 2, z: 3 }), '{"z": 3, "\\uffff": 2, "\\ud83d\\ude00": 1}');
  });

  // Python's default JSON policy writes the non-finite floats as bare words; JSON.stringify
  // writes `null` for all three.
  it('writes the non-finite numbers the way Python writes them', () => {
    assert.equal(toJson(Number.NaN), 'NaN');
    assert.equal(toJson(Infinity), 'Infinity');
    assert.equal(toJson(-Infinity), '-Infinity');
    assert.equal(toJson({ a: Number.NaN, b: 1 }), '{"a": NaN, "b": 1}');
    assert.equal(toJson(-0), '-0.0');
  });
});

/**
 * The one divergence the environment cannot close: nunjucks compiles conditions to JavaScript
 * truthiness, where an empty list and an empty dict are true and Python calls both false. It is
 * reachable today from `policies/cluster-manager.yml`, whose `{% if config.get_list(...) %}` is
 * only correct because `src/cdk/policy.ts` hands empty lists to the renderer as undefined.
 */
describe("empty container truthiness is the caller's job", () => {
  it('reports the Python answer for the values a context carries', () => {
    assert.equal(pythonTruthy([]), false);
    assert.equal(pythonTruthy({}), false);
    assert.equal(pythonTruthy(''), false);
    assert.equal(pythonTruthy(0), false);
    assert.equal(pythonTruthy(null), false);
    assert.equal(pythonTruthy(undefined), false);
    assert.equal(pythonTruthy(['a']), true);
    assert.equal(pythonTruthy({ a: 1 }), true);
  });

  it('renders the block for an empty list, which Jinja2 skips', () => {
    const gate = mkdtempSync(join(tmpdir(), 'jinja-audit-gate-'));
    writeFileSync(join(gate, 'gate.jinja2'), '{% if arns %}BLOCK{% else %}NONE{% endif %}');
    const gateEnv = jinjaEnv(gate);
    assert.equal(renderTemplate(gateEnv, 'gate.jinja2', { arns: [] }), 'BLOCK', 'Jinja2 renders NONE here');
    assert.equal(renderTemplate(gateEnv, 'gate.jinja2', { arns: undefined }), 'NONE');
  });
});
