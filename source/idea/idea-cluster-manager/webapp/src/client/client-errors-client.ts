import IdeaBaseClient, {IdeaBaseClientProps} from './base-client';
import {ClientErrorReport} from '../common/client-error-reporter';

class ClientErrorsClient extends IdeaBaseClient<IdeaBaseClientProps> {
    /** Sends nothing when signed out, and a failure here never signs anyone out. */
    async report(report: ClientErrorReport): Promise<void> {
        if (await this.apiInvoker.isLoggedIn()) {
            await this.apiInvoker.invoke_quiet('Portal.ReportClientError', report);
        }
    }
}

export default ClientErrorsClient;
