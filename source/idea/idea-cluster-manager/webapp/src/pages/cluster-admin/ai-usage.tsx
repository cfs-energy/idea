import React, {Component, RefObject} from "react";

import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {BedrockModelUsage, BedrockUserUsage, Project, SocaAmount} from '../../client/data-model'
import IdeaListView from "../../components/list-view";
import IdeaSplitPanel from "../../components/split-panel";
import {ProjectsClient} from "../../client";
import {AppContext} from "../../common";
import {Box, SpaceBetween, StatusIndicator, Table} from "@cloudscape-design/components";
import Utils from "../../common/utils";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import {withRouter} from "../../navigation/navigation-utils";

export interface AiUsageProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {

}

export interface AiUsageState {
    projectSelected: boolean
    splitPanelOpen: boolean
}

// Strips the geography prefix, the provider prefix and the version suffix, so that
// us.amazon.nova-pro-v1:0 reads as nova-pro.
const shortModelName = (modelId: string): string => modelId
    .replace(/^(us|eu|apac|global)\./, '')
    .replace(/^(anthropic|amazon|meta)\./, '')
    .replace(/-v\d+:\d+$/, '')

const tokens = (value?: number): string => Utils.asNumber(value, 0).toLocaleString()

// A per model or per user figure is a share of the project spend rather than a priced total, so
// it is always marked estimated. No answer from AWS Cost Explorer stays blank instead of zero.
const shareOfSpend = (spend?: SocaAmount, isEstimated?: boolean): string => {
    if (!spend) {
        return '--'
    }
    return `${Utils.getFormattedAmount(spend)}${isEstimated ? ' (estimated)' : ''}`
}

const projectSpend = (project: Project) => {
    const usage = project.bedrock_usage
    if (!usage || usage.is_unavailable) {
        return <span style={{color: 'grey'}}>--</span>
    }
    if (usage.spend_is_unavailable || !usage.spend) {
        return <span style={{color: 'grey'}}>cost unavailable</span>
    }
    return Utils.getFormattedAmount(usage.spend)
}

const MODEL_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<BedrockModelUsage>[] = [
    {
        id: 'model_id',
        header: 'Model',
        cell: entry => shortModelName(entry.model_id ?? '')
    },
    {
        id: 'input_tokens',
        header: 'Input tokens',
        cell: entry => tokens(entry.input_tokens)
    },
    {
        id: 'output_tokens',
        header: 'Output tokens',
        cell: entry => tokens(entry.output_tokens)
    },
    {
        id: 'total_tokens',
        header: 'Total tokens',
        cell: entry => tokens(entry.total_tokens)
    },
    {
        id: 'invocations',
        header: 'Requests',
        cell: entry => tokens(entry.invocations)
    },
    {
        id: 'spend',
        header: 'Cost',
        cell: entry => shareOfSpend(entry.spend, entry.spend_is_estimated)
    }
]

const USER_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<BedrockUserUsage>[] = [
    {
        id: 'username',
        header: 'User',
        cell: entry => entry.username
    },
    {
        id: 'total_tokens',
        header: 'Tokens',
        cell: entry => tokens(entry.total_tokens)
    },
    {
        id: 'invocations',
        header: 'Requests',
        cell: entry => tokens(entry.invocations)
    },
    {
        id: 'spend',
        header: 'Cost',
        cell: entry => shareOfSpend(entry.spend, entry.spend_is_estimated)
    },
    {
        id: 'top_model_id',
        header: 'Top model',
        cell: entry => Utils.isEmpty(entry.top_model_id) ? '--' : shortModelName(entry.top_model_id!)
    }
]

const PROJECT_COLUMN_DEFINITIONS: TableProps.ColumnDefinition<Project>[] = [
    {
        id: 'title',
        header: 'Project',
        cell: project => project.title,
        sortingField: 'title'
    },
    {
        id: 'name',
        header: 'Project Code',
        cell: project => project.name,
        sortingField: 'name'
    },
    {
        id: 'total_tokens',
        header: 'Tokens',
        cell: project => {
            const usage = project.bedrock_usage
            if (usage?.is_unavailable) {
                return <StatusIndicator type="warning">Usage unavailable</StatusIndicator>
            }
            if (!usage || Utils.asNumber(usage.total_tokens, 0) === 0) {
                return <span style={{color: 'grey'}}>No usage recorded</span>
            }
            return tokens(usage.total_tokens)
        },
        sortingComparator: (a, b) => Utils.asNumber(a.bedrock_usage?.total_tokens, 0) - Utils.asNumber(b.bedrock_usage?.total_tokens, 0)
    },
    {
        id: 'invocations',
        header: 'Requests',
        cell: project => tokens(project.bedrock_usage?.invocations)
    },
    {
        id: 'spend',
        header: 'Cost',
        cell: project => projectSpend(project)
    },
    {
        id: 'top_model',
        header: 'Top model',
        // The service sorts by_model by tokens, so the first entry is the busiest.
        cell: project => {
            const top = project.bedrock_usage?.by_model?.[0]
            return top ? shortModelName(top.model_id ?? '') : <span style={{color: 'grey'}}>--</span>
        }
    }
]

