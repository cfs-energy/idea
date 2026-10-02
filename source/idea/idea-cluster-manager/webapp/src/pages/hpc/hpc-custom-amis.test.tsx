import {act, render, screen, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import {MemoryRouter} from 'react-router-dom';
import HpcCustomAmis, {toRowFilter} from './hpc-custom-amis';
import {initTestAppContext} from '../../test-support';
import {ImageBuildRecord} from '../../client/data-model';

const ROCKY: ImageBuildRecord = {
    kind: 'desktop', base_os: 'rocky9', architecture: 'x86_64', variant: 'cpu', status: 'current',
    validated_on: '2026-10-01T02:00:00Z', release: '26.10.1', current_image_id: 'ami-rocky-new', previous_image_id: 'ami-rocky-old'
};
const UBUNTU: ImageBuildRecord = {kind: 'desktop', base_os: 'ubuntu2404', architecture: 'x86_64', variant: 'nvidia', status: 'current', current_image_id: 'ami-ubuntu'};
const COMPUTE: ImageBuildRecord = {kind: 'compute', base_os: 'rocky9', architecture: 'arm64', variant: 'cpu', status: 'failed', error: 'Lustre module did not load', current_image_id: 'ami-c-old'};

const renderPage = (onFlashbarChange = vi.fn()) => {
    render(
        <MemoryRouter>
            <HpcCustomAmis ideaPageId="hpc-custom-amis" toolsOpen={false} tools={null} onToolsChange={() => {}} onPageChange={() => {}}
                sideNavHeader={{text: 'IDEA', href: '#/'}} sideNavItems={[]} onSideNavChange={() => {}}
                onFlashbarChange={onFlashbarChange} flashbarItems={[]}/>
        </MemoryRouter>
    );
    return onFlashbarChange;
};

const prime = (desktop: ImageBuildRecord[] = [ROCKY, UBUNTU], compute: ImageBuildRecord[] = [COMPUTE]) => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'isVirtualDesktopDeployed').mockReturnValue(true);
    vi.spyOn(context.getClusterSettingsService(), 'getClusterSettings').mockResolvedValue({timezone: 'America/New_York'});
    const vda = context.client().virtualDesktopAdmin();
    const scheduler = context.client().schedulerAdmin();
    return {
        context,
        listDesktop: vi.spyOn(vda, 'listImageRows').mockResolvedValue({listing: desktop}),
        listCompute: vi.spyOn(scheduler, 'listImageRows').mockResolvedValue({listing: compute}),
        refreshDesktop: vi.spyOn(vda, 'refreshImages').mockResolvedValue({results: [{outcome: 'queued'}, {outcome: 'in_flight'}]}),
        refreshCompute: vi.spyOn(scheduler, 'refreshImages').mockResolvedValue({results: [{outcome: 'queued'}]}),
        getSchedule: vi.spyOn(vda, 'getImageSchedule').mockResolvedValue({
            schedule: {enabled: true, day: 'first sunday', hour: 2}, last_run_on: '2026-09-06T06:00:00Z', next_run_on: '2026-10-04T06:00:00Z'
        })
    };
};

const rowOf = (text: string) => screen.getAllByText(text)[0].closest('tr')!;

