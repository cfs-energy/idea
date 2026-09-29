import React, {useEffect, useState} from 'react';
import {Alert, Box, ExpandableSection, Header, Input, KeyValuePairs, Link, SideNavigation, SpaceBetween, Table} from '@cloudscape-design/components';
import {useLocation, useNavigate, useSearchParams} from 'react-router-dom';
import {AppContext} from '../../common';
import IdeaAppLayout from '../../components/app-layout';
import {EmbeddedPageContext} from '../../components/app-layout/embedded-page-context';
import {hasAccess} from '../../navigation/task-navigation';
import ClusterSettings from './cluster-settings';
import DesktopSettings from '../virtual-desktops/virtual-desktop-settings';
import SchedulerSettings from '../hpc/hpc-scheduler-settings';
import EmailTemplates from './email-templates';
import {DesktopScheduleTable, NotificationsTable} from './settings-tables';
import {EMPTY_SETTINGS, SettingsSource} from './settings-sections';
import {SettingDefinition} from '../../client/data-model';
import CatalogSettingsSection, {isHidden, settingAnchor} from './catalog-settings-section';
import CostEstimationSettings from './cost-estimation-settings';
import StorageMeasurementSettings from './storage-measurement-settings';
import {MetricsHistorySettings} from './metrics-history-settings';
import './portal-settings.scss';

export const SETTINGS_GROUPS = [
    ['appearance', 'General', 'Cluster General Maintenance'],
    ['desktop-lifecycle', 'Desktops', 'Desktop General Schedule Server'],
    ['job-placement', 'Jobs', 'Scheduler General'],
    ['ai-access', 'AI access', 'Bedrock'],
    ['email', 'Notifications', 'Desktop Notifications Email Templates'],
    ['sign-in', 'Users and sign-in', 'Identity Provider Directory Service Account reconciliation'],
    ['cost-collection', 'Costs', 'Cost estimation'],
    ['network', 'Network', 'Network Route 53 EC2 Controller Broker Connection Gateway'],
    ['storage', 'Storage', 'Shared Storage'],
    ['backup', 'Backup', 'Cluster Backup Desktop Backup'],
    ['monitoring', 'Monitoring', 'Analytics Metrics CloudWatch Logs'],
    ['deployment', 'Deployment', 'Cluster General AWS Account Packages GPU versions Tags'],
] as const;
export const SETTINGS_CARDS: Record<string, string[]> = {
    appearance: ['Portal', 'Cost header', 'Dashboard link', 'Regional defaults', 'Maintenance notice', 'GPU policy'],
    'desktop-lifecycle': ['Desktop placement', 'Desktop policy', 'Desktop schedule', 'Stopped desktop cleanup'],
    'job-placement': ['Job limits and placement', 'Fair-share scheduling', 'Scratch storage'],
    'ai-access': ['Amazon Bedrock'],
    email: ['Email delivery', 'Notifications', 'Email templates'],
    'sign-in': ['Account synchronization', 'Sign-in', 'Directory connection', 'Directory mapping', 'AD automation'],
    'cost-collection': ['Collection', 'Storage measurement', 'Cost estimation', 'History backfill'],
    network: ['Encryption and access', 'Load balancers and certificates', 'Network and connectivity'],
    storage: ['File systems'], backup: ['Backup policy'], monitoring: ['Analytics', 'Logs', 'Metrics'],
    deployment: ['Installed deployment', 'Resource tags'],
};
const ADVANCED_GROUPS = new Set(['network', 'storage', 'backup', 'monitoring', 'deployment']);
const groupAlias = (group: string) => ({maintenance: 'appearance', 'account-synchronization': 'sign-in', 'resource-tags': 'deployment', general: 'appearance', desktops: 'desktop-lifecycle', jobs: 'job-placement', notifications: 'email', 'users-and-sign-in': 'sign-in', costs: 'cost-collection'} as Record<string, string>)[group] ?? group;


