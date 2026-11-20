import {createRef} from 'react';
import {act, render} from '@testing-library/react';
import {expect, it} from 'vitest';
import {readFileSync} from 'node:fs';
import {PasswordStrengthCheck} from './password-strength-check';

// the server's symbol pattern, read from its source so the two cannot drift apart unnoticed
const source = readFileSync('../src/ideaclustermanager/app/accounts/accounts_service.py', 'utf8');
const pattern = source.match(/PASSWORD_SYMBOLS = r"""(.+?)"""/)?.[1];
const SERVER = new RegExp(pattern ?? '(?!)');
const PRINTABLE_SYMBOLS = Array.from({length: 94}, (_, i) => String.fromCharCode(33 + i)).filter(c => !/[A-Za-z0-9]/.test(c));

it('counts as a symbol exactly what the server counts as one', () => {
    expect(pattern, 'PASSWORD_SYMBOLS in accounts_service.py').toBeTruthy();
    const check = createRef<PasswordStrengthCheck>();
    render(<PasswordStrengthCheck ref={check}/>);
    const passes = (password: string) => {let result = false; act(() => {result = check.current!.checkPassword(password);}); return result;};
    for (const symbol of PRINTABLE_SYMBOLS) expect(passes(`Marker1${symbol}x`), symbol).toBe(SERVER.test(symbol));
    expect(passes('Marker1 x')).toBe(false);
    expect(passes('Marker1xy')).toBe(false);
});
