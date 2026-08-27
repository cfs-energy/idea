/** Behavioural characterization of the File Browser page: every assertion is something a user can
 * observe or something the API receives, so a list-component rebuild that passes this file unchanged
 * preserved the behaviour users depend on. The DRIVER section is the only component-coupled code. */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { vi } from 'vitest';
import { ToastContainer } from 'react-toastify';
import Uppy from '@uppy/core';
import FileBrowser from './file-browser';
import { initTestAppContext } from '../../test-support';

// ---------------------------------------------------------------------------
// jsdom environment
// ---------------------------------------------------------------------------

// jsdom gives every element a 0x0 box, so a virtualizing list concludes the viewport is empty and
// renders zero rows. Give the document a real viewport. A non-virtualizing replacement is unaffected.
const REAL_GET_BOUNDING_CLIENT_RECT = Element.prototype.getBoundingClientRect;
const VIEWPORT = { width: 1280, height: 800, top: 0, left: 0, bottom: 800, right: 1280, x: 0, y: 0 };

beforeAll(() => {
    Element.prototype.getBoundingClientRect = function () {
        return { ...VIEWPORT, toJSON: () => VIEWPORT } as DOMRect;
    };
});

afterAll(() => {
    Element.prototype.getBoundingClientRect = REAL_GET_BOUNDING_CLIENT_RECT;
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const HOME = '/home/testuser';

interface EntryOverrides {
    size?: number;
    is_dir?: boolean;
    is_hidden?: boolean;
    mod_date?: string;
}

/** One entry of a FileBrowser.ListFiles listing, as the backend emits it. */
function entry(name: string, overrides: EntryOverrides = {}) {
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

function dir(name: string) {
    return entry(name, { is_dir: true });
}

const PAGE_PROPS = {
    ideaPageId: 'file-browser',
    toolsOpen: false,
    tools: null,
    onToolsChange: () => {},
    onPageChange: () => {},
    sideNavHeader: { text: 'IDEA', href: '#/' },
    sideNavItems: [],
    onSideNavChange: () => {}
} as any;

/** Renders the current URL query so tests can assert on the address bar. */
function UrlProbe() {
    const location = useLocation();
    return <span data-testid="url-query">{decodeURIComponent(location.search)}</span>;
}

interface HarnessOptions {
    listing?: any[];
    cwd?: string;
    url?: string;
    schedulerDeployed?: boolean;
}

/** Renders the page with every backend call stubbed. Returns the API spies, the flashbar spy and a
 * userEvent session. */
function renderFileBrowser(options: HarnessOptions = {}) {
    const listing = options.listing ?? [dir('reports'), entry('notes.txt', { size: 12 })];
    const cwd = options.cwd ?? HOME;

    const context = initTestAppContext();
    const clusterSettings = context.getClusterSettingsService() as any;
    // The Bastion Host module gates the File Transfer tab; absent by default.
    vi.spyOn(clusterSettings, 'getModuleSettings').mockRejectedValue({ errorCode: 'MODULE_NOT_FOUND' });
    vi.spyOn(clusterSettings, 'isSchedulerDeployed').mockReturnValue(options.schedulerDeployed === true);

    const client = context.client().fileBrowser();
    const api = {
        listFiles: vi.spyOn(client, 'listFiles').mockResolvedValue({ cwd: cwd, listing: listing } as any),
        readFile: vi.spyOn(client, 'readFile').mockResolvedValue({ content: btoa('hello') } as any),
        saveFile: vi.spyOn(client, 'saveFile').mockResolvedValue({} as any),
        createFile: vi.spyOn(client, 'createFile').mockResolvedValue({} as any),
        deleteFiles: vi.spyOn(client, 'deleteFiles').mockResolvedValue({} as any),
        renameFile: vi.spyOn(client, 'renameFile').mockResolvedValue({} as any),
        downloadFiles: vi.spyOn(client, 'downloadFiles').mockResolvedValue({
            download_url: 'https://cluster.example/archive.zip'
        } as any),
        checkFilesPermissions: vi.spyOn(client, 'checkFilesPermissions').mockResolvedValue({ results: [] } as any)
    };

    vi.spyOn(context.auth(), 'getAccessToken').mockResolvedValue('test-access-token');

    // The signed-download-URL exchange is a plain fetch, not an IDEA RPC.
    const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ download_url: 'https://cluster.example/signed/notes.txt' })
    });
    vi.stubGlobal('fetch', fetchMock);

    // Both a download and "open in a new tab" end in a synthetic anchor click.
    const anchorClicks: { href: string; download: string | null; target: string | null }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
        anchorClicks.push({
            href: this.getAttribute('href') || '',
            download: this.getAttribute('download'),
            target: this.getAttribute('target')
        });
    });

    // Long-running downloads report progress through the app flashbar. The page
    // replaces the whole item list on each step, so keep every item ever shown:
    // the intermediate "preparing" state is part of the behaviour.
    const flashbarItems: any[] = [];
    const flashbarSeen: { header: string; content: string }[] = [];
    const onFlashbarChange = vi.fn((event: any) => {
        flashbarItems.length = 0;
        flashbarItems.push(...(event.items || []));
        flashbarItems.forEach((item) => flashbarSeen.push({ header: item.header, content: String(item.content) }));
    });

    const rendered = render(
        <MemoryRouter initialEntries={[options.url ?? '/']}>
            <UrlProbe />
            <ToastContainer />
            <FileBrowser {...PAGE_PROPS} flashbarItems={flashbarItems} onFlashbarChange={onFlashbarChange} />
        </MemoryRouter>
    );

    const user = userEvent.setup();
    // userEvent installs the clipboard stub; spy on it afterwards so the page's
    // own success path still runs.
    const clipboardWrites: string[] = [];
    vi.spyOn(navigator.clipboard, 'writeText').mockImplementation((text: string) => {
        clipboardWrites.push(text);
        return Promise.resolve();
    });

    return {
        ...rendered,
        api,
        context,
        fetchMock,
        anchorClicks,
        clipboardWrites,
        onFlashbarChange,
        flashbarItems,
        flashbarSeen,
        user
    };
}

