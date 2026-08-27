import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {Box, Header, Input, Pagination, Table} from "@cloudscape-design/components";
import {TableProps} from "@cloudscape-design/components/table/interfaces";
import {useCollection} from "@cloudscape-design/collection-hooks";
import {FontAwesomeIcon} from "@fortawesome/react-fontawesome";
import {IconDefinition} from "@fortawesome/fontawesome-svg-core";
import {
    faFile,
    faFileAudio,
    faFileCode,
    faFileCsv,
    faFileExcel,
    faFileImage,
    faFileLines,
    faFilePdf,
    faFilePowerpoint,
    faFileVideo,
    faFileWord,
    faFileZipper,
    faFolder
} from "@fortawesome/free-solid-svg-icons";

/** A directory listing rendered as a Cloudscape table. ListFiles returns a whole directory in one
 * response, so the row count is bounded by client-side pagination rather than virtualization:
 * filtering and sorting still run over every entry, only a page of them reaches the DOM. */

export const FILE_BROWSER_PAGE_SIZE = 100

// Two clicks on the same row inside this window open it. Matches the OS default
// so a deliberate double-click registers and two separate clicks do not.
const DOUBLE_CLICK_MS = 400

export interface FileBrowserEntry {
    id: string
    name: string
    isDir?: boolean
    isHidden?: boolean
    modDate?: string
    size?: number
    path?: string
}

export interface FileBrowserMenuItem {
    id: string
    text: string
    onClick: () => void
}

export interface FileBrowserTableProps {
    title: string
    entries: FileBrowserEntry[]
    selectedEntries: FileBrowserEntry[]
    onSelectionChange: (entries: FileBrowserEntry[]) => void
    onOpen: (entry: FileBrowserEntry) => void
    /** Called before the context menu opens, so the page can fix the selection. */
    onContextMenu: (entry: FileBrowserEntry) => void
    menuItems: FileBrowserMenuItem[]
    actions: React.ReactNode
    path?: React.ReactNode
    loading?: boolean
    showHiddenFiles?: boolean
}

const ICONS_BY_EXTENSION: {[extension: string]: IconDefinition} = {
    bash: faFileCode, c: faFileCode, cfg: faFileCode, conf: faFileCode, cpp: faFileCode, cs: faFileCode,
    css: faFileCode, go: faFileCode, h: faFileCode, hpp: faFileCode, html: faFileCode, ini: faFileCode,
    java: faFileCode, js: faFileCode, json: faFileCode, jsx: faFileCode, php: faFileCode, pl: faFileCode,
    py: faFileCode, r: faFileCode, rb: faFileCode, rs: faFileCode, sh: faFileCode, sql: faFileCode,
    toml: faFileCode, ts: faFileCode, tsx: faFileCode, xml: faFileCode, yaml: faFileCode, yml: faFileCode,
    log: faFileLines, md: faFileLines, rst: faFileLines, txt: faFileLines,
    csv: faFileCsv, tsv: faFileCsv,
    xls: faFileExcel, xlsx: faFileExcel,
    doc: faFileWord, docx: faFileWord,
    ppt: faFilePowerpoint, pptx: faFilePowerpoint,
    pdf: faFilePdf,
    bmp: faFileImage, gif: faFileImage, jpeg: faFileImage, jpg: faFileImage, png: faFileImage,
    svg: faFileImage, tif: faFileImage, tiff: faFileImage, webp: faFileImage,
    '7z': faFileZipper, bz2: faFileZipper, gz: faFileZipper, rar: faFileZipper, tar: faFileZipper,
    tgz: faFileZipper, xz: faFileZipper, zip: faFileZipper,
    aac: faFileAudio, flac: faFileAudio, mp3: faFileAudio, ogg: faFileAudio, wav: faFileAudio,
    avi: faFileVideo, mkv: faFileVideo, mov: faFileVideo, mp4: faFileVideo, webm: faFileVideo
}

export function entryIcon(entry: FileBrowserEntry): IconDefinition {
    if (entry.isDir) {
        return faFolder
    }
    const dot = entry.name.lastIndexOf('.')
    if (dot <= 0 || dot === entry.name.length - 1) {
        return faFile
    }
    return ICONS_BY_EXTENSION[entry.name.substring(dot + 1).toLowerCase()] ?? faFile
}

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

export function formatFileSize(bytes?: number): string {
    if (bytes == null || Number.isNaN(bytes) || bytes < 0) {
        return ''
    }
    let unit = 0
    let value = bytes
    while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
        value = value / 1024
        unit += 1
    }
    return `${unit === 0 ? value : value.toFixed(1)} ${SIZE_UNITS[unit]}`
}

export function formatModifiedDate(modDate?: string): string {
    if (modDate == null) {
        return ''
    }
    const parsed = new Date(modDate)
    return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleDateString()
}

