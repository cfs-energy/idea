import {AppContext} from '../common';
import {Constants} from '../common/constants';

export type Access = 'reporting' | 'desktop' | 'jobs' | 'cluster' | 'desktop-admin' | 'jobs-admin' | 'cluster-admin';
export interface TaskView {label: string; path: string; access: Access | Access[]; tab?: boolean}
export interface PortalTask {id: string; title: string; views: TaskView[]; admin?: boolean}
const view = (label: string, path: string, access: Access | Access[], tab = true): TaskView => ({label, path, access, tab});

export const PORTAL_TASKS: PortalTask[] = [
    {id: 'my-desktops', title: 'My desktops', views: [view('Owned', '/home/virtual-desktops', 'desktop'), view('Shared', '/home/shared-desktops', 'desktop')]},
    {id: 'my-jobs', title: 'My jobs', views: [view('Active', '/home/active-jobs', 'jobs'), view('Completed', '/home/completed-jobs', 'jobs'), view('Submit job', '/soca/jobs/submit-job', 'jobs', false), view('Write script', '/home/script-workbench', 'jobs', false)]},
    {id: 'files', title: 'Files', views: [view('Files', '/home/file-browser', 'cluster')]},
    {id: 'ssh-access', title: 'SSH access', views: [view('SSH access', '/home/ssh-access', 'cluster')]},
    {id: 'my-costs', title: 'My costs', views: [view('My costs', '/home/my-costs', 'cluster')]},
    {id: 'reporting', title: 'Reporting', admin: false, views: [view('Overview / By user', '/reporting', 'reporting', false), view('By project', '/reporting/projects', 'reporting', false), view('By facet', '/reporting/facets', 'reporting', false)]},
    {id: 'reports', title: 'Reports', views: [view('Reports', '/home/custom-dashboard', 'cluster')]},
    {id: 'manage-desktops', title: 'Manage desktops', admin: true, views: [view('Overview', '/virtual-desktop/dashboard', 'desktop-admin'), view('Sessions', '/virtual-desktop/sessions', 'desktop-admin'), view('Diagnostics', '/virtual-desktop/debug', 'desktop-admin')]},
    {id: 'manage-jobs', title: 'Manage jobs', admin: true, views: [view('Active', '/soca/active-jobs', 'jobs-admin'), view('Completed', '/soca/completed-jobs', 'jobs-admin'), view('Queues', '/soca/queues', 'jobs-admin'), view('Licenses', '/soca/licenses', 'jobs-admin'), view('Nodes', '/soca/active-jobs?view=nodes', 'jobs-admin')]},
    {id: 'images-applications', title: 'Images and applications', admin: true, views: [view('Desktop images', '/virtual-desktop/software-stacks', 'desktop-admin'), view('Custom images', '/soca/custom-amis', ['desktop-admin', 'jobs-admin']), view('Submission forms', '/soca/applications', 'jobs-admin')]},
    {id: 'projects', title: 'Projects', admin: true, views: [view('Projects', '/cluster/projects', 'cluster-admin')]},
    {id: 'people-access', title: 'People and access', admin: true, views: [view('Users', '/cluster/users', 'cluster-admin'), view('Groups', '/cluster/groups', 'cluster-admin'), view('Desktop permissions', '/virtual-desktop/permission-profiles', 'desktop-admin')]},
    {id: 'costs-usage', title: 'Costs and usage', admin: true, views: [view('By user', '/cluster/user-costs', 'cluster-admin')]},
    {id: 'operations', title: 'Operations', admin: true, views: [view('Health', '/cluster/status', 'cluster-admin'), view('Services', '/cluster/services', ['cluster-admin', 'desktop-admin', 'jobs-admin']), view('Reconciliation runs', '/cluster/reconciliation-runs', 'cluster-admin')]},
    {id: 'settings', title: 'Settings', admin: true, views: [view('Settings', '/cluster/settings', 'cluster-admin'), view('Settings', '/virtual-desktop/settings', 'desktop-admin'), view('Settings', '/soca/settings', 'jobs-admin')]},
];

