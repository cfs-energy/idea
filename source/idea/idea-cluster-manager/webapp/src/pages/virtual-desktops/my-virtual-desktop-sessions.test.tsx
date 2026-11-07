import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import createWrapper, { ModalWrapper } from '@cloudscape-design/components/test-utils/dom';
import { POPUP_BLOCKED_MESSAGE } from './desktop-connection';
import MyVirtualDesktopSessions, { matchesOsFilter, mergeSessions, needsQuickRefresh } from './my-virtual-desktop-sessions';
import { initTestAppContext } from '../../test-support';
import { AppContext } from '../../common';

const STOPPED_SESSION = {
    idea_session_id: 'session-1',
    dcv_session_id: 'dcv-1',
    name: 'my-desktop',
    owner: 'someuser',
    state: 'STOPPED',
    base_os: 'amazonlinux2023'
};

const PROJECT_ID = 'project-1';
const SHARED_INSTANCE_PROFILE_ARN = 'arn:aws:iam::111111111111:instance-profile/idea-test-vdc-host-instance-profile';
const PROJECT_INSTANCE_PROFILE_ARN = 'arn:aws:iam::111111111111:instance-profile/idea/idea-test/projects/idea-test-p1-project';
const AI_ACCESS_PENDING_TEXT = /cannot use the project's AI models yet/;

/** Renders the page with one stopped session and returns the flashbar items raised by the page, which
 * is where a failed session action has to end up. */
async function startSessionAndCaptureFlashbar(resumeSessionsResult: any, getProject?: any) {
    const context = initTestAppContext();

    const clusterSettings = context.getClusterSettingsService();
    vi.spyOn(clusterSettings, 'getDirectoryServiceSettings').mockResolvedValue({ provider: 'openldap' });
    vi.spyOn(clusterSettings, 'getVirtualDesktopSettings').mockResolvedValue({
        dcv_session: {
            idle_autostop_delay_max: 0,
            max_root_volume_memory: { value: 500, unit: 'gb' },
            working_hours: { start_up_time: '09:00', shut_down_time: '17:00' }
        }
    });

    const projects = context.client().projects();
    vi.spyOn(projects, 'getUserProjects').mockResolvedValue({ projects: [] } as any);
    vi.spyOn(projects, 'getProject').mockImplementation(getProject ?? (() => Promise.resolve({ project: undefined } as any)));

    const virtualDesktop = context.client().virtualDesktop();
    // a STOPPED session is skipped by fetchSessionScreenshots, so no screenshot API is needed.
    vi.spyOn(virtualDesktop, 'listSessions').mockResolvedValue({ listing: [STOPPED_SESSION] } as any);
    const resumeSessions = vi.spyOn(virtualDesktop, 'resumeSessions');
    if (resumeSessionsResult instanceof Error) {
        resumeSessions.mockRejectedValue(resumeSessionsResult);
    } else {
        resumeSessions.mockResolvedValue(resumeSessionsResult);
    }

    const flashbarItems: any[] = [];
    const onFlashbarChange = (event: any) => {
        flashbarItems.push(...event.items);
    };

    const user = userEvent.setup();
    render(
        <MemoryRouter>
            <MyVirtualDesktopSessions
                ideaPageId="my-virtual-desktop-sessions"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={onFlashbarChange}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );

    // the first render in a file pays the cloudscape/ace module load, so allow for it.
    await screen.findByText('my-desktop', {}, { timeout: 10000 });
    // a stopped desktop leads with Start
    await user.click(await screen.findByRole('button', { name: 'Start' }));
    await waitFor(() => expect(resumeSessions).toHaveBeenCalled());
    return flashbarItems;
}

/** Flashbar content is a React node; render it to read the copy the user sees. */
function flashbarText(item: any): string {
    const { container } = render(<div>{item.content}</div>);
    return container.textContent ?? '';
}

