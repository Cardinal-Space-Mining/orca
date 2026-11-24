// The bags page: recordings in each machine's bag_recordings/, pulled into
// this workspace's bag_recordings/ (what `sync.sh pull --bags` did).

import { useEffect, useState } from 'preact/hooks';
import { html, formatBytes, formatDuration, getJson, postJson, StatusDot }
    from './common.js';


const PULLED_LABELS = {
    yes: html`<span class="pill good">pulled</span>`,
    partial: html`<span class="pill warn">partly pulled</span>`,
    no: html`<span class="pill">not pulled</span>`,
    here: html`<span class="pill">this machine</span>`,
};

function formatStart(seconds) {
    if (!seconds) return '–';
    return new Date(seconds * 1000).toLocaleString([], {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit' });
}

function DiskBar({ disk }) {
    if (!disk) return null;
    const used = disk.total_bytes - disk.free_bytes;
    const percent = 100 * used / disk.total_bytes;
    const level = percent > 90 ? 'bad' : percent > 75 ? 'warn' : '';
    return html`
        <div class="disk">
            <span class="muted">Disk</span>
            <div class="bar wide"><div class=${level}
                style=${`width: ${percent}%`}></div></div>
            <span>${formatBytes(disk.free_bytes)} free of
                ${' ' + formatBytes(disk.total_bytes)}</span>
        </div>`;
}

function BagTable({ machineId, listing, onChanged, onLaunched }) {
    const [selected, setSelected] = useState({});
    const [error, setError] = useState(null);
    const bags = [...listing.bags].sort((a, b) =>
        (b.start_s || b.modified_s) - (a.start_s || a.modified_s));
    const isLocal = bags.some((bag) => bag.pulled === 'here')
        || listing.is_local;
    const chosen = bags.filter((bag) => selected[bag.name]);

    async function pull() {
        try {
            const run = await postJson(`/api/machines/${machineId}/bags/pull`,
                { names: chosen.map((bag) => bag.name) });
            onLaunched(run.run_id);
        } catch (failure) {
            setError(failure.message);
        }
    }

    async function remove(bag) {
        const message = `Delete ${bag.name} (${formatBytes(bag.size_bytes)}) `
            + `from ${machineId}? A complete copy is on this machine.`;
        if (!window.confirm(message)) return;
        try {
            await postJson(`/api/machines/${machineId}/bags/delete`,
                { name: bag.name });
            onChanged();
        } catch (failure) {
            setError(failure.message);
        }
    }

    if (bags.length === 0) {
        return html`<div class="placeholder">No bags in
            <span class="mono">${' ' + listing.directory}</span></div>`;
    }
    return html`
        ${error && html`<div class="error-banner">${error}</div>`}
        <table class="table">
            <thead><tr>
                <th></th><th>Bag</th><th>Started</th><th>Length</th>
                <th>Size</th><th>Messages</th><th>Topics</th><th>Here</th>
                <th></th>
            </tr></thead>
            <tbody>${bags.map((bag) => html`
                <tr key=${bag.name}>
                    <td>${!isLocal && html`<input type="checkbox"
                        checked=${!!selected[bag.name]}
                        onChange=${() => setSelected((previous) => ({
                            ...previous, [bag.name]: !previous[bag.name] }))}
                        />`}</td>
                    <td class="mono">${bag.name}</td>
                    <td>${formatStart(bag.start_s)}</td>
                    <td>${bag.duration_s != null
                        ? formatDuration(bag.duration_s) : '–'}</td>
                    <td>${formatBytes(bag.size_bytes)}</td>
                    <td>${bag.message_count != null
                        ? bag.message_count.toLocaleString() : '–'}</td>
                    <td title=${bag.topics.join('\n')}>${bag.topics.length}</td>
                    <td>${PULLED_LABELS[bag.pulled]}</td>
                    <td>${bag.pulled === 'yes' && html`<button
                        class="button small danger" onClick=${() =>
                            remove(bag)}>Delete there</button>`}</td>
                </tr>`)}</tbody>
        </table>
        ${!isLocal && html`<div class="button-row">
            <button class="button primary" disabled=${chosen.length === 0}
                onClick=${pull}>${chosen.length
                    ? `Pull ${chosen.length} bag${chosen.length > 1 ? 's' : ''}`
                    + ` (${formatBytes(chosen.reduce((total, bag) =>
                        total + bag.size_bytes, 0))})`
                    : 'Choose bags to pull'}</button>
            <span class="faint">Into this workspace's bag_recordings/; an
                interrupted pull resumes where it stopped.</span>
        </div>`}`;
}

export function BagsView({ state, selectedId, onSelect, onLaunched }) {
    const machines = state.machines.filter((entry) => entry.machine.workspace);
    const current = machines.find((entry) => entry.machine.id === selectedId)
        || machines.find((entry) => !entry.machine.is_local
            && entry.connection.state === 'online')
        || machines[0];
    const [listing, setListing] = useState(null);
    const [error, setError] = useState(null);
    const [version, setVersion] = useState(0);
    const machineId = current && current.machine.id;
    const isOnline = current && current.connection.state === 'online';
    // Refresh when a run finishes (e.g. a pull).
    const numFinished = state.runs.filter((run) =>
        run.state === 'finished').length;

    useEffect(() => {
        setError(null);
        if (!machineId || !isOnline) return;
        getJson(`/api/machines/${machineId}/bags`)
            .then((result) => setListing({ ...result, machineId,
                is_local: current.machine.is_local }))
            .catch((failure) => setError(failure.message));
    }, [machineId, isOnline, version, numFinished]);

    return html`
        <div class="page-head"><div>
            <h1>Bags</h1>
            <p>Recordings in each machine's <span class="mono">
                bag_recordings/</span> (robot and client presets record
                there).</p>
        </div></div>
        <div class="machine-tabs">
            ${machines.map((entry) => html`
                <button class="process-tab ${entry === current ? 'active'
                    : ''}" onClick=${() => onSelect(entry.machine.id)}>
                    <${StatusDot} state=${entry.connection.state} />
                    <span><b>${entry.machine.label}</b></span>
                </button>`)}
        </div>
        <section class="panel">
            ${!isOnline ? html`<div class="placeholder">${current
                ? `${current.machine.label} is ${current.connection.state}.`
                : 'No machines with a workspace.'}</div>`
            : error ? html`<div class="error-banner">${error}</div>`
            : !listing || listing.machineId !== machineId
                ? html`<div class="placeholder">Reading bags…</div>`
            : html`
                <${DiskBar} disk=${listing.disk} />
                <${BagTable} machineId=${machineId} listing=${listing}
                    onChanged=${() => setVersion(version + 1)}
                    onLaunched=${onLaunched} />`}
        </section>`;
}
