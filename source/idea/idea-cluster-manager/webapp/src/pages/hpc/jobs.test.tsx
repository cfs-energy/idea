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

import {render, screen} from '@testing-library/react';
import {JobInfo, JobWaitingSignals} from './jobs';
import {SocaJob} from '../../client/data-model';
import {initTestAppContext} from '../../test-support';

const NOW = new Date('2026-08-19T12:00:00Z');

// KeyValue renders <div><Box>{title}</Box>{value}</div>, so the value is what is
// left of the wrapper's text once the title is removed
const valueOf = (title: string): string => {
    const label = screen.getByText(title);
    const text = label.parentElement?.textContent ?? '';
    return text.replace(title, '').trim();
};

describe('job info panel', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    it('shows the error message on a job that already has a compute stack', () => {
        // the status-column popover only fires while compute_stack is 'tbd', so a
        // job that got a stack and then failed used to show no reason at all
        const job: SocaJob = {
            job_id: '2345',
            state: 'held',
            error_message: 'SERVICE_QUOTA_NOT_AVAILABLE: service quota not available for instance types',
            params: {compute_stack: 'idea-compute-node-2345'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(screen.getByText(/SERVICE_QUOTA_NOT_AVAILABLE/)).toBeInTheDocument();
    });

    it('omits the error message row when the job has no error', () => {
        render(<JobInfo job={{job_id: '2346', state: 'running'}} now={NOW}/>);
        expect(screen.queryByText('Error Message')).toBeNull();
    });

    it('reports a queued job as not started and shows how long it has waited', () => {
        const job: SocaJob = {
            job_id: '2347',
            state: 'queued',
            queue_time: '2026-08-19T11:15:00Z',
            params: {walltime: '05:00:00', compute_stack: 'tbd'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(valueOf('Elapsed vs Requested')).toBe('Not started');
        expect(valueOf('Queued For')).toBe('45 min');
        expect(valueOf('Requested Walltime')).toBe('05:00:00');
        // requested walltime must never be rendered as time the job has used
        expect(valueOf('Total Time')).toBe('-');
    });

    it('reports elapsed against requested for a running job, in minutes', () => {
        const job: SocaJob = {
            job_id: '2348',
            state: 'running',
            queue_time: '2026-08-19T10:00:00Z',
            start_time: '2026-08-19T10:30:00Z',
            params: {walltime: '05:00:00'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(valueOf('Elapsed vs Requested')).toBe('1 hr 30 min of 5 hr requested');
        expect(valueOf('Queued For')).toBe('30 min');
    });

    it('bounds a record for a job that never ran, whenever it is opened', () => {
        // total_time_secs is 0 on these records: rendering it would read as
        // "less than 1 min" beside an "Elapsed vs Requested" of "Not started"
        const job: SocaJob = {
            job_id: '2350',
            state: 'finished',
            queue_time: '2026-08-19T10:00:00Z',
            end_time: '2026-08-19T10:12:00Z',
            total_time_secs: 0,
            params: {walltime: '05:00:00', compute_stack: 'tbd'}
        };
        render(<JobInfo job={job} now={new Date('2026-09-06T12:00:00Z')}/>);
        expect(valueOf('Elapsed vs Requested')).toBe('Not started');
        expect(valueOf('Total Time')).toBe('-');
        // 12 min, not the 18 days since the job was recorded
        expect(valueOf('Queued For')).toBe('12 min');
    });

    it('renders exit status and recorded total time for a completed job', () => {
        const job: SocaJob = {
            job_id: '2349',
            state: 'finished',
            exit_status: 0,
            start_time: '2026-08-19T09:00:00Z',
            end_time: '2026-08-19T10:02:00Z',
            total_time_secs: 3720,
            provisioning_time: '2026-08-19T08:50:00Z',
            params: {walltime: '05:00:00'}
        };
        render(<JobInfo job={job} now={NOW}/>);
        expect(valueOf('Exit Status')).toBe('0');
        expect(valueOf('Total Time')).toBe('1 hr 2 min');
        expect(valueOf('Elapsed vs Requested')).toBe('1 hr 2 min of 5 hr requested');
        expect(valueOf('Provisioning Time')).not.toBe('-');
    });
});

describe('job waiting signals', () => {
    beforeEach(() => {
        initTestAppContext();
    });

    const queuedJob = (overrides: Partial<SocaJob> = {}): SocaJob => ({
        job_id: '2345',
        state: 'queued',
        queue_time: '2026-08-19T11:15:00Z',
        params: {compute_stack: 'tbd'},
        ...overrides
    });

    it('tells a queued job owner how long it has waited and which attempt it is on', () => {
        render(<JobWaitingSignals job={queuedJob({
            provisioning_attempt: 2,
            max_provisioning_attempts: 3
        })} now={NOW}/>);
        expect(screen.getByText(/waiting 45 min/)).toBeInTheDocument();
        expect(screen.getByText(/attempt 2 of 3/)).toBeInTheDocument();
    });

    it('names the queue limit holding the job, and nothing else about the queue', () => {
        const {container} = render(<JobWaitingSignals job={queuedJob({
            blocking_limit_type: 'max_provisioned_instances'
        })} now={NOW}/>);
        // asserted over the whole rendered text: a threshold or a usage count added to
        // the signal would have to show up here
        expect(container.textContent).toBe('waiting 45 min \u00b7 queue limit: max_provisioned_instances');
    });

    it('renders nothing for a running job', () => {
        const {container} = render(<JobWaitingSignals job={{
            job_id: '2346',
            state: 'running',
            queue_time: '2026-08-19T10:00:00Z',
            start_time: '2026-08-19T10:30:00Z'
        }} now={NOW}/>);
        expect(container).toBeEmptyDOMElement();
    });

    it('shows the attempt and the blocking limit in the job info panel', () => {
        render(<JobInfo job={queuedJob({
            provisioning_attempt: 3,
            max_provisioning_attempts: 3,
            blocking_limit_type: 'max_running_jobs'
        })} now={NOW}/>);
        expect(valueOf('Provisioning Attempt')).toBe('attempt 3 of 3');
        expect(valueOf('Blocking Queue Limit')).toBe('max_running_jobs');
    });

    it('omits both rows on a job that is not waiting to be provisioned', () => {
        render(<JobInfo job={{job_id: '2347', state: 'running', start_time: '2026-08-19T11:00:00Z'}} now={NOW}/>);
        expect(screen.queryByText('Provisioning Attempt')).toBeNull();
        expect(screen.queryByText('Blocking Queue Limit')).toBeNull();
    });
});
