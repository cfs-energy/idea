/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

import React, {Component} from "react";
import {
    Alert,
    Box,
    Button,
    ButtonDropdown,
    DateRangePicker,
    DateRangePickerProps,
    Flashbar,
    FlashbarProps,
    Header, PropertyFilterProps,
    SpaceBetween,
    Toggle
} from "@cloudscape-design/components";
import {ButtonDropdownProps} from "@cloudscape-design/components/button-dropdown";
import IdeaTable, {IdeaTableRef} from "../table";
import {TableProps} from "@cloudscape-design/components/table";
import {SocaDateRange, SocaFilter, SocaListingPayload, SocaPaginator, SocaSortBy, SocaUserInputParamMetadata} from "../../client/data-model";
import Utils from "../../common/utils";
import {AppContext} from "../../common";
import {reportClientError} from "../../common/client-error-reporter";
import {CollectionPreferencesProps} from "@cloudscape-design/components/collection-preferences";

// the backing list calls contend with server-side locks, so a poll can never be
// faster than this no matter what a caller asks for
export const MIN_AUTO_REFRESH_INTERVAL_SECONDS = 30
const DEFAULT_PAGE_SIZE = 30
const MAX_REACHABLE_ROWS = 10000

export interface IdeaListViewAction {
    id: string
    text: string
    onClick: () => void
    disabled?: boolean
    disabledReason?: string
}

/** exportValue is what a CSV export writes for a cell that is not plain text. */
export type IdeaListViewColumn<T = any> = TableProps.ColumnDefinition<T> & {exportValue?: (item: T) => string}

export interface IdeaListViewProps<T = any> {
    title?: string
    empty?: React.ReactNode
    // shown instead of empty when a search found nothing
    emptySearch?: React.ReactNode
    description?: string
    primaryActionDisabled?: boolean
    primaryAction?: IdeaListViewAction
    secondaryActionsDisabled?: boolean
    secondaryActions?: IdeaListViewAction[]
    children?: React.ReactNode
    onRefresh?: () => void
    listing?: T[]
    selectedItems?: T[]
    selectionType?: TableProps.SelectionType
    onFetchRecords?: () => Promise<SocaListingPayload>
    // called after every successful fetch, once the selection has been cleared,
    // so the page can reconcile anything it tracks against the new listing
    onRecordsFetched?: (listing: T[]) => void
    showPreferences?: boolean
    onPreferenceChange?: (detail: CollectionPreferencesProps.Preferences<T>) => void
    preferencesKey?: string
    showFilters?: boolean
    filteringPlaceholder?: string
    filterType?: 'text' | 'property' | 'select'
    filters?: SocaFilter[]
    selectFilters?: SocaUserInputParamMetadata[]
    onFilter?: (filters: SocaFilter[]) => SocaFilter[]
    filteringOptions?: PropertyFilterProps.FilteringOption[]
    filteringProperties?: PropertyFilterProps.FilteringProperty[]
    onFilterPropertyChange?: (query: PropertyFilterProps.Query) => IdeaListingRequestType
    showDateRange?: boolean
    dateRange?: DateRangePickerProps.Value,
    onDateRange?: (dateRange: SocaDateRange) => SocaDateRange,
    onSelectionChange?: TableProps<T>['onSelectionChange']
    columnDefinitions?: ReadonlyArray<TableProps.ColumnDefinition<T>>
    showPaginator?: boolean
    paginator?: SocaPaginator
    disablePaginator?: boolean;
    currentPage?: number;
    totalPages?: number;
    openEndedPaging?: boolean;
    cursorBasedPaging?: boolean
    onPage?: (page: number, type: 'next' | 'prev' | 'page') => SocaPaginator;
    variant?: TableProps.Variant
    stickyHeader?: boolean
    defaultSortingColumn?: string;
    defaultSortingDescending?: boolean;
    // The columns the backend can order across every row: column id to the sort_by key. The
    // only sortable columns then; the page sends getSortBy() on its request. Without this
    // prop, sorting happens in the browser, so it is offered only when one page holds every row.
    serverSort?: {columns: {[columnId: string]: string}, defaultColumn: string, defaultDescending?: boolean}
    enableExportToCsv?: boolean;
    csvFilename?: string | (() => string);
    onExportAllRecords?: () => Promise<any[]>;
    showLastRefreshed?: boolean;
    enableAutoRefresh?: boolean;
}

