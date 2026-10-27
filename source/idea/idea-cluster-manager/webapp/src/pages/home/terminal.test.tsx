import React from 'react';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {TerminalPanel, terminalEnabled} from './terminal';
import {permittedViews, PORTAL_TASKS} from '../../navigation/task-navigation';
import {initTestAppContext} from '../../test-support';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';

const mocks = vi.hoisted(() => ({
    load: vi.fn(), focus: vi.fn(), dispose: vi.fn(), fit: vi.fn(), start: vi.fn(), stop: vi.fn(),
    input: vi.fn(() => true), send: vi.fn(), wake: vi.fn(), setTheme: vi.fn(), render: vi.fn(),
    reset: vi.fn(), paste: vi.fn(), write: vi.fn(), selection: vi.fn(() => 'selected output'),
    options: null as any, terminalOptions: null as any, onData: null as any, onResize: null as any, onKey: null as any,
}));
vi.mock('ghostty-web', () => ({
    Ghostty: {load: mocks.load}, FitAddon: class {fit = mocks.fit;},
    UrlRegexProvider: class {provideLinks(_row: number, callback: any) {callback(undefined);} dispose() {}},
    Terminal: class {
        options: any; cols = 80; rows = 24; element?: HTMLElement;
        renderer = {setTheme: mocks.setTheme, render: mocks.render}; wasmTerm = {};
        constructor(options: any) {this.options = options; mocks.terminalOptions = options;}
        focus = mocks.focus; dispose = mocks.dispose; reset = mocks.reset; paste = mocks.paste; write = mocks.write;
        loadAddon() {} open(element: HTMLElement) {this.element = element;} getViewportY() {return 0;}
        registerLinkProvider() {} getSelection = mocks.selection;
        input(data: string) {mocks.onData(data);}
        attachCustomKeyEventHandler(fn: any) {mocks.onKey = fn;}
        onData(fn: any) {mocks.onData = fn; return {dispose() {}};}
        onResize(fn: any) {mocks.onResize = fn; return {dispose() {}};}
    },
}));
vi.mock('./terminal/connection', () => ({TerminalConnection: class {
    constructor(options: any) {mocks.options = options;}
    start = mocks.start; stop = mocks.stop; input = mocks.input; send = mocks.send; wake = mocks.wake;
}}));

const client = {listTargets: vi.fn().mockResolvedValue({targets: [{id: 'bastion', label: 'Bastion'}, {id: 'desktop:one', label: 'Work'}]}), socketUrl: () => 'wss://portal.example/cluster-manager/terminal', createTicket: vi.fn()};
beforeEach(() => {
    vi.resetModules(); vi.clearAllMocks(); mocks.load.mockResolvedValue({});
    vi.stubGlobal('ResizeObserver', class {observe() {} disconnect() {}});
    const computed = window.getComputedStyle;
    vi.spyOn(window, 'getComputedStyle').mockImplementation(element => {
        const style = computed(element);
        if (element.classList.contains('terminal-surface')) {style.backgroundColor = 'rgb(255, 255, 255)'; style.color = 'rgb(15, 20, 25)';}
        return style;
    });
});
afterEach(() => {vi.unstubAllGlobals(); vi.restoreAllMocks();});

