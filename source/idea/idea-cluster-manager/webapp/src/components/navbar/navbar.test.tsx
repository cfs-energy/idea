import {act, fireEvent, render, screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import IdeaNavbar from './navbar';
import {initTestAppContext} from '../../test-support';
import {GetCostTickerResult, GetMyCostsResult} from '../../client/data-model';
import {personalCostsCache} from '../../client/personal-costs-cache';

const ready: GetCostTickerResult = {enabled: true, period: 'MTD', total: 12.5, currency: 'USD', as_of: '2026-09-21T12:00:00Z'};

function setup(ticker: GetCostTickerResult = ready) {
    const context = initTestAppContext();
    const read = vi.spyOn(context.client().myCosts(), 'getCostTicker').mockResolvedValue(ticker);
    let update!: (costs: GetMyCostsResult) => void;
    const unsubscribe = vi.fn();
    const subscribe = vi.spyOn(personalCostsCache(), 'subscribe').mockImplementation(listener => {update = listener; return unsubscribe;});
    return {read, subscribe, unsubscribe, update: (costs: GetMyCostsResult) => update(costs), ...render(<IdeaNavbar/>)};
}

describe('cost ticker', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

    it.each(['MTD', 'WTD', 'QTD', 'YTD'] as const)('shows the amount and %s period without prefixes', async period => {
        setup({...ready, period});
        await act(async () => {});
        const link = screen.getByRole('link', {name: new RegExp(`${period} cost as of`)});
        expect(link).toHaveTextContent(`$12.50 · ${period}`);
        expect(link).toHaveAttribute('href', '#/home/my-costs');
        expect(link).toHaveAccessibleName(expect.stringContaining(`cost as of ${new Date(ready.as_of!).toLocaleString()}`));
        expect(screen.queryByText(/Known costs|Estimated costs/)).not.toBeInTheDocument();
    });

    it.each([true, undefined])('names the estimate state on the cost link for incomplete=%s', async incomplete => {
        setup({...ready, incomplete});
        await act(async () => {});
        const link = screen.getByRole('link', {name: /MTD cost as of/});
        expect(link).toHaveAccessibleName(expect.stringContaining('Some costs are still estimates'));
        expect(screen.queryByRole('button', {name: 'Cost information'})).not.toBeInTheDocument();
    });

    it('omits the estimate note for complete costs', async () => {
        setup({...ready, incomplete: false});
        await act(async () => {});
        const link = screen.getByRole('link', {name: /MTD cost as of/});
        expect(link).not.toHaveAccessibleName(expect.stringContaining('estimates'));
        expect(link).toHaveTextContent('$12.50 · MTD');
    });

    it('places the cost link between notifications and the user menu', async () => {
        setup({...ready, incomplete: false});
        await act(async () => {});
        const link = screen.getByRole('link', {name: /MTD cost as of/});
        const bell = screen.getByRole('button', {name: 'Notifications'});
        const buttons = screen.getAllByRole('button');
        const userMenu = buttons[buttons.length - 1];
        expect(bell.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(link.compareDocumentPosition(userMenu) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it.each([['USD', 0], ['EUR', 48.87], ['JPY', 1200], ['GBP', 9.25]] as const)('preserves %s currency and amount %s', async (currency, total) => {
        setup({...ready, currency, total});
        await act(async () => {});
        expect(screen.getByRole('link', {name: /MTD cost as of/})).toHaveTextContent(`${new Intl.NumberFormat(undefined, {style: 'currency', currency}).format(total)} · MTD`);
    });

    it('reserves no cost control when disabled', async () => {
        setup({enabled: false});
        await act(async () => {});
        expect(screen.queryByRole('link', {name: /cost as of/})).not.toBeInTheDocument();
        expect(screen.queryByRole('button', {name: 'Cost information'})).not.toBeInTheDocument();
    });

    it('retries a loading ticker and clears retries on unmount', async () => {
        const {read, unmount} = setup({...ready, total: undefined});
        await act(async () => {});
        expect(screen.queryByRole('link', {name: /cost as of/})).not.toBeInTheDocument();
        await act(async () => { vi.advanceTimersByTime(15000); });
        expect(read).toHaveBeenCalledTimes(2);
        unmount();
        await act(async () => { vi.advanceTimersByTime(300000); });
        expect(read).toHaveBeenCalledTimes(2);
    });

    it('polls only when visible and removes the visibility listener on unmount', async () => {
        const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
        const {read, unmount} = setup({enabled: false});
        await act(async () => {});
        await act(async () => { vi.advanceTimersByTime(300000); });
        expect(read).toHaveBeenCalledTimes(2);
        visibility.mockReturnValue('hidden');
        await act(async () => { vi.advanceTimersByTime(300000); });
        expect(read).toHaveBeenCalledTimes(2);
        visibility.mockReturnValue('visible');
        await act(async () => { fireEvent(document, new Event('visibilitychange')); });
        expect(read).toHaveBeenCalledTimes(3);
        unmount();
        await act(async () => { fireEvent(document, new Event('visibilitychange')); vi.advanceTimersByTime(300000); });
        expect(read).toHaveBeenCalledTimes(3);
    });

    it('updates MTD totals, currency, timestamp and completeness from the subscription', async () => {
        const {update, unsubscribe, unmount, subscribe} = setup();
        await act(async () => {});
        expect(subscribe).toHaveBeenCalledOnce();
        await act(async () => { update({current: {total: 0, incomplete: false}, currency: 'EUR', refreshed_at: '2026-09-22T12:00:00Z'} as GetMyCostsResult); });
        const link = screen.getByRole('link', {name: /MTD cost as of/});
        expect(link).toHaveTextContent(`${new Intl.NumberFormat(undefined, {style: 'currency', currency: 'EUR'}).format(0)} · MTD`);
        expect(link).toHaveAccessibleName(expect.stringContaining(`cost as of ${new Date('2026-09-22T12:00:00Z').toLocaleString()}`));
        expect(link).not.toHaveAccessibleName(expect.stringContaining('estimates'));
        await act(async () => { update({current: {total: 48.87, incomplete: true}, currency: 'USD'} as GetMyCostsResult); });
        expect(screen.getByRole('link', {name: /MTD cost as of/})).toHaveTextContent('$48.87 · MTD');
        expect(screen.getByRole('link', {name: /MTD cost as of/})).toHaveAccessibleName(expect.stringContaining('Some costs are still estimates'));
        unmount();
        expect(unsubscribe).toHaveBeenCalledOnce();
    });

    it('unsubscribes when the configured period changes and resubscribes for MTD', async () => {
        const {read, subscribe, unsubscribe, update} = setup();
        await act(async () => {});
        read.mockResolvedValue({...ready, period: 'QTD', total: 99, incomplete: false});
        await act(async () => { vi.advanceTimersByTime(300000); });
        expect(unsubscribe).toHaveBeenCalledOnce();
        await act(async () => { update({current: {total: 1, incomplete: true}, currency: 'USD'} as GetMyCostsResult); });
        expect(screen.getByRole('link', {name: /QTD cost as of/})).toHaveTextContent('$99.00 · QTD');
        read.mockResolvedValue(ready);
        await act(async () => { vi.advanceTimersByTime(300000); });
        expect(subscribe).toHaveBeenCalledTimes(2);
    });

    it('does not subscribe or schedule retries for a response after unmount', async () => {
        const {read, subscribe, unmount} = setup({enabled: false});
        await act(async () => {});
        let resolve!: (ticker: GetCostTickerResult) => void;
        read.mockReturnValue(new Promise(done => {resolve = done;}));
        await act(async () => { vi.advanceTimersByTime(300000); });
        unmount();
        await act(async () => { resolve({...ready, total: undefined}); });
        await act(async () => { vi.advanceTimersByTime(300000); });
        expect(read).toHaveBeenCalledTimes(2);
        expect(subscribe).not.toHaveBeenCalled();
    });
});