function modifiedTime(entry: FileBrowserEntry): number {
    if (entry.modDate == null) {
        return 0
    }
    const parsed = new Date(entry.modDate)
    return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime()
}

/** Favourites can repeat a file id across directories, so key on the path first. */
export function entryKey(entry: FileBrowserEntry): string {
    return entry.path ?? entry.id
}

/** Case-insensitive, as the previous component was. */
function compareNames(a: FileBrowserEntry, b: FileBrowserEntry): number {
    return a.name.localeCompare(b.name, undefined, {sensitivity: 'base'})
}

const COLUMN_DEFINITIONS: TableProps.ColumnDefinition<FileBrowserEntry>[] = [
    {
        id: 'name',
        header: 'Name',
        isRowHeader: true,
        // Directories are grouped ahead of files by sortEntries, not here.
        sortingComparator: compareNames,
        cell: (entry) => (
            <span className="soca-file-browser-name">
                <FontAwesomeIcon icon={entryIcon(entry)} fixedWidth={true}/>
                <span className="soca-file-browser-name-text">{entry.name}</span>
            </span>
        )
    },
    {
        id: 'modDate',
        header: 'Last modified',
        sortingComparator: (a, b) => modifiedTime(a) - modifiedTime(b),
        cell: (entry) => formatModifiedDate(entry.modDate)
    },
    {
        id: 'size',
        header: 'Size',
        sortingComparator: (a, b) => (a.size ?? 0) - (b.size ?? 0),
        cell: (entry) => (entry.isDir ? '' : formatFileSize(entry.size))
    }
]

const EMPTY_STATE = (
    <Box textAlign="center" color="inherit">
        Nothing to show
    </Box>
)

/** Directories first, then files, and inside each group whichever column the user chose. The grouping
 * is applied before the column comparator and is not reversed by a descending sort, so folders stay
 * together at the top under every column and in both directions, like a desktop file manager. */
export function sortEntries(
    entries: FileBrowserEntry[],
    column: {sortingComparator?: (a: FileBrowserEntry, b: FileBrowserEntry) => number} | undefined,
    isDescending: boolean
): FileBrowserEntry[] {
    const comparator = column?.sortingComparator
    return [...entries].sort((a, b) => {
        const grouped = (a.isDir ? 0 : 1) - (b.isDir ? 0 : 1)
        if (grouped !== 0) {
            return grouped
        }
        const within = comparator == null ? 0 : comparator(a, b)
        if (within !== 0) {
            return isDescending ? -within : within
        }
        // Whatever the column cannot separate (every directory, when sorting
        // by size) is ordered by name rather than by however the API happened
        // to return it.
        return compareNames(a, b)
    })
}

