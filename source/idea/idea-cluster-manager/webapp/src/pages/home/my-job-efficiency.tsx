import {useEffect, useState} from 'react';
import {Alert, Box, Header, SpaceBetween, StatusIndicator} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {ReportingInsights, ReportingSummaryRequest} from '../../client/reporting-model';
import {BudgetTable, Coaching, EfficiencyTiles, JobsTable, mergeJobs} from '../reporting/insights-components';
import {ReportLoading} from '../reporting/insights-table';
import {updated} from '../reporting/reporting-format';

export default function MyJobEfficiency({timezone, reload, period = {period: 'this_month'}}: {timezone?: string; reload: number; period?: ReportingSummaryRequest}) {
    const client = AppContext.get().client().myCosts();
    const [data, setData] = useState<ReportingInsights>();
    const [busy, setBusy] = useState(true);
    const [error, setError] = useState(false);
    const periodKey = JSON.stringify(period);
    useEffect(() => {
        let current = true;
        setBusy(true); setError(false);
        client.getInsights(JSON.parse(periodKey)).then(result => {if (current) setData(result);})
            .catch(() => {if (current) setError(true);}).finally(() => {if (current) setBusy(false);});
        return () => {current = false;};
    }, [client, reload, periodKey]);
    return <SpaceBetween size="m">
        {data && <Coaching jobs={data.jobs} currency={data.currency} personal/>}
        <Header variant="h2" description={period.period === 'this_month' ? 'This month' : data?.period.label}>My job efficiency</Header>
        {busy && <StatusIndicator type="loading">Loading job efficiency</StatusIndicator>}
        {error && <Alert type="error">Couldn't load job efficiency. Check your connection and use Refresh to try again.</Alert>}
        {data && <ReportLoading.Provider value={busy}><SpaceBetween size="m">
            {timezone && <Box>{updated(data.updated_at, timezone)}</Box>}
            <EfficiencyTiles jobs={data.jobs} currency={data.currency}/>
            {timezone && <JobsTable title="My top jobs" rows={mergeJobs(data.jobs)} currency={data.currency} timezone={timezone} personal/>}
            <BudgetTable budgets={data.budgets} currency={data.currency}/>
        </SpaceBetween></ReportLoading.Provider>}
    </SpaceBetween>;
}
