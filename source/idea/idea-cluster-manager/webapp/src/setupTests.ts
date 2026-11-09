// jest-dom adds custom matchers for asserting on DOM nodes, registered here against vitest's expect.
// learn more: https://github.com/testing-library/jest-dom
import '@testing-library/jest-dom/vitest';

// jsdom has no indexedDB; JobTemplatesService opens a database on construction.
import 'fake-indexeddb/auto';

// Node 22's experimental localStorage/sessionStorage globals evaluate to undefined without
// --localstorage-file, and their presence stops vitest's jsdom from installing the real ones.
class MemoryStorage {
    private store = new Map<string, string>();

    get length(): number {
        return this.store.size;
    }

    key(index: number): string | null {
        return Array.from(this.store.keys())[index] ?? null;
    }

    getItem(key: string): string | null {
        return this.store.has(key) ? this.store.get(key)! : null;
    }

    setItem(key: string, value: string) {
        this.store.set(String(key), String(value));
    }

    removeItem(key: string) {
        this.store.delete(key);
    }

    clear() {
        this.store.clear();
    }
}

if ((globalThis as any).localStorage == null) {
    Object.defineProperty(globalThis, 'localStorage', { value: new MemoryStorage(), writable: true, configurable: true });
}
if ((globalThis as any).sessionStorage == null) {
    Object.defineProperty(globalThis, 'sessionStorage', { value: new MemoryStorage(), writable: true, configurable: true });
}

// jsdom does not implement these browser APIs used by Cloudscape components.
class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
}

if (window.ResizeObserver == null) {
    (window as any).ResizeObserver = ResizeObserverStub;
}

// @xterm/xterm parses colors through a canvas 2d context at import time; jsdom only implements
// getContext with the optional canvas package, so a minimal stub is enough for the color round-trip.
HTMLCanvasElement.prototype.getContext = function () {
    return {
        fillStyle: '',
        fillRect: () => {},
        clearRect: () => {},
        getImageData: () => ({ data: [0, 0, 0, 255] }),
        measureText: () => ({ width: 0 })
    };
} as any;

if (window.matchMedia == null) {
    (window as any).matchMedia = (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false
    });
}

// jsdom has no layout, so every box measures zero. jsdom 26 left a computed width and height
// empty; jsdom 29 on reports 'auto', where a browser reports the used size in px. Cloudscape's
// top navigation reads the computed width and treats 0 as "no layout", but 'auto' parses to NaN
// and collapses every utility into the overflow menu, so report the zero jsdom lays out.
const computedStyle = window.getComputedStyle.bind(window);
const zeroWhenAuto = (property: string, value: string) =>
    (property === 'width' || property === 'height') && value === 'auto' ? '0px' : value;
window.getComputedStyle = ((element: Element, pseudo?: string | null) => {
    const style = computedStyle(element, pseudo);
    return new Proxy(style, {
        get(target, property) {
            if (property === 'getPropertyValue') {
                return (name: string) => zeroWhenAuto(name, target.getPropertyValue(name));
            }
            const value = Reflect.get(target, property, target);
            if (typeof value === 'function') return value.bind(target);
            return typeof property === 'string' && typeof value === 'string' ? zeroWhenAuto(property, value) : value;
        }
    });
}) as typeof window.getComputedStyle;

// Tests load Cloudscape without its stylesheets (css: false). A fly-out menu positions itself
// against its closest scrolling ancestor, the parent menu's options list, which only that
// stylesheet makes scroll; jsdom 26 computed every box's overflow as '', which hid this.
const scrollers = document.createElement('style');
scrollers.textContent = '[class*="awsui_options-list_"] { overflow-y: auto; }';
document.head.appendChild(scrollers);
