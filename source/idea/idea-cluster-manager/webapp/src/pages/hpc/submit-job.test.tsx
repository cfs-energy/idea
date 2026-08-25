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
import SubmitJob from './submit-job';
import { HpcApplication } from '../../client/data-model';
import IdeaException from '../../common/exceptions';
import { initTestAppContext } from '../../test-support';

const APPLICATION: HpcApplication = {
    application_id: 'app-1',
    title: 'Ansys Fluent',
    job_script_interpreter: 'pbs',
    job_script_template: '#!/bin/bash\n#PBS -N %job_name%\n#PBS -l instance_type=%instance_type%\n',
    projects: [{ project_id: 'p-1', name: 'proj-1', title: 'Project 1' }],
    form_template: {
        sections: [
            {
                name: 'job-params',
                params: [
                    { name: 'job_name', title: 'Job Name', param_type: 'text', data_type: 'str', default: 'test-job' },
                    { name: 'instance_type', title: 'Instance Type', param_type: 'text', data_type: 'str', default: 'c5.large' },
                    { name: 'cpus', title: 'CPUs', param_type: 'text', data_type: 'int', default: 2 }
                ]
            }
        ]
    }
};

// 2 cpus per instance, so the 2 requested cpus size a single node job and the
// large job confirmation modal stays out of the way.
const INSTANCE_TYPE_OPTIONS = {
    instance_types: [{ name: 'c5.large', default_core_count: 1, threads_per_core: 2 }]
};

function renderSubmitJob(props: any = {}) {
    // the page loads its application from ?state=, as the application catalog links do
    const state = btoa(JSON.stringify({ applicationId: APPLICATION.application_id }));
    render(
        <MemoryRouter initialEntries={[`/?state=${state}`]}>
            <SubmitJob
                ideaPageId="submit-job"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={() => {}}
                flashbarItems={[]}
                {...props}
            />
        </MemoryRouter>
    );
}

describe('submit job page', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('renders the submit job form', async () => {
        initTestAppContext();
        render(
            <MemoryRouter>
                <SubmitJob
                    ideaPageId="submit-job"
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
        const headers = await screen.findAllByText('Submit Job');
        expect(headers.length).toBeGreaterThan(0);
        expect(await screen.findByText('Submit Job Form')).toBeInTheDocument();
    });

    // a failed Scheduler.SubmitJob rejects the client promise: the reason has to reach the form,
    // otherwise the click looks like it did nothing at all.
    it('reports a rejected SubmitJob call on the form', async () => {
        const context = initTestAppContext();
        const scheduler = context.client().scheduler();
        vi.spyOn(scheduler, 'getUserApplications').mockResolvedValue({ applications: [APPLICATION] } as any);
        vi.spyOn(scheduler, 'getInstanceTypeOptions').mockResolvedValue(INSTANCE_TYPE_OPTIONS as any);
        vi.spyOn(scheduler, 'submitJob').mockRejectedValue(
            new IdeaException({
                errorCode: 'JOB_SUBMISSION_FAILED',
                message: 'Failed to submit job: qsub: Queue is not enabled'
            })
        );

        const user = userEvent.setup();
        renderSubmitJob();

        await screen.findByText('Job Name');
        const submitButton = (await screen.findAllByRole('button', { name: /^Submit Job$/ }))[0];
        await waitFor(() => expect(submitButton).not.toBeDisabled());
        await user.click(submitButton);

        expect(await screen.findByText('Job Submission Failed')).toBeInTheDocument();
        expect(await screen.findByText(/qsub: Queue is not enabled/)).toBeInTheDocument();
    });

    // a hung scheduler api answers with a timeout rather than an application listing. reported
    // once, the user knows why the form is empty instead of only finding an unusable page.
    it('reports a scheduler api failure while loading the application', async () => {
        const context = initTestAppContext();
        const scheduler = context.client().scheduler();
        vi.spyOn(scheduler, 'getUserApplications').mockRejectedValue(
            new IdeaException({
                errorCode: 'SOCKET_TIMEOUT',
                message: "HTTPSConnectionPool(host='internal-alb', port=443): Read timed out."
            })
        );

        const onFlashbarChange = vi.fn();
        renderSubmitJob({ onFlashbarChange: onFlashbarChange });

        await waitFor(() => expect(onFlashbarChange).toHaveBeenCalled());
        const item = onFlashbarChange.mock.calls[0][0].items[0];
        expect(item.type).toBe('error');
        expect(item.header).toBe('Failed to load the application');
        expect(item.content).toContain('Read timed out');
    });
});
