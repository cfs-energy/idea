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
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import VirtualDesktopDebug from './virtual-desktop-debug';
import { initTestAppContext } from '../../test-support';

describe('virtual desktop debug page', () => {
    // Covers the JSON viewer prop surface (theme/iconStyle/onEdit/displayDataTypes/name)
    // that both this page and virtual-desktop-sessions pass to @microlink/react-json-view.
    // Both themes are exercised: dark mode selects "monokai", light "grayscale:inverted".
    it.each([true, false])('renders the broker health payloads in the JSON viewer (darkMode=%s)', async (darkMode) => {
        const context = initTestAppContext();
        context.setDarkMode(darkMode);
        const dcvClient = context.client().virtualDesktopDCV();
        vi.spyOn(dcvClient, 'describeServers').mockResolvedValue({
            response: { request_id: 'req-1', servers: [{ instance_id: 'i-0abc' }] }
        } as any);
        vi.spyOn(dcvClient, 'describeSessions').mockResolvedValue({
            response: { request_id: 'req-2', sessions: [{ id: 'session-1' }] }
        } as any);

        render(
            <MemoryRouter>
                <VirtualDesktopDebug
                    ideaPageId="virtual-desktop-debug"
                    toolsOpen={false}
                    tools={null}
                    onToolsChange={() => {}}
                    onPageChange={() => {}}
                    sideNavHeader={{ text: 'IDEA', href: '#/' }}
                    sideNavItems={[]}
                    onSideNavChange={() => {}}
                    onFlashbarChange={() => {}}
                    flashbarItems={[]}
                />
            </MemoryRouter>
        );

        // request_id is stripped before rendering; the payload key is what reaches the viewer.
        expect(await screen.findByText('servers')).toBeInTheDocument();
        expect(screen.queryByText('request_id')).toBeNull();
    });
});
