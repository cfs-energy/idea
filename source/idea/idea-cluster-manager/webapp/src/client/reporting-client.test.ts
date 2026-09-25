import {beforeEach, describe, expect, it, vi} from 'vitest';
import ReportingClient from './reporting-client';
import {ReportingCsv, ReportingExportRequest, ReportingRowsRequest, ReportingSummaryRequest} from './reporting-model';

import {initTestAppData} from '../test-support';

beforeEach(() => initTestAppData());

describe('Reporting RPC', () => {
    const create = () => new ReportingClient({name: 'reporting', baseUrl: 'https://example.org', apiContextPath: '/api'});
    it.each([
        ['getCapabilities', 'Reporting.GetCapabilities', {}],
        ['getSummary', 'Reporting.GetSummary', {period: 'custom', start_date: '2020-01-01', end_date: '2020-01-31'} satisfies ReportingSummaryRequest],
        ['listRows', 'Reporting.ListRows', {snapshot_id: 'snapshot', table: 'facet', sort_by: 'jobs', descending: false, paginator: {page_size: 50, cursor: 'opaque+/='}} satisfies ReportingRowsRequest],
        ['exportCsv', 'Reporting.ExportCsv', {snapshot_id: 'snapshot', table: 'project', sort_by: 'spend_total', descending: true, columns: ['label', 'jobs']} satisfies ReportingExportRequest]
    ] as const)('sends the exact %s envelope', async (method, namespace, payload) => {
        const client = create();
        const invoke = vi.spyOn(client.apiInvoker, 'invoke').mockResolvedValue({success: true, payload: {}});
        await (client[method] as (request: unknown) => Promise<unknown>)(payload);
        expect(invoke).toHaveBeenCalledWith({header: {namespace, request_id: expect.any(String)}, payload}, false);
    });
    it('returns server CSV bytes, filename, type and all-pages row count unchanged', async () => {
        const client = create();
        const csv: ReportingCsv = {filename: 'reporting.csv', content_type: 'text/csv;charset=utf-8', content: 'label,spend_total\r\n"Recorded, label",0.00\r\n', row_count: 300, as_of: '2020-01-31T00:00:00Z'};
        vi.spyOn(client.apiInvoker, 'invoke').mockResolvedValue({success: true, payload: csv});
        expect(await client.exportCsv({snapshot_id: 'snapshot', table: 'user', sort_by: 'label', descending: false, columns: ['label', 'spend_total']})).toBe(csv);
    });
    it('preserves server error codes and narrower-period guidance', async () => {
        const client = create();
        vi.spyOn(client.apiInvoker, 'invoke').mockResolvedValue({success: false, error_code: 'REPORT_TOO_LARGE', message: 'Select a narrower period.'});
        await expect(client.getSummary({period: 'this_month'})).rejects.toMatchObject({errorCode: 'REPORT_TOO_LARGE', message: 'Select a narrower period.'});
    });
});
