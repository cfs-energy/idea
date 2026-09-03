import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import UserCostsPage from './user-costs';
import {initTestAppContext} from '../../test-support';
import {AppContext} from '../../common';

const LISTING = {
    window: 'last_30_days',
    start_date: '2026-08-03',
    end_date: '2026-09-01',
    currency: 'USD',
    ai_unavailable: false,
    jobs_unavailable: false,
    desktops_unavailable: false,
    listing: [
        {
            username: 'bob',
            ai_requests: 8,
            ai_tokens: 1050,
            ai_cost: 105.0,
            desktop_session_count: 0,
            desktop_hours: 0,
            desktop_cost: 0,
            desktop_unpriced_sessions: 0,
            job_count: 4,
            job_cost: 10.0,
            job_unpriced_jobs: 0,
            total_cost: 115.0
        },
        {
            username: 'alice',
            ai_requests: 2,
            ai_tokens: 150,
            ai_cost: 15.0,
            desktop_session_count: 2,
            desktop_hours: 6.5,
            desktop_cost: 0,
            desktop_unpriced_sessions: 2,
            job_count: 0,
            job_cost: 0,
            job_unpriced_jobs: 0,
            total_cost: 15.0
        }
    ]
};

const ALICE_SUMMARY = {
    username: 'alice',
    window: 'last_30_days',
    currency: 'USD',
    ai: {
        invocations: 2,
        total_tokens: 150,
        cost: 15.0,
        estimated: true,
        projects: [
            {
                project_id: 'project-1',
                project_name: 'project-a',
                project_title: 'Project A',
                invocations: 2,
                total_tokens: 150,
                cost: 15.0,
                estimated: true,
                by_model: []
            }
        ]
    },
    jobs: {job_count: 0, cost: 0, estimated: true, by_project: [], by_queue: [], recent_jobs: []},
    desktops: {
        session_count: 2,
        hours: 6.5,
        cost: 0,
        unpriced_sessions: 2,
        estimated: true,
        sessions: [
            {idea_session_id: 's1', name: 'alice-desktop', instance_type: 'm6g.xlarge', state: 'STOPPED', hours: 4.5, price_unavailable: true}
        ]
    }
};

const stub = (context: AppContext, listing: any, summary: any) => {
    vi.spyOn(context.client().myCosts(), 'listUserCosts').mockResolvedValue(listing);
    vi.spyOn(context.client().myCosts(), 'getUserSummary').mockResolvedValue(summary);
};

