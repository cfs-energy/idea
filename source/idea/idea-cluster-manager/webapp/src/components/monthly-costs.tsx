import React from 'react';
import {Badge, Box, Button, Container, Header, Popover, SpaceBetween} from '@cloudscape-design/components';
import {GetMyCostsResult, MyCostsAmount} from '../client/data-model';
import {FACETS, FacetComparison, facetAmount, money} from './cost-charts';
import './monthly-costs.scss';
import {updated} from '../pages/reporting/reporting-format';

export const collectingLabel = (costs: GetMyCostsResult | null) => {
    const minutes = costs?.expected_ready_at ? Math.max(1, Math.ceil((Date.parse(costs.expected_ready_at) - Date.now()) / 60000)) : 20;
    return `${costs?.collecting_delayed ? 'Collecting delayed' : 'Collecting'} · about ${minutes} min`;
};
export function costBadge(line: MyCostsAmount | undefined, collecting: boolean) {
    if (collecting) return 'Collecting';
    if (facetAmount(line) == null) return 'No data';
    if (line?.status === 'partial') return 'Partial';
    if (line?.status === 'estimated_share') return 'Shared cost';
    return null;
}
export const CostsBillboard: React.FC<{costs: GetMyCostsResult | null; home?: boolean}> = ({costs, home = false}) => {
    const currency = costs?.currency || 'USD';
    const collecting = !costs?.current;
    return <Container header={<Header variant="h2" actions={home ? <Button href="#/home/my-costs">View My costs</Button> : undefined}>Your costs</Header>}>
        <SpaceBetween size="s">
            <div className="personal-costs-headline">
                {(['previous', 'current'] as const).map((period, index) => <div key={period}>
                    <Box variant="small">{index === 0 ? 'Last month' : 'This month'}</Box>
                    <Box variant="h2">{collecting ? collectingLabel(costs) : money(costs?.[period]?.total, currency)}</Box>
                </div>)}
                {costs?.refreshed_at && costs.timezone && <Box variant="small">{updated(costs.refreshed_at, costs.timezone)}</Box>}
            </div>
            <div className="personal-costs-tiles">
                {FACETS.filter(({key}) => facetAmount(costs?.current?.[key]) != null || facetAmount(costs?.previous?.[key]) != null).map(({key, label, target}) => {
                    const line = costs?.current?.[key];
                    const previous = costs?.previous?.[key];
                    const badge = costBadge(line, collecting) || costBadge(previous, collecting);
                    return <SpaceBetween key={key} size="xxs">
                        <Button variant="inline-link" href={home ? `#/home/my-costs?facet=${target}` : undefined}
                            onClick={home ? undefined : () => {
                                const element = document.getElementById(target);
                                element?.scrollIntoView?.({behavior: 'smooth', block: 'start'});
                                element?.focus({preventScroll: true});
                            }}>{label}</Button>
                        <Box variant="h3">{money(facetAmount(line), currency)}</Box>
                        <Box variant="small" color="text-body-secondary">Last month {money(facetAmount(previous), currency)}</Box>
                        {badge && <Popover header={`${label} details`} triggerType="custom"
                            content={<SpaceBetween size="s"><Box>This month: {facetAmount(line) == null ? 'No data yet.' : line?.status === 'partial' ? 'Some days are missing.' : 'Uses the available daily costs.'}</Box>
                                <Box>Last month: {facetAmount(previous) == null ? 'No data yet.' : previous?.status === 'partial' ? 'Some days are missing.' : 'Uses the available daily costs.'}</Box></SpaceBetween>}>
                            <Button variant="inline-link" ariaLabel={`${label}: ${badge}`}><Badge>{badge}</Badge></Button>
                        </Popover>}
                    </SpaceBetween>;
                })}
            </div>
            {home && <FacetComparison costs={costs}/>}
        </SpaceBetween>
    </Container>;
};
