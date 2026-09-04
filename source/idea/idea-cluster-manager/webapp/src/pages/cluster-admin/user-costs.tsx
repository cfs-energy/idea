import React, {Component} from "react";
import {Box, Container, Header, Pagination, SpaceBetween, Table, TextFilter} from "@cloudscape-design/components";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {useCollection} from "@cloudscape-design/collection-hooks";
import {GetMyCostsSummaryResult, ListUserCostsResult, UserCosts} from "../../client/data-model";
import {IdeaSideNavigationProps} from "../../components/side-navigation";
import IdeaAppLayout, {IdeaAppLayoutProps} from "../../components/app-layout";
import CostSections, {ESTIMATED_NOTE, hours, money, number, summaryCost} from "../../components/cost-sections";
import {withRouter} from "../../navigation/navigation-utils";
import {AppContext} from "../../common";

export interface UserCostsProps extends IdeaAppLayoutProps, IdeaSideNavigationProps {
}

export interface UserCostsState {
    listing: ListUserCostsResult | null
    selected: UserCosts[]
    summary: GetMyCostsSummaryResult | null
    summaryLoading: boolean
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
    {id: 'total_cost', header: 'Total', sortingField: 'total_cost', cell: (item) => money(item.total_cost)}
]

// Matches the page size the other administration tables use.
export const USER_COSTS_PAGE_SIZE = 30

const TOTAL_COLUMN = USER_COLUMNS[USER_COLUMNS.length - 1]

const EMPTY_STATE = (
    <Box textAlign="center" color="inherit">
        <b>No measured costs</b>
        <Box variant="p" color="inherit">
            IDEA recorded no AI usage, desktop hours or completed jobs for anyone in the last 30 days.
        </Box>
    </Box>
)

const NO_MATCH_STATE = (
    <Box textAlign="center" color="inherit">
        <b>No matching users</b>
        <Box variant="p" color="inherit">No user in the window matches that name.</Box>
    </Box>
)

interface UsersTableProps {
    listing: UserCosts[]
    loading: boolean
    selected: UserCosts[]
    onSelect: (selected: UserCosts[]) => void
    note: React.ReactNode
}

/**
 * The users table, filtered, sorted and paged in the browser. The listing is one small
 * row per user and arrives in a single read, so narrowing it needs no round trip.
 */
const UsersTable: React.FC<UsersTableProps> = ({listing, loading, selected, onSelect, note}) => {

    const {items, collectionProps, filterProps, paginationProps, filteredItemsCount} = useCollection(listing, {
        filtering: {
            filteringFunction: (item, filteringText) =>
                (item.username ?? '').toLowerCase().includes(filteringText.trim().toLowerCase()),
            empty: EMPTY_STATE,
            noMatch: NO_MATCH_STATE
        },
        // Biggest spender first.
        sorting: {defaultState: {sortingColumn: TOTAL_COLUMN, isDescending: true}},
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
                    columnDefinitions={USER_COLUMNS}
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
        this.state = {listing: null, selected: [], summary: null, summaryLoading: false, error: null}
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
        this.setState({selected: selected, summary: null, summaryLoading: username != null})
        if (username == null) {
            return
        }
        this.client().getUserSummary({username: username}).then((result) => {
            this.setState({summary: result, summaryLoading: false})
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

    renderListing() {
        return (
            <UsersTable
                listing={this.state.listing?.listing ?? []}
                loading={this.state.listing === null}
                selected={this.state.selected}
                onSelect={(selected) => this.onSelect(selected)}
                note={this.renderUnavailableNote()}
            />
        )
    }

    render() {
        const username = this.selectedUsername()
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
                    {text: 'User Costs', href: ''}
                ]}
                header={
                    <Header
                        variant="h1"
                        description={`${ESTIMATED_NOTE} Window: the last 30 days.`}>
                        User Costs
                    </Header>
                }
                contentType={"default"}
                content={
                    <SpaceBetween size="l">
                        {this.renderListing()}
                        {username != null &&
                            <SpaceBetween size="l">
                                <Header variant="h2">{`Costs for ${username}`}</Header>
                                <CostSections
                                    summary={this.state.summary}
                                    loading={this.state.summaryLoading}
                                    subject="user"
                                />
                            </SpaceBetween>}
                    </SpaceBetween>
                }/>
        )
    }
}

export default withRouter(UserCostsPage)
