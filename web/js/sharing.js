// Internet sharing: a Linux machine shares its internet with devices plugged
// into one of its Ethernet ports (e.g. the robot in the pits), and lists the
// devices that show up there so they can be connected or added.

import { useState } from 'preact/hooks';
import { html, formatAgo, formatDuration, postJson, StatusDot }
    from './common.js';
import { addMachineDraft } from './inventory.js';
import { openTerminal } from './terminals.js';


function sharingPath(machineId, action) {
    return `/api/machines/${encodeURIComponent(machineId)}/sharing/${action}`;
}

function canShare(entry) {
    return entry.connection.state === 'online' && entry.sharing
        && entry.sharing.supported;
}


// --- Helper ------------------------------------------------------------------

function HelperNotice({ machineId, status }) {
    const [isChecking, setIsChecking] = useState(false);
    let message;
    if (!status.helper_installed) {
        message = 'Sharing needs a small helper installed on this machine '
            + 'once. It asks for your sudo password; after that, sharing '
            + 'starts and stops without one.';
    } else if (!status.helper_current) {
        message = "This machine's sharing helper is from an older version "
            + 'of the orchestrator. Update it (asks for your sudo password).';
    } else if (!status.sudo_ok) {
        message = "The sharing helper is installed but sudo won't run it "
            + 'without a password (the sudoers rule is missing). Install it '
            + 'again to add the rule.';
    } else if (!status.dnsmasq_installed) {
        message = 'dnsmasq (the DHCP server) is missing. Installing the '
            + 'helper again installs it.';
    } else {
        return null;
    }
    async function check() {
        setIsChecking(true);
        try {
            await postJson(sharingPath(machineId, 'refresh'));
        } finally {
            setIsChecking(false);
        }
    }
    return html`
        <div class="notice-row">
            <span>${message}</span>
            <span class="button-row">
                <button class="button primary small" onClick=${() =>
                    openTerminal(machineId, 'share-install')}>
                    ${status.helper_installed ? 'Update helper'
                        : 'Install helper'}</button>
                <button class="button small" disabled=${isChecking}
                    onClick=${check}>Check again</button>
            </span>
        </div>`;
}

function isReady(status) {
    return status.helper_installed && status.helper_current
        && status.sudo_ok && status.dnsmasq_installed;
}


// --- An active share ---------------------------------------------------------

function deviceSeen(device, now) {
    if (device.source === 'lease' && device.expires_s) {
        return `DHCP lease, ${formatDuration(device.expires_s - now)} left`;
    }
    return device.state ? `seen (${device.state})` : 'seen';
}

function DeviceRow({ device, machinesById, now }) {
    const owner = device.machine_id && machinesById[device.machine_id];
    return html`
        <tr>
            <td>${device.hostname || html`<span class="faint">unnamed</span>`}
                </td>
            <td class="mono">${device.address}</td>
            <td class="mono faint">${device.mac}</td>
            <td class="faint">${deviceSeen(device, now)}</td>
            <td>${owner
                ? html`<a class="machine-link" href="#/machines">
                    <${StatusDot} state=${owner.connection.state} />
                    ${owner.machine.label}</a>`
                : html`<button class="button small" onClick=${() =>
                    addMachineDraft({
                        id: '', label: device.hostname || '',
                        routes: [{ host: device.address }],
                    })}>Add machine</button>`}</td>
        </tr>`;
}