it('waits for WASM before connecting and preserves state, modifiers, resize, focus and clipboard', async () => {
    let loaded!: (value: object) => void;
    mocks.load.mockImplementationOnce(() => new Promise(resolve => {loaded = resolve;}));
    const {unmount} = render(<TerminalPanel client={client as any} />);
    expect(screen.getByRole('button', {name: 'Connect'})).toBeDisabled();
    await waitFor(() => expect(mocks.load).toHaveBeenCalled());
    expect(mocks.load.mock.calls[0][0]).toContain('ghostty-vt.wasm');
    await act(async () => loaded({}));
    await waitFor(() => expect(screen.getByRole('button', {name: 'Connect'})).toBeEnabled());
    fireEvent.click(screen.getByRole('button', {name: 'Connect'})); expect(mocks.start).toHaveBeenCalled(); expect(mocks.focus).toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Disconnected');
    act(() => mocks.options.state('connected'));
    expect(screen.getByRole('status')).toHaveTextContent('Connected');
    expect(mocks.send).toHaveBeenCalledWith({type: 'resize', cols: 80, rows: 24});
    act(() => mocks.onResize({cols: 100, rows: 30}));
    expect(mocks.send).toHaveBeenLastCalledWith({type: 'resize', cols: 100, rows: 30});
    act(() => mocks.onData('日本語')); expect(mocks.input).toHaveBeenCalledWith('日本語');
    fireEvent.click(screen.getByRole('button', {name: 'Ctrl'})); act(() => mocks.onData('c'));
    expect(mocks.input).toHaveBeenLastCalledWith('\x03');
    expect(screen.getByRole('button', {name: 'Ctrl'})).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByRole('button', {name: 'Alt'})); act(() => mocks.onData('x'));
    expect(mocks.input).toHaveBeenLastCalledWith('\x1bx');
    fireEvent.click(screen.getByRole('button', {name: 'Larger terminal text'}));
    expect(mocks.terminalOptions.fontSize).toBe(17); expect(mocks.fit).toHaveBeenCalled();
    expect(mocks.onKey({key: 'x'})).toBe(false);
    expect(mocks.onKey({key: 'Escape', shiftKey: true})).toBe(true);
    expect(screen.getByRole('button', {name: 'Disconnect'})).toHaveFocus();
    const clipboard = {writeText: vi.fn().mockResolvedValue(undefined), readText: vi.fn().mockResolvedValue('paste me')};
    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: clipboard});
    fireEvent.click(screen.getByRole('button', {name: 'Copy'}));
    expect(clipboard.writeText).toHaveBeenCalledWith('selected output');
    fireEvent.click(screen.getByRole('button', {name: 'Paste'}));
    await waitFor(() => expect(mocks.paste).toHaveBeenCalledWith('paste me'));
    mocks.options.output('output');
    expect(mocks.write).toHaveBeenCalledWith('output');
    act(() => mocks.options.state('reconnecting'));
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting');
    expect(screen.getByRole('button', {name: 'Paste'})).toBeDisabled();
    unmount(); expect(mocks.stop).toHaveBeenCalled(); expect(mocks.dispose).toHaveBeenCalled();
});

it('keeps the panel above the phone keyboard, bridges phone input and follows theme changes', async () => {
    const viewport = Object.assign(new EventTarget(), {height: 420, offsetTop: 12});
    vi.stubGlobal('visualViewport', viewport); vi.stubGlobal('innerWidth', 390);
    const {unmount} = render(<TerminalPanel client={client as any} />);
    await waitFor(() => expect(screen.getByRole('button', {name: 'Connect'})).toBeEnabled());
    const panel = screen.getByRole('region', {name: 'Browser terminal'});
    expect(panel.style.height).toBe('420px'); expect(panel.style.top).toBe('12px');
    viewport.height = 300; viewport.offsetTop = 20;
    act(() => viewport.dispatchEvent(new Event('resize')));
    expect(panel.style.height).toBe('300px'); expect(panel.style.top).toBe('20px');
    const surface = screen.getByLabelText('Shell. Shift Escape moves focus to the controls.');
    fireEvent.click(screen.getByRole('button', {name: 'Connect'}));
    act(() => surface.dispatchEvent(new InputEvent('beforeinput', {inputType: 'insertText', data: 'a', bubbles: true, cancelable: true})));
    expect(mocks.input).toHaveBeenLastCalledWith('a');
    const calls = mocks.input.mock.calls.length;
    act(() => surface.dispatchEvent(new InputEvent('beforeinput', {inputType: 'insertCompositionText', data: 'あ', isComposing: true, bubbles: true})));
    expect(mocks.input).toHaveBeenCalledTimes(calls);
    act(() => surface.dispatchEvent(new InputEvent('beforeinput', {inputType: 'deleteContentBackward', bubbles: true})));
    expect(mocks.input).toHaveBeenLastCalledWith('\x7f');
    await act(async () => {document.body.classList.add('awsui-dark-mode');});
    expect(mocks.setTheme).toHaveBeenCalledWith(expect.objectContaining({foreground: 'rgb(15, 20, 25)'}));
    act(() => window.dispatchEvent(new Event('online'))); expect(mocks.wake).toHaveBeenCalled();
    unmount(); document.body.classList.remove('awsui-dark-mode');
});