export interface IdeaListViewState<T = any> {
    selectedItems: T[]
    listing: T[]
    loading: boolean
    flashBarItems: FlashbarProps.MessageDefinition[]
    currentPage: number
    totalPages: number
    dateRangePickerValue?: DateRangePickerProps.Value
    lastRefreshedAt?: Date
    fetchError?: string
    autoRefresh: boolean
    exporting: boolean
    sortBy?: SocaSortBy
    searching: boolean

    listingRequest: IdeaListingRequestType
}

export interface IdeaListingRequestType {
    filters: SocaFilter[]
    dateRange?: SocaDateRange
    paginator: SocaPaginator

    [k: string]: any;
}

class IdeaListView extends Component<IdeaListViewProps, IdeaListViewState> {

    table: React.RefObject<IdeaTableRef | null>
    private autoRefreshTimer?: ReturnType<typeof setInterval>
    private latestFetchId = 0

    constructor(props: IdeaListViewProps) {
        super(props);
        this.table = React.createRef()

        this.state = {
            listing: (props.listing) ? props.listing : [],
            selectedItems: (props.selectedItems) ? props.selectedItems : [],
            loading: true,
            flashBarItems: [],
            currentPage: 1,
            totalPages: 1,
            autoRefresh: false,
            exporting: false,
            searching: false,
            sortBy: this.defaultSortBy(),
            dateRangePickerValue: this.props.dateRange,
            listingRequest: {
                filters: this.defaultFilters(),
                paginator: this.defaultPaginator(),
                dateRange: this.defaultDateRange()
            }
        }
    }

    private defaultSortBy = (): SocaSortBy | undefined => {
        const serverSort = this.props.serverSort
        if (!serverSort) {
            return undefined
        }
        return {key: serverSort.columns[serverSort.defaultColumn], order: serverSort.defaultDescending ? 'desc' : 'asc'}
    }

    private defaultFilters = () => {
        return []
    }

    private defaultPaginator = () => {
        const paginator = this.props.paginator ?? {start: 0, page_size: DEFAULT_PAGE_SIZE}
        const saved = this.savedPageSize()
        return saved == null ? paginator : {...paginator, page_size: saved}
    }

    // Only a page that offers the preference has one to honour.
    private savedPageSize(): number | null {
        if (!this.props.showPreferences || !this.props.preferencesKey) {
            return null
        }
        const saved = Number(AppContext.get().localStorage().getItem(`${this.props.preferencesKey}-table-pageSize`))
        return Number.isInteger(saved) && saved > 0 ? saved : null
    }

    private defaultDateRange = () => {
        if (this.state?.dateRangePickerValue && this.props.onDateRange) {
            const ideaDateRange = Utils.convertToDateRange(this.state.dateRangePickerValue)
            if (ideaDateRange == null) {
                return undefined
            }
            return this.props.onDateRange(ideaDateRange)
        }
        return undefined
    }

    resetState(): Promise<boolean> {
        return new Promise((resolve) => {
            this.table.current!.reset()
            this.setState({
                listing: [],
                listingRequest: {
                    paginator: this.defaultPaginator(),
                    filters: this.defaultFilters(),
                },
                selectedItems: [],
                currentPage: 1,
                totalPages: 1
            }, () => {
                resolve(true)
            })
        })
    }

    getSelectedItems<T = any>(): T[] {
        return this.state.selectedItems
    }

    getSelectedItem<T = any>(): T | null {
        if (this.state.selectedItems.length > 0) {
            return JSON.parse(JSON.stringify(this.state.selectedItems[0]))
        }
        return null
    }

    setSelectedItems(items: any[], onSelected?: () => void) {
        this.table.current?.setSelectedItems(items)
        this.setState({
            selectedItems: items
        }, onSelected)
    }

    isAnySelected(): boolean {
        return this.state.selectedItems.length > 0
    }

    isCursorBasedPaging(): boolean {
        if (this.props.cursorBasedPaging != null) {
            return this.props.cursorBasedPaging
        }
        return false
    }

