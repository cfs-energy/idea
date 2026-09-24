import React, {Component} from 'react';
import {Box, Button, ExpandableSection, Header, SpaceBetween} from '@cloudscape-design/components';
import {GetMyCostsResult} from '../../client/data-model';
import {IdeaSideNavigationProps} from '../../components/side-navigation';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {withRouter} from '../../navigation/navigation-utils';
import {CostsBillboard} from '../../components/monthly-costs';
import {DailyCostCharts, FACETS} from '../../components/cost-charts';
import {personalCostsCache} from '../../client/personal-costs-cache';
import StorageUsage from './storage-usage';

export interface MyCostsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {}
interface MyCostsState { costs: GetMyCostsResult | null; storageOpen: boolean; refreshing: boolean; acknowledged: boolean }
class MyCosts extends Component<MyCostsProps, MyCostsState> {
    state: MyCostsState = {costs: null, storageOpen: false, refreshing: false, acknowledged: false};
    private unsubscribe?: () => void;
    private disposed = false;
    private anchorHandled = false;
    componentDidMount() {
        this.unsubscribe = personalCostsCache().subscribe(costs => this.setState({costs}, () => {
            const query = window.location.hash.split('?')[1];
            const target = new URLSearchParams(query).get('facet');
            if (!this.anchorHandled && target && FACETS.some(f => f.target === target)) {
                this.anchorHandled = true;
                const element = document.getElementById(target);
                element?.scrollIntoView?.({block: 'start'});
                element?.focus({preventScroll: true});
            }
        }));
    }
    componentWillUnmount() { this.disposed = true; this.unsubscribe?.(); }
    refresh = async () => {
        this.setState({refreshing: true});
        try {
            const result = await personalCostsCache().refresh();
            if (!this.disposed) this.setState({acknowledged: result.refresh_acknowledged === true});
        } catch (error: any) {
            if (!this.disposed) this.props.onFlashbarChange({items: [{type: 'error', content: error?.message || 'Refresh unavailable', dismissible: true}]});
        } finally {
            if (!this.disposed) this.setState({refreshing: false});
        }
    };
    render() {
        const costs = this.state.costs;
        return <IdeaAppLayout {...this.props}
            breadcrumbItems={[{text: 'IDEA', href: '#/'}, {text: 'Home', href: '#/'}, {text: 'My costs', href: ''}]}
            header={<Header variant="h1" actions={<SpaceBetween direction="horizontal" size="s">
                <span role="status">{this.state.acknowledged ? 'Refresh requested' : ''}</span>
                <Button loading={this.state.refreshing} onClick={this.refresh}>Refresh</Button>
            </SpaceBetween>}>My costs</Header>}
            contentType="default" content={<SpaceBetween size="l">
                <CostsBillboard costs={costs}/>
                <DailyCostCharts costs={costs}/>
                <ExpandableSection headerText="Storage usage: folders and quotas" expanded={this.state.storageOpen}
                    onChange={({detail}) => this.setState({storageOpen: detail.expanded})}>
                    {this.state.storageOpen && <StorageUsage compact/>}
                </ExpandableSection>
                <ExpandableSection headerText="How it is calculated">
                    <SpaceBetween size="m">
                        <Box>Estimates use the cluster currency and timezone ({costs?.timezone || 'UTC'}). Last month is a full calendar month; this month ends at the snapshot time. Known costs include only available amounts. Unknown days are omitted; a measured zero is shown as zero. These estimates are not the bill.</Box>
                        {FACETS.map(({key, label}) => <div key={key}>
                            <Box variant="h3">{label}</Box>
                            <Box>{costs?.current?.[key]?.note || {
                                jobs: 'Completed-job compute only; running jobs, disks and scratch storage are excluded.',
                                desktops: 'Recorded runtime × instance rate; inferred stop and restart intervals may be incomplete.',
                                desktop_disks: 'Provisioned size × GB-month rate × calendar-month fraction, including stopped and retained disks. Dated inventory starts when collection starts; unobserved disks, snapshots and extra IOPS or throughput are excluded.',
                                shared_storage: "Daily billed spend × a dated byte share from complete measurements of the same file system and allocation pool. No historical share is inferred from today's folders.",
                                ai: "Daily project spend apportioned by the user's share of project tokens that day. Missing billing or a missing token denominator remains unknown."
                            }[key]}</Box>
                            {(['current', 'previous'] as const).map((period, index) => {
                                const line = costs?.[period]?.[key];
                                return <Box key={period} variant="small">{index === 0 ? 'This month' : 'Last month'}: {line?.reason || line?.status || 'Collecting'}
                                    {' · Source: '}{line?.source_as_of ? new Date(line.source_as_of).toLocaleString() : '--'}
                                    {' · Missing days: '}{line?.coverage?.missing_days ?? '--'}
                                    {' · Missing prices: '}{line?.coverage?.missing_prices ?? '--'}
                                    {' · Inferred intervals: '}{line?.coverage?.inferred_intervals ?? '--'}</Box>;
                            })}
                        </div>)}
                        <Box>Collection runs every 15 minutes. Refresh requests are checked each minute and keep the visible snapshot. Billing is cached for six hours and may arrive later. Folder scans are separate, cached for one hour, exclude symbolic links and can be partial. Unreadable users, stale measurements, a zero denominator or an inseparable storage allocation pool cannot establish a cost share.</Box>
                    </SpaceBetween>
                </ExpandableSection>
            </SpaceBetween>}/>;
    }
}
export default withRouter(MyCosts);