function SessionCard({ machineId, session, status, machinesById, now }) {
    const [error, setError] = useState(null);
    const [isStopping, setIsStopping] = useState(false);
    const prefix = session.subnet.split('/')[1];
    const upstream = session.upstream || (status.default_route || {})
        .interface;
    const level = !session.dnsmasq_running ? 'bad'
        : session.carrier ? 'good' : 'warn';
    const health = !session.dnsmasq_running
        ? 'DHCP server stopped: stop and start sharing again'
        : session.carrier ? 'cable connected' : 'no cable in this port';
    async function stop() {
        setIsStopping(true);
        setError(null);
        try {
            await postJson(sharingPath(machineId, 'down'),
                { name: session.name });
        } catch (failure) {
            setError(failure.message);
            setIsStopping(false);
        }
    }
    const devices = session.devices || [];
    return html`
        <div class="share-session">
            <div class="share-head">
                <span class="dot ${level}"></span>
                <div>
                    <b>Sharing on <span class="mono">${session.downstream}</span>
                        </b>
                    <div class="faint">${health} · this machine is
                        <span class="mono">${` ${session.server}/${prefix}`}
                        </span> · internet through
                        <span class="mono">${` ${upstream || 'nothing'}`}</span>
                        ${` · since ${formatAgo(session.started_s, now)}`}</div>
                </div>
                <span class="spacer"></span>
                <button class="button small danger" disabled=${isStopping}
                    onClick=${stop}>${isStopping ? 'Stopping…'
                        : 'Stop sharing'}</button>
            </div>
            ${error && html`<div class="error-banner">${error}</div>`}
            ${devices.length === 0
                ? html`<div class="console-note">No devices yet. Plug one in:
                    it gets an address within a few seconds.</div>`
                : html`<table class="table">
                    <thead><tr><th>Device</th><th>Address</th><th>MAC</th>
                        <th>Seen</th><th>Machine</th></tr></thead>
                    <tbody>${devices.map((device) => html`<${DeviceRow}
                        key=${device.mac} device=${device}
                        machinesById=${machinesById} now=${now} />`)}</tbody>
                </table>`}
        </div>`;
}


// --- Starting ------------------------------------------------------------------

function portLabel(port) {
    const notes = [port.carrier ? 'cable in' : 'no cable'];
    if (port.is_default_route) notes.push("this machine's internet");
    if (port.ipv4.length) notes.push(port.ipv4.join(', '));
    return `${port.name} (${notes.join(' · ')})`;
}

function defaultDownstream(ports, saved) {
    const usable = ports.filter((port) => port.kind === 'ethernet'
        && !port.is_default_route);
    if (saved && usable.some((port) => port.name === saved)) return saved;
    const plugged = usable.find((port) => port.carrier);
    return (plugged || usable[0] || {}).name || '';
}

function StartForm({ machineId, status, isReady: ready }) {
    const saved = status.options || {};
    const sharedPorts = new Set(status.sessions.map((session) =>
        session.downstream));
    const ports = status.ports.filter((port) => !sharedPorts.has(port.name));
    const [downstream, setDownstream] = useState(
        () => defaultDownstream(ports, saved.downstream));
    const [upstream, setUpstream] = useState(saved.upstream || '');
    const [subnet, setSubnet] = useState(saved.subnet || '');
    const [dns, setDns] = useState(saved.dns || '');
    const [error, setError] = useState(null);
    const [warnings, setWarnings] = useState([]);
    const [isStarting, setIsStarting] = useState(false);
    const route = status.default_route || {};

    async function start() {
        setIsStarting(true);
        setError(null);
        try {
            const result = await postJson(sharingPath(machineId, 'up'), {
                name: status.sessions.length ? `share-${downstream}`
                    .toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 20)
                    : 'share',
                downstream, upstream, dns,
                subnet: subnet || status.suggested_subnet,
            });
            setWarnings(result.warnings || []);
        } catch (failure) {
            setError(failure.message);
        }
        setIsStarting(false);
    }

    if (ports.every((port) => port.kind !== 'ethernet')) {
        return html`<div class="console-note">No free Ethernet port on this
            machine. Plug in a USB Ethernet adapter to share over it.</div>`;
    }
    return html`
        <div class="share-form">
            <div class="fields">
                <label class="field wide">
                    <span>Share over</span>
                    <select class="input" value=${downstream}
                        onChange=${(event) => setDownstream(event.target.value)}>
                        ${ports.filter((port) => port.kind === 'ethernet')
                            .map((port) => html`<option value=${port.name}
                                disabled=${port.is_default_route}>
                                ${portLabel(port)}</option>`)}
                    </select>
                </label>
                <label class="field">
                    <span>Internet from</span>
                    <select class="input" value=${upstream}
                        onChange=${(event) => setUpstream(event.target.value)}>
                        <option value="">${route.interface
                            ? `whatever has it (now ${route.interface})`
                            : 'whatever has it (nothing now)'}</option>
                        ${status.ports.filter((port) => port.name
                            !== downstream).map((port) => html`
                            <option value=${port.name}>${port.name}</option>`)}
                    </select>
                </label>
                <label class="field">
                    <span>Subnet</span>
                    <input class="input mono" value=${subnet}
                        placeholder=${status.suggested_subnet}
                        onInput=${(event) => setSubnet(event.target.value)} />
                </label>
                <label class="field">
                    <span>DNS servers</span>
                    <input class="input mono" value=${dns}
                        placeholder="8.8.8.8,1.1.1.1"
                        onInput=${(event) => setDns(event.target.value)} />
                </label>
            </div>
            ${!route.interface && html`<div class="warn-banner">This machine has
                no internet connection right now: devices will get addresses
                but no internet.</div>`}
            ${error && html`<div class="error-banner">${error}</div>`}
            ${warnings.map((warning) => html`<div class="warn-banner">
                ${warning}</div>`)}
            <div class="button-row">
                <button class="button primary" onClick=${start}
                    disabled=${!ready || !downstream || isStarting}>
                    ${isStarting ? 'Starting…' : 'Start sharing'}</button>
                <span class="faint">Devices on the port get addresses by DHCP
                    and this machine routes their traffic to the internet. Only
                    this session's settings change; a reboot undoes
                    everything.</span>
            </div>
        </div>`;
}


