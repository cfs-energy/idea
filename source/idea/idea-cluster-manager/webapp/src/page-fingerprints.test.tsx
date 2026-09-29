import { act, render, screen, waitFor } from '@testing-library/react';
import { HashRouter, MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';
import App from './App';
import { AppContext } from './common';
import { fingerprint } from './dom-fingerprint';
import { initTestAppContext } from './test-support';
import HpcCustomAmis from './pages/hpc/hpc-custom-amis';
import FileBrowser from './pages/home/file-browser';
import MyVirtualDesktopSessions from './pages/virtual-desktops/my-virtual-desktop-sessions';
import PieOrDonutChart from './components/charts/pie-or-donut-chart';
import ClusterStatus from './pages/cluster-admin/cluster-status';
import {SettingsGroups} from './pages/cluster-admin/portal-settings';
import Projects from './pages/cluster-admin/projects';
import SubmitJob from './pages/hpc/submit-job';
import VirtualDesktopSessionDetail from './pages/virtual-desktops/virtual-desktop-session-detail';

// Structural baseline for the pages Cloudscape restyles most. A diff here is not
// automatically a bug, but it is always something a human has to look at.

const pageProps = {
    ideaPageId: 'fingerprint',
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

function stubVirtualDesktopSettings(context: AppContext) {
    const clusterSettings = context.getClusterSettingsService() as any;
    vi.spyOn(clusterSettings, 'getDirectoryServiceSettings').mockResolvedValue({ provider: 'openldap' } as any);
    vi.spyOn(clusterSettings, 'getVirtualDesktopSettings').mockResolvedValue({
        dcv_session: {
            working_hours: { start_up_time: '09:00', shut_down_time: '17:00' },
            idle_autostop_delay: 0,
            idle_autostop_delay_max: 0,
            max_root_volume_memory: 1000
        }
    } as any);
}

describe('page dom fingerprints', () => {
    it('costs history backfill', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
        vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({});
        vi.spyOn(context.client().clusterSettings(), 'describeSettingsCatalog').mockResolvedValue({settings: []});
        const status = {state: 'completed' as const, jobs_scanned: 0, points_built: 0, points_sent: 0, points_skipped: 0, errors: 0, dry_run: true, started_at: null, finished_at: null, last_error: null};
        vi.spyOn(context.client().clusterSettings(), 'getCostMetricsBackfill').mockResolvedValue(status);
        vi.spyOn(context.client().schedulerAdmin(), 'getJobMetricsBackfill').mockResolvedValue(status);
        const {container} = render(<MemoryRouter initialEntries={['/cluster/settings/cost-collection?operation=backfill-history']}><SettingsGroups pageProps={pageProps}/></MemoryRouter>);
        expect(await screen.findByRole('heading', {name: 'History backfill'})).toBeVisible();
        await screen.findByText(/Jobs: completed/);
        expect(container.querySelectorAll('#backfill-history')).toHaveLength(1);
        expect(screen.getAllByRole('heading', {name: 'History backfill'})).toHaveLength(1);
        const operation = container.querySelector('#backfill-history')!;
        operation.querySelectorAll('input').forEach(input => {if (input.value.match(/^\d{4}\//)) input.setAttribute('value', 'YYYY/MM/DD');});
        expect(fingerprint(operation)).toMatchSnapshot();
    });

    it.each(['health', 'inactive settings'])('does not mount or poll history from %s', async page => {
        vi.useFakeTimers();
        try {
            const context = initTestAppContext();
            vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
            vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
            vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({});
            vi.spyOn(context.client().clusterSettings(), 'listClusterModules').mockResolvedValue({listing: []});
            vi.spyOn(context.client().clusterSettings(), 'listClusterHosts').mockResolvedValue({listing: []});
            vi.spyOn(context.client().clusterSettings(), 'describeSettingsCatalog').mockResolvedValue({settings: []});
            const cost = vi.spyOn(context.client().clusterSettings(), 'getCostMetricsBackfill');
            const jobs = vi.spyOn(context.client().schedulerAdmin(), 'getJobMetricsBackfill');
            const {container, unmount} = render(<MemoryRouter initialEntries={[page === 'health' ? '/cluster/status' : '/cluster/settings/appearance']}>
                {page === 'health' ? <ClusterStatus {...pageProps}/> : <SettingsGroups pageProps={pageProps}/>}
            </MemoryRouter>);
            await act(async () => { vi.advanceTimersByTime(30000); });
            expect(container.querySelector('#backfill-history')).toBeNull();
            expect(cost).not.toHaveBeenCalled();
            expect(jobs).not.toHaveBeenCalled();
            unmount();
        } finally { vi.useRealTimers(); vi.restoreAllMocks(); }
    });

    it('custom images', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
        vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
        vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages').mockResolvedValue({listing: []});
        vi.spyOn(context.client().virtualDesktopAdmin(), 'listDesktopImages').mockResolvedValue({listing: []});
        const {container} = render(<MemoryRouter><HpcCustomAmis {...pageProps}/></MemoryRouter>);
        expect(await screen.findByRole('heading', {name: 'Custom images'})).toBeVisible();
        expect(screen.getByLabelText('Breadcrumbs')).toHaveTextContent('Custom images');
        await waitFor(() => expect(screen.queryByText('Loading images')).not.toBeInTheDocument());
        expect(fingerprint(container)).toMatchSnapshot();
    });

    it('sign-in', async () => {
        initTestAppContext();
        // HashRouter as in index.tsx: the unauthenticated route renders sign-in.
        const { container } = render(
            <HashRouter>
                <App />
            </HashRouter>
        );
        await screen.findAllByText('Sign In');

        // Site of the fragment-flattening regression: title, form and actions must stay three separate
        // ColumnLayout cells. Pinned separately so regenerating the snapshot cannot quietly accept it.
        // Couples to a Cloudscape name segment, which does change, but a rename fails loudly at length 0.
        const authContent = container.querySelector('.auth-content')!;
        expect(authContent).not.toBeNull();
        expect(authContent.querySelectorAll(':scope > div > [class*="awsui_grid-column"]')).toHaveLength(3);

        expect(fingerprint(container)).toMatchSnapshot();
    });

    it('submit-job', async () => {
        initTestAppContext();
        const { container } = render(
            <MemoryRouter>
                <SubmitJob {...pageProps} />
            </MemoryRouter>
        );
        await screen.findByText('Submit Job Form');
        expect(fingerprint(container)).toMatchSnapshot();
    });

    it('projects', async () => {
        const context = initTestAppContext();
        // Projects reads cluster-manager module settings in componentDidMount, so
        // stub it the way every other service call in this file is stubbed.
        vi.spyOn(context.getClusterSettingsService() as any, 'getModuleSettings').mockResolvedValue({
            bedrock: { enabled: false, model_ids: [] }
        } as any);
        vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({
            listing: [
                {
                    project_id: 'project-1',
                    name: 'default',
                    title: 'Default Project',
                    description: 'fingerprint fixture',
                    enabled: true,
                    ldap_groups: ['default-project-group'],
                    tags: []
                }
            ],
            paginator: { page_size: 20, total: 1 }
        } as any);
        const { container } = render(
            <MemoryRouter>
                <Projects {...pageProps} />
            </MemoryRouter>
        );
        await screen.findByText('Default Project');
        expect(fingerprint(container)).toMatchSnapshot();
    });

    it('virtual-desktops', async () => {
        const context = initTestAppContext();
        stubVirtualDesktopSettings(context);
        vi.spyOn(context.client().projects(), 'getUserProjects').mockResolvedValue({ projects: [] } as any);
        vi.spyOn(context.client().projects(), 'getProject').mockResolvedValue({ project: { name: 'default' } } as any);
        vi.spyOn(context.client().virtualDesktop(), 'listSessions').mockResolvedValue({ listing: [] } as any);
        vi.spyOn(context.client().virtualDesktopUtils(), 'listSupportedOS').mockResolvedValue({ listing: [] } as any);
        const { container } = render(
            <MemoryRouter>
                <MyVirtualDesktopSessions {...pageProps} />
            </MemoryRouter>
        );
        expect(await screen.findByRole('heading', {name: 'My desktops'})).toBeInTheDocument();
        expect(fingerprint(container)).toMatchSnapshot();
    });

    it('virtual-desktop-session-detail', async () => {
        const context = initTestAppContext();
        stubVirtualDesktopSettings(context);
        const admin = context.client().virtualDesktopAdmin();
        vi.spyOn(admin, 'getSessionInfo').mockResolvedValue({
            session: {
                idea_session_id: 'session-1',
                dcv_session_id: 'dcv-1',
                name: 'fingerprint-session',
                state: 'READY',
                owner: 'fingerprint-user'
            }
        } as any);
        vi.spyOn(admin, 'listSessionPermissions').mockResolvedValue({ listing: [] } as any);
        vi.spyOn(context.client().virtualDesktopDCV(), 'describeSessions').mockResolvedValue({
            response: {
                sessions: {
                    'dcv-1': {
                        id: 'dcv-1',
                        name: 'fingerprint-session',
                        server: {
                            id: 'ZmluZ2VycHJpbnQ=',
                            endpoints: [
                                { protocol: 'HTTP', port: 8443, web_url_path: '/first' },
                                { protocol: 'QUIC', port: 8443, web_url_path: '/second' }
                            ],
                            tags: [
                                { key: 'Name', value: 'fingerprint-a' },
                                { key: 'Owner', value: 'fingerprint-b' }
                            ]
                        }
                    }
                }
            }
        } as any);

        const { container } = render(
            <MemoryRouter initialEntries={['/desktop/session-1?tab=session-health']}>
                <Routes>
                    <Route path="/desktop/:idea_session_id" element={<VirtualDesktopSessionDetail {...pageProps} />} />
                </Routes>
            </MemoryRouter>
        );
        await screen.findByText('Server Endpoints');
        // Tabs sets its roving tabindex in an effect that lands after the panel
        // content does, so fingerprinting on findByText alone races it.
        await waitFor(() => expect(screen.getByRole('tab', { selected: true })).toHaveAttribute('tabindex', '0'));

        // Site of the second fragment-flattening regression: each endpoint's three fields must stay three
        // ColumnLayout cells, taking the count from 6 to 2 when collapsed. Pinned outside the snapshot.
        const firstCell = screen.getByText('Endpoint 1 Protocol').closest('[class*="awsui_grid-column"]')!;
        expect(firstCell).not.toBeNull();
        expect(firstCell.parentElement!.querySelectorAll(':scope > [class*="awsui_grid-column"]')).toHaveLength(6);

        expect(fingerprint(container)).toMatchSnapshot();
    });

    it('pie-or-donut-chart', async () => {
        initTestAppContext();
        // The only chart on a fingerprinted page; charts are where Cloudscape
        // emits BEM class names and token-driven fills.
        const { container } = render(
            <PieOrDonutChart
                headerText="Fingerprint Chart"
                headerDescription="chart fixture"
                enableSelection={true}
                defaultChartMode="donutchart"
                data={[
                    { title: 'Running', value: 3 },
                    { title: 'Stopped', value: 1 }
                ]}
            />
        );
        await screen.findByText('Fingerprint Chart');
        expect(fingerprint(container)).toMatchSnapshot();
    });

    it('file-browser', async () => {
        const context = initTestAppContext();
        const clusterSettings = context.getClusterSettingsService() as any;
        vi.spyOn(clusterSettings, 'getModuleSettings').mockRejectedValue({ errorCode: 'MODULE_NOT_FOUND' });
        vi.spyOn(context.client().fileBrowser(), 'listFiles').mockResolvedValue({
            cwd: '/home/fingerprint',
            listing: [{ name: 'notes.txt', file_id: 'notes.txt', size: 12, is_dir: false, is_hidden: false }]
        } as any);
        const { container } = render(
            <MemoryRouter>
                <FileBrowser {...pageProps} />
            </MemoryRouter>
        );
        expect(await screen.findByRole('heading', {name: 'Files'})).toBeInTheDocument();
        expect(fingerprint(container)).toMatchSnapshot();
    });
});
