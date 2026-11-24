// The logs page: log files on each machine (hub runs, the latest build, ROS
// logs, the agent, the VM's autostart log), viewed with the same filters as
// live output.

import { useEffect, useMemo, useState } from 'preact/hooks';
import { html, formatAgo, formatBytes, getJson, StatusDot } from './common.js';
import { LineView } from './console.js';


const GROUP_ORDER = ['Hub runs', 'Latest build', 'ROS', 'Agent', 'VM'];
const READ_BYTES = 2 * 1024 * 1024;

function logUrl(machineId, path, extra = '') {
    return `/api/machines/${encodeURIComponent(machineId)}/logs/read?path=`
        + `${encodeURIComponent(path)}&max_bytes=${READ_BYTES}${extra}`;
}

function LogList({ logs, selectedPath, onSelect, now }) {
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

function LogContent({ machineId, path }) {
    const [log, setLog] = useState(null);
    const [error, setError] = useState(null);
    const [version, setVersion] = useState(0);

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
        </div>
        <${LineView} lines=${lines} height="70vh" emptyText="Empty file." />`;
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

    useEffect(() => {
        setLogs(null);
        setSelectedPath(null);
        setError(null);
        if (!isOnline) return;
        getJson(`/api/machines/${machineId}/logs`)
            .then((result) => setLogs(result.logs))
            .catch((failure) => setError(failure.message));
    }, [machineId, isOnline]);

    return html`
        <div class="page-head"><div>
            <h1>Logs</h1>
            <p>Log files on each machine, newest first.</p>
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
                onSelect=${setSelectedPath} now=${now} />
            <section class="panel">
                ${selectedPath
                    ? html`<${LogContent} key=${selectedPath}
                        machineId=${machineId} path=${selectedPath} />`
                    : html`<div class="placeholder">Choose a log.</div>`}
            </section>
        </div>`}`;
}
