import {render, screen} from '@testing-library/react';
import {vi} from 'vitest';
import IdeaErrorBoundary from '.';
import {installClientErrorReporting, resetClientErrorReporting} from '../../common/client-error-reporter';

function Bomb({armed}: {armed: boolean}) {
    if (armed) {
        throw new TypeError('Cannot read properties of undefined');
    }
    return <div>page content</div>;
}

describe('error boundary', () => {
    afterEach(() => {
        resetClientErrorReporting();
        vi.restoreAllMocks();
    });

    it('shows an error screen for a page that crashes, reports it, and clears on navigation', () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const send = vi.fn().mockResolvedValue(undefined);
        installClientErrorReporting(send, '26.11.2');
        const {rerender} = render(<IdeaErrorBoundary resetKey="/a"><Bomb armed/></IdeaErrorBoundary>);
        expect(screen.getByText('This page hit a problem')).toBeInTheDocument();
        expect(screen.getByRole('button', {name: 'Reload page'})).toBeInTheDocument();
        expect(screen.queryByText(/Cannot read/)).toBeNull();
        expect(send).toHaveBeenCalledWith(expect.objectContaining({error_type: 'render_crash', message: 'TypeError Cannot read properties of undefined'}));
        rerender(<IdeaErrorBoundary resetKey="/b"><Bomb armed={false}/></IdeaErrorBoundary>);
        expect(screen.getByText('page content')).toBeInTheDocument();
    });
});
