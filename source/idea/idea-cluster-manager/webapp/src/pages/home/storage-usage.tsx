import React, {useEffect, useState} from 'react';
import {Alert, Badge, Box, Button, Container, Header, Link, SpaceBetween, Table} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {StorageUsageResult} from '../../client/file-browser-client';

export function storageSize(bytes: number): string {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit++;
    }
    return `${value.toLocaleString(undefined, {maximumFractionDigits: 1})} ${units[unit]}`;
}

export function storageAge(timestamp: number | null): string {
    if (timestamp === null) return 'No files';
    const days = Math.max(0, Math.floor((Date.now() / 1000 - timestamp) / 86400));
    return `${days} ${days === 1 ? 'day' : 'days'} ago (${new Date(timestamp * 1000).toLocaleDateString()})`;
}

export function useStorageUsage(folder?: string) {
    const [usage, setUsage] = useState<StorageUsageResult | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [attempt, setAttempt] = useState(0);
    useEffect(() => {
        let active = true;
        let timer: ReturnType<typeof setTimeout>;
        setUsage(null);
        setError(null);
        const fetchUsage = async () => {
            try {
                const result = await AppContext.get().client().fileBrowser().getStorageUsage(folder ? {folder} : {});
                if (!active) return;
                setUsage(result);
                if (result.state === 'computing') timer = setTimeout(fetchUsage, 2000);
                if (result.state === 'error') setError(result.message || 'Storage usage could not be read.');
            } catch (e: any) {
                if (active) setError(e?.message || 'Storage usage could not be read.');
            }
        };
        fetchUsage();
        return () => { active = false; clearTimeout(timer); };
    }, [folder, attempt]);
    return {usage, error, retry: () => setAttempt(value => value + 1)};
}

export default function StorageUsage({compact = false}: {compact?: boolean}) {
    const {usage, error, retry} = useStorageUsage();
    return <Container header={<Header variant="h2">Storage</Header>}>
        <SpaceBetween size="m">
            {error ? <Alert type="error" action={<Button onClick={retry}>Retry storage usage</Button>}>{error}</Alert> :
                (!usage || usage.state === 'computing') ? <Box>Computing storage usage...</Box> : null}
            {usage?.state === 'ready' && usage.total && <SpaceBetween size="s">
                {usage.partial && (compact ? <Badge>Partial</Badge> : <Alert type="warning">Partial storage usage: the scan reached a limit or could not read some data. Sizes and counts are lower bounds.</Alert>)}
                <Box>Home directory: {usage.home}</Box>
                <Box>Total: {storageSize(usage.total.bytes)} in {usage.total.files.toLocaleString()} files</Box>
                <Box>{compact ? `Unchanged for 90 days: ${storageSize(usage.total.unchanged_90_days_bytes)}` : `${storageSize(usage.total.unchanged_90_days_bytes)} of ${storageSize(usage.total.bytes)} has not changed in 90 days.`}</Box>
                <Box>Measured {new Date(usage.measured_at! * 1000).toLocaleString()}{compact ? '' : '. Usage is cached for one hour. Symbolic links are excluded.'}</Box>
                <Table
                    items={usage.folders || []}
                    trackBy="path"
                    empty="No folders in your home directory."
                    columnDefinitions={[
                        {id: 'name', header: 'Folder', cell: item => item.name},
                        {id: 'bytes', header: 'Size', cell: item => `${item.partial ? 'At least ' : ''}${storageSize(item.bytes)}`},
                        {id: 'files', header: 'Files', cell: item => item.files.toLocaleString()},
                        {id: 'newest', header: 'Last changed', cell: item => storageAge(item.newest_mtime)},
                        {id: 'oldest', header: 'Oldest file', cell: item => storageAge(item.oldest_mtime)},
                        {id: 'open', header: 'Browse', cell: item => <Link href={`#/home/file-browser?cwd=${encodeURIComponent(item.path)}`}>Open in Files</Link>}
                    ]}
                />
            </SpaceBetween>}
            {usage?.quotas?.map((quota, index) => <Box key={index}>
                ONTAP quota ({[quota.target, quota.volume, quota.qtree].filter(Boolean).join('/')}): {storageSize(quota.used_bytes)} used, {quota.files.toLocaleString()} files, limit {quota.limit_bytes == null ? 'unlimited' : storageSize(quota.limit_bytes)}{compact ? ' · Measured ' : '. Reported '}{new Date(quota.measured_at * 1000).toLocaleString()}{compact ? '' : '.'}
            </Box>)}
            {usage?.state === 'ready' && usage.quota_status === 'unavailable' && <Box>{compact ? 'ONTAP quota: No data' : 'ONTAP quota: unavailable. No quota report is available for your account.'}</Box>}
        </SpaceBetween>
    </Container>;
}