export function legacySettingsGroup(path: string, tab: string | null): string {
    if (path === '/cluster/email-templates') return 'email';
    if (path === '/virtual-desktop/settings') return ({general: 'desktop-lifecycle', schedule: 'desktop-lifecycle', server: 'desktop-lifecycle', notifications: 'email', controller: 'network', broker: 'network', 'connection-gateway': 'network', backups: 'backup', 'cloudwatch-logs': 'monitoring'} as Record<string, string>)[tab ?? 'general'] ?? 'desktop-lifecycle';
    if (path === '/soca/settings') return tab === 'cloudwatch-logs' ? 'monitoring' : 'job-placement';
    return ({general: 'appearance', network: 'network', 'shared-storage': 'storage', 'identity-provider': 'sign-in', 'directory-service': 'sign-in', analytics: 'monitoring', metrics: 'monitoring', maintenance: 'appearance', 'account-reconciliation': 'sign-in', bedrock: 'ai-access', 'cloudwatch-logs': 'monitoring', ses: 'email', ec2: 'network', backups: 'backup', 'route-53': 'network', 'aws-account': 'deployment'} as Record<string, string>)[tab ?? 'general'] ?? 'appearance';
}

function section(source: SettingsSource, ...ids: string[]) {
    return ids.map(id => {const content = source.sections.find(item => item.id === id)?.content; return React.isValidElement(content) ? React.cloneElement(content, {key: id}) : content;});
}

