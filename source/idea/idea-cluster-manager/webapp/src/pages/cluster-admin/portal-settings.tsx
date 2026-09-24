import ClusterServices, {hasContainerControlPlane} from './cluster-services';
import React, {useEffect, useState} from 'react';
import {Alert, Box, ColumnLayout, Container, ExpandableSection, Header, Input, KeyValuePairs, Link, SideNavigation, SpaceBetween, Table} from '@cloudscape-design/components';
import {useLocation, useNavigate, useSearchParams} from 'react-router-dom';
import {AppContext} from '../../common';
import IdeaAppLayout from '../../components/app-layout';
import {EmbeddedPageContext} from '../../components/app-layout/embedded-page-context';
import {hasAccess} from '../../navigation/task-navigation';
import ClusterSettings from './cluster-settings';
import DesktopSettings from '../virtual-desktops/virtual-desktop-settings';
import SchedulerSettings from '../hpc/hpc-scheduler-settings';
import EmailTemplates from './email-templates';
import MetricsHistorySettings from './metrics-history-settings';
import {EMPTY_SETTINGS, SettingsSource} from './settings-sections';
import {SettingDefinition} from '../../client/data-model';
import CatalogSettingsSection, {settingAnchor} from './catalog-settings-section';
import './portal-settings.scss';

export const SETTINGS_GROUPS = [
    ['appearance', 'Portal appearance and reports', 'Cluster General'],
    ['desktop-lifecycle', 'Desktop access and lifecycle', 'Desktop General Schedule Server'],
    ['job-placement', 'Job limits and placement', 'Scheduler General'],
    ['ai-access', 'AI access and spending', 'Bedrock'],
    ['account-synchronization', 'Account synchronization', 'Account reconciliation'],
    ['email', 'Email and notifications', 'Desktop Notifications Email Templates'],
    ['maintenance', 'Maintenance notice', 'Maintenance'],
    ['sign-in', 'Sign-in and directory', 'Identity Provider Directory Service'],
    ['network', 'Network access', 'Network Route 53 EC2 Controller Broker Connection Gateway'],
    ['storage', 'Storage', 'Shared Storage'],
    ['backup', 'Backup and recovery', 'Cluster Backup Desktop Backup'],
    ['cost-collection', 'Cost collection', 'Cost estimation'],
    ['monitoring', 'Monitoring and logs', 'Analytics Metrics CloudWatch Logs'],
    ['resource-tags', 'Resource tags', 'Tags'],
    ['deployment', 'Deployment details', 'Cluster General AWS Account Packages GPU versions'],
] as const;
const ADVANCED_GROUPS = new Set(['sign-in', 'network', 'storage', 'backup', 'cost-collection', 'monitoring', 'resource-tags', 'deployment']);

export function legacySettingsGroup(path: string, tab: string | null): string {
    if (path === '/cluster/email-templates') return 'email';
    if (path === '/virtual-desktop/settings') return ({general: 'desktop-lifecycle', schedule: 'desktop-lifecycle', server: 'desktop-lifecycle', notifications: 'email', controller: 'network', broker: 'network', 'connection-gateway': 'network', backups: 'backup', 'cloudwatch-logs': 'monitoring'} as Record<string, string>)[tab ?? 'general'] ?? 'desktop-lifecycle';
    if (path === '/soca/settings') return tab === 'cloudwatch-logs' ? 'monitoring' : 'job-placement';
    return ({general: 'appearance', network: 'network', 'shared-storage': 'storage', 'identity-provider': 'sign-in', 'directory-service': 'sign-in', analytics: 'monitoring', metrics: 'monitoring', maintenance: 'maintenance', 'account-reconciliation': 'account-synchronization', bedrock: 'ai-access', 'cloudwatch-logs': 'monitoring', ses: 'email', ec2: 'network', backups: 'backup', 'route-53': 'network', 'aws-account': 'deployment'} as Record<string, string>)[tab ?? 'general'] ?? 'appearance';
}

function section(source: SettingsSource, ...ids: string[]) {
    return ids.map(id => <React.Fragment key={id}>{source.sections.find(item => item.id === id)?.content}</React.Fragment>);
}

function parts(source: SettingsSource, id: string, indices: number[]) {
    const content = source.sections.find(item => item.id === id)?.content;
    if (!React.isValidElement<{children: React.ReactNode}>(content)) return null;
    const children = React.Children.toArray(content.props.children);
    return indices.map(index => children[index]);
}

