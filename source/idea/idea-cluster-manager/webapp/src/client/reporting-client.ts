import IdeaBaseClient, {IdeaBaseClientProps} from './base-client';
import {ReportingCsv, ReportingExportRequest, ReportingRows, ReportingRowsRequest, ReportingSummary, ReportingSummaryRequest} from './reporting-model';

export default class ReportingClient extends IdeaBaseClient<IdeaBaseClientProps> {
    getCapabilities(): Promise<{can_read_reporting: boolean}> {
        return this.apiInvoker.invoke_alt('Reporting.GetCapabilities', {});
    }
    getSummary(request: ReportingSummaryRequest): Promise<ReportingSummary> {
        return this.apiInvoker.invoke_alt('Reporting.GetSummary', request);
    }
    listRows(request: ReportingRowsRequest): Promise<ReportingRows> {
        return this.apiInvoker.invoke_alt('Reporting.ListRows', request);
    }
    exportCsv(request: ReportingExportRequest): Promise<ReportingCsv> {
        return this.apiInvoker.invoke_alt('Reporting.ExportCsv', request);
    }
}