export function SettingsGroups({cluster = EMPTY_SETTINGS, desktop = EMPTY_SETTINGS, scheduler = EMPTY_SETTINGS, pageProps, activeEditor, onEditingChange}: {activeEditor?: string | null; onEditingChange?: (editor: string | null) => void; cluster?: SettingsSource; desktop?: SettingsSource; scheduler?: SettingsSource; pageProps: any}) {
    const location = useLocation();
    const navigate = useNavigate();
    const [params] = useSearchParams();
    const [search, setSearch] = useState('');
    const [catalog, setCatalog] = useState<SettingDefinition[]>([]);
    const [values, setValues] = useState<Record<string, any>>({});
    const [errors, setErrors] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [localEditor, setLocalEditor] = useState<string | null>(null);
    const editingSection = activeEditor === undefined ? localEditor : activeEditor;
    const setEditingSection = (editor: string | null) => {setLocalEditor(editor); onEditingChange?.(editor);};
    const context = AppContext.get();
    const service = context.getClusterSettingsService();
    const canCluster = hasAccess(context, 'cluster-admin');
    const canDesktop = hasAccess(context, 'desktop-admin');
    const canJobs = hasAccess(context, 'jobs-admin');
    const permitted = (module: string) => module === 'virtual-desktop-controller' ? canDesktop : module === 'scheduler' ? canJobs : canCluster;
    const base = location.pathname.startsWith('/virtual-desktop') ? '/virtual-desktop/settings' : location.pathname.startsWith('/soca') ? '/soca/settings' : '/cluster/settings';
    const selected = location.pathname === '/cluster/email-templates' ? '' : groupAlias(location.pathname.slice(base.length + 1));
    const target = params.get('key');
    const moduleId = (module: string) => module === 'global-settings' ? module : service.getModuleId(module);
    const enabled = (module: string) => ['global-settings', 'cluster', 'cluster-manager'].includes(module) || service.isModuleEnabled(module);
    const destination = (group: string, key?: string) => {
        const next = new URLSearchParams(params); next.delete('tab'); next.delete('group'); next.delete('key');
        if (key) next.set('key', key);
        return `${base}/${group}${next.size ? `?${next}` : ''}`;
    };
    const backfillParams = new URLSearchParams(params);
    backfillParams.delete('tab'); backfillParams.delete('group'); backfillParams.delete('key'); backfillParams.set('operation', 'backfill-history');
    const backfillLink = `${base}/cost-collection?${backfillParams}`;
    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            try {
                const result = await context.client().clusterSettings().describeSettingsCatalog();
                const available = result.settings.filter(item => permitted(item.module) && enabled(item.module) && moduleId(item.module));
                if (cancelled) return;
                setCatalog(available.map(item => ({...item, group: groupAlias(item.group), ...(item.path === 'dcv_session.stopped_session_cleanup.email_template' ? {group: 'email', section: 'Notifications'} : {})})));
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
        if (canCluster || canDesktop || canJobs) void load();
        else setLoading(false);
        return () => {cancelled = true;};
        // The mounted page has a fixed authorization context and module set.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    useEffect(() => {
        if (!selected && (params.has('tab') || params.has('group') || location.pathname === '/cluster/email-templates')) {
            navigate(destination(groupAlias(params.get('group') ?? legacySettingsGroup(location.pathname, params.get('tab')))), {replace: true});
        }
        setEditingSection(null);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.pathname, location.search]);
    useEffect(() => {
        if (selected === 'cost-collection' && params.get('operation') === 'backfill-history') return;
        const setting = target && catalog.find(item => item.key === target);
        if (setting && selected !== setting.group) navigate(destination(setting.group, setting.key), {replace: true});
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [target, selected, catalog, location.search]);
    useEffect(() => {
        if (!loading && target) document.getElementById(settingAnchor(target))?.scrollIntoView?.({block: 'center'});
        if (!loading && selected === 'cost-collection' && params.get('operation') === 'backfill-history') {
            const card = document.getElementById('backfill-history');
            if (card) {card.tabIndex = -1; card.scrollIntoView?.({block: 'center'}); card.focus();}
        }
    }, [target, selected, loading, values, location.search]);
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
    const results = query ? catalog.filter(item => !isHidden(item) && `${item.key} ${item.label} ${item.description} ${item.section}`.toLowerCase().includes(query)) : [];
    const hiddenMatches = query ? catalog.filter(item => isHidden(item) && item.key.toLowerCase() === query) : [];
    const visible = groups.filter(([id, title, aliases]) => !query || `${title} ${aliases}`.toLowerCase().includes(query) || results.some(item => item.group === id));
    const groupName = (id: string) => groups.find(([group]) => group === id)?.[1] ?? id;
    const entries = catalog.filter(item => item.group === selected);
    const saved = (module: string, patch: Record<string, any>) => setValues(current => {
        const updated = structuredClone(current[module] ?? {});
        for (const [path, value] of Object.entries(patch)) {
            const parts = path.split('.'); const leaf = parts.pop()!;
            parts.reduce((node, part) => node[part] ??= {}, updated)[leaf] = value;
        }
        return {...current, [module]: updated};
    });
    const card = (title: string) => {
        const identity = `${selected}:${title}`;
        const collectionUnavailable = title === 'Collection' && (values.metrics?.provider !== 'dogstatsd' || /^(us-gov|cn)-/.test(values.cluster?.aws?.region ?? context.auth().getAwsRegion() ?? ''));
        const edit = {editing: editingSection === identity, editDisabled: collectionUnavailable || (editingSection !== null && editingSection !== identity),
            onEdit: () => setEditingSection(identity), onEditingEnd: () => setEditingSection(null)};
        if (title === 'History backfill') return <MetricsHistorySettings key={identity} active={selected === 'cost-collection'}/>;
        if (title === 'Email templates') return canCluster && <ExpandableSection key={identity} headerText={title} expanded={editingSection === identity} onChange={event => {if (!edit.editDisabled) setEditingSection(event.detail.expanded ? identity : null);}}>{editingSection === identity && <EmbeddedPageContext.Provider value={true}><EmailTemplates {...pageProps}/></EmbeddedPageContext.Provider>}</ExpandableSection>;
        if (title === 'Desktop schedule') return canDesktop && values['virtual-desktop-controller'] && <DesktopScheduleTable key={identity} {...edit} values={values['virtual-desktop-controller']} moduleId={moduleId('virtual-desktop-controller')!}
            timezone={values.cluster?.timezone ?? cluster.values.cluster?.timezone} onSaved={patch => saved('virtual-desktop-controller', {'dcv_session.schedule': patch.dcv_session.schedule, 'dcv_session.working_hours': patch.dcv_session.working_hours})}/>;
        if (title === 'Notifications') return <NotificationsTable key={identity} {...edit} values={values} settings={catalog.filter(item => !isHidden(item) && item.module in values && (item.section === title || item.path === 'dcv_session.stopped_session_cleanup.email_template'))} moduleId={module => moduleId(module)!} onSaved={saved}/>;
        if (title === 'Maintenance notice') return canCluster && section(cluster, 'maintenance');
        if (title === 'Account synchronization') return canCluster && <div key={identity}>{entries.filter(item => item.path.startsWith('accounts.reconcile.')).map(item => <span key={item.key} id={settingAnchor(item.key)}/>)}{section(cluster, 'account-reconciliation')}</div>;
        const settings = entries.filter(item => item.section === title && item.module in values && !isHidden(item)
            && !item.path.startsWith('accounts.reconcile.') && item.path !== 'dcv_session.stopped_session_cleanup.email_template');
        if (!settings.length && title !== 'Installed deployment' && title !== 'Amazon Bedrock') return null;
        const Component = title === 'Cost estimation' ? CostEstimationSettings : title === 'Storage measurement' ? StorageMeasurementSettings : CatalogSettingsSection;
        return <Component key={identity} title={title} settings={settings} values={{...cluster.values, ...values}} moduleId={module => moduleId(module)!} highlightedKey={target} {...edit} onSaved={saved}>
            {title === 'Installed deployment' && <KeyValuePairs columns={2} items={[
                {label: 'Region', value: cluster.values.cluster?.aws?.region ?? 'Not available'},
                {label: 'Version', value: service.getModuleInfo('cluster-manager')?.version ?? 'Not available'},
            ]}/>}
            {title === 'Cost header' && <p>Show a cached cost total for the signed-in user in the portal header.</p>}
            {title === 'Dashboard link' && <p>The dashboard opens embedded in the portal. Its URL must allow framing by this portal.</p>}
            {title === 'Collection' && <p>Cost Explorer collection requires the commercial AWS partition and the dogstatsd metrics provider. The collection switch applies after restart and does not control all personal-cost sources.</p>}
        </Component>;
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
                        type: 'section' as const, text: 'Advanced settings',
                        items: visible.filter(([id]) => ADVANCED_GROUPS.has(id)).map(([id, name]) => ({type: 'link' as const, text: name, href: `#${destination(id)}`}))
                    }] : [])
                ]}/>}
            {hiddenMatches.map(item => <Alert key={item.key} type="info">This setting is managed outside Settings. {item.description} <Link href={`#${destination(item.group, item.key)}`} onFollow={event => {event.preventDefault(); navigate(destination(item.group, item.key));}}>View setting explanation</Link></Alert>)}
            {!visible.length && !hiddenMatches.length && <p>No matching settings.</p>}
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
                <SpaceBetween size="l">
                    {target && catalog.some(item => item.key === target && isHidden(item)) && <Alert type="info">This setting is managed outside Settings. {catalog.find(item => item.key === target)?.description}</Alert>}
                    {selected === 'storage' && !loading && entries.length === 0 && <p>No file systems configured.</p>}
                    {selected === 'job-placement' && <Link href="#/soca/queues">Queue overrides in Manage jobs</Link>}
                    {selected === 'ai-access' && canCluster && <SpaceBetween direction="horizontal" size="m"><Link href="#/cluster/projects">Project access and budgets</Link><Link href="#/cluster/user-costs">Costs and usage</Link></SpaceBetween>}
                    {selected === 'monitoring' && (canCluster || canJobs) && <Link href={`#${backfillLink}`} onFollow={event => {event.preventDefault(); navigate(backfillLink);}}>History backfill in Costs</Link>}
                    {selected === 'deployment' && <Link href="#/cluster/status">Operations</Link>}
                    {(SETTINGS_CARDS[selected] ?? []).map(card)}
                </SpaceBetween>
            </section>}
        </div>
    </div>;
}

