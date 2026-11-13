import React from 'react';
import {act, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {vi} from 'vitest';
import IdeaListView, {MIN_AUTO_REFRESH_INTERVAL_SECONDS} from './list-view';
import {initTestAppContext} from '../../test-support';
import {USER_TABLE_COLUMN_DEFINITIONS} from '../../pages/user-management/users';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

const COLUMNS = [
    {
        id: 'job_id',
        header: 'Job Id',
        cell: (item: any) => item.job_id
    }
];

const renderListView = (props: any = {}) => {
    const listView = React.createRef<IdeaListView>();
    render(
        <IdeaListView
            ref={listView}
            title="Active Jobs"
            columnDefinitions={COLUMNS}
            onRefresh={() => {}}
            {...props}
        />
    );
    return listView;
};

describe('list view refresh affordances', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    it('stamps the last updated time after a successful fetch', async () => {
        renderListView({
            showLastRefreshed: true,
            onFetchRecords: () => Promise.resolve({listing: [{job_id: '1'}]})
        });
        expect(await screen.findByText(/^Last updated /)).toBeInTheDocument();
    });

    it('does not stamp a last updated time when the fetch fails', async () => {
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
        renderListView({
            showLastRefreshed: true,
            onFetchRecords: () => Promise.reject(new Error('list_active_jobs failed'))
        });
        await waitFor(() => expect(consoleError).toHaveBeenCalled());
        expect(await screen.findByText('Not loaded')).toBeInTheDocument();
        expect(screen.queryByText(/^Last updated /)).toBeNull();
        consoleError.mockRestore();
    });

    it('says a failed fetch failed instead of showing the empty state, and retries', async () => {
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
        const onFetchRecords = vi.fn()
            .mockRejectedValueOnce({message: 'Network error'})
            .mockResolvedValue({listing: [{job_id: '42'}]});
        renderListView({empty: 'No jobs yet', onFetchRecords});
        expect(await screen.findByText('This list could not be loaded')).toBeInTheDocument();
        expect(screen.getByText(/Network error/)).toBeInTheDocument();
        expect(screen.queryByText('No jobs yet')).toBeNull();
        await userEvent.click(screen.getByRole('button', {name: 'Try again'}));
        expect(await screen.findByText('42')).toBeInTheDocument();
        expect(screen.queryByText('This list could not be loaded')).toBeNull();
        consoleError.mockRestore();
    });

    it('keeps the rows on screen and says how old they are when a refresh fails', async () => {
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
        const onFetchRecords = vi.fn()
            .mockResolvedValueOnce({listing: [{job_id: '42'}]})
            .mockRejectedValue({message: 'Network error'});
        const listView = renderListView({onFetchRecords});
        await screen.findByText('42');
        act(() => listView.current!.fetchRecords());
        expect(await screen.findByText(/The rows below are from/)).toBeInTheDocument();
        expect(screen.getByText('42')).toBeInTheDocument();
        consoleError.mockRestore();
    });

    it('loads the saved rows per page, and shows that number in the preferences', async () => {
        const context = initTestAppContext();
        context.localStorage().setItem('saved-table-pageSize', '100');
        const onFetchRecords = vi.fn().mockResolvedValue({listing: []});
        const listView = renderListView({showPreferences: true, preferencesKey: 'saved', onFetchRecords});
        await screen.findByText('No records');
        expect(listView.current!.getPaginator().page_size).toBe(100);
    });

    it('offers the default rows per page as a choice when nothing is saved', async () => {
        const {container} = render(<IdeaListView title="Jobs" columnDefinitions={COLUMNS} showPreferences preferencesKey="fresh-key"
                                                 onFetchRecords={() => Promise.resolve({listing: []})}/>);
        await screen.findByText('No records');
        await act(async () => createWrapper(container).findCollectionPreferences()!.findTriggerButton().click());
        const checked = document.querySelector('input[type="radio"]:checked') as HTMLInputElement;
        expect(checked.value).toBe('30');
    });

    it('keeps the newest answer when an older request returns late', async () => {
        let resolveFirst: (value: any) => void = () => {};
        const onFetchRecords = vi.fn()
            .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
            .mockResolvedValue({listing: [{job_id: 'newest'}]});
        const listView = renderListView({onFetchRecords});
        act(() => listView.current!.fetchRecords());
        await screen.findByText('newest');
        await act(async () => resolveFirst({listing: [{job_id: 'stale'}]}));
        expect(screen.getByText('newest')).toBeInTheDocument();
        expect(screen.queryByText('stale')).toBeNull();
    });

    it('does not offer pages past what the search index can return, and says so', async () => {
        const listView = renderListView({onFetchRecords: () => Promise.resolve({listing: [{job_id: '1'}], paginator: {start: 0, page_size: 30, total: 14000}})});
        await screen.findByText('1');
        expect(listView.current!.getTotalPages()).toBe(333);
        expect(screen.getByText(/Pages stop at the first 10,000 rows/)).toBeInTheDocument();
        expect(screen.getByText('(14000)')).toBeInTheDocument();
    });

    it('says what its search box looks at', async () => {
        renderListView({showFilters: true, filters: [{key: 'username'}], filteringPlaceholder: 'Search by username', onFetchRecords: () => Promise.resolve({listing: []})});
        expect(await screen.findByPlaceholderText('Search by username')).toBeInTheDocument();
    });

    it('does not advertise a date range limit it does not enforce', async () => {
        const {container} = render(
            <IdeaListView title="Jobs" columnDefinitions={COLUMNS} showDateRange
                          onFetchRecords={() => Promise.resolve({listing: []})}/>
        );
        const picker = createWrapper(container).findDateRangePicker()!;
        await act(async () => picker.openDropdown());
        await userEvent.click(screen.getByText('Absolute range'));
        expect(document.body.textContent).toContain('Use 24 hour format.');
        expect(document.body.textContent).not.toContain('Range must be between');
    });

    it('tells the person when a CSV export fails', async () => {
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
        renderListView({
            enableExportToCsv: true,
            onFetchRecords: () => Promise.resolve({listing: [{job_id: '1'}]}),
            onExportAllRecords: () => Promise.reject({message: 'Network error'})
        });
        await screen.findByText('1');
        await userEvent.click(screen.getByRole('button', {name: 'CSV Export'}));
        expect(await screen.findByText(/CSV export failed: Network error/)).toBeInTheDocument();
        consoleError.mockRestore();
    });

    it('offers no auto refresh control unless the page opts in', async () => {
        renderListView({
            showLastRefreshed: true,
            onFetchRecords: () => Promise.resolve({listing: []})
        });
        await screen.findByText(/^Last updated /);
        expect(screen.queryByText(/Auto refresh/)).toBeNull();
    });

    it('offers auto refresh at a fixed interval, switched off', async () => {
        const listView = renderListView({
            enableAutoRefresh: true,
            onFetchRecords: () => Promise.resolve({listing: []})
        });
        const toggle = await screen.findByText(`Auto refresh every ${MIN_AUTO_REFRESH_INTERVAL_SECONDS}s`);
        expect(toggle).toBeInTheDocument();
        expect(listView.current!.state.autoRefresh).toBe(false);
    });

    it('polls only after the user turns it on, and stops on unmount', async () => {
        vi.useFakeTimers();
        try {
            const onFetchRecords = vi.fn(() => Promise.resolve({listing: []}));
            const listView = React.createRef<IdeaListView>();
            const view = render(
                <IdeaListView
                    ref={listView}
                    title="Active Jobs"
                    columnDefinitions={COLUMNS}
                    enableAutoRefresh={true}
                    onFetchRecords={onFetchRecords}
                />
            );
            await vi.advanceTimersByTimeAsync(MIN_AUTO_REFRESH_INTERVAL_SECONDS * 2 * 1000);
            expect(onFetchRecords).toHaveBeenCalledTimes(1);

            listView.current!.setState({autoRefresh: true});
            (listView.current as any).startAutoRefresh();
            await vi.advanceTimersByTimeAsync(MIN_AUTO_REFRESH_INTERVAL_SECONDS * 1000);
            expect(onFetchRecords).toHaveBeenCalledTimes(2);

            view.unmount();
            await vi.advanceTimersByTimeAsync(MIN_AUTO_REFRESH_INTERVAL_SECONDS * 3 * 1000);
            expect(onFetchRecords).toHaveBeenCalledTimes(2);
        } finally {
            vi.useRealTimers();
        }
    });
});


