import React, {useEffect, useState} from 'react';
import {Alert, Button, Checkbox, Container, DatePicker, ExpandableSection, FormField, Header, Input, SpaceBetween} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {hasAccess} from '../../navigation/task-navigation';
import {MetricsBackfillStatus} from '../../client/metrics-backfill';

const today = () => new Date().toISOString().slice(0, 10);
const describe = (label: string, status: MetricsBackfillStatus | null) => status
    ? `${label}: ${status.state}${status.dry_run ? ' (dry run)' : ''}, ${status.jobs_scanned} scanned, ${status.points_built} points built, ${status.points_sent} sent, ${status.points_skipped} skipped, ${status.errors} errors${status.last_error ? ` (${status.last_error})` : ''}`
    : `${label}: status unavailable`;

export default function MetricsHistorySettings({active}: {active: boolean}) {
    const [start, setStart] = useState('');
    const [end, setEnd] = useState(today);
    const [days, setDays] = useState('400');
    const [dryRun, setDryRun] = useState(true);
    const [expanded, setExpanded] = useState(false);
    const [jobs, setJobs] = useState<MetricsBackfillStatus | null>(null);
    const [cost, setCost] = useState<MetricsBackfillStatus | null>(null);
    const [busy, setBusy] = useState({jobs: false, cost: false});
    const [errors, setErrors] = useState({jobs: '', cost: ''});
    const canJobs = hasAccess(AppContext.get(), 'jobs-admin');
    const canCost = hasAccess(AppContext.get(), 'cluster-admin');
    const client = () => AppContext.get().client();

    useEffect(() => {
        if (!active) return;
        setJobs(null);
        setCost(null);
        let cancelled = false;
        const read = async () => {
            await Promise.all((['jobs', 'cost'] as const).map(async kind => {
                if (!(kind === 'jobs' ? canJobs : canCost)) return;
                try {
                    const status = await (kind === 'jobs' ? client().schedulerAdmin().getJobMetricsBackfill() : client().clusterSettings().getCostMetricsBackfill());
                    if (cancelled) return;
                    (kind === 'jobs' ? setJobs : setCost)(status);
                    setErrors(previous => ({...previous, [kind]: ''}));
                } catch (error: any) {
                    if (cancelled) return;
                    (kind === 'jobs' ? setJobs : setCost)(null);
                    setErrors(previous => ({...previous, [kind]: error?.message || String(error)}));
                }
            }));
        };
        void read();
        const timer = window.setInterval(read, 10000);
        return () => { cancelled = true; window.clearInterval(timer); };
    }, [active, canJobs, canCost]);

    const run = async (kind: 'jobs' | 'cost') => {
        setBusy(previous => ({...previous, [kind]: true}));
        setErrors(previous => ({...previous, [kind]: ''}));
        try {
            const status = await (kind === 'jobs'
                ? client().schedulerAdmin().backfillJobMetrics({start_date: start, end_date: end, dry_run: dryRun})
                : client().clusterSettings().backfillCostMetrics({days: Number(days), dry_run: dryRun}));
            (kind === 'jobs' ? setJobs : setCost)(status);
        } catch (error: any) {
            setErrors(previous => ({...previous, [kind]: error?.message || String(error)}));
        } finally {
            setBusy(previous => ({...previous, [kind]: false}));
        }
    };
    const validJobs = /^\d{4}-\d{2}-\d{2}$/.test(start) && start <= end && end <= today();
    const validCost = Number.isInteger(Number(days)) && Number(days) > 0;
    return <Container header={<Header variant="h2">Backfill history</Header>}>
        <SpaceBetween size="m">
            <div>Enable historical ingestion for each idea.* metric in Datadog before running.</div>

            <SpaceBetween direction="horizontal" size="m">
                <FormField label="Start date (UTC)"><DatePicker ariaLabel="Start date" value={start} onChange={e => setStart(e.detail.value)}/></FormField>
                <FormField label="End date (UTC, inclusive)"><DatePicker ariaLabel="End date" value={end} onChange={e => setEnd(e.detail.value)}/></FormField>
            </SpaceBetween>
            <ExpandableSection headerText="Advanced" expanded={expanded} onChange={e => setExpanded(e.detail.expanded)}>
                {expanded && <FormField label="Cost days" description="Trailing full days, up to 15 months."><Input ariaLabel="Cost days" value={days} onChange={e => setDays(e.detail.value)}/></FormField>}
            </ExpandableSection>
            <Checkbox checked={dryRun} onChange={e => setDryRun(e.detail.checked)}>Dry run</Checkbox>
            {errors.jobs && <Alert type="error">Jobs: {errors.jobs}</Alert>}
            <Button disabled={!canJobs || !jobs || !validJobs || busy.jobs || jobs.state === 'running'} loading={busy.jobs} onClick={() => run('jobs')}>Run jobs</Button>
            <div role="status">{describe('Jobs', jobs)}</div>
            {errors.cost && <Alert type="error">Cost: {errors.cost}</Alert>}
            <Button disabled={!canCost || !cost || !validCost || busy.cost || cost.state === 'running'} loading={busy.cost} onClick={() => run('cost')}>Run cost</Button>
            <div role="status">{describe('Cost rows', cost)}</div>
        </SpaceBetween>
    </Container>;
}
