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
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import MyVirtualDesktopSessions from './my-virtual-desktop-sessions';
import { initTestAppContext } from '../../test-support';

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
async function startSessionAndCaptureFlashbar(resumeSessionsResult: any) {
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
    vi.spyOn(projects, 'getProject').mockResolvedValue({ project: undefined } as any);

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
    await user.click(await screen.findByRole('button', { name: /Actions/i }));
    await user.click(await screen.findByText('Virtual Desktop State'));
    await user.click(await screen.findByText('Start'));
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
            'Could not start virtual desktop session-1. No reason was returned, contact your administrator.'
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