// a list that holds three pages on the server: 95 rows at 30 a page
const pagedFetch = (listView: () => IdeaListView | null, total = 95) => vi.fn(() => {
    const paginator = listView()!.getPaginator();
    const start = paginator.start ?? 0;
    return Promise.resolve({
        listing: [{job_id: `row-${start}`}],
        paginator: {...paginator, total},
    });
});

const pageToThree = async (container: HTMLElement) => {
    const pagination = createWrapper(container).findPagination()!;
    await screen.findByText('row-0');
    await act(async () => pagination.findPageNumberByIndex(3)!.click());
    await screen.findByText('row-60');
};

describe('a new search, range or page size starts at page 1', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    it('filters from page 3 starting at the first row', async () => {
        const listView = React.createRef<IdeaListView>();
        const onFetchRecords = pagedFetch(() => listView.current);
        const {container} = render(<IdeaListView ref={listView} title="Jobs" columnDefinitions={COLUMNS} showPaginator showFilters
                                                 filters={[{key: '$all'}]} onFetchRecords={onFetchRecords}/>);
        await pageToThree(container);
        await act(async () => createWrapper(container).findTextFilter()!.findInput().setInputValue('abc'));
        await screen.findByText('row-0');
        expect(listView.current!.getFilters()).toEqual([{key: '$all', value: 'abc'}]);
        expect(listView.current!.getPaginator().start).toBe(0);
        expect(createWrapper(container).findPagination()!.findCurrentPage().getElement()).toHaveTextContent('1');
    });

    it('drops the cursor of the old list when filtering', async () => {
        const listView = React.createRef<IdeaListView>();
        const seen: any[] = [];
        const onFetchRecords = vi.fn(() => {
            seen.push({...listView.current!.getPaginator()});
            return Promise.resolve({listing: [{job_id: 'a'}], paginator: {page_size: 30, cursor: 'next-page'}});
        });
        const {container} = render(<IdeaListView ref={listView} title="Users" columnDefinitions={COLUMNS} showPaginator showFilters
                                                 openEndedPaging cursorBasedPaging
                                                 filters={[{key: '$all'}]} onFetchRecords={onFetchRecords}/>);
        await screen.findByText('a');
        await act(async () => createWrapper(container).findTextFilter()!.findInput().setInputValue('abc'));
        await waitFor(() => expect(onFetchRecords).toHaveBeenCalledTimes(2));
        expect(seen[1].cursor).toBeUndefined();
    });

    it('changes rows per page from page 3 starting at the first row', async () => {
        const listView = React.createRef<IdeaListView>();
        const onFetchRecords = pagedFetch(() => listView.current);
        const {container} = render(<IdeaListView ref={listView} title="Jobs" columnDefinitions={COLUMNS} showPaginator showPreferences preferencesKey="size-reset"
                                                 onFetchRecords={onFetchRecords}/>);
        await pageToThree(container);
        const preferences = createWrapper(container).findCollectionPreferences()!;
        await act(async () => preferences.findTriggerButton().click());
        const modal = preferences.findModal()!;
        await act(async () => modal.findPageSizePreference()!.findOptions()[3].findNativeInput().click());
        await act(async () => modal.findConfirmButton()!.click());
        await screen.findByText('row-0');
        expect(listView.current!.getPaginator()).toMatchObject({start: 0, page_size: 50});
    });

    it('picks a new date range from page 3 starting at the first row', async () => {
        const listView = React.createRef<IdeaListView>();
        const onFetchRecords = pagedFetch(() => listView.current);
        const {container} = render(<IdeaListView ref={listView} title="Jobs" columnDefinitions={COLUMNS} showPaginator showDateRange
                                                 dateRange={{type: 'relative', amount: 1, unit: 'month'}}
                                                 onDateRange={range => ({...range, key: 'queue_time'})}
                                                 onFetchRecords={onFetchRecords}/>);
        await pageToThree(container);
        const picker = createWrapper(container).findDateRangePicker()!;
        await act(async () => picker.findTrigger().click());
        await act(async () => picker.findDropdown()!.findRelativeRangeRadioGroup()!.findButtons()[1].findNativeInput().click());
        await act(async () => picker.findDropdown()!.findApplyButton().click());
        await screen.findByText('row-0');
        expect(listView.current!.getPaginator().start).toBe(0);
    });
});