export const LANDING_PATHS: Record<string, string> = {
    home: '/home',
    'my-jobs': '/home/active-jobs',
    'my-desktops': '/home/virtual-desktops',
    files: '/home/file-browser',
    'my-costs': '/home/my-costs',
    reports: '/home/custom-dashboard'
};

export function hasAccess(context: AppContext, access: Access): boolean {
    if (access === 'reporting') return context.auth().canReadReporting();
    const service = context.getClusterSettingsService();
    const module = access.startsWith('desktop') ? Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER : access.startsWith('jobs') ? Constants.MODULE_SCHEDULER : Constants.MODULE_CLUSTER_MANAGER;
    const deployed = module === Constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER ? service.isVirtualDesktopDeployed() : module === Constants.MODULE_SCHEDULER ? service.isSchedulerDeployed() : true;
    return deployed && (access.endsWith('-admin') ? context.auth().isModuleAdmin(module) : context.auth().hasModuleAccess(module));
}

export function permittedViews(task: PortalTask, context: AppContext): TaskView[] {
    return task.views.filter(item => (Array.isArray(item.access) ? item.access : [item.access]).some(access => hasAccess(context, access)));
}

export function tabViews(task: PortalTask, context: AppContext): TaskView[] {
    return permittedViews(task, context).filter(item => item.tab !== false);
}

export function legacyRoute(pathname: string, search: string, context?: AppContext): string | undefined {
    const query = new URLSearchParams(search);
    const desktop = pathname === '/virtual-desktop/settings' && (query.get('view') === 'services' || query.get('tab') === 'controller');
    const jobs = pathname === '/soca/settings' && (query.get('view') === 'service' || query.get('tab') === 'general');
    let target: string | undefined;
    if (desktop || jobs) {
        if (query.get('view') === (desktop ? 'services' : 'service')) query.delete('view');
        if (query.get('tab') === (desktop ? 'controller' : 'general')) query.delete('tab');
        if (!query.has('group')) query.set('group', desktop ? 'desktop' : 'jobs');
        target = '/cluster/services';
    } else if (pathname === '/cluster/users' && query.get('view') === 'reconciliation') {
        query.delete('view'); target = '/cluster/reconciliation-runs';
    } else if (pathname === '/cluster/status' && query.get('operation') === 'backfill-history') {
        target = `${context && !hasAccess(context, 'cluster-admin') && hasAccess(context, 'jobs-admin') ? '/soca/settings' : '/cluster/settings'}/cost-collection`;
    }
    return target ? `${target}${query.size ? '?' + query : ''}` : undefined;
}

export function resolveTask(pathname: string, search = ''): {task: PortalTask; view: TaskView} | undefined {
    const redirected = legacyRoute(pathname, search);
    if (redirected) {
        const [path, query] = redirected.split('?');
        return resolveTask(path, query);
    }
    const query = new URLSearchParams(search);
    const special = query.get('view') ?? (pathname === '/virtual-desktop/settings' && query.get('tab') === 'controller' ? 'services' : pathname === '/soca/settings' && query.get('tab') === 'general' ? 'service' : null);
    if (special) {
        for (const task of PORTAL_TASKS) {
            const match = task.views.find(item => item.path === `${pathname}?view=${special}`);
            if (match) return {task, view: match};
        }
    }
    const alias = pathname === '/dashboard' ? '/home/active-jobs' : pathname === '/cluster/email-templates' ? '/cluster/settings' : pathname;
    for (const task of PORTAL_TASKS) {
        const match = task.views.find(item => !item.path.includes('?') && (
            alias === item.path || (item.path !== '/home' && item.path !== '/reporting' && alias.startsWith(`${item.path}/`))
        ));
        if (match) return {task, view: match};
    }
}
