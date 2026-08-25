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

/** The editable path bar: the trail of ancestors, and the field a whole path can be typed or pasted
 * into. The behavioural characterization of the page as a whole lives in file-browser.test.tsx. */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { vi } from 'vitest';
import { ToastContainer } from 'react-toastify';
import FileBrowser from './file-browser';
import { describeListingFailure, normalizePathInput, pathSegments, scrollTrailToEnd } from './file-browser-path';
import { initTestAppContext } from '../../test-support';

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

const HOME = '/home/testuser';

function entry(name: string, overrides: { is_dir?: boolean } = {}) {
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

function UrlProbe() {
    const location = useLocation();
    return <span data-testid="url-query">{decodeURIComponent(location.search)}</span>;
}

function renderFileBrowser(options: { listing?: any[]; cwd?: string } = {}) {
    const context = initTestAppContext();
    const clusterSettings = context.getClusterSettingsService() as any;
    vi.spyOn(clusterSettings, 'getModuleSettings').mockRejectedValue({ errorCode: 'MODULE_NOT_FOUND' });
    vi.spyOn(clusterSettings, 'isSchedulerDeployed').mockReturnValue(false);

    const client = context.client().fileBrowser();
    const api = {
        listFiles: vi.spyOn(client, 'listFiles').mockResolvedValue({
            cwd: options.cwd ?? HOME,
            listing: options.listing ?? [entry('notes.txt')]
        } as any)
    };

    const rendered = render(
        <MemoryRouter initialEntries={['/']}>
            <UrlProbe />
            <ToastContainer />
            <FileBrowser {...PAGE_PROPS} />
        </MemoryRouter>
    );

    return { ...rendered, api, user: userEvent.setup() };
}

/** The rendered file browser: the page mounts one per tab, only one has content. */
function browserRoot(): HTMLElement {
    const roots = Array.from(document.querySelectorAll('.soca-file-browser')) as HTMLElement[];
    const rendered = roots.filter((root) => (root.textContent || '').trim().length > 0);
    return rendered[rendered.length - 1] ?? roots[0];
}

/** The trail, root first. */
function trail(): string[] {
    const nav = browserRoot().querySelector('nav');
    return nav == null
        ? []
        : (Array.from(nav.querySelectorAll('button')) as HTMLElement[]).map((button) => (button.textContent || '').trim());
}

function editAffordance(): HTMLElement {
    return within(browserRoot()).getByRole('button', { name: 'Edit path' });
}

function pathField(): HTMLInputElement | null {
    return within(browserRoot()).queryByLabelText('Path') as HTMLInputElement | null;
}

function requirePathField(): HTMLInputElement {
    const field = pathField();
    if (field == null) {
        throw new Error(`no path field. showing: ${browserRoot().textContent}`);
    }
    return field;
}

async function waitForRow(name: string) {
    await waitFor(() => expect(within(browserRoot()).queryAllByText(name).length).toBeGreaterThan(0), { timeout: 10000 });
}

/** Click the empty space beside the trail, which is what turns it into a field. */
async function startTyping(user: ReturnType<typeof userEvent.setup>) {
    await user.click(editAffordance());
    await waitFor(() => expect(pathField()).not.toBeNull());
}

// ---------------------------------------------------------------------------

describe('path bar', () => {
    afterEach(() => {
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    describe('reading a path', () => {
        it('splits a path into every ancestor, root first', () => {
            expect(pathSegments('/data/projects/alpha')).toEqual([
                { name: 'root', path: '/' },
                { name: 'data', path: '/data' },
                { name: 'projects', path: '/data/projects' },
                { name: 'alpha', path: '/data/projects/alpha' }
            ]);
        });

        it('shows nothing but the root at the root', () => {
            expect(pathSegments('/')).toEqual([{ name: 'root', path: '/' }]);
        });

        it('starts the trail scrolled to the current directory, not the root', () => {
            const node = { scrollLeft: 0, scrollWidth: 4200 };

            scrollTrailToEnd(node);

            expect(node.scrollLeft).toBe(4200);
        });
    });

    describe('typing a path', () => {
        it('tolerates trailing slashes, doubled slashes and surrounding whitespace', () => {
            expect(normalizePathInput('  /data/projects/  ')).toEqual({ path: '/data/projects', error: null });
            expect(normalizePathInput('/data//projects')).toEqual({ path: '/data/projects', error: null });
            expect(normalizePathInput('/data/projects///')).toEqual({ path: '/data/projects', error: null });
            expect(normalizePathInput('//')).toEqual({ path: '/', error: null });
            expect(normalizePathInput('/')).toEqual({ path: '/', error: null });
        });

        it('refuses anything that is not an absolute path, by name', () => {
            for (const typed of ['data/projects', '~/projects', './projects', '', '   ']) {
                const result = normalizePathInput(typed);
                expect(result.path).toBeNull();
                expect(result.error).toBe('Enter an absolute path, starting with /.');
            }
        });

        it('refuses "..", which the server rejects outright, rather than guessing at it', () => {
            const result = normalizePathInput('/data/../projects');

            expect(result.path).toBeNull();
            expect(result.error).toMatch(/\.\./);
        });
    });

    describe('explaining a failure', () => {
        it('names all three things the API means by UNAUTHORIZED_ACCESS', () => {
            const message = describeListingFailure('/nope', {
                errorCode: 'UNAUTHORIZED_ACCESS',
                message: 'Unauthorized Access'
            });

            expect(message).toContain('/nope');
            expect(message).toMatch(/does not exist/);
            expect(message).toMatch(/not a directory/);
            expect(message).toMatch(/access/);
        });

        it('passes any other failure through with its own message', () => {
            expect(describeListingFailure('/data', { errorCode: 'GENERAL_ERROR', message: 'backend is down' })).toBe(
                'Cannot open /data: backend is down'
            );
        });
    });

    describe('in the page', () => {
        it('navigates to a path the user pastes in', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await startTyping(user);
            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({
                cwd: '/data/projects/alpha',
                listing: [entry('run.log')]
            } as any);

            // Trailing slash and trailing space, as a path pasted out of a log
            // or a chat message arrives.
            await user.paste('/data/projects/alpha/ ');
            await user.keyboard('{Enter}');

            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: '/data/projects/alpha' }));
            await waitForRow('run.log');
            expect(trail()).toEqual(['root', 'data', 'projects', 'alpha']);
            expect(pathField()).toBeNull();
            expect(screen.getByTestId('url-query').textContent).toBe('?cwd=/data/projects/alpha');
        });

        it('says why a path did not open, and leaves the listing where it was', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await startTyping(user);
            api.listFiles.mockClear();
            api.listFiles.mockRejectedValue({ errorCode: 'UNAUTHORIZED_ACCESS', message: 'Unauthorized Access' });

            await user.paste('/data/nope');
            await user.keyboard('{Enter}');

            expect(
                await within(browserRoot()).findByText(
                    /Cannot open \/data\/nope\. It does not exist, is not a directory, or you do not have access to it\./
                )
            ).toBeInTheDocument();
            // The directory that is on screen is still the real one, and the
            // field is still open on the path that failed.
            await waitForRow('notes.txt');
            expect(trail()).toEqual([]);
            expect(requirePathField().value).toBe('/data/nope');
            // Attached to the field, not merely somewhere on the page.
            expect(requirePathField()).toHaveAttribute('aria-invalid', 'true');
            const describedBy = requirePathField().getAttribute('aria-describedby') || '';
            expect(
                describedBy
                    .split(' ')
                    .map((id) => document.getElementById(id)?.textContent || '')
                    .join(' ')
            ).toMatch(/Cannot open \/data\/nope/);
            // Said once, where the user is looking, not also as a toast that
            // repeats the error code's own wording.
            expect(screen.queryByText('Unauthorized Access')).toBeNull();
        });

        it('refuses a relative path without asking the server', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await startTyping(user);
            api.listFiles.mockClear();

            await user.paste('data/projects');
            await user.keyboard('{Enter}');

            expect(await within(browserRoot()).findByText('Enter an absolute path, starting with /.')).toBeInTheDocument();
            expect(api.listFiles).not.toHaveBeenCalled();
        });

        it('cancels on Escape, leaving the trail as it was', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await startTyping(user);
            api.listFiles.mockClear();

            await user.paste('/data/projects');
            await user.keyboard('{Escape}');

            await waitFor(() => expect(pathField()).toBeNull());
            expect(trail()).toEqual(['root', 'home', 'testuser']);
            expect(api.listFiles).not.toHaveBeenCalled();
        });

        it('cancels when the user clicks away without submitting', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await startTyping(user);
            api.listFiles.mockClear();

            await user.paste('/data/projects');
            await user.click(within(browserRoot()).getByRole('button', { name: 'Refresh' }));

            await waitFor(() => expect(pathField()).toBeNull());
            expect(trail()).toEqual(['root', 'home', 'testuser']);
            expect(api.listFiles).not.toHaveBeenCalledWith({ cwd: '/data/projects' });
        });

        it('is reachable and operable without a mouse, and opens on the whole current path', async () => {
            const { user } = renderFileBrowser();

            await waitForRow('notes.txt');
            editAffordance().focus();
            expect(document.activeElement).toBe(editAffordance());

            await user.keyboard('{Enter}');

            await waitFor(() => expect(pathField()).not.toBeNull());
            const field = requirePathField();
            expect(document.activeElement).toBe(field);
            expect(field.value).toBe(HOME);
            // Selected, so that typing or pasting replaces the path rather than
            // appending to it.
            expect(field.selectionStart).toBe(0);
            expect(field.selectionEnd).toBe(HOME.length);
        });

        it('still navigates from an ancestor in the trail', async () => {
            const { api, user } = renderFileBrowser({ cwd: '/data/projects/alpha', listing: [entry('run.log')] });

            await waitForRow('run.log');
            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: '/data/projects', listing: [entry('beta', { is_dir: true })] } as any);

            const nav = browserRoot().querySelector('nav')!;
            await user.click(
                (Array.from(nav.querySelectorAll('button')) as HTMLElement[]).find(
                    (button) => (button.textContent || '').trim() === 'projects'
                )!
            );

            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: '/data/projects' }));
            expect(trail()).toEqual(['root', 'data', 'projects']);
        });
    });
});
