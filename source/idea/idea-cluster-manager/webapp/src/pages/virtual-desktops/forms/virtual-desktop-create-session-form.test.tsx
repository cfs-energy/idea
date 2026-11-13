import React from 'react';
import {act, render, screen, waitFor} from '@testing-library/react';
import {vi} from 'vitest';
import userEvent from '@testing-library/user-event';
import createWrapper, {FormFieldWrapper} from '@cloudscape-design/components/test-utils/dom';
import VirtualDesktopCreateSessionForm from './virtual-desktop-create-session-form';
import {initTestAppContext} from '../../../test-support';

function renderForm(projects: any[], props: any = {}) {
    const context = initTestAppContext();
    const clients = context.client();
    vi.spyOn(clients.auth(), 'listUsersInGroup').mockResolvedValue({listing: []} as any);
    vi.spyOn(clients.virtualDesktopUtils(), 'listSupportedOS').mockResolvedValue({listing: ['amazonlinux2023']} as any);
    const listSoftwareStacks = vi.spyOn(clients.virtualDesktop(), 'listSoftwareStacks').mockResolvedValue({listing: []} as any);
    const getProject = vi.spyOn(clients.projects(), 'getProject').mockRejectedValue({errorCode: 'UNAUTHORIZED_ACCESS'});
    const form = React.createRef<VirtualDesktopCreateSessionForm>();
    render(<VirtualDesktopCreateSessionForm ref={form} projects={projects} maxRootVolumeMemory={500}
                                            onSubmit={() => Promise.resolve(true)} onDismiss={() => {}} {...props}/>);
    return {form, listSoftwareStacks, getProject, clients};
}

