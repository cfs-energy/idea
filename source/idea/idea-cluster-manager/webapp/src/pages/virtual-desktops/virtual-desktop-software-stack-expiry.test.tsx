import { render, screen } from '@testing-library/react';
import { describeExpiry, expiryDay } from './virtual-desktop-software-stacks';
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
