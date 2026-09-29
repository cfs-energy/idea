import React, {useCallback, useEffect, useRef, useState} from 'react';
import {Alert, Box, Button, Header, Popover, SpaceBetween, StatusIndicator, Table} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {ClusterService, ListClusterServicesResult} from '../../client/data-model';
import moment from 'moment';
import {hasAccess} from '../../navigation/task-navigation';

export async function hasContainerControlPlane(): Promise<boolean> {
    const settings = AppContext.get().getClusterSettingsService();
    if (!settings.getModuleId('ecs')) return false;
    return Boolean((await settings.getModuleSettings('ecs'))?.container_enabled);
}

const time = (value?: string) => value ? new Date(value).toLocaleString() : 'Not available';
export const imageTag = (image: string) => image.includes('@sha256:') ? 'digest…' : image.split('/').pop()!.split(':')[1] || 'latest';
export const serviceRole = (name: string): string => {
    const context = AppContext.get();
    const prefix = `${context.auth().getClusterName()}-`;
    const identity = name.startsWith(prefix) ? name.slice(prefix.length) : name;
    const settings = context.getClusterSettingsService();
    for (const [module, role] of [['cluster-manager', 'cluster manager'], ['scheduler', 'scheduler'], ['bastion-host', 'bastion']]) {
        if (identity === settings.getModuleId(module)) return role;
    }
    const desktop = settings.getModuleId('virtual-desktop-controller');
    for (const component of ['controller', 'broker', 'gateway']) {
        if (desktop && identity === `${desktop}-${component}`) return component;
    }
    // Component identities used by older deployments; compare whole identities.
    const legacy: Record<string, string> = {'virtual-desktop-controller': 'controller', 'dcv-broker': 'broker', 'dcv-connection-gateway': 'gateway', bastion: 'bastion'};
    return legacy[identity] ?? identity;
};
const statusType = (state?: string): 'success' | 'in-progress' | 'error' | 'info' => {
    if (state === 'COMPLETED') return 'success';
    if (state === 'IN_PROGRESS') return 'in-progress';
    if (state === 'FAILED') return 'error';
    return 'info';
};
const statusLabel = (state?: string) => ({COMPLETED: 'Completed', IN_PROGRESS: 'In progress', FAILED: 'Failed'}[state ?? ''] ?? 'Not available');
const healthType = (health?: string): 'success' | 'error' | 'info' => health === 'HEALTHY' ? 'success' : health === 'UNHEALTHY' ? 'error' : 'info';

const ServiceName = ({row}: {row: ClusterService}) => <Popover dismissButton={false} position="top" triggerType="text" content={row.name}>{serviceRole(row.name)}</Popover>;
const Images = ({images = []}: {images?: string[]}) => {
    const tags = Array.from(new Set(images.map(imageTag)));
    if (!tags.length) return <>Not available</>;
    const label = tags.length === 1 ? tags[0] : `${tags.length} tags`;
    return <Popover dismissButton={false} position="top" triggerType="text" content={<SpaceBetween size="xxs">{images.map((image, index) => <Box key={`${image}-${index}`}>{image}</Box>)}</SpaceBetween>}>{label}</Popover>;
};
const Tasks = ({row}: {row: ClusterService}) => row.tasks?.length ? <Popover dismissButton={false} position="top" size="large" triggerType="text"
    content={<SpaceBetween size="s">{row.tasks.map(task => <div key={task.task_id}><Box fontWeight="bold">{task.task_id}</Box><Box>Started {time(task.started_at)}</Box><StatusIndicator type={healthType(task.health)}>{task.health || 'Unknown'}</StatusIndicator></div>)}</SpaceBetween>}>
    {`${row.tasks.length} ${row.tasks.length === 1 ? 'task' : 'tasks'}`}
</Popover> : <>No running tasks</>;

const isDatadog = (row: ClusterService) => /(?:^datadog-service$|(?:^|[-_])datadogservice(?:service)?[a-f0-9]{8}-[a-z0-9]+$)/i.test(row.name)
    || (Boolean(row.images?.length) && (row.images ?? []).every(image => /^(?:(?:public\.ecr\.aws|gcr\.io|docker\.io|index\.docker\.io)\/)?datadog\/agent(?=[:@]|$)/i.test(image)));

export default function ClusterServices(_props: {desktop?: boolean}) {
    const [result, setResult] = useState<ListClusterServicesResult>({listing: [], errors: []});
    const [loading, setLoading] = useState(true);
    const generation = useRef(0);
    const refresh = useCallback(async () => {
        const current = ++generation.current;
        setLoading(true);
        try {
            const response = await AppContext.get().client().clusterSettings().listClusterServices({});
            if (current === generation.current) setResult(response);
        } catch {
            if (current === generation.current) setResult({listing: [], errors: ['Could not load services. Refresh to try again.']});
        } finally { if (current === generation.current) setLoading(false); }
    }, []);
    useEffect(() => { void refresh(); return () => { ++generation.current; }; }, [refresh]);
    const context = AppContext.get();
    const groups = [
        {title: 'Control plane', access: hasAccess(context, 'cluster-admin'), rows: [] as ClusterService[]},
        {title: 'Desktop services', access: hasAccess(context, 'desktop-admin'), rows: [] as ClusterService[]},
        {title: 'Job service', access: hasAccess(context, 'jobs-admin'), rows: [] as ClusterService[]},
    ];
    for (const row of result.listing ?? []) {
        if (isDatadog(row)) continue;
        const role = serviceRole(row.name);
        const group = ['broker', 'controller', 'gateway'].includes(role) ? 1 : role === 'scheduler' ? 2 : 0;
        groups[group].rows.push(row);
    }
    return <SpaceBetween size="m">
        {(result.errors ?? []).map((error, index) => <Alert type="error" key={index}>{error}</Alert>)}
        <Header actions={<Button onClick={refresh} disabled={loading} iconName="refresh">Refresh</Button>}>Live services</Header>
        {groups.filter(group => group.access).map(group => <Table<ClusterService> key={group.title}
            items={group.rows.sort((a, b) => a.name.localeCompare(b.name))} loading={loading} loadingText="Loading services" trackBy="name"
            wrapLines={false}
            header={<Header variant="h2">{group.title}</Header>}
            empty="No services found."
            columnDefinitions={[
                {id: 'name', header: 'Service', cell: row => <ServiceName row={row}/>, width: 180},
                {id: 'desired', header: 'Desired', cell: row => row.desired, width: 90},
                {id: 'running', header: 'Running', cell: row => row.running, width: 90},
                {id: 'pending', header: 'Pending', cell: row => row.pending, width: 90},
                {id: 'images', header: 'Image tag', cell: row => <Images images={row.images}/>, width: 140},
                {id: 'rollout', header: 'Rollout', cell: row => <SpaceBetween size="xxs"><StatusIndicator type={statusType(row.rollout_state)}>{statusLabel(row.rollout_state)}</StatusIndicator><Box variant="small">{row.updated_at ? moment(row.updated_at).fromNow() : 'Update time unavailable'}</Box></SpaceBetween>, width: 180},
                {id: 'tasks', header: 'Running tasks', cell: row => <Tasks row={row}/>, width: 140},
            ]}/>)}
    </SpaceBetween>;
}
