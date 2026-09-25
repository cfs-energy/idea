import {render, screen} from '@testing-library/react';
import {HPC_QUEUE_TABLE_COLUMN_DEFINITIONS} from './queues';
import {HpcQueueProfile} from '../../client/data-model';

const status = (queue: HpcQueueProfile) => HPC_QUEUE_TABLE_COLUMN_DEFINITIONS.find(column => column.id === 'status')!.cell(queue);

it('shows queue and group usage next to the blocked status', () => {
    render(status({enabled: true, status: 'blocked', queue_size: 7, limit_info: {limit_type: 'max_provisioned_instances', queue_threshold: 20, queue_current: 12, group_threshold: 30, group_current: 28}}));
    expect(screen.getByText('Blocked')).toBeInTheDocument();
    expect(screen.getByText('Queued jobs: 7')).toBeInTheDocument();
    expect(screen.getByText('Queue threshold / current: 20 / 12')).toBeInTheDocument();
    expect(screen.getByText('Group threshold / current: 30 / 28')).toBeInTheDocument();
});

it('preserves zero usage and tolerates missing limits', () => {
    const {rerender} = render(status({enabled: true, status: 'blocked', queue_size: 0, limit_info: {queue_threshold: 20, queue_current: 0}}));
    expect(screen.getByText('Queued jobs: 0')).toBeInTheDocument();
    expect(screen.getByText('Queue threshold / current: 20 / 0')).toBeInTheDocument();
    rerender(status({enabled: true, status: 'blocked'}));
    expect(screen.getByText('Queue threshold / current: - / -')).toBeInTheDocument();
});
