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

import { render, waitFor } from '@testing-library/react';
import { SideNavigationProps } from '@cloudscape-design/components';
import IdeaSideNavigation from './index';
import { Constants } from '../../common/constants';

const items: SideNavigationProps.Item[] = [
    { type: 'link', text: 'Dashboard', href: '#/' },
    { type: 'divider' },
    { type: 'link', text: Constants.ADMIN_ZONE_LINK_TEXT, href: '#' },
    { type: 'link', text: 'Projects', href: '#/cluster/projects' }
];

describe('side navigation', () => {
    // side-navigation.scss styles the admin-zone entry as a section heading
    // through this id. The lookup that sets it used to key on a Cloudscape class
    // name, which stopped matching years ago.
    it('tags the admin-zone entry so the stylesheet can reach it', () => {
        const { container } = render(
            <IdeaSideNavigation
                sideNavHeader={{ text: 'IDEA', href: '#/' }}
                sideNavItems={items}
                onSideNavChange={() => {}}
                navigate={() => {}}
                location={{ pathname: '/' } as any}
                params={{}}
                searchParams={new URLSearchParams()}
                setSearchParams={() => {}}
            />
        );

        const tagged = container.querySelector('#idea-admin-zone-link');
        expect(tagged).not.toBeNull();
        expect(tagged!.textContent).toBe(Constants.ADMIN_ZONE_LINK_TEXT);
        expect(container.querySelectorAll('#idea-admin-zone-link')).toHaveLength(1);
    });

    // App renders the side nav before its items load, so the anchors that carry
    // the tag do not exist at mount.
    it('tags the admin-zone entry when the items arrive after mount', async () => {
        const props = {
            sideNavHeader: { text: 'IDEA', href: '#/' },
            onSideNavChange: () => {},
            navigate: () => {},
            location: { pathname: '/' } as any,
            params: {},
            searchParams: new URLSearchParams(),
            setSearchParams: () => {}
        };
        const { container, rerender } = render(<IdeaSideNavigation {...props} sideNavItems={[]} />);
        expect(container.querySelector('#idea-admin-zone-link')).toBeNull();

        rerender(<IdeaSideNavigation {...props} sideNavItems={items} />);
        await waitFor(() => expect(container.querySelector('#idea-admin-zone-link')).not.toBeNull());
        expect(container.querySelectorAll('#idea-admin-zone-link')).toHaveLength(1);
    });
});