    /**
     * Relies on props. Should not take state into account.
     * state is updated to non-open ended once cursor is not found
     */
    isOpenEndedPaging(): boolean {
        if (this.props.openEndedPaging != null) {
            return this.props.openEndedPaging
        }
        return false
    }

    componentDidMount() {
        document.addEventListener('visibilitychange', this.onVisibilityChange)
        this.fetchRecords()
    }

    componentWillUnmount() {
        document.removeEventListener('visibilitychange', this.onVisibilityChange)
        this.stopAutoRefresh()
    }

    isAutoRefreshEnabled(): boolean {
        return this.props.enableAutoRefresh === true
    }

    private onVisibilityChange = () => {
        if (document.hidden) {
            this.stopAutoRefresh()
        } else if (this.state.autoRefresh) {
            this.startAutoRefresh()
        }
    }

    private startAutoRefresh() {
        this.stopAutoRefresh()
        this.autoRefreshTimer = setInterval(() => {
            // never stack a request on one already in flight, and stay off the
            // backend entirely while the tab is in the background
            if (this.state.loading || document.hidden) {
                return
            }
            this.fetchRecords()
        }, MIN_AUTO_REFRESH_INTERVAL_SECONDS * 1000)
    }

    private stopAutoRefresh() {
        if (this.autoRefreshTimer != null) {
            clearInterval(this.autoRefreshTimer)
            this.autoRefreshTimer = undefined
        }
    }

    getLastRefreshedLabel(): string {
        if (this.state.lastRefreshedAt == null) {
            return this.state.fetchError ? 'Not loaded' : 'Loading...'
        }
        return `Last updated ${this.state.lastRefreshedAt.toLocaleTimeString()}`
    }

    fetchRecords() {
        if (this.props.onFetchRecords) {
            const onFetchRecords = this.props.onFetchRecords!
            const fetchId = ++this.latestFetchId
            this.setState({
                loading: true
            }, () => {
                onFetchRecords().then(result => {
                    // paging or filtering again before this answer arrived: this one is out of date
                    if (fetchId !== this.latestFetchId) {
                        return
                    }
                    const listing = (result.listing) ? result.listing : []
                    delete result.listing
                    this.table.current?.clearSelectedItems()
                    this.setState({
                        selectedItems: [],
                        listing: listing,
                        fetchError: undefined,
                        // stamped on success only: a failed refresh must not make
                        // the data on screen look newer than it is
                        lastRefreshedAt: new Date(),
                        listingRequest: {
                            ...this.state.listingRequest,
                            ...result,
                            filters: result.filters ?? this.state.listingRequest.filters,
                            paginator: (result.paginator) ? result.paginator : this.defaultPaginator(),
                            dateRange: (result.date_range) ? result.date_range : this.defaultDateRange()
                        }
                    }, () => {
                        if (this.props.onRecordsFetched) {
                            this.props.onRecordsFetched(listing)
                        }
                    })
                }).catch((error) => {
                    // leaves the previous listing and its last-updated time in place
                    console.error('Failed to fetch records:', error)
                    if (fetchId !== this.latestFetchId) {
                        return
                    }
                    this.setState({fetchError: error?.message || 'The request did not complete.'})
                    reportClientError('list_load_failed', {error})
                }).finally(() => {
                    if (fetchId === this.latestFetchId) {
                        this.setState({
                            loading: false
                        })
                    }
                })
            })
        }
    }

    getPageSize(): number {
        if (this.state.listingRequest.paginator.page_size != null) {
            return this.state.listingRequest.paginator.page_size
        }
        return 50
    }

    // the search index stops at 10,000 rows however many it counts: a page past that fails
    private reachablePages(): number {
        return Math.floor(MAX_REACHABLE_ROWS / this.getPageSize())
    }

    isPastReach(): boolean {
        return (this.state.listingRequest.paginator.total ?? 0) > MAX_REACHABLE_ROWS
    }

    getTotalPages(): number {
        if (this.isOpenEndedPaging()) {
            return this.state.totalPages
        }
        if (this.state.listingRequest.paginator.total != null) {
            return Math.min(Math.ceil(this.state.listingRequest.paginator.total / this.getPageSize()), this.reachablePages())
        }
        return 1
    }

