import {render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter, useLocation, useNavigate} from 'react-router-dom';
import {beforeEach, vi} from 'vitest';
import App from '../App';
import IdeaAppLayout from '../components/app-layout';
import {initTestAppContext} from '../test-support';

vi.mock('../components/navbar', () => ({default: () => null}));
vi.mock('../pages/home', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>Home content</p>}/>}));
vi.mock('../pages/user-management/users', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>Users content</p>}/>}));
vi.mock('../pages/user-management/groups', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>Groups content</p>}/>}));
vi.mock('../pages/hpc/queues', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>Queues content</p>}/>}));
vi.mock('../pages/hpc/hpc-applications', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>HpcApplications content</p>}/>}));
vi.mock('../pages/hpc/update-queue-profile', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>HpcUpdateQueueProfile content</p>}/>}));
vi.mock('../pages/home/file-browser', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>SocaFileBrowser content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-dashboard', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopDashboard content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-sessions', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopSessions content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-software-stacks', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopSoftwareStacks content</p>}/>}));
vi.mock('../pages/virtual-desktops/my-virtual-desktop-sessions', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>MyVirtualDesktopSessions content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-session-detail', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopSessionDetail content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-debug', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopDebug content</p>}/>}));
vi.mock('../pages/hpc/update-hpc-application', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>UpdateHpcApplication content</p>}/>}));
vi.mock('../pages/hpc/submit-job', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>SubmitJob content</p>}/>}));
vi.mock('../pages/account/account-settings', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>AccountSettings content</p>}/>}));
vi.mock('../pages/home/ssh-access', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>SSHAccess content</p>}/>}));
vi.mock('../pages/home/my-costs', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>MyCosts content</p>}/>}));
vi.mock('../pages/home/custom-dashboard', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>CustomDashboard content</p>}/>}));
vi.mock('../pages/hpc/hpc-nodes', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>HpcNodes content</p>}/>}));
vi.mock('../pages/cluster-admin/reconciliation-runs', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>ReconciliationRuns content</p>}/>}));
vi.mock('../pages/cluster-admin/cluster-status', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>ClusterStatus content</p>}/>}));
vi.mock('../pages/cluster-admin/projects', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>Projects content</p>}/>}));
vi.mock('../pages/cluster-admin/ai-usage', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>AiUsage content</p>}/>}));
vi.mock('../pages/cluster-admin/user-costs', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>UserCostsPage content</p>}/>}));
vi.mock('../pages/hpc/hpc-licenses', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>HpcLicenses content</p>}/>}));
vi.mock('../pages/hpc/hpc-custom-amis', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>HpcCustomAmis content</p>}/>}));
vi.mock('../pages/hpc/update-hpc-license', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>UpdateHpcLicense content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-permission-profiles', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopPermissionProfiles content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-permission-profile-detail', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopPermissionProfileDetail content</p>}/>}));
vi.mock('../pages/virtual-desktops/my-shared-virtual-desktop-sessions', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>MySharedVirtualDesktopSessions content</p>}/>}));
vi.mock('../pages/virtual-desktops/virtual-desktop-software-stack-detail', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>VirtualDesktopSoftwareStackDetail content</p>}/>}));
vi.mock('../pages/home/log-tail', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>IdeaLogTail content</p>}/>}));
vi.mock('../pages/hpc/script-workbench', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>ScriptWorkbench content</p>}/>}));
vi.mock('../pages/hpc/jobs', () => ({ActiveJobs: (props: any) => <IdeaAppLayout {...props} content={<p>Own active jobs</p>}/>, CompletedJobs: (props: any) => <IdeaAppLayout {...props} content={<p>Own completed jobs</p>}/>, AdminActiveJobs: (props: any) => <IdeaAppLayout {...props} content={<p>All active jobs</p>}/>, AdminCompletedJobs: (props: any) => <IdeaAppLayout {...props} content={<p>All completed jobs</p>}/>}));
vi.mock('../pages/cluster-admin/portal-settings', () => ({default: (props: any) => <IdeaAppLayout {...props} content={<p>Settings content</p>}/>}));
vi.mock('../pages/cluster-admin/services-page', () => ({ServicesPage: (props: any) => <IdeaAppLayout {...props} content={<><p>Service content</p><h2>Desktop services</h2><h2>Job service</h2></>}/>}));