describe('my virtual desktop sessions', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('shows one page heading and keeps every action in the Actions menu', async () => {
        await renderSessionWithSchedule('START_ALL_DAY', '/home/virtual-desktops');
        expect(screen.getAllByRole('heading', { name: 'My desktops' })).toHaveLength(1);
        expect(screen.getByRole('button', { name: 'Launch desktop' })).toBeInTheDocument();
        const user = userEvent.setup();
        await user.click(screen.getByRole('button', { name: /Actions/i }));
        for (const item of ['Connect', 'Share desktop', 'Show info', 'Schedule', 'Rename or resize']) {
            expect(screen.getAllByText(item).length).toBeGreaterThan(0);
        }
        await user.click(screen.getByText('Desktop state'));
        for (const item of ['Stop', 'Reboot', 'Terminate']) {
            expect(screen.getByText(item)).toBeInTheDocument();
        }
    });

    it('does not request a project on page load, which fails for anyone outside it', async () => {
        const getProject = vi.fn();
        await startSessionAndCaptureFlashbar({ success: [STOPPED_SESSION], failed: [] }, getProject);
        expect(getProject).not.toHaveBeenCalled();
    });

    it('reports a session action the controller refused', async () => {
        const items = await startSessionAndCaptureFlashbar({
            success: [],
            failed: [{ ...STOPPED_SESSION, failure_reason: 'Instance type is not available in this subnet' }]
        });

        const errors = items.filter((item) => item.type === 'error');
        expect(errors).toHaveLength(1);
        expect(flashbarText(errors[0])).toContain(
            'Could not start virtual desktop my-desktop: Instance type is not available in this subnet'
        );
    });

    it('reports a session action that failed with no reason', async () => {
        const items = await startSessionAndCaptureFlashbar({
            success: [],
            failed: [{ idea_session_id: 'session-1' }]
        });

        const errors = items.filter((item) => item.type === 'error');
        expect(errors).toHaveLength(1);
        expect(flashbarText(errors[0])).toContain(
            'Could not start this desktop. No reason was returned, contact your administrator.'
        );
    });

    it('reports a session action that failed with an api error', async () => {
        const items = await startSessionAndCaptureFlashbar(
            Object.assign(new Error(), { errorCode: 'SESSION_NOT_FOUND', message: 'Session not found' })
        );

        const errors = items.filter((item) => item.type === 'error');
        expect(errors).toHaveLength(1);
        expect(flashbarText(errors[0])).toContain('Could not start virtual desktop: Session not found');
    });
});

async function renderSessionWithSchedule(scheduleType: 'START_ALL_DAY' | 'WORKING_HOURS', path = '/', state = 'STOPPED') {
    const context = initTestAppContext();

    const clusterSettings = context.getClusterSettingsService();
    vi.spyOn(clusterSettings, 'getDirectoryServiceSettings').mockResolvedValue({ provider: 'openldap' });
    vi.spyOn(clusterSettings, 'getVirtualDesktopSettings').mockResolvedValue({
        dcv_session: {
            idle_autostop_delay_max: 60,
            max_root_volume_memory: { value: 500, unit: 'gb' },
            working_hours: { start_up_time: '09:00', shut_down_time: '17:00' }
        }
    });

    const projects = context.client().projects();
    vi.spyOn(projects, 'getUserProjects').mockResolvedValue({ projects: [] } as any);
    vi.spyOn(projects, 'getProject').mockResolvedValue({ project: undefined } as any);

    const daySchedule = { schedule_type: scheduleType };
    const virtualDesktop = context.client().virtualDesktop();
    vi.spyOn(virtualDesktop, 'listSessions').mockResolvedValue({
        listing: [{
            ...STOPPED_SESSION,
            state,
            idle_autostop_delay: 30,
            schedule: {
                monday: daySchedule,
                tuesday: daySchedule,
                wednesday: daySchedule,
                thursday: daySchedule,
                friday: daySchedule,
                saturday: daySchedule,
                sunday: daySchedule
            }
        }]
    } as any);

    vi.spyOn(virtualDesktop, 'getSessionScreenshot').mockResolvedValue({ screenshots: [] } as any);
    const flashbarItems: any[] = [];
    render(
        <MemoryRouter initialEntries={[path]}>
            <MyVirtualDesktopSessions
                ideaPageId="my-virtual-desktop-sessions"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={(event: any) => flashbarItems.push(...event.items)}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );

    await screen.findByText('my-desktop', {}, { timeout: 10000 });
    return { virtualDesktop, flashbarItems };
}

