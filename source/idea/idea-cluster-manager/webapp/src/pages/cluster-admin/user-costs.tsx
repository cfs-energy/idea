import React, {Component} from "react";
import {Alert, Box, Button, Container, Header, Pagination, SpaceBetween, Table, TextFilter} from "@cloudscape-design/components";
import {TableProps} from "@cloudscape-design/components/table";
import {useCollection} from "@cloudscape-design/collection-hooks";
import {GetMyCostsResult, GetMyCostsSummaryResult, ListUserCostsResult, UserCosts} from "../../client/data-model";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import CostSections, {hours, money, number, summaryCost} from "../../components/cost-sections";
import {withRouter} from "../../navigation/navigation-utils";
import {AppContext} from "../../common";
import IdeaSplitPanel from "../../components/split-panel";
import {CostsBillboard} from "../../components/monthly-costs";
import {DailyCostCharts} from "../../components/cost-charts";

export interface UserCostsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
}

export interface UserCostsState {
    listing: ListUserCostsResult | null
    selected: UserCosts[]
    summary: GetMyCostsSummaryResult | null
    costs: GetMyCostsResult | null
    summaryLoading: boolean
    splitPanelOpen: boolean
    error: string | null
}

// Every column sorts. The two whose cells can carry a subtotal marker sort on the number
// underneath it.
const USER_COLUMNS: TableProps.ColumnDefinition<UserCosts>[] = [
    {id: 'username', header: 'User', sortingField: 'username', cell: (item) => item.username},
    {id: 'ai_tokens', header: 'AI tokens', sortingField: 'ai_tokens', cell: (item) => number(item.ai_tokens)},
    {
        id: 'ai_cost',
        header: 'AI cost',
        sortingField: 'ai_cost',
        cell: (item) => (item.ai_cost_unavailable ? 'Not available' : money(item.ai_cost))
    },
    {
        id: 'storage_cost',
        header: 'Storage',
        sortingField: 'storage_cost',
        cell: (item) => item.storage_cost == null ? '-' : `${money(item.storage_cost)}${item.storage_cost_period ? ` (${item.storage_cost_period})` : ''}`
    },
    {id: 'storage_gb', header: 'Storage GB', sortingField: 'storage_gb', cell: (item) => item.storage_gb == null ? '-' : `${item.storage_gb.toFixed(2)} GB`},
    {
        id: 'desktop_hours',
        header: 'Desktop hours',
        sortingField: 'desktop_hours',
        cell: (item) => hours(item.desktop_hours)
    },
    {
        id: 'desktop_cost',
        header: 'Desktop cost',
        sortingField: 'desktop_cost',
        // An unpriced instance type must not read as a free desktop.
        cell: (item) => summaryCost(item.desktop_cost, item.desktop_session_count, item.desktop_unpriced_sessions)
    },
    {id: 'job_count', header: 'Jobs', sortingField: 'job_count', cell: (item) => number(item.job_count)},
    {
        id: 'job_cost',
        header: 'Job cost',
        sortingField: 'job_cost',
        // A job whose instance hours could not be priced leaves this total short.
        cell: (item) => (item.job_cost_unavailable
            ? 'Not available'
            : summaryCost(item.job_cost, item.job_count, item.job_unpriced_jobs))
    },
    {
        id: 'total_cost',
        header: 'Total',
        sortingField: 'total_cost',
        cell: (item) => `${money(item.total_cost)}${item.total_cost_excludes_storage ? ' (excludes storage)' : ''}`
    }
]

// Matches the page size the other administration tables use.
export const USER_COSTS_PAGE_SIZE = 30

const EMPTY_STATE = (
    <Box textAlign="center" color="inherit">
        <Box variant="strong">No costs recorded</Box>
        <Box variant="p" color="inherit">
            No costs were recorded for the enabled sources in this measurement window.
        </Box>
    </Box>
)

const NO_MATCH_STATE = (
    <Box textAlign="center" color="inherit">
        <Box variant="strong">No matching users</Box>
        <Box variant="p" color="inherit">No user in the window matches that name.</Box>
    </Box>
)

interface UsersTableProps {
    listing: UserCosts[]
    loading: boolean
    selected: UserCosts[]
    onSelect: (selected: UserCosts[]) => void
    note: React.ReactNode
    storageDisabled: boolean
}

/**
 * The users table, filtered, sorted and paged in the browser. The listing is one small
 * row per user and arrives in a single read, so narrowing it needs no round trip.
 */
