import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import MyCosts from './my-costs';
import {initTestAppContext} from '../../test-support';
import {AppContext} from '../../common';

const SUMMARY = {
    username: 'user-a',
    window: 'last_30_days',
    start_date: '2026-08-03',
    end_date: '2026-09-01',
    currency: 'USD',
    ai: {
        invocations: 3,
        total_tokens: 300,
        cost: 12.5,
        estimated: true,
        projects: [
            {
                project_id: 'project-1',
                project_name: 'project-a',
                project_title: 'Project A',
                invocations: 3,
                total_tokens: 300,
                cost: 12.5,
                estimated: true,
                by_model: [
                    {
                        model_id: 'vendor-a.model-1',
                        invocations: 3,
                        total_tokens: 300,
                        cost: 12.5,
                        estimated: true
                    }
                ]
            }
        ]
    },
    jobs: {
        job_count: 2,
        cost: 8.25,
        estimated: true,
        by_project: [{name: 'project-a', job_count: 2, cost: 8.25}],
        by_queue: [{name: 'normal', job_count: 2, cost: 8.25}],
        recent_jobs: [
            {
                job_id: '101',
                name: 'solve',
                queue: 'normal',
                project: 'project-a',
                end_time: '2026-08-30T10:00:00+00:00',
                cost: 4.5
            }
        ]
    },
    desktops: {
        session_count: 2,
        hours: 6.5,
        cost: 0.65,
        estimated: true,
        sessions: [
            {
                idea_session_id: 'sess-1',
                name: 'desktop-one',
                instance_type: 'm5.large',
                state: 'STOPPED',
                hours: 4.5,
                cost: 0.45,
                estimated: true
            },
            {
                idea_session_id: 'sess-2',
                name: 'desktop-two',
                instance_type: 'zz.unknown',
                state: 'STOPPED',
                hours: 2.0,
                estimated: true,
                price_unavailable: true
            }
        ]
    }
};

const EMPTY_SUMMARY = {
    username: 'user-a',
    window: 'last_30_days',
    ai: {invocations: 0, total_tokens: 0, cost: 0, estimated: true, projects: []},
    jobs: {job_count: 0, cost: 0, estimated: true, by_project: [], by_queue: [], recent_jobs: []},
    desktops: {session_count: 0, hours: 0, cost: 0, estimated: true, sessions: []}
};

const stubMyCosts = (context: AppContext, summary: any) => {
    vi.spyOn(context.client().myCosts(), 'getSummary').mockResolvedValue(summary);
};

const SECTIONS = ['AI usage', 'Desktops', 'Jobs'];

/** Section headings in DOM order. The side nav header is an h2 too, so filter. */
const sectionOrder = (): (string | null)[] =>
    screen.getAllByRole('heading', {level: 2})
        .map((node) => node.textContent)
        .filter((text) => SECTIONS.includes(text ?? ''));

const renderMyCosts = () => {
    render(
        <MemoryRouter>
            <MyCosts
                ideaPageId="my-costs"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{text: 'IDEA', href: '#/'}}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={() => {}}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );
};

