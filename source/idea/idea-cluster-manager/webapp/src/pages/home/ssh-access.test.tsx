import {render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import SSHAccess from './ssh-access';
import {initTestAppContext} from '../../test-support';

vi.mock('../../components/navbar', () => ({default: () => null}));

it('renders the connection steps from the bastion address and the signed-in identity', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'getUsername').mockReturnValue('rivera');
    vi.spyOn(context.auth(), 'getClusterName').mockReturnValue('idea-test');
    vi.spyOn(context.auth(), 'getAwsRegion').mockReturnValue('us-east-2');
    const settings = context.getClusterSettingsService();
    vi.spyOn(settings, 'getModuleSettings').mockResolvedValue({public: true, public_ip: '203.0.113.10'});
    vi.spyOn(settings, 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    render(<MemoryRouter initialEntries={['/home/ssh-access']}><SSHAccess
        ideaPageId="ssh-access" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
        sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}}
        onFlashbarChange={() => {}} flashbarItems={[]}/></MemoryRouter>);
    expect(await screen.findByText('ssh -i ~/.ssh/rivera_idea-test_privatekey.pem rivera@203.0.113.10')).toBeInTheDocument();
    expect(screen.getByText(/Host idea-test-us-east-2/)).toHaveTextContent('IdentityFile ~/.ssh/rivera_idea-test_privatekey.pem');
    expect(screen.getAllByRole('button', {name: 'Download private key'})).toHaveLength(2);
    expect(screen.getAllByRole('heading', {level: 1})).toHaveLength(1);
});
