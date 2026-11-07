import React, {useCallback, useEffect, useRef, useState} from 'react';
import {Alert, Box, Button, ColumnLayout, Container, FormField, Header, Input, KeyValuePairs, Link, Modal, Pagination, ProgressBar, SegmentedControl, Select, SpaceBetween, StatusIndicator, Table} from '@cloudscape-design/components';
import IdeaAppLayout from '../../components/app-layout';
import {AppContext} from '../../common';
import {Constants} from '../../common/constants';
import {GetScratchCleanupStatusResult, ScratchCleanupPreviewSummary, ScratchCleanupRow, ScratchCleanupRun} from '../../client/data-model';

const PAGE_SIZE = 25;
const WEEKDAYS = ['Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays', 'Sundays'];
const ACTION_LABELS: Record<string, string> = {
    schedule: 'Would schedule',
    scheduled: 'Already scheduled',
    keep: 'Kept',
};
const AUDIT_LABELS: Record<string, string> = {started: 'Not finished, can resume', removed: 'Removed', failed: 'Stopped, folder changed', interrupted: 'Interrupted, folder gone', failed_gone: 'Stopped, folder later gone'};
const RUN_STATUS: Record<string, {type: 'success' | 'info' | 'warning' | 'error' | 'in-progress' | 'stopped'; text: string}> = {
    running: {type: 'in-progress', text: 'Running'},
    finished: {type: 'success', text: 'Finished'},
    nothing_to_do: {type: 'info', text: 'Nothing to do'},
    dry_run: {type: 'info', text: 'Dry run'},
    stopped: {type: 'warning', text: 'Stopped'},
    failed: {type: 'error', text: 'Failed'},
    interrupted: {type: 'warning', text: 'Interrupted'},
    preview: {type: 'info', text: 'Preview'},
};

export const formatBytes = (value?: number) => {
    if (value == null) return '-';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let size = value;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {size /= 1024; unit += 1;}
    return `${unit === 0 ? size : size.toFixed(1)} ${units[unit]}`;
};
const when = (value?: string) => value ? new Date(value).toLocaleString() : '-';
const client = () => AppContext.get().client().schedulerAdmin();

function message(error: any): string {
    return error?.message ?? 'The request failed.';
}

export function modeText(status: GetScratchCleanupStatusResult): {type: 'info' | 'warning' | 'success'; text: string} {
    const settings = status.settings;
    if (settings?.kill_switch) return {type: 'warning', text: 'All removals are stopped. Nothing is removed until this is allowed again.'};
    if (!settings?.enabled) return {type: 'info', text: 'Automatic cleanup is off. Nothing is removed. Jobs that finish now get no date until a backfill.'};
    if (settings.dry_run) return {type: 'info', text: 'Dry run. Checks report what they would remove. Nothing is removed, nobody is told, and jobs that finish now get no date until a backfill.'};
    return {type: 'success', text: `On. A job's numbered folder goes ${settings.success_retention_days} days after it ends, or ${settings.failed_retention_days} if it failed, once its owner has had two weekly notices and every check still passes.`};
}