let context: ReturnType<typeof initTestAppContext>;
beforeEach(() => {
    context = initTestAppContext();
    vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(true);
    vi.spyOn(context.auth(), 'hasModuleAccess').mockReturnValue(true);
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
    const service = context.getClusterSettingsService();
    vi.spyOn(service, 'initialize').mockResolvedValue(true);
    vi.spyOn(service, 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(service, 'isSchedulerDeployed').mockReturnValue(true);
    vi.spyOn(service, 'fetchMaintenance').mockResolvedValue({enabled: false, message: '', ends_at: ''});
});
function History() {
    const location = useLocation();
    const navigate = useNavigate();
    return <><output data-testid="url">{location.pathname}{location.search}</output><button onClick={() => navigate(-1)}>Back</button><button onClick={() => navigate(1)}>Forward</button></>;
}
function open(path: string) {return render(<MemoryRouter initialEntries={[path]}><App/><History/></MemoryRouter>);}

it.each([
    ['/dashboard', 'My jobs'],
    ['/home/virtual-desktops', 'My desktops'],
    ['/home/shared-desktops', 'My desktops'],
    ['/home/active-jobs', 'My jobs'],
    ['/home/completed-jobs', 'My jobs'],
    ['/home/script-workbench', 'My jobs'],
    ['/soca/jobs/submit-job', 'My jobs'],
    ['/home/file-browser', 'Files'],
    ['/home/file-browser/tail', 'Files'],
    ['/home/ssh-access', 'SSH access'],
    ['/home/my-costs', 'My costs'],
    ['/home/custom-dashboard', 'Reports'],
    ['/virtual-desktop/dashboard', 'Manage desktops'],
    ['/virtual-desktop/sessions', 'Manage desktops'],
    ['/virtual-desktop/sessions/record%2Fid', 'Manage desktops'],
    ['/virtual-desktop/debug', 'Manage desktops'],
    ['/virtual-desktop/software-stacks', 'Images and applications'],
    ['/virtual-desktop/software-stacks/record%2Fid', 'Images and applications'],
    ['/virtual-desktop/permission-profiles', 'People and access'],
    ['/virtual-desktop/permission-profiles/record%2Fid', 'People and access'],
    ['/soca/active-jobs', 'Manage jobs'],
    ['/soca/completed-jobs', 'Manage jobs'],
    ['/soca/queues', 'Manage jobs'],
    ['/soca/queues/create', 'Manage jobs'],
    ['/soca/queues/update', 'Manage jobs'],
    ['/soca/licenses', 'Manage jobs'],
    ['/soca/licenses/create', 'Manage jobs'],
    ['/soca/licenses/update', 'Manage jobs'],
    ['/soca/applications', 'Images and applications'],
    ['/soca/applications/create', 'Images and applications'],
    ['/soca/applications/update', 'Images and applications'],
    ['/soca/custom-amis', 'Images and applications'],
    ['/cluster/projects', 'Projects'],
    ['/cluster/users', 'People and access'],
    ['/cluster/groups', 'People and access'],
    ['/cluster/user-costs', 'Costs and usage'],
    ['/cluster/status', 'Operations'],
    ['/cluster/settings', 'Settings'],
    ['/virtual-desktop/settings', 'Settings'],
    ['/soca/settings', 'Settings'],
    ['/cluster/settings/mail', 'Settings'],
    ['/virtual-desktop/settings/desktop-lifecycle', 'Settings'],
    ['/soca/settings/job-placement', 'Settings'],
    ['/cluster/email-templates', 'Settings'],
    ['/soca/active-jobs?view=nodes', 'Manage jobs'],
    ['/cluster/users?view=reconciliation', 'Operations'],
    ['/virtual-desktop/settings?view=services', 'Operations'],
    ['/soca/settings?view=service', 'Operations'],
    ['/soca/settings?tab=general&project=p%2Fa', 'Operations'],
    ['/virtual-desktop/settings?tab=controller', 'Operations'],
])('renders %s inside %s', async (path, title) => {
    open(path);
    expect(await screen.findByRole('heading', {name: title, level: 1})).toBeInTheDocument();
    if (path !== '/dashboard' && !/view=(services?|reconciliation)|tab=(controller|general)/.test(path)) expect(screen.getByTestId('url').textContent).toBe(path);
});

it('redirects the retired AI cost page to By user', async () => {
    open('/cluster/ai-usage');
    expect(await screen.findByRole('heading', {name: 'Costs and usage', level: 1})).toBeInTheDocument();
    expect(screen.getByTestId('url')).toHaveTextContent('/cluster/user-costs');
});

it('preserves query parameters and selection through task tabs, back and forward', async () => {
    open('/home/active-jobs?job_id=42&path=%2Fhome%2Fu%2Fa%20b');
    expect(await screen.findByText('Own active jobs')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', {name: 'Completed'}));
    expect(await screen.findByText('Own completed jobs')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', {name: 'Back'}));
    expect(await screen.findByText('Own active jobs')).toBeInTheDocument();
    expect(screen.getByTestId('url').textContent).toBe('/home/active-jobs?job_id=42&path=%2Fhome%2Fu%2Fa%20b');
    await userEvent.click(screen.getByRole('button', {name: 'Forward'}));
    expect(await screen.findByRole('tab', {name: 'Completed'})).toHaveAttribute('aria-selected', 'true');
});

it('keeps submission and script actions under My jobs for administrators', async () => {
    open('/home/active-jobs');
    await userEvent.click(await screen.findByRole('button', {name: 'Submit'}));
    expect(await screen.findByText('SubmitJob content')).toBeInTheDocument();
    expect(screen.getByRole('heading', {name: 'My jobs', level: 1})).toBeInTheDocument();
    expect(screen.getByRole('tab', {name: 'Active'})).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByLabelText('Breadcrumbs')).toHaveTextContent('Submit job');
    await userEvent.click(screen.getByRole('button', {name: 'Write script'}));
    expect(await screen.findByText('ScriptWorkbench content')).toBeInTheDocument();
    expect(screen.getByRole('tab', {name: 'Active'})).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByLabelText('Breadcrumbs')).toHaveTextContent('Write script');
});

it('allows a desktop-only administrator to open the shared image view', async () => {
    vi.spyOn(context.auth(), 'isModuleAdmin').mockImplementation(module => module === 'virtual-desktop-controller');
    open('/soca/custom-amis');
    expect(await screen.findByText('HpcCustomAmis content')).toBeInTheDocument();
    expect(screen.getByRole('tab', {name: 'Custom images'})).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByLabelText('Breadcrumbs')).toHaveTextContent('Custom images');
    expect(screen.queryByText('Destination unavailable')).not.toBeInTheDocument();
});

