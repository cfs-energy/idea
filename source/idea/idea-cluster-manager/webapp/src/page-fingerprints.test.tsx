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

import { render, screen, waitFor } from '@testing-library/react';
import { HashRouter, MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';
import App from './App';
import { AppContext } from './common';
import { fingerprint } from './dom-fingerprint';
import { initTestAppContext } from './test-support';
import FileBrowser from './pages/home/file-browser';
import MyVirtualDesktopSessions from './pages/virtual-desktops/my-virtual-desktop-sessions';
import PieOrDonutChart from './components/charts/pie-or-donut-chart';
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
        await screen.findAllByText('Virtual Desktops');
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
        await screen.findAllByText('File Browser');
        expect(fingerprint(container)).toMatchSnapshot();
    });
});
