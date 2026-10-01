import {createContext, ReactNode, useContext, useState} from 'react';
import {useCollection} from '@cloudscape-design/collection-hooks';
import {Box, Button, CollectionPreferences, Header, Pagination, Table, TableProps, TextFilter, Select, SpaceBetween} from '@cloudscape-design/components';

export const ReportLoading = createContext(false);
export const ReportUser = createContext('');

export interface InsightColumn<T> extends TableProps.ColumnDefinition<T> {
    id: string;
    label: string;
    value: (row: T) => string | number | null | undefined;
}
export function downloadCsv(filename: string, content: string, type = 'text/csv;charset=utf-8') {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const link = document.createElement('a');
    try {link.href = url; link.download = filename; document.body.appendChild(link); link.click();}
    finally {link.remove(); URL.revokeObjectURL(url);}
}
export const csvCell = (value: unknown) => `"${(typeof value === 'string' ? value.replace(/^(\s*[=+@-])/, "'$1") : String(value ?? '')).replaceAll('"', '""')}"`;
export function readPreferences<T>(key: string, fallback: T): T {
    try {return JSON.parse(localStorage.getItem(key) ?? 'null') ?? fallback;} catch {return fallback;}
}
export function savePreferences(key: string, value: unknown) {
    try {localStorage.setItem(key, JSON.stringify(value));} catch { /* Storage can be disabled. */ }
}
export default function InsightsTable<T>({title, rows, columns, empty, defaultSort, hidden = [], tableId = title, description, jobSort = false}: {title: string; rows: T[]; columns: InsightColumn<T>[]; empty: string; defaultSort?: string; hidden?: string[]; tableId?: string; description?: ReactNode; jobSort?: boolean}) {
    const loading = useContext(ReportLoading);
    const username = useContext(ReportUser);
    const key = `reporting.table.${tableId}`;
    const defaults = columns.map(column => ({id: column.id, visible: !hidden.includes(column.id)}));
    const [display, setDisplay] = useState(() => readPreferences(key, defaults));
    const available = columns.filter(column => rows.some(row => column.value(row) != null));
    const definitions = available.map(column => ({...column, sortingField: column.id, sortingComparator: (a: T, b: T) => {
        const av = column.value(a), bv = column.value(b);
        if (av == null) return bv == null ? 0 : -1;
        if (bv == null) return 1;
        return typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv));
    }}));
    const {items, collectionProps, filterProps, paginationProps, filteredItemsCount, allPageItems, actions} = useCollection(rows, {
        filtering: {filteringFunction: (row, text) => columns.some(column => String(column.value(row) ?? '').toLowerCase().includes(text.toLowerCase())), empty: loading ? null : <Box>{empty}</Box>, noMatch: loading ? null : <Box>No matches</Box>},
        pagination: {pageSize: 25}, sorting: {defaultState: {sortingColumn: definitions.find(column => column.id === defaultSort) ?? definitions[0] ?? {}, isDescending: true}}
    });
    return <Table {...collectionProps} loading={loading && !rows.length} loadingText="Loading rows" stickyColumns={{first: 1}} items={items} columnDefinitions={definitions} columnDisplay={display.filter(column => available.some(item => item.id === column.id))}
        ariaLabels={{tableLabel: title}} header={<Header variant="h2" description={description} counter={`(${rows.length})`} actions={<Button disabled={!rows.length} onClick={() => {
            const selected = display.filter(column => column.visible).flatMap(column => available.filter(item => item.id === column.id));
            downloadCsv(`${title.toLowerCase().replaceAll(' ', '-')}${username ? `-${encodeURIComponent(username)}` : ''}.csv`, [selected.map(column => csvCell(column.label)).join(','), ...allPageItems.map(row => selected.map(column => csvCell(column.value(row))).join(','))].join('\r\n'));
        }}>Export CSV</Button>}>{title}</Header>}
        filter={<SpaceBetween size="s" direction="horizontal">{jobSort && <Select ariaLabel="Sort jobs" options={[{label: 'Highest cost', value: 'cost'}, {label: 'Most unused cores', value: 'wasted_core_hours'}]}
            selectedOption={{label: collectionProps.sortingColumn?.sortingField === 'wasted_core_hours' ? 'Most unused cores' : 'Highest cost', value: collectionProps.sortingColumn?.sortingField ?? 'cost'}}
            onChange={({detail}) => actions.setSorting({sortingColumn: definitions.find(column => column.id === detail.selectedOption.value) ?? {}, isDescending: true})}/>}
        <TextFilter {...filterProps} filteringAriaLabel={`Find in ${title}`} filteringPlaceholder="Find rows" countText={`${filteredItemsCount} matches`}/></SpaceBetween>}
        pagination={<Pagination {...paginationProps} ariaLabels={{nextPageLabel: 'Next page', previousPageLabel: 'Previous page', pageLabel: page => `Page ${page}`}}/>}
        preferences={<CollectionPreferences title="Table preferences" confirmLabel="Confirm" cancelLabel="Cancel" preferences={{contentDisplay: display}}
            contentDisplayPreference={{title: 'Columns', options: available.map(column => ({id: column.id, label: column.label}))}}
            onConfirm={({detail}) => {const next = [...(detail.contentDisplay ?? display)]; setDisplay(next); savePreferences(key, next);}}/>}/>;
}
