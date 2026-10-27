import React, {useEffect, useRef, useState} from 'react';
import {Alert, Button, Header, Select, SpaceBetween} from '@cloudscape-design/components';
import type {Terminal, FitAddon} from 'ghostty-web';
import {colorBackgroundContainerContent, colorTextBodyDefault} from '@cloudscape-design/design-tokens';
import {AppContext} from '../../common';
import IdeaAppLayout, {IdeaAppLayoutProps} from '../../components/app-layout';
import {withRouter} from '../../navigation/navigation-utils';
import TerminalClient, {TerminalTarget} from '../../client/terminal-client';
import {ConnectionState, TerminalConnection} from './terminal/connection';
import {KeyBar, modifiedInput} from './terminal/key-bar';
import './terminal/terminal.scss';

const labels: Record<ConnectionState, string> = {
    disconnected: 'Disconnected', connecting: 'Connecting…', connected: 'Connected', reconnecting: 'Reconnecting…',
    ended: 'Connection ended. Select Connect to try again.', denied: 'Terminal access was denied.'
};
const reasonLabels: Record<string, string> = {
    'idle-timeout': 'Closed after the input-idle limit. Select Connect to return.',
    'max-duration': 'Closed after the maximum connection duration. Select Connect to return.',
    'shell-exited': 'Your shell exited. Connect to start a new one.',
    'access-denied': 'Terminal access was denied. Check that Terminal and your account are enabled and that you own the target.',
    'unavailable': 'The target is temporarily unavailable or the terminal limit has been reached. Close any extra terminals. Retrying…',
    'trust-not-ready': 'The target is refreshing terminal access. Retrying…',
    'target-stopped': 'This desktop is stopped. Start it from My desktops, then select Connect.',
    'target-misconfigured': 'The target needs administrator attention. Ask your administrator to check terminal SSH trust and infrastructure configuration.',
    'ack-timeout': 'Terminal stopped accepting output. Select Connect to try again.',
    'retry-exhausted': 'Unable to connect after 8 attempts. Check the target and your network, then select Connect to try again.',
    'server-shutdown': 'The terminal service is restarting. Reconnecting…',
};
export const terminalEnabled = () => (window as any).idea?.app?.web_terminal_enabled !== false;