describe('images page', () => {

    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('renders every status in plain words', async () => {
        const at = (status: string, extra: Partial<ImageBuildRecord> = {}): ImageBuildRecord =>
            ({kind: 'desktop', base_os: `os-${status}`, architecture: 'x86_64', status, ...extra});
        prime([
            at('current', {validated_on: '2026-10-01T12:00:00Z', release: '26.10.1'}),
            at('queued'),
            at('resolving'),
            at('building'),
            at('checking'),
            at('test_launching'),
            at('promoting'),
            at('failed', {error: 'Desktop did not reach Ready in 5 minutes', current_image_id: 'ami-prev', log_link: 'https://logs.example/x'}),
            at('waiting_capacity', {retry_after: '2026-10-02T18:00:00Z'}),
            at('pinned', {current_image_id: 'ami-held'}),
            at('current', {base_os: 'os-rowpin', pinned: true}),
            at('unsupported')
        ], []);
        renderPage();

        expect(await screen.findByText(/^Current – validated .* \(release 26\.10\.1\)$/)).toBeInTheDocument();
        expect(screen.getByText('Baking: queued')).toBeInTheDocument();
        expect(screen.getByText('Baking: finding the newest vendor image (step 1 of 5)')).toBeInTheDocument();
        expect(screen.getByText('Baking: building (step 2 of 5)')).toBeInTheDocument();
        expect(screen.getByText('Baking: checking (step 3 of 5)')).toBeInTheDocument();
        expect(screen.getByText('Baking: test-launching (step 4 of 5)')).toBeInTheDocument();
        expect(screen.getByText('Baking: switching new launches (step 5 of 5)')).toBeInTheDocument();
        expect(screen.getByText('Failed: Desktop did not reach Ready in 5 minutes – still on ami-prev')).toBeInTheDocument();
        expect(screen.getByRole('link', {name: /View log/})).toHaveAttribute('href', 'https://logs.example/x');
        expect(screen.getByText(/^Waiting for capacity – retry /)).toBeInTheDocument();
        expect(screen.getByText('Pinned to ami-held')).toBeInTheDocument();
        expect(within(rowOf('os-rowpin')).getByText('Pinned')).toBeInTheDocument();
        expect(within(rowOf('os-rowpin')).getByRole('button', {name: 'Unpin'})).toBeInTheDocument();
        expect(screen.getByText('Unsupported in this region')).toBeInTheDocument();
    });

    it('refreshes selected rows by key, split per module', async () => {
        const mocks = prime();
        const flash = renderPage();
        await screen.findByText('ubuntu2404');

        await userEvent.click(within(rowOf('ubuntu2404')).getByRole('checkbox'));
        await userEvent.click(within(rowOf('arm64 · CPU')).getByRole('checkbox'));
        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate selected'}));
        expect(await screen.findByText(/test-launches a desktop or a job from it/)).toBeInTheDocument();
        expect(screen.getByText(/Rows already in progress are skipped/)).toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate'}));

        await vi.waitFor(() => expect(flash).toHaveBeenCalled());
        expect(mocks.refreshDesktop).toHaveBeenCalledWith({rows: [{kind: 'desktop', base_os: 'ubuntu2404', architecture: 'x86_64', variant: 'nvidia'}]});
        expect(mocks.refreshCompute).toHaveBeenCalledWith({rows: [{kind: 'compute', base_os: 'rocky9', architecture: 'arm64', variant: 'cpu'}]});
        expect(flash.mock.calls[0][0].items[0].content).toBe('Queued 2, already in progress 1.');
    });

    it('refreshes all with all, a filtered view with the filter, and free text with the shown keys', async () => {
        const mocks = prime();
        renderPage();
        await screen.findByText('ubuntu2404');

        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate all'}));
        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate'}));
        await vi.waitFor(() => expect(mocks.refreshDesktop).toHaveBeenCalledWith({all: true}));
        expect(mocks.refreshCompute).toHaveBeenCalledWith({all: true});

        // OS family = rocky, kind = desktop: one service-side filter on the desktop module only
        const selects = createWrapper().findAllMultiselects();
        for (const [index, value] of [[0, 'desktop'], [1, 'rocky']] as const) {
            act(() => selects[index].openDropdown());
            act(() => selects[index].selectOptionByValue(value));
            act(() => selects[index].closeDropdown());
        }
        mocks.refreshDesktop.mockClear();
        mocks.refreshCompute.mockClear();
        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate shown (1)'}));
        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate'}));
        await vi.waitFor(() => expect(mocks.refreshDesktop).toHaveBeenCalledWith({filter: {kind: 'desktop', base_os_family: 'rocky'}}));
        expect(mocks.refreshCompute).not.toHaveBeenCalled();

        // free text cannot travel as a row filter, so the shown row goes by key
        mocks.refreshDesktop.mockClear();
        await userEvent.type(screen.getByRole('searchbox', {name: 'Find images'}), 'ami-rocky-new');
        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate shown (1)'}));
        await userEvent.click(screen.getByRole('button', {name: 'Refresh and validate'}));
        await vi.waitFor(() => expect(mocks.refreshDesktop)
            .toHaveBeenCalledWith({rows: [{kind: 'desktop', base_os: 'rocky9', architecture: 'x86_64', variant: 'cpu'}]}));
    });

    it('turns the table filters into a row filter only when they fit one', () => {
        const none = {kind: [], family: [], architecture: [], variant: [], status: []};
        expect(toRowFilter({...none, variant: ['nvidia'], status: ['Failed', 'Waiting for capacity']}, ''))
            .toEqual({variant: 'nvidia', statuses: ['failed', 'waiting_capacity']});
        expect(toRowFilter({...none, family: ['rocky', 'ubuntu']}, '')).toBeUndefined();
        expect(toRowFilter(none, 'ami-123')).toBeUndefined();
    });

    it('rolls back after a plain confirmation, and pins and unpins', async () => {
        const mocks = prime([ROCKY, {...UBUNTU, pinned: true}], []);
        const vda = mocks.context.client().virtualDesktopAdmin();
        const rollback = vi.spyOn(vda, 'rollbackImage').mockResolvedValue({});
        const pin = vi.spyOn(vda, 'setImagePinned').mockResolvedValue({});
        renderPage();
        await screen.findByText('ubuntu2404');

        await userEvent.click(within(rowOf('rocky9')).getByRole('button', {name: 'Roll back'}));
        expect(await screen.findByText(/pauses|paused until the next manual refresh/)).toBeInTheDocument();
        expect(screen.getByText(/to the previous validated image, ami-rocky-old/)).toBeInTheDocument();
        const buttons = screen.getAllByRole('button', {name: 'Roll back'});
        await userEvent.click(buttons[buttons.length - 1]);
        await vi.waitFor(() => expect(rollback).toHaveBeenCalledWith({row: {kind: 'desktop', base_os: 'rocky9', architecture: 'x86_64', variant: 'cpu'}}));

        await userEvent.click(within(rowOf('rocky9')).getByRole('button', {name: 'Pin'}));
        await userEvent.click(within(rowOf('ubuntu2404')).getByRole('button', {name: 'Unpin'}));
        expect(pin).toHaveBeenCalledWith({row: {kind: 'desktop', base_os: 'rocky9', architecture: 'x86_64', variant: 'cpu'}, pinned: true});
        expect(pin).toHaveBeenCalledWith({row: {kind: 'desktop', base_os: 'ubuntu2404', architecture: 'x86_64', variant: 'nvidia'}, pinned: false});
        // a row with no previous image has nothing to roll back to
        expect(within(rowOf('ubuntu2404')).getByRole('button', {name: 'Roll back'})).toBeDisabled();
    });

    it('shows the checks behind a row', async () => {
        prime([{...ROCKY, source_ami: 'ami-vendor', trigger: 'monthly', requested_by: 'system', attempts: 2,
            checks: [{name: 'lustre_module', ok: true, seconds: 3}, {name: 'dcv_session', ok: false, detail: 'no session listed', seconds: 41}]}], []);
        renderPage();
        await screen.findByText('rocky9');
        await userEvent.click(screen.getByRole('button', {name: 'Details'}));
        expect(await screen.findByText('ami-vendor')).toBeInTheDocument();
        expect(screen.getByText('monthly')).toBeInTheDocument();
        expect(screen.getByText('no session listed')).toBeInTheDocument();
        expect(screen.getByText('Passed')).toBeInTheDocument();
        expect(screen.getByText('41')).toBeInTheDocument();
    });

    it('shows the vendor check schedule and saves an edit', async () => {
        const mocks = prime();
        const update = vi.spyOn(mocks.context.client().virtualDesktopAdmin(), 'updateImageSchedule').mockResolvedValue({});
        renderPage();
        await vi.waitFor(() => expect(screen.getByTestId('schedule-line'))
            .toHaveTextContent(/^Checks for new vendor images: First Sunday of each month at 02:00 \(America\/New_York\) · last run .+ · next run .+$/));

        await userEvent.click(screen.getByRole('button', {name: 'Edit'}));
        createWrapper().findModal()!.findContent().findSelect()!.openDropdown();
        act(() => createWrapper().findModal()!.findContent().findSelect()!.selectOptionByValue('5'));
        await userEvent.click(screen.getByRole('button', {name: 'Save'}));
        await vi.waitFor(() => expect(update).toHaveBeenCalledWith({schedule: {enabled: true, day: 'first sunday', hour: 5}}));
        expect(mocks.getSchedule).toHaveBeenCalledTimes(2);
    });

    it('polls every 30 seconds while a row is baking and stops once every row is idle', async () => {
        vi.useFakeTimers({shouldAdvanceTime: true});
        const mocks = prime([{...ROCKY, status: 'building'}], []);
        mocks.listDesktop
            .mockResolvedValueOnce({listing: [{...ROCKY, status: 'building'}]})
            .mockResolvedValue({listing: [ROCKY]});
        renderPage();
        expect(await screen.findByText('Baking: building (step 2 of 5)')).toBeInTheDocument();
        expect(screen.getByText('Rows in progress refresh every 30 seconds.')).toBeInTheDocument();

        await act(() => vi.advanceTimersByTimeAsync(30000));
        expect(await screen.findByText(/^Current – validated/)).toBeInTheDocument();
        expect(mocks.listDesktop).toHaveBeenCalledTimes(2);

        await act(() => vi.advanceTimersByTimeAsync(120000));
        expect(mocks.listDesktop).toHaveBeenCalledTimes(2);
    });

    it('lists compute rows only for a jobs-only administrator', async () => {
        const mocks = prime();
        vi.spyOn(mocks.context.auth(), 'isModuleAdmin').mockImplementation(module => module === 'scheduler');
        const getComputeSchedule = vi.spyOn(mocks.context.client().schedulerAdmin(), 'getImageSchedule').mockResolvedValue({
            schedule: {enabled: true, day: 'first sunday', hour: 2}, next_run_on: '2026-10-04T06:00:00Z'
        });
        renderPage();
        expect(await screen.findByText('arm64 · CPU')).toBeInTheDocument();
        expect(mocks.listDesktop).not.toHaveBeenCalled();
        // the schedule is read from the scheduler and cannot be edited there
        expect(await screen.findByTestId('schedule-line')).toBeInTheDocument();
        expect(getComputeSchedule).toHaveBeenCalled();
        expect(mocks.getSchedule).not.toHaveBeenCalled();
        expect(screen.queryByRole('button', {name: 'Edit'})).not.toBeInTheDocument();
    });
});

