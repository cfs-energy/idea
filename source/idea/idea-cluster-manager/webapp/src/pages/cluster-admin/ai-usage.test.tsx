import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import AiUsage from './ai-usage';
import {initTestAppContext} from '../../test-support';
import {AppContext} from '../../common';

const USAGE_LISTING = [
    {
        project_id: 'p-1',
        name: 'research',
        title: 'Research',
        enabled: true,
        bedrock: {enabled: true, model_ids: ['vendor.model-a']},
        bedrock_usage: {
            window: 'last_30_days',
            invocations: 5,
            total_tokens: 1500,
            spend: {amount: 3, unit: 'USD'},
            by_model: [
                {
                    model_id: 'us.amazon.nova-pro-v1:0',
                    input_tokens: 900,
                    output_tokens: 100,
                    total_tokens: 1000,
                    invocations: 4,
                    spend: {amount: 2, unit: 'USD'},
                    spend_is_estimated: true
                },
                {
                    model_id: 'vendor.model-b',
                    input_tokens: 500,
                    output_tokens: 0,
                    total_tokens: 500,
                    invocations: 1,
                    spend: {amount: 1, unit: 'USD'},
                    spend_is_estimated: true
                }
            ],
            by_user: [
                {
                    username: 'alice',
                    total_tokens: 1000,
                    invocations: 4,
                    spend: {amount: 2, unit: 'USD'},
                    spend_is_estimated: true,
                    top_model_id: 'us.amazon.nova-pro-v1:0'
                },
                {
                    username: 'bob',
                    total_tokens: 500,
                    invocations: 1,
                    spend: {amount: 1, unit: 'USD'},
                    spend_is_estimated: true,
                    top_model_id: 'vendor.model-b'
                }
            ]
        }
    },
    {
        project_id: 'p-2',
        name: 'physics',
        title: 'Physics',
        enabled: true,
        bedrock: {enabled: true, model_ids: ['vendor.model-a']}
    }
];

const renderAiUsagePage = () => {
    render(
        <MemoryRouter>
            <AiUsage
                ideaPageId="ai-usage"
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

const withUsage = (context: AppContext, listing: any[]) => {
    vi.spyOn(context.client().projects(), 'listBedrockUsage').mockResolvedValue({listing: listing});
};

describe('ai usage page', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('lists every bedrock project with its tokens, requests, cost and top model', async () => {
        const context = initTestAppContext();
        withUsage(context, USAGE_LISTING);
        renderAiUsagePage();

        expect(await screen.findByText('Research')).toBeInTheDocument();
        expect(await screen.findByText('1,500')).toBeInTheDocument();
        expect(await screen.findByText('$3.00')).toBeInTheDocument();
        expect(await screen.findByText('nova-pro')).toBeInTheDocument();
        // A Bedrock project nobody used is still listed, so it is visibly at zero.
        expect(await screen.findByText('Physics')).toBeInTheDocument();
        expect(await screen.findByText('No usage recorded')).toBeInTheDocument();
    });

    it('breaks the selected project down per model and per user, cost labeled estimated', async () => {
        const context = initTestAppContext();
        withUsage(context, USAGE_LISTING);
        renderAiUsagePage();
        await screen.findByText('Research');

        await userEvent.click(screen.getAllByRole('radio')[0]);

        expect(await screen.findByText('Per model')).toBeInTheDocument();
        expect(await screen.findByText('Per user')).toBeInTheDocument();
        // The input and output split comes from the day rows, so both are counted numbers.
        expect(await screen.findByText('900')).toBeInTheDocument();
        expect(await screen.findByText('alice')).toBeInTheDocument();
        expect(await screen.findByText('bob')).toBeInTheDocument();
        // A share of the project spend is never presented as a priced total.
        expect((await screen.findAllByText('$2.00 (estimated)')).length).toBeGreaterThan(0);
        expect(await screen.findByText(/Cost per model and per user is estimated/)).toBeInTheDocument();
    });

    it('reports a project whose usage read failed instead of claiming no usage', async () => {
        const context = initTestAppContext();
        withUsage(context, [{
            project_id: 'p-3',
            name: 'unreadable',
            title: 'Unreadable',
            enabled: true,
            bedrock: {enabled: true, model_ids: ['vendor.model-a']},
            bedrock_usage: {is_unavailable: true}
        }]);
        renderAiUsagePage();

        expect(await screen.findByText('Usage unavailable')).toBeInTheDocument();
        expect(screen.queryByText('No usage recorded')).toBeNull();
    });
});