it('gates navigation and direct page access on the setting', () => {
    const context = initTestAppContext();
    const app = (window as any).idea.app;
    app.web_terminal_enabled = false;
    expect(terminalEnabled()).toBe(false);
    expect(permittedViews(PORTAL_TASKS.find(task => task.id === 'terminal')!, context)).toEqual([]);
    delete app.web_terminal_enabled;
    expect(terminalEnabled()).toBe(true);
});

it('reports a WASM failure without enabling a connection', async () => {
    mocks.load.mockRejectedValueOnce(new Error('WASM fetch failed'));
    render(<TerminalPanel client={client as any} />);
    await screen.findByText('Terminal could not be loaded. Reload the page to try again.');
    expect(screen.getByRole('button', {name: 'Connect'})).toBeDisabled();
    expect(mocks.start).not.toHaveBeenCalled();
});

it('disposes late WASM initialization after navigating away', async () => {
    let loaded!: (value: object) => void;
    mocks.load.mockImplementationOnce(() => new Promise(resolve => {loaded = resolve;}));
    const {unmount} = render(<TerminalPanel client={client as any} />);
    await waitFor(() => expect(mocks.load).toHaveBeenCalled());
    unmount();
    await act(async () => loaded({}));
    expect(mocks.dispose).toHaveBeenCalledOnce();
    expect(mocks.start).not.toHaveBeenCalled();
});

it('accepts output while animation frames are suspended, including after renderer cleanup', async () => {
    vi.stubGlobal('requestAnimationFrame', vi.fn());
    mocks.write.mockImplementation((_data: unknown, callback?: () => void) => {if (callback) requestAnimationFrame(callback);});
    const {unmount} = render(<TerminalPanel client={client as any} />);
    await waitFor(() => expect(screen.getByRole('button', {name: 'Connect'})).toBeEnabled());
    fireEvent.click(screen.getByRole('button', {name: 'Connect'}));
    mocks.options.output(new Uint8Array([65]));
    expect(mocks.write).toHaveBeenCalledExactlyOnceWith(new Uint8Array([65]));
    unmount();
    mocks.options.output(new Uint8Array([66]));
    expect(mocks.write).toHaveBeenCalledOnce();
});

it.each([
    ['ended', 'idle-timeout', 'Closed after the input-idle limit.'],
    ['ended', 'max-duration', 'Closed after the maximum connection duration.'],
    ['ended', 'shell-exited', 'Your shell exited. Connect to start a new one.'],
    ['ended', 'target-stopped', 'This desktop is stopped. Start it from My desktops, then select Connect.'],
    ['ended', 'target-error', 'This desktop is in an error or deleting state. Check it in My desktops.'],
    ['reconnecting', 'trust-not-ready', 'The target is refreshing terminal access.'],
    ['denied', 'target-misconfigured', 'The target needs administrator attention.'],
    ['ended', 'ack-timeout', 'Terminal stopped accepting output.'],
    ['ended', 'retry-exhausted', 'Unable to connect after 8 attempts.'],
    ['reconnecting', 'unavailable', 'The target is temporarily unavailable or the terminal limit has been reached.'],
    ['denied', 'access-denied', 'Terminal access was denied.'],
])('explains %s %s', async (state, reason, message) => {
    render(<TerminalPanel client={client as any} />);
    await waitFor(() => expect(screen.getByRole('button', {name: 'Connect'})).toBeEnabled());
    fireEvent.click(screen.getByRole('button', {name: 'Connect'}));
    act(() => mocks.options.state(state, reason));
    expect(screen.getByRole('status')).toHaveTextContent(message);
});