describe('my virtual desktop sessions idle badge', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('says always on and no idle stop when today starts all day', async () => {
        await renderSessionWithSchedule('START_ALL_DAY');
        expect(screen.getByText('Always on')).toBeInTheDocument();
        expect(screen.queryByText(/min idle/)).not.toBeInTheDocument();
    });

    it('says when a working-hours desktop runs and when it stops', async () => {
        await renderSessionWithSchedule('WORKING_HOURS');
        expect(screen.getByText('Runs 09:00–17:00 UTC, then stops after 30 min idle')).toBeInTheDocument();
    });
});

/** Renders the page with desktops in one Bedrock project, each named with the instance profile the
 * controller recorded against it. */
async function renderSessionsInBedrockProject(recordedInstanceProfileArns: (string | undefined)[]) {
    const context = initTestAppContext();

    const clusterSettings = context.getClusterSettingsService();
    vi.spyOn(clusterSettings, 'getDirectoryServiceSettings').mockResolvedValue({ provider: 'openldap' });
    vi.spyOn(clusterSettings, 'getVirtualDesktopSettings').mockResolvedValue({
        dcv_session: {
            idle_autostop_delay_max: 0,
            max_root_volume_memory: { value: 500, unit: 'gb' },
            working_hours: { start_up_time: '09:00', shut_down_time: '17:00' }
        }
    });

    const projects = context.client().projects();
    vi.spyOn(projects, 'getUserProjects').mockResolvedValue({
        projects: [
            {
                project_id: PROJECT_ID,
                name: 'project-a',
                title: 'Project A',
                enabled: true,
                bedrock: {
                    enabled: true,
                    model_ids: ['vendor.model-a'],
                    instance_profile_arn: PROJECT_INSTANCE_PROFILE_ARN
                }
            }
        ]
    } as any);
    vi.spyOn(projects, 'getProject').mockResolvedValue({ project: undefined } as any);

    const virtualDesktop = context.client().virtualDesktop();
    vi.spyOn(virtualDesktop, 'listSessions').mockResolvedValue({
        listing: recordedInstanceProfileArns.map((instanceProfileArn, index) => ({
            ...STOPPED_SESSION,
            idea_session_id: `session-${index}`,
            name: `my-desktop-${index}`,
            project: { project_id: PROJECT_ID, name: 'project-a', title: 'Project A' },
            server: { instance_id: `i-0000000000000000${index}`, instance_profile_arn: instanceProfileArn }
        }))
    } as any);

    render(
        <MemoryRouter>
            <MyVirtualDesktopSessions
                ideaPageId="my-virtual-desktop-sessions"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={() => {}}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );

    await screen.findByText('my-desktop-0', {}, { timeout: 10000 });
}

describe('my virtual desktop sessions project ai access', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('marks a desktop that is not on its project instance profile', async () => {
        await renderSessionsInBedrockProject([SHARED_INSTANCE_PROFILE_ARN]);
        expect(await screen.findByText(AI_ACCESS_PENDING_TEXT)).toBeInTheDocument();
        // the fixture is STOPPED, so the notice has to say what to do instead of promising a repair
        expect(screen.getByText(/Start it and it will come up with them/)).toBeInTheDocument();
    });

    it('marks a desktop whose instance profile was never recorded', async () => {
        // a desktop from before the controller recorded it: unknown is not access.
        await renderSessionsInBedrockProject([undefined]);
        expect(await screen.findByText(AI_ACCESS_PENDING_TEXT)).toBeInTheDocument();
    });

    it('says nothing about a desktop that is on its project instance profile', async () => {
        // both desktops are in the same project, so exactly one marker also proves the
        // project record was loaded before the assertion.
        await renderSessionsInBedrockProject([SHARED_INSTANCE_PROFILE_ARN, PROJECT_INSTANCE_PROFILE_ARN]);
        expect(await screen.findByText(AI_ACCESS_PENDING_TEXT)).toBeInTheDocument();
        expect(screen.getAllByText(AI_ACCESS_PENDING_TEXT)).toHaveLength(1);
    });
});


