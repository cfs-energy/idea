import {render, screen} from '@testing-library/react';
import {MemoryRouter, Route, Routes, useLocation} from 'react-router-dom';
import {afterEach, expect, it, vi} from 'vitest';
import {LandingPage} from './landing-page';
import {initTestAppContext} from '../test-support';

const Location = () => <output>{useLocation().pathname}</output>;
const open = () => render(<MemoryRouter initialEntries={['/']}><Routes>
    <Route path="/" element={<LandingPage/>}/><Route path="*" element={<Location/>}/>
</Routes></MemoryRouter>);

afterEach(() => vi.restoreAllMocks());

it('redirects to the configured cluster landing page', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'getUser').mockResolvedValue({username: 'user-a'});
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({web_portal: {default_landing_page: 'files'}});
    open();
    expect(await screen.findByText('/home/file-browser')).toBeInTheDocument();
});

it('prefers the signed-in user override', async () => {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'getUser').mockResolvedValue({username: 'user-a', landing_page: 'my-costs'});
    vi.spyOn(context.getClusterSettingsService(), 'getModuleSettings').mockResolvedValue({web_portal: {default_landing_page: 'files'}});
    open();
    expect(await screen.findByText('/home/my-costs')).toBeInTheDocument();
});
