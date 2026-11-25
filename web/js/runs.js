// The runs page: everything Orca has launched, with live output.

import { useState } from 'preact/hooks';
import { html, formatDuration, formatAgo, getJson, postJson }
    from './common.js';
import { Console } from './console.js';
import { outputStore } from './state.js';


const RUN_STATE_LEVELS = {
    starting: 'warn', running: 'good', stopping: 'warn', finished: '',
    failed: 'bad',
};

function processState(entry) {
    return entry.record ? entry.record.state : 'pending';
}

function processLevel(entry) {
    const record = entry.record;
    if (!record) return '';
    if (record.state === 'running') return 'good';
    if (record.state === 'exited') {
        return record.exit_code === 0 ? '' : 'bad';
    }
    return 'warn';
}

function exitText(record) {
    if (!record || record.state !== 'exited') return null;
    if (record.exit_signal) return `killed (${record.exit_signal})`;
    return `exit ${record.exit_code}`;
}

export function sortedRuns(runs) {
    return [...runs].sort((a, b) => b.started_s - a.started_s);
}

export function isRunActive(run) {
    return ['starting', 'running', 'stopping'].includes(run.state);
}


// --- Run list ----------------------------------------------------------------

function RunList({ runs, selectedId, onSelect, now }) {
    return html`
        <nav class="scenario-list">
            ${runs.map((run) => html`
                <button class="scenario-item ${run.run_id === selectedId
                    ? 'active' : ''}" onClick=${() => onSelect(run.run_id)}>
                    <b><span class="dot ${RUN_STATE_LEVELS[run.state] || ''}
                        ${isRunActive(run) && run.state !== 'running'
                            ? 'pulse' : ''}"></span> ${run.label}</b>
                    <span>${run.state} · ${formatAgo(run.started_s, now)}</span>
                    <span class="roles">${run.processes.map((entry) =>
                        `${entry.label} @ ${entry.machine_id}`).join(' · ')}
                    </span>
                </button>`)}
        </nav>`;
}


// --- Run detail --------------------------------------------------------------

function ProcessTab({ entry, isActive, onSelect, now }) {
    const record = entry.record;
    const uptime = record && record.started_s
        ? (record.ended_s || now) - record.started_s : null;
    return html`
        <button class="process-tab ${isActive ? 'active' : ''}"
            onClick=${onSelect}>
            <span class="dot ${processLevel(entry)}"></span>
            <span><b>${entry.label}</b>
                <small>${entry.machine_id} · ${exitText(record)
                    || `${processState(entry)}${uptime != null
                        ? ` ${formatDuration(uptime)}` : ''}`}</small></span>
        </button>`;
}

function ProcessHeader({ entry }) {
    const record = entry.record;
    async function stop() {
        await postJson(`/api/machines/${encodeURIComponent(entry.machine_id)}`
            + `/processes/${encodeURIComponent(entry.process_id)}/stop`);
    }
    return html`
        <div class="process-header">
            <span class="mono muted">${record
                ? record.argv.join(' ') : 'not started'}</span>
            <span class="spacer"></span>
            ${record && record.pid && html`<span class="faint">
                ${`PID ${record.pid}`}</span>`}
            ${record && ['running', 'stopping'].includes(record.state)
                && html`<button class="button small" onClick=${stop}>
                    ${record.state === 'stopping' ? 'Force stop' : 'Stop'}
                    </button>`}
        </div>`;
}

// In competition mode other machines don't stream output (to save the
// robot's link); fetch it when needed.
function PausedNotice({ entry }) {
    const [isFetching, setIsFetching] = useState(false);
    async function fetchOutput() {
        setIsFetching(true);
        try {
            const output = await getJson(`/api/machines/${encodeURIComponent(
                entry.machine_id)}/processes/${encodeURIComponent(
                entry.process_id)}/output?from=0&refresh=1`);
            outputStore.merge(entry.machine_id, entry.process_id,
                output.first_line_number, output.lines);
        } finally {
            setIsFetching(false);
        }
    }
    return html`<div class="paused-notice">Live output paused (competition
        mode).
        <button class="button small" disabled=${isFetching}
            onClick=${fetchOutput}>${isFetching ? 'Fetching…'
                : 'Fetch output now'}</button></div>`;
}