describe('create desktop form', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('does not ask for a project the person may not belong to', async () => {
        const {getProject} = renderForm([{project_id: 'p1', title: 'One'}, {project_id: 'p2', title: 'Two'}]);
        await act(async () => {});
        expect(getProject).not.toHaveBeenCalled();
    });

    it('picks the only project and loads what it offers', async () => {
        const {form, listSoftwareStacks} = renderForm([{project_id: 'p1', title: 'One'}]);
        await act(async () => form.current!.showModal());
        await waitFor(() => expect(listSoftwareStacks).toHaveBeenCalledWith(expect.objectContaining({project_id: 'p1'})));
        expect(screen.getByText('One')).toBeInTheDocument();
    });

    it('leaves the choice open with several projects', async () => {
        const {form, listSoftwareStacks} = renderForm([{project_id: 'p1', title: 'One'}, {project_id: 'p2', title: 'Two'}]);
        await act(async () => form.current!.showModal());
        await act(async () => {});
        expect(listSoftwareStacks).not.toHaveBeenCalled();
    });

    /** Cloudscape shows the empty label inside the open dropdown. */
    async function emptyLabelOf(label: string): Promise<string> {
        const field = createWrapper().findAll(`.${FormFieldWrapper.rootSelector}`)
            .map(found => new FormFieldWrapper(found.getElement()))
            .find(found => found.findLabel()?.getElement().textContent === label)!;
        const select = field.findControl()!.findSelect()!;
        select.openDropdown();
        return select.findDropdown()!.getElement().textContent ?? '';
    }

    const open = async (projects: any[], props: any = {}) => {
        const rendered = renderForm(projects, props);
        await act(async () => rendered.form.current!.showModal());
        await act(async () => {await new Promise(resolve => setTimeout(resolve, 150));});
        return rendered;
    };

    it('uses plain names and offers no subnet to a non-admin', async () => {
        const {form} = await open([{project_id: 'p1', title: 'One'}, {project_id: 'p2', title: 'Two'}]);
        for (const title of ['Desktop name', 'Software', 'Desktop size', 'Hibernate']) {
            expect(screen.getByText(title)).toBeInTheDocument();
        }
        expect(screen.getByText("Save your open work when the desktop stops. The size can't be changed later.")).toBeInTheDocument();
        for (const jargon of ['Session Name', 'Software Stack', 'Virtual Desktop Size', 'Enable Instance Hibernation']) {
            expect(screen.queryByText(jargon)).not.toBeInTheDocument();
        }
        await act(async () => form.current!.getForm().setParamValue('advanced_options', true));
        expect(await screen.findByText('Session type')).toBeInTheDocument();
        expect(screen.queryByText('DCV Session Type')).not.toBeInTheDocument();
        expect(screen.queryByText('VPC Subnet Id')).not.toBeInTheDocument();
        expect(screen.getByText('Network subnet')).toBeInTheDocument();
    });

    it('does not name the image in a software choice', () => {
        const form = new VirtualDesktopCreateSessionForm({projects: [], maxRootVolumeMemory: 500, onSubmit: () => Promise.resolve(true), onDismiss: () => {}});
        expect(form.generateSoftwareStackListing([{enabled: true, description: 'Rocky 9 with GPU', name: 'rocky9-gpu', ami_id: 'ami-0123', base_os: 'rocky9', stack_id: 's1'}]))
            .toEqual([{title: 'Rocky 9 with GPU', description: 'rocky9-gpu', value: 's1'}]);
    });

    it('tells someone with no project what to do', async () => {
        await open([]);
        expect(await emptyLabelOf('Project')).toContain("You aren't in a project yet. Ask your administrator to add you.");
    });

    it('asks for the project before the operating system', async () => {
        await open([{project_id: 'p1', title: 'One'}, {project_id: 'p2', title: 'Two'}]);
        expect(await emptyLabelOf('Operating system')).toContain('Choose a project first.');
    });

    it('says a project with nothing to offer has no software', async () => {
        await open([{project_id: 'p1', title: 'One'}]);
        await waitFor(async () => expect(await emptyLabelOf('Operating system')).toContain('No software is set up for this project. Ask your administrator.'));
    });

    it('does not call a failed request an empty list', async () => {
        const {clients, form} = renderForm([{project_id: 'p1', title: 'One'}]);
        vi.spyOn(clients.virtualDesktop(), 'listSoftwareStacks').mockRejectedValue({errorCode: 'GENERAL_ERROR', message: 'Service Unavailable'});
        await act(async () => form.current!.showModal());
        expect(await screen.findByText('The operating systems could not be loaded. Close this form and try again.', {}, {timeout: 2000})).toBeInTheDocument();
    });

    it('says when the projects themselves could not be loaded', async () => {
        await open([], {projectsLoadFailed: true});
        expect(screen.getByText('Your projects could not be loaded. Close this form and try again.')).toBeInTheDocument();
    });

    it('rejects a short name in plain words', async () => {
        await open([{project_id: 'p1', title: 'One'}, {project_id: 'p2', title: 'Two'}], {defaultName: 'ab'});
        await userEvent.setup().click(screen.getByRole('button', {name: 'Launch desktop'}));
        expect(screen.getAllByText('Use 3 to 24 characters.').length).toBeGreaterThan(1);
        expect(screen.queryByText(/regex/)).not.toBeInTheDocument();
    });

    it('says what is happening during the slow submit', async () => {
        let answer: (value: boolean) => void = () => {};
        const {form} = await open([{project_id: 'p1', title: 'One'}], {onSubmit: () => new Promise<boolean>(resolve => {answer = resolve;}), defaultName: 'good-name'});
        const message = 'Requesting your desktop. This can take up to a minute.';
        expect(screen.queryByText(message)).not.toBeInTheDocument();
        vi.spyOn(form.current!.getForm(), 'validate').mockReturnValue(true);
        await act(async () => form.current!.getForm().handleOnSubmit());
        expect(screen.getByText(message)).toBeInTheDocument();
        await act(async () => answer(true));
        expect(screen.queryByText(message)).not.toBeInTheDocument();
    });
});

describe('software that is past its date', () => {
    afterEach(() => vi.restoreAllMocks());

    it('warns in the choice and names what to use instead', () => {
        const {form} = renderForm([{project_id: 'p1', title: 'One'}]);
        const [choice] = form.current!.generateSoftwareStackListing([{
            enabled: true,
            description: 'Rocky 9 pinned',
            name: 'rocky9-pinned',
            stack_id: 's1',
            expiry_warning: 'Past its expiry date, 2026-01-01. Use Rocky 9 instead.'
        }]);
        expect(choice.description).toBe('rocky9-pinned – Past its expiry date, 2026-01-01. Use Rocky 9 instead.');
    });

    it('says nothing about a stack with no warning', () => {
        const {form} = renderForm([{project_id: 'p1', title: 'One'}]);
        const [choice] = form.current!.generateSoftwareStackListing([{enabled: true, description: 'Rocky 9', name: 'rocky9', stack_id: 's1'}]);
        expect(choice.description).toBe('rocky9');
    });
});
