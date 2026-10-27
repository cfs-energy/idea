import {afterEach, describe, expect, it, vi} from 'vitest';
import {TerminalConnection, retryDelay} from './connection';

class Socket {
    readyState: number = WebSocket.OPEN; bufferedAmount = 0; binaryType = '';
    onopen: any; onmessage: any; onclose: any; onerror: any;
    send = vi.fn(); close = vi.fn();
}
const ticket = {ticket: 'single-use', username: 'alice', expires_in: 30};
function setup() {
    const sockets: Socket[] = [];
    const options = {url: 'wss://portal.example/cluster-manager/terminal', target: 'bastion', ticket: vi.fn().mockResolvedValue(ticket), state: vi.fn(), output: vi.fn(), inputInterrupted: vi.fn(), socket: vi.fn(() => {const s = new Socket(); sockets.push(s); return s as unknown as WebSocket;})};
    return {connection: new TerminalConnection(options), options, sockets};
}
afterEach(() => {vi.useRealTimers(); vi.restoreAllMocks();});
describe('terminal connection', () => {
    it('uses a fresh bound ticket, keeps secrets out of the URL, and waits for readiness', async () => {
        const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
        expect(options.ticket.mock.calls[0][1]).toMatch(/^[a-f0-9]{64}$/);
        expect(options.socket).toHaveBeenCalledWith(options.url);
        expect(connection.input('ls')).toBe(false);
        sockets[0].onopen();
        expect(JSON.parse(sockets[0].send.mock.calls[0][0])).toMatchObject({ticket: 'single-use', username: 'alice', target: 'bastion'});
        sockets[0].onmessage({data: '{"type":"ready"}'});
        expect(connection.input('ls\n')).toBe(true);
        expect(options.state).toHaveBeenLastCalledWith('connected'); connection.stop();
    });
    it('acknowledges receipt exactly once, independently of renderer callbacks', async () => {
        const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
        sockets[0].onmessage({data: '{"type":"ready"}'});
        sockets[0].onmessage({data: new ArrayBuffer(16)});
        expect(options.output).toHaveBeenCalledOnce();
        expect(sockets[0].send).toHaveBeenCalledExactlyOnceWith('{"type":"ack"}'); connection.stop();
    });
    it('reconnects with backoff and a new ticket after sleep or network loss', async () => {
        vi.useFakeTimers();
        const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
        sockets[0].onclose({code: 1006});
        expect(options.state).toHaveBeenLastCalledWith('reconnecting');
        await vi.advanceTimersByTimeAsync(601);
        expect(options.ticket).toHaveBeenCalledTimes(2);
        expect(options.ticket.mock.calls[1][1]).not.toBe(options.ticket.mock.calls[0][1]);
        sockets[1].onmessage({data: '{"type":"ready"}'});
        await vi.advanceTimersByTimeAsync(60000); connection.wake(); await Promise.resolve();
        expect(options.ticket).toHaveBeenCalledTimes(2);
        expect(sockets[1].close).not.toHaveBeenCalled(); connection.stop();
        expect(retryDelay(20, () => 0.5)).toBe(30000);
    });
    it.each([4000, 4003, 1009])('does not bypass a server termination (%s)', async code => {
        vi.useFakeTimers(); const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
        sockets[0].onclose({code}); await vi.advanceTimersByTimeAsync(60000); connection.wake();
        expect(options.ticket).toHaveBeenCalledTimes(1); connection.stop();
    });
    it('cancels pending tickets and does not replay typed input after disconnect', async () => {
        const {connection, options, sockets} = setup(); connection.start(); connection.stop(); await Promise.resolve();
        expect(options.socket).not.toHaveBeenCalled(); expect(connection.input('dangerous command')).toBe(false);
        connection.start(); await Promise.resolve(); sockets[0].onmessage({data: '{"type":"ready"}'});
        sockets[0].bufferedAmount = 65537;
        expect(connection.input('ls')).toBe(false); expect(sockets[0].close).not.toHaveBeenCalled(); connection.stop();
    });
});

it('retries transient ticket failures even when the network interface still reports online', async () => {
    vi.useFakeTimers();
    const {connection, options} = setup();
    options.ticket.mockRejectedValueOnce(new TypeError('fetch failed'));
    connection.start(); await Promise.resolve();
    expect(options.state).toHaveBeenLastCalledWith('reconnecting');
    await vi.advanceTimersByTimeAsync(601);
    expect(options.ticket).toHaveBeenCalledTimes(2);
    connection.stop();
});

