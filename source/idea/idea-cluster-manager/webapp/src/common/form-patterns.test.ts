import {describe, expect, it} from 'vitest';
import {readFileSync} from 'node:fs';
import {DESKTOP_NAME_PATTERN, EMAIL_TEMPLATE_NAME_PATTERN, GROUP_OR_PROJECT_NAME_PATTERN, patternOrUnchanged, PROJECT_TITLE_PATTERN, USERNAME_PATTERN} from './form-patterns';

// the patterns these replaced, kept to prove the same names pass and fail
const CASES: [string, string, string, string[]][] = [
    ['group name', GROUP_OR_PROJECT_NAME_PATTERN, '^([a-z0-9-]+){3,64}$', ['abc', 'a-b', 'ab', 'a', '', 'ABC', 'a_b', 'a b', 'x'.repeat(100)]],
    ['project code', GROUP_OR_PROJECT_NAME_PATTERN, '^([a-z0-9-]+){3,18}$', ['abc', 'proj-1', 'ab', 'Abc', 'x'.repeat(40)]],
    ['project title', PROJECT_TITLE_PATTERN, '^([A-Za-z0-9- ]+){3,32}$', ['My Project', 'ab', 'a b', 'bad_title', 'x'.repeat(60)]],
    ['template name', EMAIL_TEMPLATE_NAME_PATTERN, '^([a-z0-9-._]+){3,32}$', ['a.b_c-d', 'ab', 'ABC', 'a b', 'x'.repeat(50)]]
];

describe.each(CASES)('%s', (_, pattern, previous, samples) => {
    it.each(samples)('agrees with the old pattern on "%s"', sample => {
        expect(new RegExp(pattern).test(sample)).toBe(new RegExp(previous).test(sample));
    });

    it('rejects a long name with a late bad character at once', () => {
        const start = performance.now();
        expect(new RegExp(pattern).test(`${'a'.repeat(5000)}!`)).toBe(false);
        expect(performance.now() - start).toBeLessThan(50);
    });
});

it('leaves no nested repeat in the pages that use these', () => {
    const pages = import.meta.glob(['../pages/user-management/groups.tsx', '../pages/cluster-admin/projects.tsx', '../pages/cluster-admin/email-templates.tsx'], {query: '?raw', import: 'default', eager: true}) as Record<string, string>;
    expect(Object.keys(pages)).toHaveLength(3);
    Object.values(pages).forEach(source => expect(source).not.toMatch(/\+\)\{\d/));
});

describe('rules the server also enforces', () => {
    // read from the server source, so the two cannot drift apart unnoticed
    const server = (path: string, name: string) => readFileSync(path, 'utf8').match(new RegExp(`${name} = (?:re\\.compile\\()?r'([^']+)'`))![1];
    const username = server('../src/ideaclustermanager/app/accounts/auth_constants.py', 'USERNAME_REGEX');
    const desktop = server('../../idea-virtual-desktop-controller/src/ideavirtualdesktopcontroller/app/sessions/constants.py', 'SESSION_NAME_PATTERN');

    it('uses the server username rule, written without a lookbehind', () => {
        expect(USERNAME_PATTERN).not.toContain('(?<');
        const alphabet = ['a', 'z', '0', '9', '.', '_', '-', 'A', ' '];
        const samples = new Set<string>(['', 'x'.repeat(21)]);
        const grow = (prefix: string) => {for (const c of alphabet) {const next = prefix + c; samples.add(next); if (next.length < 4) grow(next);}};
        grow('');
        for (const sample of samples) expect(new RegExp(USERNAME_PATTERN).test(sample), sample).toBe(new RegExp(username).test(sample));
        for (const sample of ['j-smith', 'a.b_c', 'x'.repeat(20)]) expect(new RegExp(USERNAME_PATTERN).test(sample), sample).toBe(true);
        for (const sample of ['x'.repeat(21), '-ab', 'a..b', 'Ab', 'ab']) expect(new RegExp(USERNAME_PATTERN).test(sample), sample).toBe(false);
    });

    it('uses the server desktop name rule', () => {
        expect(DESKTOP_NAME_PATTERN).toBe(`^${desktop}$`);
    });

    it('accepts the current value unchanged, and only that value, outside the rule', () => {
        const current = "Marker's (desk) [v1.2]*";
        const pattern = new RegExp(patternOrUnchanged(DESKTOP_NAME_PATTERN, current));
        expect(pattern.test(current)).toBe(true);
        expect(pattern.test('marker desk')).toBe(true);
        expect(pattern.test("Marker's (desk) [v1.2]")).toBe(false);
        expect(pattern.test("x" + current)).toBe(false);
        expect(patternOrUnchanged(DESKTOP_NAME_PATTERN, undefined)).toBe(DESKTOP_NAME_PATTERN);
    });
});

it('asks only for a software stack name, as the server does (virtual_desktop_api.py validate_create_software_stack_request)', () => {
    const pages = import.meta.glob(['../pages/virtual-desktops/virtual-desktop-software-stacks.tsx', '../pages/virtual-desktops/virtual-desktop-sessions.tsx', '../pages/virtual-desktops/forms/virtual-desktop-software-stack-edit-form.tsx'], {query: '?raw', import: 'default', eager: true}) as Record<string, string>;
    expect(Object.keys(pages)).toHaveLength(3);
    for (const [path, source] of Object.entries(pages)) {
        const field = source.match(/name: 'name',[\s\S]*?validate: \{([\s\S]*?)\}/);
        expect(field, path).not.toBeNull();
        expect(field![1].trim(), path).toBe('required: true');
    }
});
