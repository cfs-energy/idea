import React, {useState} from 'react';
import {act, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {vi} from 'vitest';
import {initTestAppContext} from '../../test-support';
import {FetchPricingRatesResult, SettingDefinition} from '../../client/data-model';
import CostEstimationSettings from './cost-estimation-settings';

const rates = {ebs_gp3_storage: 0.08, ebs_io1_storage: 0.125, provisioned_iops: 0.065, fsx_lustre: 0.14 / 730};
const defaults = {...rates, default_fsx_lustre_size: 1200, ec2_boot_penalty_seconds: 300};
const settings: SettingDefinition[] = Object.keys(defaults).map(name => ({key: `scheduler.cost_estimation.${name}`, module: 'scheduler', path: `cost_estimation.${name}`, group: 'costs', section: 'Cost estimation', label: ({ebs_gp3_storage: 'EBS gp3 storage (USD/GB-month)', ebs_io1_storage: 'EBS io1 storage (USD/GB-month)', provisioned_iops: 'io1 provisioned IOPS (USD/IOPS-month)', fsx_lustre: 'FSx for Lustre (USD/GB-hour)', default_fsx_lustre_size: 'Default Lustre capacity (GB)', ec2_boot_penalty_seconds: 'EC2 boot penalty (seconds)'} as Record<string, string>)[name], description: '', value_type: name in rates ? 'number' : 'integer', effect: 'runtime', advanced: false, validation: {minimum: 0}}));
const response: FetchPricingRatesResult = {region: 'us-west-2', as_of: '2026-09-25T12:00:00Z', rates, unavailable: {}, assumptions: ['SCRATCH_2 / SSD; monthly price / 730']};
const deferred = () => {
    let resolve!: (value: FetchPricingRatesResult) => void;
    const promise = new Promise<FetchPricingRatesResult>(done => {resolve = done;});
    return {promise, resolve};
};
function setup(admin = true) {
    const context = initTestAppContext();
    vi.spyOn(context.auth(), 'isModuleAdmin').mockReturnValue(admin);
    vi.spyOn(context.getClusterSettingsService(), 'isSchedulerDeployed').mockReturnValue(true);
    const client = context.client().clusterSettings();
    vi.spyOn(client, 'fetchPricingRates').mockResolvedValue(response);
    vi.spyOn(client, 'updateModuleSettings').mockResolvedValue({success: true});
    return client;
}
function Card() {
    const [editing, setEditing] = useState(false);
    const [values, setValues] = useState(defaults);
    return <CostEstimationSettings title="Cost estimation" settings={settings} values={{scheduler: {cost_estimation: values}, cluster: {aws: {region: 'us-west-2', pricing_region: 'us-east-1'}}}}
        moduleId={module => module} editing={editing} editDisabled={false} onEdit={() => setEditing(true)} onEditingEnd={() => setEditing(false)}
        onSaved={(_, patch) => setValues(current => ({...current, ...Object.fromEntries(Object.entries(patch).map(([path, value]) => [path.split('.')[1], value]))}))}/>;
}
const edit = async () => userEvent.click(screen.getByRole('button', {name: 'Edit'}));
const fetchRates = async () => userEvent.click(screen.getByRole('button', {name: 'Fetch current rates'}));
const gp3 = () => screen.getByRole('spinbutton', {name: 'EBS gp3 storage (USD/GB-month)'});

it('fetches only in Edit and saves fractional rates without persisting the draft region', async () => {
    const client = setup(); render(<Card/>);
    expect(screen.queryByRole('button', {name: 'Fetch current rates'})).toBeNull();
    expect(screen.queryByRole('button', {name: 'Advanced'})).toBeNull();
    await edit();
    expect(screen.getByRole('textbox', {name: 'Fetch region'})).toHaveValue('us-west-2');
    await fetchRates();
    expect(client.fetchPricingRates).toHaveBeenCalledWith({region: 'us-west-2'});
    expect(client.updateModuleSettings).not.toHaveBeenCalled();
    expect(screen.getByText(/Fetched 2026-09-25T12:00:00Z/)).toBeVisible();
    expect(screen.getByText('Rates shown for us-west-2')).toBeVisible();
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(client.updateModuleSettings).toHaveBeenCalledExactlyOnceWith({module_id: 'scheduler', settings: {cost_estimation: rates}});
});

it('fills successful rates and preserves manual and missing drafts after partial failure', async () => {
    const client = setup();
    vi.mocked(client.fetchPricingRates).mockResolvedValue({...response, rates: {ebs_gp3_storage: 0.09}, unavailable: {provisioned_iops: 'Product unavailable'}});
    render(<Card/>); await edit();
    const iops = screen.getByRole('spinbutton', {name: 'io1 provisioned IOPS (USD/IOPS-month)'});
    await userEvent.clear(iops); await userEvent.type(iops, '0.071'); await fetchRates();
    expect(gp3()).toHaveValue(0.09); expect(iops).toHaveValue(0.071);
    expect(screen.getByRole('alert')).toHaveTextContent('Product unavailable');
    expect(screen.getByRole('spinbutton', {name: 'EBS io1 storage (USD/GB-month)'})).toHaveValue(0.125);
});

it('reports GovCloud unreachability without changing drafts and permits retry', async () => {
    const client = setup();
    vi.mocked(client.fetchPricingRates).mockResolvedValueOnce({...response, rates: {}, unavailable: {ebs_gp3_storage: 'Pricing API is unreachable from GovCloud'}});
    render(<Card/>); await edit(); await userEvent.clear(gp3()); await userEvent.type(gp3(), '0.11'); await fetchRates();
    expect(gp3()).toHaveValue(0.11); expect(screen.getByRole('alert')).toHaveTextContent('unreachable from GovCloud');
    await fetchRates(); expect(gp3()).toHaveValue(0.08);
});

it.each(['Cancel', 'region', 'rate', 'unmount'])('discards an in-flight response after %s', async action => {
    const client = setup(); const pending = deferred(); vi.mocked(client.fetchPricingRates).mockReturnValueOnce(pending.promise);
    const mounted = render(<Card/>); await edit(); await fetchRates();
    expect(screen.getByRole('button', {name: 'Save'})).toBeDisabled();
    if (action === 'Cancel') {await userEvent.click(screen.getByRole('button', {name: 'Cancel'})); await edit();}
    if (action === 'region') {await userEvent.clear(screen.getByRole('textbox', {name: 'Fetch region'})); await userEvent.type(screen.getByRole('textbox', {name: 'Fetch region'}), 'eu-west-1');}
    if (action === 'rate') {await userEvent.clear(gp3()); await userEvent.type(gp3(), '0.11');}
    if (action === 'unmount') mounted.unmount();
    await act(async () => pending.resolve({...response, rates: {ebs_gp3_storage: 9}}));
    expect(client.updateModuleSettings).not.toHaveBeenCalled();
    expect(screen.queryByText(/Fetched /)).toBeNull();
    if (action !== 'unmount') expect(gp3()).toHaveValue(action === 'rate' ? 0.11 : 0.08);
});

it('keeps response provenance tied to its region and marks it stale after edits', async () => {
    setup(); render(<Card/>); await edit(); await fetchRates();
    await userEvent.clear(screen.getByRole('textbox', {name: 'Fetch region'})); await userEvent.type(screen.getByRole('textbox', {name: 'Fetch region'}), 'eu-west-1');
    expect(screen.getByText(/Rates shown for us-west-2.*stale/)).toBeVisible();
    expect(screen.getByText(/Fetched 2026-09-25T12:00:00Z/)).toBeVisible();
});

it('preserves drafts after Save failure and retries only when explicitly saved', async () => {
    const client = setup(); vi.mocked(client.updateModuleSettings).mockRejectedValueOnce(new Error('Save failed'));
    render(<Card/>); await edit(); await fetchRates();
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    expect(screen.getByRole('alert')).toHaveTextContent('Drafts are retained');
    expect(screen.getByRole('spinbutton', {name: 'io1 provisioned IOPS (USD/IOPS-month)'})).toHaveValue(0.065);
    await userEvent.click(screen.getByRole('button', {name: 'Save'}));
    await waitFor(() => expect(screen.getByRole('button', {name: 'Edit'})).toBeEnabled());
    expect(client.updateModuleSettings).toHaveBeenCalledTimes(2);
});

it('requires a valid region and deployed scheduler authorization', async () => {
    const client = setup(); render(<Card/>); await edit();
    await userEvent.clear(screen.getByRole('textbox', {name: 'Fetch region'})); await userEvent.type(screen.getByRole('textbox', {name: 'Fetch region'}), 'invalid');
    expect(screen.getByRole('button', {name: 'Fetch current rates'})).toBeDisabled();
    expect(client.fetchPricingRates).not.toHaveBeenCalled();
});

it('offers no editor to an unauthorized user', () => {
    const client = setup(false); render(<Card/>);
    expect(screen.queryByRole('button', {name: 'Edit'})).toBeNull();
    expect(client.fetchPricingRates).not.toHaveBeenCalled();
});

it('retains manual drafts on a network failure and retries with current region', async () => {
    const client = setup(); vi.mocked(client.fetchPricingRates).mockRejectedValueOnce(new Error('Network unavailable'));
    render(<Card/>); await edit(); await userEvent.clear(gp3()); await userEvent.type(gp3(), '0.12'); await fetchRates();
    expect(screen.getByRole('alert')).toHaveTextContent('Network unavailable');
    expect(gp3()).toHaveValue(0.12);
    expect(client.updateModuleSettings).not.toHaveBeenCalled();
    await fetchRates(); expect(gp3()).toHaveValue(0.08);
});

it('lets a newer fetch win after a region change invalidates the earlier request', async () => {
    const client = setup(); const first = deferred();
    vi.mocked(client.fetchPricingRates).mockReturnValueOnce(first.promise).mockResolvedValueOnce({...response, region: 'eu-west-1', rates: {ebs_gp3_storage: 0.1}});
    render(<Card/>); await edit(); await fetchRates();
    await userEvent.clear(screen.getByRole('textbox', {name: 'Fetch region'})); await userEvent.type(screen.getByRole('textbox', {name: 'Fetch region'}), 'eu-west-1');
    await fetchRates();
    await act(async () => first.resolve({...response, rates: {ebs_gp3_storage: 9}}));
    expect(gp3()).toHaveValue(0.1);
    expect(screen.getByText('Rates shown for eu-west-1')).toBeVisible();
});
