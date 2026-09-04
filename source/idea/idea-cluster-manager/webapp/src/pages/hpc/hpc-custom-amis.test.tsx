import {render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import HpcCustomAmis from './hpc-custom-amis';
import {initTestAppContext} from '../../test-support';

const COMPUTE_ROW = {base_os: 'rocky9', architecture: 'x86_64', state: 'stock', referenced_by: ['scheduler default']};
const DESKTOP_ROWS = [
    {base_os: 'rocky9', architecture: 'x86_64', stack_id: 'ss-base-rocky9-x86-64-base', state: 'stock', referenced_by: []},
    {base_os: 'amazonlinux2023', architecture: 'x86_64', stack_id: 'ss-base-amazonlinux2023-x86-64-base', state: 'stock', referenced_by: []}
];

const renderPage = (onFlashbarChange = () => {}) => {
    render(
        <MemoryRouter>
            <HpcCustomAmis
                ideaPageId="hpc-custom-amis"
                toolsOpen={false}
                tools={null}
                onToolsChange={() => {}}
                onPageChange={() => {}}
                sideNavHeader={{text: 'IDEA', href: '#/'}}
                sideNavItems={[]}
                onSideNavChange={() => {}}
                onFlashbarChange={onFlashbarChange}
                flashbarItems={[]}
            />
        </MemoryRouter>
    );
};

const primeContext = () => {
    const context = initTestAppContext();
    vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages').mockResolvedValue({listing: [COMPUTE_ROW]});
    vi.spyOn(context.client().virtualDesktopAdmin(), 'listDesktopImages').mockResolvedValue({listing: DESKTOP_ROWS});
    return context;
};

describe('custom amis page', () => {

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('build dialog sends both drivers by default', async () => {
        const context = primeContext();
        const build = vi.spyOn(context.client().schedulerAdmin(), 'buildComputeImage')
            .mockResolvedValue({record: {status: 'building', ami_name: 'idea-compute-node-rocky9-v1', base_ami: 'ami-stock'}});
        renderPage();
        await userEvent.click((await screen.findAllByRole('button', {name: 'Build'}))[0]);
        const buildButtons = screen.getAllByRole('button', {name: 'Build'});
        await userEvent.click(buildButtons[buildButtons.length - 1]);
        expect(build).toHaveBeenCalledWith({
            base_os: 'rocky9',
            architecture: 'x86_64',
            base_ami: undefined,
            instance_type: undefined,
            enable_drivers: ['efa', 'fsx_lustre']
        });
    });

    it('hides OSes with no image and adds one through Add image', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(false);
        const noneRow = {base_os: 'rocky10', architecture: 'x86_64', state: 'none', referenced_by: []};
        const listSpy = vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages')
            .mockResolvedValue({listing: [COMPUTE_ROW, noneRow], supported_base_os: ['rocky9', 'rocky10']});
        const build = vi.spyOn(context.client().schedulerAdmin(), 'buildComputeImage')
            .mockResolvedValue({record: {status: 'building'}});
        renderPage();
        expect(await screen.findByText('rocky9')).toBeInTheDocument();
        expect(screen.queryByText('rocky10')).not.toBeInTheDocument();
        listSpy.mockResolvedValue({listing: [COMPUTE_ROW, {...noneRow, state: 'building'}], supported_base_os: ['rocky9', 'rocky10']});
        await userEvent.click(screen.getByTestId('add-image'));
        // the picker defaults to the only missing OS; the modal primary is the last Build button
        const buildButtons = screen.getAllByRole('button', {name: 'Build'});
        await userEvent.click(buildButtons[buildButtons.length - 1]);
        expect(build).toHaveBeenCalledWith({
            base_os: 'rocky10',
            architecture: 'x86_64',
            base_ami: undefined,
            instance_type: undefined,
            enable_drivers: ['efa', 'fsx_lustre']
        });
        expect(await screen.findByText('Building')).toBeInTheDocument();
        expect(await screen.findByText('rocky10')).toBeInTheDocument();
    });

    it('add image offers the architecture an OS has no image for yet', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(false);
        vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages')
            .mockResolvedValue({listing: [COMPUTE_ROW], supported_base_os: ['rocky9']});
        const build = vi.spyOn(context.client().schedulerAdmin(), 'buildComputeImage')
            .mockResolvedValue({record: {status: 'building'}});
        renderPage();
        expect(await screen.findByText('rocky9')).toBeInTheDocument();
        await userEvent.click(screen.getByTestId('add-image'));
        // rocky9 x86_64 is already a row, so arm64 is the only combination left to offer
        expect(await screen.findByText('arm64')).toBeInTheDocument();
        const buildButtons = screen.getAllByRole('button', {name: 'Build'});
        await userEvent.click(buildButtons[buildButtons.length - 1]);
        expect(build).toHaveBeenCalledWith({
            base_os: 'rocky9',
            architecture: 'arm64',
            base_ami: undefined,
            instance_type: undefined,
            enable_drivers: ['efa', 'fsx_lustre']
        });
    });

    it('build all confirms in plain terms and reports the outcome', async () => {
        const context = primeContext();
        const buildAll = vi.spyOn(context.client().virtualDesktopAdmin(), 'buildAllDesktopImages')
            .mockResolvedValue({results: [
                {stack_id: 'ss-base-rocky9-x86-64-base', status: 'started'},
                {stack_id: 'ss-base-amazonlinux2023-x86-64-base', status: 'skipped', message: 'already running'}
            ]});
        const flash = vi.fn();
        renderPage(flash);
        await userEvent.click(await screen.findByTestId('build-all'));
        expect(await screen.findByText(/repointed at its new image only after that build succeeds/)).toBeInTheDocument();
        expect(screen.getByText(/2 builds/)).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Build all'}));
        expect(buildAll).toHaveBeenCalledWith({});
        await vi.waitFor(() => expect(flash).toHaveBeenCalled());
        const content = flash.mock.calls.map(call => call[0].items[0].content).join(' ');
        expect(content).toContain('1 started, 1 skipped');
    });

    it('use built image lists every change and repoints the default and the queue profiles', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(false);
        vi.spyOn(context.getClusterSettingsService(), 'getModuleId').mockReturnValue('scheduler');
        const al2023 = {
            base_os: 'amazonlinux2023', architecture: 'x86_64', image_id: 'ami-stock', state: 'stock',
            referenced_by: ['scheduler default', 'queue profile: compute', 'queue profile: job-shared', 'queue profile: test'],
            last_build: {status: 'complete', image_id: 'ami-built', ami_name: 'idea-compute-node-amazonlinux2023-v1'}
        };
        const ubuntu = {base_os: 'ubuntu2204', architecture: 'x86_64', image_id: 'ami-oldubuntu', state: 'built', referenced_by: []};
        vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages')
            .mockResolvedValue({listing: [al2023, ubuntu], supported_base_os: ['amazonlinux2023', 'ubuntu2204'], compute_node_os: 'amazonlinux2023'});
        const profile = (name: string) => ({name, queue_profile_id: `id-${name}`, default_job_params: {base_os: 'amazonlinux2023', instance_ami: 'ami-stock'}});
        vi.spyOn(context.client().schedulerAdmin(), 'listQueueProfiles')
            .mockResolvedValue({listing: [profile('compute'), profile('job-shared'), profile('test'), profile('other')]});
        const updateProfile = vi.spyOn(context.client().schedulerAdmin(), 'updateQueueProfile').mockResolvedValue({});
        const updateSettings = vi.spyOn(context.client().clusterSettings(), 'updateModuleSettings').mockResolvedValue({success: true});
        renderPage();

        expect(await screen.findByRole('button', {name: 'Use built image'})).toBeInTheDocument();
        // the ubuntu row is another operating system with no build: only Build, never Set as default
        expect(screen.queryByRole('button', {name: 'Set as default'})).not.toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Use built image'}));
        expect(await screen.findByText(/scheduler.compute_node_ami/)).toBeInTheDocument();
        for (const name of ['compute', 'job-shared', 'test']) {
            expect(screen.getByText(name, {selector: 'strong'})).toBeInTheDocument();
        }
        const buttons = screen.getAllByRole('button', {name: 'Use built image'});
        await userEvent.click(buttons[buttons.length - 1]);

        await vi.waitFor(() => expect(updateProfile).toHaveBeenCalledTimes(3));
        expect(updateSettings).toHaveBeenCalledWith({module_id: 'scheduler', settings: {compute_node_ami: 'ami-built'}});
        const repointed = updateProfile.mock.calls.map(call => call[0].queue_profile!);
        expect(repointed.map(p => p.name).sort()).toEqual(['compute', 'job-shared', 'test']);
        expect(repointed.every(p => p.default_job_params?.instance_ami === 'ami-built')).toBe(true);
        expect(repointed.every(p => p.default_job_params?.base_os === 'amazonlinux2023')).toBe(true);
    });

    it('set as default is offered only within the compute operating system', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(false);
        const rocky = {base_os: 'rocky9', architecture: 'x86_64', image_id: 'ami-rocky', state: 'built', referenced_by: ['queue profile: bio']};
        const list = vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages')
            .mockResolvedValue({listing: [rocky], supported_base_os: ['rocky9'], compute_node_os: 'rocky9'});
        const {unmount} = render(
            <MemoryRouter>
                <HpcCustomAmis ideaPageId="hpc-custom-amis" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
                    sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}} onFlashbarChange={() => {}} flashbarItems={[]}/>
            </MemoryRouter>
        );
        expect(await screen.findByRole('button', {name: 'Set as default'})).toBeInTheDocument();
        unmount();

        list.mockResolvedValue({listing: [rocky], supported_base_os: ['rocky9'], compute_node_os: 'amazonlinux2023'});
        renderPage();
        expect(await screen.findByText('rocky9')).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Set as default'})).not.toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Use built image'})).not.toBeInTheDocument();
    });

    it('a desktop row with a newer completed build offers Use built image and shows an outdated base', async () => {
        const context = primeContext();
        vi.spyOn(context.client().virtualDesktopAdmin(), 'listDesktopImages').mockResolvedValue({listing: [
            {
                base_os: 'rocky9', architecture: 'x86_64', stack_id: 'ss-base-rocky9-x86-64-base',
                image_id: 'ami-stock', base_ami_id: 'ami-stock', state: 'stock', referenced_by: ['ss-base-rocky9-x86-64-base'],
                last_build: {status: 'complete', image_id: 'ami-built'}
            },
            {
                base_os: 'rhel9', architecture: 'x86_64', stack_id: 'ss-base-rhel9-x86-64-base',
                image_id: 'ami-rhelbuilt', base_ami_id: 'ami-rhelnewbase', state: 'built_outdated', referenced_by: []
            }
        ]});
        const useBuilt = vi.spyOn(context.client().virtualDesktopAdmin(), 'useBuiltDesktopImages')
            .mockResolvedValue({results: [{stack_id: 'ss-base-rocky9-x86-64-base', status: 'updated'}]});
        renderPage();

        expect(await screen.findByText('Built (base outdated)')).toBeInTheDocument();
        // the base the stack would rebuild from is behind the image id now, not a third line
        await userEvent.click(screen.getByText('ami-rhelbuilt'));
        expect(await screen.findByText('base: ami-rhelnewbase')).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Use built image'}));
        expect(await screen.findByText(/last completed build/)).toBeInTheDocument();
        const buttons = screen.getAllByRole('button', {name: 'Use built image'});
        await userEvent.click(buttons[buttons.length - 1]);
        await vi.waitFor(() => expect(useBuilt).toHaveBeenCalledWith({stack_ids: ['ss-base-rocky9-x86-64-base']}));
    });

    it('keeps an arm64 build away from the x86_64 scheduler default', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(false);
        const x86 = {
            base_os: 'amazonlinux2023', architecture: 'x86_64', image_id: 'ami-x86', state: 'built',
            referenced_by: ['scheduler default'],
            last_build: {status: 'complete', image_id: 'ami-x86new'}
        };
        const arm64 = {
            base_os: 'amazonlinux2023', architecture: 'arm64', state: 'none', referenced_by: [],
            last_build: {status: 'complete', image_id: 'ami-arm64new'}
        };
        vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages')
            .mockResolvedValue({listing: [x86, arm64], supported_base_os: ['amazonlinux2023'], compute_node_os: 'amazonlinux2023'});
        renderPage();

        expect(await screen.findByText('Not in use')).toBeInTheDocument();
        // the x86_64 row matches the default and still adopts its build; the arm64 row offers neither action
        expect(screen.getAllByRole('button', {name: 'Use built image'})).toHaveLength(1);
        expect(screen.queryByRole('button', {name: 'Set as default'})).not.toBeInTheDocument();
        await userEvent.click(screen.getByText('Not in use'));
        expect(await screen.findByText(/Scheduler default runs x86_64/)).toBeInTheDocument();
    });

    it('adopts an arm64 build into the queue profiles that name the row', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(false);
        const x86 = {
            base_os: 'amazonlinux2023', architecture: 'x86_64', image_id: 'ami-x86', state: 'built',
            referenced_by: ['scheduler default']
        };
        const arm64 = {
            base_os: 'amazonlinux2023', architecture: 'arm64', image_id: 'ami-arm64', state: 'built',
            referenced_by: ['queue profile: graviton'],
            last_build: {status: 'complete', image_id: 'ami-arm64new'}
        };
        vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages')
            .mockResolvedValue({listing: [x86, arm64], supported_base_os: ['amazonlinux2023'], compute_node_os: 'amazonlinux2023'});
        renderPage();

        expect(await screen.findByRole('button', {name: 'Use built image'})).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Set as default'})).not.toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Use built image'}));
        // only the queue profile changes; the x86_64 default is not in the list
        expect(await screen.findByText('graviton', {selector: 'strong'})).toBeInTheDocument();
        expect(screen.queryByText(/scheduler.compute_node_ami/)).not.toBeInTheDocument();
    });

    it('leaves desktop rows out of the architecture guard', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
        vi.spyOn(context.client().schedulerAdmin(), 'listComputeImages')
            .mockResolvedValue({listing: [COMPUTE_ROW], compute_node_os: 'rocky9'});
        vi.spyOn(context.client().virtualDesktopAdmin(), 'listDesktopImages').mockResolvedValue({listing: [{
            base_os: 'amazonlinux2023', architecture: 'arm64', stack_id: 'ss-base-amazonlinux2023-arm64-base',
            image_id: 'ami-armstock', state: 'stock', referenced_by: ['ss-base-amazonlinux2023-arm64-base'],
            last_build: {status: 'complete', image_id: 'ami-armbuilt'}
        }]});
        renderPage();

        // the compute default runs x86_64, which says nothing about a desktop stack
        expect(await screen.findByRole('button', {name: 'Use built image'})).toBeInTheDocument();
    });
});
