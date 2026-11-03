import {readFileSync} from 'node:fs';
import {afterEach, expect, it, vi} from 'vitest';
import {LinkDetector, UrlRegexProvider} from 'ghostty-web';
import type {Terminal} from 'ghostty-web';
import {configureTerminal, createTerminal, portalTheme} from './renderer';

afterEach(() => {vi.unstubAllGlobals(); vi.restoreAllMocks();});

it('never shows a closed terminal\'s output in the next one after it is sized', async () => {
    const wasm = readFileSync('node_modules/ghostty-web/ghostty-vt.wasm');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok: true, arrayBuffer: async () => wasm.buffer.slice(wasm.byteOffset, wasm.byteOffset + wasm.byteLength)}));
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
    // jsdom has no canvas. Only drawing is stubbed; both terminals and their WASM parsers are real.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({measureText: () => ({width: 8}), scale() {}, fillRect() {}, fillText() {}} as any);
    const text = (term: Terminal) => Array.from({length: term.buffer.active.length}, (_, y) => term.buffer.active.getLine(y)?.translateToString(true) ?? '').join('\n');
    const earlier = await createTerminal();
    earlier.open(document.createElement('div'));
    earlier.resize(137, 40);
    earlier.write('[alice@desktop ~]$ ls\r\nDesktop  Documents  Downloads  private-notes.txt\r\n'.repeat(60));
    expect(text(earlier)).toContain('private-notes.txt');
    earlier.dispose();
    const next = await createTerminal();
    try {
        next.open(document.createElement('div'));
        next.resize(137, 40);
        expect(text(next)).not.toMatch(/alice|Documents|private-notes/);
    } finally {next.dispose();}
});

it('keeps plain HTTP links clickable and prevents the built-in provider from opening other schemes', async () => {
    const text = 'https://example.org/path mailto:user@example.org ssh://host/path';
    const buffer = {active: {getLine: () => ({length: text.length, getCell: (col: number) => ({getCodepoint: () => text.codePointAt(col)!, getHyperlinkId: () => 0})})}};
    const detector = new LinkDetector({buffer} as any);
    detector.registerProvider(new UrlRegexProvider({buffer}));
    const surface = document.createElement('div');
    const term = {element: surface, buffer, hasSelection: () => false, registerLinkProvider: (provider: any) => detector.registerProvider(provider)} as unknown as Terminal;
    const release = configureTerminal(term);
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    (await detector.getLinkAt(3, 0))!.activate(new MouseEvent('click'));
    expect(open).toHaveBeenCalledWith('https://example.org/path', '_blank', 'noopener,noreferrer');
    open.mockClear();
    for (const protocol of ['mailto:', 'ssh://']) {
        (await detector.getLinkAt(text.indexOf(protocol), 0))!.activate(new MouseEvent('click', {ctrlKey: true}));
    }
    expect(open).not.toHaveBeenCalled();
    release(); detector.dispose();
});

it('recolors viewport and scrollback on theme changes without mutating cells or losing ANSI colors', () => {
    const normal = {fg_r: 204, fg_g: 204, fg_b: 204, bg_r: 0, bg_g: 0, bg_b: 0, codepoint: 65};
    const ansi = {...normal, fg_r: 205, fg_g: 49, fg_b: 49, bg_b: 255};
    const cells = [normal, ansi];
    let rendered: any, history: any;
    const renderer = {setTheme: vi.fn(), render: vi.fn((buffer, _force, _viewport, scrollback) => {rendered = buffer.getLine(0); history = scrollback.getScrollbackLine(0);})};
    const term = {renderer, wasmTerm: {getLine: () => cells}, getScrollbackLine: () => cells, getViewportY: () => 4} as unknown as Terminal;
    const theme = portalTheme(term);
    theme({foreground: 'rgb(10, 20, 30)', background: 'rgb(250, 251, 252)'});
    expect(rendered[0]).toEqual({...normal, fg_r: 10, fg_g: 20, fg_b: 30, bg_r: 250, bg_g: 251, bg_b: 252});
    expect(rendered[1]).toEqual(ansi); expect(history).toEqual(rendered); expect(cells[0]).toEqual(normal);
    theme({foreground: 'rgb(240, 241, 242)', background: 'rgb(10, 11, 12)'});
    expect(rendered[0]).toMatchObject({fg_r: 240, bg_r: 10});
    expect(renderer.setTheme).toHaveBeenLastCalledWith(expect.objectContaining({selectionForeground: 'rgb(10, 11, 12)'}));
});
