import React from 'react';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {FONT_KEY, HEIGHT_KEY, isCopyKey, ShellDock, TerminalRedirect, TerminalView, terminalEnabled} from './terminal';
import {PORTAL_TASKS} from '../../navigation/task-navigation';
import {initTestAppContext} from '../../test-support';
import {shellStore} from './terminal/shell-store';
import {resetHardwareKeyboard} from './terminal/touch';
import {MemoryRouter, Route, Routes, useNavigate} from 'react-router-dom';

const mocks = vi.hoisted(() => ({
    load: vi.fn(), focus: vi.fn(), dispose: vi.fn(), fit: vi.fn(), start: vi.fn(), stop: vi.fn(),
    input: vi.fn(() => true), send: vi.fn(), wake: vi.fn(), setTheme: vi.fn(), render: vi.fn(),
    reset: vi.fn(), paste: vi.fn(), write: vi.fn(), selection: vi.fn(() => 'selected output'), hasSelection: vi.fn(() => true), mode: vi.fn(() => false),
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
        textarea?: HTMLTextAreaElement; buffer = {active: {length: 2, getLine: (y: number) => ({translateToString: () => ['$ ls', 'file.txt'][y]})}};
        loadAddon() {} open(element: HTMLElement) {this.element = element; this.textarea = document.createElement('textarea'); element.appendChild(this.textarea);} getViewportY() {return 0;}
        registerLinkProvider() {} getSelection = mocks.selection; hasSelection = mocks.hasSelection; getMode = mocks.mode;
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
const pointer = (coarse: boolean) => vi.stubGlobal('matchMedia', (query: string) => ({matches: coarse && query === '(pointer: coarse)', media: query, addEventListener() {}, removeEventListener() {}}));
beforeEach(() => {
    vi.resetModules(); vi.clearAllMocks(); mocks.load.mockResolvedValue({}); mocks.hasSelection.mockReturnValue(true);
    shellStore.reset(); resetHardwareKeyboard(); localStorage.clear(); pointer(false);
    vi.stubGlobal('ResizeObserver', class {observe() {} disconnect() {}});
    const computed = window.getComputedStyle;
    vi.spyOn(window, 'getComputedStyle').mockImplementation(element => {
        const style = computed(element);
        if (element.classList.contains('terminal-surface')) {style.backgroundColor = 'rgb(255, 255, 255)'; style.color = 'rgb(15, 20, 25)';}
        return style;
    });
});
afterEach(() => {vi.unstubAllGlobals(); vi.restoreAllMocks();});
const view = async (target = 'bastion') => {
    const result = render(<TerminalView client={client as any} target={target} />);
    await waitFor(() => expect(mocks.start).toHaveBeenCalled());
    return result;
};
const key = (init: KeyboardEventInit) => new KeyboardEvent('keydown', init);

it('waits for WASM, connects on open, and preserves state, resize, focus and Ctrl', async () => {
    let loaded!: (value: object) => void;
    mocks.load.mockImplementationOnce(() => new Promise(resolve => {loaded = resolve;}));
    const {unmount} = render(<TerminalView client={client as any} target="bastion" />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading terminal…');
    await waitFor(() => expect(mocks.load).toHaveBeenCalled());
    expect(mocks.load.mock.calls[0][0]).toContain('ghostty-vt.wasm');
    expect(mocks.start).not.toHaveBeenCalled();
    await act(async () => loaded({}));
    await waitFor(() => expect(mocks.start).toHaveBeenCalledOnce());
    expect(mocks.options.target).toBe('bastion'); expect(mocks.focus).toHaveBeenCalled();
    act(() => mocks.options.state('connected'));
    expect(screen.getByRole('status')).toHaveTextContent('Connected');
    expect(mocks.send).toHaveBeenCalledWith({type: 'resize', cols: 80, rows: 24});
    act(() => mocks.onResize({cols: 100, rows: 30}));
    expect(mocks.send).toHaveBeenLastCalledWith({type: 'resize', cols: 100, rows: 30});
    act(() => mocks.onData('日本語')); expect(mocks.input).toHaveBeenCalledWith('日本語');
    fireEvent.click(screen.getByRole('button', {name: 'Larger terminal text'}));
    expect(mocks.terminalOptions.fontSize).toBe(17); expect(mocks.fit).toHaveBeenCalled();
    expect(localStorage.getItem(FONT_KEY)).toBe('17');
    expect(mocks.onKey(key({key: 'x'}))).toBe(false);
    expect(mocks.onKey(key({key: 'Escape', shiftKey: true}))).toBe(true);
    expect(screen.getByRole('button', {name: 'Disconnect'})).toHaveFocus();
    mocks.options.output('output');
    expect(mocks.write).toHaveBeenCalledWith('output');
    act(() => mocks.options.state('reconnecting'));
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting');
    fireEvent.click(screen.getByRole('button', {name: 'Disconnect'}));
    expect(screen.getByRole('button', {name: 'Connect'})).toBeEnabled();
    unmount(); expect(mocks.stop).toHaveBeenCalled(); expect(mocks.dispose).toHaveBeenCalled();
});

it('has no copy, paste or key bar controls on a laptop', async () => {
    await view();
    act(() => mocks.options.state('connected'));
    expect(screen.queryByRole('button', {name: 'Copy'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Paste'})).not.toBeInTheDocument();
    expect(screen.queryByRole('group', {name: 'Terminal keys'})).not.toBeInTheDocument();
});

it.each([
    ['macOS Cmd+C', {code: 'KeyC', key: 'c', metaKey: true}, true, true],
    ['macOS Ctrl+C', {code: 'KeyC', key: 'c', ctrlKey: true}, true, false],
    ['Linux Ctrl+Shift+C', {code: 'KeyC', key: 'C', ctrlKey: true, shiftKey: true}, false, true],
    ['Linux Ctrl+C', {code: 'KeyC', key: 'c', ctrlKey: true}, false, false],
    ['Windows Cmd-less Shift+C', {code: 'KeyC', key: 'C', shiftKey: true}, false, false],
])('maps %s to copy=%s', (_name, init, mac, copy) => {
    expect(isCopyKey(key(init), mac as boolean)).toBe(copy);
});

it('copies the selection with the copy key and passes Ctrl+C and paste keys to ghostty', async () => {
    const clipboard = {writeText: vi.fn().mockResolvedValue(undefined)};
    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: clipboard});
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('Linux x86_64');
    await view();
    expect(mocks.onKey(key({code: 'KeyC', key: 'C', ctrlKey: true, shiftKey: true}))).toBe(true);
    expect(clipboard.writeText).toHaveBeenCalledExactlyOnceWith('selected output');
    // Returning false lets ghostty encode Ctrl+C as an interrupt and leave Ctrl+Shift+V to the browser paste event.
    expect(mocks.onKey(key({code: 'KeyC', key: 'c', ctrlKey: true}))).toBe(false);
    expect(mocks.onKey(key({code: 'KeyV', key: 'V', ctrlKey: true, shiftKey: true}))).toBe(false);
    expect(clipboard.writeText).toHaveBeenCalledOnce();
    mocks.hasSelection.mockReturnValue(false);
    expect(mocks.onKey(key({code: 'KeyC', key: 'C', ctrlKey: true, shiftKey: true}))).toBe(true);
    expect(clipboard.writeText).toHaveBeenCalledOnce();
});

it('shows the touch key bar on a coarse pointer until a hardware key arrives', async () => {
    pointer(true);
    await view();
    act(() => mocks.options.state('connected'));
    const bar = screen.getByRole('group', {name: 'Terminal keys'});
    fireEvent.click(screen.getByRole('button', {name: 'Ctrl'})); act(() => mocks.onData('c'));
    expect(mocks.input).toHaveBeenLastCalledWith('\x03');
    expect(screen.getByRole('button', {name: 'Ctrl'})).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByRole('button', {name: 'Esc'})); expect(mocks.input).toHaveBeenLastCalledWith('\x1b');
    // A soft-keyboard letter is not proof of a hardware keyboard.
    fireEvent.keyDown(window, {key: 'a'});
    expect(bar).toBeInTheDocument();
    fireEvent.keyDown(window, {key: 'Escape'});
    expect(screen.queryByRole('group', {name: 'Terminal keys'})).not.toBeInTheDocument();
});

it('prepares the hidden input for phone keyboards and moves the shell cursor with native caret gestures', async () => {
    pointer(true);
    await view();
    const textarea = document.querySelector('textarea')!;
    for (const [name, value] of [['autocapitalize', 'off'], ['autocorrect', 'off'], ['autocomplete', 'off'], ['spellcheck', 'false'], ['inputmode', 'text']]) expect(textarea).toHaveAttribute(name, value);
    textarea.focus();
    expect(textarea.selectionStart).toBe(32);
    textarea.setSelectionRange(35, 35);
    act(() => {document.dispatchEvent(new Event('selectionchange'));});
    expect(mocks.input).toHaveBeenLastCalledWith('\x1b[C\x1b[C\x1b[C');
    expect(textarea.selectionStart).toBe(32);
    mocks.mode.mockReturnValue(true);
    textarea.setSelectionRange(31, 31);
    act(() => {document.dispatchEvent(new Event('selectionchange'));});
    expect(mocks.input).toHaveBeenLastCalledWith('\x1bOD');
    expect(mocks.mode).toHaveBeenCalledWith(1);
    // A jump to the end is a vertical drag: nothing is sent.
    const calls = mocks.input.mock.calls.length;
    textarea.setSelectionRange(64, 64);
    act(() => {document.dispatchEvent(new Event('selectionchange'));});
    expect(mocks.input).toHaveBeenCalledTimes(calls);
    expect(textarea.selectionStart).toBe(32);
});

it('leaves the hidden input and caret alone on a laptop', async () => {
    await view();
    const textarea = document.querySelector('textarea')!;
    expect(textarea).not.toHaveAttribute('inputmode');
    textarea.focus(); textarea.value = 'abc'; textarea.setSelectionRange(1, 1);
    act(() => {document.dispatchEvent(new Event('selectionchange'));});
    expect(mocks.input).not.toHaveBeenCalled();
    expect(textarea.value).toBe('abc');
});

it('bridges phone input', async () => {
    await view();
    const surface = screen.getByLabelText('Shell. Shift Escape moves focus to the controls.');
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
    document.body.classList.remove('awsui-dark-mode');
});

const touches = (...points: [number, number][]) => points.map(([clientX, clientY], identifier) => ({clientX, clientY, identifier}));
const touch = (element: Element, type: string, points: [number, number][], timeStamp = 0) => {
    const event = new Event(type, {bubbles: true, cancelable: true});
    Object.defineProperties(event, {touches: {value: type === 'touchend' ? [] : touches(...points)}, changedTouches: {value: touches(...points)}, timeStamp: {value: timeStamp}});
    act(() => {element.dispatchEvent(event);});
    return event;
};

it('scrolls with momentum through ghostty wheel handling, pinches the text size and opens native selection on a phone', async () => {
    pointer(true);
    vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame']});
    try {
        render(<TerminalView client={client as any} target="bastion" />);
        await vi.waitFor(() => expect(mocks.start).toHaveBeenCalled());
        const surface = screen.getByLabelText('Shell. Shift Escape moves focus to the controls.');
        const wheels: number[] = [];
        surface.addEventListener('wheel', event => wheels.push((event as WheelEvent).deltaY));
        // ghostty-web focuses its input from the canvas touchend: a tap must reach it, a drag must not.
        const canvas = document.querySelector('textarea')!, tapped = vi.fn();
        canvas.addEventListener('touchend', tapped);
        touch(canvas, 'touchstart', [[100, 300]], 0); touch(canvas, 'touchend', [[100, 300]], 50);
        expect(tapped).toHaveBeenCalledOnce();
        touch(canvas, 'touchstart', [[100, 300]], 0);
        expect(touch(canvas, 'touchmove', [[100, 260]], 16).defaultPrevented).toBe(true);
        touch(canvas, 'touchmove', [[100, 220]], 32);
        expect(wheels).toEqual([40, 40]);
        touch(canvas, 'touchend', [[100, 220]], 48);
        expect(tapped).toHaveBeenCalledOnce();
        act(() => {vi.advanceTimersByTime(500);});
        expect(wheels.length).toBeGreaterThan(3);
        expect(screen.queryByRole('dialog', {name: 'Select terminal text'})).not.toBeInTheDocument();
        touch(surface, 'touchstart', [[100, 100], [100, 200]]);
        touch(surface, 'touchmove', [[100, 50], [100, 250]]);
        touch(surface, 'touchend', [[100, 50]]);
        expect(mocks.terminalOptions.fontSize).toBe(28);
        expect(localStorage.getItem(FONT_KEY)).toBe('28');
        touch(surface, 'touchstart', [[40, 40]]);
        act(() => {vi.advanceTimersByTime(600);});
        const layer = screen.getByRole('dialog', {name: 'Select terminal text'});
        expect(layer).toHaveTextContent('$ ls');
        const paste = new Event('paste', {bubbles: true, cancelable: true});
        Object.defineProperty(paste, 'clipboardData', {value: {getData: () => 'echo hi'}});
        act(() => {layer.querySelector('.terminal-select-text')!.dispatchEvent(paste);});
        expect(mocks.paste).toHaveBeenCalledWith('echo hi');
        expect(screen.queryByRole('dialog', {name: 'Select terminal text'})).not.toBeInTheDocument();
    } finally {vi.useRealTimers();}
});

it('ignores touch gestures on a laptop', async () => {
    await view();
    const surface = screen.getByLabelText('Shell. Shift Escape moves focus to the controls.');
    const wheels = vi.fn(); surface.addEventListener('wheel', wheels);
    touch(surface, 'touchstart', [[100, 300]]);
    expect(touch(surface, 'touchmove', [[100, 200]], 16).defaultPrevented).toBe(false);
    expect(wheels).not.toHaveBeenCalled();
});

it('gates the dock and removes Terminal from the navigation', () => {
    initTestAppContext();
    const app = (window as any).idea.app;
    app.web_terminal_enabled = false;
    expect(terminalEnabled()).toBe(false);
    delete app.web_terminal_enabled;
    expect(terminalEnabled()).toBe(true);
    expect(PORTAL_TASKS.some(task => task.id === 'terminal' || task.views.some(item => item.path === '/home/terminal'))).toBe(false);
});

it('reports a WASM failure without connecting', async () => {
    mocks.load.mockRejectedValueOnce(new Error('WASM fetch failed'));
    render(<TerminalView client={client as any} target="bastion" />);
    await screen.findByText('Terminal could not be loaded. Reload the page to try again.');
    expect(screen.getByRole('button', {name: 'Connect'})).toBeDisabled();
    expect(mocks.start).not.toHaveBeenCalled();
});

it('disposes late WASM initialization after closing', async () => {
    let loaded!: (value: object) => void;
    mocks.load.mockImplementationOnce(() => new Promise(resolve => {loaded = resolve;}));
    const {unmount} = render(<TerminalView client={client as any} target="bastion" />);
    await waitFor(() => expect(mocks.load).toHaveBeenCalled());
    unmount();
    await act(async () => loaded({}));
    expect(mocks.dispose).toHaveBeenCalledOnce();
    expect(mocks.start).not.toHaveBeenCalled();
});

it('accepts output while animation frames are suspended, including after renderer cleanup', async () => {
    vi.stubGlobal('requestAnimationFrame', vi.fn());
    mocks.write.mockImplementation((_data: unknown, callback?: () => void) => {if (callback) requestAnimationFrame(callback);});
    const {unmount} = await view();
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
    ['reconnecting', 'unavailable', 'the terminal limit has been reached. Close any extra terminals.'],
    ['denied', 'access-denied', 'Terminal access was denied.'],
])('explains %s %s', async (state, reason, message) => {
    await view();
    act(() => mocks.options.state(state, reason));
    expect(screen.getByRole('status')).toHaveTextContent(message);
});

function Pages() {
    const navigate = useNavigate();
    return <><button onClick={() => navigate('/b')}>Go</button><Routes><Route path="/a" element={<p>Page A</p>}/><Route path="/b" element={<p>Page B</p>}/></Routes></>;
}

it('opens tabs per target, keeps them across navigation, closes per tab and remembers its height', async () => {
    localStorage.setItem(HEIGHT_KEY, '400');
    render(<MemoryRouter initialEntries={['/a']}><Pages/><ShellDock client={client as any}/></MemoryRouter>);
    expect(screen.queryByRole('region', {name: 'Terminal'})).not.toBeInTheDocument();
    act(() => shellStore.open('bastion', 'Bastion'));
    act(() => shellStore.open('desktop:one', 'Work'));
    act(() => shellStore.open('bastion', 'Bastion'));
    const dock = screen.getByRole('region', {name: 'Terminal'});
    expect(dock.style.height).toBe('400px');
    expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Bastion', 'Work']);
    expect(screen.getByRole('tab', {name: 'Bastion'})).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(mocks.start).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', {name: 'Go'}));
    expect(screen.getByText('Page B')).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(2);
    expect(mocks.stop).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('tab', {name: 'Work'}));
    expect(screen.getByRole('tab', {name: 'Work'})).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('tabpanel', {hidden: true})[0]).toHaveStyle({display: 'none'});
    const separator = screen.getByRole('separator', {name: 'Resize terminal'});
    fireEvent.keyDown(separator, {key: 'ArrowUp'});
    expect(localStorage.getItem(HEIGHT_KEY)).toBe('424');
    fireEvent.click(screen.getByRole('button', {name: 'Minimize terminal'}));
    expect(screen.queryByRole('separator')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: 'Restore terminal'}));
    fireEvent.click(screen.getByRole('button', {name: 'Close Work'}));
    expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Bastion']);
    expect(mocks.stop).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', {name: 'Close Bastion'}));
    expect(screen.queryByRole('region', {name: 'Terminal'})).not.toBeInTheDocument();
});

