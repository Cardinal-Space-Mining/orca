// Shared helpers: the htm binding, formatting, small components and the
// hub API.

import { h } from 'preact';
import htm from 'htm';

export const html = htm.bind(h);


// --- Formatting --------------------------------------------------------------

export function formatBytes(bytes) {
    if (bytes == null) return '–';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1000 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

export function formatDuration(seconds) {
    if (seconds == null) return '–';
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    if (days > 0) return `${days}d ${hours % 24}h`;
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    if (minutes > 0) return `${minutes}m`;
    return `${Math.floor(seconds)}s`;
}

export function formatAgo(timestampSeconds, nowSeconds) {
    if (!timestampSeconds) return 'never';
    return `${formatDuration(Math.max(0, nowSeconds - timestampSeconds))} ago`;
}

export function formatMilliseconds(value) {
    if (value == null) return '–';
    const rounded = Math.abs(value) < 10 ? value.toFixed(1) : value.toFixed(0);
    return `${rounded} ms`;
}

export function formatOffset(value) {
    if (value == null) return '–';
    return `${value >= 0 ? '+' : '−'}${formatMilliseconds(Math.abs(value))}`;
}

export function shortCommit(head) {
    return head ? head.slice(0, 7) : '–';
}

export function levelFor(percent, warnAt, badAt) {
    if (percent >= badAt) return 'bad';
    if (percent >= warnAt) return 'warn';
    return '';
}


// --- Hub API -----------------------------------------------------------------

async function readResponse(response) {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || response.statusText);
    return body;
}

export async function getJson(path) {
    return readResponse(await fetch(path));
}

export async function postJson(path, body = {}) {
    return readResponse(await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }));
}


// --- Small components --------------------------------------------------------

export function StatusDot({ state }) {
    const level = {
        online: 'good', connecting: 'warn', 'installing agent': 'warn',
        'updating agent': 'warn', offline: 'bad',
    }[state] || '';
    const isPulsing = level === 'warn';
    return html`<span class="dot ${level} ${isPulsing ? 'pulse' : ''}"
        title=${state}></span>`;
}

// Render text with ANSI colour codes (ROS log levels, launch prefixes) as
// spans.
const ANSI_PATTERN = /\x1b\[([0-9;]*)m/g;
// Other terminal escapes (hyperlinks, cursor and erase commands) that log
// files can contain; live output arrives without them.
const OTHER_ESCAPES = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-ln-z@`]/g;

export function stripAnsi(text) {
    return text.replace(OTHER_ESCAPES, '').replace(ANSI_PATTERN, '');
}

export function AnsiText({ text: rawText }) {
    const text = rawText.replace(OTHER_ESCAPES, '');
    const parts = [];
    let classes = [];
    let last = 0;
    let match;
    ANSI_PATTERN.lastIndex = 0;
    while ((match = ANSI_PATTERN.exec(text)) !== null) {
        if (match.index > last) {
            parts.push(html`<span class=${classes.join(' ')}>${
                text.slice(last, match.index)}</span>`);
        }
        for (const code of (match[1] || '0').split(';').map(Number)) {
            if (code === 0) classes = [];
            else if (code === 1) classes.push('ansi-bold');
            else if (code === 2) classes.push('ansi-dim');
            else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
                classes = classes.filter((name) => !name.startsWith('ansi-fg'));
                classes.push(`ansi-fg-${code}`);
            } else if (code === 39) {
                classes = classes.filter((name) => !name.startsWith('ansi-fg'));
            }
        }
        last = ANSI_PATTERN.lastIndex;
    }
    if (last < text.length) {
        parts.push(html`<span class=${classes.join(' ')}>${
            text.slice(last)}</span>`);
    }
    return parts;
}
