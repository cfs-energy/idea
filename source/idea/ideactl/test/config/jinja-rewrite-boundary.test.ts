/**
 * The boundary of the source rewrite in `src/config/jinja.ts`: what it may edit, what it must copy
 * through, which spellings of one operation it has to translate, and which Python forms it refuses.
 *
 * Every `python` value below is the bytes Jinja2 3.1.6 produced for that exact template and
 * context, taken from a run of the real renderer, not from what nunjucks happens to do. The
 * `refuses` table records the Python output this layer deliberately does not reproduce: those cases
 * have to raise, because a rewrite that guessed would change generated shell or configuration text
 * with nothing to show for it.
 */

import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { jinjaEnv, normalizePythonSyntax, renderTemplate } from '../../src/config/jinja.ts';

interface Case {
  readonly name: string;
  readonly template: string;
  readonly context?: Record<string, unknown>;
  /** Rendered by Jinja2 3.1.6. */
  readonly python: string;
}

interface Refusal {
  readonly name: string;
  readonly template: string;
  readonly context?: Record<string, unknown>;
  /** What Jinja2 3.1.6 renders, which this layer does not reproduce. */
  readonly python: string;
  /** Part of the message the failure has to carry. */
  readonly message: string;
}

const dir = mkdtempSync(join(tmpdir(), 'jinja-boundary-'));
let counter = 0;

function render(template: string, context: Record<string, unknown> = {}): string {
  counter += 1;
  const name = `case-${counter}.jinja2`;
  writeFileSync(join(dir, name), template);
  return renderTemplate(jinjaEnv(dir), name, context);
}

function run(cases: readonly Case[]): void {
  for (const testCase of cases) {
    it(testCase.name, () => {
      assert.equal(render(testCase.template, testCase.context), testCase.python);
    });
  }
}

/**
 * A string literal, a comment and a `{% raw %}` body all hold text, not code. Finding an apparent
 * tag or an apparent Python operation there and rewriting it changes bytes the template author
 * wrote by hand.
 */
const dataNotCode: readonly Case[] = [
  { name: 'a tuple inside a string literal', template: `{{ "x in ('a', 'b')" }}`, python: "x in ('a', 'b')" },
  {
    name: 'a tuple inside a filter argument',
    template: `{{ missing | default("x in ('a', 'b')", true) }}`,
    python: "x in ('a', 'b')",
  },
  {
    name: 'a tuple inside a filter argument that is used',
    template: `{{ items | join("x in ('a', 'b')") }}`,
    context: { items: ['1', '2'] },
    python: "1x in ('a', 'b')2",
  },
  { name: 'a negative index inside a string literal', template: '{{ "index [-1]" }}', python: 'index [-1]' },
  { name: 'a default value holding a negative index', template: '{{ missing | default("index [-1]") }}', python: 'index [-1]' },
  { name: 'an items call inside a string literal', template: '{{ "mapping.items()" }}', python: 'mapping.items()' },
  { name: 'a lower call inside a string literal', template: '{{ "text.lower()" }}', python: 'text.lower()' },
  { name: 'a strip call inside a string literal', template: '{{ "text.strip()" }}', python: 'text.strip()' },
  { name: 'a join call inside a string literal', template: `{{ "','.join(x)" }}`, python: "','.join(x)" },
  { name: 'an addition inside a string literal', template: '{{ "a + b" }}', python: 'a + b' },
  { name: 'a with tag inside a string literal', template: '{{ "{% with a = 1 %}" }}', python: '{% with a = 1 %}' },
  { name: 'an output delimiter inside a string literal', template: "{{ '{{' }}", python: '{{' },
  { name: 'a closing delimiter inside a string literal', template: '{{ "}}" }}', python: '}}' },
  { name: 'an escaped quote inside a string literal', template: "{{ 'it\\'s' }}", python: "it's" },
  { name: 'an escaped double quote inside a string literal', template: '{{ "say \\"hi\\"" }}', python: 'say "hi"' },
  {
    name: 'a tuple whose element is a pipe character',
    template: "{% if sep in ('|', ';') %}HIT{% else %}MISS{% endif %}",
    context: { sep: '|' },
    python: 'HIT',
  },
  { name: 'a tag inside a comment renders nothing', template: `A{# {{ x in ('a', 'b') }} #}B`, python: 'AB' },
  { name: 'a with tag inside a comment renders nothing', template: 'A{# {% with a = 1 %} #}B', python: 'AB' },
  {
    name: 'an apostrophe inside a comment does not open a literal',
    template: `A{# it's a comment with x in ('a','b') #}B`,
    python: 'AB',
  },
  {
    name: 'a raw body holding an output tag',
    template: `{% raw %}{{ x in ('a', 'b') }}{% endraw %}`,
    python: `{{ x in ('a', 'b') }}`,
  },
  {
    name: 'a raw body holding a with block',
    template: '{% raw %}{% with a = 1 %}{% endwith %}{% endraw %}',
    python: '{% with a = 1 %}{% endwith %}',
  },
  {
    name: 'a raw body spanning lines',
    template: `{% raw %}\nDEVICES=$(ls /dev/nvme[0-9]n1)\nx in ('a', 'b')\n{% endraw %}`,
    python: `\nDEVICES=$(ls /dev/nvme[0-9]n1)\nx in ('a', 'b')\n`,
  },
  {
    name: 'a raw block with whitespace control on both tags',
    template: 'A\n{%- raw -%}\n{{ x.lower() }}\n{%- endraw -%}\nB',
    python: 'A{{ x.lower() }}B',
  },
];

