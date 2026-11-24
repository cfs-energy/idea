import {render, screen} from '@testing-library/react';
import {expect, it, vi} from 'vitest';
import {toast} from 'react-toastify';
import {notifyPortalUpdate} from './index';

it('offers a reload and stays until dismissed', () => {
    const info = vi.spyOn(toast, 'info').mockReturnValue('id');
    notifyPortalUpdate();
    const [content, options] = info.mock.calls[0];
    expect(options).toMatchObject({autoClose: false});
    render(content as React.ReactElement);
    expect(screen.getByText(/A new version of the portal is available/)).toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Reload'})).toBeInTheDocument();
});
