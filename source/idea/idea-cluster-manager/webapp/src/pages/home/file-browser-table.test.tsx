/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

/** Row order: directories ahead of files, whichever column is sorted and in whichever direction. The
 * characterization of the page as a whole lives in file-browser.test.tsx, which asserts no order. */

import { render, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import FileBrowser from './file-browser';
import { FileBrowserEntry, sortEntries } from './file-browser-table';
import { initTestAppContext } from '../../test-support';

const HOME = '/home/testuser';

const BY_NAME = {
    sortingComparator: (a: FileBrowserEntry, b: FileBrowserEntry) =>
        a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
};

const BY_SIZE = {
    sortingComparator: (a: FileBrowserEntry, b: FileBrowserEntry) => (a.size ?? 0) - (b.size ?? 0)
};

function names(entries: FileBrowserEntry[]): string[] {
    return entries.map((entry) => entry.name);
}

function file(name: string, size = 0): FileBrowserEntry {
    return { id: name, name: name, isDir: false, size: size };
}

function folder(name: string): FileBrowserEntry {
    return { id: name, name: name, isDir: true };
}

// ---------------------------------------------------------------------------
// Harness for the page-level checks
// ---------------------------------------------------------------------------

function entry(name: string, overrides: { is_dir?: boolean; size?: number } = {}) {
    return {
        file_id: name,
        name: name,
        size: 0,
        is_dir: false,
        is_hidden: false,
        mod_date: '2026-01-02T03:04:05.000Z',
        ...overrides
    };
}

const PAGE_PROPS = {
    ideaPageId: 'file-browser',
    toolsOpen: false,
    tools: null,
    onToolsChange: () => {},
    onPageChange: () => {},
    sideNavHeader: { text: 'IDEA', href: '#/' },
    sideNavItems: [],
    onSideNavChange: () => {},
    onFlashbarChange: () => {},
    flashbarItems: []
} as any;

function renderFileBrowser(listing: any[]) {
    const context = initTestAppContext();
    const clusterSettings = context.getClusterSettingsService() as any;
    vi.spyOn(clusterSettings, 'getModuleSettings').mockRejectedValue({ errorCode: 'MODULE_NOT_FOUND' });
    vi.spyOn(clusterSettings, 'isSchedulerDeployed').mockReturnValue(false);

    const client = context.client().fileBrowser();
    vi.spyOn(client, 'listFiles').mockResolvedValue({ cwd: HOME, listing: listing } as any);

    const rendered = render(
        <MemoryRouter initialEntries={['/']}>
            <FileBrowser {...PAGE_PROPS} />
        </MemoryRouter>
    );

    return { ...rendered, user: userEvent.setup() };
}

function browserRoot(): HTMLElement {
    const roots = Array.from(document.querySelectorAll('.soca-file-browser')) as HTMLElement[];
    const rendered = roots.filter((root) => (root.textContent || '').trim().length > 0);
    return rendered[rendered.length - 1] ?? roots[0];
}

/** The names on screen, in the order they are listed. */
function listedNames(): string[] {
    return (Array.from(browserRoot().querySelectorAll('.soca-file-browser-name-text')) as HTMLElement[]).map((node) =>
        (node.textContent || '').trim()
    );
}

async function sortBy(user: ReturnType<typeof userEvent.setup>, column: string | RegExp) {
    await user.click(within(browserRoot()).getByRole('button', { name: column }));
}

// ---------------------------------------------------------------------------

describe('listing order', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    describe('sortEntries', () => {
        it('puts directories ahead of files, each group by name and case-insensitively', () => {
            const entries = [file('Zeta.txt'), folder('reports'), file('alpha.txt'), folder('Archive')];

            expect(names(sortEntries(entries, BY_NAME, false))).toEqual([
                'Archive',
                'reports',
                'alpha.txt',
                'Zeta.txt'
            ]);
        });

        it('keeps directories ahead of files when the sort is reversed', () => {
            const entries = [file('Zeta.txt'), folder('reports'), file('alpha.txt'), folder('Archive')];

            expect(names(sortEntries(entries, BY_NAME, true))).toEqual([
                'reports',
                'Archive',
                'Zeta.txt',
                'alpha.txt'
            ]);
        });

        it('keeps directories ahead of files under another column, both ways round', () => {
            const entries = [file('big.bin', 900), folder('reports'), file('small.bin', 10)];

            expect(names(sortEntries(entries, BY_SIZE, false))).toEqual(['reports', 'small.bin', 'big.bin']);
            expect(names(sortEntries(entries, BY_SIZE, true))).toEqual(['reports', 'big.bin', 'small.bin']);
        });

        it('does not reorder the array it was given', () => {
            const entries = [file('b.txt'), folder('a')];

            sortEntries(entries, BY_NAME, false);

            expect(names(entries)).toEqual(['b.txt', 'a']);
        });
    });

    describe('in the page', () => {
        const LISTING = [
            entry('notes.txt', { size: 900 }),
            entry('reports', { is_dir: true }),
            entry('archive.txt', { size: 10 }),
            entry('Backups', { is_dir: true })
        ];

        it('lists the folders first, whatever order the API returned', async () => {
            renderFileBrowser(LISTING);

            await waitFor(() => expect(listedNames().length).toBe(4));
            expect(listedNames()).toEqual(['Backups', 'reports', 'archive.txt', 'notes.txt']);
        });

        it('keeps the folders together when the user sorts by size', async () => {
            const { user } = renderFileBrowser(LISTING);

            await waitFor(() => expect(listedNames().length).toBe(4));

            await sortBy(user, /Size/);
            await waitFor(() => expect(listedNames()).toEqual(['Backups', 'reports', 'archive.txt', 'notes.txt']));

            await sortBy(user, /Size/);
            await waitFor(() => expect(listedNames()).toEqual(['Backups', 'reports', 'notes.txt', 'archive.txt']));
        });
    });
});