describe('custom images tab', () => {

    const COMPUTE_ROW = {base_os: 'rocky9', architecture: 'x86_64', state: 'stock', image_id: 'ami-stock', referenced_by: ['scheduler default']};
    const DESKTOP_ROW = {base_os: 'rocky9', architecture: 'x86_64', stack_id: 'ss-base-rocky9-x86-64-base', state: 'stock', referenced_by: []};

    const primeCustom = (compute: any = {listing: [COMPUTE_ROW], supported_base_os: ['rocky9'], compute_node_os: 'rocky9'}, desktop: any[] = [DESKTOP_ROW]) => {
        const mocks = prime([], []);
        const scheduler = mocks.context.client().schedulerAdmin();
        const vda = mocks.context.client().virtualDesktopAdmin();
        return {
            ...mocks,
            listComputeImages: vi.spyOn(scheduler, 'listComputeImages').mockResolvedValue(compute),
            listDesktopImages: vi.spyOn(vda, 'listDesktopImages').mockResolvedValue({listing: desktop}),
            buildCompute: vi.spyOn(scheduler, 'buildComputeImage').mockResolvedValue({record: {status: 'building'}}),
            buildDesktop: vi.spyOn(vda, 'buildDesktopImage').mockResolvedValue({record: {status: 'building'}})
        };
    };

    const openCustom = async () => {
        await userEvent.click(await screen.findByRole('tab', {name: 'Custom images'}));
    };

    const clickModalBuild = async () => {
        const buttons = screen.getAllByRole('button', {name: 'Build'});
        await userEvent.click(buttons[buttons.length - 1]);
    };

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('lists custom images only once the tab is opened', async () => {
        const mocks = primeCustom();
        renderPage();
        await screen.findByRole('heading', {name: 'Images'});
        expect(mocks.listComputeImages).not.toHaveBeenCalled();
        await openCustom();
        expect(await screen.findByRole('heading', {name: 'Compute images'})).toBeInTheDocument();
        expect(screen.getByRole('heading', {name: 'Desktop images'})).toBeInTheDocument();
    });

    it('custom compute build sends both drivers by default', async () => {
        const mocks = primeCustom(undefined, []);
        renderPage();
        await openCustom();
        await userEvent.click((await screen.findAllByRole('button', {name: 'Build'}))[0]);
        expect(screen.getByText(/the managed refresh never touches it/)).toBeInTheDocument();
        await clickModalBuild();
        expect(mocks.buildCompute).toHaveBeenCalledWith({
            base_os: 'rocky9', architecture: 'x86_64', base_ami: undefined, instance_type: undefined, enable_drivers: ['efa', 'fsx_lustre']
        });
    });

    it('custom desktop build never repoints a base stack', async () => {
        const mocks = primeCustom({listing: []});
        renderPage();
        await openCustom();
        await userEvent.click(await screen.findByRole('button', {name: 'Build'}));
        await userEvent.type(screen.getByPlaceholderText('ami-...'), 'ami-project');
        await clickModalBuild();
        expect(mocks.buildDesktop).toHaveBeenCalledWith({
            base_os: 'rocky9', architecture: 'x86_64', base_ami: 'ami-project', instance_type: undefined, update_stack: false
        });
    });

    it('add image hides OSes with no image and builds the missing combination', async () => {
        const noneRow = {base_os: 'rocky10', architecture: 'x86_64', state: 'none', referenced_by: []};
        const mocks = primeCustom({listing: [COMPUTE_ROW, noneRow], supported_base_os: ['rocky9', 'rocky10']}, []);
        renderPage();
        await openCustom();
        expect(await screen.findByText('rocky9')).toBeInTheDocument();
        expect(screen.queryByText('rocky10')).not.toBeInTheDocument();
        await userEvent.click(screen.getByTestId('add-image'));
        await clickModalBuild();
        expect(mocks.buildCompute).toHaveBeenCalledWith(expect.objectContaining({base_os: 'rocky10', architecture: 'x86_64'}));
    });

    it('add image offers the architecture an OS has no image for yet', async () => {
        const mocks = primeCustom({listing: [COMPUTE_ROW], supported_base_os: ['rocky9']}, []);
        renderPage();
        await openCustom();
        await screen.findByText('rocky9');
        await userEvent.click(screen.getByTestId('add-image'));
        expect(await screen.findByText('arm64')).toBeInTheDocument();
        await clickModalBuild();
        expect(mocks.buildCompute).toHaveBeenCalledWith(expect.objectContaining({base_os: 'rocky9', architecture: 'arm64'}));
    });

    it('sets a completed custom build as the scheduler default, only within the compute OS', async () => {
        const built = {...COMPUTE_ROW, last_build: {status: 'complete', image_id: 'ami-custom'}};
        const other = {base_os: 'ubuntu2404', architecture: 'x86_64', state: 'built', image_id: 'ami-ubuntu', referenced_by: []};
        const mocks = primeCustom({listing: [built, other], supported_base_os: ['rocky9', 'ubuntu2404'], compute_node_os: 'rocky9'}, []);
        vi.spyOn(mocks.context.getClusterSettingsService(), 'getModuleId').mockReturnValue('scheduler');
        const update = vi.spyOn(mocks.context.client().clusterSettings(), 'updateModuleSettings').mockResolvedValue({success: true});
        renderPage();
        await openCustom();
        expect(await screen.findAllByRole('button', {name: 'Set as default'})).toHaveLength(1);
        expect(screen.queryByRole('button', {name: 'Use built image'})).not.toBeInTheDocument();
        await userEvent.click(screen.getByRole('button', {name: 'Set as default'}));
        const buttons = screen.getAllByRole('button', {name: 'Set as default'});
        await userEvent.click(buttons[buttons.length - 1]);
        await vi.waitFor(() => expect(update).toHaveBeenCalledWith({module_id: 'scheduler', settings: {compute_node_ami: 'ami-custom'}}));
    });

    it('keeps an arm64 image away from the x86_64 scheduler default', async () => {
        const arm64 = {base_os: 'rocky9', architecture: 'arm64', state: 'none', referenced_by: [], last_build: {status: 'complete', image_id: 'ami-arm'}};
        primeCustom({listing: [COMPUTE_ROW, arm64], supported_base_os: ['rocky9'], compute_node_os: 'rocky9'}, []);
        renderPage();
        await openCustom();
        expect(await screen.findByText('Not in use')).toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Set as default'})).not.toBeInTheDocument();
        await userEvent.click(screen.getByText('Not in use'));
        expect(await screen.findByText(/Scheduler default runs x86_64/)).toBeInTheDocument();
    });
});