describe('desktop connection failures', () => {
    afterEach(() => vi.restoreAllMocks());

    it.each(['connect', 'download'])('shows a rejected %s request in the flashbar', async (action) => {
        const { virtualDesktop, flashbarItems } = await renderSessionWithSchedule('START_ALL_DAY', '/', 'READY');
        const request = vi.spyOn(virtualDesktop, 'getSessionConnectionInfo').mockRejectedValue(new Error('Your desktop session is restarting.'));
        const open = vi.spyOn(window, 'open').mockImplementation(() => null);
        const user = userEvent.setup();
        if (action === 'connect') {
            await user.click(screen.getByRole('button', { name: 'Connect' }));
        } else {
            await user.click(screen.getByRole('button', { name: /Actions/i }));
            await user.click(screen.getByText('DCV client file'));
        }
        await waitFor(() => expect(flashbarItems.some(item => flashbarText(item).includes('Your desktop session is restarting.'))).toBe(true));
        expect(request).toHaveBeenCalledWith({ connection_info: { idea_session_id: 'session-1', idea_session_owner: 'someuser', dcv_session_id: 'dcv-1' } });
        expect(open).not.toHaveBeenCalled();
    });

    it('shows a failure reason returned in a successful response', async () => {
        const { virtualDesktop, flashbarItems } = await renderSessionWithSchedule('START_ALL_DAY', '/', 'READY');
        vi.spyOn(virtualDesktop, 'getSessionConnectionInfo').mockResolvedValue({ connection_info: { failure_reason: 'The session could not restart after 3 attempts.' } });
        await userEvent.setup().click(screen.getByRole('button', { name: 'Connect' }));
        await waitFor(() => expect(flashbarItems.some(item => flashbarText(item).includes('could not restart after 3 attempts'))).toBe(true));
    });
});

/** Renders the page with whatever the listing answers, for 20 desktops or none. */
async function renderListing(listSessions: any, setup?: (virtualDesktop: any) => void) {
    const context = initTestAppContext();
    const clusterSettings = context.getClusterSettingsService();
    vi.spyOn(clusterSettings, 'getDirectoryServiceSettings').mockResolvedValue({ provider: 'openldap' });
    vi.spyOn(clusterSettings, 'getVirtualDesktopSettings').mockResolvedValue({
        dcv_session: { idle_autostop_delay_max: 0, max_root_volume_memory: { value: 500, unit: 'gb' }, working_hours: {} }
    });
    vi.spyOn(context.client().projects(), 'getUserProjects').mockResolvedValue({ projects: [] } as any);
    const virtualDesktop = context.client().virtualDesktop();
    vi.spyOn(virtualDesktop, 'listSessions').mockImplementation(listSessions);
    setup?.(virtualDesktop);
    const flashbarItems: any[] = [];
    render(
        <MemoryRouter>
            <MyVirtualDesktopSessions
                ideaPageId="my-virtual-desktop-sessions"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={(event: any) => flashbarItems.push(...event.items)}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );
    return { virtualDesktop, flashbarItems };
}

const twentyLinuxDesktops = Array.from({ length: 20 }, (_, index) => ({
    ...STOPPED_SESSION,
    idea_session_id: `session-${index}`,
    name: `desktop-${index}`,
    updated_on: '2020-01-01T00:00:00Z'
}));

describe('a listing that failed', () => {
    afterEach(() => vi.restoreAllMocks());

    it('never says there are no desktops when the first load failed, and can try again', async () => {
        const list = vi.fn().mockRejectedValueOnce(new Error('Service Unavailable')).mockResolvedValue({ listing: twentyLinuxDesktops });
        const { flashbarItems } = await renderListing(list);
        expect(await screen.findByText('Your desktops could not be loaded. They are not affected.', {}, { timeout: 10000 })).toBeInTheDocument();
        expect(screen.queryByText('No desktops yet')).not.toBeInTheDocument();
        expect(flashbarItems).toEqual([]);
        await userEvent.setup().click(screen.getByRole('button', { name: 'Try again' }));
        expect(await screen.findByText('desktop-19')).toBeInTheDocument();
        expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument();
    });

    it('keeps the rows and says so when a later refresh fails', async () => {
        const list = vi.fn().mockResolvedValueOnce({ listing: twentyLinuxDesktops }).mockRejectedValue(new Error('Service Unavailable'));
        await renderListing(list);
        await screen.findByText('desktop-19', {}, { timeout: 10000 });
        await userEvent.setup().click(screen.getByRole('button', { name: 'Refresh desktops' }));
        expect(await screen.findByText('Could not refresh. Showing the last known state.')).toBeInTheDocument();
        expect(screen.getByText('desktop-19')).toBeInTheDocument();
    });
});

