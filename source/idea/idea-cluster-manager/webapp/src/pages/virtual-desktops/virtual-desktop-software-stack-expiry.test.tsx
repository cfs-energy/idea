import { render, screen } from '@testing-library/react';
import { describeExpiry } from './virtual-desktop-software-stacks';
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

    it('shows the day that was typed, wherever the viewer is', () => {
        // stored as midnight UTC, which is the day before anywhere west of it
        const midnight = new Date('2027-01-15T00:00:00Z');
        expect(describeExpiry({ effective_expires_on: '2027-01-15T00:00:00Z' }))
            .toBe(new Intl.DateTimeFormat(undefined, { timeZone: 'UTC' }).format(midnight));
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
