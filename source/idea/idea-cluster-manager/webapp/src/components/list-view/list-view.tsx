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
    Button,
    ButtonDropdown,
    DateRangePicker,
    DateRangePickerProps,
    Flashbar,
    FlashbarProps,
    Header, PropertyFilterProps,
    SpaceBetween
} from "@cloudscape-design/components";
import {ButtonDropdownProps} from "@cloudscape-design/components/button-dropdown/interfaces";
import IdeaTable, {IdeaTableRef} from "../table";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {NonCancelableEventHandler} from "@cloudscape-design/components/internal/events";
import {SocaDateRange, SocaFilter, SocaListingPayload, SocaPaginator, SocaUserInputParamMetadata} from "../../client/data-model";
import Utils from "../../common/utils";
import {CollectionPreferencesProps} from "@cloudscape-design/components/collection-preferences/interfaces";

export interface IdeaListViewAction {
    id: string
    text: string
    onClick: () => void
    disabled?: boolean
    disabledReason?: string
}

export interface IdeaListViewProps<T = any> {
    title?: string
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
    showPreferences?: boolean
    onPreferenceChange?: (detail: CollectionPreferencesProps.Preferences<T>) => void
    preferencesKey?: string
    showFilters?: boolean
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
    onSelectionChange?: NonCancelableEventHandler<TableProps.SelectionChangeDetail<T>>
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
    enableExportToCsv?: boolean;
    csvFilename?: string | (() => string);
    onExportAllRecords?: () => Promise<any[]>;
}

export interface IdeaListViewState<T = any> {
    selectedItems: T[]
    listing: T[]
    loading: boolean
    flashBarItems: FlashbarProps.MessageDefinition[]
    currentPage: number
    totalPages: number
    dateRangePickerValue?: DateRangePickerProps.Value

    listingRequest: IdeaListingRequestType
}

export interface IdeaListingRequestType {
    filters: SocaFilter[]
    dateRange?: SocaDateRange
    paginator: SocaPaginator

    [k: string]: any;
}

class IdeaListView extends Component<IdeaListViewProps, IdeaListViewState> {

    table: React.RefObject<IdeaTableRef>

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
            dateRangePickerValue: this.props.dateRange,
            listingRequest: {
                filters: this.defaultFilters(),
                paginator: this.defaultPaginator(),
                dateRange: this.defaultDateRange()
            }
        }
    }

    private defaultFilters = () => {
        return []
    }

    private defaultPaginator = () => {
        if (this.props.paginator) {
            return this.props.paginator
        }
        return {
            start: 0,
            page_size: 30
        }
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
        this.fetchRecords()
    }

    fetchRecords() {
        if (this.props.onFetchRecords) {
            const onFetchRecords = this.props.onFetchRecords!
            this.setState({
                loading: true
            }, () => {
                onFetchRecords().then(result => {
                    const listing = (result.listing) ? result.listing : []
                    delete result.listing
                    this.table.current?.clearSelectedItems()
                    this.setState({
                        selectedItems: [],
                        listing: listing,
                        listingRequest: {
                            ...this.state.listingRequest,
                            ...result,
                            filters: (result.filters) ? result.filters : this.defaultFilters(),
                            paginator: (result.paginator) ? result.paginator : this.defaultPaginator(),
                            dateRange: (result.date_range) ? result.date_range : this.defaultDateRange()
                        }
                    })
                }).finally(() => {
                    this.setState({
                        loading: false
                    })
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

    getTotalPages(): number {
        if (this.isOpenEndedPaging()) {
            return this.state.totalPages
        }
        if (this.state.listingRequest.paginator.total != null) {
            return Math.ceil(this.state.listingRequest.paginator.total / this.getPageSize())
        }
        return 1
    }

    getFilters(): SocaFilter[] {
        return this.state.listingRequest.filters
    }

    getPaginator(): SocaPaginator {
        return this.state.listingRequest.paginator
    }

    getDateRange(): SocaDateRange | undefined {
        if (this.state.dateRangePickerValue) {
            return Utils.convertToDateRange(this.state.dateRangePickerValue)!
        }
        return undefined
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
                            listingRequest: {
                                ...this.state.listingRequest,
                                dateRange: undefined
                            }
                        }, () => {
                            this.fetchRecords()
                        })
                    } else {
                        this.setState({
                            listingRequest: {
                                ...this.state.listingRequest,
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
                dateTimeConstraintText:
                    "Range must be between 6 - 30 days. Use 24 hour format.",
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
                {this.props.onRefresh && <Button variant="normal" iconName="refresh" onClick={() => {
                    if (this.props.onRefresh) {
                        this.props.onRefresh()
                    }
                }}/>}
                {this.showDateRange() && this.buildDateRange()}
                {this.props.enableExportToCsv &&
                    <Button
                        variant="normal"
                        iconName="download"
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
    }

    buildTable() {
        return <IdeaTable
            ref={this.table}
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
                    listingRequest: {
                        ...this.state.listingRequest,
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
                        listingRequest: request,
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
            onPreferenceChange={(detail) => {
                let shouldReload = detail.pageSize !== this.state?.listingRequest?.paginator?.page_size
                this.setState({
                    listingRequest: {
                        ...this.state.listingRequest,
                        paginator: {
                            ...this.state.listingRequest.paginator,
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
            columnDefinitions={this.props.columnDefinitions}
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
        if (!this.props.columnDefinitions) {
            return;
        }

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

                    // First, check if there's a sortingField - this represents the raw data value
                    if ((col as any).sortingField && (item as any)[(col as any).sortingField] !== undefined) {
                        cellValue = String((item as any)[(col as any).sortingField] || '');
                    } else if (typeof col.cell === 'function') {
                        // If cell is a React component, try to extract text
                        const cellContent = col.cell(item);
                        if (React.isValidElement(cellContent)) {
                            // For React elements, use a simplified text extraction
                            cellValue = this.extractTextFromReactElement(cellContent);
                        } else {
                            cellValue = String(cellContent || '');
                        }
                    } else if (col.id && (item as any)[col.id] !== undefined) {
                        cellValue = String((item as any)[col.id] || '');
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
            // Don't show flash messages here as they can interfere with parent component's flash system
        }
    }

    /**
     * Attempt to extract text from a React element for CSV export
     */
    private extractTextFromReactElement(element: React.ReactElement): string {
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
                {this.buildTable()}
                {this.props.children}
            </div>
        )
    }
}

export default IdeaListView