/**
 * The same operation written a different way has to reach the same bytes. Each row is a spelling
 * the rewrite has to resolve as a primary expression.
 */
const spellings: readonly Case[] = [
  {
    name: 'chained replace replaces in both calls',
    template: "{{ text.replace('a', 'b').replace('b', 'c') }}",
    context: { text: 'aaa' },
    python: 'ccc',
  },
  {
    name: 'three list operands concatenate',
    template: "{{ (a + b + c) | join(',') }}",
    context: { a: ['a'], b: ['b'], c: ['c'] },
    python: 'a,b,c',
  },
  {
    name: 'a list literal in the middle of a concatenation',
    template: "{{ ([a] + [b] + c) | join(',') }}",
    context: { a: '1', b: '2', c: ['3'] },
    python: '1,2,3',
  },
  {
    name: 'a tuple of mixed types, matching the string',
    template: "{% if x in ('a', 2) %}HIT{% else %}MISS{% endif %}",
    context: { x: 'a' },
    python: 'HIT',
  },
  {
    name: 'a tuple of mixed types, matching the number',
    template: "{% if x in ('a', 2) %}HIT{% else %}MISS{% endif %}",
    context: { x: 2 },
    python: 'HIT',
  },
  {
    name: 'a nested tuple',
    template: "{% if x in ('a', ('b', 'c')) %}HIT{% else %}MISS{% endif %}",
    context: { x: 'a' },
    python: 'HIT',
  },
  {
    name: 'a tuple element carrying an escaped quote',
    template: "{% if x in ('a\\'b', 'c') %}HIT{% else %}MISS{% endif %}",
    context: { x: "a'b" },
    python: 'HIT',
  },
  { name: 'a tuple bound by set', template: "{% set pair = ('a', 'b') %}{{ pair | join('-') }}", python: 'a-b' },
  { name: 'a tuple iterated by for', template: "{% for v in ('a', 'b') %}{{ v }}{% endfor %}", python: 'ab' },
  { name: 'an index other than -1 on a list', template: '{{ seq[-2] }}', context: { seq: ['a', 'b', 'c'] }, python: 'b' },
  { name: 'an index other than -1 on a string', template: '{{ word[-2] }}', context: { word: 'abcd' }, python: 'c' },
  {
    name: 'a negative index on a concatenation',
    template: '{{ (a + b)[-1] }}',
    context: { a: ['x'], b: ['y'] },
    python: 'y',
  },
  {
    name: 'a negative index followed by a method call',
    template: "{{ url.split('/')[-1].upper() }}",
    context: { url: 'https://example.invalid/a/b.tgz' },
    python: 'B.TGZ',
  },
  {
    name: 'items with a space before its parentheses',
    template: '{% for k, v in mapping.items () %}{{ k }}={{ v }};{% endfor %}',
    context: { mapping: { z: '1', a: '2' } },
    python: 'z=1;a=2;',
  },
  {
    name: 'items with a space before its dot',
    template: '{% for key, value in mapping . items() %}{{ key }}={{ value }};{% endfor %}',
    context: { mapping: { z: '1' } },
    python: 'z=1;',
  },
  {
    name: 'a join argument holding a quoted closing parenthesis',
    template: "{{ ','.join([left, ')', right]) }}",
    context: { left: 'L', right: 'R' },
    python: 'L,),R',
  },
  {
    name: 'a join whose separator is a name',
    template: '{{ sep.join(parts) }}',
    context: { sep: '-', parts: ['a', 'b'] },
    python: 'a-b',
  },
  { name: 'a method call on a string literal receiver', template: "{{ 'MiXeD'.lower() }}", python: 'mixed' },
  {
    name: 'a method call on a parenthesised receiver',
    template: '{{ (a + b).lower() }}',
    context: { a: 'AB', b: 'CD' },
    python: 'abcd',
  },
  { name: 'strip then lower chained', template: '{{ padded.strip().lower() }}', context: { padded: '  AB  ' }, python: 'ab' },
  {
    name: 'a with tag spanning lines whose value is a concatenation',
    template: "{% with messages = [\n  'head'\n] + packages %}{{ messages | join(',') }}{% endwith %}",
    context: { packages: ['curl', 'jq'] },
    python: 'head,curl,jq',
  },
  {
    name: 'a negative index outside the sequence renders empty, as Undefined does',
    template: '[{{ empty[-1] }}]',
    context: { empty: [] },
    python: '[]',
  },
  {
    name: 'indent with a string prefix rather than a width',
    template: "A\n{{ block | indent('--') }}\nB",
    context: { block: 'l1\nl2' },
    python: 'A\nl1\n--l2\nB',
  },
  {
    name: 'a list literal of negative numbers is not a subscript',
    template: '{% if x in [-1, -2] %}HIT{% else %}MISS{% endif %}',
    context: { x: -2 },
    python: 'HIT',
  },
  { name: 'a list literal of one negative number bound by set', template: "{% set v = [-1] %}{{ v | join(',') }}", python: '-1' },
  {
    name: 'a method call is not a receiver for the keyword before it',
    template: "{% if not text.lower() == 'ab' %}HIT{% else %}MISS{% endif %}",
    context: { text: 'AB' },
    python: 'MISS',
  },
  {
    name: 'a method call on the left of a tuple membership test',
    template: "{% if text.lower() in ('ab', 'cd') %}HIT{% else %}MISS{% endif %}",
    context: { text: 'AB' },
    python: 'HIT',
  },
  { name: 'a space before a subscript', template: '{{ seq [-1] }}', context: { seq: ['a', 'b'] }, python: 'b' },
  { name: 'a space before a method dot', template: '{{ padded .strip() }}', context: { padded: ' q ' }, python: 'q' },
];

