import React, {useEffect, useRef, useState} from 'react';
import {Alert, Box, BreadcrumbGroup, Button, Container, Header, Link, ProgressBar, SpaceBetween, Table} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {StorageFolderUsage, StorageStudyProgress, StorageUsageResult} from '../../client/file-browser-client';

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

// "3.67M", not "3,670,000": the second number in "8,361 of about 3.67M" is only an estimate.
export function aboutCount(count: number): string {
    if (count < 10000) return count.toLocaleString();
    if (count < 1000000) return `${Math.round(count / 1000).toLocaleString()}K`;
    return `${(count / 1000000).toLocaleString(undefined, {maximumFractionDigits: 2})}M`;
}

export function useStorageUsage(folder?: string, refreshSignal = 0) {
    const [usage, setUsage] = useState<StorageUsageResult | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [attempt, setAttempt] = useState(0);
    const shownFolder = useRef(folder);
    const lastRefresh = useRef(refreshSignal);
    useEffect(() => {
        let active = true;
        let timer: ReturnType<typeof setTimeout>;
        // Keep the rows on screen while a refresh runs; only a different folder starts blank.
        if (shownFolder.current !== folder) setUsage(null);
        shownFolder.current = folder;
        setError(null);
        let refresh = lastRefresh.current !== refreshSignal;
        lastRefresh.current = refreshSignal;
        const fetchUsage = async () => {
            try {
                const request: {folder?: string, refresh?: boolean} = folder ? {folder} : {};
                if (refresh) request.refresh = true;
                refresh = false;
                const result = await AppContext.get().client().fileBrowser().getStorageUsage(request);
                if (!active) return;
                setUsage(result);
                if (result.state === 'computing') timer = setTimeout(fetchUsage, 2000);
                else if (result.progress?.running) timer = setTimeout(fetchUsage, 1000);
                if (result.state === 'error') setError(result.message || 'Storage usage could not be read.');
            } catch (e: any) {
                if (active) setError(e?.message || 'Storage usage could not be read.');
            }
        };
        fetchUsage();
        return () => { active = false; clearTimeout(timer); };
    }, [folder, attempt, refreshSignal]);
    return {usage, error, retry: () => setAttempt(value => value + 1)};
}