function urlQuery(): string {
    return screen.getByTestId('url-query').textContent || '';
}

// ===========================================================================
// DRIVER - the only component-coupled code here. Replacing the list component
// means re-pointing the functions below and nothing else.

/** The rendered file browser. The page mounts one per tab and only the active tab's panel has content. */
function browserRoot(): HTMLElement {
    const roots = Array.from(document.querySelectorAll('.soca-file-browser')) as HTMLElement[];
    const rendered = roots.filter((root) => (root.textContent || '').trim().length > 0);
    return rendered[rendered.length - 1] ?? roots[0];
}

/** Everything the file browser is currently showing, as one string. */
function browserText(): string {
    const root = browserRoot();
    return root ? root.textContent || '' : '';
}

/** The row for a named entry. Excludes the toolbar and the breadcrumb trail so a folder called
 * "reports" is not confused with a breadcrumb segment called "reports". */
function row(name: string): HTMLElement | null {
    const root = browserRoot();
    if (!root) {
        return null;
    }
    const candidates = Array.from(root.querySelectorAll('*')).filter((element) => {
        if ((element.textContent || '').trim() !== name) {
            return false;
        }
        if (Array.from(element.children).some((child) => (child.textContent || '').trim() === name)) {
            return false;
        }
        return element.closest('button') == null && element.closest('nav') == null;
    });
    return (candidates[candidates.length - 1] as HTMLElement) ?? null;
}

function requireRow(name: string): HTMLElement {
    const found = row(name);
    if (found == null) {
        throw new Error(`no row named "${name}". showing: ${browserText()}`);
    }
    return found;
}

async function waitForRow(name: string) {
    await waitFor(() => expect(row(name)).not.toBeNull(), { timeout: 10000 });
    return requireRow(name);
}

async function waitForNoRow(name: string) {
    await waitFor(() => expect(row(name)).toBeNull(), { timeout: 10000 });
}

/** A toolbar button, addressed the way a user reads it. */
function toolbarButton(label: string): HTMLElement | null {
    const root = browserRoot();
    if (!root) {
        return null;
    }
    const buttons = Array.from(root.querySelectorAll('button')) as HTMLElement[];
    return (
        buttons.find((button) => (button.getAttribute('title') || '').trim() === label) ??
        buttons.find((button) => (button.textContent || '').trim() === label) ??
        null
    );
}

function requireToolbarButton(label: string): HTMLElement {
    const button = toolbarButton(label);
    if (button == null) {
        throw new Error(`no toolbar button "${label}". showing: ${browserText()}`);
    }
    return button;
}

/** A segment of the breadcrumb trail. */
function breadcrumb(name: string): HTMLElement | null {
    const root = browserRoot();
    const nav = root ? root.querySelector('nav') : null;
    if (nav == null) {
        return null;
    }
    return (
        (Array.from(nav.querySelectorAll('button')) as HTMLElement[]).find(
            (button) => (button.textContent || '').trim() === name
        ) ?? null
    );
}

/** The full breadcrumb trail, root first. */
function breadcrumbTrail(): string[] {
    const root = browserRoot();
    const nav = root ? root.querySelector('nav') : null;
    if (nav == null) {
        return [];
    }
    return (Array.from(nav.querySelectorAll('button')) as HTMLElement[]).map((button) =>
        (button.textContent || '').trim()
    );
}

/** The in-directory filter box. */
function searchBox(): HTMLElement {
    const input = browserRoot().querySelector('input[type="text"]') as HTMLElement;
    if (input == null) {
        throw new Error('no search box in the file browser');
    }
    return input;
}

/** True when the element, or an ancestor, is visually hidden. Menus for closed dropdowns stay
 * mounted, so this separates the menu the user is looking at from the ones they are not. */
function isVisuallyHidden(element: Element): boolean {
    let node: Element | null = element;
    while (node) {
        const style = (node as HTMLElement).style;
        if (style && (style.visibility === 'hidden' || style.display === 'none')) {
            return true;
        }
        node = node.parentElement;
    }
    return false;
}

/** An entry in whichever menu is currently open. */
function openMenuItem(label: string): HTMLElement | null {
    const items = Array.from(document.querySelectorAll('[role="menuitem"]')) as HTMLElement[];
    return (
        items.filter((item) => (item.textContent || '').trim() === label).find((item) => !isVisuallyHidden(item)) ?? null
    );
}

/** A dialog, addressed by something written in it, which is how a user refers to one. Every modal
 * here is mounted at all times, so "the visible dialog" is not answerable in jsdom. */
function dialogContaining(text: string | RegExp): HTMLElement | null {
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')) as HTMLElement[];
    return (
        dialogs.find((dialog) => {
            const content = dialog.textContent || '';
            return typeof text === 'string' ? content.includes(text) : text.test(content);
        }) ?? null
    );
}

async function findDialogContaining(text: string | RegExp): Promise<HTMLElement> {
    await waitFor(() => expect(dialogContaining(text)).not.toBeNull(), { timeout: 10000 });
    return dialogContaining(text)!;
}

type User = ReturnType<typeof userEvent.setup>;

/** Open an entry: a folder navigates into it, a file opens it. */
async function openEntry(user: User, name: string) {
    await user.dblClick(requireRow(name));
}