    getFilters(): SocaFilter[] {
        return this.state.listingRequest.filters
    }

    getPaginator(): SocaPaginator {
        return this.state.listingRequest.paginator
    }

    // onDateRange names the field the range applies to: without it the backend filters on
    // no field at all and a page shows no rows.
    getDateRange(): SocaDateRange | undefined {
        const dateRange = Utils.convertToDateRange(this.state.dateRangePickerValue)
        if (dateRange == null) {
            return undefined
        }
        return this.props.onDateRange ? this.props.onDateRange(dateRange) : dateRange
    }

    /** The chosen range in words, for messages about it. */
    getDateRangeLabel(): string {
        const value = this.state.dateRangePickerValue
        if (value == null) {
            return 'all dates'
        }
        if (value.type === 'absolute') {
            return `${value.startDate.slice(0, 10)} to ${value.endDate.slice(0, 10)}`
        }
        return value.amount === 1 ? `the last ${value.unit}` : `the last ${value.amount} ${value.unit}s`
    }

    // A new search, range or page size is a new list: an offset or cursor from the old one
    // would land past its end and show no rows while the pager says page 1.
    private firstPage(paginator: SocaPaginator = this.state.listingRequest.paginator): SocaPaginator {
        return {...paginator, start: 0, cursor: undefined}
    }

    getSortBy(): SocaSortBy | undefined {
        return this.state.sortBy
    }

    private holdsEveryRow(): boolean {
        return this.state.currentPage === 1 && this.getTotalPages() <= 1 && !this.hasMorePages()
    }

    private usesClientSort(): boolean {
        return !this.props.serverSort && this.holdsEveryRow()
    }

    // A column that cannot order every row offers no sort at all: an arrow over one page reads as the whole list.
    private sortableColumns(): IdeaListViewProps['columnDefinitions'] {
        const serverColumns = this.props.serverSort?.columns
        return this.props.columnDefinitions?.map(column => {
            const serverKey = serverColumns?.[column.id!]
            if (serverKey) {
                return {...column, sortingField: serverKey, sortingComparator: undefined}
            }
            return this.usesClientSort() ? column : {...column, sortingField: undefined, sortingComparator: undefined}
        })
    }

    private buildServerSorting() {
        if (!this.props.serverSort) {
            return undefined
        }
        return {
            sortingColumn: {sortingField: this.state.sortBy?.key},
            sortingDescending: this.state.sortBy?.order === 'desc',
            onSortingChange: (event: {detail: TableProps.SortingState<any>}) => {
                this.setState({
                    sortBy: {key: event.detail.sortingColumn.sortingField, order: event.detail.isDescending ? 'desc' : 'asc'},
                    currentPage: 1,
                    totalPages: 1,
                    listingRequest: {
                        ...this.state.listingRequest,
                        paginator: this.firstPage()
                    }
                }, () => {
                    this.fetchRecords()
                })
            }
        }
    }

    getListingRequest(): IdeaListingRequestType {
        return this.state.listingRequest
    }

    hasMorePages(): boolean {
        if (this.isCursorBasedPaging()) {
            return Utils.isNotEmpty(this.state.listingRequest.paginator.cursor)
        } else if (this.isOpenEndedPaging()) {
            return this.state.listing.length >= this.state.listingRequest.paginator.page_size!
        }
        return false
    }

    showDateRange(): boolean {
        if (this.props.showDateRange != null) {
            return this.props.showDateRange
        }
        return false
    }

