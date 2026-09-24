import React, {useCallback, useEffect, useState} from 'react';
import {Alert, Box, Button, Header, Popover, SpaceBetween, StatusIndicator, Table} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {ClusterService, ListClusterServicesResult} from '../../client/data-model';
import moment from 'moment';

export async function hasContainerControlPlane(): Promise<boolean> {
    const settings = AppContext.get().getClusterSettingsService();
    if (!settings.getModuleId('ecs')) return false;
    return Boolean((await settings.getModuleSettings('ecs'))?.cluster_name);
}

const time = (value?: string) => value ? new Date(value).toLocaleString() : 'Not available';
export const imageTag = (image: string) => image.includes('@sha256:') ? 'digest…' : image.split('/').pop()!.split(':')[1] || 'latest';
export const serviceRole = (name: string): string => {
    const value = name.toLowerCase();
    if (value.includes('broker')) return 'broker';
    if (value.includes('controller')) return 'controller';
    if (value.includes('gateway')) return 'gateway';
    if (value.includes('bastion')) return 'bastion';
    if (value.includes('cluster-manager') || value.includes('cluster_manager')) return 'cluster manager';
    if (value.includes('scheduler')) return 'scheduler';
    if (value.includes('monitor') || value.includes('agent')) return 'monitoring agent';
    return name;
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

export default function ClusterServices({desktop}: {desktop: boolean}) {
    const [result, setResult] = useState<ListClusterServicesResult>({listing: [], errors: []});
    const [loading, setLoading] = useState(true);
    const refresh = useCallback(async () => {
        setLoading(true);
        try {
            setResult(await AppContext.get().client().clusterSettings().listClusterServices({}));
        } catch {
            setResult({listing: [], errors: ['Could not load services. Refresh to try again.']});
        } finally { setLoading(false); }
    }, []);
    useEffect(() => { void refresh(); }, [refresh]);
    const priority = (row: ClusterService) => (desktop ? /desktop|vdc|dcv|broker|gateway/i : /scheduler/i).test(row.name) ? 0 : 1;
    const rows = [...(result.listing ?? [])].sort((a, b) => priority(a) - priority(b) || a.name.localeCompare(b.name));
    return <SpaceBetween size="m">
        {(result.errors ?? []).map((error, index) => <Alert type="error" key={index}>{error}</Alert>)}
        <Table<ClusterService> items={rows} loading={loading} loadingText="Loading services" trackBy="name"
            wrapLines={false}
            header={<Header actions={<Button onClick={refresh} disabled={loading} iconName="refresh">Refresh</Button>}>Live control-plane services</Header>}
            empty="No services found."
            columnDefinitions={[
                {id: 'name', header: 'Service', cell: row => <ServiceName row={row}/>, width: 180},
                {id: 'desired', header: 'Desired', cell: row => row.desired, width: 90},
                {id: 'running', header: 'Running', cell: row => row.running, width: 90},
                {id: 'pending', header: 'Pending', cell: row => row.pending, width: 90},
                {id: 'images', header: 'Image tag', cell: row => <Images images={row.images}/>, width: 140},
                {id: 'rollout', header: 'Rollout', cell: row => <SpaceBetween size="xxs"><StatusIndicator type={statusType(row.rollout_state)}>{statusLabel(row.rollout_state)}</StatusIndicator><Box variant="small">{row.updated_at ? moment(row.updated_at).fromNow() : 'Update time unavailable'}</Box></SpaceBetween>, width: 180},
                {id: 'tasks', header: 'Running tasks', cell: row => <Tasks row={row}/>, width: 140},
            ]}/>
    </SpaceBetween>;
}
