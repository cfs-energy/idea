import {afterEach, expect, it} from 'vitest';
import {readFileSync} from 'node:fs';
import {guarded, MAX_RENDER_OUTPUT, MAX_RENDER_STEPS, renderJobTemplate, replacePlaceholders, templateSurface, TEMPLATE_FILTERS, TEMPLATE_GLOBALS, TEMPLATE_TESTS} from './job-template';
import nunjucks from 'nunjucks';

afterEach(() => {delete (globalThis as any).markerReached;});

const values = {job_name: 'marker', cpus: 4, modules: ['gcc', 'openmpi'], hosts: 'a,b'};

it('renders the values, loops, conditions, filters and string and array methods a job script uses', () => {
    const template = [
        '#PBS -N {{ job_name|upper }}',
        '#PBS -l ncpus={{ cpus * 2 }}',
        '{% for module in modules %}module load {{ module }}\n{% endfor %}',
        '{% if cpus > 2 %}parallel{% endif %} {{ hosts.split(",")|join("+") }} {{ modules.join(" ") }} {{ missing|default("none") }}',
    ].join('\n');
    expect(renderJobTemplate(template, values)).toBe('#PBS -N MARKER\n#PBS -l ncpus=8\nmodule load gcc\nmodule load openmpi\n\nparallel a+b gcc openmpi none');
});

it.each([
    '{{ "".constructor.constructor("globalThis.markerReached = 1")() }}',
    '{{ constructor.constructor("globalThis.markerReached = 1")() }}',
    '{{ range.constructor("globalThis.markerReached = 1")() }}',
    '{{ job_name.__proto__.constructor.constructor("globalThis.markerReached = 1")() }}',
    '{{ modules["constructor"]["constructor"]("globalThis.markerReached = 1")() }}',
    '{{ valueOf.call.constructor("globalThis.markerReached = 1")() }}',
])('reaches no function constructor: %s', template => {
    try {
        expect(renderJobTemplate(template, values)).toBe('');
    } catch (error) {
        expect(String(error)).toMatch(/undefined or falsey|cannot use the (name|key)/);
    }
    expect((globalThis as any).markerReached).toBeUndefined();
});

it('writes plain-text values as they are', () => {
    const template = 'echo %first% %second% %project_name% %unknown% $$';
    const values = {first: "cost $$5 $& $' $`", second: '%first%', project_name: 'marker-project'};
    expect(replacePlaceholders(template, values, ['first', 'second'])).toBe("echo cost $$5 $& $' $` %first% marker-project %unknown% $$");
    expect(replacePlaceholders('%first%', {}, ['first'])).toBe('');
});

it.each([
    ['LOG=run_%Y%m%d_%job_name%.log', 'LOG=run_%Y%m%d_marker.log'],
    ['printf "%s%ncpus%"', 'printf "%s4"'],
    ['$(date +%Y%m%d)_%job_name%', '$(date +%Y%m%d)_marker'],
    ['%job-name% %queue.name%', 'dashed dotted'],
    ['%job_name%%ncpus%', 'marker4'],
    ['%%job_name%%', '%marker%'],
])('substitutes every placeholder the earlier per-parameter replace did: %s', (template, expected) => {
    const values = {job_name: 'marker', ncpus: 4, 'job-name': 'dashed', 'queue.name': 'dotted'};
    expect(replacePlaceholders(template, values, ['job_name', 'ncpus', 'job-name', 'queue.name'])).toBe(expected);
});

