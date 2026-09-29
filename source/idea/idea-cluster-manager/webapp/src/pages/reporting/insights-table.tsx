import {useState} from 'react';
import {useCollection} from '@cloudscape-design/collection-hooks';
import {Box, Button, CollectionPreferences, Header, Pagination, Table, TableProps, TextFilter} from '@cloudscape-design/components';

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
export default function InsightsTable<T>({title, rows, columns, empty, defaultSort}: {title: string; rows: T[]; columns: InsightColumn<T>[]; empty: string; defaultSort?: string}) {
    const [display, setDisplay] = useState(columns.map(column => ({id: column.id, visible: true})));
    const available = columns.filter(column => rows.some(row => column.value(row) != null));
    const definitions = available.map(column => ({...column, sortingComparator: (a: T, b: T) => {
        const av = column.value(a), bv = column.value(b);
        if (av == null) return bv == null ? 0 : -1;
        if (bv == null) return 1;
        return typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv));
    }}));
    const {items, collectionProps, filterProps, paginationProps, filteredItemsCount, allPageItems} = useCollection(rows, {
        filtering: {filteringFunction: (row, text) => columns.some(column => String(column.value(row) ?? '').toLowerCase().includes(text.toLowerCase())), empty: <Box>{empty}</Box>, noMatch: <Box>No matches</Box>},
        pagination: {pageSize: 25}, sorting: {defaultState: {sortingColumn: definitions.find(column => column.id === defaultSort) ?? definitions[0] ?? {}, isDescending: true}}
    });
    return <Table {...collectionProps} items={items} columnDefinitions={definitions} columnDisplay={display.filter(column => available.some(item => item.id === column.id))}
        ariaLabels={{tableLabel: title}} header={<Header variant="h2" counter={`(${rows.length})`} actions={<Button disabled={!rows.length} onClick={() => {
            const selected = display.filter(column => column.visible).flatMap(column => available.filter(item => item.id === column.id));
            downloadCsv(`${title.toLowerCase().replaceAll(' ', '-')}.csv`, [selected.map(column => csvCell(column.label)).join(','), ...allPageItems.map(row => selected.map(column => csvCell(column.value(row))).join(','))].join('\r\n'));
        }}>Export CSV</Button>}>{title}</Header>}
        filter={<TextFilter {...filterProps} filteringAriaLabel={`Find in ${title}`} filteringPlaceholder="Find rows" countText={`${filteredItemsCount} matches`}/>}
        pagination={<Pagination {...paginationProps} ariaLabels={{nextPageLabel: 'Next page', previousPageLabel: 'Previous page', pageLabel: page => `Page ${page}`}}/>}
        preferences={<CollectionPreferences title="Table preferences" confirmLabel="Confirm" cancelLabel="Cancel" preferences={{contentDisplay: display}}
            contentDisplayPreference={{title: 'Columns', options: available.map(column => ({id: column.id, label: column.label}))}}
            onConfirm={({detail}) => setDisplay([...(detail.contentDisplay ?? display)])}/>}/>;
}
