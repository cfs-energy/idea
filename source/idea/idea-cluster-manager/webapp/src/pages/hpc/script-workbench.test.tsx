import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ScriptWorkbench from './script-workbench';
import { SubmitJobResult } from '../../client/data-model';
import { initTestAppContext } from '../../test-support';

const DRY_RUN_ACCEPTED: SubmitJobResult = {
    dry_run: 'true',
    accepted: false,
    job: { job_id: 'tbd', job_uid: 'job-uid-1' },
    validations: { results: [] },
    incidentals: { results: [] }
};

/** Scheduler.SubmitJob answers a rejection with a successful API envelope: accepted is false, the job
 * keeps its placeholder id and the reasons are in validations/incidentals. */
function rejected(results: { error_code: string; message: string }[]): SubmitJobResult {
    return {
        accepted: false,
        job: { job_id: 'tbd', job_uid: 'job-uid-1' },
        validations: { results: results },
        incidentals: { results: [] }
    };
}

function accepted(): SubmitJobResult {
    return {
        accepted: true,
        job: { job_id: '101', job_uid: 'job-uid-1' },
        validations: { results: [] },
        incidentals: { results: [] }
    };
}

/** Records every SubmitJob request so the submission id can be inspected. A real submit clears the
 * dry-run state, so each retry goes through Dry Run again, as it does in the ui. */
async function submitRecordingRequests(result: SubmitJobResult) {
    const context = initTestAppContext();
    const scheduler = context.client().scheduler();
    const requests: any[] = [];
    vi.spyOn(scheduler, 'submitJob').mockImplementation((req: any) => {
        requests.push(req);
        return Promise.resolve(req.dry_run ? DRY_RUN_ACCEPTED : result);
    });

    const user = userEvent.setup();
    renderScriptWorkbench();
    await user.click(await screen.findByRole('button', { name: /Insert sample PBS script/i }));

    const submitOnce = async () => {
        await user.click(await screen.findByRole('button', { name: /Dry Run/i }));
        await screen.findByText('Dry Run Successful');
        await user.click(await screen.findByRole('button', { name: /Submit Job/i }));
    };

    await submitOnce();
    const realRequests = () => requests.filter((request) => !request.dry_run);
    return { submitOnce, realRequests };
}

const SONNET = 'anthropic.claude-sonnet-4-5-20250929-v1:0';
const PROFILE_ARN = 'arn:aws:bedrock:us-east-2:111122223333:application-inference-profile/abc123';

/** The sample script names project "default", so the stub answers with a project of that name. */
async function renderWithProject(bedrock: any) {
    const context = initTestAppContext();
    vi.spyOn(context.client().projects(), 'getUserProjects').mockResolvedValue({
        projects: [{project_id: 'p-1', name: 'default', title: 'Default Project', bedrock}]
    });
    const user = userEvent.setup();
    renderScriptWorkbench();
    await user.click(await screen.findByRole('button', {name: /Insert sample PBS script/i}));
    return user;
}

function renderScriptWorkbench() {
    render(
        <MemoryRouter>
            <ScriptWorkbench
                ideaPageId="script-workbench"
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
}

async function submitRejectedJob(result: SubmitJobResult) {
    const context = initTestAppContext();
    const scheduler = context.client().scheduler();
    vi.spyOn(scheduler, 'submitJob').mockImplementation((req: any) =>
        Promise.resolve(req.dry_run ? DRY_RUN_ACCEPTED : result)
    );

    const user = userEvent.setup();
    renderScriptWorkbench();

    await user.click(await screen.findByRole('button', { name: /Insert sample PBS script/i }));
    await user.click(await screen.findByRole('button', { name: /Dry Run/i }));
    await screen.findByText('Dry Run Successful');
    await user.click(await screen.findByRole('button', { name: /Submit Job/i }));
}

describe('script workbench', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("shows the models the script's project allows, with the profile arn to pass", async () => {
        await renderWithProject({
            enabled: true,
            model_ids: [SONNET],
            inference_profile_arns: {[SONNET]: PROFILE_ARN}
        });

        expect(await screen.findByText(PROFILE_ARN)).toBeInTheDocument();
    });

    it('shows no models section for a project without them', async () => {
        await renderWithProject({enabled: false});

        await screen.findByText(/#PBS -P project_name/);
        // the section itself must be absent, not merely empty: a rendered "--" would still pass an
        // assertion that only looks for the arn.
        expect(screen.queryByText('AI Models')).toBeNull();
        expect(screen.queryByText(PROFILE_ARN)).toBeNull();
    });
    it('reports a rejected job submission and its reasons', async () => {
        await submitRejectedJob(
            rejected([
                { error_code: 'BUDGET_LIMIT_EXCEEDED', message: 'Project budget has been exhausted.' }
            ])
        );

        expect(await screen.findByText('Job Submission Failed')).toBeInTheDocument();
        expect(await screen.findByText('Project budget has been exhausted.')).toBeInTheDocument();
    });

    it('reports a rejected job submission that carries no reason', async () => {
        await submitRejectedJob(rejected([]));

        expect(await screen.findByText('Job Submission Failed')).toBeInTheDocument();
        expect(
            await screen.findByText('The scheduler did not return a reason. Contact your cluster administrator.')
        ).toBeInTheDocument();
    });
    it('sends a client_submission_id so the server can deduplicate', async () => {
        const { realRequests } = await submitRecordingRequests(accepted());

        expect(realRequests()).toHaveLength(1);
        expect(realRequests()[0].client_submission_id).toBeTruthy();
    });

    it('reuses the submission id when a failed submission is retried', async () => {
        // the case the dedupe exists for: the first attempt did not queue a job, so the retry
        // has to carry the same id or it becomes a second job
        const { submitOnce, realRequests } = await submitRecordingRequests(rejected([]));
        await screen.findByText('Job Submission Failed');
        await submitOnce();

        expect(realRequests()).toHaveLength(2);
        // truthy first: two requests that both omit the id would compare equal and prove nothing
        expect(realRequests()[0].client_submission_id).toBeTruthy();
        expect(realRequests()[1].client_submission_id).toBe(
            realRequests()[0].client_submission_id
        );
    });

    it('takes a new submission id once a job has been queued', async () => {
        const { submitOnce, realRequests } = await submitRecordingRequests(accepted());
        await submitOnce();

        expect(realRequests()).toHaveLength(2);
        expect(realRequests()[1].client_submission_id).not.toBe(
            realRequests()[0].client_submission_id
        );
    });
});
