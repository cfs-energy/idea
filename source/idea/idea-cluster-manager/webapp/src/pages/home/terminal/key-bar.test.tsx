import React, {useState} from 'react';
import {fireEvent, render, screen} from '@testing-library/react';
import {expect, it, vi} from 'vitest';
import {KeyBar, modifiedInput} from './key-bar';

it('has touch keys, sticky modifiers, and no focus-stealing pointer default', () => {
    const send = vi.fn();
    function Example() {
        const [ctrl, setCtrl] = useState(false);
        return <KeyBar ctrl={ctrl} alt={false} modifier={() => setCtrl(value => !value)} send={send} disabled={false} />;
    }
    render(<Example />);
    fireEvent.click(screen.getByRole('button', {name: 'Ctrl'}));
    expect(screen.getByRole('button', {name: 'Ctrl'})).toHaveAttribute('aria-pressed', 'true');
    expect(fireEvent.pointerDown(screen.getByRole('button', {name: 'Tab'}))).toBe(false);
    fireEvent.click(screen.getByRole('button', {name: 'Tab'})); expect(send).toHaveBeenCalledWith('\t');
    for (const name of ['Esc', 'Alt', 'Left', 'Right', 'Up', 'Down', '|', '~', '/', '-']) expect(screen.getByRole('button', {name})).toBeEnabled();
    expect(modifiedInput('c', true, false)).toBe('\x03');
    expect(modifiedInput('x', true, true)).toBe('\x1b\x18');
    expect(modifiedInput('日本語', false, false)).toBe('日本語');
});


it('opens tmux copy mode from the touch key bar and keeps it disabled offline', () => {
    const send = vi.fn();
    const {rerender} = render(<KeyBar ctrl={false} alt={false} modifier={() => {}} send={send} disabled={false} />);
    fireEvent.click(screen.getByRole('button', {name: 'Scroll'}));
    expect(send).toHaveBeenCalledExactlyOnceWith('\x02[');
    fireEvent.click(screen.getByRole('button', {name: 'Up'}));
    expect(send).toHaveBeenLastCalledWith('\x1b[A');
    fireEvent.click(screen.getByRole('button', {name: 'Esc'}));
    expect(send).toHaveBeenLastCalledWith('\x1b');
    rerender(<KeyBar ctrl={false} alt={false} modifier={() => {}} send={send} disabled={true} />);
    expect(screen.getByRole('button', {name: 'Scroll'})).toBeDisabled();
});
