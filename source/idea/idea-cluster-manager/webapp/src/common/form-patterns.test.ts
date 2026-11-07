import {describe, expect, it} from 'vitest';
import {EMAIL_TEMPLATE_NAME_PATTERN, GROUP_OR_PROJECT_NAME_PATTERN, PROJECT_TITLE_PATTERN} from './form-patterns';

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