it('keeps the phone dock above the keyboard with the visual viewport', async () => {
    const viewport = Object.assign(new EventTarget(), {height: 420, offsetTop: 12});
    vi.stubGlobal('visualViewport', viewport); vi.stubGlobal('innerWidth', 390);
    render(<MemoryRouter><ShellDock client={client as any}/></MemoryRouter>);
    act(() => shellStore.open('bastion', 'Bastion'));
    const dock = screen.getByRole('region', {name: 'Terminal'});
    expect(dock.style.height).toBe('420px'); expect(dock.style.top).toBe('12px');
    viewport.height = 300; viewport.offsetTop = 20;
    act(() => {viewport.dispatchEvent(new Event('resize'));});
    expect(dock.style.height).toBe('300px'); expect(dock.style.top).toBe('20px');
    fireEvent.click(screen.getByRole('button', {name: 'Minimize terminal'}));
    expect(dock.style.top).toBe('');
    await waitFor(() => expect(mocks.start).toHaveBeenCalled());
});

it('redirects the old Terminal page to the dock', async () => {
    render(<MemoryRouter initialEntries={['/home/terminal']}><Routes>
        <Route path="/home/terminal" element={<TerminalRedirect client={client as any}/>}/>
        <Route path="/" element={<p>Home</p>}/>
    </Routes></MemoryRouter>);
    expect(await screen.findByText('Home')).toBeInTheDocument();
    await waitFor(() => expect(shellStore.get().tabs).toEqual([{target: 'bastion', label: 'Bastion'}]));
});
