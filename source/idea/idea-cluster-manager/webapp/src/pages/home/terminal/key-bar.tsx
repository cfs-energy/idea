import React from 'react';

export function modifiedInput(data: string, ctrl: boolean, alt: boolean): string {
    if (ctrl && data.length === 1) {
        const code = data.toUpperCase().charCodeAt(0);
        if (code >= 64 && code <= 95) data = String.fromCharCode(code - 64);
        else if (data === ' ') data = '\x00';
        else if (data === '?') data = '\x7f';
    }
    return (alt ? '\x1b' : '') + data;
}

// Only keys a phone keyboard lacks. Scroll enters tmux copy mode to read earlier output.
const keys = [['Esc', '\x1b'], ['Tab', '\t'], ['Left', '\x1b[D'], ['Down', '\x1b[B'], ['Up', '\x1b[A'], ['Right', '\x1b[C'], ['Scroll', '\x02[']];
export function KeyBar({ctrl, modifier, send, disabled}: {
    ctrl: boolean; modifier: () => void; send: (data: string) => void; disabled: boolean;
}) {
    return <div className="terminal-keys" role="group" aria-label="Terminal keys">
        <button type="button" disabled={disabled} aria-pressed={ctrl} onPointerDown={e => e.preventDefault()} onClick={modifier}>Ctrl</button>
        {keys.map(([label, value]) => <button type="button" key={label} disabled={disabled} aria-label={label} onPointerDown={e => e.preventDefault()} onClick={() => send(value)}>{label}</button>)}
    </div>;
}