describe('list counter and loading text', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    const counter = (container: HTMLElement) => createWrapper(container).findHeader()!.findCounter()?.getElement().textContent?.trim();

    it('counts the rows of a cursor-paged list that fits on one page', async () => {
        const {container} = render(<IdeaListView title="Users" columnDefinitions={COLUMNS} showPaginator openEndedPaging cursorBasedPaging
                                                 onFetchRecords={() => Promise.resolve({listing: [{job_id: 'a'}, {job_id: 'b'}], paginator: {page_size: 30}})}/>);
        await screen.findByText('a');
        expect(counter(container)).toBe('(2)');
    });

    it('claims no count while a cursor says there are more rows', async () => {
        const {container} = render(<IdeaListView title="Users" columnDefinitions={COLUMNS} showPaginator openEndedPaging cursorBasedPaging
                                                 onFetchRecords={() => Promise.resolve({listing: [{job_id: 'a'}], paginator: {page_size: 30, cursor: 'next'}})}/>);
        await screen.findByText('a');
        expect(counter(container)).toBeFalsy();
    });

    it('prefers the total the server gives', async () => {
        const {container} = render(<IdeaListView title="Jobs" columnDefinitions={COLUMNS} showPaginator
                                                 onFetchRecords={() => Promise.resolve({listing: [{job_id: 'a'}], paginator: {page_size: 30, total: 412}})}/>);
        await screen.findByText('a');
        expect(counter(container)).toBe('(412)');
    });

    it('says it is loading', async () => {
        render(<IdeaListView title="Jobs" columnDefinitions={COLUMNS} onFetchRecords={() => new Promise(() => {})}/>);
        expect(await screen.findByText('Loading')).toBeInTheDocument();
    });
});

