import {describe, expect, it, vi} from 'vitest';
import {initTestAppContext} from '../test-support';
import {IdeaSideNavHeader, IdeaSideNavItems} from './side-nav-items';
import {PORTAL_TASKS, permittedViews, resolveTask} from './task-navigation';

function context(admin = false) {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'hasModuleAccess').mockReturnValue(true);
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(admin);
    const service = context.getClusterSettingsService();
    vi.spyOn(service, 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(service, 'isSchedulerDeployed').mockReturnValue(true);
    vi.spyOn(service, 'isBastionHostDeployed').mockReturnValue(true);
    return context;
}

describe('portal navigation', () => {
    it('puts personal destinations first and has no administrative boundary for a plain user', () => {
        expect(IdeaSideNavItems(context()).map(item => 'text' in item && item.text)).toEqual(['My desktops', 'My jobs', 'Files', 'SSH access', 'My costs']);
    });
    it('has one collapsible Administration boundary with tasks in order', () => {
        const items = IdeaSideNavItems(context(true));
        expect(items.slice(0, 5).map(item => 'text' in item && item.text)).toEqual(['My desktops', 'My jobs', 'Files', 'SSH access', 'My costs']);
        expect(items[5]).toMatchObject({type: 'section', text: 'Administration', defaultExpanded: true});
        expect((items[5] as any).items.map((item: any) => item.text)).toEqual(['Manage desktops', 'Manage jobs', 'Images and applications', 'Projects', 'People and access', 'Costs and usage', 'Operations', 'Settings']);
        expect(items).toHaveLength(6);
    });
    it('uses Reports only when a usable dashboard URL is configured, ignoring its custom title', () => {
        const ctx = context();
        const service = ctx.getClusterSettingsService();
        service.customDashboard = {enabled: true, title: 'Conflicting name', url: ''};
        expect(IdeaSideNavItems(ctx)).not.toContainEqual(expect.objectContaining({text: 'Reports'}));
        service.customDashboard.url = 'https://reports.example.org';
        expect(IdeaSideNavItems(ctx).at(-1)).toMatchObject({text: 'Reports', href: '#/home/custom-dashboard'});
        service.customDashboard.enabled = false;
        expect(IdeaSideNavItems(ctx)).not.toContainEqual(expect.objectContaining({text: 'Reports'}));
    });
    it('keeps module administrator views separate and removes undeployed modules', () => {
        const ctx = context();
        vi.spyOn(ctx.auth(), 'isModuleAdmin').mockImplementation(module => module === 'virtual-desktop-controller');
        const items = IdeaSideNavItems(ctx);
        expect((items.at(-1) as any).items.map((item: any) => item.text)).toEqual(['Manage desktops', 'Images and applications', 'People and access', 'Operations', 'Settings']);
        expect(permittedViews(PORTAL_TASKS.find(task => task.id === 'images-applications')!, ctx).map(view => view.label)).toEqual(['Desktop images', 'Custom images']);
        vi.spyOn(ctx.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(false);
        expect(IdeaSideNavItems(ctx).some(item => item.type === 'section')).toBe(false);
        expect(IdeaSideNavHeader(ctx).href).toBe('#/home/active-jobs');
        vi.spyOn(ctx.auth(), 'hasModuleAccess').mockImplementation(module => module === 'cluster-manager');
        expect(IdeaSideNavHeader(ctx).href).toBe('#/home/file-browser');
    });
    it.each([
        ['/home/virtual-desktops', 'my-desktops', 'Owned'], ['/home/shared-desktops', 'my-desktops', 'Shared'],
        ['/home/active-jobs', 'my-jobs', 'Active'], ['/home/completed-jobs', 'my-jobs', 'Completed'],
        ['/dashboard', 'my-jobs', 'Active'], ['/home/script-workbench', 'my-jobs', 'Write script'], ['/soca/jobs/submit-job', 'my-jobs', 'Submit job'],
        ['/home/file-browser', 'files', 'Files'], ['/home/file-browser/tail', 'files', 'Files'], ['/home/ssh-access', 'ssh-access', 'SSH access'],
        ['/home/my-costs', 'my-costs', 'My costs'], ['/home/custom-dashboard', 'reports', 'Reports'],
        ['/virtual-desktop/dashboard', 'manage-desktops', 'Overview'], ['/virtual-desktop/sessions', 'manage-desktops', 'Sessions'],
        ['/virtual-desktop/sessions/session%2Fid', 'manage-desktops', 'Sessions'], ['/virtual-desktop/debug', 'manage-desktops', 'Diagnostics'],
        ['/virtual-desktop/software-stacks', 'images-applications', 'Desktop images'], ['/virtual-desktop/software-stacks/image-id', 'images-applications', 'Desktop images'],
        ['/virtual-desktop/permission-profiles', 'people-access', 'Desktop permissions'], ['/virtual-desktop/permission-profiles/profile-id', 'people-access', 'Desktop permissions'],
        ['/soca/active-jobs', 'manage-jobs', 'Active'], ['/soca/completed-jobs', 'manage-jobs', 'Completed'],
        ['/soca/queues', 'manage-jobs', 'Queues'], ['/soca/queues/create', 'manage-jobs', 'Queues'], ['/soca/queues/update', 'manage-jobs', 'Queues'],
        ['/soca/licenses', 'manage-jobs', 'Licenses'], ['/soca/licenses/create', 'manage-jobs', 'Licenses'], ['/soca/licenses/update', 'manage-jobs', 'Licenses'],
        ['/soca/applications', 'images-applications', 'Submission forms'], ['/soca/applications/create', 'images-applications', 'Submission forms'], ['/soca/applications/update', 'images-applications', 'Submission forms'],
        ['/soca/custom-amis', 'images-applications', 'Custom images'], ['/cluster/projects', 'projects', 'Projects'],
        ['/cluster/users', 'people-access', 'Users'], ['/cluster/groups', 'people-access', 'Groups'],
        ['/cluster/user-costs', 'costs-usage', 'By user'], ['/cluster/status', 'operations', 'Health'],
        ['/cluster/settings', 'settings', 'Settings'], ['/virtual-desktop/settings', 'settings', 'Settings'], ['/soca/settings', 'settings', 'Settings'], ['/cluster/email-templates', 'settings', 'Settings'],
    ])('%s retains its task and local view', (path, task, view) => {
        const resolved = resolveTask(path, '?path=%2Fhome%2Fuser%2Fa%20b&job_id=123');
        expect(resolved?.task.id).toBe(task);
        expect(resolved?.view.label).toBe(view);
    });
    it('keeps account and authentication outside sidebar tasks and exposes the added local views', () => {
        expect(resolveTask('/home/account-settings')).toBeUndefined();
        expect(resolveTask('/auth/login')).toBeUndefined();
        expect(resolveTask('/soca/active-jobs', '?view=nodes')?.view.label).toBe('Nodes');
        expect(resolveTask('/cluster/users', '?view=reconciliation')?.view.label).toBe('Reconciliation runs');
        expect(resolveTask('/virtual-desktop/settings', '?view=services')?.task.id).toBe('operations');
        expect(resolveTask('/soca/jobs/submit-job')?.view.tab).toBe(false);
        expect(resolveTask('/home/script-workbench')?.view.tab).toBe(false);
    });
});
