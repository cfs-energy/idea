import React, {Component} from 'react';
import {Button, ExpandableSection, Header, SpaceBetween} from '@cloudscape-design/components';
import {GetMyCostsResult} from '../../client/data-model';
import {IdeaSideNavigationProps} from '../../components/side-navigation';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {AppContext} from '../../common';
import {withRouter} from '../../navigation/navigation-utils';
import {CostsBillboard} from '../../components/monthly-costs';
import {DailyCostCharts, FACETS} from '../../components/cost-charts';
import {personalCostsCache} from '../../client/personal-costs-cache';
import StorageUsage from './storage-usage';
import MyJobEfficiency from './my-job-efficiency';

export interface MyCostsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {}
interface MyCostsState { costs: GetMyCostsResult | null; storageOpen: boolean; refreshing: boolean; acknowledged: boolean; reload: number }
class MyCosts extends Component<MyCostsProps, MyCostsState> {
    state: MyCostsState = {costs: null, storageOpen: false, refreshing: false, acknowledged: false, reload: 0};
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
        this.setState(state => ({refreshing: true, reload: state.reload + 1}));
        try {
            const result = await personalCostsCache().refresh();
            if (!this.disposed) this.setState({acknowledged: result.refresh_acknowledged === true});
        } catch (error: any) {
            if (!this.disposed) this.props.onFlashbarChange({items: [{type: 'error', content: 'Could not refresh costs. Try Refresh again.', dismissible: true}]});
        } finally {
            if (!this.disposed) this.setState({refreshing: false});
        }
    };
    render() {
        const costs = this.state.costs;
        return <IdeaAppLayout {...this.props}
            breadcrumbItems={[{text: 'IDEA', href: '#/'}, {text: 'Home', href: '#/'}, {text: 'My costs', href: ''}]}
            header={<Header variant="h1" description="Estimated costs and resource use" actions={<SpaceBetween direction="horizontal" size="s">
                <span role="status">{this.state.acknowledged ? 'Refresh requested' : ''}</span>
                <Button loading={this.state.refreshing} onClick={this.refresh}>Refresh</Button>
            </SpaceBetween>}>My costs</Header>}
            contentType="default" content={<SpaceBetween size="l">
                <MyJobEfficiency timezone={costs?.timezone ?? AppContext.get().getClusterSettingsService().getClusterTimeZone()} reload={this.state.reload}/>
                <CostsBillboard costs={costs}/>
                <DailyCostCharts costs={costs}/>
                <ExpandableSection headerText="Storage usage: folders and quotas" expanded={this.state.storageOpen}
                    onChange={({detail}) => this.setState({storageOpen: detail.expanded})}>
                    {this.state.storageOpen && <StorageUsage compact/>}
                </ExpandableSection>
            </SpaceBetween>}/>;
    }
}
export default withRouter(MyCosts);