/**
 * A whitespace-control marker is not a unary operator, and a pipe is not proof that the value
 * reaching the output is already a string.
 */
const output: readonly Case[] = [
  { name: 'a trimmed boolean output', template: 'X\n{{- flag -}}\nY', context: { flag: true }, python: 'XTrueY' },
  { name: 'a trimmed block', template: 'X\n{%- if flag -%}\nZ\n{%- endif -%}\nY', context: { flag: true }, python: 'XZY' },
  { name: 'one trimmed side of a block', template: 'A\n{%- if flag %}\nZ\n{% endif -%}\nB', context: { flag: true }, python: 'A\nZ\nB' },
  {
    name: 'the plus marker, which only cancels options this environment leaves off',
    template: 'A\n{%+ if flag %}Z{% endif %}\nB',
    context: { flag: true },
    python: 'A\nZ\nB',
  },
  { name: 'a boolean a filter produced', template: '{{ missing | default(True) }}', python: 'True' },
  { name: 'a boolean taken from a list', template: '{{ flags | first }}', context: { flags: [true, false] }, python: 'True' },
  {
    name: 'a pipe character inside a literal is not a filter',
    template: "{{ True if separator == '|' else False }}",
    context: { separator: '|' },
    python: 'True',
  },
  { name: 'a bare None', template: '[{{ nothing }}]', context: { nothing: null }, python: '[None]' },
];

/**
 * Jinja2 evaluates every `with` value against the outer scope before installing any new binding,
 * and puts the outer bindings back at `endwith`.
 */
const withBlocks: readonly Case[] = [
  {
    name: 'a binding that reads the name it shadows',
    template: "{% set a = 'outer' %}{% with a = 'inner', b = a %}{{ b }}{% endwith %}|{{ a }}",
    python: 'outer|outer',
  },
  { name: 'a name bound by with is gone after endwith', template: "{% with n = 'x' %}{{ n }}{% endwith %}[{{ n }}]", python: 'x[]' },
  { name: 'nested with blocks', template: '{% with a = 1 %}{% with a = 2 %}{{ a }}{% endwith %}{{ a }}{% endwith %}', python: '21' },
  { name: 'a with block with no bindings', template: '{% with %}in{% endwith %}', python: 'in' },
  {
    name: 'a with block inside a loop',
    template: '{% for i in items %}{% with v = i %}{{ v }}{% endwith %}{% endfor %}',
    context: { items: ['a', 'b'] },
    python: 'ab',
  },
  { name: 'whitespace control on the with and endwith tags', template: "A\n{%- with n = 'x' -%}\n{{ n }}\n{%- endwith -%}\nB", python: 'AxB' },
];

