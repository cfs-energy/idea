import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';
import VirtualDesktopSoftwareStackDetail from './virtual-desktop-software-stack-detail';
import { initTestAppContext } from '../../test-support';

vi.mock('./forms/virtual-desktop-software-stack-edit-form', () => {
    const React = require('react');
    class Form extends React.Component<any> {
        showModal() {}
        hideForm() {}
        render() {
            return React.createElement('button', {
                onClick: () => this.props.onSubmit('stack-1', 'rocky9', 'n', 'd', [], false, '', 'default', [], 'ami-1', undefined, undefined, 'new-owner', {clear_expires_on: true})
            }, 'Save the form');
        }
    }
    return { default: Form, expiryChange: () => ({}) };
});

const PAGE_PROPS = {
    toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {},
    sideNavHeader: { text: 'IDEA', href: '#/' }, sideNavItems: [], onSideNavChange: () => {},
    onFlashbarChange: () => {}, flashbarItems: []
} as any;

const STACK = {
    stack_id: 'stack-1', base_os: 'rocky9', name: 'n', projects: [], owner: 'a-person',
    effective_expires_on: '2027-01-15T00:00:00Z'
};

async function renderStack() {
    const context = initTestAppContext();
    const admin = context.client().virtualDesktopAdmin();
    vi.spyOn(admin, 'getSoftwareStackInfo').mockResolvedValue({ software_stack: STACK } as any);
    const update = vi.spyOn(admin, 'updateSoftwareStack').mockResolvedValue({ software_stack: STACK } as any);
    render(
        <MemoryRouter initialEntries={['/stacks/stack-1?base_os=rocky9']}>
            <Routes>
                <Route path="/stacks/:software_stack_id" element={<VirtualDesktopSoftwareStackDetail ideaPageId="virtual-desktop-software-stacks" {...PAGE_PROPS}/>}/>
            </Routes>
        </MemoryRouter>
    );
    await screen.findByText('Stack: n', {}, { timeout: 10000 });
    return update;
}

describe('desktop image detail page', () => {
    afterEach(() => vi.restoreAllMocks());

    it('saves the owner and the expiry change the edit form sends', async () => {
        const update = await renderStack();
        const user = userEvent.setup();
        await user.click(screen.getByRole('button', { name: 'Edit' }));
        await user.click(await screen.findByRole('button', { name: 'Save the form' }));
        const sent = update.mock.calls[0][0].software_stack!;
        expect(sent.owner).toBe('new-owner');
        expect(sent.clear_expires_on).toBe(true);
    });

    it('shows who owns the image and when it expires', async () => {
        await renderStack();
        expect(screen.getByText('a-person')).toBeInTheDocument();
        expect(screen.getByText('Expires')).toBeInTheDocument();
        expect(screen.getByText(/2027/)).toBeInTheDocument();
    });
});
