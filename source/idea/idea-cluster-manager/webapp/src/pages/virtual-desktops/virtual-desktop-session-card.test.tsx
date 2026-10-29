import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import VirtualDesktopSessionCard, { describeSchedule, primaryAction } from './components/virtual-desktop-session-card';
import { initTestAppContext } from '../../test-support';
import { desktopTerminalReason, shellStore } from '../home/terminal/shell-store';

const BASE = {
    idea_session_id: 'session-1',
    dcv_session_id: 'dcv-1',
    name: 'my-desktop',
    owner: 'someuser',
    base_os: 'amazonlinux2023',
    software_stack: { base_os: 'amazonlinux2023', ami_id: 'ami-0', launch_tenancy: 'default' },
    server: { instance_id: 'i-0123', instance_type: 'm6i.xlarge', private_ip: '10.0.0.5' },
    project: { project_id: 'p1', title: 'Project A' },
    created_on: '2026-01-02T03:04:05Z'
} as any;

function renderCard(state: string, props: any = {}) {
    initTestAppContext();
    const handlers = {
        onLaunchSession: vi.fn().mockResolvedValue(true),
        onStartSession: vi.fn().mockResolvedValue(true),
        onDownloadDcvSessionFile: vi.fn().mockResolvedValue(true),
        onConnectHelp: vi.fn().mockResolvedValue(true)
    };
    const card = (overrides: any = {}) => <VirtualDesktopSessionCard
        virtualDesktopClient={{} as any}
        isActiveDirectory={false}
        session={{ ...BASE, state }}
        {...handlers}
        {...props}
        {...overrides}/>;
    const view = render(card());
    return {...handlers, rerender: (overrides: any) => view.rerender(card(overrides))};
}

describe('primaryAction', () => {
    it.each([
        ['READY', 'Connect', 'connect'],
        ['STOPPED', 'Start', 'start'],
        ['ERROR', 'Show info', 'info'],
        ['PROVISIONING', 'Setting up', undefined],
        ['RESUMING', 'Starting', undefined],
        ['STOPPING', 'Stopping', undefined],
        ['DELETING', 'Terminating', undefined]
    ])('%s leads with %s', (state, label, action) => {
        expect(primaryAction({ state } as any, true, false)).toEqual({ label, action });
    });

    it('does not offer Start when the desktop cannot be started from here', () => {
        expect(primaryAction({ state: 'STOPPED' } as any, false, false)).toEqual({ label: 'Stopped' });
    });
});

describe('describeSchedule', () => {
    it('names the cluster timezone the hours run in', () => {
        expect(describeSchedule({ schedule_type: 'CUSTOM_SCHEDULE', start_up_time: '08:00', shut_down_time: '18:00' }, 30, undefined, 'UTC'))
            .toBe('Runs 08:00–18:00 UTC, then stops after 30 min idle');
    });

    it('says what will happen', () => {
        expect(describeSchedule({ schedule_type: 'START_ALL_DAY' }, 30)).toBe('Always on');
        expect(describeSchedule({ schedule_type: 'STOP_ON_IDLE' }, 30)).toBe('Stops after 30 min idle');
        expect(describeSchedule({ schedule_type: 'STOP_ON_IDLE' }, 0)).toBe('Stops when idle');
        expect(describeSchedule({ schedule_type: 'CUSTOM_SCHEDULE', start_up_time: '08:00', shut_down_time: '18:00' }, 0))
            .toBe('Runs 08:00–18:00, then stops when idle');
        expect(describeSchedule({ schedule_type: 'WORKING_HOURS' }, 0, { start_up_time: '09:00', shut_down_time: '17:00' }))
            .toBe('Runs 09:00–17:00, then stops when idle');
        expect(describeSchedule({ schedule_type: 'NO_SCHEDULE' }, 0)).toBe('No schedule today');
    });
});

