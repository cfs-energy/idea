import {expect, it} from 'vitest';
import Utils from './utils';

it('shows a field on a numeric condition over another field', () => {
    const registry: any = {list: () => [{getParamName: () => 'cpus', getParamMeta: () => ({name: 'cpus', data_type: 'int'})}]};
    expect(Utils.canShowFormField(registry, {cpus: 8}, {param: 'cpus', gt: 4})).toBe(true);
    expect(Utils.canShowFormField(registry, {cpus: 2}, {param: 'cpus', gt: 4})).toBe(false);
    expect(Utils.canShowFormField(registry, {cpus: 4}, {param: 'cpus', lte: 4})).toBe(true);
});
