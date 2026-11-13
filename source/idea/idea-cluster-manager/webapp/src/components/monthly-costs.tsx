import React from 'react';
import {Badge, Box, Button, Container, Header, Popover, SpaceBetween} from '@cloudscape-design/components';
import {GetMyCostsResult, MyCostsAmount} from '../client/data-model';
import {FACETS, facetAmount, money} from './cost-charts';
import './monthly-costs.scss';
import {updated} from '../pages/reporting/reporting-format';

export const collectingLabel = (costs: GetMyCostsResult | null) => {
    if (costs == null) return 'Not available';
    if (costs.collecting_delayed) return 'Taking longer than usual';
    const minutes = costs.expected_ready_at ? Math.max(1, Math.ceil((Date.parse(costs.expected_ready_at) - Date.now()) / 60000)) : 20;
    return `Collecting · about ${minutes} min`;
};
export interface CostGaps {missing: number; partly: number}
// The one rule for what the portal calls not counted. A day is not counted only when it has no amount; today is
// still being counted, so it isn't one, and neither is a day the cost does not apply to. A partial day has an
// amount that is a floor: a price was missing, or the observations missed part of the day.
export function costGaps(line: MyCostsAmount | undefined, today: string): CostGaps {
    const days = line?.daily ?? [];
    if (!days.length) return {missing: facetAmount(line) == null ? 0 : line?.coverage?.missing_days ?? (line?.status === 'partial' ? 1 : 0), partly: 0};
    return {missing: days.filter(day => day.date < today && day.amount == null && day.status !== 'not_applicable').length,
        partly: days.filter(day => day.amount != null && day.status === 'partial').length};
}
const days = (count: number) => count === 1 ? 'A day is' : `${count} days are`;
export const costCaveat = ({missing, partly}: CostGaps) => missing ? `So far. ${days(missing)} not counted yet.` : partly ? `So far. ${days(partly)} only partly counted.` : null;
export const localToday = (timezone?: string) => new Intl.DateTimeFormat('en-CA', {timeZone: timezone ?? 'UTC'}).format(new Date());
export function costBadge(line: MyCostsAmount | undefined, collecting: boolean, today = '9999-12-31') {
    if (collecting) return 'Collecting';
    if (facetAmount(line) == null) return 'No data';
    if (costCaveat(costGaps(line, today))) return 'Partial';
    if (line?.status === 'estimated_share') return 'Shared cost';
    return null;
}
export const CostsBillboard: React.FC<{costs: GetMyCostsResult | null}> = ({costs}) => {
    const currency = costs?.currency || 'USD';
    const collecting = !costs?.current;
    const today = localToday(costs?.timezone);
    const details = (line?: MyCostsAmount) => facetAmount(line) == null ? 'No data yet.' : costCaveat(costGaps(line, today)) ?? 'Uses the available daily costs.';
    return <Container header={<Header variant="h2">Your costs</Header>}>
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
                    const badge = costBadge(line, collecting, today) || costBadge(previous, collecting, today);
                    return <SpaceBetween key={key} size="xxs">
                        <Button variant="inline-link"
                            onClick={() => {
                                const element = document.getElementById(target);
                                element?.scrollIntoView?.({behavior: 'smooth', block: 'start'});
                                element?.focus({preventScroll: true});
                            }}>{label}</Button>
                        <Box variant="h3">{money(facetAmount(line), currency)}</Box>
                        <Box variant="small" color="text-body-secondary">Last month {money(facetAmount(previous), currency)}</Box>
                        {badge && <Popover header={`${label} details`} triggerType="custom"
                            content={<SpaceBetween size="s"><Box>This month: {details(line)}</Box><Box>Last month: {details(previous)}</Box></SpaceBetween>}>
                            <Button variant="inline-link" ariaLabel={`${label}: ${badge}`}><Badge>{badge}</Badge></Button>
                        </Popover>}
                    </SpaceBetween>;
                })}
            </div>
        </SpaceBetween>
    </Container>;
};