describe('sorting', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    const SORT_COLUMNS = [
        {id: 'name', header: 'Name', cell: (item: any) => item.name, sortingField: 'name'},
        {id: 'cost', header: 'Cost', cell: (item: any) => item.cost, sortingComparator: (a: any, b: any) => a.cost - b.cost},
    ];
    const SERVER_SORT = {columns: {name: 'name'}, defaultColumn: 'name', defaultDescending: true};
    const rows = () => [{name: 'b', cost: 2}, {name: 'a', cost: 1}, {name: 'c', cost: 3}];
    const sortArea = (container: HTMLElement, header: string) => Array.from(container.querySelectorAll('th'))
        .find(th => th.textContent?.includes(header))?.querySelector<HTMLElement>('[role="button"]') ?? null;
    const renderNames = (container: HTMLElement) => createWrapper(container).findTable()!.findRows().map(row => row.getElement().textContent);

    it('asks the server for the sort, from the first row, when a header is clicked on page 3', async () => {
        const listView = React.createRef<IdeaListView>();
        const seen: any[] = [];
        const onFetchRecords = vi.fn(() => {
            const paginator = listView.current!.getPaginator();
            seen.push({start: paginator.start, sort_by: listView.current!.getSortBy()});
            return Promise.resolve({listing: [{name: `row-${paginator.start ?? 0}`, cost: 1}], paginator: {...paginator, total: 95}});
        });
        const {container} = render(<IdeaListView ref={listView} title="Jobs" columnDefinitions={SORT_COLUMNS} showPaginator
                                                 serverSort={SERVER_SORT} onFetchRecords={onFetchRecords}/>);
        await screen.findByText('row-0');
        expect(seen[0].sort_by).toEqual({key: 'name', order: 'desc'});
        await act(async () => createWrapper(container).findPagination()!.findPageNumberByIndex(3)!.click());
        await screen.findByText('row-60');
        await act(async () => sortArea(container, 'Name')!.click());
        await screen.findByText('row-0');
        expect(seen[seen.length - 1]).toEqual({start: 0, sort_by: {key: 'name', order: 'asc'}});
        expect(createWrapper(container).findPagination()!.findCurrentPage().getElement()).toHaveTextContent('1');
    });

    it('shows the arrow on the column the server is sorting by, and keeps the order the server sent', async () => {
        const {container} = render(<IdeaListView title="Jobs" columnDefinitions={SORT_COLUMNS} serverSort={SERVER_SORT}
                                                 onFetchRecords={() => Promise.resolve({listing: rows(), paginator: {total: 3, page_size: 30}})}/>);
        await screen.findByText('b');
        const table = createWrapper(container).findTable()!;
        expect(table.findDescSortedColumn()!.getElement()).toHaveTextContent('Name');
        expect(sortArea(container, 'Cost')).toBeNull();
        expect(renderNames(container)[0]).toContain('b');
    });

    it('sorts in the browser when one page holds every row', async () => {
        const {container} = render(<IdeaListView title="Projects" columnDefinitions={SORT_COLUMNS} defaultSortingColumn="name"
                                                 onFetchRecords={() => Promise.resolve({listing: rows(), paginator: {total: 3, page_size: 30}})}/>);
        await screen.findByText('b');
        const table = createWrapper(container).findTable()!;
        expect(table.findAscSortedColumn()!.getElement()).toHaveTextContent('Name');
        expect(renderNames(container)[0]).toContain('a');
        expect(sortArea(container, 'Cost')).not.toBeNull();
    });

    it('offers no sort over one page of many, and leaves the rows in the server order', async () => {
        const {container} = render(<IdeaListView title="Projects" columnDefinitions={SORT_COLUMNS} defaultSortingColumn="name" showPaginator
                                                 onFetchRecords={() => Promise.resolve({listing: rows(), paginator: {total: 95, page_size: 30}})}/>);
        await screen.findByText('b');
        const table = createWrapper(container).findTable()!;
        expect(sortArea(container, 'Name')).toBeNull();
        expect(sortArea(container, 'Cost')).toBeNull();
        expect(table.findAscSortedColumn()).toBeNull();
        expect(renderNames(container)[0]).toContain('b');
    });
});

