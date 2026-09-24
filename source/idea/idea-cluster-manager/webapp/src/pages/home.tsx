import React, {Component} from 'react';
import {Box, Button, Container, Header, Link, SpaceBetween, StatusIndicator, Table} from '@cloudscape-design/components';
import {GetMyCostsResult, SocaJob, VirtualDesktopSession} from '../client/data-model';
import {AppContext} from '../common';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../components/app-layout';
import {personalCostsCache} from '../client/personal-costs-cache';
import {CostsBillboard} from '../components/monthly-costs';
import {IdeaSideNavigationProps} from '../components/side-navigation';
import {withRouter} from '../navigation/navigation-utils';
import {hasAccess, LANDING_PATHS} from '../navigation/task-navigation';
import {OnToolsChangeEvent} from '../App';

export interface HomePageProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
    toolsOpen: boolean
    tools: React.ReactNode
    onToolsChange: (event: OnToolsChangeEvent) => void
}

interface HomePageState {
    costs: GetMyCostsResult | null
    costsError: string | null
    jobs: SocaJob[] | null
    jobsError: string | null
    desktops: VirtualDesktopSession[] | null
    desktopsError: string | null
}

const errorText = (value: unknown) => (value as any)?.message ?? `${value}`;

class Home extends Component<HomePageProps, HomePageState> {
    private disposed = false;

    constructor(props: HomePageProps) {
        super(props);
        this.state = {costs: null, costsError: null, jobs: null, jobsError: null, desktops: null, desktopsError: null};
    }

    componentDidMount() {
        const context = AppContext.get();
        this.unsubscribeCosts = personalCostsCache().subscribe(costs => this.setState({costs, costsError: null}), error => this.setState({costsError: errorText(error)}));
        if (hasAccess(context, 'jobs')) {
            Promise.all([
                context.client().scheduler().listActiveJobs({paginator: {page_size: 5}}),
                context.client().scheduler().listCompletedJobs({paginator: {page_size: 5}})
            ]).then(([active, completed]) => {
                const rows = [...(active.listing ?? []), ...(completed.listing ?? [])]
                    .sort((a, b) => `${b.end_time ?? b.start_time ?? b.queue_time ?? ''}`.localeCompare(`${a.end_time ?? a.start_time ?? a.queue_time ?? ''}`))
                    .slice(0, 5);
                if (!this.disposed) this.setState({jobs: rows});
            }).catch(error => {
                if (!this.disposed) this.setState({jobs: [], jobsError: errorText(error)});
            });
        } else {
            this.setState({jobs: []});
        }
        if (hasAccess(context, 'desktop')) {
            context.client().virtualDesktop().listSessions({paginator: {page_size: 5}}).then(result => {
                if (!this.disposed) this.setState({desktops: (result.listing ?? []).slice(0, 5)});
            }).catch(error => {
                if (!this.disposed) this.setState({desktops: [], desktopsError: errorText(error)});
            });
        } else {
            this.setState({desktops: []});
        }
    }

    private unsubscribeCosts?: () => void;
    componentWillUnmount() { this.disposed = true; this.unsubscribeCosts?.(); }

    content() {
        const context = AppContext.get();
        const links = [
            ['Home', 'home'], ['My jobs', 'my-jobs'], ['My desktops', 'my-desktops'],
            ['Files', 'files'], ['My costs', 'my-costs'], ['Reports', 'reports']
        ].filter(([, id]) => id !== 'my-jobs' || hasAccess(context, 'jobs'))
            .filter(([, id]) => id !== 'my-desktops' || hasAccess(context, 'desktop'))
            .filter(([, id]) => id !== 'reports' || context.getClusterSettingsService().isCustomDashboardEnabled());
        return <SpaceBetween size="l">
            {this.state.costs && this.state.costs.state !== 'error' && <CostsBillboard costs={this.state.costs} home/>}
            {!this.state.costs && !this.state.costsError && <StatusIndicator type="loading">Loading your costs</StatusIndicator>}
            {this.state.costsError && <Box color="text-status-error">Your costs could not be loaded: {this.state.costsError}</Box>}

            {this.state.jobs === null && hasAccess(context, 'jobs') && <StatusIndicator type="loading">Loading recent jobs</StatusIndicator>}
            {this.state.jobsError && <Box color="text-status-error">Recent jobs could not be loaded: {this.state.jobsError}</Box>}
            {this.state.jobs && this.state.jobs.length > 0 && <Container header={<Header variant="h2" actions={<Button href="#/home/active-jobs">View all jobs</Button>}>Recent jobs</Header>}>
                <Table variant="embedded" items={this.state.jobs} columnDefinitions={[
                    {id: 'name', header: 'Job', cell: job => job.name ?? job.job_id ?? '-'},
                    {id: 'state', header: 'State', cell: job => job.state ?? '-'}
                ]}/>
            </Container>}

            {this.state.desktops === null && hasAccess(context, 'desktop') && <StatusIndicator type="loading">Loading recent desktops</StatusIndicator>}
            {this.state.desktopsError && <Box color="text-status-error">Recent desktops could not be loaded: {this.state.desktopsError}</Box>}
            {this.state.desktops && this.state.desktops.length > 0 && <Container header={<Header variant="h2" actions={<Button href="#/home/virtual-desktops">View all desktops</Button>}>Recent desktops</Header>}>
                <Table variant="embedded" items={this.state.desktops} columnDefinitions={[
                    {id: 'name', header: 'Desktop', cell: desktop => desktop.name ?? desktop.idea_session_id ?? '-'},
                    {id: 'state', header: 'State', cell: desktop => desktop.state ?? '-'}
                ]}/>
            </Container>}

            <Container header={<Header variant="h2">Quick links</Header>}>
                <SpaceBetween direction="horizontal" size="s">
                    {links.map(([label, id]) => <Link key={id} href={`#${LANDING_PATHS[id]}`}>{label}</Link>)}
                </SpaceBetween>
            </Container>
        </SpaceBetween>;
    }

    render() {
        return <IdeaAppLayout {...this.props} contentType="default" header={<Header variant="h1">Home</Header>} content={this.content()}/>;
    }
}

export default withRouter(Home);