it.each([
    [4006, 'target-stopped', 'ended'], [4006, 'target-error', 'ended'],
    [4000, 'idle-timeout', 'ended'], [4000, 'max-duration', 'ended'], [4000, 'shell-exited', 'ended'],
    [1009, 'access-denied', 'denied'], [4003, 'access-denied', 'denied'], [4004, 'target-misconfigured', 'denied'], [4005, 'ack-timeout', 'ended'],
])('stops permanently for %s %s and retains the reason', async (code, reason, state) => {
    vi.useFakeTimers();
    const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
    sockets[0].onclose({code, reason});
    expect(options.state).toHaveBeenLastCalledWith(state, reason);
    await vi.advanceTimersByTimeAsync(300000); connection.wake();
    expect(options.ticket).toHaveBeenCalledTimes(1); connection.stop();
});

it.each([1000, 1006, 1011, 1012, 4001, 4002])('retries transient close %s with a fresh ticket', async code => {
    vi.useFakeTimers();
    const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
    sockets[0].onclose({code, reason: code === 4002 ? 'unavailable' : ''});
    await vi.advanceTimersByTimeAsync(601);
    expect(options.ticket).toHaveBeenCalledTimes(2); connection.stop();
});

it.each([
    ['UNAUTHORIZED_ACCESS', 'access-denied'], ['AUTH_INVALID_TOKEN', 'access-denied'],
    ['TERMINAL_TARGET_MISCONFIGURED', 'target-misconfigured'],
    ['TERMINAL_TARGET_STOPPED', 'target-stopped'],
    ['TERMINAL_TARGET_ERROR', 'target-error'],
])('stops on permanent API error %s', async (errorCode, reason) => {
    vi.useFakeTimers(); const {connection, options} = setup();
    options.ticket.mockRejectedValue({errorCode}); connection.start(); await Promise.resolve();
    expect(options.state).toHaveBeenLastCalledWith(reason.startsWith('target-') && reason !== 'target-misconfigured' ? 'ended' : 'denied', reason);
    await vi.advanceTimersByTimeAsync(300000); connection.wake();
    expect(options.ticket).toHaveBeenCalledTimes(1); connection.stop();
});

it('shares an eight-attempt budget across API and socket failures and resets only on ready or Connect', async () => {
    vi.useFakeTimers(); const {connection, options, sockets} = setup();
    options.ticket.mockRejectedValueOnce({errorCode: 'TERMINAL_UNAVAILABLE'});
    connection.start(); await Promise.resolve();
    expect(options.state).toHaveBeenLastCalledWith('reconnecting', 'unavailable');
    for (let attempt = 2; attempt <= 8; attempt++) {
        await vi.advanceTimersByTimeAsync(36001);
        expect(options.ticket).toHaveBeenCalledTimes(attempt);
        sockets.at(-1)!.onclose({code: 1011});
    }
    expect(options.state).toHaveBeenLastCalledWith('ended', 'retry-exhausted');
    await vi.advanceTimersByTimeAsync(300000); connection.wake();
    expect(options.ticket).toHaveBeenCalledTimes(8);
    connection.start(); await Promise.resolve();
    expect(options.ticket).toHaveBeenCalledTimes(9);
    sockets.at(-1)!.onmessage({data: '{"type":"ready"}'});
    sockets.at(-1)!.onclose({code: 1006});
    await vi.advanceTimersByTimeAsync(601);
    expect(options.ticket).toHaveBeenCalledTimes(10); connection.stop();
});

it('cancels backoff on stop and leaves pending tickets and connecting sockets alone on wake', async () => {
    vi.useFakeTimers(); const {connection, options, sockets} = setup();
    connection.start(); connection.wake(); await Promise.resolve();
    sockets[0].readyState = WebSocket.CONNECTING;
    await vi.advanceTimersByTimeAsync(31000); connection.wake();
    expect(options.ticket).toHaveBeenCalledTimes(1);
    sockets[0].onclose({code: 1011}); connection.stop();
    await vi.advanceTimersByTimeAsync(300000); connection.wake();
    expect(options.ticket).toHaveBeenCalledTimes(1);
});


