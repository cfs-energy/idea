import {FitAddon as GhosttyFitAddon, Ghostty, Terminal, UrlRegexProvider} from 'ghostty-web';
import type {GhosttyCell, ITheme} from 'ghostty-web';
import wasmUrl from 'ghostty-web/ghostty-vt.wasm?url';

export class FitAddon extends GhosttyFitAddon {
    override proposeDimensions() {
        const size = super.proposeDimensions();
        return size && {cols: Math.min(500, Math.max(1, size.cols)), rows: Math.min(500, Math.max(1, size.rows))};
    }
}
let engine: Promise<Ghostty> | undefined;
export async function createTerminal() {
    engine ??= Ghostty.load(wasmUrl).catch(error => {engine = undefined; throw error;});
    return new Terminal({
        ghostty: await engine,
        fontSize: 16, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        cursorBlink: true, scrollback: 5000,
        // 0.4.0 requires a complete palette when passing a scrollback limit.
        theme: {
            foreground: '#cccccc', background: '#000000', cursor: '#cccccc',
            black: '#000000', red: '#cd3131', green: '#0dbc79', yellow: '#e5e510',
            blue: '#2472c8', magenta: '#bc3fbc', cyan: '#11a8cd', white: '#e5e5e5',
            brightBlack: '#666666', brightRed: '#f14c4c', brightGreen: '#23d18b', brightYellow: '#f5f543',
            brightBlue: '#3b8eea', brightMagenta: '#d670d6', brightCyan: '#29b8db', brightWhite: '#e5e5e5',
        },
    });
}

export function configureTerminal(term: Terminal) {
    const surface = term.element!;
    const urls = new UrlRegexProvider(term);
    // Later providers replace identical ranges from the built-in provider.
    term.registerLinkProvider({provideLinks: (row, callback) => urls.provideLinks(row, links => callback(links?.map(link => ({
        ...link,
        activate: () => {
            if (term.hasSelection()) return;
            try {
                const url = new URL(link.text);
                if (['https:', 'http:'].includes(url.protocol)) window.open(url.href, '_blank', 'noopener,noreferrer');
            } catch { /* Ignore malformed terminal output. */ }
        },
    }))))});
    // Phone keyboards may emit beforeinput without a usable keydown. IME stays upstream.
    const beforeInput = (event: InputEvent) => {
        if (event.isComposing) return;
        const data = ({deleteContentBackward: '\x7f', deleteContentForward: '\x1b[3~', insertParagraph: '\r', insertLineBreak: '\r'} as Record<string, string>)[event.inputType]
            ?? (['insertText', 'insertReplacementText'].includes(event.inputType) ? event.data : null);
        if (data) {event.preventDefault(); term.input(data, true);}
    };
    surface.addEventListener('beforeinput', beforeInput);
    return () => {surface.removeEventListener('beforeinput', beforeInput); urls.dispose();};
}

// 0.4.0's options.theme setter does not update the renderer or VT default colors.
// Adapt its public render API without resetting the terminal or replaying shell output.
export function portalTheme(term: Terminal) {
    const renderer = term.renderer!;
    const render = renderer.render.bind(renderer);
    let foreground = [204, 204, 204], background = [0, 0, 0];
    const recolor = (cells: GhosttyCell[] | null) => cells?.map(cell => ({
        ...cell,
        // shortcut: 0.4.0 exposes RGB only; explicit #cccccc/#000000 also follow the theme.
        // Remove this mapping when upstream exposes mutable VT defaults and color flags.
        ...(cell.fg_r === 204 && cell.fg_g === 204 && cell.fg_b === 204 ? {fg_r: foreground[0], fg_g: foreground[1], fg_b: foreground[2]} : {}),
        ...(cell.bg_r === 0 && cell.bg_g === 0 && cell.bg_b === 0 ? {bg_r: background[0], bg_g: background[1], bg_b: background[2]} : {}),
    })) ?? null;
    renderer.render = (buffer, force, viewport, scrollback, opacity) => render({
        getLine: row => recolor(buffer.getLine(row)),
        getCursor: () => buffer.getCursor(), getDimensions: () => buffer.getDimensions(),
        isRowDirty: row => buffer.isRowDirty(row), needsFullRedraw: () => buffer.needsFullRedraw?.() ?? false,
        clearDirty: () => buffer.clearDirty(), getGraphemeString: (row, col) => buffer.getGraphemeString?.(row, col) ?? '',
    }, force, viewport, scrollback && {
        getScrollbackLine: row => recolor(scrollback.getScrollbackLine(row)),
        getScrollbackLength: () => scrollback.getScrollbackLength(),
    }, opacity);
    return (theme: ITheme) => {
        // Computed portal colors are rgb() values, including in both supported modes.
        foreground = theme.foreground!.match(/\d+/g)!.slice(0, 3).map(Number);
        background = theme.background!.match(/\d+/g)!.slice(0, 3).map(Number);
        renderer.setTheme({...theme, selectionBackground: theme.foreground, selectionForeground: theme.background});
        renderer.render(term.wasmTerm!, true, term.getViewportY(), term);
    };
}
