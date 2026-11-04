import {useSyncExternalStore} from 'react';
import type {Terminal} from 'ghostty-web';

export const coarsePointer = () => typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches;

// Keys a phone's on-screen keyboard cannot produce. iOS sends keydown for soft-keyboard letters,
// so only these prove a hardware keyboard.
const HARDWARE_KEYS = new Set(['Escape', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Control', 'Meta', 'Alt', 'PageUp', 'PageDown', 'Home', 'End']);
export const isHardwareKey = (event: KeyboardEvent) => event.ctrlKey || event.metaKey || event.altKey || HARDWARE_KEYS.has(event.key) || /^F\d+$/.test(event.key);

let hardwareKeyboard = false;
const keyboardListeners = new Set<() => void>();
const onHardwareKey = (event: KeyboardEvent) => {
    if (hardwareKeyboard || !isHardwareKey(event)) return;
    hardwareKeyboard = true; keyboardListeners.forEach(listener => listener());
};
const subscribeKeyboard = (listener: () => void) => {
    if (!keyboardListeners.size) window.addEventListener('keydown', onHardwareKey, true);
    keyboardListeners.add(listener);
    return () => {keyboardListeners.delete(listener); if (!keyboardListeners.size) window.removeEventListener('keydown', onHardwareKey, true);};
};
export const resetHardwareKeyboard = () => {hardwareKeyboard = false;};
/** The key bar is for touch screens without a keyboard: never on a laptop, gone once a hardware key arrives. */
export const useTouchKeyBar = () => useSyncExternalStore(subscribeKeyboard, () => coarsePointer() && !hardwareKeyboard);

export const caretKeys = (delta: number, applicationCursor: boolean) =>
    `${applicationCursor ? '\x1bO' : '\x1b['}${delta < 0 ? 'D' : 'C'}`.repeat(Math.abs(delta));

export const CARET_PAD = 32;
const SENTINEL = ' '.repeat(CARET_PAD * 2);

/** Touch-only: native caret gestures (space-bar trackpad, cursor handles) move the shell cursor. */
export function trackCaret(textarea: HTMLTextAreaElement, send: (data: string) => void, applicationCursor: () => boolean) {
    if (!coarsePointer()) return () => {};
    let composing = false;
    const prime = () => {if (textarea.value !== SENTINEL) textarea.value = SENTINEL; textarea.setSelectionRange(CARET_PAD, CARET_PAD);};
    const moved = () => {
        if (document.activeElement !== textarea || composing) return;
        if (textarea.value !== SENTINEL) {prime(); return;}
        const {selectionStart: start, selectionEnd: end} = textarea;
        if (start !== end || start === CARET_PAD) return;
        // An end of the buffer means a vertical drag or a jump, not a counted move; ignore it.
        if (start > 0 && start < SENTINEL.length) send(caretKeys(start - CARET_PAD, applicationCursor()));
        prime();
    };
    const compositionStart = () => {composing = true;};
    const compositionEnd = () => {composing = false; setTimeout(prime);};
    const input = () => {if (!composing) prime();};
    Object.entries({autocomplete: 'off', autocorrect: 'off', autocapitalize: 'off', spellcheck: 'false', inputmode: 'text', enterkeyhint: 'enter', wrap: 'off'})
        .forEach(([name, value]) => textarea.setAttribute(name, value));
    document.addEventListener('selectionchange', moved);
    textarea.addEventListener('focus', prime); textarea.addEventListener('input', input);
    textarea.addEventListener('compositionstart', compositionStart); textarea.addEventListener('compositionend', compositionEnd);
    prime();
    return () => {
        document.removeEventListener('selectionchange', moved);
        textarea.removeEventListener('focus', prime); textarea.removeEventListener('input', input);
        textarea.removeEventListener('compositionstart', compositionStart); textarea.removeEventListener('compositionend', compositionEnd);
    };
}

export function visibleText(term: Terminal) {
    const buffer = term.buffer.active;
    const top = Math.max(0, buffer.length - term.rows - Math.round(term.getViewportY()));
    return Array.from({length: term.rows}, (_, row) => buffer.getLine(top + row)?.translateToString(true) ?? '').join('\n').replace(/\n+$/, '');
}

/**
 * Touch-only gestures ghostty-web 0.4.0 lacks: one-finger drag scrolls like a trackpad (through ghostty's
 * own wheel handling, with momentum), pinch changes the text size, and a long press opens native selection.
 */
export function touchGestures(element: HTMLElement, options: {zoom: (scale: number, done: boolean) => void; longPress: (x: number, y: number) => void}) {
    if (!coarsePointer()) return () => {};
    let lastY = 0, lastTime = 0, velocity = 0, pending = 0, frame = 0, pinch = 0, scale = 1, timer: ReturnType<typeof setTimeout> | undefined;
    let startX = 0, startY = 0, moved = false, pressed = false;
    const wheel = (deltaY: number) => element.dispatchEvent(new WheelEvent('wheel', {deltaY, deltaMode: 0, bubbles: true, cancelable: true}));
    // Alternate-screen wheel steps are 33px in ghostty; batch small moves so none are lost.
    const scroll = (deltaY: number) => {pending += deltaY; if (Math.abs(pending) >= 33) {wheel(pending); pending = 0;}};
    const distance = (touches: TouchList) => Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
    const stop = () => {cancelAnimationFrame(frame); frame = 0; clearTimeout(timer); timer = undefined;};
    const start = (event: TouchEvent) => {
        stop();
        if (event.touches.length === 2) {pinch = distance(event.touches); scale = 1; moved = true; return;}
        if (event.touches.length !== 1) return;
        const touch = event.touches[0];
        startX = touch.clientX; startY = lastY = touch.clientY; lastTime = event.timeStamp; velocity = 0; pending = 0; moved = pressed = false;
        timer = setTimeout(() => {timer = undefined; if (!moved) {pressed = true; options.longPress(startX, startY);}}, 500);
    };
    const move = (event: TouchEvent) => {
        event.preventDefault();
        if (event.touches.length === 2 && pinch) {scale = distance(event.touches) / pinch; options.zoom(scale, false); return;}
        const touch = event.touches[0];
        if (!touch) return;
        if (Math.hypot(touch.clientX - startX, touch.clientY - startY) > 8) {moved = true; clearTimeout(timer);}
        if (!moved) return;
        const delta = lastY - touch.clientY, elapsed = Math.max(1, event.timeStamp - lastTime);
        velocity = 0.8 * (delta / elapsed) + 0.2 * velocity;
        lastY = touch.clientY; lastTime = event.timeStamp;
        scroll(delta);
    };
    const end = (event: TouchEvent) => {
        clearTimeout(timer); timer = undefined;
        // Only a plain tap reaches ghostty's canvas handler, which focuses the input and raises the keyboard.
        if (pinch || moved || pressed) {event.stopPropagation(); event.preventDefault();}
        if (pinch) {if (event.touches.length < 2) {options.zoom(scale, true); pinch = 0; moved = false; startX = startY = NaN;} return;}
        if (!moved) return;
        // Momentum: keep scrolling at the release speed, decaying each frame like a native list.
        let speed = velocity * 16;
        const coast = () => {
            speed *= 0.95;
            if (Math.abs(speed) < 0.5) {frame = 0; return;}
            scroll(speed); frame = requestAnimationFrame(coast);
        };
        if (Math.abs(speed) > 1) frame = requestAnimationFrame(coast);
    };
    element.addEventListener('touchstart', start, {passive: true});
    element.addEventListener('touchmove', move, {passive: false});
    element.addEventListener('touchend', end, {capture: true}); element.addEventListener('touchcancel', stop);
    return () => {
        stop();
        element.removeEventListener('touchstart', start); element.removeEventListener('touchmove', move);
        element.removeEventListener('touchend', end, {capture: true}); element.removeEventListener('touchcancel', stop);
    };
}