    private buildDateRange() {
        return <DateRangePicker
            value={this.state.dateRangePickerValue!}
            relativeOptions={[
                {
                    key: "previous-1-hour",
                    amount: 1,
                    unit: "hour",
                    type: "relative"
                },
                {
                    key: "previous-12-hours",
                    amount: 12,
                    unit: "hour",
                    type: "relative"
                },
                {
                    key: "previous-1-day",
                    amount: 1,
                    unit: "day",
                    type: "relative"
                },
                {
                    key: "previous-1-month",
                    amount: 1,
                    unit: "month",
                    type: "relative"
                },
                {
                    key: "previous-3-months",
                    amount: 3,
                    unit: "month",
                    type: "relative"
                },
                {
                    key: "previous-1-year",
                    amount: 1,
                    unit: "year",
                    type: "relative"
                }
            ]}
            isValidRange={(value) => {
                return {
                    valid: true
                }
            }}
            onChange={(event) => {
                this.setState({
                    dateRangePickerValue: event.detail.value!
                }, () => {
                    if (!this.props.onDateRange) {
                        return
                    }
                    const value = event.detail.value
                    const dateRange = Utils.convertToDateRange(value)
                    if (dateRange == null) {
                        this.setState({
                            currentPage: 1,
                            totalPages: 1,
                            listingRequest: {
                                ...this.state.listingRequest,
                                paginator: this.firstPage(),
                                dateRange: undefined
                            }
                        }, () => {
                            this.fetchRecords()
                        })
                    } else {
                        this.setState({
                            currentPage: 1,
                            totalPages: 1,
                            listingRequest: {
                                ...this.state.listingRequest,
                                paginator: this.firstPage(),
                                dateRange: this.props.onDateRange(dateRange)
                            }
                        }, () => {
                            this.fetchRecords()
                        })
                    }
                })
            }}
            i18nStrings={{
                todayAriaLabel: "Today",
                nextMonthAriaLabel: "Next month",
                previousMonthAriaLabel: "Previous month",
                customRelativeRangeDurationLabel: "Duration",
                customRelativeRangeDurationPlaceholder:
                    "Enter duration",
                customRelativeRangeOptionLabel: "Custom range",
                customRelativeRangeOptionDescription:
                    "Set a custom range in the past",
                customRelativeRangeUnitLabel: "Unit of time",
                formatRelativeRange: e => {
                    const t =
                        1 === e.amount ? e.unit : `${e.unit}s`;
                    return `Last ${e.amount} ${t}`;
                },
                formatUnit: (e, t) => (1 === t ? e : `${e}s`),
                dateTimeConstraintText: "Use 24 hour format.",
                relativeModeTitle: "Relative range",
                absoluteModeTitle: "Absolute range",
                relativeRangeSelectionHeading: "Choose a range",
                startDateLabel: "Start date",
                endDateLabel: "End date",
                startTimeLabel: "Start time",
                endTimeLabel: "End time",
                clearButtonLabel: "Clear",
                cancelButtonLabel: "Cancel",
                applyButtonLabel: "Apply"
            }}
        />
    }

    private buildActions() {

        const secondaryActions: ButtonDropdownProps.ItemOrGroup[] = []
        if (this.props.secondaryActions) {
            this.props.secondaryActions.forEach((action) => {
                secondaryActions.push({
                    id: action.id,
                    text: action.text,
                    disabled: action.disabled,
                    disabledReason: action.disabledReason
                })
            })
        }

        // Remove export to CSV from secondaryActions as it will have its own button

        return (
            <SpaceBetween direction="horizontal"
                          size="xs">
                {this.props.showLastRefreshed &&
                    <Box variant="small" color="text-status-inactive" padding={{top: 'xs'}}>
                        {this.getLastRefreshedLabel()}
                    </Box>}
                {this.isAutoRefreshEnabled() &&
                    <Box padding={{top: 'xs'}}>
                        <Toggle checked={this.state.autoRefresh}
                                onChange={(event) => {
                                    this.setState({
                                        autoRefresh: event.detail.checked
                                    }, () => {
                                        if (this.state.autoRefresh) {
                                            this.startAutoRefresh()
                                        } else {
                                            this.stopAutoRefresh()
                                        }
                                    })
                                }}>
                            {`Auto refresh every ${MIN_AUTO_REFRESH_INTERVAL_SECONDS}s`}
                        </Toggle>
                    </Box>}
                {this.props.onRefresh && <Button variant="normal" iconName="refresh" ariaLabel="Refresh" onClick={() => {
                    if (this.props.onRefresh) {
                        this.props.onRefresh()
                    }
                }}/>}
                {this.showDateRange() && this.buildDateRange()}
                {this.props.enableExportToCsv &&
                    <Button
                        variant="normal"
                        iconName="download"
                        loading={this.state.exporting}
                        disabled={(!this.props.onFetchRecords && !this.props.onExportAllRecords) || this.state.loading}
                        onClick={() => this.exportToCsv()}>
                        CSV Export
                    </Button>
                }
                {secondaryActions.length > 0 &&
                    <ButtonDropdown disabled={this.props.secondaryActionsDisabled} items={secondaryActions} onItemClick={(event) => {
                        this.props.secondaryActions?.forEach((value => {
                            if (value.id === event.detail.id) {
                                if (value.onClick != null) {
                                    value.onClick()
                                }
                            }
                        }))
                    }}>Actions</ButtonDropdown>}
                {this.props.primaryAction && <Button disabled={this.props.primaryActionDisabled} variant="primary" onClick={() => {
                    if (this.props.primaryAction?.onClick) {
                        this.props.primaryAction.onClick()
                    }
                }}>{this.props.primaryAction?.text}</Button>}
            </SpaceBetween>
        )
    }

