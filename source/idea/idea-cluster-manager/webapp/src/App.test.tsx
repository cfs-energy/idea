/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
 * with the License. A copy of the License is located at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
 * OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
 * and limitations under the License.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import { HashRouter } from 'react-router-dom';
import { vi } from 'vitest';
import App from './App';
import { initTestAppContext } from './test-support';
import { shellStore } from './pages/home/terminal/shell-store';

describe('web portal app', () => {
    it('mounts and shows the sign-in page when not authenticated', async () => {
        initTestAppContext();
        // HashRouter as in index.tsx: IdeaAuthenticatedRoute derives the current
        // route from window.location.hash.
        render(
            <HashRouter>
                <App />
            </HashRouter>
        );
        const signInElements = await screen.findAllByText('Sign In');
        expect(signInElements.length).toBeGreaterThan(0);
        expect(screen.getByText('Integrated Digital Engineering on AWS')).toBeInTheDocument();
    });

    describe('terminal dock across sessions', () => {
        const mount = async () => {
            const context = initTestAppContext();
            const hooks = vi.spyOn(context, 'setHooks');
            let reporting = () => {};
            vi.spyOn(context.auth(), 'subscribeReporting').mockImplementation(listener => {reporting = listener; return () => {};});
            const username = vi.spyOn(context.auth(), 'getUsername').mockReturnValue('alice');
            vi.spyOn(context.auth(), 'isLoggedIn').mockResolvedValue(false);
            render(<HashRouter><App /></HashRouter>);
            await waitFor(() => expect(hooks).toHaveBeenCalled());
            const [onLogin, onLogout] = hooks.mock.calls[0];
            return {context, onLogin, onLogout, username, reporting: () => act(() => reporting())};
        };
        beforeEach(() => {shellStore.reset(); localStorage.clear();});
        afterEach(() => {vi.restoreAllMocks(); shellStore.reset();});

        it('closes every terminal and keeps display preferences when the session ends', async () => {
            const {onLogout} = await mount();
            act(() => {shellStore.open('bastion', 'Bastion'); shellStore.open('desktop:one', 'Work');});
            localStorage.setItem('idea.terminal.height', '500');
            act(() => {void onLogout();});
            expect(shellStore.get()).toMatchObject({tabs: [], active: ''}); expect(shellStore.get().targets).toBeUndefined();
            expect(localStorage.getItem('idea.terminal.height')).toBe('500');
        });

        it('starts a new sign-in with the dock closed', async () => {
            const {context, onLogin} = await mount();
            vi.spyOn(context.getClusterSettingsService(), 'initialize').mockResolvedValue(undefined as any);
            act(() => shellStore.open('desktop:one', 'Work'));
            act(() => {void onLogin();});
            expect(shellStore.get().tabs).toEqual([]);
        });

        it('closes the previous user\'s terminals when a refresh signs in someone else', async () => {
            const {username, reporting} = await mount();
            reporting();
            act(() => shellStore.open('bastion', 'Bastion'));
            reporting();
            username.mockReturnValue('');
            reporting();
            expect(shellStore.get().tabs).toHaveLength(1);
            username.mockReturnValue('bob');
            reporting();
            expect(shellStore.get().tabs).toEqual([]);
        });
    });
});
