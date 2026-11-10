import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import SubmitJob, {NoApplications} from './submit-job';
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

    it('confirms a large job before submitting it', async () => {
        const context = initTestAppContext();
        const scheduler = context.client().scheduler();
        vi.spyOn(scheduler, 'getUserApplications').mockResolvedValue({
            applications: [{
                ...APPLICATION,
                form_template: {
                    sections: [{
                        name: 'job-params',
                        params: [
                            { name: 'job_name', title: 'Job Name', param_type: 'text', data_type: 'str', default: 'test-job' },
                            { name: 'instance_type', title: 'Instance Type', param_type: 'text', data_type: 'str', default: 'c5.large' },
                            { name: 'cpus', title: 'CPUs', param_type: 'text', data_type: 'int', default: 20 }
                        ]
                    }]
                }
            }]
        } as any);
        vi.spyOn(scheduler, 'getInstanceTypeOptions').mockResolvedValue(INSTANCE_TYPE_OPTIONS as any);
        const submitJob = vi.spyOn(scheduler, 'submitJob').mockResolvedValue({ accepted: true } as any);

        const user = userEvent.setup();
        renderSubmitJob();

        await screen.findByText('Job Name');
        const submitButton = (await screen.findAllByRole('button', { name: /^Submit Job$/ }))[0];
        await waitFor(() => expect(submitButton).not.toBeDisabled());
        await user.click(submitButton);

        let dialog = await screen.findByRole('dialog', { name: 'Confirm Job Size' });
        await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
        expect(submitJob).not.toHaveBeenCalled();

        await user.click(submitButton);
        dialog = await screen.findByRole('dialog', { name: 'Confirm Job Size' });
        await user.click(within(dialog).getByRole('button', { name: 'Submit Job' }));
        await waitFor(() => expect(submitJob).toHaveBeenCalledTimes(1));
    });

    it('says to lower ncpus or turn hyper-threading on when ncpus exceeds cores', async () => {
        const context = initTestAppContext();
        const scheduler = context.client().scheduler();
        vi.spyOn(scheduler, 'getUserApplications').mockResolvedValue({
            applications: [{
                ...APPLICATION,
                form_template: {
                    sections: [{
                        name: 'job-params',
                        params: [
                            { name: 'job_name', title: 'Job Name', param_type: 'text', data_type: 'str', default: 'test-job' },
                            { name: 'instance_type', title: 'Instance Type', param_type: 'text', data_type: 'str', default: 'c7i.4xlarge' },
                            { name: 'ht_support', title: 'HT', param_type: 'text', data_type: 'str', default: 'false' },
                            { name: 'cpus', title: 'CPUs', param_type: 'text', data_type: 'int', default: 16 }
                        ]
                    }]
                }
            }]
        } as any);
        vi.spyOn(scheduler, 'getInstanceTypeOptions').mockResolvedValue({
            instance_types: [{ name: 'c7i.4xlarge', default_core_count: 8, threads_per_core: 2 }]
        } as any);
        const submitJob = vi.spyOn(scheduler, 'submitJob').mockResolvedValue({ accepted: true } as any);

        const user = userEvent.setup();
        renderSubmitJob();

        await screen.findByText('Job Name');
        const dryRun = await screen.findByRole('button', { name: /Dry Run/i });
        await waitFor(() => expect(dryRun).not.toBeDisabled());
        await user.click(dryRun);

        expect(await screen.findByText(
            'c7i.4xlarge has 8 cores with hyper-threading off; set CPUs per instance to 8 or enable hyper-threading'
        )).toBeInTheDocument();
        expect(submitJob).not.toHaveBeenCalled();
    });

    it('shows an architecture validation rejection before a job is queued', async () => {
        const context = initTestAppContext();
        const scheduler = context.client().scheduler();
        vi.spyOn(scheduler, 'getUserApplications').mockResolvedValue({ applications: [APPLICATION] } as any);
        vi.spyOn(scheduler, 'getInstanceTypeOptions').mockResolvedValue(INSTANCE_TYPE_OPTIONS as any);
        vi.spyOn(scheduler, 'submitJob').mockResolvedValue({
            accepted: false,
            validations: {results: [{error_code: 'INVALID_PARAMS', message: 'Requested instances run arm64, but the image is x86_64.'}]}
        });
        const user = userEvent.setup();
        renderSubmitJob();
        await screen.findByText('Job Name');
        const submit = (await screen.findAllByRole('button', {name: /^Submit Job$/}))[0];
        await waitFor(() => expect(submit).not.toBeDisabled());
        await user.click(submit);
        expect(await screen.findByText('Requested instances run arm64, but the image is x86_64.')).toBeInTheDocument();
        expect(screen.queryByText('Job Submitted Successfully')).toBeNull();
    });

    async function submitWithResult(result: any) {
        const context = initTestAppContext();
        const scheduler = context.client().scheduler();
        vi.spyOn(scheduler, 'getUserApplications').mockResolvedValue({ applications: [APPLICATION] } as any);
        vi.spyOn(scheduler, 'getInstanceTypeOptions').mockResolvedValue(INSTANCE_TYPE_OPTIONS as any);
        vi.spyOn(scheduler, 'submitJob').mockResolvedValue(result);
        const user = userEvent.setup();
        renderSubmitJob();
        await screen.findByText('Job Name');
        const dryRun = await screen.findByRole('button', { name: /Dry Run/i });
        await waitFor(() => expect(dryRun).not.toBeDisabled());
        await user.click(dryRun);
        return user;
    }

    const PRICED = {line_items: [{title: 'c5.large', quantity: 1, unit: 'per hour', unit_price: {amount: 0.1, unit: 'USD'}, total_price: {amount: 0.1, unit: 'USD'}}], line_items_total: {amount: 0.1, unit: 'USD'}};
    const UNPRICED = {price_unavailable: true, line_items: [{title: 'c5.large', quantity: 1, unit: 'per hour', unit_price: {amount: 0, unit: 'USD'}, total_price: {amount: 0, unit: 'USD'}}], line_items_total: {amount: 0, unit: 'USD'}};
    const BUDGET = {budget_name: 'fusion', budget_limit: {amount: 1000, unit: 'USD'}, actual_spend: {amount: 200, unit: 'USD'}, forecasted_spend: {amount: 300, unit: 'USD'}, job_usage_dollars: {amount: 0, unit: 'USD'}, job_usage_percent: 0, job_usage_percent_with_savings: 0};

    it('labels the estimate as the whole job, not an hourly rate', async () => {
        const user = await submitWithResult({accepted: true, job: {job_id: '101'}, estimated_bom_cost: PRICED});
        await user.click(await screen.findByRole('tab', {name: /Cost/}));
        expect(await screen.findByText('Estimated total for the requested walltime (1 hour if none set)')).toBeInTheDocument();
        expect(screen.queryByText(/Per Hour/)).toBeNull();
    });

    it('shows an unknown price as not available, never as $0', async () => {
        const user = await submitWithResult({accepted: true, job: {job_id: '101'}, estimated_bom_cost: UNPRICED, budget_usage: BUDGET});
        await user.click(await screen.findByRole('tab', {name: /Cost/}));
        expect(await screen.findByText('Price not available')).toBeInTheDocument();
        expect(screen.queryByText(/\$0/)).toBeNull();
        await user.click(await screen.findByRole('tab', {name: /Budget/}));
        expect(await screen.findByText("Budget impact can't be calculated without a price.")).toBeInTheDocument();
    });

    it('lists a refusal in words, without error codes', async () => {
        const user = await submitWithResult({
            accepted: false,
            validations: {results: [{error_code: 'instance_ami', message: 'Remove instance_ami to use the queue default.'}]},
            incidentals: {results: [{error_code: 'Licenses.NotFound', message: 'License comsol is not set up on this cluster.'}]}
        });
        void user;
        expect(await screen.findByText("This job can't be queued:")).toBeInTheDocument();
        expect(screen.getByText('Remove instance_ami to use the queue default.')).toBeInTheDocument();
        expect(screen.getByText('License comsol is not set up on this cluster.')).toBeInTheDocument();
        expect(screen.queryByText('Licenses.NotFound')).toBeNull();
        expect(screen.queryByText('Error Code')).toBeNull();
    });

    // the scheduler sanitizes the job name and picks the mount and extension, so the page opens
    // the path it was given and never one rebuilt from the job.
    it('opens the script the scheduler kept in the script workbench', async () => {
        const path = '/shared/home/alice/jobs/r_d_run_uid1.que';
        const user = await submitWithResult({dry_run: 'true', accepted: false, job: {name: 'r&d run', job_uid: 'uid1', owner: 'alice'}, job_script_path: path});
        await user.click(await screen.findByRole('tab', {name: 'Job Script'}));
        expect(await screen.findByRole('link', {name: 'Open in Script Workbench'}))
            .toHaveAttribute('href', `#/home/script-workbench?file=${encodeURIComponent(path)}`);
    });

    it('offers no script to open when the scheduler kept none', async () => {
        const user = await submitWithResult({dry_run: 'true', accepted: false, job: {name: 'test-job', job_uid: 'uid1', owner: 'alice'}});
        await user.click(await screen.findByRole('tab', {name: 'Job Script'}));
        expect(await screen.findByText('PBS Job Script')).toBeInTheDocument();
        expect(screen.queryByText('Open in Script Workbench')).toBeNull();
        expect(screen.queryByText(/opening it in the Script Workbench/)).toBeNull();
    });

    it('offers a way forward when no application is available', async () => {
        const navigate = vi.fn();
        render(<NoApplications navigate={navigate}/>);
        expect(screen.getByText('No applications are available for your projects. Ask an administrator to add you to a project, or write your own job script.')).toBeInTheDocument();
        await userEvent.setup().click(screen.getByRole('button', {name: 'Write script'}));
        expect(navigate).toHaveBeenCalledWith('/home/script-workbench');
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

it('shows the selected queue rules beside the instance type and updates when the queue changes', async () => {
    const context = initTestAppContext();
    const scheduler = context.client().scheduler();
    vi.spyOn(scheduler, 'listQueues').mockResolvedValue({listing: [
        {name: 'normal', instance_type_rules: 'Allowed instance types: c5 (large, xlarge).'},
        {name: 'arm', instance_type_rules: 'Allowed instance types: c8g (all sizes). Excluded: c8g (large).'},
    ]} as any);
    vi.spyOn(scheduler, 'getUserApplications').mockResolvedValue({applications: [{
        ...APPLICATION,
        form_template: {sections: [{params: [
            {name: 'queue', title: 'Queue', param_type: 'text', data_type: 'str', default: 'normal'},
            ...APPLICATION.form_template!.sections![0].params!,
        ]}]},
    }]} as any);
    vi.spyOn(scheduler, 'getInstanceTypeOptions').mockResolvedValue(INSTANCE_TYPE_OPTIONS as any);
    renderSubmitJob();
    const hint = await screen.findByText('Queue normal. Allowed instance types: c5 (large, xlarge).');
    expect(screen.getByRole('textbox', {name: 'Instance Type'})).toHaveAccessibleDescription(hint.textContent!);
    const user = userEvent.setup();
    const queue = screen.getByRole('textbox', {name: 'Queue'});
    await user.clear(queue);
    await user.type(queue, 'arm');
    expect(await screen.findByText('Queue arm. Allowed instance types: c8g (all sizes). Excluded: c8g (large).')).toBeInTheDocument();
    expect(screen.queryByText('Queue normal. Allowed instance types: c5 (large, xlarge).')).toBeNull();
    vi.restoreAllMocks();
});