export function TerminalPanel({client}: {client: TerminalClient}) {
    const [targets, setTargets] = useState<TerminalTarget[]>([]);
    const [target, setTarget] = useState('');
    const [targetsLoaded, setTargetsLoaded] = useState(false);
    const [state, setState] = useState<ConnectionState>('disconnected');
    const [reason, setReason] = useState('');
    const [error, setError] = useState('');
    const [ready, setReady] = useState(false);
    const [fontSize, setFontSize] = useState(16);
    const [ctrl, setCtrl] = useState(false);
    const [alt, setAlt] = useState(false);
    const modifiers = useRef({ctrl, alt}); modifiers.current = {ctrl, alt};
    const surface = useRef<HTMLDivElement>(null);
    const panel = useRef<HTMLElement>(null);
    const terminal = useRef<Terminal | null>(null);
    const fit = useRef<FitAddon | null>(null);
    const connection = useRef<TerminalConnection | null>(null);
    const send = (data: string) => {
        const value = modifiedInput(data, modifiers.current.ctrl, modifiers.current.alt);
        setCtrl(false); setAlt(false);
        if (!connection.current?.input(value)) setError('Input was not queued. Wait for the connection or paste less text, then try again.');
    };
    const sendRef = useRef(send); sendRef.current = send;
    const reloadTargets = useRef(() => {});
    useEffect(() => {
        let active = true;
        let request = 0;
        reloadTargets.current = () => {
            const current = ++request;
            client.listTargets().then(result => {
                if (!active || current !== request) return;
                setTargets(result.targets); setTargetsLoaded(true);
                setTarget(selected => result.targets.some(item => item.id === selected) ? selected : result.targets[0]?.id ?? '');
            }).catch(() => {if (active && current === request) setError('Targets could not be loaded. Focus the target selector to try again.');});
        };
        reloadTargets.current();
        return () => {active = false;};
    }, [client]);
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
            term.loadAddon(fitter);
            term.open(surface.current!);
            const releaseRenderer = configureTerminal(term);
            const applyTheme = portalTheme(term);
            const input = term.onData(data => sendRef.current(data));
            const resize = term.onResize(({cols, rows}) => connection.current?.send({type: 'resize', cols, rows}));
            // Ghostty returns true for a handled key, the inverse of xterm's convention.
            term.attachCustomKeyEventHandler(event => {
                if (event.key === 'Escape' && event.shiftKey) {panel.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus(); return true;}
                return false;
            });
            const theme = () => {
                if (!surface.current) return;
                surface.current.style.backgroundColor = colorBackgroundContainerContent;
                surface.current.style.color = colorTextBodyDefault;
                const style = getComputedStyle(surface.current);
                applyTheme({background: style.backgroundColor, foreground: style.color, cursor: style.color});
            };
            const resizePanel = () => {
                if (!panel.current) return;
                const viewport = window.visualViewport;
                const mobile = window.innerWidth <= 600;
                const bottom = (viewport?.height ?? window.innerHeight) + (viewport?.offsetTop ?? 0);
                panel.current.style.top = mobile ? `${viewport?.offsetTop ?? 0}px` : '';
                panel.current.style.height = `${mobile ? (viewport?.height ?? window.innerHeight) : Math.max(300, bottom - panel.current.getBoundingClientRect().top - 16)}px`;
                requestAnimationFrame(() => {if (terminal.current === term) fitter.fit();});
            };
            const observer = new ResizeObserver(() => fitter.fit()); observer.observe(surface.current!);
            const themes = new MutationObserver(theme); themes.observe(document.documentElement, {attributes: true, attributeFilter: ['class']}); themes.observe(document.body, {attributes: true, attributeFilter: ['class']});
            window.visualViewport?.addEventListener('resize', resizePanel); window.visualViewport?.addEventListener('scroll', resizePanel);
            window.addEventListener('resize', resizePanel);
            const wake = () => {if (document.visibilityState === 'visible') {resizePanel(); connection.current?.wake();}};
            window.addEventListener('online', wake); document.addEventListener('visibilitychange', wake);
            cleanup = () => {
                connection.current?.stop(); observer.disconnect(); themes.disconnect(); input.dispose(); resize.dispose();
                window.visualViewport?.removeEventListener('resize', resizePanel); window.visualViewport?.removeEventListener('scroll', resizePanel);
                window.removeEventListener('resize', resizePanel); window.removeEventListener('online', wake); document.removeEventListener('visibilitychange', wake);
                releaseRenderer(); fit.current = null; terminal.current = null; term.dispose();
            };
            theme(); resizePanel(); setReady(true);
        };
        void initialize().catch(() => {
            cleanup(); cleanup = () => {}; fit.current = null; terminal.current = null;
            if (active) setError('Terminal could not be loaded. Reload the page to try again.');
        });
        return () => {active = false; cleanup();};
    }, []);
    useEffect(() => {if (terminal.current) {terminal.current.options.fontSize = fontSize; fit.current?.fit();}}, [fontSize]);
    useEffect(() => {
        connection.current?.stop(); terminal.current?.reset();
        setState('disconnected'); setReason('');
    }, [target]);
    const connect = () => {
        connection.current?.stop(); setError('');
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
    };
    const active = ['connected', 'connecting', 'reconnecting'].includes(state);
    const options = targets.map(item => {
        const id = item.id.replace(/^desktop:/, '').slice(0, 8);
        const duplicate = targets.some(other => other.id !== item.id && other.label === item.label);
        const name = item.label && item.label !== 'My desktop' ? `${item.label}${duplicate ? ` (${id})` : ''}` : id;
        return {label: item.id === 'bastion' ? item.label : `My desktop: ${name}`, value: item.id};
    });
    return <section ref={panel} className="terminal-panel" aria-label="Browser terminal">
        <div className="terminal-mobile-heading"><strong>Terminal</strong><a href="#/home">Back to portal</a></div>
        <div className="terminal-controls">
            <Select ariaLabel="Terminal target" selectedOption={options.find(item => item.value === target) ?? null}
                options={options} onFocus={() => reloadTargets.current()}
                disabled={active} placeholder="Choose a target" onChange={({detail}) => setTarget(detail.selectedOption.value!)} />
            <Button disabled={!ready || !target} onClick={() => {if (active) {connection.current?.stop(); setState('disconnected'); setReason('');} else connect();}}>{active ? 'Disconnect' : 'Connect'}</Button>
            <Button ariaLabel="Smaller terminal text" disabled={!ready || fontSize <= 10} onClick={() => setFontSize(size => size - 1)}>A−</Button>
            <Button ariaLabel="Larger terminal text" disabled={!ready || fontSize >= 28} onClick={() => setFontSize(size => size + 1)}>A+</Button>
            <Button disabled={!ready} onClick={() => {
                const selection = terminal.current?.getSelection();
                if (!selection) {setError('Select text first.'); return;}
                void navigator.clipboard.writeText(selection).catch(() => setError('Copy was blocked. Use the browser copy action.'));
            }}>Copy</Button>
            <Button disabled={state !== 'connected'} onClick={() => {void navigator.clipboard.readText().then(text => terminal.current?.paste(text)).catch(() => setError('Paste was blocked. Use your keyboard or the browser paste action.'));}}>Paste</Button>
        </div>
        <div role="status" aria-live="polite" className="terminal-status">{ready ? targetsLoaded && !targets.length ? 'No bastion or eligible Linux desktop is available. Linux desktops launched before this cluster ran 26.11.1 are not listed; launch a new one to use it here.' : reasonLabels[reason] ?? labels[state] : 'Loading terminal…'}</div>
        {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
        <div className="terminal-surface" ref={surface} aria-label="Shell. Shift Escape moves focus to the controls." />
        <KeyBar ctrl={ctrl} alt={alt} disabled={state !== 'connected'} modifier={key => {if (key === 'ctrl') setCtrl(value => !value); else setAlt(value => !value); terminal.current?.focus();}} send={data => {send(data); terminal.current?.focus();}} />
    </section>;
}

function TerminalPage(props: IdeaAppLayoutProps) {
    return <IdeaAppLayout {...props} content={<SpaceBetween size="s">
        <Header variant="h1" description="Your shell stays in tmux when you disconnect. Use Shift Escape to return to the controls.">Terminal</Header>
        {terminalEnabled() ? <TerminalPanel client={AppContext.get().client().terminal()} /> : <Alert>Terminal is disabled on this cluster.</Alert>}
    </SpaceBetween>} />;
}
export default withRouter(TerminalPage);
