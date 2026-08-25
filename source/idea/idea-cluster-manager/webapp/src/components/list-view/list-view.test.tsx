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

import React from 'react';
import {render, screen, waitFor} from '@testing-library/react';
import {vi} from 'vitest';
import IdeaListView, {MIN_AUTO_REFRESH_INTERVAL_SECONDS} from './list-view';
import {initTestAppContext} from '../../test-support';

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