function RunDetail({ run, now, isCompetition }) {
    const [selectedKey, setSelectedKey] = useState(null);
    const [sideBySide, setSideBySide] = useState(run.processes.length > 1);
    const processes = run.processes;
    const selected = processes.find((entry) => entry.key === selectedKey)
        || processes[0];

    async function stop() {
        await postJson(`/api/runs/${run.run_id}/stop`);
    }
    async function dismiss() {
        await postJson(`/api/runs/${run.run_id}/dismiss`);
    }

    const consoleFor = (entry) => html`
        <div class="process-pane" key=${entry.process_id}>
            ${sideBySide && html`<div class="pane-title"><span class="dot ${
                processLevel(entry)}"></span><b>${entry.label}</b>
                <span class="faint">${entry.machine_id}</span>
                ${exitText(entry.record) && html`<span class="pill">${
                    exitText(entry.record)}</span>`}</div>`}
            <${ProcessHeader} entry=${entry} />
            ${isCompetition && entry.machine_id !== 'local' && entry.record
                && html`<${PausedNotice} entry=${entry} />`}
            ${entry.record
                ? html`<${Console} machineId=${entry.machine_id}
                    processId=${entry.process_id}
                    height=${sideBySide ? '62vh' : '66vh'} />`
                : html`<div class="console-note">Waiting to start…</div>`}
        </div>`;

    return html`
        <section class="panel run-panel">
            <div class="run-head">
                <div>
                    <h2>${run.label}</h2>
                    <span class="pill ${RUN_STATE_LEVELS[run.state] || ''}">
                        ${run.state}</span>
                    <span class="muted">${` started ${formatAgo(
                        run.started_s, now)} · ${run.run_id}`}</span>
                </div>
                <div class="button-row">
                    ${processes.length > 1 && html`<button class="button"
                        onClick=${() => setSideBySide(!sideBySide)}>
                        ${sideBySide ? 'Tabs' : 'Side by side'}</button>`}
                    ${isRunActive(run)
                        ? html`<button class="button danger" onClick=${stop}
                            disabled=${run.state === 'stopping'}>
                            Stop all</button>`
                        : html`<button class="button" onClick=${dismiss}>
                            Dismiss</button>`}
                </div>
            </div>
            ${run.error && html`<div class="error-banner">${run.error}</div>`}
            ${!sideBySide && html`<div class="process-tabs">
                ${processes.map((entry) => html`<${ProcessTab}
                    key=${entry.key} entry=${entry} now=${now}
                    isActive=${entry === selected}
                    onSelect=${() => setSelectedKey(entry.key)} />`)}</div>`}
            <div class=${sideBySide ? 'side-by-side' : ''}>
                ${sideBySide ? processes.map(consoleFor)
                    : selected && consoleFor(selected)}
            </div>
        </section>`;
}


// --- Page --------------------------------------------------------------------

export function RunsView({ state, selectedId, onSelect, now }) {
    const runs = sortedRuns(state.runs);
    const run = runs.find((entry) => entry.run_id === selectedId) || runs[0];
    return html`
        <div class="page-head"><div>
            <h1>Runs</h1>
            <p>Processes keep running if Orca disconnects; reopen
                Orca to see them again.</p>
        </div></div>
        ${runs.length === 0
            ? html`<div class="placeholder">Nothing launched yet.</div>`
            : html`<div class="split">
                <${RunList} runs=${runs} selectedId=${run && run.run_id}
                    onSelect=${onSelect} now=${now} />
                ${run && html`<${RunDetail} key=${run.run_id} run=${run}
                    now=${now} isCompetition=${state.settings
                        .competition_mode} />`}
            </div>`}`;
}