it('renders the bundled job script templates', () => {
    const sample = readFileSync('src/pages/hpc/sample-job-script-jinja2.txt', 'utf8');
    const rendered = renderJobTemplate(sample, {queue_name: 'normal', job_name: 'marker', project_name: 'p', instance_type: 'c5.large', wall_time: '01:00:00', force_ri: false, input_file: '/data/in.dat', user_pre_exec: '', binary_location: '/apps/solver', param1: 1, param2: 2, param3: 3, user_post_exec: ''});
    expect(rendered).toContain('#PBS -N marker');
    expect(rendered).toContain('such as MARKER');
    expect(rendered).toContain('/apps/solver  /data/in.dat');
    // the scheduler's demo application (scheduler_default_settings.py)
    const demo = readFileSync('../../idea-scheduler/src/ideascheduler/app/scheduler_default_settings.py', 'utf8');
    const lines = [...demo.slice(demo.indexOf("job_script_type='jinja2'")).matchAll(/^\s+'(#!\/bin\/bash|#PBS[^']*|# [^']*|\/bin\/sleep[^']*)',$/gm)].map(match => match[1]).slice(0, 8);
    expect(lines.length).toBe(8);
    expect(renderJobTemplate(lines.join('\n'), {queue_name: 'normal', job_name: 'marker', project_name: 'p', instance_type: 'm7i.large', sleep_time: 60}))
        .toBe('#!/bin/bash\n#PBS -q normal\n#PBS -N  marker\n#PBS -P p\n#PBS -l instance_type=m7i.large\n# You can use Jinja templating\n# ex: MARKER\n/bin/sleep 60');
});

it('keeps the number and string methods and filters existing templates call', () => {
    expect(renderJobTemplate('{{ (mem * 1024).toFixed(0) }} {{ cpus.toString() }} {{ (ratio).toFixed(2) }} {{ name.toUpperCase() }} {{ hosts.split(",").join("+") }}', {mem: 1.5, cpus: 4, ratio: 0.5, name: 'mixed', hosts: 'a,b'})).toBe('1536 4 0.50 MIXED a+b');
    expect(renderJobTemplate('{{ name | lower }} {{ name | upper }} {{ missing | default("x") }} {{ 2.567 | round(1) }}', {name: 'Mixed'})).toBe('mixed MIXED x 2.6');
    // a template helper's own methods are not on the list
    expect(() => renderJobTemplate('{% set c = cycler("a", "b") %}{{ c.next() }}', {})).toThrow(/undefined or falsey/);
});

describe('the shared prototypes', () => {
    const before = Object.getOwnPropertyNames(Object.prototype).sort();
    afterEach(() => expect(Object.getOwnPropertyNames(Object.prototype).sort()).toEqual(before));

    it.each([
        // the reviewer's case: a key whose string form differs between conversions
        '{% macro k() %}__proto__{% endmacro %}{% set p = values[{toString: k}] %}{{ p.__defineGetter__("markerReached", k) }}',
        '{{ values["__proto__"]["__defineGetter__"]("markerReached", values) }}',
        '{{ job_name.__proto__ }}{{ modules.__proto__ }}{{ (1).__proto__ }}',
        '{{ job_name.split(",").constructor.constructor("globalThis.markerReached = 1")() }}',
        '{{ job_name.concat.constructor("globalThis.markerReached = 1")() }}',
        '{{ (1).toFixed.constructor("globalThis.markerReached = 1")() }}',
        '{{ modules.slice(0).constructor.prototype.markerReached }}',
        '{{ values.hasOwnProperty("x") }}{{ values.valueOf() }}{{ values.toString() }}',
    ])('reaches no prototype or constructor: %s', template => {
        try {
            renderJobTemplate(template, {...values, values: {x: 1}});
        } catch (error) {
            expect(String(error)).toMatch(/undefined or falsey|takes only text|cannot use the (name|key)/);
        }
        expect((globalThis as any).markerReached).toBeUndefined();
        expect(({} as any).markerReached).toBeUndefined();
    });

    it('cannot write through a value it was given or got back', () => {
        const given = {modules: ['gcc'], nested: {a: 1}};
        expect(() => renderJobTemplate('{{ modules.concat({toString: modules}) }}', given)).toThrow(/takes only text|cannot use the (name|key)/);
        expect(() => renderJobTemplate('{{ modules.push("x") }}', given)).toThrow(/only on a list the template made/);
        expect(renderJobTemplate('{% set copy = modules.slice(0) %}{{ copy.join(",") }}', given)).toBe('gcc');
        expect(given).toEqual({modules: ['gcc'], nested: {a: 1}});
    });
});

it.each([
    '{{ (1).constructor.constructor("globalThis.markerReached = 1")() }}',
    '{{ cycler("a").next.constructor("globalThis.markerReached = 1")() }}',
    '{{ cpus.toFixed.call.constructor("globalThis.markerReached = 1")() }}',
])('still reaches no function constructor through a number or a helper: %s', template => {
    try {
        expect(renderJobTemplate(template, values)).toBe('');
    } catch (error) {
        expect(String(error)).toMatch(/undefined or falsey|cannot use the (name|key)/);
    }
    expect((globalThis as any).markerReached).toBeUndefined();
});

describe('the names a template can use', () => {
    const before = Object.getOwnPropertyNames(Object.prototype).sort();
    afterEach(() => {
        expect(Object.getOwnPropertyNames(Object.prototype).sort()).toEqual(before);
        expect((globalThis as any).markerReached).toBeUndefined();
    });

    it('are exactly the listed filters, tests and globals, with nothing inherited', () => {
        const {filters, tests, globals} = templateSurface();
        // pinned here, not read from the module: adding a name must change this test
        const expected = {
            filters: ['abs', 'batch', 'capitalize', 'center', 'd', 'default', 'dictsort', 'dump', 'e', 'escape', 'first', 'float', 'groupby', 'indent', 'int', 'join', 'last', 'length', 'list', 'lower', 'nl2br', 'reject', 'rejectattr', 'replace', 'reverse', 'round', 'safe', 'select', 'selectattr', 'slice', 'sort', 'string', 'striptags', 'sum', 'title', 'trim', 'truncate', 'upper', 'urlencode', 'wordcount'],
            tests: ['defined', 'divisibleby', 'eq', 'equalto', 'escaped', 'even', 'falsy', 'ge', 'greaterthan', 'gt', 'iterable', 'le', 'lessthan', 'lower', 'lt', 'mapping', 'ne', 'null', 'number', 'odd', 'sameas', 'string', 'truthy', 'undefined', 'upper'],
            globals: ['joiner', 'range'],
        };
        for (const [table, names] of [[filters, expected.filters], [tests, expected.tests], [globals, expected.globals]] as const) {
            expect(Object.getPrototypeOf(table)).toBeNull();
            expect(Object.isFrozen(table)).toBe(true);
            expect(Object.keys(table).sort()).toEqual(names);
        }
        expect([...TEMPLATE_FILTERS].sort()).toEqual(expected.filters);
        expect([...TEMPLATE_TESTS].sort()).toEqual(expected.tests);
        expect([...TEMPLATE_GLOBALS].sort()).toEqual(expected.globals);
    });

    // every entry on sample values: no result, and nothing reachable from it, is a function; a refusal by the guard
    // for handing back a function fails the sweep rather than being skipped
    const SAMPLES: any[] = ['a,b', 3.25, -2, ['b', 'a', 'a'], [{n: 'x', v: 1}, {n: 'y', v: 2}], {k: 'v'}, true, null, undefined, ''];
    const EXTRA: any[][] = [[], ['n'], [1], [2, 'x'], ['a', 'b'], ['v']];
    const reachable = (value: any, seen = new Set<any>()): any[] => {
        if (value == null || typeof value !== 'object' || seen.has(value)) return [value];
        seen.add(value);
        return [value, ...Object.keys(value).flatMap(key => reachable(value[key], seen))];
    };
    const sweepEntry = (name: string, fn: Function, kind: string) => {
        const surface: any = templateSurface();
        for (const sample of SAMPLES) for (const args of EXTRA) {
            let result: any;
            try {
                result = kind === 'global' ? fn(sample, ...args) : fn.call({env: {getTest: (t: string) => surface.tests[t], opts: {}}, ctx: {}}, sample, ...args);
            } catch (error) {
                if (/handed back a function/.test(String(error))) throw new Error(`${name} handed back a function for ${JSON.stringify(sample)}`);
                continue;
            }
            if (typeof result === 'function' && surface.handedOut(result)) continue; // the joiner's callable
            for (const item of reachable(result)) {
                if (typeof item === 'function') throw new Error(`${name}(${JSON.stringify(sample)}) reaches a function`);
            }
        }
    };
    it.each([...TEMPLATE_FILTERS.map(name => ['filter', name]), ...TEMPLATE_TESTS.map(name => ['test', name]), ...TEMPLATE_GLOBALS.map(name => ['global', name])])('%s %s hands back no function', (kind, name) => {
        const surface: any = templateSurface();
        sweepEntry(name, surface[kind === 'filter' ? 'filters' : kind === 'test' ? 'tests' : 'globals'][name], kind);
    });

    it('fails for an entry that hands back a function', () => {
        // a stand-in entry, guarded as the real ones are: the sweep must catch it
        const leaky = guarded('leaky', () => () => 'x');
        expect(() => sweepEntry('leaky', leaky, 'filter')).toThrow(/leaky handed back a function/);
        const nested = guarded('nested', () => [[{deep: {deeper: {deepest: {fn: () => 1}}}}]]);
        expect(() => sweepEntry('nested', nested, 'filter')).toThrow(/reaches a function/);
    });

    // the same through member lookup: every name on every sample gives nothing, data, or a callable the rules made,
    // and that callable's result is not a function
    const NAMES = [...new Set([...Object.getOwnPropertyNames(Object.prototype), ...Object.getOwnPropertyNames(String.prototype),
        ...Object.getOwnPropertyNames(Number.prototype), ...Object.getOwnPropertyNames(Array.prototype), 'prototype', '__proto__', 'length', '0', 'k', 'n'])];
    it('member lookup hands out no function but its own callables, whose results are not functions', () => {
        const {memberLookup, handedOut} = templateSurface() as any;
        const samples = [...SAMPLES, ['x'], Object.freeze(['y'])];
        for (const sample of samples) for (const name of NAMES) {
            const value = memberLookup(sample, name);
            if (typeof value !== 'function') continue;
            expect(handedOut(value), `${String(sample)}.${name}`).toBe(true);
            for (const args of [[], [','], [1], ['a', 'b']]) {
                let result: any;
                try {result = value(...args);} catch {continue;}
                for (const item of reachable(result)) expect(typeof item, `${String(sample)}.${name}()`).not.toBe('function');
            }
        }
    });

    it.each([
        // the reviewer's routes through the filter and test tables
        ['{% set c = 0 | valueOf %}{{ c.env is defined }}', /filter not found: valueOf|cannot use the name/],
        ['{{ ("1" | constructor) is defined }}', /filter not found: constructor|cannot use the name/],
        ['{{ 0 is valueOf }}', /test not found: valueOf|cannot use the name/],
        ['{{ x | toString }}', /filter not found: toString|cannot use the name/],
        ['{{ x is constructor }}', /test not found: constructor|cannot use the name/],
        ['{{ cycler("a").next() }}', /undefined or falsey/],
        ['{{ hasOwnProperty("x") }}', /undefined or falsey|cannot use the name/],
        ['{% include "x" %}', /.+/],
        ['{% import "x" as y %}', /.+/],
        ['{{ range.constructor("globalThis.markerReached = 1")() }}', /undefined or falsey/],
        ['{{ joiner().constructor("globalThis.markerReached = 1")() }}', /undefined or falsey/],
        ['{{ ["a"] | sort(false, false, "constructor") }}', /.*/],
        ['{% for i in range("a", "n") %}{% endfor %}', /range takes numbers/],
        ['{% for i in range(1e9) %}{% endfor %}', /at most 100000/],
    ])('refuses %s', (template, error) => {
        let output = '';
        try {output = renderJobTemplate(template, {x: 1});} catch (e) {expect(String(e)).toMatch(error); return;}
        expect(output).not.toMatch(/function|native code/);
    });

    it('refuses a filter given a function', () => {
        expect(() => renderJobTemplate('{% macro m() %}x{% endmacro %}{{ [1] | join(m) }}', {})).toThrow(/does not take a function/);
    });
});

describe('what honest templates used before', () => {
    it.each([
        ['{{ name.replace("a", "b") }}', 'bbc'],
        ['{{ name.replaceAll("b", "x") }}', 'axc'],
        ['{{ name.substr(1, 1) }} {{ name.at(-1) }} {{ name.charCodeAt(0) }}', 'b c 97'],
        ['{{ name.match("b").length }} {{ name.search("c") }}', '1 2'],
        ['{{ " x ".trimLeft() }}|{{ " x ".trimRight() }}|', 'x | x|'],
        ['{{ name.localeCompare("abz") }} {{ name.normalize() }}', '-1 abc'],
        ['{{ (1234.5).toLocaleString("en-US") }}', '1,234.5'],
        ['{{ "a,b".split(r/,/).join("+") }}', 'a+b'],
        ['{% macro m() %}abc{% endmacro %}{{ m().toUpperCase() }} {{ (name | safe).toUpperCase() }}', 'ABC ABC'],
        ['{% set out = [] %}{% for m in modules %}{% set _ = out.push(m) %}{% endfor %}{{ out.reverse().join(",") }}', 'b,a'],
        ['{% set copy = modules.slice(0) %}{{ copy.sort().pop() }}', 'b'],
        ['{% set j = joiner("+") %}{% for m in modules %}{{ j() }}{{ m }}{% endfor %}', 'a+b'],
        ['{% for i in range(3) %}{{ loop.index }}{% endfor %} {{ name | replace("b", "x") | upper }}', '123 AXC'],
    ])('%s', (template, expected) => {
        expect(renderJobTemplate(template, {name: 'abc', modules: ['a', 'b']})).toBe(expected);
    });
});

describe('names the template library binds itself', () => {
    const before = Object.getOwnPropertyNames(Object.prototype).sort();
    afterEach(() => expect(Object.getOwnPropertyNames(Object.prototype).sort()).toEqual(before));

    // the reviewer's cases (audit4/rereview3/webapp-templates.md B1): each refused, nothing written to Object.prototype
    it.each([
        '{% macro m(constructor) %}{{ constructor }}{% endmacro %}{{ m() }}',
        '{% macro m(__proto__) %}{{ __proto__ }}{% endmacro %}{{ m() }}',
        '{% macro m(constructor) %}{% macro n(constructor) %}{{ constructor }}{% endmacro %}{{ n({"__keywords": true, "__proto__": constructor}) }}{% endmacro %}{{ m() }}',
        '{% macro n(x) %}{{ x }}{% endmacro %}{{ n(__proto__=1) }}',
        '{% set d = {"__proto__": ("a" | safe), "val": "zz"} %}{{ d.toUpperCase() }}',
        // a key written with backslash escapes parses to __proto__ without that text in the source
        '{% set d = {"\\_\\_proto__": ("a" | safe), "val": "zz"} %}{{ d.toUpperCase() }}',
        '{% set __proto__ = [] %}{{ push }}',
        '{% macro m(__proto__) %}{% for loop in [__proto__] %}{% endfor %}{% endmacro %}{{ m() }}',
        '{% for loop in [{}] %}{% endfor %}',
        '{% for x in [1] %}{% set loop = {} %}{% endfor %}',
        '{% set __proto__ = [] %}{{ {"toString": pop, "length": 1, "0": "called"} }}',
    ])('refuses %s', template => {
        let output = '';
        try {output = renderJobTemplate(template, {});} catch (error) {
            expect(String(error)).toMatch(/cannot (use the (name|key)|name a variable loop)/);
            return;
        }
        expect(output).not.toMatch(/native code|called/);
    });

    it('gives a macro parameter the call leaves out no inherited value', () => {
        expect(renderJobTemplate('{% macro m(a, b) %}[{{ a }}|{{ b }}]{% endmacro %}{{ m(1) }}{{ m(b=2) }}', {})).toBe('[1|][|2]');
    });
});

describe('the work one render may take', () => {
    it('stops a template that loops far beyond any job script', () => {
        expect(() => renderJobTemplate('{% for i in range(100000) %}{% for j in range(100000) %}{% endfor %}{% endfor %}', {}))
            .toThrow(new RegExp(`more than ${MAX_RENDER_STEPS.toLocaleString('en-US')} loop steps`));
    });

    it('stops a template that writes far more than any job script', () => {
        expect(() => renderJobTemplate('{% for i in range(20000) %}{{ "x".repeat(1000) }}{% endfor %}', {}))
            .toThrow(new RegExp(`more than ${MAX_RENDER_OUTPUT.toLocaleString('en-US')} characters`));
    });

    it('leaves a large honest loop alone', () => {
        expect(renderJobTemplate('{% for i in range(5000) %}{{ i }}\n{% endfor %}', {}).split('\n')).toHaveLength(5001);
    });
});

describe('the bundled, demo and documented templates', () => {
    const plain = new nunjucks.Environment([], {autoescape: false});
    const docs = readFileSync('../../../../docs/modules/hpc-workloads/admin-documentation/create-web-based-job-submission-workflows.md', 'utf8');
    const documented = docs.slice(docs.indexOf('```\n#PBS -N {{ job_name }}') + 4).split('```')[0];
    const demo = readFileSync('../../idea-scheduler/src/ideascheduler/app/scheduler_default_settings.py', 'utf8');
    const demoLines = [...demo.slice(demo.indexOf("job_script_type='jinja2'")).matchAll(/^\s+'(#!\/bin\/bash|#PBS[^']*|# [^']*|\/bin\/sleep[^']*)',$/gm)].map(match => match[1]).slice(0, 8).join('\n');
    const values = {queue_name: 'normal', job_name: 'marker', project_name: 'p', instance_type: 'c5.large', wall_time: '01:00:00', force_ri: false,
        input_file: '/data/in.dat', user_pre_exec: 'echo pre', binary_location: '/apps/solver', param1: 1, param2: 'two', param3: 3.5, user_post_exec: 'echo post',
        sleep_time: 60, version: '1.2', cpus: 4, parameter_1: 'p1', backup_enabled: true, bucket_to_archive: 'bucket'};
    it.each([
        ['sample-job-script-jinja2.txt', readFileSync('src/pages/hpc/sample-job-script-jinja2.txt', 'utf8')],
        ['scheduler demo application', demoLines],
        ['documented example', documented],
    ])('render as the unrestricted library does: %s', (_, template) => {
        expect(template.length).toBeGreaterThan(100);
        expect(renderJobTemplate(template, values)).toBe(plain.renderString(template, values));
    });
});