const UsersTable: React.FC<UsersTableProps> = ({listing, loading, selected, onSelect, note, storageDisabled}) => {

    const columns = storageDisabled
        ? USER_COLUMNS.filter((column) => column.id !== 'storage_cost' && column.id !== 'storage_gb')
        : USER_COLUMNS
    const totalColumn = columns.find((column) => column.id === 'total_cost') ?? columns[0]

    const {items, collectionProps, filterProps, paginationProps, filteredItemsCount} = useCollection(listing, {
        filtering: {
            filteringFunction: (item, filteringText) =>
                (item.username ?? '').toLowerCase().includes(filteringText.trim().toLowerCase()),
            empty: EMPTY_STATE,
            noMatch: NO_MATCH_STATE
        },
        // Biggest spender first.
        sorting: {defaultState: {sortingColumn: totalColumn, isDescending: true}},
        pagination: {pageSize: USER_COSTS_PAGE_SIZE}
    })

    const total = listing.length
    const shown = filteredItemsCount ?? total

    return (
        <Container header={
            <Header
                variant="h2"
                counter={`(${shown} of ${total} users)`}
                description="Every user with a measured cost in the last 30 days. Select a user to see their breakdown.">
                Users
            </Header>
        }>
            <SpaceBetween size="s">
                {note}
                <Table
                    {...collectionProps}
                    variant="embedded"
                    trackBy="username"
                    selectionType="single"
                    selectedItems={selected}
                    onSelectionChange={({detail}) => onSelect(detail.selectedItems)}
                    items={items}
                    loading={loading}
                    loadingText="Retrieving user costs ..."
                    columnDefinitions={columns}
                    ariaLabels={{
                        selectionGroupLabel: 'User selection',
                        itemSelectionLabel: (_data, item) => `Show costs for ${item.username}`
                    }}
                    filter={
                        <TextFilter
                            {...filterProps}
                            filteringAriaLabel="Filter users by name"
                            filteringPlaceholder="Find a user"
                            countText={`${shown} matches`}
                        />
                    }
                    pagination={paginationProps.pagesCount > 1
                        ? <Pagination
                            {...paginationProps}
                            ariaLabels={{
                                nextPageLabel: 'Next page',
                                previousPageLabel: 'Previous page',
                                pageLabel: (pageNumber) => `Page ${pageNumber}`
                            }}/>
                        : undefined}
                />
            </SpaceBetween>
        </Container>
    )
}

class UserCostsPage extends Component<UserCostsProps, UserCostsState> {

    constructor(props: UserCostsProps) {
        super(props);
        this.state = {listing: null, selected: [], summary: null, costs: null, summaryLoading: false, splitPanelOpen: false, error: null}
    }

    componentDidMount() {
        this.fetchListing().finally()
    }

    client() {
        return AppContext.get().client().myCosts()
    }

    fetchListing(): Promise<boolean> {
        return new Promise<boolean>((resolve) => {
            this.client().listUserCosts({}).then((result) => {
                this.setState({listing: result, error: null}, () => resolve(true))
            }).catch((e) => {
                const message = e?.message ?? `${e}`
                this.setState({listing: {} as ListUserCostsResult, error: message}, () => {
                    this.props.onFlashbarChange({
                        items: [{type: 'error', header: 'Failed to load user costs', content: message, dismissible: true}]
                    })
                    resolve(false)
                })
            })
        })
    }

    onSelect(selected: UserCosts[]) {
        const username = selected[0]?.username
        this.setState({selected: selected, summary: null, costs: null, summaryLoading: username != null, splitPanelOpen: username != null})
        if (username == null) {
            return
        }
        Promise.all([this.client().getUserSummary({username: username}), this.client().getUserCosts({username: username})]).then(([summary, costs]) => {
            this.setState({summary: costs.current?.details ?? summary, costs: costs, summaryLoading: false})
        }).catch((e) => {
            this.setState({summary: {} as GetMyCostsSummaryResult, summaryLoading: false})
            this.props.onFlashbarChange({
                items: [{
                    type: 'error',
                    header: `Failed to load costs for ${username}`,
                    content: e?.message ?? `${e}`,
                    dismissible: true
                }]
            })
        })
    }

    buildSplitPanel() {
        const username = this.selectedUsername()
        return username == null ? undefined : <IdeaSplitPanel title={`Costs for ${username}`}>
            <SpaceBetween size="l">
                <CostsBillboard costs={this.state.costs}/>
                <CostSections
                    summary={this.state.summary}
                    loading={this.state.summaryLoading}
                    subject="user"
                    historicalStorageNotice={Boolean(this.state.listing?.storage_disabled || this.state.listing?.storage_unavailable)}/>
                <DailyCostCharts costs={this.state.costs}/>
            </SpaceBetween>
        </IdeaSplitPanel>
    }

    selectedUsername(): string | undefined {
        return this.state.selected[0]?.username
    }