export default function PortalSettings(props: any) {
    const context = AppContext.get();
    const [activeEditor, setActiveEditor] = useState<string | null>(null);
    const renderScheduler = (cluster: SettingsSource, desktop: SettingsSource) => hasAccess(context, 'jobs-admin')
        ? <SchedulerSettings {...props} renderSections={(scheduler: SettingsSource) => <SettingsGroups activeEditor={activeEditor} onEditingChange={setActiveEditor} cluster={cluster} desktop={desktop} scheduler={scheduler} pageProps={props}/>}/>
        : <SettingsGroups activeEditor={activeEditor} onEditingChange={setActiveEditor} cluster={cluster} desktop={desktop} pageProps={props}/>;
    const renderDesktop = (cluster: SettingsSource) => hasAccess(context, 'desktop-admin')
        ? <DesktopSettings {...props} renderSections={(desktop: SettingsSource) => renderScheduler(cluster, desktop)}/>
        : renderScheduler(cluster, EMPTY_SETTINGS);
    if (!hasAccess(context, 'cluster-admin') && !hasAccess(context, 'desktop-admin') && !hasAccess(context, 'jobs-admin')) return <Alert type="warning">Settings requires administrator access.</Alert>;
    const content = hasAccess(context, 'cluster-admin') ? <ClusterSettings {...props} activeEditor={activeEditor} onEditingChange={setActiveEditor} renderSections={renderDesktop}/> : renderDesktop(EMPTY_SETTINGS);
    return <IdeaAppLayout {...props} ideaPageId="settings" header={<Header variant="h1">Settings</Header>} contentType="default" content={content}/>;
}