    getCounter() {
        if (this.state.listingRequest && this.state.listingRequest.paginator) {
            if (typeof this.state.listingRequest.paginator.total !== 'undefined') {
                return `(${this.state.listingRequest.paginator.total})`
            }
        }
        // a cursor-paged API sends no total: the first page is the whole list when there is no next one
        if (this.state.lastRefreshedAt != null && this.state.currentPage === 1 && !this.hasMorePages()
            && Utils.isEmpty(this.state.listingRequest?.paginator?.cursor)) {
            return `(${this.state.listing.length})`
        }
    }

    buildTable() {
        return <IdeaTable
            ref={this.table}
            empty={this.state.fetchError ? <Box textAlign="center" color="inherit">This list could not be loaded.</Box>
                : this.state.searching && this.props.emptySearch ? this.props.emptySearch : this.props.empty}
            header={
                <Header
                    variant="awsui-h1-sticky"
                    counter={this.getCounter()}
                    description={this.props.description}
                    actions={this.buildActions()}>{this.props.title}
                </Header>}
            variant={this.props.variant}
            stickyHeader={this.props.stickyHeader}
            loading={this.state.loading}
            loadingText="Loading"
            listing={this.state.listing}
            selectedItems={this.props.selectedItems}
            selectionType={this.props.selectionType}
            showFilters={this.props.showFilters}
            filterType={this.props.filterType}
            filters={this.props.filters}
            selectFilters={this.props.selectFilters}
            filteringProperties={this.props.filteringProperties}
            filteringOptions={this.props.filteringOptions}
            defaultSortingColumn={this.props.defaultSortingColumn}
            defaultSortingDescending={this.props.defaultSortingDescending}
            serverSorting={this.buildServerSorting()}
            clientSortingDisabled={!this.usesClientSort()}
            onFilter={(filters) => {
                let applicableFilters
                if (this.props.onFilter) {
                    applicableFilters = this.props.onFilter(filters)
                } else {
                    applicableFilters = filters
                }
                this.setState({
                    listing: [],
                    selectedItems: [],
                    currentPage: 1,
                    totalPages: 1,
                    searching: applicableFilters.some(filter => Utils.isNotEmpty(filter.value)),
                    listingRequest: {
                        ...this.state.listingRequest,
                        paginator: this.firstPage(),
                        filters: applicableFilters
                    }
                }, () => {
                    this.fetchRecords()
                })
            }}
            onPropertyFilterChange={(query) => {
                if (this.props.onFilterPropertyChange) {
                    const request = this.props.onFilterPropertyChange(query)
                    this.setState({
                        listing: [],
                        listingRequest: {...request, paginator: this.firstPage(request.paginator)},
                        selectedItems: [],
                        currentPage: 1,
                        totalPages: 1
                    }, () => {
                        this.fetchRecords()
                    })
                }
            }}
            showPreferences={this.props.showPreferences}
            preferencesKey={this.props.preferencesKey}
            defaultPageSize={this.props.paginator?.page_size ?? DEFAULT_PAGE_SIZE}
            filteringPlaceholder={this.props.filteringPlaceholder}
            onPreferenceChange={(detail) => {
                let shouldReload = detail.pageSize !== this.state?.listingRequest?.paginator?.page_size
                this.setState({
                    currentPage: shouldReload ? 1 : this.state.currentPage,
                    totalPages: shouldReload ? 1 : this.state.totalPages,
                    listingRequest: {
                        ...this.state.listingRequest,
                        paginator: {
                            ...(shouldReload ? this.firstPage() : this.state.listingRequest.paginator),
                            page_size: detail.pageSize
                        }
                    }
                }, () => {
                    if (this.props.onPreferenceChange) {
                        this.props.onPreferenceChange(detail)
                    }

                    if (shouldReload) {
                        this.fetchRecords()
                    }
                })
            }}
            onSelectionChange={(event) => {
                this.setState({
                    selectedItems: event.detail.selectedItems
                }, () => {
                    if (this.props.onSelectionChange) {
                        this.props.onSelectionChange(event)
                    }
                })
            }}
            columnDefinitions={this.sortableColumns()}
            showPaginator={this.props.showPaginator}
            currentPage={this.state.currentPage}
            totalPages={this.getTotalPages()}
            openEndPaging={this.hasMorePages()}
            onPage={(page, type) => {

                let totalPages = this.state.totalPages
                if (this.isOpenEndedPaging()) {
                    if (type === 'next' && this.hasMorePages()) {
                        totalPages = page
                    }
                }

                this.setState({
                    currentPage: page,
                    totalPages: totalPages
                }, () => {
                    if (this.props.onPage) {
                        const paginator = this.props.onPage(page, type)
                        this.setState({
                            listingRequest: {
                                ...this.state.listingRequest,
                                paginator: paginator
                            }
                        }, () => {
                            this.fetchRecords()
                        })
                    } else {
                        const paginator = this.getPaginator()
                        this.setState({
                            listingRequest: {
                                ...this.state.listingRequest,
                                paginator: {
                                    ...paginator,
                                    start: (page - 1) * paginator.page_size!
                                }
                            }
                        }, () => {
                            this.fetchRecords()
                        })
                    }
                })
            }}
        />
    }

