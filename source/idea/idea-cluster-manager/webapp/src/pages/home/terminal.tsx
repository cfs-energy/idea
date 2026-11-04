import React, {useEffect, useRef, useState} from 'react';
import {Alert, Button} from '@cloudscape-design/components';
import type {Terminal, FitAddon} from 'ghostty-web';
import {colorBackgroundContainerContent, colorTextBodyDefault} from '@cloudscape-design/design-tokens';
import {Navigate, useSearchParams} from 'react-router-dom';
import TerminalClient from '../../client/terminal-client';
import {ConnectionState, TerminalConnection} from './terminal/connection';
import {KeyBar, modifiedInput} from './terminal/key-bar';
import {shellStore, terminalEnabled, useShell} from './terminal/shell-store';
import {touchGestures, trackCaret, useTouchKeyBar, visibleText} from './terminal/touch';
import './terminal/terminal.scss';

export {terminalEnabled};

const labels: Record<ConnectionState, string> = {
    disconnected: 'Disconnected', connecting: 'Connecting…', connected: 'Connected', reconnecting: 'Reconnecting…',
    ended: 'Connection ended. Close this tab and open it again.', denied: 'Terminal access was denied.'
};
const reasonLabels: Record<string, string> = {
    'idle-timeout': 'Closed after the input-idle limit. Close this tab and open it again to return.',
    'max-duration': 'Closed after the maximum connection duration. Close this tab and open it again to return.',
    'shell-exited': 'Your shell exited. Close this tab and open it again for a new one.',
    'access-denied': 'Terminal access was denied. Check that Terminal and your account are enabled and that you own the target.',
    'unavailable': 'The target is temporarily unavailable or the terminal limit has been reached. Close any extra terminals. Retrying…',
    'trust-not-ready': 'The target is refreshing terminal access. Retrying…',
    'target-stopped': 'This desktop is stopped. Start it from My desktops, then close this tab and open it again.',
    'target-error': 'This desktop is in an error or deleting state. Check it in My desktops.',
    'target-misconfigured': 'The target needs administrator attention. Ask your administrator to check terminal SSH trust and infrastructure configuration.',
    'ack-timeout': 'Terminal stopped accepting output. Close this tab and open it again.',
    'retry-exhausted': 'Unable to connect after 8 attempts. Check the target and your network, then close this tab and open it again.',
    'server-shutdown': 'The terminal service is restarting. Reconnecting…',
};

const MIN_FONT = 10, MAX_FONT = 28;
const clampFont = (size: number) => Math.min(MAX_FONT, Math.max(MIN_FONT, Math.round(size)));
const stored = (key: string) => {try {return Number(localStorage.getItem(key)) || undefined;} catch {return undefined;}};
const store = (key: string, value: number) => {try {localStorage.setItem(key, String(value));} catch { /* Private mode keeps the default. */ }};
export const FONT_KEY = 'idea.terminal.font-size';
export const HEIGHT_KEY = 'idea.terminal.height';

export const isMac = () => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
/** Copy is Cmd+C on macOS and Ctrl+Shift+C elsewhere, so Ctrl+C always reaches the shell as an interrupt. */
export const isCopyKey = (event: KeyboardEvent, mac = isMac()) =>
    event.code === 'KeyC' && (mac ? event.metaKey && !event.ctrlKey && !event.altKey : event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey);