describe('virtual desktop session card', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it.each(['ubuntu2604', 'rocky10', 'windows2022', 'amazonlinux2023'])('replaces a black %s preview and still connects', async (base_os) => {
        const data = new Uint8ClampedArray(32 * 32 * 4);
        for (let i = 3; i < data.length; i += 4) data[i] = 255;
        vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
            drawImage: vi.fn(), getImageData: () => ({ data })
        } as any);
        const handlers = renderCard('READY', { screenshot: 'black', session: { ...BASE, state: 'READY', base_os } });
        fireEvent.load(screen.getByAltText('Desktop preview'));
        expect(screen.getByText('Desktop locked for security.')).toBeInTheDocument();
        expect(screen.getByText('Preview unavailable. Connect to unlock.')).toBeInTheDocument();
        expect(screen.queryByAltText('Desktop preview')).not.toBeInTheDocument();
        const placeholder = screen.getByText('Preview unavailable. Connect to unlock.');
        expect(placeholder.closest('.virtual-desktop-placeholder-image')).toHaveStyle({cursor: 'pointer'});
        await userEvent.setup().click(placeholder);
        expect(handlers.onLaunchSession).toHaveBeenCalledWith(expect.objectContaining({idea_session_id: BASE.idea_session_id}));
    });

    it.each(['normal', 'canvas unavailable'])('keeps a preview for %s', (kind) => {
        vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
            if (kind === 'canvas unavailable') throw new Error('canvas unavailable');
            return { drawImage: vi.fn(), getImageData: () => ({ data: new Uint8ClampedArray(32 * 32 * 4).fill(255) }) } as any;
        });
        renderCard('READY', { screenshot: 'normal' });
        fireEvent.load(screen.getByAltText('Desktop preview'));
        expect(screen.getByAltText('Desktop preview')).toBeInTheDocument();
        expect(screen.queryByText('Desktop locked for security.')).not.toBeInTheDocument();
    });

    it('allows JPEG noise and clears the locked card when a new preview has content', () => {
        const data = new Uint8ClampedArray(32 * 32 * 4).fill(8);
        vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
            drawImage: vi.fn(), getImageData: () => ({ data })
        } as any);
        const card = renderCard('READY', { screenshot: 'near-black' });
        fireEvent.load(screen.getByAltText('Desktop preview'));
        expect(screen.getByText('Desktop locked for security.')).toBeInTheDocument();
        data[0] = 9;
        card.rerender({ screenshot: 'new-content' });
        fireEvent.load(screen.getByAltText('Desktop preview'));
        expect(screen.queryByText('Desktop locked for security.')).not.toBeInTheDocument();
        expect(screen.getByAltText('Desktop preview')).toHaveAttribute('src', 'data:image/jpeg;base64,new-content');
    });

    it('connects a ready desktop from the primary button', async () => {
        const handlers = renderCard('READY');
        await userEvent.setup().click(screen.getByRole('button', { name: 'Connect' }));
        expect(handlers.onLaunchSession).toHaveBeenCalled();
    });

    it('starts a stopped desktop from the primary button', async () => {
        const handlers = renderCard('STOPPED');
        await userEvent.setup().click(screen.getByRole('button', { name: 'Start' }));
        expect(handlers.onStartSession).toHaveBeenCalled();
    });

    it('disables the primary button while a desktop is changing state', () => {
        renderCard('STOPPING');
        expect(screen.getByRole('button', { name: 'Stopping' })).toBeDisabled();
    });

    it('shows a placeholder that matches the state', () => {
        renderCard('READY');
        expect(screen.getByText('Preview not captured yet')).toBeInTheDocument();
    });

    it('connects from the not-captured placeholder like the preview', async () => {
        const handlers = renderCard('READY');
        const placeholder = screen.getByText('Preview not captured yet');
        expect(placeholder).toHaveStyle({cursor: 'pointer'});
        await userEvent.setup().click(placeholder);
        expect(handlers.onLaunchSession).toHaveBeenCalled();
    });

    it('drops the last capture once the desktop stops', () => {
        const card = renderCard('READY', { screenshot: 'running' });
        expect(screen.getByAltText('Desktop preview')).toBeInTheDocument();
        card.rerender({ session: { ...BASE, state: 'STOPPED' } });
        expect(screen.queryByAltText('Desktop preview')).not.toBeInTheDocument();
        expect(screen.getAllByText('Stopped').length).toBe(2);
    });

    it('shows a stopped placeholder for a stopped desktop', () => {
        renderCard('STOPPED');
        // status indicator and preview both say it
        expect(screen.getAllByText('Stopped').length).toBe(2);
        expect(screen.queryByText('No preview available.')).not.toBeInTheDocument();
    });

    it('puts OS and instance type in one line and the project in one badge', () => {
        renderCard('READY');
        expect(screen.getByText('Amazon Linux 2023 · m6i.xlarge')).toBeInTheDocument();
        expect(screen.getByText('Project A')).toBeInTheDocument();
    });

    it('labels the DCV client file and links to client setup', async () => {
        const handlers = renderCard('READY');
        const user = userEvent.setup();
        await user.click(screen.getByRole('button', { name: 'DCV client file' }));
        expect(handlers.onDownloadDcvSessionFile).toHaveBeenCalled();
        await user.click(screen.getByText('Info'));
        await user.click(await screen.findByText('Get the client and setup steps'));
        expect(handlers.onConnectHelp).toHaveBeenCalled();
    });

    it('shows every info field with human labels', async () => {
        renderCard('ERROR');
        await userEvent.setup().click(screen.getByRole('button', { name: 'Show info' }));
        for (const label of ['Desktop ID', 'DCV session ID', 'Project', 'State', 'Operating system', 'Instance type', 'Instance ID', 'Private IP', 'AMI ID', 'Tenancy', 'Created']) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
        expect(screen.getByText('session-1')).toBeInTheDocument();
        expect(screen.getByText('10.0.0.5')).toBeInTheDocument();
    });
});


