// Terminals: shells on any machine and VM serial consoles, shown in a dock
// at the bottom of every page. xterm.js (web/vendor) draws them; keystrokes
// go to Orca by POST and output comes back on the event stream.

import { useEffect, useRef, useState } from 'preact/hooks';
import { html, postJson } from './common.js';


const THEME = {
    background: '#06090c',
    foreground: '#e0e7ee',
    cursor: '#ff5c68',
    cursorAccent: '#06090c',
    selectionBackground: 'rgba(76, 194, 255, 0.3)',
    black: '#151f2a', red: '#ff6b70', green: '#4fdc9c', yellow: '#f5b74a',
    blue: '#6fb3ff', magenta: '#d59bff', cyan: '#5fd6e0', white: '#e0e7ee',
    brightBlack: '#586779', brightRed: '#ff8a8e', brightGreen: '#7ae8b6',
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

    async open(machineId, kind = 'shell', extra = {}) {
        const opened = await postJson(
            `/api/machines/${encodeURIComponent(machineId)}/terminals`,
            { ...extra, kind, cols: 120, rows: 30 });
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

// `extra` goes to the server with the request (e.g. what to install a key
// on, for kind "install-key").
export function openTerminal(machineId, kind = 'shell', extra = {}) {
    return terminalStore.open(machineId, kind, extra)
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

// --- Dock layout ---------------------------------------------------------------

// 16x16 icons: where the panel sits, maximize/restore, show/hide.
const ICONS = {
    bottom: 'M2 2.5h12v11H2z M2 9.5h12',
    right: 'M2 2.5h12v11H2z M9.5 2.5v11',
    maximize: 'M3 3h10v10H3z',
    restore: 'M5 3h8v8H5z M3 5v8h8',
    hide: 'M4 6l4 4 4-4',
    show: 'M4 10l4-4 4 4',
};

function Icon({ name }) {
    return html`<svg width="16" height="16" viewBox="0 0 16 16" fill="none"
        stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"
        stroke-linecap="round"><path d=${ICONS[name]} /></svg>`;
}

// Docked at the bottom or the right, resizable by dragging its inner edge,
// maximizable; remembered per browser. The page makes room for it (body
// padding), so it never covers anything.
const LAYOUT_KEY = 'orca.terminal-dock';
const MIN_SIZE = 160;
const MINIMIZED_HEIGHT = 41;
const TOPBAR_HEIGHT = 60;

function loadLayout() {
    const defaults = { position: 'bottom', bottom: 0.42, right: 0.45,
        isMaximized: false };
    try {
        return { ...defaults,
            ...JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}') };
    } catch (failure) {
        return defaults;
    }
}

function useDockLayout() {
    const [layout, setLayout] = useState(loadLayout);
    const update = (changes) => setLayout((previous) => {
        const next = { ...previous, ...changes };
        localStorage.setItem(LAYOUT_KEY, JSON.stringify(next));
        return next;
    });
    return [layout, update];
}

// Sizes are stored as fractions of the window so they survive resizing.
function dockSize(layout) {
    const isRight = layout.position === 'right';
    const total = isRight ? window.innerWidth
        : window.innerHeight - TOPBAR_HEIGHT;
    if (layout.isMaximized) return Math.round(total * (isRight ? 0.8 : 1));
    return Math.round(Math.max(MIN_SIZE,
        Math.min(total - 120, total * layout[layout.position])));
}

function ResizeHandle({ layout, setLayout }) {
    function onPointerDown(event) {
        event.preventDefault();
        const handle = event.currentTarget;
        handle.setPointerCapture(event.pointerId);
        const isRight = layout.position === 'right';
        const total = isRight ? window.innerWidth
            : window.innerHeight - TOPBAR_HEIGHT;
        const move = (moveEvent) => {
            const size = isRight ? window.innerWidth - moveEvent.clientX
                : window.innerHeight - moveEvent.clientY;
            setLayout({ [layout.position]: Math.max(MIN_SIZE, size) / total,
                isMaximized: false });
        };
        const up = () => {
            handle.removeEventListener('pointermove', move);
            handle.removeEventListener('pointerup', up);
            document.body.classList.remove('is-resizing');
        };
        handle.addEventListener('pointermove', move);
        handle.addEventListener('pointerup', up);
        document.body.classList.add('is-resizing');
    }
    return html`<div class="dock-handle ${layout.position}"
        onPointerDown=${onPointerDown}
        onDblClick=${() => setLayout({ isMaximized: !layout.isMaximized })}
        title="Drag to resize; double-click to maximize"></div>`;
}

export function TerminalDock() {
    const store = useTerminalStore();
    const [layout, setLayout] = useDockLayout();
    const [, setWindowVersion] = useState(0);
    const isOpen = store.entries.length > 0;
    const isMinimized = store.isMinimized;
    const isRight = layout.position === 'right' && !isMinimized;
    const size = dockSize(layout);

    useEffect(() => {
        const onResize = () => setWindowVersion((version) => version + 1);
        window.addEventListener('resize', onResize);
        return () => window.removeEventListener('resize', onResize);
    }, []);

    // Make room for the dock beside or below the page.
    useEffect(() => {
        const style = document.body.style;
        style.paddingRight = isOpen && isRight ? `${size}px` : '';
        style.paddingBottom = !isOpen ? ''
            : isMinimized ? `${MINIMIZED_HEIGHT}px`
            : isRight ? '' : `${size}px`;
    }, [isOpen, isRight, isMinimized, size]);

    if (!isOpen) return null;
    const toggle = () => {
        store.isMinimized = !store.isMinimized;
        store.notify();
    };
    const dockStyle = isMinimized ? ''
        : isRight ? `width: ${size}px` : `height: ${size}px`;
    const iconButton = (label, title, onClick, isActive = false) => html`
        <button class="dock-button ${isActive ? 'active' : ''}" title=${title}
            onClick=${onClick}>${label}</button>`;
    return html`
        <div class="terminal-dock ${isRight ? 'right' : 'bottom'}
            ${isMinimized ? 'minimized' : ''}" style=${dockStyle}>
            ${!isMinimized && html`<${ResizeHandle} layout=${layout}
                setLayout=${setLayout} />`}
            <div class="terminal-tabs">
                <div class="terminal-tab-list">
                    ${store.entries.map((entry) => html`
                        <div class="terminal-tab ${entry.key === store.activeKey
                            ? 'active' : ''} ${entry.exited ? 'exited' : ''}"
                            key=${entry.key} title=${entry.title}
                            onClick=${() => {
                                store.activeKey = entry.key;
                                store.isMinimized = false;
                                store.notify();
                            }}>
                            <span class="dot ${entry.exited ? '' : 'good'}">
                                </span>
                            <span class="terminal-tab-title">${entry.title}
                                </span>
                            <button class="tab-close" title="Close"
                                onClick=${(event) => {
                                    event.stopPropagation();
                                    store.close(entry);
                                }}>×</button>
                        </div>`)}
                </div>
                <div class="dock-buttons">
                    ${iconButton(html`<${Icon} name="bottom" />`,
                        'Dock at the bottom', () => setLayout(
                            { position: 'bottom' }),
                        layout.position === 'bottom')}
                    ${iconButton(html`<${Icon} name="right" />`,
                        'Dock on the right (beside the page)', () => setLayout(
                            { position: 'right' }),
                        layout.position === 'right')}
                    ${!isMinimized && iconButton(html`<${Icon}
                        name=${layout.isMaximized ? 'restore' : 'maximize'} />`,
                        layout.isMaximized ? 'Restore size' : 'Maximize',
                        () => setLayout({ isMaximized: !layout.isMaximized }))}
                    ${iconButton(html`<${Icon}
                        name=${isMinimized ? 'show' : 'hide'} />`,
                        isMinimized ? 'Show' : 'Hide', toggle)}
                </div>
            </div>
            <div class="terminal-body">
                ${store.entries.map((entry) => html`<${TerminalView}
                    key=${entry.key} entry=${entry}
                    isVisible=${!isMinimized
                        && entry.key === store.activeKey} />`)}
            </div>
        </div>`;
}
