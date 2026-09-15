/**
 * The places nunjucks and Jinja2 disagree, plus Jinja2's
 * `keep_trailing_newline=False`. Expectations come from Jinja2 3.x `filters.do_indent`,
 * `Environment.keep_trailing_newline` and PyYAML `yaml.dump(..., sort_keys=False, width=140)`.
 */

import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { jinjaEnv, jinjaIndent, renderTemplate, toYaml } from '../../src/config/jinja.ts';

describe('jinjaIndent (Jinja2 3.x do_indent)', () => {
  it('leaves the first line alone', () => {
    assert.equal(jinjaIndent('a\nb', 2), 'a\n  b');
  });

  it('indents the first line with first=true', () => {
    assert.equal(jinjaIndent('a\nb', 2, true), '  a\n  b');
  });

  it('leaves blank lines blank', () => {
    assert.equal(jinjaIndent('a\n\nb', 2), 'a\n\n  b');
  });

  it('indents blank lines with blank=true', () => {
    assert.equal(jinjaIndent('a\n\nb', 2, false, true), 'a\n  \n  b');
  });

  it('keeps a trailing newline, unindented - which is how to_yaml output arrives', () => {
    assert.equal(jinjaIndent('a\nb\n', 2), 'a\n  b\n');
  });

  it('passes a single line through', () => {
    assert.equal(jinjaIndent('a', 6), 'a');
  });
});

describe('toYaml (Utils.to_yaml)', () => {
  it('emits a block sequence of plain scalars, one per line, with a trailing newline', () => {
    assert.equal(toYaml(['store://bucket/*', 'store://bucket']), '- store://bucket/*\n- store://bucket\n');
  });

  it('emits flow style for empties', () => {
    assert.equal(toYaml([]), '[]\n');
    assert.equal(toYaml({}), '{}\n');
  });

  it('keeps insertion order rather than sorting', () => {
    assert.equal(toYaml({ b: 1, a: 2 }), 'b: 1\na: 2\n');
  });
});

describe('jinjaEnv', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ideactl-jinja-'));
  writeFileSync(join(dir, 'inc.yml'), 'included: {{ value }}\n');
  writeFileSync(
    join(dir, 'main.yml'),
    [
      // `{{ True }}` prints `True` in Jinja2, which the `pystr` shim reproduces; the globals
      // exist so `True` is not an undefined name in kwargs and comparisons.
      'truthy: {{ True }}',
      'lower_true: {{ True | lower }}',
      'lower_false: {{ False | lower }}',
      "none_is_falsy: {{ 'no' if None else 'yes' }}",
      "default_kwarg: {{ 'yes' if flag == False else 'no' }}",
      'missing: [{{ not_defined }}]',
      "block:\n  {{ utils.to_yaml(items) | indent(2) }}",
      "{% include 'inc.yml' %}",
    ].join('\n'),
  );
  const env = jinjaEnv(dir);

  it('renders the Python literals, filters and includes the policy templates use', () => {
    const out = renderTemplate(env, 'main.yml', { flag: false, items: ['x', 'y'], value: 7, utils: { to_yaml: toYaml } });
    assert.equal(
      out,
      [
        'truthy: True',
        'lower_true: true',
        'lower_false: false',
        'none_is_falsy: yes',
        'default_kwarg: yes',
        'missing: []',
        'block:',
        '  - x',
        '  - y',
        '', // to_yaml's trailing newline: Jinja2 does not indent the empty line it opens
        'included: 7', // and no newline after it: Jinja2 strips one from every template source
      ].join('\n'),
    );
  });

  it('strips one trailing newline from the template source, not from an interpolated value', () => {
    writeFileSync(join(dir, 'trailing.yml'), 'a: {{ value }}\n');
    assert.equal(renderTemplate(env, 'trailing.yml', { value: 'x' }), 'a: x');
    // the newline the value carries is not the template's, so it survives
    assert.equal(renderTemplate(env, 'trailing.yml', { value: 'x\n' }), 'a: x\n');
    writeFileSync(join(dir, 'trailing2.yml'), 'a: 1\n\n');
    assert.equal(renderTemplate(env, 'trailing2.yml', {}), 'a: 1\n');
  });
});