describe('the operating system filter', () => {
    afterEach(() => vi.restoreAllMocks());

    const filter = async (name: string) => {
        const user = userEvent.setup();
        await user.click(createWrapper().findSegmentedControl()!.findSegments().find(segment => segment.getElement().textContent === name)!.getElement());
    };

    it('keeps the cards on screen while a filter loads', async () => {
        let answer: (value: any) => void = () => {};
        const list = vi.fn(({ filters }: any) => filters[0].value === 'windows'
            ? new Promise(resolve => { answer = resolve; })
            : Promise.resolve({ listing: twentyLinuxDesktops }));
        await renderListing(list);
        await screen.findByText('desktop-19', {}, { timeout: 10000 });
        await filter('Windows');
        expect(createWrapper().findCards()!.findLoadingText()).toBeNull();
        answer({ listing: [] });
    });

    it('says a filter matched nothing, and offers the way back', async () => {
        const list = vi.fn(({ filters }: any) => Promise.resolve({ listing: filters[0].value === 'windows' ? [] : twentyLinuxDesktops }));
        await renderListing(list);
        await screen.findByText('desktop-19', {}, { timeout: 10000 });
        await filter('Windows');
        expect(await screen.findByText('No Windows desktops.')).toBeInTheDocument();
        expect(screen.queryByText('No desktops yet')).not.toBeInTheDocument();
        await userEvent.setup().click(screen.getByRole('button', { name: 'Show all desktops' }));
        expect(await screen.findByText('desktop-19')).toBeInTheDocument();
    });

    it('hides a recently changed row the filter excludes, which the merge keeps on purpose', async () => {
        const fresh = { ...STOPPED_SESSION, name: 'just-changed', updated_on: new Date().toISOString() };
        expect(mergeSessions(new Map([['session-1', fresh as any]]), [], true).size).toBe(1);
        const list = vi.fn(({ filters }: any) => Promise.resolve({ listing: filters[0].value === 'windows' ? [] : [fresh] }));
        await renderListing(list);
        await screen.findByText('just-changed', {}, { timeout: 10000 });
        await filter('Windows');
        expect(await screen.findByText('No Windows desktops.')).toBeInTheDocument();
        expect(screen.queryByText('just-changed')).not.toBeInTheDocument();
    });

    it('ignores the answer to an older filter that arrives late', async () => {
        let answerAll: (value: any) => void = () => {};
        const list = vi.fn()
            .mockResolvedValueOnce({ listing: [] })
            .mockResolvedValueOnce({ listing: [] })
            .mockImplementationOnce(() => new Promise(resolve => { answerAll = resolve; }))
            .mockResolvedValue({ listing: [{ ...STOPPED_SESSION, name: 'win', base_os: 'windows2022' }] });
        await renderListing(list);
        await screen.findByText('No desktops yet', {}, { timeout: 10000 });
        await filter('Linux');
        await filter('All');
        await filter('Windows');
        await screen.findByText('win');
        answerAll({ listing: [] });
        await act(async () => {});
        expect(screen.getByText('win')).toBeInTheDocument();
    });

    it('sorts every operating system', () => {
        expect(matchesOsFilter({ base_os: 'windows2022' } as any, 'windows')).toBe(true);
        expect(matchesOsFilter({ base_os: 'rocky9' } as any, 'windows')).toBe(false);
        expect(matchesOsFilter({ software_stack: { base_os: 'windows2025' } } as any, 'linux')).toBe(false);
        expect(matchesOsFilter({ base_os: 'ubuntu2604' } as any, 'linux')).toBe(true);
        expect(matchesOsFilter({ base_os: 'ubuntu2604' } as any, '$all')).toBe(true);
    });
});