    renderUnavailableNote() {
        const listing = this.state.listing
        const missing = [
            listing?.ai_unavailable ? 'AI usage' : null,
            listing?.desktops_unavailable ? 'desktops' : null,
            listing?.jobs_unavailable ? 'jobs' : null
        ].filter((entry) => entry != null)
        if (missing.length === 0) {
            return null
        }
        return (
            <Box variant="small" color="text-status-warning">
                {`Could not read ${missing.join(', ')}. Rows below are incomplete.`}
            </Box>
        )
    }

    storageSetupSteps() {
        return <div>
            <ol>
                <li>Enable cluster-manager.metrics.storage.enabled.</li>
                <li>For each ONTAP attachment, set shared-storage.&lt;name&gt;.fsx_netapp_ontap.metrics.username.</li>
                <li>Store the read-only account password in Secrets Manager and set metrics.password_secret_arn. Never enter the plaintext password here.</li>
                <li>Save the settings and restart cluster-manager.</li>
                <li>Wait for storage collection and the cost refresh, which normally run hourly.</li>
            </ol>
            <Box variant="small">
                The SVM HTTPS endpoint and quota and volume read permissions are required. Use the existing deployment or runbook permissions for secret reads and any applicable KMS decrypt permission. TLS verification remains enabled where configured.
            </Box>
        </div>
    }

    renderStorageStatus() {
        const listing = this.state.listing
        if (listing == null || this.state.error != null) {
            return null
        }
        if (listing.storage_unavailable) {
            return <Alert type="warning" header="Storage costs could not be loaded">Storage costs could not be loaded. Try again.</Alert>
        }
        if (!listing.storage_disabled && listing.storage_data_available === false) {
            return <Alert type="info" header="Storage costs are pending">Storage collection is enabled. Costs will appear after usage is collected and the cost data is refreshed.</Alert>
        }
        if (!listing.storage_disabled) {
            return null
        }
        if (listing.storage_configuration_status === 'unsupported') {
            const provider = listing.storage_metrics_provider || 'not set'
            return <Alert type="warning" header="Storage costs are not enabled">
                Storage metrics require CloudWatch or DogStatsD. The configured provider is {provider}.
            </Alert>
        }
        if (listing.storage_configuration_reason === 'no_ontap' || listing.storage_configuration_reason === 'efs_only') {
            return <Alert type="info" header="ONTAP storage metrics are unavailable">
                No ONTAP file systems are attached, so ONTAP quota metrics are unavailable here.
                {listing.storage_has_efs && ' EFS storage continues to use the existing measured-storage cost path in user cost details.'}
            </Alert>
        }
        if (listing.storage_configuration_reason === 'missing_credentials') {
            return <Alert
                type="warning"
                header="Complete storage cost setup"
                action={<Button href="#/cluster/settings/cost-collection?key=cluster-manager.metrics.storage.enabled">Set up storage costs</Button>}>
                <SpaceBetween size="s">
                    <div>Set the metrics username and password secret ARN for each ONTAP file system.</div>
                    {this.storageSetupSteps()}
                </SpaceBetween>
            </Alert>
        }
        return <Alert
            type="info"
            header="Storage costs are not enabled"
            action={<Button href="#/cluster/settings/cost-collection?key=cluster-manager.metrics.storage.enabled">Set up storage costs</Button>}>
            <SpaceBetween size="s">
                <div>Enable storage metrics and configure ONTAP credentials to collect storage usage for cost estimates.</div>
                {this.storageSetupSteps()}
            </SpaceBetween>
        </Alert>
    }

    renderListing() {
        return (
            <UsersTable
                listing={this.state.listing?.listing ?? []}
                loading={this.state.listing === null}
                selected={this.state.selected}
                onSelect={(selected) => this.onSelect(selected)}
                note={<SpaceBetween size="s">{this.renderStorageStatus()}{this.renderUnavailableNote()}</SpaceBetween>}
                storageDisabled={Boolean(this.state.listing?.storage_disabled)}
            />
        )
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
                    {text: 'IDEA', href: '#/'},
                    {text: 'Cluster Management', href: '#/cluster/status'},
                    {text: 'By user', href: ''}
                ]}
                header={
                    <Header
                        variant="h1"
                        description={this.state.listing?.storage_disabled
                            ? 'Estimated measurements: compute and AI cover the last 30 days. Storage is excluded from these subtotals.'
                            : 'Estimated measurements: compute and AI cover the last 30 days; storage covers the current month.'}>
                        By user
                    </Header>
                }
                contentType={"default"}
                content={
                    this.renderListing()
                }
                splitPanelOpen={this.state.splitPanelOpen}
                splitPanel={this.buildSplitPanel()}
                onSplitPanelToggle={(event: any) => this.setState({splitPanelOpen: event.detail.open})}/>
        )
    }
}

export default withRouter(UserCostsPage)
