import type {Plugin} from 'vite';

// 0.4.0 embeds the WASM in its ESM bundle. Emit one same-origin asset instead.
export function ghosttyWasm(): Plugin {
    return {
        name: 'idea:ghostty-wasm',
        enforce: 'pre',
        transform(code, id) {
            if (!id.endsWith('/ghostty-web/dist/ghostty-web.js')) return;
            const embedded = /"data:application\/wasm;base64,[A-Za-z0-9+/=]+"/g;
            if (code.match(embedded)?.length !== 1) throw new Error('ghostty-web WASM packaging changed');
            return {
                code: `import ideaGhosttyWasm from 'ghostty-web/ghostty-vt.wasm?url';\n${code.replace(embedded, 'ideaGhosttyWasm')}`,
                map: null,
            };
        },
    };
}