function PreviewRows({preview}: {preview: ScratchCleanupPreviewSummary}) {
    const counts = preview.action_counts ?? {};
    const [action, setAction] = useState(counts.schedule ? 'schedule' : 'keep');
    const [rows, setRows] = useState<ScratchCleanupRow[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    useEffect(() => {
        let current = true;
        setLoading(true);
        client().listScratchCleanupPreview({preview_id: preview.preview_id, action, paginator: {start: (page - 1) * PAGE_SIZE, page_size: PAGE_SIZE}}).then(result => {
            if (!current) return;
            setRows(result.listing ?? []);
            setTotal(result.paginator?.total ?? 0);
            setError('');
        }).catch(e => {if (current) setError(message(e));}).finally(() => {if (current) setLoading(false);});
        return () => {current = false;};
    }, [preview.preview_id, action, page]);
    return <SpaceBetween size="s">
        {error && <Alert type="error">{error}</Alert>}
        <SegmentedControl
            label="Folders to show"
            selectedId={action}
            onChange={event => {setAction(event.detail.selectedId); setPage(1);}}
            options={Object.keys(ACTION_LABELS).map(id => ({id, text: `${ACTION_LABELS[id]} (${counts[id] ?? 0})`}))}/>
        <Table
            variant="embedded"
            loading={loading}
            loadingText="Loading folders"
            items={rows}
            trackBy="path"
            empty={<Box textAlign="center">No folders here.</Box>}
            pagination={<Pagination currentPageIndex={page} pagesCount={Math.max(1, Math.ceil(total / PAGE_SIZE))} onChange={event => setPage(event.detail.currentPageIndex)}/>}
            columnDefinitions={[
                {id: 'path', header: 'Folder', cell: row => row.path},
                {id: 'owner', header: 'Owner', cell: row => row.owner ?? '-'},
                {id: 'job', header: 'Job', cell: row => row.job_id ?? '-'},
                {id: 'ended', header: 'Job ended', cell: row => when(row.job_end_time)},
                {id: 'size', header: 'Size', cell: row => row.size_bytes ? formatBytes(row.size_bytes) : '-'},
                {id: 'why', header: 'Why', cell: row => row.reason_text},
            ]}/>
    </SpaceBetween>;
}

function RunNow({preview, onDone, onDismiss}: {preview: ScratchCleanupPreviewSummary; onDone: () => void; onDismiss: () => void}) {
    const [typed, setTyped] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const counts = preview.action_counts ?? {};
    const bytes = preview.action_bytes ?? {};
    const run = async () => {
        setBusy(true);
        try {
            await client().backfillScratchCleanup({preview_id: preview.preview_id, confirm_phrase: typed});
            onDone();
        } catch (e) {
            setError(message(e));
            setBusy(false);
        }
    };
    return <Modal
        visible
        onDismiss={() => !busy && onDismiss()}
        header="Run the backfill?"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
            <Button disabled={busy} onClick={onDismiss}>Cancel</Button>
            <Button variant="primary" loading={busy} disabled={typed !== preview.confirm_phrase} onClick={run}>Run backfill</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="s">
            {error && <Alert type="error">{error}</Alert>}
            <div>{counts.schedule ?? 0} folders ({formatBytes(bytes.schedule ?? 0)}) get a removal date from when their job ended. Nothing goes now: owners get two weekly notices first, then the folders are permanently removed.</div>
            <div>Each folder is checked again just before it goes. Anything that changed stays.</div>
            <FormField label={`Type ${preview.confirm_phrase} to confirm`}>
                <Input ariaLabel="Confirmation" value={typed} onChange={event => setTyped(event.detail.value)}/>
            </FormField>
        </SpaceBetween>
    </Modal>;
}

function Upcoming({refresh}: {refresh: number}) {
    const [rows, setRows] = useState<ScratchCleanupRow[]>([]);
    const [token, setToken] = useState<string | undefined>();
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const load = useCallback(async (cursor?: string) => {
        setLoading(true);
        try {
            const result = await client().listScratchCleanupUpcoming({paginator: {page_size: PAGE_SIZE, cursor}});
            setRows(previous => cursor ? [...previous, ...(result.listing ?? [])] : (result.listing ?? []));
            setToken(result.next_token);
            setError('');
        } catch (e) {
            setError(message(e));
        } finally {
            setLoading(false);
        }
    }, []);
    useEffect(() => {load();}, [load, refresh]);
    return <Container header={<Header variant="h2" description="Set when a job finishes. A date moves if the owner has not had both notices. Every check runs again on the date, and a folder a check keeps stays here with its reason.">Upcoming removals</Header>}>
        <SpaceBetween size="s">
            {error && <Alert type="error">{error}</Alert>}
            <Table
                variant="embedded"
                loading={loading}
                loadingText="Loading upcoming removals"
                items={rows}
                trackBy={row => `${row.owner}${row.job_id}${row.job_uid}`}
                empty={<Box textAlign="center">No removals are scheduled.</Box>}
                columnDefinitions={[
                    {id: 'owner', header: 'Owner', cell: row => row.owner ?? '-'},
                    {id: 'job', header: 'Job', cell: row => row.job_id ?? '-'},
                    {id: 'ended', header: 'Job ended', cell: row => when(row.job_end_time)},
                    {id: 'date', header: 'Removal date', cell: row => when(row.remove_after)},
                    {id: 'result', header: 'Job result', cell: row => row.outcome === 'failed' ? 'Failed' : 'Succeeded'},
                    {id: 'told', header: 'Owner told', cell: row => row.final_notice_at ? 'Twice' : row.first_notice_at ? 'Once' : 'Not yet'},
                    {id: 'state', header: 'State', cell: row => row.reason_text ? `Kept: ${row.reason_text}` : 'Scheduled'},
                ]}/>
            {token && <Button disabled={loading} onClick={() => load(token)}>Show more</Button>}
        </SpaceBetween>
    </Container>;
}

function RemovalLog({refresh}: {refresh: number}) {
    const [rows, setRows] = useState<ScratchCleanupRow[]>([]);
    const [token, setToken] = useState<string | undefined>();
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const load = useCallback(async (cursor?: string) => {
        setLoading(true);
        try {
            const result = await client().listScratchCleanupAudit({paginator: {page_size: 50, cursor}});
            setRows(previous => cursor ? [...previous, ...(result.listing ?? [])] : (result.listing ?? []));
            setToken(result.next_token);
            setError('');
        } catch (e) {
            setError(message(e));
        } finally {
            setLoading(false);
        }
    }, []);
    useEffect(() => {load();}, [load, refresh]);
    return <Container header={<Header variant="h2" description="Every folder cleanup has removed, kept indefinitely.">Removal log</Header>}>
        <SpaceBetween size="s">
            {error && <Alert type="error">{error}</Alert>}
            <Table
                variant="embedded"
                loading={loading}
                loadingText="Loading the removal log"
                items={rows}
                trackBy={row => `${row.path}${row.run_id}`}
                empty={<Box textAlign="center">Nothing has been removed.</Box>}
                columnDefinitions={[
                    {id: 'path', header: 'Folder', cell: row => row.path},
                    {id: 'owner', header: 'Owner', cell: row => row.owner ?? '-'},
                    {id: 'job', header: 'Job', cell: row => row.job_id ?? '-'},
                    {id: 'ended', header: 'Job ended', cell: row => when(row.job_end_time)},
                    {id: 'size', header: 'Size', cell: row => formatBytes(row.size_bytes)},
                    {id: 'started', header: 'Started', cell: row => when(row.started_at)},
                    {id: 'by', header: 'By', cell: row => row.requested_by ?? '-'},
                    {id: 'entries', header: 'Items removed', cell: row => row.removed_entries ?? '-'},
                    {id: 'finished', header: 'Finished', cell: row => when(row.finished_at)},
                    {id: 'result', header: 'Result', cell: row => AUDIT_LABELS[row.action ?? ''] ?? row.action},
                    {id: 'why', header: 'Why', cell: row => row.reason_text},
                ]}/>
            {token && <Button disabled={loading} onClick={() => load(token)}>Show more</Button>}
        </SpaceBetween>
    </Container>;
}

export function ScratchCleanupContent() {
    const [status, setStatus] = useState<GetScratchCleanupStatusResult | null>(null);
    const [error, setError] = useState('');
    const [selected, setSelected] = useState<string | undefined>();
    const [confirming, setConfirming] = useState(false);
    const [starting, setStarting] = useState(false);
    const [saving, setSaving] = useState(false);
    const [refresh, setRefresh] = useState(0);
    const wasWorking = useRef(false);
    const load = useCallback(async () => {
        try {
            const next = await client().getScratchCleanupStatus({});
            setStatus(next);
            setError('');
            const working = Boolean(next.progress);
            if (wasWorking.current && !working) setRefresh(value => value + 1);
            wasWorking.current = working;
        } catch (e) {
            setError(message(e));
        }
    }, []);
    useEffect(() => {load();}, [load]);
    const working = Boolean(status?.progress);
    useEffect(() => {
        const timer = setInterval(load, working ? 3000 : 30000);
        return () => clearInterval(timer);
    }, [load, working]);

    if (!status && !error) return <StatusIndicator type="loading">Loading</StatusIndicator>;
    if (!status) return <Alert type="error">{error}</Alert>;
    if (!status.available) {
        return <Container header={<Header variant="h2">Scratch cleanup</Header>}>
            {status.message ?? 'This cluster has no Lustre scratch file system, so there is nothing to clean.'}
        </Container>;
    }

    const fileSystems = status.file_systems ?? [];
    const current = fileSystems.find(item => item.name === selected) ?? fileSystems[0];
    const preview = (status.previews ?? []).find(item => item.file_system === current.name);
    const progress = status.progress;
    const mode = modeText(status);
    const settings = status.settings ?? {};
    const runs = (status.runs ?? []).filter((run: ScratchCleanupRun) => run.file_system === current.name);
    const lastRun = runs.find(run => run.status !== 'preview');
    const isAdmin = AppContext.get().auth().isAdmin();
    const moduleId = () => AppContext.get().getClusterSettingsService().getModuleId(Constants.MODULE_SCHEDULER) || Constants.MODULE_SCHEDULER;
    const setKillSwitch = async (value: boolean) => {
        setSaving(true);
        try {
            await AppContext.get().client().clusterSettings().updateModuleSettings({module_id: moduleId(), settings: {scratch_cleanup: {kill_switch: value}}});
            await load();
        } catch (e) {
            setError(message(e));
        } finally {
            setSaving(false);
        }
    };
    const startPreview = async () => {
        setStarting(true);
        try {
            await client().previewScratchCleanup({file_system: current.name});
            await load();
        } catch (e) {
            setError(message(e));
        } finally {
            setStarting(false);
        }
    };

    return <SpaceBetween size="l">
        <Header
            variant="h1"
            description="Removes a job's numbered scratch folder a set time after it ends. Named folders and owners' own folders stay. A folder goes only when every check still passes on that date: the job is over, its owner has no active job, and everything in the folder dates from that job's run."
            actions={settings.kill_switch
                ? <Button loading={saving} disabled={!AppContext.get().auth().isAdmin()} onClick={() => setKillSwitch(false)}>Allow removals</Button>
                : <Button loading={saving} onClick={() => setKillSwitch(true)}>Stop all removals</Button>}>
            Scratch cleanup
        </Header>
        {error && <Alert type="error">{error}</Alert>}
        {status.message && <Alert type="warning">{status.message}</Alert>}
        {settings.problem && <Alert type="error" header="These settings stop cleanup">{settings.problem}</Alert>}
        <Alert type={mode.type}>{mode.text}</Alert>
        {['stopped', 'failed'].includes(lastRun?.status ?? '') && <Alert type="error" header="The last run stopped">{lastRun?.message}</Alert>}
        {fileSystems.length > 1 && <FormField label="File system">
            <Select
                selectedOption={{value: current.name, label: current.title || current.name}}
                options={fileSystems.map(item => ({value: item.name, label: item.title || item.name}))}
                onChange={event => setSelected(event.detail.selectedOption.value)}/>
        </FormField>}
        <Container header={<Header variant="h2" description={current.mount_dir}>{current.title || current.name}</Header>}>
            <SpaceBetween size="m">
                {current.problem && <Alert type="warning">{current.problem}</Alert>}
                {current.used_percent != null
                    ? <ProgressBar
                        label="Used"
                        value={current.used_percent}
                        description={`${formatBytes(current.used_bytes)} of ${formatBytes(current.capacity_bytes)}, read ${when(current.usage_read_at)}`}/>
                    : !current.problem && <Box>Usage has not been read yet. It updates every minute.</Box>}
                {!current.selected && <Alert type="info">This file system is not selected for cleanup, so nothing goes from it. Add {current.name} to File systems to clean in settings.</Alert>}
                <KeyValuePairs columns={3} items={[
                    {label: 'Removed after job ends', value: `${settings.success_retention_days} days, or ${settings.failed_retention_days} if it failed`},
                    {label: 'While the owner has an active job', value: 'All their folders stay'},
                    {label: 'On the record', value: `${status.scheduled_folders ?? 0} folders, ${status.failed_folders ?? 0} from failed jobs`},
                    {label: 'Notice to owners', value: `Two weekly digests, ${WEEKDAYS[settings.digest_weekday ?? 0]}`},
                    {label: 'Most per hourly check', value: `${settings.max_folders_per_run} folders, ${settings.max_run_bytes_percent}% of capacity`},
                ]}/>
                <Link href="#/soca/settings/job-placement">Change settings</Link>
            </SpaceBetween>
        </Container>
        <Container header={<Header
            variant="h2"
            description="For folders from before cleanup was on. Run once: numbered folders of finished jobs get a date from when their job ended, and after that only finishing jobs add to the record. Previewing removes nothing and tells nobody."
            actions={<SpaceBetween direction="horizontal" size="xs">
                <Button disabled={working || !isAdmin} loading={starting} onClick={startPreview}>Preview</Button>
                <Button
                    variant="primary"
                    disabled={working || !isAdmin || !preview?.confirm_phrase || !settings.enabled || settings.dry_run || settings.kill_switch || !current.selected}
                    onClick={() => setConfirming(true)}>
                    Run backfill
                </Button>
            </SpaceBetween>}>
            Backfill
        </Header>}>
            <SpaceBetween size="m">
                {progress && <ProgressBar
                    label={`${progress.operation === 'preview' ? 'Previewing' : progress.operation === 'backfill' ? 'Backfilling' : 'Running'}: ${progress.phase ?? ''}`}
                    value={progress.total ? Math.round(100 * (progress.done ?? 0) / progress.total) : undefined}
                    variant="standalone"
                    additionalInfo={progress.current ?? (progress.total ? `${progress.done ?? 0} of ${progress.total}` : `${progress.done ?? 0} job folders found`)}/>}
                {!isAdmin && <Alert type="info">Only a cluster administrator can run a backfill.</Alert>}
                {!preview && !progress && <Box>No preview yet. A preview reads folder metadata only, and can take several minutes on a full file system.</Box>}
                {preview && <SpaceBetween size="m">
                    {preview.problem && <Alert type="error" header="This preview cannot run yet">{preview.problem}</Alert>}
                    <Box variant="small">Made {when(preview.created_at)}. Good until {when(preview.expires_at)}.</Box>
                    {(preview.truncated_rows ?? 0) > 0 && <Alert type="info">{preview.truncated_rows} kept folders are counted but not listed.</Alert>}
                    {(preview.other_kept ?? 0) > 0 && <Box>{preview.other_kept} other items are not job folders. They stay, and their names are not shown.</Box>}
                    <ColumnLayout columns={2} variant="text-grid">
                        {['schedule', 'scheduled'].map(action => <div key={action}>
                            <Box variant="awsui-key-label">{ACTION_LABELS[action]}</Box>
                            <div>{preview.action_counts?.[action] ?? 0} folders, {formatBytes(preview.action_bytes?.[action] ?? 0)}</div>
                        </div>)}
                    </ColumnLayout>
                    <Table
                        variant="embedded"
                        header={<Header variant="h3">Why folders stay</Header>}
                        items={Object.entries(preview.reason_counts ?? {}).filter(([reason]) => reason !== 'eligible').map(([reason, count]) => ({reason, count}))}
                        trackBy="reason"
                        empty={<Box textAlign="center">No folder was kept.</Box>}
                        columnDefinitions={[
                            {id: 'why', header: 'Reason', cell: item => preview.reason_text?.[item.reason] ?? item.reason},
                            {id: 'count', header: 'Folders', cell: item => item.count},
                        ]}/>
                    {isAdmin
                        ? <PreviewRows key={preview.preview_id} preview={preview}/>
                        : <Box>Only a cluster administrator can see the folder list.</Box>}
                </SpaceBetween>}
            </SpaceBetween>
        </Container>
        {confirming && preview && <RunNow preview={preview} onDismiss={() => setConfirming(false)} onDone={() => {setConfirming(false); load();}}/>}
        <Upcoming refresh={refresh}/>
        <Container header={<Header variant="h2">Last runs</Header>}>
            <Table
                variant="embedded"
                items={runs}
                trackBy="run_id"
                empty={<Box textAlign="center">No runs yet.</Box>}
                columnDefinitions={[
                    {id: 'started', header: 'Started', cell: run => when(run.started_at)},
                    {id: 'by', header: 'By', cell: run => run.trigger === 'schedule' ? 'Hourly check' : run.requested_by},
                    {id: 'status', header: 'Result', cell: run => {
                        const shown = RUN_STATUS[run.status ?? ''] ?? {type: 'info' as const, text: run.status};
                        return <StatusIndicator type={shown.type}>{shown.text}</StatusIndicator>;
                    }},
                    {id: 'removed', header: 'Removed', cell: run => run.status === 'preview' ? '-' : run.status === 'dry_run' ? `Would remove ${run.would_remove_folders ?? 0}` : `${run.removed_folders ?? 0} folders, ${formatBytes(run.removed_bytes ?? 0)}${run.partial_folders ? `, ${run.partial_folders} partly` : ''}`},
                    {id: 'told', header: 'Owners told', cell: run => run.noticed_folders ?? 0},
                    {id: 'used', header: 'Used', cell: run => run.used_percent_before == null ? '-' : `${run.used_percent_before}% to ${run.used_percent_after ?? '?'}%`},
                    {id: 'note', header: 'Note', cell: run => run.message ?? ''},
                ]}/>
        </Container>
        <RemovalLog refresh={refresh}/>
    </SpaceBetween>;
}

export default function ScratchCleanup(props: any) {
    return <IdeaAppLayout {...props} content={<ScratchCleanupContent/>}/>;
}
