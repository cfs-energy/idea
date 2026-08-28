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
