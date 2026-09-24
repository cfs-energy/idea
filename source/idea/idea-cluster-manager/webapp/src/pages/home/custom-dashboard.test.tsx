import {render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import CustomDashboard from './custom-dashboard';
import {initTestAppContext} from '../../test-support';

vi.mock('../../components/navbar', () => ({default: () => null}));

it('uses the configured dashboard title as the page heading while navigation stays Reports', async () => {
    const context = initTestAppContext();
    const service = context.getClusterSettingsService();
    service.customDashboard = {enabled: true, title: 'Engineering overview', url: 'https://reports.example.org/view'};
    vi.spyOn(service, 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    render(<MemoryRouter initialEntries={['/home/custom-dashboard']}><CustomDashboard
        ideaPageId="reports" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
        sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}}
        onFlashbarChange={() => {}} flashbarItems={[]}/></MemoryRouter>);
    expect(await screen.findByRole('heading', {name: 'Engineering overview', level: 1})).toBeInTheDocument();
    expect(screen.queryByRole('heading', {name: 'Reports', level: 1})).not.toBeInTheDocument();
    expect(screen.getByLabelText('Breadcrumbs')).toHaveTextContent('Reports');
});