describe('desktop recovery', () => {
    it('explains why Connect is unavailable while the session restarts', () => {
        renderCard('INITIALIZING', { session: { ...BASE, state: 'INITIALIZING', recovering: true } });
        expect(screen.getByText('Your desktop session is restarting. Connect will be available when it is ready.')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Session restarting' })).toBeDisabled();
        expect(screen.queryByText('Preview not captured yet')).not.toBeInTheDocument();
    });

    it('keeps the READY placeholder clickable', async () => {
        const handlers = renderCard('READY');
        await userEvent.setup().click(screen.getByText('Preview not captured yet'));
        expect(handlers.onLaunchSession).toHaveBeenCalled();
    });
});

describe('Terminal action', () => {
    const terminal = async (session: any, targets: any[] = [{ id: 'bastion', label: 'Bastion' }, { id: 'desktop:session-1', label: 'my-desktop' }], props: any = {}) => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'hasModuleAccess').mockReturnValue(true);
        const listTargets = vi.spyOn(context.client().terminal(), 'listTargets').mockResolvedValue({ targets });
        render(<VirtualDesktopSessionCard virtualDesktopClient={{} as any} isActiveDirectory={false} session={{ ...BASE, ...session }}
            onLaunchSession={vi.fn()} onDownloadDcvSessionFile={vi.fn()} onConnectHelp={vi.fn()} {...props}/>);
        return listTargets;
    };
    beforeEach(() => shellStore.reset());
    afterEach(() => { vi.restoreAllMocks(); delete (window as any).idea?.app?.web_terminal_enabled; });

    const SHELL = 'Open a shell on my-desktop';

    it('opens a terminal tab from the shell button beside Connect and from the Actions menu', async () => {
        await terminal({ state: 'READY' });
        const button = await screen.findByRole('button', { name: SHELL });
        await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled'));
        expect(button).toHaveAttribute('title', SHELL);
        fireEvent.click(button);
        expect(shellStore.get().tabs).toEqual([{ target: 'desktop:session-1', label: 'my-desktop' }]);
        shellStore.close('desktop:session-1');
        await userEvent.click(screen.getByRole('button', { name: 'Actions' }));
        await userEvent.click(screen.getByRole('menuitem', { name: 'Terminal' }));
        expect(shellStore.get().active).toBe('desktop:session-1');
    });

    it('sits immediately before the primary button, icon only, in the card header', async () => {
        await terminal({ state: 'READY' });
        const shell = await screen.findByRole('button', { name: SHELL });
        const connect = screen.getByRole('button', { name: 'Connect' });
        expect(shell.textContent).toBe('');
        const group = shell.closest('div[style*="flex-shrink: 0"]') as HTMLElement;
        // two children: the shell button, then the primary button
        expect(group.children).toHaveLength(2);
        expect(group.children[0]).toContainElement(shell);
        expect(group.children[1]).toContainElement(connect);
        // the group keeps its buttons on one line; the name column takes any wrapping
        expect(group.style.whiteSpace).toBe('nowrap');
        expect((group.previousElementSibling as HTMLElement).style.minWidth).toBe('0');
        expect(group.previousElementSibling).toHaveTextContent('my-desktop');
    });

    it('no longer puts a Terminal button in the bottom row', async () => {
        await terminal({ state: 'READY' });
        await waitFor(() => expect(shellStore.get().targets).toBeDefined());
        const download = screen.getByRole('button', { name: 'DCV client file' });
        const row = download.closest('div[style*="space-between"]') as HTMLElement;
        expect(row).toContainElement(screen.getByRole('button', { name: 'Actions' }));
        expect(within(row).queryByRole('button', { name: /terminal|shell/i })).not.toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Terminal' })).not.toBeInTheDocument();
    });

    it.each([
        ['a Windows desktop', { state: 'READY', base_os: 'windows' }, 'Terminal is for Linux desktops.'],
        ['a stopped desktop', { state: 'STOPPED' }, 'Start this desktop to use Terminal.'],
        ['a broken desktop', { state: 'ERROR' }, 'This desktop is in an error or deleting state.'],
        ['a starting desktop', { state: 'RESUMING' }, 'Terminal opens when the desktop is ready.'],
        ['a desktop whose trust the refresh has not installed yet', { state: 'READY', idea_session_id: 'old' }, 'Terminal becomes available after this desktop next starts.'],
    ])('disables the shell button with its reason in the tooltip for %s', async (_name, session, reason) => {
        await terminal(session);
        if (session.state === 'READY') await waitFor(() => expect(shellStore.get().targets).toBeDefined());
        const button = screen.getByRole('button', { name: SHELL });
        expect(button).toHaveAttribute('aria-disabled', 'true');
        fireEvent.click(button);
        expect(shellStore.get().tabs).toEqual([]);
        expect(desktopTerminalReason({ ...BASE, ...session } as any, shellStore.get())).toBe(reason);
        fireEvent.focus(button);
        expect((await screen.findAllByText(reason))[0]).toBeInTheDocument();
    });

    it('opens once the refresh has published the desktop as a target', async () => {
        await terminal({ state: 'READY', idea_session_id: 'old' }, [{ id: 'desktop:old', label: 'my-desktop' }]);
        const button = await screen.findByRole('button', { name: SHELL });
        await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled'));
    });

    it('is not offered on shared desktops or when Terminal is disabled', async () => {
        await terminal({ state: 'READY' }, undefined, { isSharedSession: true });
        expect(screen.queryByRole('button', { name: SHELL })).not.toBeInTheDocument();
    });

    it('is not offered when the cluster disables Terminal', async () => {
        initTestAppContext();
        (window as any).idea.app.web_terminal_enabled = false;
        const context = initTestAppContext();
        (window as any).idea.app.web_terminal_enabled = false;
        vi.spyOn(context.auth(), 'hasModuleAccess').mockReturnValue(true);
        render(<VirtualDesktopSessionCard virtualDesktopClient={{} as any} isActiveDirectory={false} session={{ ...BASE, state: 'READY' }}
            onLaunchSession={vi.fn()} onDownloadDcvSessionFile={vi.fn()} onConnectHelp={vi.fn()}/>);
        expect(screen.queryByRole('button', { name: SHELL })).not.toBeInTheDocument();
    });
});
