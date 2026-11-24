import React from 'react';
import {render, screen} from '@testing-library/react';
import {expect, it, vi} from 'vitest';
import StorageMeasurementSettings from './storage-measurement-settings';

vi.mock('./catalog-settings-section', () => ({default: ({children}: {children?: React.ReactNode}) => <div>{children}</div>}));

const SVM = 'svm-1.fs-1.fsx.us-east-2.amazonaws.com';
const shown = (apps: Record<string, unknown>) => {
    const values = {
        metrics: {provider: 'dogstatsd'},
        'cluster-manager': {metrics: {storage: {enabled: true}}},
        'shared-storage': {
            data: {provider: 'fsx_netapp_ontap', fsx_netapp_ontap: {svm: {management_dns: SVM}, metrics: {username: 'reader', password_secret_arn: 'arn'}}},
            ideaapps: {provider: 'fsx_netapp_ontap', fsx_netapp_ontap: apps},
            scratch: {provider: 'fsx_lustre'},
        },
    };
    const view = render(<StorageMeasurementSettings settings={[]} values={values} moduleId={module => module} title="Storage measurement" editing={false} editDisabled={false} onEdit={() => {}} onEditingEnd={() => {}} onSaved={() => {}}/>);
    const found = screen.queryByText('Set up ONTAP measurement') != null;
    view.unmount();
    return found;
};

it('asks for a login only for an SVM that no attachment reads', () => {
    // idea-dev: /idea_apps is a second volume of the SVM that /data already reads
    expect(shown({svm: {management_dns: `https://${SVM.toUpperCase()}/`}})).toBe(false);
    expect(shown({svm: {management_dns: 'svm-2.fs-1.fsx.us-east-2.amazonaws.com'}})).toBe(true);
    expect(shown({})).toBe(true);
});