describe('launching with projects that failed to load', () => {
    afterEach(() => vi.restoreAllMocks());

    it('tries again when the form opens, so a passing failure does not read as no projects', async () => {
        let getUserProjects: any;
        await renderListing(vi.fn().mockResolvedValue({ listing: [] }), () => {
            const clients = AppContext.get().client();
            getUserProjects = vi.spyOn(clients.projects(), 'getUserProjects')
                .mockRejectedValueOnce(new Error('Service Unavailable'))
                .mockResolvedValue({ projects: [{ project_id: 'p1', title: 'Physics' }] } as any);
            vi.spyOn(clients.auth(), 'listUsersInGroup').mockResolvedValue({ listing: [] } as any);
            vi.spyOn(clients.virtualDesktopUtils(), 'listSupportedOS').mockResolvedValue({ listing: [] } as any);
            vi.spyOn(clients.virtualDesktop(), 'listSoftwareStacks').mockResolvedValue({ listing: [] } as any);
        });
        await screen.findByText('No desktops yet', {}, { timeout: 10000 });
        expect(getUserProjects).toHaveBeenCalledTimes(1);
        await userEvent.setup().click(screen.getAllByRole('button', { name: 'Launch desktop' })[0]);
        await waitFor(() => expect(getUserProjects).toHaveBeenCalledTimes(2));
        expect(await screen.findByText('Physics')).toBeInTheDocument();
        expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument();
    });
});

describe('connecting with pop-ups blocked', () => {
    afterEach(() => vi.restoreAllMocks());

    it('says the browser blocked the tab, and only then', async () => {
        const { virtualDesktop, flashbarItems } = await renderSessionWithSchedule('START_ALL_DAY', '/', 'READY');
        vi.spyOn(virtualDesktop, 'getSessionConnectionInfo').mockResolvedValue({ connection_info: { endpoint: 'https://cluster.example', web_url_path: '/dcv', access_token: 'token', dcv_session_id: 'dcv-1' } } as any);
        const open = vi.spyOn(window, 'open').mockReturnValueOnce(null).mockReturnValueOnce({} as any);
        const user = userEvent.setup();
        const connect = screen.getByRole('button', { name: 'Connect' });
        await user.click(connect);
        await waitFor(() => expect(flashbarItems.map(flashbarText)).toContain(POPUP_BLOCKED_MESSAGE));
        const raised = flashbarItems.length;
        await waitFor(() => expect(createWrapper(connect.parentElement!).findButton()!.findLoadingIndicator()).toBeNull());
        await user.click(connect);
        await waitFor(() => expect(open).toHaveBeenCalledTimes(2));
        expect(flashbarItems).toHaveLength(raised);
    });
});

describe('opening rename or resize', () => {
    afterEach(() => vi.restoreAllMocks());

    it('reports a failed request instead of doing nothing', async () => {
        const { flashbarItems } = await renderSessionWithSchedule('START_ALL_DAY');
        vi.spyOn(AppContext.get().client().virtualDesktopUtils(), 'listAllowedInstanceTypesForSession').mockRejectedValue(new Error('Service Unavailable'));
        const user = userEvent.setup();
        await user.click(screen.getByRole('button', { name: 'Actions' }));
        await user.click(screen.getByText('Rename or resize'));
        await waitFor(() => expect(flashbarItems.map(flashbarText)).toContain('Could not update virtual desktop: Service Unavailable'));
    });
});

