import {useEffect, useState} from 'react';
import {Alert, Box, Header, SpaceBetween, StatusIndicator} from '@cloudscape-design/components';
import {AppContext} from '../../common';
import {ReportingInsights} from '../../client/reporting-model';
import {BudgetTable, EfficiencyTiles, JobsTable} from '../reporting/insights-components';
import {updated} from '../reporting/reporting-format';

export default function MyJobEfficiency({timezone, reload}: {timezone?: string; reload: number}) {
    const client = AppContext.get().client().myCosts();
    const [data, setData] = useState<ReportingInsights>();
    const [busy, setBusy] = useState(true);
    const [error, setError] = useState(false);
    useEffect(() => {
        let current = true;
        setBusy(true); setError(false);
        client.getInsights({period: 'this_month'}).then(result => {if (current) setData(result);})
            .catch(() => {if (current) setError(true);}).finally(() => {if (current) setBusy(false);});
        return () => {current = false;};
    }, [client, reload]);
    return <SpaceBetween size="m">
        <Header variant="h2">My job efficiency</Header>
        {busy && <StatusIndicator type="loading">Loading job efficiency</StatusIndicator>}
        {error && <Alert type="error">Could not load job efficiency. Use Refresh to try again.</Alert>}
        {data && <SpaceBetween size="m">
            {timezone && <Box>{updated(data.updated_at, timezone)}</Box>}
            <EfficiencyTiles jobs={data.jobs} currency={data.currency}/>
            {timezone && <JobsTable title="Least efficient jobs" rows={data.jobs.least_efficient} currency={data.currency} timezone={timezone} personal/>}
            <BudgetTable budgets={data.budgets} currency={data.currency}/>
        </SpaceBetween>}
    </SpaceBetween>;
}
