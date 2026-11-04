import {readFileSync} from 'node:fs';
import {afterEach, expect, it, vi} from 'vitest';
import {Ghostty, Terminal} from 'ghostty-web';
import {TerminalConnection} from './connection';

afterEach(() => {vi.unstubAllGlobals(); vi.restoreAllMocks();});

it('ACKs real ghostty parsing with paints suspended and preserves UTF-8 across frames', async () => {
    const wasm = readFileSync('node_modules/ghostty-web/ghostty-vt.wasm');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok: true, arrayBuffer: async () => wasm.buffer.slice(wasm.byteOffset, wasm.byteOffset + wasm.byteLength)}));
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
    // jsdom has no canvas. Only drawing is stubbed; Terminal and its WASM parser are real.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
        measureText: () => ({width: 8}), scale() {}, fillRect() {}, fillText() {},
    } as any);
    const term = new Terminal({ghostty: await Ghostty.load('test.wasm')});
    term.open(document.createElement('div'));
    const socket = {readyState: WebSocket.OPEN, bufferedAmount: 0, send: vi.fn(), close: vi.fn(), onmessage: null as any};
    const connection = new TerminalConnection({
        url: 'wss://portal.example/terminal', target: 'bastion',
        ticket: async () => ({ticket: 'one-use', username: 'alice', expires_in: 30}),
        state() {}, socket: () => socket as unknown as WebSocket,
        output: (data, done?: () => void) => term.write(data, done),
    });
    try {
        const painted = vi.fn();
        term.write('\r', painted);
        expect(painted).not.toHaveBeenCalled();
        connection.start(); await Promise.resolve();
        socket.onmessage({data: '{"type":"ready"}'});
        const bytes = new TextEncoder().encode('日😀');
        for (const byte of Array.from(bytes)) socket.onmessage({data: new Uint8Array([byte]).buffer});
        expect(term.buffer.active.getLine(0)?.translateToString(true)).toBe('日😀');
        expect(socket.send).toHaveBeenCalledTimes(bytes.length);
        for (const [frame] of socket.send.mock.calls) expect(frame).toBe('{"type":"ack"}');
        expect(painted).not.toHaveBeenCalled();
    } finally {connection.stop(); term.dispose();}
});

it('fits the real renderer and wire dimensions within the server limit on an ultrawide', async () => {
    const {FitAddon} = await import('./renderer');
    const wasm = readFileSync('node_modules/ghostty-web/ghostty-vt.wasm');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok: true, arrayBuffer: async () => wasm.buffer.slice(wasm.byteOffset, wasm.byteOffset + wasm.byteLength)}));
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
        measureText: () => ({width: 6}), scale() {}, fillRect() {}, fillText() {},
    } as any);
    const term = new Terminal({ghostty: await Ghostty.load('test.wasm'), fontSize: 10});
    const fitter = new FitAddon();
    term.loadAddon(fitter);
    const surface = document.createElement('div');
    let width = 3400, height = 8000;
    Object.defineProperties(surface, {clientWidth: {get: () => width}, clientHeight: {get: () => height}});
    term.open(surface);
    const socket = {readyState: WebSocket.OPEN, bufferedAmount: 0, send: vi.fn(), close: vi.fn(), onmessage: null as any};
    const connection = new TerminalConnection({
        url: 'wss://portal.example/terminal', target: 'bastion',
        ticket: async () => ({ticket: 'one-use', username: 'alice', expires_in: 30}),
        state(status) {if (status === 'connected') connection.send({type: 'resize', cols: term.cols, rows: term.rows});},
        socket: () => socket as unknown as WebSocket, output() {},
    });
    term.onResize(({cols, rows}) => connection.send({type: 'resize', cols, rows}));
    try {
        fitter.fit();
        expect(term.cols).toBe(500); expect(term.rows).toBe(500);
        connection.start(); await Promise.resolve();
        socket.onmessage({data: '{"type":"ready"}'});
        expect(JSON.parse(socket.send.mock.lastCall![0])).toEqual({type: 'resize', cols: 500, rows: 500});
        await new Promise(resolve => setTimeout(resolve, 60));
        width = 1200; height = 600;
        fitter.fit();
        expect(term.cols).toBeLessThan(500); expect(term.rows).toBeLessThan(500);
        expect(JSON.parse(socket.send.mock.lastCall![0])).toEqual({type: 'resize', cols: term.cols, rows: term.rows});
    } finally {connection.stop(); term.dispose();}
});