class AiUsage extends Component<AiUsageProps, AiUsageState> {

    listing: RefObject<IdeaListView | null>

    constructor(props: AiUsageProps) {
        super(props);
        this.listing = React.createRef()
        this.state = {
            projectSelected: false,
            splitPanelOpen: false
        }
    }

    projects(): ProjectsClient {
        return AppContext.get().client().projects()
    }

    getListing(): IdeaListView {
        return this.listing.current!
    }

    getSelected(): Project | null {
        if (this.getListing() == null) {
            return null
        }
        return this.getListing().getSelectedItem()
    }

    isSelected(): boolean {
        return this.state.projectSelected && this.getSelected() != null
    }

    buildListing() {
        return (
            <IdeaListView
                ref={this.listing}
                title="AI Usage"
                description="Amazon Bedrock tokens, requests and cost per project over the last 30 days. Select a project to break it down per model and per user."
                selectionType="single"
                onRefresh={() => {
                    this.setState({
                        projectSelected: false,
                        splitPanelOpen: false
                    }, () => {
                        this.getListing().fetchRecords()
                    })
                }}
                onSelectionChange={() => {
                    this.setState({
                        projectSelected: true,
                        splitPanelOpen: true
                    })
                }}
                onFetchRecords={() => {
                    return this.projects().listBedrockUsage({})
                }}
                columnDefinitions={PROJECT_COLUMN_DEFINITIONS}
            />
        )
    }

    buildSplitPanelContent() {
        const usage = this.getSelected()?.bedrock_usage
        return (this.isSelected() &&
            <IdeaSplitPanel
                title={`AI usage for ${this.getSelected()?.title}`}
                description="Last 30 days. Cost per model and per user is estimated by splitting the project spend by token share, because AWS Cost Explorer prices the project cost allocation tag rather than a model or a caller."
            >
                <SpaceBetween size="l" direction="vertical">
                    <Table
                        variant="embedded"
                        header={<Box variant="h3">Per model</Box>}
                        columnDefinitions={MODEL_COLUMN_DEFINITIONS}
                        items={usage?.by_model ?? []}
                        empty={<Box textAlign="center" color="text-body-secondary">No usage recorded in the last 30 days.</Box>}
                    />
                    <Table
                        variant="embedded"
                        header={<Box variant="h3">Per user</Box>}
                        columnDefinitions={USER_COLUMN_DEFINITIONS}
                        items={usage?.by_user ?? []}
                        empty={<Box textAlign="center" color="text-body-secondary">No usage recorded in the last 30 days.</Box>}
                    />
                </SpaceBetween>
            </IdeaSplitPanel>)
    }

    render() {
        return (
            <IdeaAppLayout
                ideaPageId={this.props.ideaPageId}
                toolsOpen={this.props.toolsOpen}
                tools={this.props.tools}
                onToolsChange={this.props.onToolsChange}
                onPageChange={this.props.onPageChange}
                sideNavHeader={this.props.sideNavHeader}
                sideNavItems={this.props.sideNavItems}
                onSideNavChange={this.props.onSideNavChange}
                onFlashbarChange={this.props.onFlashbarChange}
                flashbarItems={this.props.flashbarItems}
                breadcrumbItems={[
                    {
                        text: 'IDEA',
                        href: '#/'
                    },
                    {
                        text: 'Cluster Management',
                        href: '#/cluster/status'
                    },
                    {
                        text: 'AI Usage',
                        href: ''
                    }
                ]}
                content={
                    <div>
                        {this.buildListing()}
                    </div>
                }
                splitPanelOpen={this.state.splitPanelOpen}
                splitPanel={this.buildSplitPanelContent()}
                onSplitPanelToggle={(event: any) => {
                    this.setState({
                        splitPanelOpen: event.detail.open
                    })
                }}
            />
        )
    }
}

export default withRouter(AiUsage)