// --- Panel -------------------------------------------------------------------

export function SharingPanel({ state, now }) {
    const candidates = state.machines.filter(canShare);
    const active = candidates.find((entry) =>
        entry.sharing.sessions.length > 0);
    const [chosenId, setChosenId] = useState(null);
    const entry = candidates.find((other) => other.machine.id === chosenId)
        || active || candidates.find((other) => other.machine.is_local)
        || candidates[0];
    const machinesById = Object.fromEntries(state.machines.map((other) =>
        [other.machine.id, other]));
    const [showForm, setShowForm] = useState(false);
    if (!entry) {
        return html`
            <section class="panel">
                <h2>Internet sharing</h2>
                <p class="muted">Share a Linux machine's internet with devices
                    plugged into its Ethernet port. No Linux machine is online
                    right now.</p>
            </section>`;
    }
    const status = entry.sharing;
    const sessions = status.sessions;
    const ready = isReady(status);
    return html`
        <section class="panel">
            <div class="run-head">
                <div>
                    <h2>Internet sharing</h2>
                    <p class="muted">Share a machine's internet with devices
                        plugged into one of its Ethernet ports, e.g. the robot
                        in the pits: they get addresses and internet from it,
                        and show up here to connect or add.</p>
                </div>
                <label class="field sharing-machine">
                    <span>Machine</span>
                    <select class="input" value=${entry.machine.id}
                        onChange=${(event) => setChosenId(event.target.value)}>
                        ${candidates.map((other) => html`
                            <option value=${other.machine.id}>
                                ${other.machine.label}${other.sharing.sessions
                                    .length ? ' (sharing)' : ''}</option>`)}
                    </select>
                </label>
            </div>
            <${HelperNotice} machineId=${entry.machine.id} status=${status} />
            ${sessions.map((session) => html`<${SessionCard}
                key=${session.name} machineId=${entry.machine.id}
                session=${session} status=${status}
                machinesById=${machinesById} now=${now} />`)}
            ${sessions.length > 0 && !showForm
                ? html`<button class="link-button" onClick=${() =>
                    setShowForm(true)}>Share another port</button>`
                : html`<${StartForm} key=${entry.machine.id}
                    machineId=${entry.machine.id} status=${status}
                    isReady=${ready} />`}
        </section>`;
}
