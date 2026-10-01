import {fireEvent, render, screen} from '@testing-library/react';
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
    const key = '~/.ssh/rivera_idea-test_privatekey.pem';
    const target = 'rivera@203.0.113.10';
    expect(await screen.findByText(`ssh -i ${key} ${target}`)).toBeInTheDocument();
    expect(screen.getByText(/Host idea-test-us-east-2/)).toHaveTextContent(`IdentityFile ${key}`);
    // one set of terminal steps; Windows restricts the key with icacls instead of chmod
    expect(screen.getByText(`chmod 600 ${key}`)).toBeInTheDocument();
    expect(screen.getByText('icacls "$env:USERPROFILE\\.ssh\\rivera_idea-test_privatekey.pem" /inheritance:r /grant:r "$($env:USERNAME):R"')).toBeInTheDocument();
    // file transfer one-liners reuse the same key and host
    expect(screen.getByText(`scp -i ${key} my-file.txt ${target}:~/`)).toBeInTheDocument();
    expect(screen.getByText(`scp -i ${key} ${target}:~/my-file.txt .`)).toBeInTheDocument();
    expect(screen.getByText(`rsync -avz -e "ssh -i ${key}" my-folder/ ${target}:~/my-folder/`)).toBeInTheDocument();
    expect(screen.getByText(`sftp -i ${key} ${target}`)).toBeInTheDocument();
    // compute node access from the bastion host
    expect(screen.getByText('qstat -f <job id> | grep exec_host')).toBeInTheDocument();
    expect(screen.getByText('ssh <node host name>')).toBeInTheDocument();
    // PuTTY is secondary, behind an expandable section with the .ppk download
    fireEvent.click(screen.getByRole('button', {name: 'Using PuTTY'}));
    expect(screen.getAllByRole('button', {name: 'Download private key'})).toHaveLength(2);
    expect(screen.getAllByRole('heading', {level: 1})).toHaveLength(1);
});

it('connects to the bastion host name when the administrator has set one', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'getUsername').mockReturnValue('rivera');
    vi.spyOn(context.auth(), 'getClusterName').mockReturnValue('idea-test');
    vi.spyOn(context.auth(), 'getAwsRegion').mockReturnValue('us-east-2');
    const settings = context.getClusterSettingsService();
    vi.spyOn(settings, 'getModuleSettings').mockResolvedValue({public: true, public_ip: '203.0.113.10', ssh_hostname: 'ssh.example.org'});
    vi.spyOn(settings, 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    render(<MemoryRouter initialEntries={['/home/ssh-access']}><SSHAccess
        ideaPageId="ssh-access" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
        sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}}
        onFlashbarChange={() => {}} flashbarItems={[]}/></MemoryRouter>);
    expect(await screen.findByText('ssh -i ~/.ssh/rivera_idea-test_privatekey.pem rivera@ssh.example.org')).toBeInTheDocument();
});
