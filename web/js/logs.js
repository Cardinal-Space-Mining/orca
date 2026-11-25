// The logs page: log files on each machine (orca runs, the latest
// build, ROS logs, the agent, the VM's autostart log), viewed with the same
// filters as live output, and a cleanup panel for what piles up on a
// machine's disk (old logs, launch overlays).

import { useEffect, useMemo, useState } from 'preact/hooks';
import {
    html, formatAgo, formatBytes, getJson, Icon, postJson, StatusDot,
} from './common.js';
import { LineView } from './console.js';
import { DiskBar } from './bags.js';


const GROUP_ORDER = [
    'Orca runs', 'Latest build', 'ROS', 'Agent', 'VM'];
const READ_BYTES = 2 * 1024 * 1024;
// Groups whose files can be deleted (the agent's and the VM's logs are open).
const DELETABLE_GROUPS = ['Orca runs', 'Latest build', 'ROS'];

function machinePath(machineId, rest) {
    return `/api/machines/${encodeURIComponent(machineId)}/${rest}`;
}

function logUrl(machineId, path, extra = '') {
    return `/api/machines/${encodeURIComponent(machineId)}/logs/read?path=`
        + `${encodeURIComponent(path)}&max_bytes=${READ_BYTES}${extra}`;
}

function LogList({ logs, selectedPath, onSelect, now, cleanupBytes }) {
    const [query, setQuery] = useState('');
    const needle = query.toLowerCase();
    const groups = GROUP_ORDER.map((group) => [group, logs.filter((log) =>
        log.group === group && (!needle
            || log.label.toLowerCase().includes(needle)))]);
    return html`
        <nav class="config-list">
            <input class="input log-search" placeholder="Filter logs"
                value=${query}
                onInput=${(event) => setQuery(event.target.value)} />
            <button class="config-preset log-item cleanup-item ${
                selectedPath ? '' : 'active'}" onClick=${() => onSelect(null)}>
                <span><${Icon} name="trash-2" size=${13} /> Clean up</span>
                <small>${cleanupBytes == null ? 'old logs and leftovers'
                    : `${formatBytes(cleanupBytes)} can go`}</small>
            </button>
            ${groups.map(([group, entries]) => entries.length > 0 && html`
                <div class="config-group">${group}</div>
                ${entries.map((log) => html`
                    <button class="config-preset log-item ${log.path
                        === selectedPath ? 'active' : ''}"
                        title=${log.path} onClick=${() => onSelect(log.path)}>
                        <span class="mono">${log.label}</span>
                        <small>${formatAgo(log.modified_s, now)} ·
                            ${' ' + formatBytes(log.size_bytes)}</small>
                    </button>`)}`)}
        </nav>`;
}

function LogContent({ machineId, path, canDelete, onDeleted }) {
    const [log, setLog] = useState(null);
    const [error, setError] = useState(null);
    const [version, setVersion] = useState(0);
    const [isDeleting, setIsDeleting] = useState(false);

    async function remove() {
        if (!window.confirm(`Delete ${path}?`)) return;
        setIsDeleting(true);
        try {
            await postJson(machinePath(machineId, 'logs/delete'), { path });
            onDeleted();
        } catch (failure) {
            window.alert(failure.message);
            setIsDeleting(false);
        }
    }

    useEffect(() => {
        setLog(null);
        setError(null);
        getJson(logUrl(machineId, path)).then(setLog)
            .catch((failure) => setError(failure.message));
    }, [machineId, path, version]);

    const lines = useMemo(() => log ? log.text.split('\n') : [], [log]);
    if (error) return html`<div class="error-banner">${error}</div>`;
    if (!log) return html`<div class="placeholder">Reading…</div>`;
    return html`
        <div class="process-header">
            <span class="mono muted">${path}</span>
            <span class="spacer"></span>
            ${log.truncated && html`<span class="faint">${`last ${
                formatBytes(READ_BYTES)} of ${formatBytes(log.size_bytes)}`}
                </span>`}
            <button class="button small" onClick=${() =>
                setVersion(version + 1)}>Reload</button>
            <a class="button small" href=${logUrl(machineId, path,
                '&download=1')}>Download</a>
            ${canDelete && html`<button class="button small danger"
                disabled=${isDeleting} onClick=${remove}>Delete</button>`}
        </div>
        <${LineView} lines=${lines} height="70vh" emptyText="Empty file." />`;
}