export function SettingsGroups({cluster = EMPTY_SETTINGS, desktop = EMPTY_SETTINGS, scheduler = EMPTY_SETTINGS, pageProps}: {cluster?: SettingsSource; desktop?: SettingsSource; scheduler?: SettingsSource; pageProps: any}) {
    const location = useLocation();
    const navigate = useNavigate();
    const [params] = useSearchParams();
    const [search, setSearch] = useState('');
    const [catalog, setCatalog] = useState<SettingDefinition[]>([]);
    const [values, setValues] = useState<Record<string, any>>({});
    const [errors, setErrors] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [advanced, setAdvanced] = useState(false);
    const [editingSection, setEditingSection] = useState<string | null>(null);
    const context = AppContext.get();
    const service = context.getClusterSettingsService();
    const canCluster = hasAccess(context, 'cluster-admin');
    const canDesktop = hasAccess(context, 'desktop-admin');
    const canJobs = hasAccess(context, 'jobs-admin');
    const permitted = (module: string) => module === 'virtual-desktop-controller' ? canDesktop : module === 'scheduler' ? canJobs : canCluster;
    const base = location.pathname.startsWith('/virtual-desktop') ? '/virtual-desktop/settings' : location.pathname.startsWith('/soca') ? '/soca/settings' : '/cluster/settings';
    const selected = location.pathname.slice(base.length + 1);
    const target = params.get('key');
    const moduleId = (module: string) => module === 'global-settings' ? module : service.getModuleId(module);
    const enabled = (module: string) => ['global-settings', 'cluster', 'cluster-manager'].includes(module) || service.isModuleEnabled(module);
    const destination = (group: string, key?: string) => {
        const next = new URLSearchParams(params); next.delete('tab'); next.delete('group'); next.delete('key');
        if (key) next.set('key', key);
        return `${base}/${group}${next.size ? `?${next}` : ''}`;
    };
    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            try {
                const result = await context.client().clusterSettings().describeSettingsCatalog();
                const available = result.settings.filter(item => permitted(item.module) && enabled(item.module) && moduleId(item.module));
                if (cancelled) return;
                setCatalog(available);
                await Promise.all(Array.from(new Set(available.map(item => item.module))).map(async module => {
                    try {
                        const result = await context.client().clusterSettings().getModuleSettings({module_id: moduleId(module)!});
                        if (!cancelled) setValues(current => ({...current, [module]: result.settings ?? {}}));
                    } catch (reason: any) {
                        if (!cancelled) setErrors(current => [...current, `${module}: ${reason.message ?? 'could not load settings'}`]);
                    }
                }));
            } catch (reason: any) {
                if (!cancelled) setErrors(current => [...current, reason.message ?? 'Could not load the settings catalog.']);
            } finally { if (!cancelled) setLoading(false); }
        };
        void load();
        return () => {cancelled = true;};
        // The mounted page has a fixed authorization context and module set.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    useEffect(() => {
        if (!selected && (params.has('tab') || params.has('group') || location.pathname === '/cluster/email-templates')) {
            navigate(destination(params.get('group') ?? legacySettingsGroup(location.pathname, params.get('tab'))), {replace: true});
        }
        setAdvanced(false);
        setEditingSection(null);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.pathname, location.search]);
    useEffect(() => {
        if (target && catalog.some(item => item.key === target && item.group === selected && item.advanced)) setAdvanced(true);
    }, [target, selected, catalog]);
    useEffect(() => {
        if (!loading && target) document.getElementById(settingAnchor(target))?.scrollIntoView?.({block: 'center'});
    }, [target, selected, loading, advanced, values]);
    const allowed = (id: string) => {
        if (id === 'desktop-lifecycle') return canDesktop;
        if (id === 'job-placement') return canJobs;
        if (['email', 'network', 'ai-access', 'monitoring'].includes(id)) return canCluster || canDesktop || canJobs;
        if (id === 'backup') return canCluster || canDesktop;
        if (id === 'cost-collection') return canCluster || canJobs;
        return canCluster;
    };
    const groups = SETTINGS_GROUPS.filter(([id]) => allowed(id));
    const query = search.trim().toLowerCase();
    const results = query ? catalog.filter(item => `${item.key} ${item.label} ${item.description} ${item.section}`.toLowerCase().includes(query)) : [];
    const visible = groups.filter(([id, title, aliases]) => !query || `${title} ${aliases}`.toLowerCase().includes(query) || results.some(item => item.group === id));
    const groupName = (id: string) => groups.find(([group]) => group === id)?.[1] ?? id;
    const entries = catalog.filter(item => item.group === selected);
    const renderSections = (isAdvanced: boolean) => {
        const sections = new Map<string, SettingDefinition[]>();
        entries.filter(item => item.advanced === isAdvanced).forEach(item => {
            const key = `${item.module}:${item.section}`;
            sections.set(key, [...(sections.get(key) ?? []), item]);
        });
        return Array.from(sections.entries()).map(([key, settings]) => settings[0].module in values && <CatalogSettingsSection key={key} settings={settings}
            values={values[settings[0].module]} moduleId={moduleId(settings[0].module)!} highlightedKey={target} editing={editingSection === key}
            editDisabled={editingSection !== null && editingSection !== key} onEdit={() => setEditingSection(key)} onEditingEnd={() => setEditingSection(null)} onSaved={patch => setValues(current => {
                const module = settings[0].module;
                const updated = structuredClone(current[module]);
                for (const [path, value] of Object.entries(patch)) {
                    const parts = path.split('.'); const leaf = parts.pop()!;
                    parts.reduce((node, part) => node[part] ??= {}, updated)[leaf] = value;
                }
                return {...current, [module]: updated};
            })}/>);
    };
    const guided: Record<string, React.ReactNode> = {
        'desktop-lifecycle': section(desktop, 'general', 'schedule', 'server'),
        'ai-access': canCluster && section(cluster, 'bedrock'),
        'account-synchronization': section(cluster, 'account-reconciliation'),
        maintenance: section(cluster, 'maintenance'),
        monitoring: canCluster && <MetricsHistorySettings active={true}/>,
        email: canCluster && <EmbeddedPageContext.Provider value={true}><EmailTemplates {...pageProps}/></EmbeddedPageContext.Provider>,
    };
    const title = groups.find(([id]) => id === selected)?.[1];
    const allErrors = [...errors, ...(cluster.errors ?? []), ...(desktop.errors ?? []), ...(scheduler.errors ?? [])];
    return <div className="portal-settings">
        <nav aria-label="Settings groups">
            <Link href={`#${base}`} onFollow={event => {event.preventDefault(); navigate(base);}}>All settings</Link>
            <Input ariaLabel="Search settings" placeholder="Search settings or configuration keys" type="search" value={search} onChange={event => setSearch(event.detail.value)}/>
            {visible.length > 0 && <SideNavigation activeHref={selected ? `#${destination(selected)}` : `#${base}`}
                onFollow={event => {event.preventDefault(); navigate(event.detail.href.replace(/^#/, ''));}}
                items={[
                    ...visible.filter(([id]) => !ADVANCED_GROUPS.has(id)).map(([id, name]) => ({type: 'link' as const, text: name, href: `#${destination(id)}`})),
                    ...(visible.some(([id]) => ADVANCED_GROUPS.has(id)) ? [{
                        type: 'section' as const, text: 'More settings',
                        items: visible.filter(([id]) => ADVANCED_GROUPS.has(id)).map(([id, name]) => ({type: 'link' as const, text: name, href: `#${destination(id)}`}))
                    }] : [])
                ]}/>}
            {!visible.length && <p>No matching settings.</p>}
            {results.length > 0 && <Table ariaLabels={{tableLabel: 'Matching settings'}} variant="embedded" trackBy="key" items={results} wrapLines={true}
                columnDefinitions={[
                    {id: 'setting', header: 'Setting', cell: item => <SpaceBetween size="xxs"><Link href={`#${destination(item.group, item.key)}`}
                        onFollow={event => {event.preventDefault(); navigate(destination(item.group, item.key));}}>{item.label}</Link>
                        <Box variant="small" color="text-body-secondary">{item.key}</Box></SpaceBetween>},
                    {id: 'group', header: 'Group', cell: item => groupName(item.group)},
                ]}/>}
        </nav>
        <div>
            {allErrors.length > 0 && <Alert type="error" header="Some settings could not be loaded">{allErrors.map((message, index) => <p key={index}>{message}</p>)}</Alert>}
            {loading && <p role="status">Loading settings…</p>}
            {!selected && <p>Choose a group or search for a setting; edit and save one section at a time.</p>}
            {selected && !title && <Alert type="warning">Settings group not found.</Alert>}
            {title && <section aria-label={title} key={selected}>
                <Header variant="h2">{title}</Header>
                {selected === 'deployment' ? <SpaceBetween size="l"><Container header={<Header variant="h2">Installed deployment</Header>}><ColumnLayout columns={2}><KeyValuePairs columns={2} items={[
                    {label: 'Region', value: cluster.values.cluster?.aws?.region ?? 'Not available'},
                    {label: 'Version', value: context.getClusterSettingsService().getModuleInfo('cluster-manager')?.version ?? 'Not available'},
                ]}/></ColumnLayout></Container><Link href="#/cluster/status">Operations</Link>{renderSections(false)}{entries.some(item => item.advanced) && <details open={advanced} onToggle={event => setAdvanced(event.currentTarget.open)}><summary>Advanced</summary><SpaceBetween size="l">{renderSections(true)}</SpaceBetween></details>}</SpaceBetween> : <SpaceBetween size="l">
                    {guided[selected]}
                    {selected === 'storage' && !loading && entries.length === 0 && <p>No file systems configured.</p>}
                    {selected === 'job-placement' && <Link href="#/soca/queues">Queue overrides in Manage jobs</Link>}
                    {selected === 'ai-access' && canCluster && [<Link key="projects" href="#/cluster/projects">Project access</Link>, <Link key="usage" href="#/cluster/user-costs">Costs and usage</Link>]}
                    {selected === 'resource-tags' && <Link href="#/cluster/projects">Project tags</Link>}
                    {renderSections(false)}
                    {entries.some(item => item.advanced) && <ExpandableSection headerText="Advanced" expanded={advanced} onChange={event => setAdvanced(event.detail.expanded)}>
                        <SpaceBetween size="l">{renderSections(true)}</SpaceBetween>
                    </ExpandableSection>}
                </SpaceBetween>}
            </section>}
        </div>
    </div>;
}

export default function PortalSettings(props: any) {
    const context = AppContext.get();
    const renderScheduler = (cluster: SettingsSource, desktop: SettingsSource) => hasAccess(context, 'jobs-admin')
        ? <SchedulerSettings {...props} renderSections={(scheduler: SettingsSource) => <SettingsGroups cluster={cluster} desktop={desktop} scheduler={scheduler} pageProps={props}/>}/>
        : <SettingsGroups cluster={cluster} desktop={desktop} pageProps={props}/>;
    const renderDesktop = (cluster: SettingsSource) => hasAccess(context, 'desktop-admin')
        ? <DesktopSettings {...props} renderSections={(desktop: SettingsSource) => renderScheduler(cluster, desktop)}/>
        : renderScheduler(cluster, EMPTY_SETTINGS);
    const content = hasAccess(context, 'cluster-admin') ? <ClusterSettings {...props} renderSections={renderDesktop}/> : renderDesktop(EMPTY_SETTINGS);
    return <IdeaAppLayout {...props} ideaPageId="settings" header={<Header variant="h1">Settings</Header>} contentType="default" content={content}/>;
}

export function SettingsServiceDetails(props: any) {
    const location = useLocation();
    const navigate = useNavigate();
    const desktop = location.pathname.startsWith('/virtual-desktop');
    const [container, setContainer] = useState<boolean | undefined>();
    const [serviceError, setServiceError] = useState(false);
    useEffect(() => { hasContainerControlPlane().then(setContainer).catch(() => setServiceError(true)); }, []);
    const Provider = desktop ? DesktopSettings : SchedulerSettings;
    const module = desktop ? 'virtual-desktop-controller' : 'scheduler';
    const info = AppContext.get().getClusterSettingsService().getModuleInfo(module);
    useEffect(() => {
        const params = new URLSearchParams(location.search);
        if (params.get('tab') === (desktop ? 'controller' : 'general')) {
            params.delete('tab'); params.set('view', desktop ? 'services' : 'service');
            navigate(`${location.pathname}?${params}`, {replace: true});
        }
    }, [location.pathname, location.search, navigate, desktop]);
    return <IdeaAppLayout {...props} header={<Header variant="h1">{desktop ? 'Desktop services' : 'Job service'}</Header>} content={<SpaceBetween size="l">
        {serviceError ? <Alert type="error">Could not read service settings. Reload the page to try again.</Alert> : container === undefined ? <p>Loading service settings</p> : container ? <ClusterServices desktop={desktop}/> : <SpaceBetween size="m">
        <dl><dt>Module</dt><dd>{info?.name}</dd><dt>Module ID</dt><dd>{info?.module_id}</dd><dt>Version</dt><dd>{info?.version}</dd></dl>
        <Alert type="info">These are deployment settings, not a live service inventory. Host autoscaling settings may not describe container capacity. Use Operations runbooks to change capacity or restart services.</Alert>
        <Link external href="https://docs.idea-hpc.com/first-time-users/cluster-operations">CLI runbooks</Link>
        <Provider {...props} renderSections={(source: SettingsSource) => desktop ? <>{section(source, 'controller')}{parts(source, 'broker', [0, 1])}{parts(source, 'connection-gateway', [0, 1])}</> : null}/>
        </SpaceBetween>}
    </SpaceBetween>}/>;
}