    // A failed fetch must not read as "no records": say so, keep what was on screen, and offer the retry.
    buildFetchError() {
        const shownAt = this.state.listing.length > 0 ? this.state.lastRefreshedAt : undefined
        return <Alert type="error"
                      header="This list could not be loaded"
                      action={<Button onClick={() => this.fetchRecords()}>Try again</Button>}>
            {this.state.fetchError}{shownAt && ` The rows below are from ${shownAt.toLocaleTimeString()}.`}
        </Alert>
    }

    buildFlashBar() {
        return <Flashbar items={this.state.flashBarItems}
        />
    }

    setFlashMessage(message: string | React.ReactNode, type: FlashbarProps.Type = 'info') {
        this.setState({
            flashBarItems: [
                {
                    content: message,
                    dismissible: true,
                    type: type,
                    onDismiss: () => {
                        this.setState({
                            flashBarItems: []
                        })
                    }
                }
            ]
        })
    }

    setFlashMessages(items: FlashbarProps.MessageDefinition[]) {

    }

    /**
     * Export all records to CSV (fetches all data regardless of current pagination)
     */
    private async exportToCsv() {
        if (!this.props.columnDefinitions || this.state.exporting) {
            return;
        }
        this.setState({exporting: true});

        const columnDefinitions = this.props.columnDefinitions;

        try {
            let allRecords = this.state.listing;

            // If there's a custom export function, use it
            if (this.props.onExportAllRecords) {
                allRecords = await this.props.onExportAllRecords();
            } else if (this.props.onFetchRecords) {
                // Fallback to the original logic for backward compatibility
                const currentTotal = this.state.listingRequest?.paginator?.total || this.state.listing.length;

                // If we have more records than currently loaded, fetch all records
                if (currentTotal > this.state.listing.length) {
                    // Create a request with a very large page size to get all records
                    const exportRequest = {
                        ...this.state.listingRequest,
                        paginator: {
                            ...this.state.listingRequest.paginator,
                            start: 0,
                            page_size: Math.max(currentTotal, 10000) // Use total count or 10k as fallback
                        }
                    };

                    // Temporarily store current request state
                    const originalRequest = { ...this.state.listingRequest };

                    // Update state to use export pagination and wait for it to complete
                    await new Promise<void>((resolve) => {
                        this.setState({
                            listingRequest: exportRequest
                        }, resolve);
                    });

                    // Fetch all records (now that state is updated)
                    const result = await this.props.onFetchRecords();
                    allRecords = result.listing || [];

                    // Restore original pagination state
                    await new Promise<void>((resolve) => {
                        this.setState({
                            listingRequest: originalRequest
                        }, resolve);
                    });
                }
            }

            // Filter out columns that shouldn't be exported (like interactive UI elements)
            const exportableColumns = columnDefinitions.filter(col =>
                col.id !== 'connect-session' // Exclude join session column
            );

            // Create CSV header row
            const headers = exportableColumns
                .map(col => col.header)
                .join(',');

            // Create CSV data rows
            const rows = allRecords.map(item => {
                return exportableColumns.map(col => {
                    // Get raw cell value
                    let cellValue = '';

                    // A column whose cell is not plain text says what to export. Otherwise a
                    // sortingField is the raw data value.
                    if ((col as IdeaListViewColumn).exportValue) {
                        cellValue = (col as IdeaListViewColumn).exportValue!(item);
                    } else if ((col as any).sortingField && (item as any)[(col as any).sortingField] !== undefined) {
                        cellValue = String((item as any)[(col as any).sortingField] ?? '');
                    } else if (typeof col.cell === 'function') {
                        // If cell is a React component, try to extract text
                        const cellContent = col.cell(item);
                        if (React.isValidElement(cellContent)) {
                            // For React elements, use a simplified text extraction
                            cellValue = this.extractTextFromReactElement(cellContent);
                        } else {
                            cellValue = String(cellContent ?? '');
                        }
                    } else if (col.id && (item as any)[col.id] !== undefined) {
                        cellValue = String((item as any)[col.id] ?? '');
                    }

                    // Escape CSV value: wrap in quotes and escape existing quotes
                    return '"' + cellValue.replace(/"/g, '""') + '"';
                }).join(',');
            }).join('\n');

            // Combine headers and rows
            const csvContent = headers + '\n' + rows;

            // Create download link
            const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.setAttribute('href', url);

            // Handle both string and function types for csvFilename
            const filename = typeof this.props.csvFilename === 'function'
                ? this.props.csvFilename()
                : (this.props.csvFilename || `${this.props.title || 'export'}.csv`);

            link.setAttribute('download', filename);
            link.style.visibility = 'hidden';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);

        } catch (error) {
            console.error('CSV Export failed:', error);
            this.setFlashMessage(`CSV export failed: ${(error as any)?.message || 'the request did not complete.'} Try again.`, 'error');
        } finally {
            this.setState({exporting: false});
        }
    }