describe('csv export', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    const exportedCsv = async (columns: any[], rows: any[]) => {
        const createObjectURL = vi.fn((_blob: Blob) => 'blob:csv');
        (URL as any).createObjectURL = createObjectURL;
        render(<IdeaListView title="Jobs" columnDefinitions={columns} enableExportToCsv
                             onExportAllRecords={() => Promise.resolve(rows)}
                             onFetchRecords={() => Promise.resolve({listing: []})}/>);
        await screen.findByText('No records');
        await userEvent.click(screen.getByRole('button', {name: 'CSV Export'}));
        await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
        return await new Promise<string>(resolve => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result as string);
            reader.readAsText(createObjectURL.mock.calls[0][0]);
        });
    };

    it('writes an exit code of 0, not a blank, whichever way the column gets its value', async () => {
        const csv = await exportedCsv([
            {id: 'by_field', header: 'Field', cell: (j: any) => j.exit_status, sortingField: 'exit_status'},
            {id: 'by_cell', header: 'Cell', cell: (j: any) => j.exit_status},
        ], [{exit_status: 0}, {exit_status: 271}, {}]);
        expect(csv.split('\n').slice(1)).toEqual(['"0","0"', '"271","271"', '"",""']);
    });

    it('writes the value a column names for export', async () => {
        const csv = await exportedCsv([
            {id: 'status', header: 'Status', cell: () => <b>on screen</b>, sortingField: 'state', exportValue: (j: any) => j.label},
        ], [{state: 'finished', label: 'Failed (exit 271)'}]);
        expect(csv.split('\n')[1]).toBe('"Failed (exit 271)"');
    });

    it('shows progress and takes one export at a time', async () => {
        (URL as any).createObjectURL = vi.fn(() => 'blob:csv');
        let finish: (rows: any[]) => void = () => {};
        const onExportAllRecords = vi.fn(() => new Promise<any[]>(resolve => { finish = resolve; }));
        render(<IdeaListView title="Jobs" columnDefinitions={COLUMNS} enableExportToCsv
                             onExportAllRecords={onExportAllRecords}
                             onFetchRecords={() => Promise.resolve({listing: []})}/>);
        await screen.findByText('No records');
        const button = screen.getByRole('button', {name: 'CSV Export'});
        await userEvent.click(button);
        await userEvent.click(button);
        expect(onExportAllRecords).toHaveBeenCalledTimes(1);
        expect(button).toHaveAttribute('aria-disabled', 'true');
        await act(async () => finish([]));
        await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled', 'true'));
    });
});

