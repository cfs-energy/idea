import React from 'react';
import {render, screen, waitFor} from '@testing-library/react';
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
        expect(screen.getByText('Loading...')).toBeInTheDocument();
        expect(screen.queryByText(/^Last updated /)).toBeNull();
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