/** Select one entry, replacing any existing selection. */
async function selectEntry(user: User, name: string) {
    await user.click(requireRow(name));
}

/** Add an entry to the current selection. */
async function addToSelection(user: User, name: string) {
    await user.keyboard('{Control>}');
    await user.click(requireRow(name));
    await user.keyboard('{/Control}');
}

/** Shift-click an entry: extends the selection from the last plain click. */
async function selectRangeTo(user: User, name: string) {
    await user.keyboard('{Shift>}');
    await user.click(requireRow(name));
    await user.keyboard('{/Shift}');
}

/** Right-click an entry, without choosing anything. */
async function openContextMenu(user: User, name: string) {
    await user.pointer({ target: requireRow(name), keys: '[MouseRight]' });
    await waitFor(() => expect(openMenuItem('Delete files')).not.toBeNull(), { timeout: 10000 });
}

/** Right-click an entry and choose a named action from its context menu. */
async function chooseFromContextMenu(user: User, name: string, action: string) {
    await user.pointer({ target: requireRow(name), keys: '[MouseRight]' });
    await waitFor(() => expect(openMenuItem(action)).not.toBeNull(), { timeout: 10000 });
    await user.click(openMenuItem(action)!);
}

/** Choose a named entry from the toolbar's own menus, the view and display options that are not
 * per-file actions. */
async function chooseFromToolbarMenu(user: User, label: string) {
    const triggers = (Array.from(browserRoot().querySelectorAll('button')) as HTMLElement[]).filter(
        (button) => (button.textContent || '').trim() === ''
    );
    for (const trigger of triggers) {
        await user.click(trigger);
        try {
            await waitFor(() => expect(openMenuItem(label)).not.toBeNull(), { timeout: 1000 });
        } catch {
            await user.keyboard('{Escape}');
            continue;
        }
        await user.click(openMenuItem(label)!);
        return;
    }
    throw new Error(`no toolbar menu entry "${label}"`);
}

/** Replace the whole contents of a text field. Not clear() + type(): the rename fields fall back to
 * the original name when empty, so clearing one is not something a user can actually do. */
async function replaceText(user: User, field: HTMLElement, value: string) {
    await user.tripleClick(field);
    await user.paste(value);
}

/** The number of entries the browser says the directory holds, with thousands separators removed. */
function reportedItemCount(): number | null {
    const match = browserText().replace(/[,\u00A0\u200B]/g, '').match(/(\d+)\s+items?/);
    return match ? Number(match[1]) : null;
}

/** The empty-directory message. */
function showsEmptyState(): boolean {
    return browserText().includes('Nothing to show');
}

/** Uppy mounts its dashboard outside the React tree; RTL cleanup misses it. */
function removeLeakedUploadDialogs() {
    document.querySelectorAll('.uppy-Root, .uppy-Dashboard, [data-uppy-theme]').forEach((node) => {
        const top = node.closest('body > *');
        if (top && top.parentElement === document.body) {
            top.remove();
        } else {
            node.remove();
        }
    });
}

// ===========================================================================
// END DRIVER. Everything below is behavior.
// ===========================================================================

