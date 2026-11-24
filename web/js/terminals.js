// Terminals: shells on any machine and VM serial consoles, shown in a dock
// at the bottom of every page. xterm.js (web/vendor) draws them; keystrokes
// go to the hub by POST and output comes back on the event stream.

import { useEffect, useRef, useState } from 'preact/hooks';
import { html, postJson } from './common.js';


const THEME = {
    background: '#08080a',
    foreground: '#e6e6e8',
    cursor: '#ff5c68',
    cursorAccent: '#08080a',
    selectionBackground: 'rgba(200, 32, 47, 0.35)',
    black: '#1d1e22', red: '#ff6b70', green: '#4fdc9c', yellow: '#f5b74a',
    blue: '#6fb3ff', magenta: '#d59bff', cyan: '#5fd6e0', white: '#e6e6e8',
    brightBlack: '#64666d', brightRed: '#ff8a8e', brightGreen: '#7ae8b6',
    brightYellow: '#ffcf7a', brightBlue: '#9ccaff', brightMagenta: '#e3bcff',
    brightCyan: '#8ee6ee', brightWhite: '#ffffff',
};


// --- Store -------------------------------------------------------------------

function terminalPath(entry, action = '') {
    return `/api/machines/${encodeURIComponent(entry.machineId)}/terminals/`
        + `${encodeURIComponent(entry.terminalId)}${action ? `/${action}` : ''}`;
}

class TerminalStore {
    constructor() {
        this.entries = [];        // open terminals, in tab order
        this.listeners = new Set();
        this.activeKey = null;
        this.isMinimized = false;
    }

    key(machineId, terminalId) {
        return `${machineId}/${terminalId}`;
    }

    find(machineId, terminalId) {
        return this.entries.find((entry) =>
            entry.key === this.key(machineId, terminalId));
    }

    notify() {
        for (const listener of this.listeners) listener();
    }

    async open(machineId, kind = 'shell') {
        const opened = await postJson(
            `/api/machines/${encodeURIComponent(machineId)}/terminals`,
            { kind, cols: 120, rows: 30 });
        const entry = {
            key: this.key(machineId, opened.terminal_id),
            machineId, terminalId: opened.terminal_id, title: opened.title,
            kind, pending: [], term: null, exited: null,
            queuedInput: '', isSending: false,
        };
        this.entries.push(entry);
        this.activeKey = entry.key;
        this.isMinimized = false;
        this.notify();
    }

    // --- Output ---

    onOutput({ machine_id, terminal_id, data }) {
        const entry = this.find(machine_id, terminal_id);
        if (!entry) return;
        if (entry.term) entry.term.write(data);
        else entry.pending.push(data);
    }

    onExit({ machine_id, terminal_id, exit_code, reason }) {
        const entry = this.find(machine_id, terminal_id);
        if (!entry) return;
        entry.exited = reason || `exited${exit_code != null
            ? ` with code ${exit_code}` : ''}`;
        const note = `\r\n\x1b[2m[${entry.exited}]\x1b[0m\r\n`;
        if (entry.term) entry.term.write(note);
        else entry.pending.push(note);
        this.notify();
    }

    attach(entry, term) {
        entry.term = term;
        for (const data of entry.pending) term.write(data);
        entry.pending = [];
    }

    // --- Input ---

    // Keystrokes are queued and sent in order; while one request is in
    // flight, new ones collect into the next.
    input(entry, data) {
        if (entry.exited) return;
        entry.queuedInput += data;
        if (!entry.isSending) this.sendInput(entry);
    }

    async sendInput(entry) {
        entry.isSending = true;
        while (entry.queuedInput && !entry.exited) {
            const data = entry.queuedInput;
            entry.queuedInput = '';
            try {
                await postJson(terminalPath(entry, 'input'), { data });
            } catch (failure) {
                break;
            }
        }
        entry.isSending = false;
    }

    resize(entry, columns, rows) {
        if (entry.exited) return;
        postJson(terminalPath(entry, 'resize'), { cols: columns, rows })
            .catch(() => {});
    }

    // --- Closing ---