// What can be deleted on a machine, by kind, with sizes. Anything a
// running process uses stays (the agent checks again when deleting).
function CleanupPanel({ machineId, label, onCleaned, onScanned }) {
    const [scan, setScan] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(null);  // category id, or 'all'
    const [result, setResult] = useState(null);
    const [version, setVersion] = useState(0);

    useEffect(() => {
        setError(null);
        getJson(machinePath(machineId, 'cleanup'))
            .then((found) => {
                setScan(found);
                onScanned(found.categories.reduce(
                    (total, category) => total + category.bytes, 0));
            })
            .catch((failure) => setError(failure.message));
    }, [machineId, version]);

    async function clean(categories, key) {
        const chosen = scan.categories.filter((category) =>
            categories.includes(category.id) && category.count > 0);
        const bytes = chosen.reduce((total, category) =>
            total + category.bytes, 0);
        if (!window.confirm(`Delete ${chosen.map((category) =>
            category.label.toLowerCase()).join(', ')} on ${label} (${
            formatBytes(bytes)})?`)) {
            return;
        }
        setBusy(key);
        setResult(null);
        try {
            const done = await postJson(machinePath(machineId, 'cleanup'),
                { categories: chosen.map((category) => category.id) });
            const removed = Object.values(done.removed);
            setResult({
                count: removed.reduce((total, item) => total + item.count, 0),
                bytes: removed.reduce((total, item) => total + item.bytes, 0),
                errors: done.errors,
            });
            onCleaned();
        } catch (failure) {
            setError(failure.message);
        }
        setBusy(null);
        setVersion((value) => value + 1);
    }

    if (!scan) {
        return error ? html`<div class="error-banner">${error}</div>`
            : html`<div class="placeholder">Looking…</div>`;
    }
    const total = scan.categories.reduce((sum, category) =>
        sum + category.bytes, 0);
    const anything = scan.categories.some((category) => category.count > 0);
    return html`
        <div class="panel-title-row">
            <h2>Clean up ${label}</h2>
            <button class="button danger" disabled=${!anything || busy}
                onClick=${() => clean(scan.categories.map((category) =>
                    category.id), 'all')}>
                ${busy === 'all' ? 'Deleting…'
                    : `Delete all (${formatBytes(total)})`}</button>
        </div>
        <p class="muted">What piles up on this machine's disk. Launch overlays
            are removed when their launch ends; the rest stays until deleted
            here. Pick a log on the left to read or delete just that one.</p>
        <${DiskBar} disk=${scan.disk} />
        <table class="table cleanup-table">
            <tbody>${scan.categories.map((category) => html`
                <tr class=${category.count ? '' : 'faint'}>
                    <td><b>${category.label}</b>
                        <div class="small-text muted">${category.description}
                        </div></td>
                    <td class="numeric">${category.count
                        ? `${category.count} item${category.count === 1 ? ''
                            : 's'}` : 'nothing'}</td>
                    <td class="numeric">${category.count
                        ? formatBytes(category.bytes) : ''}</td>
                    <td class="actions"><button class="button small danger"
                        disabled=${!category.count || busy}
                        onClick=${() => clean([category.id], category.id)}>
                        ${busy === category.id ? 'Deleting…' : 'Delete'}
                        </button></td>
                </tr>`)}</tbody>
        </table>
        ${result && html`<div class="ok-banner">Deleted ${result.count}
            item${result.count === 1 ? '' : 's'}, ${formatBytes(result.bytes)}
            freed.</div>`}
        ${result && result.errors.length > 0 && html`<div class="error-banner">
            Some couldn't be deleted:${' '}${result.errors.join('; ')}</div>`}
        ${error && html`<div class="error-banner">${error}</div>`}`;
}

export function LogsView({ state, selectedId, onSelect, now }) {
    const machines = state.machines;
    const current = machines.find((entry) => entry.machine.id === selectedId)
        || machines.find((entry) => entry.machine.is_local);
    const machineId = current.machine.id;
    const isOnline = current.connection.state === 'online';
    const [logs, setLogs] = useState(null);
    const [selectedPath, setSelectedPath] = useState(null);
    const [error, setError] = useState(null);
    const [listVersion, setListVersion] = useState(0);
    const [cleanupBytes, setCleanupBytes] = useState(null);

    useEffect(() => {
        setLogs(null);
        setSelectedPath(null);
        setCleanupBytes(null);
    }, [machineId]);

    useEffect(() => {
        setError(null);
        if (!isOnline) {
            setLogs(null);
            return;
        }
        getJson(machinePath(machineId, 'logs'))
            .then((result) => setLogs(result.logs))
            .catch((failure) => setError(failure.message));
    }, [machineId, isOnline, listVersion]);
    const reloadList = () => setListVersion((value) => value + 1);
    const selectedLog = logs && logs.find((log) => log.path === selectedPath);

    return html`
        <div class="page-head"><div>
            <h1>Logs</h1>
            <p>Log files on each machine, newest first, and cleaning up
                what piles up on its disk.</p>
        </div></div>
        <div class="machine-tabs">
            ${machines.map((entry) => html`
                <button class="process-tab ${entry === current ? 'active'
                    : ''}" onClick=${() => onSelect(entry.machine.id)}>
                    <${StatusDot} state=${entry.connection.state} />
                    <span><b>${entry.machine.label}</b></span>
                </button>`)}
        </div>
        ${!isOnline ? html`<div class="placeholder">${current.machine.label}
            is ${current.connection.state}.</div>`
        : error ? html`<div class="error-banner">${error}</div>`
        : !logs ? html`<div class="placeholder">Reading…</div>`
        : html`<div class="split">
            <${LogList} logs=${logs} selectedPath=${selectedPath}
                onSelect=${setSelectedPath} now=${now}
                cleanupBytes=${cleanupBytes} />
            <section class="panel">
                ${selectedPath
                    ? html`<${LogContent} key=${selectedPath}
                        machineId=${machineId} path=${selectedPath}
                        canDelete=${selectedLog
                            && DELETABLE_GROUPS.includes(selectedLog.group)}
                        onDeleted=${() => {
                            setSelectedPath(null);
                            reloadList();
                        }} />`
                    : html`<${CleanupPanel} key=${machineId}
                        machineId=${machineId} label=${current.machine.label}
                        onCleaned=${reloadList}
                        onScanned=${setCleanupBytes} />`}
            </section>
        </div>`}`;
}
