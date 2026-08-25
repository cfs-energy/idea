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

import { render, screen } from '@testing-library/react';
import { HashRouter } from 'react-router-dom';
import App from './App';
import { initTestAppContext } from './test-support';

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
});
