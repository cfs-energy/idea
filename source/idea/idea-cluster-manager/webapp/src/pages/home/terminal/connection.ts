import {TerminalTicket} from '../../../client/terminal-client';

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'reconnecting' | 'ended' | 'denied';
export const retryDelay = (attempt: number, random = Math.random) => Math.min(30000, 500 * 2 ** Math.min(attempt, 6)) * (0.8 + random() * 0.4);

export class TerminalConnection {
    private socket?: WebSocket;
    private timer?: ReturnType<typeof setTimeout>;
    private inputTimer?: ReturnType<typeof setTimeout>;
    private inputQueue: {wire: string; bytes: number}[] = [];
    private queuedBytes = 0;
    private generation = 0;
    private attempt = 0;
    private running = false;
    private ready = false;
    constructor(private options: {
        url: string; target: string;
        ticket: (target: string, client: string) => Promise<TerminalTicket>;
        state: (state: ConnectionState, reason?: string) => void;
        output: (data: Uint8Array) => void;
        inputInterrupted?: () => void;
        socket?: (url: string) => WebSocket;
    }) {}
    start() {
        this.stop(); this.running = true; this.attempt = 0;
        void this.connect();
    }
    stop() {
        this.running = this.ready = false; this.generation++;
        this.discardInput();
        clearTimeout(this.timer); this.timer = undefined;
        if (this.socket) {this.socket.onclose = null; this.socket.close(); this.socket = undefined;}
    }
    wake() {
        if (this.running && this.socket?.readyState === WebSocket.CLOSED && !this.timer) this.retry();
    }
    private async connect() {
        const generation = ++this.generation;
        this.ready = false;
        this.options.state(this.attempt ? 'reconnecting' : 'connecting');
        this.attempt++;
        const client = Array.from(crypto.getRandomValues(new Uint8Array(32)), byte => byte.toString(16).padStart(2, '0')).join('');
        let ticket: TerminalTicket;
        try {ticket = await this.options.ticket(this.options.target, client);}
        catch (error) {
            if (generation !== this.generation) return;
            const code = (error as {errorCode?: string})?.errorCode ?? '';
            if (code === 'TERMINAL_TARGET_MISCONFIGURED') this.finish('denied', 'target-misconfigured');
            else if (/UNAUTHORIZED|AUTH_|FORBIDDEN|INVALID_PARAM/.test(code)) this.finish('denied', 'access-denied');
            else this.retry(code === 'TERMINAL_UNAVAILABLE' ? 'unavailable' : undefined);
            return;
        }
        if (!this.running || generation !== this.generation) return;
        let ws: WebSocket;
        try {ws = (this.options.socket ?? (url => new WebSocket(url)))(this.options.url);}
        catch {this.retry(); return;}
        this.socket = ws; ws.binaryType = 'arraybuffer';
        this.timer = setTimeout(() => ws.close(), 45000);
        ws.onopen = () => {
            if (generation === this.generation) ws.send(JSON.stringify({ticket: ticket.ticket, username: ticket.username, target: this.options.target, client}));
        };
        ws.onmessage = event => {
            if (generation !== this.generation) return;
            if (event.data instanceof ArrayBuffer) {
                if (!this.ready) return;
                // Receipt backpressure follows synchronous parsing, never a paint callback.
                this.options.output(new Uint8Array(event.data));
                if (generation === this.generation && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'ack'}));
            } else {
                try {
                    if (JSON.parse(event.data).type === 'ready') {
                        clearTimeout(this.timer); this.timer = undefined;
                        this.ready = true; this.attempt = 0; this.options.state('connected');
                    }
                } catch {ws.close();}
            }
        };
        ws.onclose = event => {
            if (generation !== this.generation || !this.running) return;
            this.ready = false;
            this.discardInput();
            this.generation++;
            if (event.code === 4000) this.finish('ended', event.reason);
            else if (event.code === 4003 || event.code === 1009) this.finish('denied', 'access-denied');
            else if (event.code === 4004) this.finish('denied', 'target-misconfigured');
            else if (event.code === 4005) this.finish('ended', 'ack-timeout');
            else this.retry(event.code === 4002 ? 'unavailable' : event.code === 1012 ? 'server-shutdown' : undefined);
        };
        ws.onerror = () => ws.close();
    }
    private finish(state: ConnectionState, reason?: string) {
        this.stop(); this.options.state(state, reason);
    }
    private retry(reason?: string) {
        clearTimeout(this.timer);
        if (this.attempt >= 8) {this.finish('ended', 'retry-exhausted'); return;}
        if (reason) this.options.state('reconnecting', reason);
        else this.options.state('reconnecting');
        this.timer = setTimeout(() => {
            this.timer = undefined;
            if (this.running) void this.connect();
        }, retryDelay(Math.max(0, this.attempt - 1)));
    }
    send(frame: object): boolean {
        if (!this.ready || !this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
        if (this.socket.bufferedAmount > 65536) return false;
        this.socket.send(JSON.stringify(frame)); return true;
    }
    input(data: string): boolean {
        if (!this.ready || !this.socket || this.socket.readyState !== WebSocket.OPEN || data.length > 65536) return false;
        const chars = Array.from(data), frames = [];
        const encoder = new TextEncoder();
        let bytes = 0;
        // 1024 code points fit both the 16 KiB input and 32 KiB JSON frame limits.
        for (let i = 0; i < chars.length; i += 1024) {
            const wire = JSON.stringify({type: 'input', data: chars.slice(i, i + 1024).join('')});
            const size = encoder.encode(wire).length;
            frames.push({wire, bytes: size}); bytes += size;
        }
        if (bytes + this.queuedBytes + this.socket.bufferedAmount > 65536) return false;
        this.inputQueue.push(...frames); this.queuedBytes += bytes;
        this.drainInput();
        return true;
    }
    private drainInput() {
        clearTimeout(this.inputTimer); this.inputTimer = undefined;
        if (!this.ready || this.socket?.readyState !== WebSocket.OPEN) return;
        while (this.inputQueue.length && this.socket.bufferedAmount + this.inputQueue[0].bytes <= 16384) {
            const frame = this.inputQueue[0];
            try {this.socket.send(frame.wire);}
            catch {this.discardInput(); this.socket.close(); return;}
            this.inputQueue.shift(); this.queuedBytes -= frame.bytes;
        }
        if (this.inputQueue.length) this.inputTimer = setTimeout(() => this.drainInput(), 25);
    }
    private discardInput() {
        clearTimeout(this.inputTimer); this.inputTimer = undefined;
        if (this.inputQueue.length) this.options.inputInterrupted?.();
        this.inputQueue = []; this.queuedBytes = 0;
    }
}
