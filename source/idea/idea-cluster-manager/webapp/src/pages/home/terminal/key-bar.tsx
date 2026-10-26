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

const keys = [['Scroll', '\x02['], ['Esc', '\x1b'], ['Tab', '\t'], ['Left', '\x1b[D'], ['Down', '\x1b[B'], ['Up', '\x1b[A'], ['Right', '\x1b[C'], ['|', '|'], ['~', '~'], ['/', '/'], ['-', '-']];
export function KeyBar({ctrl, alt, modifier, send, disabled}: {
    ctrl: boolean; alt: boolean; modifier: (key: 'ctrl' | 'alt') => void; send: (data: string) => void; disabled: boolean;
}) {
    return <div className="terminal-keys" role="group" aria-label="Terminal keys">
        <button type="button" disabled={disabled} aria-pressed={ctrl} onPointerDown={e => e.preventDefault()} onClick={() => modifier('ctrl')}>Ctrl</button>
        <button type="button" disabled={disabled} aria-pressed={alt} onPointerDown={e => e.preventDefault()} onClick={() => modifier('alt')}>Alt</button>
        {keys.map(([label, value]) => <button type="button" key={label} disabled={disabled} aria-label={label} onPointerDown={e => e.preventDefault()} onClick={() => send(value)}>{label}</button>)}
    </div>;
}