function FileBrowserTable(props: FileBrowserTableProps) {

    const [menuPosition, setMenuPosition] = useState<{x: number, y: number} | null>(null)
    const menuRef = useRef<HTMLDivElement | null>(null)
    // onRowClick carries no native event, so the modifier state is read off the
    // click as it passes the wrapper on the way down.
    const modifiers = useRef<{range: boolean, toggle: boolean}>({range: false, toggle: false})
    const lastClick = useRef<{key: string, at: number} | null>(null)
    // The anchor for Shift-range selection, keyed on the entry rather than a row index so a directory
    // change, a re-sort, a re-page or a filter collapses the next Shift-click to a plain single click.
    const lastSelectedKey = useRef<string | null>(null)

    const showHiddenFiles = props.showHiddenFiles === true
    const visibleEntries = useMemo(
        () => (showHiddenFiles ? props.entries : props.entries.filter((entry) => !entry.isHidden)),
        [props.entries, showHiddenFiles]
    )

    // Sorting is not left to useCollection: it negates the whole comparator for
    // a descending sort, which would send the directories to the bottom.
    const [sorting, setSorting] = useState<{column: TableProps.ColumnDefinition<FileBrowserEntry>, isDescending: boolean}>({
        column: COLUMN_DEFINITIONS[0],
        isDescending: false
    })
    const sortedEntries = useMemo(
        () => sortEntries(visibleEntries, sorting.column, sorting.isDescending),
        [visibleEntries, sorting]
    )

    const {items, collectionProps, filterProps, paginationProps, filteredItemsCount, actions} = useCollection(sortedEntries, {
        filtering: {
            filteringFunction: (entry, filteringText) =>
                entry.name.toLowerCase().includes(filteringText.trim().toLowerCase()),
            empty: EMPTY_STATE,
            noMatch: EMPTY_STATE
        },
        pagination: {pageSize: FILE_BROWSER_PAGE_SIZE}
    })

    const closeMenu = useCallback(() => setMenuPosition(null), [])

    useEffect(() => {
        if (menuPosition == null) {
            return
        }
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                closeMenu()
            }
        }
        const onPointerDown = (event: MouseEvent) => {
            if (menuRef.current != null && !menuRef.current.contains(event.target as Node)) {
                closeMenu()
            }
        }
        document.addEventListener('keydown', onKeyDown)
        document.addEventListener('mousedown', onPointerDown)
        window.addEventListener('resize', closeMenu)
        menuRef.current?.querySelector('button')?.focus()
        return () => {
            document.removeEventListener('keydown', onKeyDown)
            document.removeEventListener('mousedown', onPointerDown)
            window.removeEventListener('resize', closeMenu)
        }
    }, [menuPosition, closeMenu])

    const onRowClick = (item: FileBrowserEntry, rowIndex: number) => {
        const key = entryKey(item)
        const now = Date.now()
        const previous = lastClick.current
        lastClick.current = {key: key, at: now}
        if (previous != null && previous.key === key && now - previous.at < DOUBLE_CLICK_MS) {
            lastClick.current = null
            props.onOpen(item)
            return
        }
        if (modifiers.current.range && lastSelectedKey.current != null) {
            const anchorIndex = items.findIndex((entry) => entryKey(entry) === lastSelectedKey.current)
            if (anchorIndex !== -1) {
                const from = Math.min(anchorIndex, rowIndex)
                const to = Math.max(anchorIndex, rowIndex)
                props.onSelectionChange(items.slice(from, to + 1))
                return
            }
            // Anchor is gone from the current page: fall through to a plain
            // single click on the row that was actually clicked.
        }
        lastSelectedKey.current = key
        if (modifiers.current.toggle) {
            const selected = props.selectedEntries.some((entry) => entryKey(entry) === key)
            props.onSelectionChange(
                selected ? props.selectedEntries.filter((entry) => entryKey(entry) !== key) : [...props.selectedEntries, item]
            )
            return
        }
        props.onSelectionChange([item])
    }

    const summary = () => {
        const total = `${visibleEntries.length} item${visibleEntries.length === 1 ? '' : 's'}`
        return props.selectedEntries.length > 0 ? `${total} (${props.selectedEntries.length} selected)` : total
    }

    return (
        <div
            className="soca-file-browser"
            onClickCapture={(event) => {
                modifiers.current = {range: event.shiftKey, toggle: event.ctrlKey || event.metaKey}
            }}
        >
            {props.path}
            <Table
                {...collectionProps}
                variant="container"
                loading={props.loading}
                loadingText="Loading files"
                trackBy={entryKey}
                items={items}
                columnDefinitions={COLUMN_DEFINITIONS}
                sortingColumn={sorting.column}
                sortingDescending={sorting.isDescending}
                onSortingChange={(event) => {
                    setSorting({
                        column: event.detail.sortingColumn as TableProps.ColumnDefinition<FileBrowserEntry>,
                        isDescending: event.detail.isDescending === true
                    })
                    actions.setCurrentPage(1)
                }}
                selectionType="multi"
                selectedItems={props.selectedEntries}
                onSelectionChange={(event) => props.onSelectionChange([...event.detail.selectedItems])}
                onRowClick={(event) => onRowClick(event.detail.item, event.detail.rowIndex)}
                onRowContextMenu={(event) => {
                    event.preventDefault()
                    props.onContextMenu(event.detail.item)
                    setMenuPosition({x: event.detail.clientX, y: event.detail.clientY})
                }}
                ariaLabels={{
                    selectionGroupLabel: 'File selection',
                    allItemsSelectionLabel: () => 'Select all',
                    itemSelectionLabel: (data, entry) => `Select ${entry.name}`,
                    tableLabel: props.title
                }}
                header={
                    <Header counter={summary()} actions={props.actions}>
                        {props.title}
                    </Header>
                }
                filter={
                    <div className="soca-file-browser-filter">
                        <Input
                            value={filterProps.filteringText}
                            placeholder="Search"
                            type="text"
                            ariaLabel="Search this directory"
                            onChange={(event) => actions.setFiltering(event.detail.value)}
                        />
                        <span className="soca-file-browser-filter-count">
                            {filterProps.filteringText.trim().length > 0 ? `${filteredItemsCount ?? 0} matches` : ''}
                        </span>
                    </div>
                }
                pagination={paginationProps.pagesCount > 1 ? <Pagination {...paginationProps}/> : undefined}
            />
            {menuPosition != null && props.menuItems.length > 0 && (
                <div
                    ref={menuRef}
                    role="menu"
                    aria-label={`${props.title} actions`}
                    className="soca-file-browser-menu"
                    style={{left: `${menuPosition.x}px`, top: `${menuPosition.y}px`}}
                >
                    {props.menuItems.map((item) => (
                        <button
                            key={item.id}
                            type="button"
                            role="menuitem"
                            className="soca-file-browser-menu-item"
                            onClick={() => {
                                closeMenu()
                                item.onClick()
                            }}
                        >
                            {item.text}
                        </button>
                    ))}
                </div>
            )}
        </div>
    )
}

export default FileBrowserTable
