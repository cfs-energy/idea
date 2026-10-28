import {useSyncExternalStore} from 'react';
import type TerminalClient from '../../../client/terminal-client';
import type {TerminalTarget} from '../../../client/terminal-client';
import type {VirtualDesktopSession} from '../../../client/data-model';

export interface ShellTab {target: string; label: string}
export interface ShellState {
    tabs: ShellTab[]; active: string; expanded: boolean;
    targets?: TerminalTarget[]; targetsFailed: boolean;
}

export const terminalEnabled = () => (window as any).idea?.app?.web_terminal_enabled !== false;

const initial: ShellState = {tabs: [], active: '', expanded: true, targetsFailed: false};
let state = initial;
let loading: Promise<void> | undefined;
let inflight = false;
const listeners = new Set<() => void>();
const set = (next: Partial<ShellState>) => {state = {...state, ...next}; listeners.forEach(listener => listener());};

// One dock per page load. It lives beside the routes, so tabs and connections survive navigation.
export const shellStore = {
    get: () => state,
    subscribe(listener: () => void) {listeners.add(listener); return () => {listeners.delete(listener);};},
    open(target: string, label: string) {
        const tabs = state.tabs.some(tab => tab.target === target) ? state.tabs : [...state.tabs, {target, label}];
        set({tabs, active: target, expanded: true});
    },
    close(target: string) {
        const index = state.tabs.findIndex(tab => tab.target === target);
        const tabs = state.tabs.filter(tab => tab.target !== target);
        set({tabs, active: state.active === target ? tabs[Math.max(0, index - 1)]?.target ?? '' : state.active});
    },
    select: (target: string) => set({active: target, expanded: true}),
    setExpanded: (expanded: boolean) => set({expanded}),
    loadTargets(client: TerminalClient, force = false): Promise<void> {
        if (!terminalEnabled()) return Promise.resolve();
        if (loading && (!force || inflight)) return loading;
        inflight = true;
        loading = client.listTargets()
            .then(result => set({targets: result.targets, targetsFailed: false}))
            .catch(() => {loading = undefined; set({targetsFailed: true});})
            .finally(() => {inflight = false;});
        return loading;
    },
    reset() {state = initial; loading = undefined; inflight = false; listeners.forEach(listener => listener());},
};

export const useShell = () => useSyncExternalStore(shellStore.subscribe, shellStore.get);

export const bastionAvailable = (current: ShellState) => terminalEnabled() && !!current.targets?.some(target => target.id === 'bastion');

const LINUX_STOPPED = new Set(['STOPPED']);
const BROKEN = new Set(['ERROR', 'DELETING', 'DELETED']);
/** Why a desktop has no Terminal, or undefined when it can open one. Mirrors the server's target rules. */
export function desktopTerminalReason(session: VirtualDesktopSession, current: ShellState): string | undefined {
    if (session.base_os?.includes('windows')) return 'Terminal is for Linux desktops.';
    if (session.state && LINUX_STOPPED.has(session.state)) return 'Start this desktop to use Terminal.';
    if (session.state && BROKEN.has(session.state)) return 'This desktop is in an error or deleting state.';
    if (session.state !== 'READY' || session.recovering) return 'Terminal opens when the desktop is ready.';
    if (current.targetsFailed) return 'Terminal access could not be checked. Reload the page to try again.';
    if (!current.targets) return 'Checking terminal access…';
    if (!current.targets.some(target => target.id === `desktop:${session.idea_session_id}`)) return 'Desktops launched before 26.11.1 have no Terminal. Launch a new one to use it.';
}