it('denies an admin route before its page mounts for a plain user', async () => {
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(false);
    open('/soca/active-jobs');
    expect(await screen.findByText('Destination unavailable')).toBeInTheDocument();
    expect(screen.queryByText('All active jobs')).not.toBeInTheDocument();
});

it.each(['/reporting', '/reporting/projects', '/reporting/facets'])('denies %s without requesting reporting data', async path => {
    vi.spyOn(context.auth(), 'isReportingResolved').mockReturnValue(true);
    vi.spyOn(context.auth(), 'canReadReporting').mockReturnValue(false);
    const summary = vi.spyOn(context.reporting(), 'getSummary');
    const rows = vi.spyOn(context.reporting(), 'listRows');
    open(path);
    expect(await screen.findByRole('alert')).toHaveTextContent('Access denied');
    expect(summary).not.toHaveBeenCalled();
    expect(rows).not.toHaveBeenCalled();
});

it('waits for Reporting capability resolution before mounting a direct view', async () => {
    vi.spyOn(context.auth(), 'isReportingResolved').mockReturnValue(false);
    const summary = vi.spyOn(context.reporting(), 'getSummary');
    open('/reporting/projects');
    expect(await screen.findByText('Checking Reporting access')).toBeInTheDocument();
    expect(summary).not.toHaveBeenCalled();
});

