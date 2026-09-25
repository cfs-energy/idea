import {vi} from 'vitest';
import {initTestAppContext} from '../test-support';

it('sends only the draft region to the pricing read API', async () => {
    const context = initTestAppContext();
    const client = context.client().clusterSettings();
    const invoke = vi.spyOn((client as any).apiInvoker, 'invoke_alt').mockResolvedValue({region: 'us-west-2', as_of: '2026-09-25T12:00:00Z', rates: {provisioned_iops: 0.065}, unavailable: {}, assumptions: []});
    const update = vi.spyOn(client, 'updateModuleSettings');
    const result = await client.fetchPricingRates({region: 'us-west-2'});
    expect(invoke).toHaveBeenCalledExactlyOnceWith('ClusterSettings.FetchPricingRates', {region: 'us-west-2'});
    expect(result.rates?.provisioned_iops).toBe(0.065);
    expect(update).not.toHaveBeenCalled();
});
