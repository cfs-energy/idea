import { render, screen } from '@testing-library/react';
import { describeExpiry, expiryDay, isBaseStack, VirtualDesktopSoftwareStacks } from './virtual-desktop-software-stacks';
import { expiryChange } from './forms/virtual-desktop-software-stack-edit-form';

describe('the expiry column', () => {
    it('says never for a stack with no date', () => {
        expect(describeExpiry({ stack_id: 'rebuilt' })).toBe('Never');
    });

    it('says expired in words for an expired stack', () => {
        render(<>{describeExpiry({ effective_expires_on: '2026-01-15T12:00:00Z', expiry_state: 'expired' })}</>);
        expect(screen.getByText(/Expired/)).toBeInTheDocument();
    });

    it('gives only the date for one with time left', () => {
        const cell = describeExpiry({ effective_expires_on: '2027-01-15T12:00:00Z' });
        expect(cell).toBe(new Date('2027-01-15T12:00:00Z').toLocaleDateString(undefined, { timeZone: 'UTC' }));
    });

    describe('shows the day that was typed, wherever the viewer is', () => {
        const zone = process.env.TZ;
        afterEach(() => {
            if (zone === undefined) delete process.env.TZ; else process.env.TZ = zone;
        });

        // stored as midnight UTC, which is the day before anywhere west of it
        const stored = '2027-03-15T00:00:00Z';
        it.each([
            ['America/Los_Angeles', 14],
            ['UTC', 15],
            ['Asia/Tokyo', 15],
            ['Pacific/Kiritimati', 15],
        ])('in %s', (viewer, dayOnTheirClock) => {
            process.env.TZ = viewer;
            // the zone took: this is the day a plain local format would show
            expect(new Date(stored).getDate()).toBe(dayOnTheirClock);
            // 15 March as this viewer's locale writes a date
            const typed = new Date(2027, 2, 15, 12).toLocaleDateString();
            expect(expiryDay(stored)).toBe(typed);
            expect(describeExpiry({ effective_expires_on: stored })).toBe(typed);
        });
    });
});

describe('the expiry date on the edit form', () => {
    it('sends a date that was typed', () => {
        expect(expiryChange('2027-01-15', undefined)).toEqual({ expires_on: '2027-01-15T00:00:00Z' });
    });

    it('asks to clear a date that was blanked', () => {
        expect(expiryChange('', '2027-01-15T00:00:00Z')).toEqual({ clear_expires_on: true });
    });

    it('says nothing when there was none and is none', () => {
        expect(expiryChange('', undefined)).toEqual({});
    });
});

describe('the delayed list refresh after a change', () => {
    afterEach(() => vi.useRealTimers());
    const page = () => {
        const stacks = new VirtualDesktopSoftwareStacks({} as any);
        const fetchRecords = vi.fn();
        (stacks as any).listing = {current: {fetchRecords}};
        return {stacks, fetchRecords};
    };

    it('reloads the list once, a few seconds after the last change', () => {
        vi.useFakeTimers();
        const {stacks, fetchRecords} = page();
        stacks.refreshSoon();
        stacks.refreshSoon();
        vi.advanceTimersByTime(3000);
        expect(fetchRecords).toHaveBeenCalledTimes(1);
    });

    it('does nothing once the page is left', () => {
        vi.useFakeTimers();
        const {stacks, fetchRecords} = page();
        stacks.refreshSoon();
        stacks.componentWillUnmount();
        (stacks as any).listing = {current: null};
        expect(() => vi.advanceTimersByTime(3000)).not.toThrow();
        expect(fetchRecords).not.toHaveBeenCalled();
    });
});

it('knows a base stack by the id the controller gives it', () => {
    expect(isBaseStack({stack_id: 'ss-base-amazonlinux2023-x86-64'})).toBe(true);
    expect(isBaseStack({stack_id: 'ss-marker'})).toBe(false);
    expect(isBaseStack(undefined)).toBe(false);
});

it('enables or disables a stack by sending only its id, OS and status', async () => {
    const {initTestAppContext} = await import('../../test-support');
    const context = initTestAppContext();
    const update = vi.spyOn(context.client().virtualDesktopAdmin(), 'updateSoftwareStack').mockResolvedValue({software_stack: {}} as any);
    const stacks = new VirtualDesktopSoftwareStacks({onFlashbarChange: () => {}} as any);
    const stale = {stack_id: 'ss-marker', base_os: 'amazonlinux2023', name: 'marker', description: 'stale', ami_id: 'ami-stale', enabled: true} as any;
    vi.spyOn(stacks, 'getSelectedSoftwareStacks').mockReturnValue([stale]);
    let state: any = {};
    (stacks as any).setState = (next: any, callback?: () => void) => {state = {...state, ...next}; callback?.();};
    (stacks as any).getActionConfirmModal = () => ({show: () => {}});
    (stacks as any).refreshSoon = () => {};
    (stacks as any).setFlashMessage = () => {};
    stacks.toggleSoftwareStackEnabled();
    state.confirmAction.onConfirm();
    await vi.waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][0]).toEqual({software_stack: {stack_id: 'ss-marker', base_os: 'amazonlinux2023', enabled: false}});
});