    /**
     * Attempt to extract text from a React element for CSV export
     */
    private extractTextFromReactElement(element: React.ReactElement<any>): string {
        // Simple implementation to extract text from React elements
        if (!element) return '';

        // Extract props.children and convert to string
        if (element.props && element.props.children) {
            if (typeof element.props.children === 'string') {
                return element.props.children;
            } else if (Array.isArray(element.props.children)) {
                return element.props.children
                    .map((child: React.ReactNode) => {
                        if (typeof child === 'string') return child;
                        if (React.isValidElement(child)) return this.extractTextFromReactElement(child);
                        return '';
                    })
                    .join(' ');
            } else if (React.isValidElement(element.props.children)) {
                return this.extractTextFromReactElement(element.props.children);
            }
        }

        // For components that might have a 'value' or 'text' prop
        if (element.props && (element.props.value || element.props.text)) {
            return String(element.props.value || element.props.text || '');
        }

        // If we can't extract anything useful, return empty string
        return '';
    }

    render() {
        return (
            <div>
                {this.state.flashBarItems.length > 0 && this.buildFlashBar()}
                {this.state.fetchError && this.buildFetchError()}
                {this.buildTable()}
                {this.isPastReach() && <Box variant="small" color="text-body-secondary" padding={{top: 'xs'}}>Pages stop at the first 10,000 rows. Search or narrow the date range to see the rest.</Box>}
                {this.props.children}
            </div>
        )
    }
}

export default IdeaListView
