import {act, render, screen} from '@testing-library/react';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import IdeaNavbar from './navbar';
import {initTestAppContext} from '../../test-support';

describe('cost ticker', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

    it('renders a ready ticker and links to My costs', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.client().myCosts(), 'getCostTicker').mockResolvedValue({
            enabled: true, period: 'MTD', total: 12.5, currency: 'USD', as_of: '2026-09-21T12:00:00Z'
        });
        render(<IdeaNavbar/>);
        await act(async () => {});
        await act(async () => { vi.advanceTimersByTime(0); });
        const link = screen.getByRole('link', {name: /MTD cost as of/i});
        expect(link).toHaveTextContent(/12\.50.*MTD/);
        expect(link).toHaveAttribute('href', '#/home/my-costs');
        expect(screen.getByTitle(/^As of /)).toBeInTheDocument();
    });

    it('reserves no header utility when disabled', async () => {
        const context = initTestAppContext();
        vi.spyOn(context.client().myCosts(), 'getCostTicker').mockResolvedValue({enabled: false});
        render(<IdeaNavbar/>);
        await act(async () => {});
        expect(screen.queryByText(/MTD|WTD|QTD|YTD/)).not.toBeInTheDocument();
    });

    it('refreshes every five minutes while visible', async () => {
        const context = initTestAppContext();
        const read = vi.spyOn(context.client().myCosts(), 'getCostTicker').mockResolvedValue({enabled: false});
        render(<IdeaNavbar/>);
        await act(async () => {});
        expect(read).toHaveBeenCalledTimes(1);
        await act(async () => { vi.advanceTimersByTime(300000); });
        expect(read).toHaveBeenCalledTimes(2);
    });
});