it('selects an available desktop when the cluster has no bastion and disables Connect for an empty list', async () => {
    const desktops = {...client, listTargets: vi.fn().mockResolvedValue({targets: [{id: 'desktop:one', label: 'Work'}]})};
    const {unmount} = render(<TerminalPanel client={desktops as any} />);
    await waitFor(() => expect(screen.getByRole('button', {name: 'Connect'})).toBeEnabled());
    fireEvent.click(screen.getByRole('button', {name: 'Connect'}));
    expect(mocks.options.target).toBe('desktop:one');
    unmount();
    render(<TerminalPanel client={{...client, listTargets: vi.fn().mockResolvedValue({targets: []})} as any} />);
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('No bastion or eligible Linux desktop is available'));
    expect(screen.queryByText('No terminal targets.')).not.toBeInTheDocument();
    expect(screen.getByText(/before this cluster ran 26.11.1/)).toBeInTheDocument();
    expect(screen.queryByText(/Targets include the bastion/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'Connect'})).toBeDisabled();
});

it('uses identical selected and option labels, distinguishes duplicate names, and refreshes on focus', async () => {
    const desktops = {...client, listTargets: vi.fn().mockResolvedValue({targets: [
        {id: 'desktop:12345678-abcd', label: 'Work'}, {id: 'desktop:87654321-abcd', label: 'Work'}, {id: 'desktop:three', label: 'My desktop'},
    ]})};
    const {container} = render(<TerminalPanel client={desktops as any} />);
    const select = createWrapper(container).findSelect()!;
    await waitFor(() => expect(select.findTrigger().getElement()).toHaveTextContent('My desktop: Work (12345678)'));
    await userEvent.click(select.findTrigger().getElement());
    expect(await screen.findByText('My desktop: Work (87654321)')).toBeInTheDocument();
    expect(await screen.findByText('My desktop: three')).toBeInTheDocument();
    expect(select.findDropdown().getElement()).toHaveTextContent('My desktop: Work (12345678)');
    await userEvent.click(select.findTrigger().getElement());
    desktops.listTargets.mockResolvedValue({targets: [{id: 'desktop:new', label: 'New'}]});
    fireEvent.focus(select.findTrigger().getElement());
    await waitFor(() => expect(select.findTrigger().getElement()).toHaveTextContent('My desktop: New'));
    expect(select.findTrigger().getElement()).not.toHaveTextContent('(new)');
    fireEvent.click(screen.getByRole('button', {name: 'Connect'}));
    expect(mocks.options.target).toBe('desktop:new');
});


it('preserves the clipboard when Copy has no selected text', async () => {
    const clipboard = {writeText: vi.fn().mockResolvedValue(undefined)};
    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: clipboard});
    mocks.selection.mockReturnValueOnce('');
    render(<TerminalPanel client={client as any} />);
    await waitFor(() => expect(screen.getByRole('button', {name: 'Copy'})).toBeEnabled());
    fireEvent.click(screen.getByRole('button', {name: 'Copy'}));
    expect(clipboard.writeText).not.toHaveBeenCalled();
    expect(screen.getByText('Select text first.')).toBeInTheDocument();
});

it('omits migration guidance when targets are available', async () => {
    render(<TerminalPanel client={client as any} />);
    await waitFor(() => expect(screen.getByRole('button', {name: 'Connect'})).not.toBeDisabled());
    expect(screen.queryByText(/Targets include|older desktop/)).not.toBeInTheDocument();
});