it.each(['日'.repeat(22000), '\n"\\'.repeat(21000), '😀'.repeat(32000)])('rejects an oversized encoded paste without sending a prefix', async data => {
    const {connection, sockets} = setup(); connection.start(); await Promise.resolve();
    const socket = sockets[0]; socket.onmessage({data: '{"type":"ready"}'});
    socket.send.mockImplementation(frame => {socket.bufferedAmount += new TextEncoder().encode(frame).length;});
    expect(connection.input(data)).toBe(false);
    expect(socket.send).not.toHaveBeenCalled(); expect(socket.close).not.toHaveBeenCalled(); connection.stop();
});

it('queues a whole paste and drains bounded UTF-8 frames without splitting Unicode or blocking ACKs', async () => {
    vi.useFakeTimers(); const {connection, sockets} = setup(); connection.start(); await Promise.resolve();
    const socket = sockets[0]; socket.onmessage({data: '{"type":"ready"}'});
    socket.bufferedAmount = 16000;
    socket.send.mockImplementation(frame => {socket.bufferedAmount += new TextEncoder().encode(frame).length;});
    const data = '日本語😀\n"\\'.repeat(2000);
    expect(connection.input(data)).toBe(true);
    expect(socket.send).not.toHaveBeenCalled();
    socket.onmessage({data: new ArrayBuffer(1)});
    expect(socket.send).toHaveBeenCalledWith('{"type":"ack"}');
    for (let i = 0; i < 10; i++) {socket.bufferedAmount = 0; await vi.advanceTimersByTimeAsync(25);}
    const frames = socket.send.mock.calls.map(([wire]) => {
        expect(new TextEncoder().encode(wire).length).toBeLessThanOrEqual(32768);
        return JSON.parse(wire);
    }).filter(frame => frame.type === 'input');
    expect(frames.map(frame => frame.data).join('')).toBe(data);
    for (const frame of frames) {
        expect(new TextEncoder().encode(frame.data).length).toBeLessThanOrEqual(16384);
        expect(new TextDecoder('utf-8', {fatal: true}).decode(new TextEncoder().encode(frame.data))).toBe(frame.data);
    }
    expect(socket.close).not.toHaveBeenCalled(); connection.stop();
});

it('counts existing queued bytes and reports discarded input without replaying it on reconnect', async () => {
    vi.useFakeTimers(); const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
    const socket = sockets[0]; socket.onmessage({data: '{"type":"ready"}'}); socket.bufferedAmount = 16384;
    expect(connection.input('x'.repeat(30000))).toBe(true);
    expect(connection.input('y'.repeat(30000))).toBe(false);
    expect(socket.send).not.toHaveBeenCalled();
    socket.onclose({code: 1006});
    expect(options.inputInterrupted).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(601);
    sockets[1].onmessage({data: '{"type":"ready"}'});
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets[1].send).not.toHaveBeenCalled(); connection.stop();
});

it('discards queued input on explicit disconnect and never closes a congested socket for a resize', async () => {
    vi.useFakeTimers(); const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
    sockets[0].onmessage({data: '{"type":"ready"}'}); sockets[0].bufferedAmount = 16384;
    expect(connection.input('queued')).toBe(true);
    sockets[0].bufferedAmount = 65537;
    expect(connection.send({type: 'resize', cols: 80, rows: 24})).toBe(false);
    expect(sockets[0].close).not.toHaveBeenCalled();
    connection.stop();
    expect(options.inputInterrupted).toHaveBeenCalledOnce();
    sockets[0].bufferedAmount = 0; await vi.advanceTimersByTimeAsync(1000);
    expect(sockets[0].send).not.toHaveBeenCalled();
});


it.each([true, false])('retries pending SSH trust with a bounded budget (recovers=%s)', async recovers => {
    vi.useFakeTimers();
    const {connection, options, sockets} = setup(); connection.start(); await Promise.resolve();
    for (let attempt = 1; attempt <= (recovers ? 2 : 8); attempt++) {
        sockets.at(-1)!.onclose({code: 4002, reason: 'trust-not-ready'});
        if (attempt < 8) expect(options.state).toHaveBeenLastCalledWith('reconnecting', 'trust-not-ready');
        await vi.advanceTimersByTimeAsync(36001);
    }
    if (recovers) {
        sockets.at(-1)!.onmessage({data: '{"type":"ready"}'});
        expect(options.state).toHaveBeenLastCalledWith('connected');
    } else {
        expect(options.state).toHaveBeenLastCalledWith('denied', 'target-misconfigured');
        await vi.advanceTimersByTimeAsync(300000); connection.wake();
        expect(options.ticket).toHaveBeenCalledTimes(8);
    }
    connection.stop();
});
