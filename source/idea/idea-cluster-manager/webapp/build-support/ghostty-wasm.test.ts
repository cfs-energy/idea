import {readFileSync} from 'node:fs';
import {expect, it} from 'vitest';
import {ghosttyWasm} from './ghostty-wasm';

it('externalizes the pinned package WASM and fails if its packaging changes', () => {
    const id = `${process.cwd()}/node_modules/ghostty-web/dist/ghostty-web.js`;
    const transform = ghosttyWasm().transform as (code: string, id: string) => {code: string} | undefined;
    const result = transform(readFileSync(id, 'utf8'), id)!;
    expect(result.code).toContain("import ideaGhosttyWasm from 'ghostty-web/ghostty-vt.wasm?url'");
    expect(result.code).not.toContain('data:application/wasm;base64,');
    expect(transform('unrelated', '/src/other.js')).toBeUndefined();
    expect(() => transform('changed upstream packaging', id)).toThrow('packaging changed');
});