describe('previews at scale', () => {
    afterEach(() => vi.restoreAllMocks());

    it('keeps fetching after one batch fails, and says which previews are unavailable', async () => {
        const ready = twentyLinuxDesktops.slice(0, 6).map((session, index) => ({ ...session, state: 'READY', dcv_session_id: `dcv-${index}` }));
        let getSessionScreenshot: any;
        await renderListing(vi.fn().mockResolvedValue({ listing: ready }), (virtualDesktop) => {
            getSessionScreenshot = vi.spyOn(virtualDesktop, 'getSessionScreenshot')
                .mockRejectedValueOnce(new Error('Service Unavailable'))
                .mockResolvedValue({ success: [{ dcv_session_id: 'dcv-3', image_data: 'aaaa' }, { dcv_session_id: 'dcv-4', image_data: 'aaaa' }, { dcv_session_id: 'dcv-5', image_data: 'aaaa' }] });
        });
        await waitFor(() => expect(screen.getAllByText('Preview unavailable. You can still connect.')).toHaveLength(3), { timeout: 10000 });
        await waitFor(() => expect(getSessionScreenshot).toHaveBeenCalledTimes(2), { timeout: 5000 });
        await waitFor(() => expect(screen.getAllByAltText('Desktop preview')).toHaveLength(3));
    });
});

describe('a refused stop or terminate', () => {
    afterEach(() => vi.restoreAllMocks());

    const stop = async (failed: any) => {
        const { virtualDesktop, flashbarItems } = await renderSessionWithSchedule('START_ALL_DAY', '/', 'READY');
        const stopSessions = vi.spyOn(virtualDesktop, 'stopSessions').mockResolvedValue({ success: [], failed: [failed] } as any);
        const user = userEvent.setup();
        await user.click(screen.getByRole('button', { name: 'Actions' }));
        await user.click(screen.getByText('Desktop state'));
        await user.click(screen.getByText('Stop'));
        // the confirm modal
        const dialog = await screen.findByText(/Are you sure you want to stop/);
        await user.click(within(dialog.closest('[role="dialog"]') as HTMLElement).getByRole('button', { name: 'Yes' }));
        await waitFor(() => expect(stopSessions).toHaveBeenCalled());
        return { stopSessions, flashbarItems, user };
    };

    it('shows the real reason, not a connections warning, when nobody is connected', async () => {
        const { flashbarItems, stopSessions } = await stop({ ...STOPPED_SESSION, failure_reason: "This desktop isn't running, so it can't be stopped right now." });
        await waitFor(() => expect(flashbarItems.some(item => flashbarText(item).includes("This desktop isn't running"))).toBe(true));
        const modal = new ModalWrapper(screen.getByText('This desktop is in use').closest<HTMLElement>(`.${ModalWrapper.rootSelector}`)!);
        expect(modal.isVisible()).toBe(false);
        expect(stopSessions).toHaveBeenCalledTimes(1);
    });

    it('asks only when someone is connected, then stops with force', async () => {
        const { stopSessions, user } = await stop({ ...STOPPED_SESSION, connection_count: 2, failure_reason: '2 connection(s) are open on this desktop.' });
        const warning = await screen.findByText(/There are 2 open connections/);
        await waitFor(() => expect(warning).toBeVisible());
        await user.click(within(warning.closest('[role="dialog"]') as HTMLElement).getByRole('button', { name: 'Yes' }));
        await waitFor(() => expect(stopSessions).toHaveBeenCalledTimes(2));
        expect(stopSessions.mock.calls[1][0].sessions![0].force).toBe(true);
    });
});

describe('needsQuickRefresh', () => {
    const desktops = (...states: string[]) => new Map(states.map((state, i) => [`s${i}`, { idea_session_id: `s${i}`, dcv_session_id: `d${i}`, state } as any]));

    it('refreshes quickly while a desktop is changing state', () => {
        expect(needsQuickRefresh(desktops('RESUMING'), {}, {}, 0)).toBe(true);
        expect(needsQuickRefresh(desktops('STOPPED'), {}, {}, 0)).toBe(false);
    });

    it('refreshes quickly for a minute after a desktop is ready without a preview', () => {
        const readySince: { [k: string]: number } = {};
        expect(needsQuickRefresh(desktops('READY'), {}, readySince, 1000)).toBe(true);
        expect(needsQuickRefresh(desktops('READY'), {}, readySince, 60_999)).toBe(true);
        expect(needsQuickRefresh(desktops('READY'), {}, readySince, 61_000)).toBe(false);
    });

    it('stops once the preview has arrived', () => {
        expect(needsQuickRefresh(desktops('READY'), { d0: { data: 'x' } }, {}, 0)).toBe(false);
    });
});