describe('my costs page', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('renders the three sections with the measured numbers', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        expect(await screen.findByRole('heading', {name: 'My Costs'}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByRole('heading', {name: 'AI usage'})).toBeInTheDocument();
        expect(await screen.findByRole('heading', {name: 'Jobs'})).toBeInTheDocument();
        expect(await screen.findByRole('heading', {name: 'Desktops'})).toBeInTheDocument();

        expect(await screen.findByText('Project A')).toBeInTheDocument();
        expect(await screen.findByText('solve')).toBeInTheDocument();
        expect(await screen.findByText('desktop-one')).toBeInTheDocument();
    }, 20000);

    it('marks every section as estimated and says these are not the AWS bill', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        const badges = await screen.findAllByText('Estimated', {}, {timeout: 10000});
        expect(badges.length).toBe(3);
        expect(await screen.findByText(/not the AWS bill/)).toBeInTheDocument();
    });

    it('expands a project row to its models', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        await screen.findByText('Project A', {}, {timeout: 10000});
        expect(screen.queryByText('vendor-a.model-1')).toBeNull();
        await userEvent.click(screen.getByRole('button', {name: 'Show models for Project A'}));
        expect(await screen.findByText('vendor-a.model-1')).toBeInTheDocument();
    });

    it('reports hours without a cost when the instance price is unavailable', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        expect(await screen.findByText('Price not available', {}, {timeout: 10000})).toBeInTheDocument();
    });

    it('shows a per section empty state when nothing was measured', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        renderMyCosts();

        expect(await screen.findByText('No desktops', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findAllByText('No completed jobs')).toHaveLength(2);
    });

    it('hides the AI section entirely when the user has no AI usage', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        renderMyCosts();

        // An unused section is absent, not shown empty.
        await screen.findByText('No desktops', {}, {timeout: 10000});
        expect(screen.queryByRole('heading', {name: 'AI usage'})).toBeNull();
        expect(screen.queryByText('No AI usage')).toBeNull();
    });

    it('still shows the AI section when the usage read failed', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {...EMPTY_SUMMARY, ai: {is_unavailable: true}});
        renderMyCosts();

        // An unavailable read must not be hidden the way no usage is.
        expect(await screen.findByRole('heading', {name: 'AI usage'}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText('AI usage is not available')).toBeInTheDocument();
    });

    it('orders the sections AI, then desktops, then jobs', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, SUMMARY);
        renderMyCosts();

        await screen.findByRole('heading', {name: 'AI usage'}, {timeout: 10000});
        expect(sectionOrder()).toEqual(['AI usage', 'Desktops', 'Jobs']);
    });

    it('orders desktops before jobs when the AI section is hidden', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, EMPTY_SUMMARY);
        renderMyCosts();

        await screen.findByText('No desktops', {}, {timeout: 10000});
        expect(sectionOrder()).toEqual(['Desktops', 'Jobs']);
    });

    it('reports desktop cost as not available when no session could be priced', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            jobs: {is_unavailable: true},
            desktops: {
                session_count: 2,
                hours: 6.5,
                cost: 0,
                unpriced_sessions: 2,
                estimated: true,
                sessions: [
                    {idea_session_id: 's1', name: 'one', instance_type: 'm6g.xlarge', state: 'STOPPED', hours: 4.5, price_unavailable: true},
                    {idea_session_id: 's2', name: 'two', instance_type: 'g4dn.2xlarge', state: 'STOPPED', hours: 2.0, price_unavailable: true}
                ]
            }
        });
        renderMyCosts();

        // Real hours with no prices must not read as $0.00.
        expect(await screen.findByText('Not available', {}, {timeout: 10000})).toBeInTheDocument();
        expect(screen.queryByText('$0.00')).toBeNull();
    });

    it('shows a terminated desktop with its hours and cost', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            desktops: {
                session_count: 1,
                hours: 24.0,
                cost: 2.4,
                estimated: true,
                sessions: [
                    {
                        idea_session_id: 's1',
                        name: 'gone-but-billed',
                        instance_type: 'm5.large',
                        state: 'Terminated',
                        hours: 24.0,
                        cost: 2.4
                    }
                ]
            }
        });
        renderMyCosts();

        // A desktop deleted inside the window still cost what it cost while running.
        expect(await screen.findByText('gone-but-billed', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText('Terminated')).toBeInTheDocument();
        expect(screen.queryByText('DELETED')).toBeNull();
        // Once in the row and once in the section total, so it counts toward the hours.
        expect(await screen.findAllByText('24.00 h')).toHaveLength(2);
        expect(await screen.findByText('1')).toBeInTheDocument();
    }, 20000);

    it('marks a session whose stop time had to be inferred', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            desktops: {
                session_count: 2,
                hours: 6.5,
                cost: 0.65,
                estimated: true,
                sessions: [
                    {idea_session_id: 's1', name: 'recorded', instance_type: 'm5.large', state: 'STOPPED', hours: 4.5, cost: 0.45},
                    {idea_session_id: 's2', name: 'legacy', instance_type: 'm5.large', state: 'STOPPED', hours: 2.0, cost: 0.2, stop_time_estimated: true}
                ]
            }
        });
        renderMyCosts();

        // A session stopped before the stop time was recorded is counted to its last write, which
        // is an upper bound.
        expect(await screen.findByText('2.00 h (estimated)', {}, {timeout: 10000})).toBeInTheDocument();
        // The session with a recorded stop time is not marked.
        expect(await screen.findByText('4.50 h')).toBeInTheDocument();
    });

    it('marks a partly priced total and says how many rows are missing', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            desktops: {
                session_count: 2,
                hours: 6.5,
                cost: 0.45,
                unpriced_sessions: 1,
                estimated: true,
                sessions: [
                    {idea_session_id: 's1', name: 'one', instance_type: 'm5.large', state: 'STOPPED', hours: 4.5, cost: 0.45},
                    {idea_session_id: 's2', name: 'two', instance_type: 'zz.unknown', state: 'STOPPED', hours: 2.0, price_unavailable: true}
                ]
            }
        });
        renderMyCosts();

        expect(await screen.findByText('$0.45 *', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText(/1 of 2 sessions could not be priced/)).toBeInTheDocument();
    });

    it('shows the section as unavailable rather than as zero when a read failed', async () => {
        const context = initTestAppContext();
        stubMyCosts(context, {
            ...EMPTY_SUMMARY,
            jobs: {is_unavailable: true},
            desktops: {is_unavailable: true}
        });
        renderMyCosts();

        expect(await screen.findByText('Job costs are not available', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText('Desktop costs are not available')).toBeInTheDocument();
    });
});