it.each(['operations only', 'administrator', 'manager', 'module with explicit grant', 'custom group grant'])('opens Reporting for %s without adding administrative access', async role => {
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(role === 'administrator' || role === 'manager');
    vi.spyOn(context.auth(), 'isReportingResolved').mockReturnValue(true);
    vi.spyOn(context.auth(), 'canReadReporting').mockReturnValue(true);
    vi.spyOn(context.reporting(), 'getSummary').mockRejectedValue({message: 'No source response.'});
    open('/reporting/projects');
    expect(await screen.findByRole('heading', {name: 'Reporting', level: 1})).toBeInTheDocument();
    expect(await screen.findByText('No source response.')).toBeInTheDocument();
    if (role !== 'administrator' && role !== 'manager') expect(screen.queryByText('Administration')).not.toBeInTheDocument();
});

it.each([
    ['/virtual-desktop/settings?view=services&project=sample', '/cluster/services?project=sample&group=desktop'],
    ['/soca/settings?view=service&project=sample', '/cluster/services?project=sample&group=jobs'],
    ['/virtual-desktop/settings?tab=controller&group=desktop', '/cluster/services?group=desktop'],
    ['/soca/settings?tab=general&group=jobs', '/cluster/services?group=jobs'],
    ['/cluster/users?view=reconciliation&project=sample', '/cluster/reconciliation-runs?project=sample'],
    ['/cluster/status?operation=backfill-history&project=sample', '/cluster/settings/cost-collection?operation=backfill-history&project=sample'],
])('replaces the legacy bookmark %s and retains query state', async (path, expected) => {
    open(path);
    await screen.findByText(expected.includes('/cluster/services') ? 'Service content' : expected.includes('/reconciliation-runs') ? 'ReconciliationRuns content' : 'Settings content');
    expect(screen.getByTestId('url')).toHaveTextContent(expected);
});

it('replaces backfill bookmarks with the jobs-authorized Settings base', async () => {
    vi.mocked(context.auth().isModuleAdmin).mockImplementation(module => module === 'scheduler');
    open('/cluster/status?operation=backfill-history&project=sample');
    expect(await screen.findByText('Settings content')).toBeVisible();
    expect(screen.getByTestId('url')).toHaveTextContent('/soca/settings/cost-collection?operation=backfill-history&project=sample');
});

it.each(['/cluster/services', '/cluster/reconciliation-runs', '/cluster/status?operation=backfill-history', '/virtual-desktop/settings?view=services', '/soca/settings/cost-collection'])('denies %s to plain users before mounting privileged content', async path => {
    vi.mocked(context.auth().isModuleAdmin).mockReturnValue(false);
    const catalog = vi.spyOn(context.client().clusterSettings(), 'describeSettingsCatalog');
    const services = vi.spyOn(context.client().clusterSettings(), 'listClusterServices');
    const backfill = vi.spyOn(context.client().clusterSettings(), 'getCostMetricsBackfill');
    open(path);
    expect(await screen.findByText('Destination unavailable')).toBeVisible();
    expect(screen.queryByText('Service content')).toBeNull();
    expect(screen.queryByText('Settings content')).toBeNull();
    expect(catalog).not.toHaveBeenCalled(); expect(services).not.toHaveBeenCalled(); expect(backfill).not.toHaveBeenCalled();
});

it('preserves canonical service selection through back and forward after replacing an alias', async () => {
    render(<MemoryRouter initialEntries={['/cluster/status', '/soca/settings?view=service&project=sample']} initialIndex={1}><App/><History/></MemoryRouter>);
    expect(await screen.findByText('Service content')).toBeVisible();
    expect(screen.getByRole('tab', {name: 'Services'})).toHaveAttribute('aria-selected', 'true');
    await userEvent.click(screen.getByRole('button', {name: 'Back'}));
    expect(await screen.findByText('ClusterStatus content')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Forward'}));
    expect(await screen.findByText('Service content')).toBeVisible();
    expect(screen.getByTestId('url')).toHaveTextContent('/cluster/services?project=sample&group=jobs');
});

it('focuses the service group retained by a legacy bookmark', async () => {
    open('/soca/settings?view=service');
    const heading = await screen.findByRole('heading', {name: 'Job service'});
    await waitFor(() => expect(document.activeElement).toBe(heading));
});

it('denies Costs to desktop-only administrators before mounting its content', async () => {
    vi.mocked(context.auth().isModuleAdmin).mockImplementation(module => module === 'virtual-desktop-controller');
    open('/cluster/status?operation=backfill-history');
    expect(await screen.findByText('Destination unavailable')).toBeVisible();
    expect(screen.queryByText('Settings content')).toBeNull();
});