describe('file browser', () => {
    beforeEach(() => {
        localStorage.clear();
    });

    afterEach(() => {
        removeLeakedUploadDialogs();
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    describe('listing a directory', () => {
        it('lists the entries the API returned', async () => {
            renderFileBrowser({
                listing: [dir('reports'), entry('notes.txt', { size: 12 }), entry('data.csv', { size: 2048 })]
            });

            await waitForRow('notes.txt');
            expect(row('reports')).not.toBeNull();
            expect(row('data.csv')).not.toBeNull();
            expect(reportedItemCount()).toBe(3);
        });

        it('asks for the user default directory when the URL names none', async () => {
            const { api } = renderFileBrowser();

            await waitForRow('notes.txt');
            expect(api.listFiles).toHaveBeenCalledWith({ cwd: undefined });
        });

        it('opens the directory named in the URL', async () => {
            const { api } = renderFileBrowser({
                url: '/?cwd=/data/projects',
                cwd: '/data/projects',
                listing: [entry('run.log', { size: 64 })]
            });

            await waitForRow('run.log');
            expect(api.listFiles).toHaveBeenCalledWith({ cwd: '/data/projects' });
        });

        it('reports an empty directory as empty and lists nothing', async () => {
            renderFileBrowser({ listing: [] });

            await waitFor(() => expect(showsEmptyState()).toBe(true), { timeout: 10000 });
            expect(reportedItemCount()).toBe(0);
            expect(row('notes.txt')).toBeNull();
        });

        it('hides hidden entries until the user asks for them', async () => {
            const { user } = renderFileBrowser({
                listing: [entry('notes.txt', { size: 12 }), entry('.bashrc', { size: 7, is_hidden: true })]
            });

            await waitForRow('notes.txt');
            expect(row('.bashrc')).toBeNull();
            expect(reportedItemCount()).toBe(1);

            await chooseFromToolbarMenu(user, 'Show hidden files');

            await waitForRow('.bashrc');
            expect(reportedItemCount()).toBe(2);
        });
    });

    describe('navigating', () => {
        it('requests the folder a user opens', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('reports');
            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: `${HOME}/reports`, listing: [entry('q1.txt')] } as any);

            await openEntry(user, 'reports');

            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: `${HOME}/reports` }));
            await waitForRow('q1.txt');
        });

        it('shows the current path as a breadcrumb trail', async () => {
            renderFileBrowser({ cwd: '/data/projects/alpha', listing: [entry('run.log')] });

            await waitForRow('run.log');
            expect(breadcrumbTrail()).toEqual(['root', 'data', 'projects', 'alpha']);
        });

        it('requests the parent when a user clicks an ancestor breadcrumb', async () => {
            const { api, user } = renderFileBrowser({ cwd: '/data/projects/alpha', listing: [entry('run.log')] });

            await waitForRow('run.log');
            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: '/data/projects', listing: [dir('alpha'), dir('beta')] } as any);

            await user.click(breadcrumb('projects')!);

            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: '/data/projects' }));
            await waitForRow('beta');
            expect(breadcrumbTrail()).toEqual(['root', 'data', 'projects']);
        });

        it('keeps the current directory in the URL so it can be shared and reloaded', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('reports');
            await waitFor(() => expect(urlQuery()).toBe(`?cwd=${HOME}`));

            api.listFiles.mockResolvedValue({ cwd: `${HOME}/reports`, listing: [entry('q1.txt')] } as any);
            await openEntry(user, 'reports');

            await waitFor(() => expect(urlQuery()).toBe(`?cwd=${HOME}/reports`));
        });

        it('refreshes the current directory on request', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: HOME, listing: [entry('notes.txt'), entry('added.txt')] } as any);

            await user.click(requireToolbarButton('Refresh'));

            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: HOME }));
            await waitForRow('added.txt');
        });
    });

    describe('creating a folder', () => {
        it('creates the folder the user named, then re-lists the directory', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await user.click(requireToolbarButton('Create folder'));

            const dialog = await findDialogContaining('Create New Folder');
            await user.type(within(dialog).getByLabelText(/Folder Name/i), 'quarterly');

            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: HOME, listing: [dir('quarterly'), entry('notes.txt')] } as any);
            await user.click(within(dialog).getByRole('button', { name: 'Submit' }));

            await waitFor(() =>
                expect(api.createFile).toHaveBeenCalledWith({ cwd: HOME, filename: 'quarterly', is_folder: true })
            );
            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: HOME }));
            await waitForRow('quarterly');
        });

        it('shows the server message when the folder cannot be created', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.createFile.mockRejectedValue({ errorCode: 'DISK_QUOTA_EXCEEDED', message: 'Disk quota exceeded' });

            await user.click(requireToolbarButton('Create folder'));
            const dialog = await findDialogContaining('Create New Folder');
            await user.type(within(dialog).getByLabelText(/Folder Name/i), 'quarterly');
            await user.click(within(dialog).getByRole('button', { name: 'Submit' }));

            expect(await within(dialog).findByText('Disk quota exceeded')).toBeInTheDocument();
        });
    });

    describe('uploading', () => {
        it('offers an upload dialog that targets the current directory', async () => {
            const useSpy = vi.spyOn(Uppy.prototype, 'use');
            const { user } = renderFileBrowser({ cwd: '/data/projects' });

            await waitForRow('notes.txt');
            await user.click(requireToolbarButton('Upload files'));

            // The user gets a file-picking dialog...
            await waitFor(() => expect(document.querySelector('.uppy-Dashboard')).not.toBeNull(), { timeout: 10000 });

            // ...pointed at the upload endpoint for the directory they are
            // looking at. Upload is Uppy, which a list-component swap does not
            // touch; this pins the request, not the uploader.
            const uploadOptions = useSpy.mock.calls
                .map((call) => call[1] as any)
                .filter((options) => options && options.endpoint);
            expect(uploadOptions).toHaveLength(1);
            expect(uploadOptions[0].endpoint).toBe(
                'http://localhost:8080/cluster-manager/api/v1/upload?cwd=/data/projects'
            );
            expect(uploadOptions[0].method).toBe('PUT');
            expect(uploadOptions[0].headers.Authorization).toBe('Bearer test-access-token');
        });
    });

    describe('downloading', () => {
        it('downloads a single small file directly', async () => {
            const { user, fetchMock, anchorClicks, api } = renderFileBrowser();

            await waitForRow('notes.txt');
            await chooseFromContextMenu(user, 'notes.txt', 'Download files');

            await waitFor(() => expect(fetchMock).toHaveBeenCalled());
            const [url, init] = fetchMock.mock.calls[0];
            expect(url).toBe('http://localhost:8080/cluster-manager/api/v1/generate-download-url');
            expect(init.method).toBe('POST');
            expect(JSON.parse(init.body)).toEqual({ file: `${HOME}/notes.txt` });

            // The user's browser is handed the signed URL.
            await waitFor(() => expect(anchorClicks).toHaveLength(1));
            expect(anchorClicks[0].href).toBe('https://cluster.example/signed/notes.txt');
            expect(anchorClicks[0].download).toBe('notes.txt');

            // A single small file is not an archive job.
            expect(api.downloadFiles).not.toHaveBeenCalled();
        });

        it('asks the server to build an archive for several selected files', async () => {
            const { api, user } = renderFileBrowser({
                listing: [entry('a.txt', { size: 10 }), entry('b.txt', { size: 20 })]
            });

            await waitForRow('a.txt');
            await selectEntry(user, 'a.txt');
            await addToSelection(user, 'b.txt');

            await chooseFromContextMenu(user, 'b.txt', 'Download files');

            await waitFor(() =>
                expect(api.downloadFiles).toHaveBeenCalledWith({ files: [`${HOME}/a.txt`, `${HOME}/b.txt`] })
            );
        });

        it('asks the server to build an archive for a directory', async () => {
            const { api, user } = renderFileBrowser({ listing: [dir('reports')] });

            await waitForRow('reports');
            await chooseFromContextMenu(user, 'reports', 'Download files');

            await waitFor(() => expect(api.downloadFiles).toHaveBeenCalledWith({ files: [`${HOME}/reports`] }));
        });

        it('tells the user an archive is being prepared, then that it is ready', async () => {
            const { user, flashbarSeen } = renderFileBrowser({ listing: [dir('reports')] });

            await waitForRow('reports');
            await chooseFromContextMenu(user, 'reports', 'Download files');

            await waitFor(() => expect(flashbarSeen.map((item) => item.header)).toContain('Download Ready'), {
                timeout: 10000
            });
            const headers = flashbarSeen.map((item) => item.header);
            expect(headers.indexOf('Preparing Download')).toBeGreaterThanOrEqual(0);
            expect(headers.indexOf('Preparing Download')).toBeLessThan(headers.indexOf('Download Ready'));
        });

        it('surfaces a failure to prepare the archive', async () => {
            const { api, user, flashbarSeen } = renderFileBrowser({ listing: [dir('reports')] });

            await waitForRow('reports');
            api.downloadFiles.mockRejectedValue({ errorCode: 'GENERAL_ERROR', message: 'archive failed' });

            await chooseFromContextMenu(user, 'reports', 'Download files');

            await waitFor(() => expect(flashbarSeen.map((item) => item.header)).toContain('Download Failed'), {
                timeout: 10000
            });
            expect(flashbarSeen.find((item) => item.header === 'Download Failed')!.content).toContain('archive failed');
        });
    });

    describe('deleting', () => {
        it('confirms with the user, names what will go, then deletes it', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await chooseFromContextMenu(user, 'notes.txt', 'Delete files');

            const dialog = await findDialogContaining('Are you sure you want to delete');
            expect(within(dialog).getByText('Delete File(s)')).toBeInTheDocument();
            expect(within(dialog).getByText('notes.txt')).toBeInTheDocument();

            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: HOME, listing: [dir('reports')] } as any);
            await user.click(within(dialog).getByRole('button', { name: 'Yes' }));

            await waitFor(() => expect(api.deleteFiles).toHaveBeenCalledWith({ files: [`${HOME}/notes.txt`] }));
            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: HOME }));
            await waitForNoRow('notes.txt');
        });

        it('deletes everything the user selected', async () => {
            const { api, user } = renderFileBrowser({
                listing: [entry('a.txt', { size: 1 }), entry('b.txt', { size: 2 }), entry('c.txt', { size: 3 })]
            });

            await waitForRow('a.txt');
            await selectEntry(user, 'a.txt');
            await addToSelection(user, 'c.txt');

            await chooseFromContextMenu(user, 'c.txt', 'Delete files');
            const dialog = await findDialogContaining('Are you sure you want to delete');
            expect(within(dialog).getByText('a.txt')).toBeInTheDocument();
            expect(within(dialog).getByText('c.txt')).toBeInTheDocument();
            expect(within(dialog).queryByText('b.txt')).toBeNull();

            await user.click(within(dialog).getByRole('button', { name: 'Yes' }));

            await waitFor(() =>
                expect(api.deleteFiles).toHaveBeenCalledWith({ files: [`${HOME}/a.txt`, `${HOME}/c.txt`] })
            );
        });

        it('deletes nothing if the user backs out', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await chooseFromContextMenu(user, 'notes.txt', 'Delete files');

            const dialog = await findDialogContaining('Are you sure you want to delete');
            await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));

            await waitFor(() => expect(dialogContaining('Are you sure you want to delete')).toBeNull());
            expect(api.deleteFiles).not.toHaveBeenCalled();
        });

        it('tells the user when the server refuses the delete', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.deleteFiles.mockRejectedValue({ errorCode: 'UNAUTHORIZED_ACCESS', message: 'not allowed' });

            await chooseFromContextMenu(user, 'notes.txt', 'Delete files');
            const dialog = await findDialogContaining('Are you sure you want to delete');
            await user.click(within(dialog).getByRole('button', { name: 'Yes' }));

            expect(await screen.findByText('Permission denied')).toBeInTheDocument();
        });
    });

    describe('renaming', () => {
        const RENAME_DIALOG = 'Enter new names for the selected';

        it('checks permissions for the selection before offering to rename', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.checkFilesPermissions.mockResolvedValue({
                results: [{ file: `${HOME}/notes.txt`, has_permission: true, is_protected: false }]
            } as any);

            await chooseFromContextMenu(user, 'notes.txt', 'Rename');

            await waitFor(() =>
                expect(api.checkFilesPermissions).toHaveBeenCalledWith({
                    files: [`${HOME}/notes.txt`],
                    operation: 'rename'
                })
            );
            const dialog = await findDialogContaining(RENAME_DIALOG);
            expect(within(dialog).getByDisplayValue('notes.txt')).toBeInTheDocument();
        });

        it('renames the file to the name the user typed, then re-lists', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.checkFilesPermissions.mockResolvedValue({
                results: [{ file: `${HOME}/notes.txt`, has_permission: true, is_protected: false }]
            } as any);

            await chooseFromContextMenu(user, 'notes.txt', 'Rename');
            const dialog = await findDialogContaining(RENAME_DIALOG);

            await replaceText(user, within(dialog).getByDisplayValue('notes.txt'), 'minutes.txt');

            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: HOME, listing: [dir('reports'), entry('minutes.txt')] } as any);
            await user.click(within(dialog).getByRole('button', { name: 'Rename 1 Item' }));

            await waitFor(() =>
                expect(api.renameFile).toHaveBeenCalledWith({ file: `${HOME}/notes.txt`, new_name: 'minutes.txt' })
            );
            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: HOME }));
            await waitForRow('minutes.txt');
        });

        it('renames several selected files in one pass', async () => {
            const { api, user } = renderFileBrowser({
                listing: [entry('a.txt', { size: 1 }), entry('b.txt', { size: 2 })]
            });

            await waitForRow('a.txt');
            api.checkFilesPermissions.mockResolvedValue({
                results: [
                    { file: `${HOME}/a.txt`, has_permission: true, is_protected: false },
                    { file: `${HOME}/b.txt`, has_permission: true, is_protected: false }
                ]
            } as any);

            await selectEntry(user, 'a.txt');
            await addToSelection(user, 'b.txt');
            await chooseFromContextMenu(user, 'b.txt', 'Rename');

            const dialog = await findDialogContaining(RENAME_DIALOG);
            await replaceText(user, within(dialog).getByDisplayValue('a.txt'), 'alpha.txt');
            await replaceText(user, within(dialog).getByDisplayValue('b.txt'), 'bravo.txt');

            await user.click(within(dialog).getByRole('button', { name: 'Rename 2 Items' }));

            await waitFor(() => expect(api.renameFile).toHaveBeenCalledTimes(2));
            expect(api.renameFile).toHaveBeenCalledWith({ file: `${HOME}/a.txt`, new_name: 'alpha.txt' });
            expect(api.renameFile).toHaveBeenCalledWith({ file: `${HOME}/b.txt`, new_name: 'bravo.txt' });
        });

        it('marks protected and forbidden entries and leaves them alone', async () => {
            const { api, user } = renderFileBrowser({
                listing: [
                    entry('ok.txt', { size: 1 }),
                    entry('locked.txt', { size: 2 }),
                    entry('forbidden.txt', { size: 3 })
                ]
            });

            await waitForRow('ok.txt');
            api.checkFilesPermissions.mockResolvedValue({
                results: [
                    { file: `${HOME}/ok.txt`, has_permission: true, is_protected: false },
                    { file: `${HOME}/locked.txt`, has_permission: true, is_protected: true },
                    { file: `${HOME}/forbidden.txt`, has_permission: false, is_protected: false }
                ]
            } as any);

            await selectEntry(user, 'ok.txt');
            await addToSelection(user, 'locked.txt');
            await addToSelection(user, 'forbidden.txt');
            await chooseFromContextMenu(user, 'ok.txt', 'Rename');

            const dialog = await findDialogContaining(RENAME_DIALOG);
            expect(within(dialog).getByText(/Protected - cannot be renamed/)).toBeInTheDocument();
            expect(within(dialog).getByText(/Permission denied/)).toBeInTheDocument();
            expect(within(dialog).getByDisplayValue('locked.txt')).toBeDisabled();
            expect(within(dialog).getByDisplayValue('forbidden.txt')).toBeDisabled();

            await replaceText(user, within(dialog).getByDisplayValue('ok.txt'), 'fine.txt');
            await user.click(within(dialog).getByRole('button', { name: 'Rename 1 Item' }));

            await waitFor(() => expect(api.renameFile).toHaveBeenCalledTimes(1));
            expect(api.renameFile).toHaveBeenCalledWith({ file: `${HOME}/ok.txt`, new_name: 'fine.txt' });
            expect(await screen.findByText(/2 protected or inaccessible item\(s\) were skipped/)).toBeInTheDocument();
        });

        it('refuses to submit a name the server would reject', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.checkFilesPermissions.mockResolvedValue({
                results: [{ file: `${HOME}/notes.txt`, has_permission: true, is_protected: false }]
            } as any);

            await chooseFromContextMenu(user, 'notes.txt', 'Rename');
            const dialog = await findDialogContaining(RENAME_DIALOG);

            await replaceText(user, within(dialog).getByDisplayValue('notes.txt'), 'bad/name.txt');

            expect(await within(dialog).findByText(/cannot contain path separators/i)).toBeInTheDocument();
            expect(within(dialog).getByRole('button', { name: 'Fix Validation Errors' })).toBeDisabled();
            expect(api.renameFile).not.toHaveBeenCalled();
        });
    });

    describe('opening a file', () => {
        it('reads a text file and shows it in an editor', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await openEntry(user, 'notes.txt');

            await waitFor(() => expect(api.readFile).toHaveBeenCalledWith({ file: `${HOME}/notes.txt` }));
            expect(await screen.findByText(`${HOME}/notes.txt`)).toBeInTheDocument();
        });

        it('saves the edited file back to the same path', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await openEntry(user, 'notes.txt');

            const dialog = await findDialogContaining(`${HOME}/notes.txt`);
            await user.click(within(dialog).getByRole('button', { name: 'Save' }));

            await waitFor(() =>
                expect(api.saveFile).toHaveBeenCalledWith({ file: `${HOME}/notes.txt`, content: btoa('hello') })
            );
            expect(await screen.findByText('File saved successfully')).toBeInTheDocument();
        });

        it('reports a rejected save instead of pretending it worked', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await openEntry(user, 'notes.txt');

            const dialog = await findDialogContaining(`${HOME}/notes.txt`);
            api.saveFile.mockRejectedValue({ errorCode: 'UNAUTHORIZED_ACCESS', message: 'nope' });
            await user.click(within(dialog).getByRole('button', { name: 'Save' }));

            expect(await screen.findByText(/Failed to save file: Permission Denied/)).toBeInTheDocument();
        });

        it('refuses to open a file too large to edit, and does not fetch it', async () => {
            const { api, user } = renderFileBrowser({
                listing: [entry('huge.log', { size: 6 * 1024 * 1024 })]
            });

            await waitForRow('huge.log');
            await openEntry(user, 'huge.log');

            expect(await screen.findByText(/File is too large \(6\.00MB\)/)).toBeInTheDocument();
            expect(api.readFile).not.toHaveBeenCalled();
        });

        it('downloads a file that turns out not to be text', async () => {
            const { api, user, fetchMock } = renderFileBrowser({
                listing: [entry('image.png', { size: 1024 })]
            });

            await waitForRow('image.png');
            api.readFile.mockRejectedValue({ errorCode: 'NOT_A_TEXT_FILE', message: 'not text' });

            await openEntry(user, 'image.png');

            await waitFor(() => expect(fetchMock).toHaveBeenCalled());
            expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ file: `${HOME}/image.png` });
            // and the editor never showed it
            expect(screen.queryByText(`${HOME}/image.png`)).toBeNull();
        });
    });

    describe('acting on the selection', () => {
        it('copies the absolute path of the selected file', async () => {
            const { user, clipboardWrites } = renderFileBrowser();

            await waitForRow('notes.txt');
            await chooseFromContextMenu(user, 'notes.txt', 'Copy selection');

            await waitFor(() => expect(clipboardWrites).toEqual([`${HOME}/notes.txt`]));
            expect(await screen.findByText(/notes\.txt path copied to clipboard/)).toBeInTheDocument();
        });

        it('opens the log tail view for the selected file in a new tab', async () => {
            const { user, anchorClicks } = renderFileBrowser();

            await waitForRow('notes.txt');
            await chooseFromContextMenu(user, 'notes.txt', 'Tail File');

            await waitFor(() => expect(anchorClicks).toHaveLength(1));
            expect(anchorClicks[0].href).toBe(`/#/home/file-browser/tail?file=${HOME}/notes.txt&cwd=${HOME}`);
            expect(anchorClicks[0].target).toBe('_blank');
        });

        it('hands a text file to the script workbench', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await chooseFromContextMenu(user, 'notes.txt', 'Open in Script Workbench');

            await waitFor(() => expect(api.readFile).toHaveBeenCalledWith({ file: `${HOME}/notes.txt` }));
        });

        it('refuses to hand a binary file to the script workbench', async () => {
            const { api, user } = renderFileBrowser({ listing: [entry('binary.bin', { size: 1024 })] });

            await waitForRow('binary.bin');
            api.readFile.mockRejectedValue({ errorCode: 'NOT_A_TEXT_FILE', message: 'not text' });

            await chooseFromContextMenu(user, 'binary.bin', 'Open in Script Workbench');

            await waitFor(() => expect(api.readFile).toHaveBeenCalledWith({ file: `${HOME}/binary.bin` }));
            expect(await screen.findByText(/Cannot open binary files in Script Workbench/)).toBeInTheDocument();
        });

        it('does not carry a Shift-click range anchor across a directory change', async () => {
            const { api, user, anchorClicks } = renderFileBrowser({
                cwd: `${HOME}/reports`,
                listing: [entry('m.txt'), entry('a.txt')]
            });

            await waitForRow('m.txt');
            // Anchors row index 1 (a.txt sorts to 0, m.txt to 1) in the old directory.
            await selectEntry(user, 'm.txt');

            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({
                cwd: HOME,
                listing: [entry('x.txt'), entry('y.txt'), entry('z.txt')]
            } as any);
            await user.click(breadcrumb('testuser')!);
            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: HOME }));
            await waitForRow('z.txt');

            // The anchor from the old directory is not in this listing, so this Shift-click falls
            // through to a plain single click on 'z.txt' rather than resolving row index 1.
            await selectRangeTo(user, 'z.txt');
            await chooseFromContextMenu(user, 'z.txt', 'Download files');

            await waitFor(() => expect(anchorClicks).toHaveLength(1));
            expect(anchorClicks[0].download).toBe('z.txt');
            expect(api.downloadFiles).not.toHaveBeenCalled();
        });

        it('does not carry a Shift-click range anchor past a filter that hides it', async () => {
            const { api, user, anchorClicks } = renderFileBrowser({
                listing: [entry('apple.txt'), entry('item-b.txt'), entry('item-c.txt'), entry('item-d.txt')]
            });

            await waitForRow('apple.txt');
            await selectEntry(user, 'apple.txt');

            await replaceText(user, searchBox(), 'item');
            await waitForRow('item-d.txt');
            expect(row('apple.txt')).toBeNull();

            // The anchor ('apple.txt') is filtered out of view, so this
            // Shift-click must fall through to a plain single click on
            // 'item-d.txt' rather than sweeping in 'item-b.txt' and 'item-c.txt'.
            await selectRangeTo(user, 'item-d.txt');
            await chooseFromContextMenu(user, 'item-d.txt', 'Download files');

            await waitFor(() => expect(anchorClicks).toHaveLength(1));
            expect(anchorClicks[0].download).toBe('item-d.txt');
            expect(api.downloadFiles).not.toHaveBeenCalled();
        });
    });

    describe('scheduler integration', () => {
        it('offers no job submission when the scheduler is not deployed', async () => {
            const { user } = renderFileBrowser({ schedulerDeployed: false });

            await waitForRow('notes.txt');
            expect(toolbarButton('Submit Job')).toBeNull();

            await openContextMenu(user, 'notes.txt');
            expect(openMenuItem('Submit Job')).toBeNull();
        });

        it('offers job submission for the selected file when the scheduler is deployed', async () => {
            const { user } = renderFileBrowser({ schedulerDeployed: true });

            await waitForRow('notes.txt');
            expect(toolbarButton('Submit Job')).not.toBeNull();

            await selectEntry(user, 'notes.txt');
            await user.click(requireToolbarButton('Submit Job'));

            await waitFor(() => expect(urlQuery()).toBe(`?input_file=${HOME}/notes.txt`));
        });
    });

    describe('favourites', () => {
        it('remembers a favourite and shows it on the Favorites tab', async () => {
            const { user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await selectEntry(user, 'notes.txt');
            await user.click(requireToolbarButton('Favorite'));

            await user.click(screen.getByRole('tab', { name: 'Favorites' }));
            await waitForRow(`(${HOME}) notes.txt`);
        });

        it('survives a reload, because favourites are stored per user', async () => {
            const first = renderFileBrowser();

            await waitForRow('notes.txt');
            await selectEntry(first.user, 'notes.txt');
            await first.user.click(requireToolbarButton('Favorite'));
            await waitFor(() => expect(localStorage.length).toBeGreaterThan(0));
            first.unmount();

            const second = renderFileBrowser();
            await waitForRow('notes.txt');
            await second.user.click(screen.getByRole('tab', { name: 'Favorites' }));
            await waitForRow(`(${HOME}) notes.txt`);
        });

        it('forgets a favourite the user removes', async () => {
            const { user } = renderFileBrowser();

            await waitForRow('notes.txt');
            await selectEntry(user, 'notes.txt');
            await user.click(requireToolbarButton('Favorite'));

            await user.click(screen.getByRole('tab', { name: 'Favorites' }));
            await waitForRow(`(${HOME}) notes.txt`);

            await chooseFromContextMenu(user, `(${HOME}) notes.txt`, 'Remove Favorite');

            await waitForNoRow(`(${HOME}) notes.txt`);
        });

        it('returns to the file list at the favourite directory when one is opened', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('reports');
            await selectEntry(user, 'reports');
            await user.click(requireToolbarButton('Favorite'));

            await user.click(screen.getByRole('tab', { name: 'Favorites' }));
            await waitForRow(`(${HOME}) reports`);

            api.listFiles.mockClear();
            api.listFiles.mockResolvedValue({ cwd: `${HOME}/reports`, listing: [entry('q1.txt')] } as any);
            await openEntry(user, `(${HOME}) reports`);

            await waitFor(() => expect(api.listFiles).toHaveBeenCalledWith({ cwd: `${HOME}/reports` }));
            await waitForRow('q1.txt');
        });
    });

    describe('when the API fails', () => {
        it('tells the user why a directory would not open', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('reports');
            api.listFiles.mockRejectedValue({ errorCode: 'UNAUTHORIZED_ACCESS', message: 'Permission denied' });

            await openEntry(user, 'reports');

            expect(await screen.findByText('Permission denied')).toBeInTheDocument();
            // and leaves the user where they were
            expect(row('reports')).not.toBeNull();
            expect(breadcrumbTrail()).toEqual(['root', 'home', 'testuser']);
        });

        it('tells the user why a rename could not be attempted', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.checkFilesPermissions.mockRejectedValue({ errorCode: 'GENERAL_ERROR', message: 'backend is down' });

            await chooseFromContextMenu(user, 'notes.txt', 'Rename');

            expect(await screen.findByText(/Failed to check permissions: backend is down/)).toBeInTheDocument();
            expect(api.renameFile).not.toHaveBeenCalled();
        });

        it('tells the user why a file would not open', async () => {
            const { api, user } = renderFileBrowser();

            await waitForRow('notes.txt');
            api.readFile.mockRejectedValue({ errorCode: 'GENERAL_ERROR', message: 'read failed' });

            await openEntry(user, 'notes.txt');

            expect(await screen.findByText('read failed')).toBeInTheDocument();
        });
    });

    describe('a directory with many entries', () => {
        // ListFiles has a paginator field it never populates: the backend returns the whole directory
        // in one response. Every other fixture in this suite is small, so only this test notices.
        const LARGE_DIRECTORY_SIZE = 5000;

        function name(index: number) {
            return `file-${String(index).padStart(5, '0')}.txt`;
        }

        function largeListing() {
            return Array.from({ length: LARGE_DIRECTORY_SIZE }, (_, index) => entry(name(index), { size: index }));
        }

        it('lists every entry and stays usable', async () => {
            const started = Date.now();
            const { user } = renderFileBrowser({ listing: largeListing() });

            await waitForRow(name(0));
            const elapsed = Date.now() - started;

            // The count is of the whole directory, not of a page of it.
            expect(reportedItemCount()).toBe(LARGE_DIRECTORY_SIZE);

            // Entries throughout the listing are reachable. Whether a given row
            // is already in the DOM or arrives when the list is filtered is the
            // component's business; that a user can get to it is not.
            for (const index of [2500, LARGE_DIRECTORY_SIZE - 1]) {
                await replaceText(user, searchBox(), name(index).replace('.txt', ''));
                await waitForRow(name(index));
            }

            // Not a benchmark, a smoke guard against a replacement that renders
            // every row, and a number to compare against after the swap.
            // eslint-disable-next-line no-console
            console.info(`[characterization] ${LARGE_DIRECTORY_SIZE}-entry directory first render: ${elapsed}ms`);
            expect(elapsed).toBeLessThan(20000);
        }, 120000);

        it('deletes only the entries selected out of a large directory', async () => {
            const { api, user } = renderFileBrowser({ listing: largeListing() });

            await waitForRow(name(0));
            await selectEntry(user, name(0));
            await addToSelection(user, name(2));

            await chooseFromContextMenu(user, name(2), 'Delete files');
            const dialog = await findDialogContaining('Are you sure you want to delete');
            await user.click(within(dialog).getByRole('button', { name: 'Yes' }));

            await waitFor(() =>
                expect(api.deleteFiles).toHaveBeenCalledWith({
                    files: [`${HOME}/${name(0)}`, `${HOME}/${name(2)}`]
                })
            );
        }, 120000);
    });
});

/* Not covered: grid view, sort and search as features in their own right (asserting row order is the
 * coupling this file avoids), keyboard selection, Uppy transferring bytes, the File Transfer tab, dark
 * mode, icons, drag and drop (off today), and deleting at the filesystem root. Check those by hand. */
