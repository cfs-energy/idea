import IdeaBaseClient, {IdeaBaseClientProps} from './base-client';

export interface TerminalTarget {id: string; label: string}
export interface TerminalTicket {ticket: string; username: string; expires_in: number}

export default class TerminalClient extends IdeaBaseClient<IdeaBaseClientProps> {
    listTargets(): Promise<{targets: TerminalTarget[]}> {
        return this.apiInvoker.invoke_alt('Terminal.ListTargets', {});
    }
    createTicket(target: string, client: string): Promise<TerminalTicket> {
        return this.apiInvoker.invoke_alt('Terminal.CreateTicket', {target, client});
    }
    socketUrl(): string {
        const url = new URL(this.getEndpointUrl().replace(/\/api\/v1\/?$/, '/terminal'), window.location.origin);
        url.protocol = 'wss:';
        return url.toString();
    }
}
