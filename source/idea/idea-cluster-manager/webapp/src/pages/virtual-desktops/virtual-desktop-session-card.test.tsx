import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import VirtualDesktopSessionCard, { describeSchedule, primaryAction } from './components/virtual-desktop-session-card';
import { initTestAppContext } from '../../test-support';

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
    render(<VirtualDesktopSessionCard
        virtualDesktopClient={{} as any}
        isActiveDirectory={false}
        session={{ ...BASE, state }}
        {...handlers}
        {...props}/>);
    return handlers;
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