describe('date range field', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    it('offers ranges past one month', async () => {
        const {container} = render(<IdeaListView title="Jobs" columnDefinitions={COLUMNS} showDateRange
                                                 dateRange={{type: 'relative', amount: 1, unit: 'month'}}
                                                 onDateRange={range => range}
                                                 onFetchRecords={() => Promise.resolve({listing: []})}/>);
        await screen.findByText('No records');
        const picker = createWrapper(container).findDateRangePicker()!;
        await act(async () => picker.findTrigger().click());
        const options = picker.findDropdown()!.findRelativeRangeRadioGroup()!.findButtons().map(button => button.getElement().textContent);
        expect(options).toEqual(expect.arrayContaining(['Last 3 months', 'Last 1 year']));
    });

    it('names the range in words', async () => {
        const listView = React.createRef<IdeaListView>();
        render(<IdeaListView ref={listView} title="Jobs" columnDefinitions={COLUMNS} showDateRange
                             dateRange={{type: 'relative', amount: 3, unit: 'month'}}
                             onFetchRecords={() => Promise.resolve({listing: []})}/>);
        await screen.findByText('No records');
        expect(listView.current!.getDateRangeLabel()).toBe('the last 3 months');
    });

    it('says a search found nothing instead of showing the empty list state', async () => {
        const onFetchRecords = vi.fn().mockResolvedValue({listing: []});
        const {container} = render(<IdeaListView title="Jobs" columnDefinitions={COLUMNS} showFilters filters={[{key: '$all'}]}
                                                 empty="You have no active jobs." emptySearch="No active jobs match your search."
                                                 onFetchRecords={onFetchRecords}/>);
        await screen.findByText('You have no active jobs.');
        await act(async () => createWrapper(container).findTextFilter()!.findInput().setInputValue('abc'));
        expect(await screen.findByText('No active jobs match your search.')).toBeInTheDocument();
        await act(async () => createWrapper(container).findTextFilter()!.findInput().setInputValue(''));
        expect(await screen.findByText('You have no active jobs.')).toBeInTheDocument();
    });

    it('keeps the field the page names on the range it hands back', async () => {
        const listView = React.createRef<IdeaListView>();
        render(<IdeaListView ref={listView} title="Nodes" columnDefinitions={COLUMNS} showDateRange
                             dateRange={{type: 'relative', amount: 1, unit: 'month'}}
                             onDateRange={range => ({key: 'launch_time', start: range.start, end: range.end})}
                             onFetchRecords={() => Promise.resolve({listing: [{job_id: 'node-1'}]})}/>);
        await screen.findByText('node-1');
        const range = listView.current!.getDateRange()!;
        expect(range.key).toBe('launch_time');
        expect(new Date(range.end!).getTime() - new Date(range.start!).getTime()).toBe(30 * 24 * 60 * 60 * 1000);
    });

    it('sends no range when the page has none to send', async () => {
        const listView = React.createRef<IdeaListView>();
        render(<IdeaListView ref={listView} title="Projects" columnDefinitions={COLUMNS}
                             onFetchRecords={() => Promise.resolve({listing: [{job_id: 'p'}]})}/>);
        await screen.findByText('p');
        expect(listView.current!.getDateRange()).toBeUndefined();
    });
});

describe('users table task status', () => {
    it('renders a terminal failure with its message and time', async () => {
        initTestAppContext();
        const at = '2026-01-01T12:00:00+00:00';
        renderListView({
            title: 'Users',
            columnDefinitions: USER_TABLE_COLUMN_DEFINITIONS,
            onFetchRecords: () => Promise.resolve({listing: [{
                username: 'user',
                enabled: true,
                last_task_failure: {task: 'accounts.sync-user', message: 'Directory unavailable', at}
            }]})
        });
        const message = await screen.findByText(`accounts.sync-user: Directory unavailable (${new Date(at).toLocaleString()})`);
        const indicator = createWrapper(message.closest('td')!).findStatusIndicator();
        expect(indicator).not.toBeNull();
        expect(screen.getByRole('img', {name: 'Error'})).toBeInTheDocument();
        expect(screen.queryByText('Enabled')).not.toBeInTheDocument();
    });

    it('keeps the enabled status when there is no failure', async () => {
        initTestAppContext();
        renderListView({
            title: 'Users',
            columnDefinitions: USER_TABLE_COLUMN_DEFINITIONS,
            onFetchRecords: () => Promise.resolve({listing: [{username: 'user', enabled: true}]})
        });
        expect(await screen.findByText('Enabled')).toBeInTheDocument();
    });
});