const renderPage = () => {
    render(
        <MemoryRouter>
            <UserCostsPage
                ideaPageId="user-costs"
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

const userRow = (username: string, total: number) => ({
    username: username,
    ai_requests: 0,
    ai_tokens: 0,
    ai_cost: 0,
    desktop_session_count: 0,
    desktop_hours: 0,
    desktop_cost: 0,
    desktop_unpriced_sessions: 0,
    job_count: 0,
    job_cost: 0,
    job_unpriced_jobs: 0,
    total_cost: total
});

const listingOf = (rows: any[]) => ({...LISTING, listing: rows});

/** Usernames in render order. Row 0 is the header, cell 0 is the selection radio. */
const renderedUsernames = () =>
    screen.getAllByRole('row')
        .slice(1)
        .map((row) => (row as HTMLTableRowElement).cells[1]?.textContent ?? '');

describe('admin user costs page', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('lists every user with a measured cost, biggest spender first', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        renderPage();

        expect(await screen.findByText('bob', {}, {timeout: 10000})).toBeInTheDocument();
        expect(await screen.findByText('alice')).toBeInTheDocument();
        const rows = screen.getAllByRole('row').map((node) => node.textContent);
        expect(rows.findIndex((row) => row?.includes('bob'))).toBeLessThan(
            rows.findIndex((row) => row?.includes('alice'))
        );
    }, 20000);

    it('reports a users desktop cost as not available when nothing could be priced', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        renderPage();

        await screen.findByText('alice', {}, {timeout: 10000});
        // Two sessions with no price for either must not read as a free desktop.
        expect(await screen.findByText('Not available')).toBeInTheDocument();
    });

    it('drills into the selected user and shows their three sections', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        renderPage();

        await screen.findByText('alice', {}, {timeout: 10000});
        await userEvent.click(screen.getByRole('radio', {name: 'Show costs for alice'}));

        expect(await screen.findByText('Costs for alice')).toBeInTheDocument();
        expect(await screen.findByRole('heading', {name: 'AI usage'})).toBeInTheDocument();
        expect(await screen.findByText('Project A')).toBeInTheDocument();
        expect(await screen.findByText('alice-desktop')).toBeInTheDocument();
    });

    it('asks the server for the selected user by name', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        const getUserSummary = vi.spyOn(context.client().myCosts(), 'getUserSummary');
        renderPage();

        await screen.findByText('alice', {}, {timeout: 10000});
        await userEvent.click(screen.getByRole('radio', {name: 'Show costs for alice'}));

        expect(getUserSummary).toHaveBeenCalledWith({username: 'alice'});
    });

    it('warns when a source could not be read', async () => {
        const context = initTestAppContext();
        stub(context, {...LISTING, jobs_unavailable: true}, ALICE_SUMMARY);
        renderPage();

        expect(await screen.findByText(/Could not read jobs/, {}, {timeout: 10000})).toBeInTheDocument();
    });

    it('shows an empty state when nobody has a measured cost', async () => {
        const context = initTestAppContext();
        stub(context, {...LISTING, listing: []}, ALICE_SUMMARY);
        renderPage();

        expect(await screen.findByText('No measured costs', {}, {timeout: 10000})).toBeInTheDocument();
    });

    it('narrows the table to users matching the typed name', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        renderPage();

        await screen.findByText('bob', {}, {timeout: 10000});
        await userEvent.type(screen.getByPlaceholderText('Find a user'), 'ali');

        expect(await screen.findByText('alice')).toBeInTheDocument();
        expect(screen.queryByText('bob')).toBeNull();
    }, 20000);

    it('says so when the filter matches nobody', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        renderPage();

        await screen.findByText('bob', {}, {timeout: 10000});
        await userEvent.type(screen.getByPlaceholderText('Find a user'), 'nobody');

        expect(await screen.findByText('No matching users')).toBeInTheDocument();
    });

    it('sorts by total descending whatever order the server sent', async () => {
        const context = initTestAppContext();
        // The server order is reversed, so the table must not simply echo it.
        stub(context, listingOf([userRow('cheap', 1), userRow('dear', 900), userRow('middling', 50)]), ALICE_SUMMARY);
        renderPage();

        await screen.findByText('dear', {}, {timeout: 10000});
        expect(renderedUsernames()).toEqual(['dear', 'middling', 'cheap']);
    });

    it('pages a listing longer than one page', async () => {
        const context = initTestAppContext();
        const many = Array.from({length: 35}, (_, index) => userRow(`user${String(index).padStart(2, '0')}`, index));
        stub(context, listingOf(many), ALICE_SUMMARY);
        renderPage();

        await screen.findByText('user34', {}, {timeout: 10000});
        // One page of 30, highest total first, so user34 down to user05.
        expect(renderedUsernames()).toHaveLength(30);
        expect(screen.queryByText('user00')).toBeNull();
        expect(screen.getByRole('button', {name: 'Next page'})).toBeInTheDocument();
    });

    it('counts the users shown against the total', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        renderPage();

        expect(await screen.findByText('(2 of 2 users)', {}, {timeout: 10000})).toBeInTheDocument();

        await userEvent.type(screen.getByPlaceholderText('Find a user'), 'ali');
        expect(await screen.findByText('(1 of 2 users)')).toBeInTheDocument();
    });

    it('keeps the selected user detail while the filter changes', async () => {
        const context = initTestAppContext();
        stub(context, LISTING, ALICE_SUMMARY);
        renderPage();

        await screen.findByText('alice', {}, {timeout: 10000});
        await userEvent.click(screen.getByRole('radio', {name: 'Show costs for alice'}));
        await screen.findByText('Costs for alice');

        // Filtering the table must not tear down the breakdown underneath it.
        await userEvent.type(screen.getByPlaceholderText('Find a user'), 'bo');
        expect(await screen.findByText('Costs for alice')).toBeInTheDocument();
    });
});
