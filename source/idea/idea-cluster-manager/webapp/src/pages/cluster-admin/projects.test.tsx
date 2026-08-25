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

import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import Projects from './projects';
import {initTestAppContext} from '../../test-support';
import {AppContext} from '../../common';

const BEDROCK_ENABLE_LABEL = 'Do you want to enable Amazon Bedrock for this project?';
const TITLE_FIELD_DESCRIPTION = 'Enter a user friendly project title';

const stubClusterManagerSettings = (context: AppContext, settings: any) => {
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue(settings);
    vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({listing: []});
};

const renderProjectsPage = () => {
    render(
        <MemoryRouter>
            <Projects
                ideaPageId="projects"
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

describe('projects page bedrock controls', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('offers the bedrock controls when the cluster feature flag is on', async () => {
        const context = initTestAppContext();
        stubClusterManagerSettings(context, {
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a', 'vendor.model-b']
            }
        });
        renderProjectsPage();
        await userEvent.click(await screen.findByRole('button', {name: 'Create Project'}));
        expect(await screen.findByText(BEDROCK_ENABLE_LABEL)).toBeInTheDocument();
    });

    it('still renders the page when the settings lookup throws synchronously', async () => {
        // getModuleSettings reads globalSettings.module_sets before returning a promise, so on a
        // cluster whose config has not loaded it throws synchronously. A .catch() on the returned
        // promise never runs for that path, which took the whole page down rather than logging.
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockImplementation(() => {
            throw new TypeError("Cannot read properties of undefined (reading 'module_sets')");
        });
        vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({listing: []});
        renderProjectsPage();
        await userEvent.click(await screen.findByRole('button', {name: 'Create Project'}));
        expect(await screen.findByText(TITLE_FIELD_DESCRIPTION)).toBeInTheDocument();
        expect(screen.queryByText(BEDROCK_ENABLE_LABEL)).toBeNull();
    });

    it('renders no bedrock controls when the cluster feature flag is off', async () => {
        const context = initTestAppContext();
        stubClusterManagerSettings(context, {
            bedrock: {
                enabled: false,
                model_ids: ['vendor.model-a', 'vendor.model-b']
            }
        });
        renderProjectsPage();
        await userEvent.click(await screen.findByRole('button', {name: 'Create Project'}));
        expect(await screen.findByText(TITLE_FIELD_DESCRIPTION)).toBeInTheDocument();
        expect(screen.queryByText(BEDROCK_ENABLE_LABEL)).toBeNull();
    });
});