/**
 * Forms with no equivalent this layer can build. Each one renders in Jinja2 and has to raise here:
 * the alternative is different bytes with nothing to show for it.
 */
const refuses: readonly Refusal[] = [
  {
    name: 'split with no separator, which Python splits on whitespace',
    template: `{{ "a b  c".split() | join('|') }}`,
    python: 'a|b|c',
    message: '`.split()` with no separator',
  },
  {
    name: 'strip with an argument',
    template: "{{ 'xxaxx'.strip('x') }}",
    python: 'a',
    message: '`.strip()` with an argument',
  },
  {
    name: 'replace with a count',
    template: "{{ 'aaa'.replace('a', 'b', 1) }}",
    python: 'baa',
    message: '`.replace()` with a count argument',
  },
  {
    name: 'items outside a two-target loop',
    template: '{{ mapping.items() }}',
    context: { mapping: { a: 1 } },
    python: "dict_items([('a', 1)])",
    message: '`.items()` outside a two-target for loop',
  },
  {
    name: 'a named Unicode escape',
    template: "{{ 'x'.replace('x', '\\N{SNOWMAN}') }}",
    python: '☃',
    message: 'a named Unicode escape',
  },
  {
    name: 'a truncated hex escape, which Jinja2 also rejects',
    template: "{{ 'a'.replace('a', '\\x4') }}",
    python: 'TemplateSyntaxError: truncated \\xXX escape',
    message: 'the truncated escape',
  },
  {
    name: 'a list written bare',
    template: '{{ packages }}',
    context: { packages: ['curl', 'jq'] },
    python: "['curl', 'jq']",
    message: 'written bare',
  },
  {
    name: 'a dict written bare',
    template: '{{ mapping }}',
    context: { mapping: { a: 1 } },
    python: "{'a': 1}",
    message: 'written bare',
  },
  {
    name: 'a list added to a string, which Jinja2 also rejects',
    template: '{{ packages + suffix }}',
    context: { packages: ['a'], suffix: 'b' },
    python: 'TypeError: can only concatenate list (not "str") to list',
    message: 'cannot add',
  },
  {
    name: 'lower on a list, which Python applies to the repr',
    template: '{{ items | lower }}',
    context: { items: ['A', 'B'] },
    python: "['a', 'b']",
    message: "`lower` on a list",
  },
  {
    name: 'upper on a dict, which Python applies to the repr',
    template: '{{ mapping | upper }}',
    context: { mapping: { a: 'b' } },
    python: "{'A': 'B'}",
    message: '`upper` on a dict',
  },
  {
    name: 'indent on a list, which Jinja2 also rejects',
    template: '{{ items | indent(2) }}',
    context: { items: ['a', 'b'] },
    python: "AttributeError: 'list' object has no attribute 'splitlines'",
    message: '`indent` on a list',
  },
];

describe('the rewrite edits code and never data', () => {
  run(dataNotCode);

  it('leaves text outside a tag byte for byte alone', () => {
    const source = "packages=('a' 'b')\nif [[ x in ('y') ]]; then echo x.lower(); fi\n";
    assert.equal(normalizePythonSyntax(source), source);
  });

  it('leaves a comment byte for byte alone', () => {
    const source = `{# x in ('a', 'b') and y.lower() #}`;
    assert.equal(normalizePythonSyntax(source), source);
  });

  it('leaves a raw body byte for byte alone', () => {
    const source = `{% raw %}{{ x in ('a', 'b') }}{% endraw %}`;
    assert.equal(normalizePythonSyntax(source), source);
  });
});

describe('an equivalent spelling of an operation translates', () => {
  run(spellings);
});

describe('whitespace control and filtered values survive the output wrapper', () => {
  run(output);
});

describe('a with block binds simultaneously and restores the outer scope', () => {
  run(withBlocks);
});

describe('a form with no equivalent raises instead of rendering', () => {
  for (const refusal of refuses) {
    it(`${refusal.name} (Jinja2: ${refusal.python})`, () => {
      assert.throws(
        () => render(refusal.template, refusal.context),
        (error: unknown) => String(error).includes(refusal.message),
        `expected a failure naming ${refusal.message}`,
      );
    });
  }
});

describe("Jinja2's source normalization", () => {
  it('normalizes CRLF and strips one trailing newline', () => {
    assert.equal(render('A\r\n{{ flag }}\r\n', { flag: true }), 'A\nTrue');
  });
});