export function TerminalView({client, target, visible = true}: {client: TerminalClient; target: string; visible?: boolean}) {
    const [state, setState] = useState<ConnectionState>('disconnected');
    const [reason, setReason] = useState('');
    const [error, setError] = useState('');
    const [ready, setReady] = useState(false);
    const [fontSize, setFontSize] = useState(() => clampFont(stored(FONT_KEY) ?? 16));
    const [ctrl, setCtrl] = useState(false);
    const [selecting, setSelecting] = useState<{text: string; x: number; y: number} | null>(null);
    const keyBar = useTouchKeyBar();
    const modifiers = useRef(ctrl); modifiers.current = ctrl;
    const surface = useRef<HTMLDivElement>(null);
    const panel = useRef<HTMLElement>(null);
    const terminal = useRef<Terminal | null>(null);
    const fit = useRef<FitAddon | null>(null);
    const connection = useRef<TerminalConnection | null>(null);
    const pinchBase = useRef<number | null>(null);
    const send = (data: string) => {
        const value = modifiedInput(data, modifiers.current, false);
        setCtrl(false);
        if (!connection.current?.input(value)) setError('Input was not queued. Wait for the connection or paste less text, then try again.');
    };
    const sendRef = useRef(send); sendRef.current = send;
    useEffect(() => {
        let active = true;
        let cleanup = () => {};
        const initialize = async () => {
            const {createTerminal, FitAddon, configureTerminal, portalTheme} = await import('./terminal/renderer');
            if (!active) return;
            const term = await createTerminal();
            if (!active) {term.dispose(); return;}
            cleanup = () => term.dispose();
            const fitter = new FitAddon(); fit.current = fitter; terminal.current = term;
            term.options.fontSize = clampFont(stored(FONT_KEY) ?? 16);
            term.loadAddon(fitter);
            term.open(surface.current!);
            const releaseRenderer = configureTerminal(term);
            const applyTheme = portalTheme(term);
            const input = term.onData(data => sendRef.current(data));
            const resize = term.onResize(({cols, rows}) => connection.current?.send({type: 'resize', cols, rows}));
            // Ghostty returns true for a handled key, the inverse of xterm's convention. Unhandled
            // Ctrl+C is encoded as an interrupt; Cmd+V and Ctrl+Shift+V fall through to the browser paste.
            term.attachCustomKeyEventHandler(event => {
                if (event.key === 'Escape' && event.shiftKey) {panel.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus(); return true;}
                if (isCopyKey(event)) {
                    if (term.hasSelection()) void navigator.clipboard?.writeText(term.getSelection()).catch(() => setError('Copy was blocked. Use the browser copy action.'));
                    return true;
                }
                return false;
            });
            const releaseCaret = term.textarea ? trackCaret(term.textarea, data => sendRef.current(data), () => term.getMode(1)) : () => {};
            const releaseGestures = touchGestures(surface.current!, {
                zoom: (scale, done) => {
                    pinchBase.current ??= term.options.fontSize;
                    const size = clampFont(pinchBase.current * scale);
                    setFontSize(size);
                    if (done) {pinchBase.current = null; store(FONT_KEY, size);}
                },
                longPress: (x, y) => setSelecting({text: visibleText(term), x, y}),
            });
            const theme = () => {
                if (!surface.current) return;
                surface.current.style.backgroundColor = colorBackgroundContainerContent;
                surface.current.style.color = colorTextBodyDefault;
                const style = getComputedStyle(surface.current);
                applyTheme({background: style.backgroundColor, foreground: style.color, cursor: style.color});
            };
            const observer = new ResizeObserver(() => fitter.fit()); observer.observe(surface.current!);
            const themes = new MutationObserver(theme); themes.observe(document.documentElement, {attributes: true, attributeFilter: ['class']}); themes.observe(document.body, {attributes: true, attributeFilter: ['class']});
            const wake = () => {if (document.visibilityState === 'visible') connection.current?.wake();};
            window.addEventListener('online', wake); document.addEventListener('visibilitychange', wake);
            cleanup = () => {
                connection.current?.stop(); observer.disconnect(); themes.disconnect(); input.dispose(); resize.dispose();
                window.removeEventListener('online', wake); document.removeEventListener('visibilitychange', wake);
                releaseCaret(); releaseGestures(); releaseRenderer(); fit.current = null; terminal.current = null; term.dispose();
            };
            theme(); setReady(true);
        };
        void initialize().catch(() => {
            cleanup(); cleanup = () => {}; fit.current = null; terminal.current = null;
            if (active) setError('Terminal could not be loaded. Reload the page to try again.');
        });
        return () => {active = false; cleanup();};
    }, []);
    useEffect(() => {if (terminal.current) {terminal.current.options.fontSize = fontSize; fit.current?.fit();}}, [fontSize]);
    useEffect(() => {if (visible && terminal.current) {fit.current?.fit(); terminal.current.focus();}}, [visible]);
    // Opening a tab connects and closing it disconnects; there is no manual connect control.
    useEffect(() => {
        if (!ready) return;
        const next = new TerminalConnection({
            url: client.socketUrl(), target,
            ticket: (id, binding) => client.createTicket(id, binding),
            state: (status, cause) => {
                setState(status); setReason(cause ?? '');
                if (status === 'connected' && terminal.current) {
                    next.send({type: 'resize', cols: terminal.current.cols, rows: terminal.current.rows});
                }
            },
            output: data => terminal.current?.write(data),
            inputInterrupted: () => setError('Connection interrupted before all queued input was sent. Check the shell before retrying; part of the input may have run.'),
        });
        connection.current = next; next.start(); terminal.current?.focus();
    }, [ready]); // eslint-disable-line react-hooks/exhaustive-deps
    const changeFont = (size: number) => {const next = clampFont(size); setFontSize(next); store(FONT_KEY, next);};
    return <section ref={panel} className="terminal-panel" aria-label="Browser terminal">
        <div className="terminal-controls">
            <div role="status" aria-live="polite" className="terminal-status">{ready ? reasonLabels[reason] ?? labels[state] : 'Loading terminal…'}</div>
            <Button ariaLabel="Smaller terminal text" variant="icon" iconName="zoom-out" disabled={!ready || fontSize <= MIN_FONT} onClick={() => changeFont(fontSize - 1)}/>
            <Button ariaLabel="Larger terminal text" variant="icon" iconName="zoom-in" disabled={!ready || fontSize >= MAX_FONT} onClick={() => changeFont(fontSize + 1)}/>
        </div>
        {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
        <div className="terminal-surface" ref={surface} aria-label="Shell. Shift Escape moves focus to the controls." />
        {selecting && <SelectionLayer {...selecting} fontSize={fontSize} close={() => {setSelecting(null); terminal.current?.focus();}}
            paste={text => {terminal.current?.paste(text); setSelecting(null); terminal.current?.focus();}} />}
        {keyBar && <KeyBar ctrl={ctrl} disabled={state !== 'connected'} modifier={() => {setCtrl(value => !value); terminal.current?.focus();}} send={data => {send(data); terminal.current?.focus();}} />}
    </section>;
}

/** Long-press on a phone: the screen's text as real DOM text, so the system selection handles and Copy/Paste menu work. */
export function SelectionLayer({text, x, y, fontSize, close, paste}: {text: string; x: number; y: number; fontSize: number; close: () => void; paste: (text: string) => void}) {
    const body = useRef<HTMLDivElement>(null);
    useEffect(() => {
        const element = body.current!;
        const block = (event: Event) => event.preventDefault();
        const pasted = (event: ClipboardEvent) => {event.preventDefault(); const value = event.clipboardData?.getData('text/plain'); if (value) paste(value);};
        element.addEventListener('beforeinput', block); element.addEventListener('paste', pasted);
        element.focus({preventScroll: true});
        try {
            const caret = (document as any).caretRangeFromPoint?.(x, y) as Range | undefined;
            const selection = window.getSelection() as any;
            if (caret && selection?.modify) {
                selection.removeAllRanges(); selection.addRange(caret);
                selection.modify('move', 'backward', 'word'); selection.modify('extend', 'forward', 'word');
            }
        } catch { /* The user can still select by hand. */ }
        return () => {element.removeEventListener('beforeinput', block); element.removeEventListener('paste', pasted);};
    }, []); // eslint-disable-line react-hooks/exhaustive-deps
    return <div className="terminal-select" role="dialog" aria-label="Select terminal text">
        <div className="terminal-select-bar"><span>Select text to copy, or paste into the shell.</span><button type="button" onClick={close}>Done</button></div>
        <div ref={body} className="terminal-select-text" contentEditable suppressContentEditableWarning inputMode="none" spellCheck={false}
            onKeyDown={event => {if (event.key === 'Escape') close();}} style={{fontSize}}>{text}</div>
    </div>;
}

const clampHeight = (height: number) => Math.round(Math.min(Math.max(160, height), Math.max(160, window.innerHeight - 120)));

/** CloudShell-style dock: one tab per target, kept beside the routes so it survives navigation. */
export function ShellDock({client}: {client: TerminalClient}) {
    const {tabs, active, expanded} = useShell();
    const [height, setHeight] = useState(() => clampHeight(stored(HEIGHT_KEY) ?? 320));
    const dock = useRef<HTMLElement>(null);
    const open = tabs.length > 0;
    const resize = (next: number, save: boolean) => {const value = clampHeight(next); setHeight(value); if (save) store(HEIGHT_KEY, value);};
    useEffect(() => {
        if (!open) return;
        // Phones show an expanded dock full screen, sized to the visual viewport so the prompt stays above the keyboard.
        const place = () => {
            const element = dock.current, viewport = window.visualViewport;
            if (!element) return;
            const phone = window.innerWidth <= 600 && expanded;
            element.style.top = phone ? `${viewport?.offsetTop ?? 0}px` : '';
            element.style.height = phone ? `${viewport?.height ?? window.innerHeight}px` : expanded ? `${height}px` : '';
        };
        place();
        window.visualViewport?.addEventListener('resize', place); window.visualViewport?.addEventListener('scroll', place);
        window.addEventListener('resize', place);
        return () => {
            window.visualViewport?.removeEventListener('resize', place); window.visualViewport?.removeEventListener('scroll', place);
            window.removeEventListener('resize', place);
        };
    }, [open, expanded, height]);
    useEffect(() => {
        // Keep the end of the page reachable above the dock.
        document.body.style.paddingBottom = open ? `${expanded ? height : 48}px` : '';
        return () => {document.body.style.paddingBottom = '';};
    }, [open, expanded, height]);
    if (!open) return null;
    const drag = (event: React.PointerEvent) => {
        event.preventDefault();
        const move = (moved: PointerEvent) => resize(window.innerHeight - moved.clientY, false);
        const up = (released: PointerEvent) => {
            resize(window.innerHeight - released.clientY, true);
            window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
        };
        window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
    };
    return <section ref={dock} className={`shell-dock${expanded ? '' : ' shell-dock-collapsed'}`} aria-label="Terminal">
        {expanded && <div className="shell-dock-resize" role="separator" aria-orientation="horizontal" aria-label="Resize terminal" tabIndex={0}
            aria-valuenow={height} aria-valuemin={160} aria-valuemax={Math.max(160, window.innerHeight - 120)}
            onPointerDown={drag} onKeyDown={event => {
                if (event.key === 'ArrowUp') {event.preventDefault(); resize(height + 24, true);}
                if (event.key === 'ArrowDown') {event.preventDefault(); resize(height - 24, true);}
            }} />}
        <div className="shell-dock-header">
            <div role="tablist" aria-label="Terminals" className="shell-dock-tabs">
                {tabs.map(tab => <div key={tab.target} className={`shell-dock-tab${tab.target === active ? ' shell-dock-tab-active' : ''}`}>
                    <button type="button" role="tab" id={`shell-tab-${tab.target}`} aria-selected={tab.target === active} aria-controls={`shell-panel-${tab.target}`}
                        onClick={() => shellStore.select(tab.target)}>{tab.label}</button>
                    <button type="button" className="shell-dock-close" aria-label={`Close ${tab.label}`} onClick={() => shellStore.close(tab.target)}>×</button>
                </div>)}
            </div>
            <Button variant="icon" iconName={expanded ? 'angle-down' : 'angle-up'} ariaLabel={expanded ? 'Minimize terminal' : 'Restore terminal'}
                onClick={() => shellStore.setExpanded(!expanded)} />
        </div>
        {tabs.map(tab => {
            const shown = expanded && tab.target === active;
            return <div key={tab.target} role="tabpanel" id={`shell-panel-${tab.target}`} aria-labelledby={`shell-tab-${tab.target}`} className="shell-dock-panel" style={shown ? undefined : {display: 'none'}}>
                <TerminalView client={client} target={tab.target} visible={shown} />
            </div>;
        })}
    </section>;
}

export const openBastion = () => shellStore.open('bastion', 'Bastion');

/** Old bookmarks of the Terminal page open the dock instead. */
export function TerminalRedirect({client}: {client: TerminalClient}) {
    const [params] = useSearchParams();
    useEffect(() => {
        if (!terminalEnabled()) return;
        const wanted = params.get('target');
        void shellStore.loadTargets(client).then(() => {
            const targets = shellStore.get().targets ?? [];
            const target = targets.find(item => item.id === wanted) ?? targets.find(item => item.id === 'bastion');
            if (target) shellStore.open(target.id, target.id === 'bastion' ? 'Bastion' : target.label);
        });
    }, []); // eslint-disable-line react-hooks/exhaustive-deps
    return <Navigate to={params.get('target')?.startsWith('desktop:') ? '/home/virtual-desktops' : '/'} replace/>;
}
