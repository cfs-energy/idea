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

import { IdeaAuthenticationContext } from './authentication-context';
import { initTestAppData } from '../test-support';

describe('IdeaAuthenticationContext', () => {
    beforeEach(() => {
        initTestAppData();
    });

    it('initializes in local-storage mode and reports logged out with no tokens', async () => {
        const authContext = new IdeaAuthenticationContext({
            sessionManagement: 'local-storage',
            authEndpoint: 'http://localhost:8080/cluster-manager/api/v1'
        });
        await expect(authContext.isLoggedIn()).resolves.toBe(false);
    });

    it('has no access token before authentication', async () => {
        const authContext = new IdeaAuthenticationContext({
            sessionManagement: 'local-storage',
            authEndpoint: 'http://localhost:8080/cluster-manager/api/v1'
        });
        await expect(authContext.getAccessToken()).rejects.toMatchObject({
            error_code: 'AUTH_TOKEN_EXPIRED'
        });
    });
});