function StudyProgress({progress}: {progress: StorageStudyProgress}) {
    const [now, setNow] = useState(Date.now());
    useEffect(() => {
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, []);
    const seconds = Math.max(0, Math.round(now / 1000 - progress.started_at));
    return <div role="status" aria-label="Storage study progress">
        <SpaceBetween size="xs">
            <ProgressBar
                value={progress.total > 0 ? Math.min(100, progress.done / progress.total * 100) : 0}
                label={progress.step}
                additionalInfo={`${progress.done.toLocaleString()} of ${progress.total.toLocaleString()} folders checked · ${seconds} s so far`}/>
            <ol style={{margin: 0, paddingLeft: '1.25em'}}>
                {progress.steps.map(step => <li key={step.label} style={{fontWeight: step.state === 'running' ? 700 : 400, opacity: step.state === 'waiting' ? 0.6 : 1}}>
                    {step.label}{step.state === 'done' ? ' (done)' : ''}
                </li>)}
            </ol>
        </SpaceBetween>
    </div>;
}

function sizeCell(item: StorageFolderUsage): string {
    if (item.unreadable) return 'No access';
    if (item.measured === false) return 'Not measured yet';
    return `${item.partial ? 'At least ' : ''}${storageSize(item.bytes)}`;
}

function FolderTable(props: {items: StorageFolderUsage[], empty: string, showPath?: boolean, canOpen: boolean, fromAnalytics: boolean, onOpen: (path: string) => void}) {
    return <Table
        items={props.items}
        trackBy="path"
        empty={props.empty}
        sortingDisabled
        columnDefinitions={[
            {id: 'name', header: 'Folder', cell: item => props.canOpen && !item.unreadable && item.subfolders !== 0
                ? <Link onFollow={event => {event.preventDefault(); props.onOpen(item.path);}} href="#">{props.showPath ? item.path : item.name}</Link>
                : (props.showPath ? item.path : item.name)},
            {id: 'bytes', header: 'Size', cell: sizeCell},
            {id: 'files', header: 'Files', cell: item => item.unreadable || item.measured === false ? '' : item.files.toLocaleString()},
            {id: 'unchanged', header: props.fromAnalytics ? 'Unchanged 3+ months' : 'Unchanged 90 days', cell: item => item.unreadable || item.measured === false ? '' : storageSize(item.unchanged_90_days_bytes)},
            {id: 'newest', header: 'Last changed', cell: item => item.unreadable || item.measured === false ? '' : `${props.fromAnalytics && item.newest_mtime !== null ? 'About ' : ''}${storageAge(item.newest_mtime)}`},
            {id: 'open', header: 'Browse', cell: item => item.unreadable ? '' : <Link href={`#/home/file-browser?cwd=${encodeURIComponent(item.path)}`}>Open in Files</Link>}
        ]}
    />;
}

export function storageScanSentence(usage: StorageUsageResult): string | null {
    if (usage.source === 'analytics' || !usage.partial || !usage.total) return null;
    const scanned = (usage.scanned_files ?? usage.total.files).toLocaleString();
    const quotaFiles = usage.quotas?.reduce((sum, quota) => sum + quota.files, 0);
    return quotaFiles
        ? `Scanned ${scanned} of about ${aboutCount(quotaFiles)} files, then stopped at the time limit. Sizes are lower bounds.`
        : `Scanned ${scanned} files, then stopped at the time limit. Sizes are lower bounds.`;
}

export default function StorageUsage({compact = false, refreshSignal = 0}: {compact?: boolean, refreshSignal?: number}) {
    const [folder, setFolder] = useState<string | undefined>(undefined);
    const [refreshes, setRefreshes] = useState(0);
    const {usage, error, retry} = useStorageUsage(folder, refreshSignal + refreshes);
    const home = usage?.home;
    const relative = home && folder?.startsWith(`${home}/`) ? folder.slice(home.length + 1).split('/') : [];
    const crumbs = [{text: 'Home folder', href: home ?? ''}].concat(relative.map((text, index) => ({
        text, href: `${home}/${relative.slice(0, index + 1).join('/')}`
    })));
    const outsideHome = !!folder && !!home && !folder.startsWith(`${home}/`);
    const scanSentence = usage ? storageScanSentence(usage) : null;
    const quotaHasBytes = !!usage?.quotas?.length;
    const fromAnalytics = usage?.source === 'analytics';
    return <Container header={<Header variant="h2" actions={<Button onClick={() => setRefreshes(value => value + 1)} disabled={usage?.progress?.running}>Refresh</Button>}>Storage</Header>}>
        <SpaceBetween size="m">
            {error ? <Alert type="error" action={<Button onClick={retry}>Retry storage usage</Button>}>{error}</Alert> :
                (!usage || usage.state === 'computing') && !usage?.progress ? <Box>Computing storage usage...</Box> : null}
            {usage?.progress?.running && <StudyProgress progress={usage.progress}/>}
            {quotaHasBytes && <SpaceBetween size="xxs">
                {usage?.quotas?.map((quota, index) => <Box variant="strong" key={index}>
                    {storageSize(quota.used_bytes)} in {aboutCount(quota.files)} files on {[quota.volume, quota.qtree].filter(Boolean).join('/')}{quota.limit_bytes == null ? '' : ` (limit ${storageSize(quota.limit_bytes)})`}
                </Box>)}
                <Box>Your ONTAP quota counts everything you own on this volume, including outside your home folder.</Box>
            </SpaceBetween>}
            {usage?.state === 'ready' && !quotaHasBytes && usage.quota_status === 'unavailable' && <Box>{compact ? 'ONTAP quota: No data' : 'ONTAP quota: unavailable. No quota report is available for your account.'}</Box>}
            {usage?.outside_home_bytes !== undefined && usage.outside_home_bytes > 0 && !folder && <Box>
                {storageSize(usage.outside_home_bytes)} of that is outside your home folder. ONTAP totals what you own but can't say which folders hold it.
            </Box>}
            {!!usage?.unattributed_quota_bytes && !folder && <Box color="text-body-secondary" fontSize="body-s">
                ONTAP can't name the owner of another {storageSize(usage.unattributed_quota_bytes)} on this volume, so it isn't in anyone's total. Files you wrote from Windows could be part of it.
            </Box>}
            {usage?.state === 'unavailable' && <Alert type="info">{usage.message}</Alert>}
            {usage?.state === 'ready' && usage.total && <SpaceBetween size="s">
                {scanSentence && <Box>{scanSentence}</Box>}
                {usage.analytics_progress !== undefined && <Box>Storage analytics is being set up ({usage.analytics_progress}% done). Until it finishes, these sizes come from a short scan.</Box>}
                {folder && !outsideHome && <BreadcrumbGroup ariaLabel="Folder path" items={crumbs} onFollow={event => {
                    event.preventDefault();
                    setFolder(event.detail.href === home ? undefined : event.detail.href);
                }}/>}
                {outsideHome && <Button variant="link" onClick={() => setFolder(undefined)}>Back to your home folder</Button>}
                <Box>{folder ? folder : `Home folder ${usage.home}`}: {usage.partial ? 'at least ' : ''}{storageSize(usage.total.bytes)} in {usage.total.files.toLocaleString()} files. {storageSize(usage.total.unchanged_90_days_bytes)} has not changed in {fromAnalytics ? '3 months or more' : '90 days'}.</Box>
                <FolderTable items={usage.folders || []} empty="No folders here." canOpen fromAnalytics={fromAnalytics} onOpen={setFolder}/>
                {usage.files_here && usage.files_here.files > 0 && <Box>Files directly in this folder: {storageSize(usage.files_here.bytes)} in {usage.files_here.files.toLocaleString()} files.</Box>}
                {!folder && !!usage.largest?.length && <SpaceBetween size="xs">
                    <Header variant="h3">Biggest folders anywhere in your home</Header>
                    <FolderTable items={usage.largest} empty="" showPath canOpen fromAnalytics={fromAnalytics} onOpen={setFolder}/>
                </SpaceBetween>}
                {!folder && !!usage.shared?.length && <SpaceBetween size="xs">
                    <Header variant="h3" description="Sizes include everyone's files.">Shared folders</Header>
                    <FolderTable items={usage.shared} empty="" canOpen fromAnalytics={fromAnalytics} onOpen={setFolder}/>
                </SpaceBetween>}
                <Box color="text-body-secondary" fontSize="body-s">Measured {new Date(usage.measured_at! * 1000).toLocaleString()}. Symbolic links are not followed.</Box>
            </SpaceBetween>}
        </SpaceBetween>
    </Container>;
}
