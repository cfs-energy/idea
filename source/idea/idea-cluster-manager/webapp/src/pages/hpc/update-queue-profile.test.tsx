import {render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {HpcUpdateQueueProfile} from './update-queue-profile';
import {initTestAppContext} from '../../test-support';

// round 5 B1: the wizard edits its values in place, and the page compared "stored" against
// the object the form had edited, so turning scratch off never sent the clears. these drive
// the real page: load, toggle the real control, submit, read the update request.

const FSX = 'fs-0123456789abcdef0.fsx.us-east-1.amazonaws.com';

const storedProfile = (enableScratch: boolean | undefined): any => ({
    queue_profile_id: 'qp-1',
    name: 'lustre',
    title: 'Lustre',
    enabled: true,
    projects: [{project_id: 'p1'}],
    queues: ['lustre'],
    queue_mode: 'fifo',
    keep_forever: false,
    scaling_mode: 'single-job',
    terminate_when_idle: 0,
    queue_management_params: {allow_new_fsx_lustre: false},
    default_job_params: {
        instance_types: ['c5.large'],
        base_os: 'amazonlinux2023',
        instance_ami: 'ami-0123456789abcdef0',
        security_groups: ['sg-1'],
        enable_scratch: enableScratch,
        scratch_provider: 'fsx-lustre-existing',
        fsx_lustre: {enabled: true, existing_fsx: FSX},
    },
});

const NOTICE = /Saving with scratch storage off clears this queue's stored scratch/;
const SPOT_NOTICE = /Saving with Spot off clears this queue's stored Spot price/;

async function setup(profile: any) {
    const context = initTestAppContext();
    const admin = context.client().schedulerAdmin();
    let current = profile;
    vi.spyOn(admin, 'getQueueProfile').mockImplementation(async () => ({queue_profile: current}));
    const update = vi.spyOn(admin, 'updateQueueProfile').mockImplementation(async (request: any) => {
        // the service removes a default sent as null
        const stripNulls = (value: any): any => value && typeof value === 'object' && !Array.isArray(value)
            ? Object.fromEntries(Object.entries(value).filter(([, v]) => v !== null).map(([k, v]) => [k, stripNulls(v)]))
            : value;
        current = stripNulls({...current, ...request.queue_profile});
        return {queue_profile: current};
    });
    vi.spyOn(context.client().projects(), 'listProjects').mockResolvedValue({
        listing: [{project_id: 'p1', name: 'p1', title: 'P1'}],
    } as any);
    const navigate = vi.fn();
    const view = render(<MemoryRouter>
        <HpcUpdateQueueProfile
            {...({ideaPageId: 'queues', flashbarItems: [], sideNavItems: [], sideNavHeader: {text: 'x', href: '#'}, toolsOpen: false, tools: null, onToolsChange: () => {}, onPageChange: () => {}, onSideNavChange: () => {}, onFlashbarChange: () => {}} as any)}
            location={{pathname: '/soca/queues/update', search: '?id=qp-1'}}
            navigate={navigate}
            searchParams={new URLSearchParams('id=qp-1')}
        />
    </MemoryRouter>);
    await screen.findByText('Queue Profile Settings', {selector: 'h1,h2,h3,span'}).catch(() => undefined);
    return {update, navigate, view, current: () => current};
}

const scratchBox = () => screen.getByRole('checkbox', {name: /Enable Scratch Storage/});

async function toDefaultsStep() {
    await waitFor(() => expect(screen.getAllByRole('button', {name: 'Next'}).length).toBeGreaterThan(0));
    await userEvent.click(screen.getAllByRole('button', {name: 'Next'})[0]);
    await waitFor(() => expect(scratchBox()).toBeInTheDocument());
}

async function submit() {
    // the review step follows the last section
    for (let step = 0; step < 5; step++) {
        const submitButton = screen.queryByRole('button', {name: 'Update Queue Profile'});
        if (submitButton) {
            await userEvent.click(submitButton);
            return;
        }
        await userEvent.click(screen.getAllByRole('button', {name: 'Next'})[0]);
    }
    throw new Error('no submit button');
}

const sentParams = (update: any, call = 0) => update.mock.calls[call][0].queue_profile.default_job_params;

describe('queue profile page: turning scratch off', () => {
    afterEach(() => vi.restoreAllMocks());

    it('sends the clears and shows the notice when this edit turns scratch off', async () => {
        const {update} = await setup(storedProfile(true));
        await toDefaultsStep();
        expect(scratchBox()).toBeChecked();
        expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
        await userEvent.click(scratchBox());
        expect(await screen.findByText(NOTICE)).toBeInTheDocument();
        await submit();
        await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
        const params = sentParams(update);
        expect(params.scratch_provider).toBeNull();
        expect(params.fsx_lustre.enabled).toBe(false);
        expect(params.fsx_lustre.existing_fsx).toBeNull();
        expect('scratch_off_notice' in (update.mock.calls[0][0] as any).queue_profile).toBe(false);
    });

    it('sends an untouched scratch-off profile as stored', async () => {
        const {update} = await setup(storedProfile(undefined));
        await toDefaultsStep();
        expect(scratchBox()).not.toBeChecked();
        expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
        await submit();
        await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
        const params = sentParams(update);
        expect(params.fsx_lustre).toEqual({enabled: true, existing_fsx: FSX});
        expect(params.scratch_provider).toBe('fsx-lustre-existing');
    });

    it('clears nothing when scratch is turned off and on again before saving', async () => {
        const {update} = await setup(storedProfile(true));
        await toDefaultsStep();
        await userEvent.click(scratchBox());
        await userEvent.click(scratchBox());
        expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
        await submit();
        await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
        const params = sentParams(update);
        expect(params.enable_scratch).toBe(true);
        expect(params.fsx_lustre.existing_fsx).toBe(FSX);
    });

    it('a second save after turning scratch off clears nothing more and restores nothing', async () => {
        const first = await setup(storedProfile(true));
        await toDefaultsStep();
        await userEvent.click(scratchBox());
        await submit();
        await waitFor(() => expect(first.update).toHaveBeenCalledTimes(1));
        const saved = first.current();
        first.view.unmount();
        vi.restoreAllMocks();

        // the page is opened again on what was saved and saved unchanged
        const second = await setup(saved);
        await toDefaultsStep();
        expect(scratchBox()).not.toBeChecked();
        expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
        await submit();
        await waitFor(() => expect(second.update).toHaveBeenCalledTimes(1));
        const params = sentParams(second.update);
        expect(params.fsx_lustre).toEqual({enabled: false});
        expect('scratch_provider' in params).toBe(false);
    });
});

describe('queue profile page: turning Spot off', () => {
    afterEach(() => vi.restoreAllMocks());

    const withSpot = (spot: boolean): any => {
        const profile = storedProfile(true);
        profile.default_job_params.spot = spot;
        profile.default_job_params.spot_price = {amount: 0.25, unit: 'USD'};
        profile.default_job_params.spot_allocation_count = 2;
        return profile;
    };
    const spotBox = () => screen.getByRole('checkbox', {name: /Use Spot Fleet/});

    it('clears the stored Spot defaults and says so when this edit turns Spot off', async () => {
        const {update} = await setup(withSpot(true));
        await toDefaultsStep();
        expect(spotBox()).toBeChecked();
        expect(screen.queryByText(SPOT_NOTICE)).not.toBeInTheDocument();
        await userEvent.click(spotBox());
        expect(await screen.findByText(SPOT_NOTICE)).toBeInTheDocument();
        await submit();
        await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
        const params = sentParams(update);
        expect(params.spot_price).toBeNull();
        expect(params.spot_allocation_count).toBeNull();
        expect('spot_off_notice' in (update.mock.calls[0][0] as any).queue_profile).toBe(false);
    });

    it('keeps the stored Spot defaults of a profile stored with Spot off', async () => {
        const {update} = await setup(withSpot(false));
        await toDefaultsStep();
        expect(spotBox()).not.toBeChecked();
        expect(screen.queryByText(SPOT_NOTICE)).not.toBeInTheDocument();
        await submit();
        await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
        const params = sentParams(update);
        expect(params.spot_price).toEqual({amount: 0.25, unit: 'USD'});
        expect(params.spot_allocation_count).toBe(2);
    });
});