describe('projects page bedrock usage column', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    const withProjects = (context: AppContext, listing: any[]) => {
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {enabled: true, model_ids: ['vendor.model-a']}
        });
        vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({listing: listing});
    };

    it('shows month to date tokens, cost, requests and the tokens per model', async () => {
        const context = initTestAppContext();
        withProjects(context, [{
            project_id: 'p-1',
            name: 'research',
            title: 'Research',
            enabled: true,
            bedrock: {enabled: true, model_ids: ['vendor.model-a']},
            bedrock_usage: {
                period: '2026-08',
                invocations: 12,
                total_tokens: 34567,
                spend: {amount: 1.32, unit: 'USD'},
                by_user: [
                    {username: 'bob', total_tokens: 20000},
                    {username: 'alice', total_tokens: 14567}
                ],
                by_model: [
                    {model_id: 'us.anthropic.claude-opus-5', total_tokens: 20000},
                    {model_id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', total_tokens: 14567}
                ]
            }
        }]);
        renderProjectsPage();
        expect(await screen.findByText('34,567 tokens, $1.32 MTD')).toBeInTheDocument();
        expect(await screen.findByText('12 requests in 2026-08')).toBeInTheDocument();
        expect(await screen.findByText('claude-opus-5: 20,000')).toBeInTheDocument();
        expect(await screen.findByText('claude-haiku-4-5-20251001: 14,567')).toBeInTheDocument();
        // per user attribution belongs to the usage api, not this table
        expect(screen.queryByText('bob: 20,000')).toBeNull();
    });

    it('reports an unpriced project as zero and an unreadable one as unavailable', async () => {
        const context = initTestAppContext();
        withProjects(context, [{
            project_id: 'p-8',
            name: 'priced',
            title: 'Priced',
            enabled: true,
            bedrock: {enabled: true, model_ids: ['vendor.model-a']},
            bedrock_usage: {period: '2026-08', invocations: 1, total_tokens: 10, spend: {amount: 0, unit: 'USD'}}
        }, {
            project_id: 'p-9',
            name: 'unpriced',
            title: 'Unpriced',
            enabled: true,
            bedrock: {enabled: true, model_ids: ['vendor.model-a']},
            bedrock_usage: {period: '2026-08', invocations: 1, total_tokens: 20, spend_is_unavailable: true}
        }]);
        renderProjectsPage();
        expect(await screen.findByText('10 tokens, $0.00 MTD')).toBeInTheDocument();
        expect(await screen.findByText('20 tokens, cost unavailable')).toBeInTheDocument();
    });

    it('reports no usage for a bedrock project that has not been used', async () => {
        const context = initTestAppContext();
        withProjects(context, [{
            project_id: 'p-2',
            name: 'quiet',
            title: 'Quiet',
            enabled: true,
            bedrock: {enabled: true, model_ids: ['vendor.model-a']}
        }]);
        renderProjectsPage();
        expect(await screen.findByText('No usage recorded')).toBeInTheDocument();
    });

    it('reports a failed usage read instead of claiming no usage', async () => {
        const context = initTestAppContext();
        withProjects(context, [{
            project_id: 'p-6',
            name: 'unreadable',
            title: 'Unreadable',
            enabled: true,
            bedrock: {enabled: true, model_ids: ['vendor.model-a']},
            bedrock_usage: {is_unavailable: true}
        }]);
        renderProjectsPage();
        expect(await screen.findByText('Usage unavailable')).toBeInTheDocument();
        expect(screen.queryByText('No usage recorded')).toBeNull();
    });

    it('does not claim no usage when idea is not managing invocation logging', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {
                enabled: true,
                model_ids: ['vendor.model-a'],
                invocation_logging: {manage_configuration: false}
            }
        });
        vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({
            listing: [{
                project_id: 'p-7',
                name: 'quiet',
                title: 'Quiet',
                enabled: true,
                bedrock: {enabled: true, model_ids: ['vendor.model-a']}
            }]
        });
        renderProjectsPage();
        expect(await screen.findByText('Not collected')).toBeInTheDocument();
        expect(screen.queryByText('No usage recorded')).toBeNull();
    });

    it('renders nothing for a project without bedrock', async () => {
        const context = initTestAppContext();
        withProjects(context, [{
            project_id: 'p-3',
            name: 'plain',
            title: 'Plain',
            enabled: true
        }]);
        renderProjectsPage();
        expect(await screen.findByText('Plain')).toBeInTheDocument();
        expect(screen.queryByText('No usage recorded')).toBeNull();
    });

    it('pluralizes a single request', async () => {
        const context = initTestAppContext();
        withProjects(context, [{
            project_id: 'p-4',
            name: 'once',
            title: 'Once',
            enabled: true,
            bedrock: {enabled: true, model_ids: ['vendor.model-a']},
            bedrock_usage: {period: '2026-08', invocations: 1, total_tokens: 42}
        }]);
        renderProjectsPage();
        expect(await screen.findByText('1 request in 2026-08')).toBeInTheDocument();
    });

    it('does not render the column at all when the cluster feature flag is off', async () => {
        // the column definitions are built per render, so a default-off cluster
        // sees exactly the pre-feature table.
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {enabled: false}
        });
        vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({
            listing: [{
                project_id: 'p-5',
                name: 'plain',
                title: 'Plain',
                enabled: true,
                bedrock: {enabled: true, model_ids: ['vendor.model-a']},
                bedrock_usage: {period: '2026-08', invocations: 3, total_tokens: 99}
            }]
        });
        renderProjectsPage();
        expect(await screen.findByText('Plain')).toBeInTheDocument();
        expect(screen.queryByText('AI Usage')).toBeNull();
        expect(screen.queryByText('99 tokens, cost unavailable')).toBeNull();
    });
});


describe('update project form prefill', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    const BEDROCK_PROJECT = {
        project_id: 'p-1',
        name: 'research',
        title: 'Research',
        enabled: true,
        bedrock: {
            enabled: true,
            model_ids: ['vendor.model-a']
        }
    };

    const openUpdateFormFor = async (project: any) => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({
            bedrock: {enabled: true, model_ids: ['vendor.model-a', 'vendor.model-b']}
        });
        vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({listing: [project]});
        renderProjectsPage();
        await screen.findByText(project.title);
        // select the row, then open the update form from the actions menu
        await userEvent.click(document.querySelector('input[type="radio"]') as HTMLElement);
        await userEvent.click(await screen.findByRole('button', {name: /Actions/i}));
        await userEvent.click(await screen.findByText('Edit Project'));
        return await screen.findByText(BEDROCK_ENABLE_LABEL);
    };

    it('carries bedrock enabled through from the project being edited', async () => {
        // the form addresses nested params by their dotted name. without an explicit
        // entry the toggle reads as its default, false, and submitting the form to
        // change anything else switches bedrock off and clears the model list.
        await openUpdateFormFor(BEDROCK_PROJECT);

        const toggle = document.querySelector(
            'input[type="checkbox"][name="bedrock.enabled"], [data-testid="bedrock.enabled"] input'
        ) as HTMLInputElement | null;
        const toggles = Array.from(
            document.querySelectorAll('input[type="checkbox"]')
        ) as HTMLInputElement[];
        const anyChecked = (toggle?.checked ?? false) || toggles.some((t) => t.checked);
        expect(anyChecked).toBe(true);
    });

    it('leaves bedrock off for a project that does not have it', async () => {
        await openUpdateFormFor({
            project_id: 'p-2',
            name: 'plain',
            title: 'Plain',
            enabled: true
        });

        const toggles = Array.from(
            document.querySelectorAll('input[type="checkbox"]')
        ) as HTMLInputElement[];
        expect(toggles.some((t) => t.checked)).toBe(false);
    });
});
