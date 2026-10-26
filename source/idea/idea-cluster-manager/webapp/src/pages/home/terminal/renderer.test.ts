import {afterEach, expect, it, vi} from 'vitest';
import {LinkDetector, UrlRegexProvider} from 'ghostty-web';
import type {Terminal} from 'ghostty-web';
import {configureTerminal, portalTheme} from './renderer';

afterEach(() => vi.restoreAllMocks());

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
