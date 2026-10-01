import {render, screen} from '@testing-library/react';
import QueueReference from './queue-reference';
import {initTestAppContext} from '../../test-support';

it('lists each queue with its architecture and operating system', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.client().scheduler(), 'listQueues').mockResolvedValue({listing: [
        {name: 'arm-normal', architecture: 'arm64', base_os: 'amazonlinux2023', instance_types: ['r8g.large']},
        {name: 'normal', architecture: 'x86_64', base_os: 'rhel9', instance_types: ['c6i.large', 'c6i.xlarge']},
    ]});
    render(<QueueReference/>);
    expect(await screen.findByText('Queues you can use (2)')).toBeInTheDocument();
    expect(screen.getByText('arm64')).toBeInTheDocument();
    expect(screen.getByText('RHEL 9')).toBeInTheDocument();
    expect(screen.getByText('c6i.large, c6i.xlarge')).toBeInTheDocument();
});

it('renders nothing when the queues cannot be read', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.client().scheduler(), 'listQueues').mockRejectedValue(new Error('denied'));
    const {container} = render(<QueueReference/>);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(container).toBeEmptyDOMElement();
});
