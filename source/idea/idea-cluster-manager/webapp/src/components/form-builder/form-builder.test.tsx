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

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import IdeaFormBuilder from './form-builder';
import { initTestAppContext } from '../../test-support';

describe('form builder', () => {
    // The Form Builder tab is the only drag-and-drop surface in the web portal.
    // @hello-pangea/dnd emits data-rfd-* attributes (react-beautiful-dnd used data-rbd-*).
    it('renders a droppable list with one draggable per form field', async () => {
        initTestAppContext();
        render(
            <IdeaFormBuilder
                params={[
                    { name: 'field_one', title: 'Field One', data_type: 'str', param_type: 'text' },
                    { name: 'field_two', title: 'Field Two', data_type: 'str', param_type: 'text' }
                ]}
            />
        );

        await userEvent.click(await screen.findByRole('tab', { name: 'Form Builder' }));

        await waitFor(() => {
            expect(document.querySelector('[data-rfd-droppable-id="test"]')).not.toBeNull();
        });
        expect(document.querySelectorAll('[data-rfd-draggable-id]')).toHaveLength(2);
        // each field row carries edit/copy/delete FontAwesome icons
        expect(document.querySelectorAll('svg[data-icon="trash"]')).toHaveLength(2);
    });
});
