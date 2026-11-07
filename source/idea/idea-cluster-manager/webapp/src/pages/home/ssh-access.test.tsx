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

const renderPage = (settingsResult: () => Promise<any>) => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'getUsername').mockReturnValue('rivera');
    vi.spyOn(context.auth(), 'getClusterName').mockReturnValue('idea-test');
    vi.spyOn(context.auth(), 'getAwsRegion').mockReturnValue('us-east-2');
    const settings = context.getClusterSettingsService();
    const getModuleSettings = vi.spyOn(settings, 'getModuleSettings').mockImplementation(settingsResult as any);
    vi.spyOn(settings, 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    render(<MemoryRouter initialEntries={['/home/ssh-access']}><SSHAccess
        ideaPageId="ssh-access" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
        sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}}
        onFlashbarChange={() => {}} flashbarItems={[]}/></MemoryRouter>);
    return {context, getModuleSettings};
};

it('offers no half-built commands when the connection details did not load, and can retry', async () => {
    const {getModuleSettings} = renderPage(() => Promise.reject({errorCode: 'GENERAL_ERROR', message: 'nope'}));

    expect(await screen.findByText('Could not load the connection details.')).toBeInTheDocument();
    expect(screen.queryByText(/rivera@$/)).toBeNull();
    expect(screen.queryByText(/^ssh -i/)).toBeNull();

    getModuleSettings.mockResolvedValue({public: true, public_ip: '203.0.113.10'} as any);
    fireEvent.click(screen.getByRole('button', {name: 'Retry'}));
    expect(await screen.findByText(/^ssh -i .* rivera@203\.0\.113\.10$/)).toBeInTheDocument();
});

it('says when the key could not be downloaded', async () => {
    const {context} = renderPage(() => Promise.resolve({public: true, public_ip: '203.0.113.10'}));
    vi.spyOn(context.auth(), 'downloadPrivateKey').mockRejectedValue({message: 'key service is down'});

    await screen.findByText(/^ssh -i/);
    fireEvent.click(screen.getAllByRole('button', {name: 'Download private key'})[0]);

    expect(await screen.findByText('Could not download your key: key service is down. Try again, or contact your administrator.')).toBeInTheDocument();
});

it('tells PuTTY users which name to log in with', async () => {
    renderPage(() => Promise.resolve({public: true, public_ip: '203.0.113.10'}));

    await screen.findByText(/^ssh -i/);
    fireEvent.click(screen.getByRole('button', {name: 'Using PuTTY'}));

    expect(screen.getByText(/When asked "login as", enter/).textContent).toContain('rivera');
});

it('says the details could not load when the bastion has no address, instead of loading forever', async () => {
    const context = initTestAppContext();
    const settings = context.getClusterSettingsService();
    vi.spyOn(settings, 'getModuleSettings').mockResolvedValue({public: true});
    vi.spyOn(settings, 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
    render(<MemoryRouter initialEntries={['/home/ssh-access']}><SSHAccess
        ideaPageId="ssh-access" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
        sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}}
        onFlashbarChange={() => {}} flashbarItems={[]}/></MemoryRouter>);
    expect(await screen.findByText('Could not load the connection details.')).toBeInTheDocument();
});