    close(entry) {
        if (!entry.exited) {
            postJson(terminalPath(entry, 'close')).catch(() => {});
        }
        if (entry.term) entry.term.dispose();
        this.entries = this.entries.filter((other) => other !== entry);
        if (this.activeKey === entry.key) {
            this.activeKey = this.entries.length
                ? this.entries[this.entries.length - 1].key : null;
        }
        this.notify();
    }

    // When the page goes away, hang up its terminals (sendBeacon survives
    // the unload; fetch doesn't).
    closeAllOnUnload() {
        for (const entry of this.entries) {
            if (!entry.exited) {
                navigator.sendBeacon(terminalPath(entry, 'close'), '{}');
            }
        }
    }
}

export const terminalStore = new TerminalStore();
window.addEventListener('pagehide', () => terminalStore.closeAllOnUnload());

export function openTerminal(machineId, kind = 'shell') {
    return terminalStore.open(machineId, kind)
        .catch((failure) => window.alert(failure.message));
}

function useTerminalStore() {
    const [, setVersion] = useState(0);
    useEffect(() => {
        const listener = () => setVersion((version) => version + 1);
        terminalStore.listeners.add(listener);
        return () => terminalStore.listeners.delete(listener);
    }, []);
    return terminalStore;
}


// --- Dock --------------------------------------------------------------------

function TerminalView({ entry, isVisible }) {
    const container = useRef(null);

    useEffect(() => {
        const term = new window.Terminal({
            theme: THEME,
            fontFamily: 'ui-monospace, "JetBrains Mono", "SF Mono", Menlo, '
                + '"DejaVu Sans Mono", monospace',
            fontSize: 13,
            cursorBlink: true,
            scrollback: 5000,
        });
        const fit = new window.FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(container.current);
        term.onData((data) => terminalStore.input(entry, data));
        terminalStore.attach(entry, term);

        let size = '';
        const refit = () => {
            if (!container.current || container.current.offsetParent === null) {
                return;  // hidden tab
            }
            fit.fit();
            const next = `${term.cols}x${term.rows}`;
            if (next !== size) {
                size = next;
                terminalStore.resize(entry, term.cols, term.rows);
            }
        };
        const observer = new ResizeObserver(refit);
        observer.observe(container.current);
        entry.refit = refit;
        setTimeout(() => {
            refit();
            term.focus();
        }, 0);
        return () => observer.disconnect();
    }, [entry.key]);

    useEffect(() => {
        if (isVisible && entry.refit) {
            setTimeout(() => {
                entry.refit();
                entry.term && entry.term.focus();
            }, 0);
        }
    }, [isVisible]);

    return html`<div class="terminal-view" ref=${container}
        style=${isVisible ? '' : 'display: none'}></div>`;
}

export function TerminalDock() {
    const store = useTerminalStore();
    if (store.entries.length === 0) return null;
    const toggle = () => {
        store.isMinimized = !store.isMinimized;
        store.notify();
    };
    return html`
        <div class="terminal-dock ${store.isMinimized ? 'minimized' : ''}">
            <div class="terminal-tabs">
                ${store.entries.map((entry) => html`
                    <div class="terminal-tab ${entry.key === store.activeKey
                        ? 'active' : ''} ${entry.exited ? 'exited' : ''}"
                        key=${entry.key}
                        onClick=${() => {
                            store.activeKey = entry.key;
                            store.isMinimized = false;
                            store.notify();
                        }}>
                        <span class="dot ${entry.exited ? '' : 'good'}"></span>
                        ${entry.title}
                        <button class="tab-close" title="Close"
                            onClick=${(event) => {
                                event.stopPropagation();
                                store.close(entry);
                            }}>×</button>
                    </div>`)}
                <span class="spacer"></span>
                <button class="link-button" onClick=${toggle}>
                    ${store.isMinimized ? 'Show' : 'Hide'}</button>
            </div>
            <div class="terminal-body">
                ${store.entries.map((entry) => html`<${TerminalView}
                    key=${entry.key} entry=${entry}
                    isVisible=${!store.isMinimized
                        && entry.key === store.activeKey} />`)}
            </div>
        </div>`;
}
