// The machines page: one card per machine with its connection, load,
// network, workspace and (for a mac host) its VM.

import { useState } from 'preact/hooks';
import {
    html, formatBytes, formatDuration, formatAgo, formatMilliseconds,
    formatOffset, shortCommit, levelFor, getJson, postJson, StatusDot,
} from './common.js';


// --- Reference data ----------------------------------------------------------

// USB devices csm-vm passes to the VM (run-vm.sh USB_WHITELIST).
const KNOWN_USB_DEVICES = {
    '0x16d0:0x117e': 'CANable 2',
    '0x045e:0x0b12': 'Xbox controller',
    '0x0bda:0x8153': 'Realtek USB Ethernet',
    '0x0e8d:0x7961': 'Brostrend AXE3000 Wi-Fi',
};

const VM_NETWORK_NAMES = {
    'vmnet-bridged': 'bridged',
    'vmnet-host': 'host-only',
    'vmnet-shared': 'shared Wi-Fi',
};

const KIND_LABELS = {
    wifi: 'WIFI', ethernet: 'ETH', bridge: 'BR', can: 'CAN', virtual: 'VIRT',
};

function postReconnect(machineId) {
    fetch(`/api/machines/${encodeURIComponent(machineId)}/reconnect`,
        { method: 'POST' });
}


function Meter({ label, value, detail, percent, warnAt = 70, badAt = 90 }) {
    return html`
        <div class="meter">
            <div class="label">${label}</div>
            <div class="value">${value}</div>
            <div class="caption">${detail || '\u00a0'}</div>
            ${percent != null && html`
                <div class="bar"><div class=${levelFor(percent, warnAt, badAt)}
                    style=${`width: ${Math.min(100, percent)}%`}></div></div>`}
        </div>`;
}


// --- Machine card sections ---------------------------------------------------

function ConnectionRow({ connection, info }) {
    return html`
        <div class="connection-row">
            <span class="item"><span class="mono">${connection.route === 'local'
                ? 'this machine' : (connection.route || '–')}</span></span>
            ${connection.state === 'online' && html`
                <span class="item">latency <b>${formatMilliseconds(
                    connection.rtt_ms)}</b></span>
                <span class="item">clock <b>${formatOffset(
                    connection.clock_offset_ms)}</b></span>`}
            ${info && html`<span class="item faint">${
                `${info.os} · ${info.arch}`}</span>`}
            ${connection.update_pending && html`
                <span class="pill warn">agent update pending</span>`}
        </div>`;
}

function Meters({ stats, info }) {
    const memoryPercent = stats.memory_total_bytes
        ? 100 * stats.memory_used_bytes / stats.memory_total_bytes : null;
    const numCpus = info ? info.num_cpus : null;
    const load = stats.load_average ? stats.load_average[0] : null;
    return html`
        <div class="meters">
            <${Meter} label="CPU" percent=${stats.cpu_percent}
                value=${stats.cpu_percent == null ? '–'
                    : `${stats.cpu_percent.toFixed(0)}%`}
                detail=${numCpus && `${numCpus} cores`} />
            <${Meter} label="Memory" percent=${memoryPercent}
                value=${formatBytes(stats.memory_used_bytes)}
                detail=${`of ${formatBytes(stats.memory_total_bytes)}`} />
            <${Meter} label="Load" value=${load == null ? '–' : load.toFixed(2)}
                percent=${load != null && numCpus ? 100 * load / numCpus : null}
                detail=${stats.temperature_c != null
                    ? `${stats.temperature_c.toFixed(0)}°C` : '1 min avg'} />
            <${Meter} label="Uptime" value=${formatDuration(stats.uptime_s)}
                detail="since boot" />
        </div>`;
}

function wifiSummary(wifi) {
    if (!wifi) return 'reading Wi-Fi status…';
    if (!wifi.connected) return wifi.power === 'spairport_status_off'
        ? 'Wi-Fi off' : 'not connected';
    const parts = [];
    parts.push(wifi.ssid ? `“${wifi.ssid}”`
        : wifi.ssid_redacted ? 'network name hidden by macOS' : 'unknown network');
    if (wifi.channel != null) parts.push(`ch ${wifi.channel}`);
    if (wifi.band) parts.push(wifi.band.replace(/^\d+\s*\(?|\)$/g, ''));
    else if (wifi.frequency_mhz) parts.push(`${wifi.frequency_mhz} MHz`);
    if (wifi.signal_dbm != null) parts.push(`${wifi.signal_dbm} dBm`);
    if (wifi.signal_percent != null) parts.push(`${wifi.signal_percent}%`);
    if (wifi.phy_mode) parts.push(wifi.phy_mode);
    return parts.join(' · ');
}

function isInterfaceInteresting(entry) {
    return entry.is_up || entry.ipv4.length > 0 || entry.kind === 'wifi'
        || entry.kind === 'can';
}

function InterfaceRow({ entry, isDefault }) {
    return html`
        <div class="row ${entry.is_up ? '' : 'down'}">
            <span class="kind ${entry.kind}">${KIND_LABELS[entry.kind]
                || entry.kind}</span>
            <span class="name"><span class="mono">${entry.name}</span>
                ${entry.label && entry.label !== entry.name
                    && html`<small>${entry.label}</small>`}
                ${isDefault && html`<span class="tag">default</span>`}</span>
            <span class="detail mono">${entry.ipv4.length
                ? entry.ipv4.join(', ')
                : html`<span class="faint">${entry.is_up ? 'no IPv4'
                    : 'down'}</span>`}</span>
            ${entry.kind === 'wifi' && html`
                <div class="sub">${wifiSummary(entry.wifi)}</div>`}
        </div>`;
}

function NetworkSection({ stats }) {
    const [showAll, setShowAll] = useState(false);
    const interfaces = stats.interfaces || [];
    const shown = showAll ? interfaces
        : interfaces.filter(isInterfaceInteresting);
    const numHidden = interfaces.length - shown.length;
    const defaultInterface = stats.default_route
        && stats.default_route.interface;
    return html`
        <div class="section">
            <div class="section-head">
                <span>Network</span>
                ${(numHidden > 0 || showAll) && html`
                    <button class="link-button"
                        onClick=${() => setShowAll(!showAll)}>
                        ${showAll ? 'hide inactive' : `+${numHidden} inactive`}
                    </button>`}
            </div>
            <div class="rows">
                ${shown.map((entry) => html`<${InterfaceRow} key=${entry.name}
                    entry=${entry} isDefault=${entry.name === defaultInterface}
                    />`)}
            </div>
        </div>`;
}

function workspaceBadges(workspace, referenceHead, isReference) {
    const badges = [];
    if (isReference) {
        badges.push(html`<span class="pill accent">hub's copy</span>`);
    } else if (referenceHead && workspace.head === referenceHead) {
        badges.push(html`<span class="pill good">matches hub</span>`);
    } else if (referenceHead) {
        badges.push(html`<span class="pill warn"
            title=${`hub is at ${shortCommit(referenceHead)}`}>
            differs from hub</span>`);
    }
    if (workspace.num_changes > 0) {
        badges.push(html`<span class="pill warn">
            ${workspace.num_changes} uncommitted</span>`);
    }
    if (workspace.num_submodules_off > 0) {
        badges.push(html`<span class="pill warn">
            ${workspace.num_submodules_off} submodules off</span>`);
    }
    return badges;
}

function WorkspaceSection({ path, workspace, referenceHead, isReference, now }) {
    let body;
    if (!workspace) {
        body = html`<span class="faint">reading…</span>`;
    } else if (!workspace.exists) {
        body = html`<span class="pill bad">not found</span>
            <span class="muted mono"> ${path}</span>`;
    } else if (!workspace.is_git) {
        body = html`<span class="pill warn">not a git checkout</span>`;
    } else {
        body = html`
            <dl class="kv">
                <dt>Path</dt><dd class="mono">${path}</dd>
                <dt>Commit</dt>
                <dd><span class="mono">${workspace.branch} @ ${shortCommit(
                    workspace.head)}</span>
                    <span class="truncate muted">${workspace.subject}</span></dd>
                <dt>Built</dt>
                <dd>${workspace.has_install
                    ? formatAgo(workspace.last_build_s, now)
                    : html`<span class="pill warn">not built</span>`}</dd>
                <dt>Status</dt>
                <dd><div class="badges">${workspaceBadges(
                    workspace, referenceHead, isReference)}</div></dd>
            </dl>`;
    }
    return html`
        <div class="section">
            <div class="section-head"><span>Workspace</span></div>
            ${body}
        </div>`;
}

function VmSection({ vm, guestLabel }) {
    if (!vm) {
        return html`<div class="section"><div class="section-head">
            <span>Virtual machine</span></div>
            <span class="faint">reading…</span></div>`;
    }
    const networks = (vm.networks || []).map((network) =>
        (VM_NETWORK_NAMES[network.type] || network.type)
        + (network.interface ? ` → ${network.interface}` : ''));
    const autostart = vm.autostart || [];
    return html`
        <div class="section">
            <div class="section-head"><span>Virtual machine${guestLabel
                && ` · ${guestLabel}`}</span></div>
            <div class="vm-head">
                ${vm.is_running
                    ? html`<span class="pill good"><span class="dot good">
                        </span>Running</span>
                        <span class="muted">${[
                            `up ${formatDuration(vm.uptime_s)}`,
                            `${vm.cpus} CPUs`, vm.memory, `PID ${vm.pid}`,
                        ].join(' · ')}</span>`
                    : html`<span class="pill">Stopped</span>`}
            </div>
            <dl class="kv">
                ${vm.is_running && html`
                    <dt>Networks</dt><dd>${networks.join(', ') || '–'}</dd>
                    <dt>USB</dt>
                    <dd><div class="badges">${(vm.usb_devices || []).length
                        ? vm.usb_devices.map((device) => html`
                            <span class="pill" title=${device}>
                                ${KNOWN_USB_DEVICES[device] || device}</span>`)
                        : html`<span class="faint">none passed through</span>`}
                    </div></dd>`}
                <dt>Disks</dt>
                <dd>${[
                    `base ${formatBytes(vm.disks.base_bytes)}`,
                    `overlay ${formatBytes(vm.disks.overlay_bytes)}`,
                    vm.disks.backup_bytes != null
                        ? `backup ${formatBytes(vm.disks.backup_bytes)}`
                        : 'no backup',
                ].join(' · ')}</dd>
                <dt>Autostart</dt>
                <dd>${autostart.length
                    ? autostart.map((entry) => html`<span class="mono">${
                        entry.label}</span>${' '}<span class="faint">${
                        entry.arguments.slice(1).join(' ')}</span>`)
                    : html`<span class="muted">not installed</span>`}</dd>
            </dl>
        </div>`;
}


// --- Machine card ------------------------------------------------------------

// First contact with a machine: SSH refuses an unknown host key. Show the
// key's fingerprints so they can be compared (e.g. with `ssh-keygen -lf
// /etc/ssh/ssh_host_ed25519_key.pub` on the machine) before trusting it.
function HostKeyPanel({ machineId, route }) {
    const [scan, setScan] = useState(null);
    const [error, setError] = useState(null);
    const [isBusy, setIsBusy] = useState(false);
    const path = `/api/machines/${encodeURIComponent(machineId)}/host-key`;

    async function check() {
        setIsBusy(true);
        setError(null);
        try {
            setScan(await getJson(path));
        } catch (failure) {
            setError(failure.message);
        }
        setIsBusy(false);
    }

    async function trust() {
        setIsBusy(true);
        try {
            await postJson(path, { fingerprints: scan.fingerprints });
            setScan(null);
        } catch (failure) {
            setError(failure.message);
        }
        setIsBusy(false);
    }

    return html`
        <div class="host-key">
            <div><b>Unknown host key</b> on <span class="mono">${route}</span>.
                SSH won't connect until this machine's key is trusted.</div>
            ${!scan && html`<button class="button" disabled=${isBusy}
                onClick=${check}>${isBusy ? 'Reading key…'
                    : 'Show fingerprint'}</button>`}
            ${scan && html`
                <div class="fingerprints mono">${scan.fingerprints.map(
                    (line) => html`<div>${line}</div>`)}</div>
                <div class="muted">Saved to ~/.ssh/known_hosts as
                    <span class="mono">${' ' + scan.name}</span></div>
                <div class="button-row">
                    <button class="button primary" disabled=${isBusy}
                        onClick=${trust}>Trust this key</button>
                    <button class="button" onClick=${() => setScan(null)}>
                        Cancel</button>
                </div>`}
            ${error && html`<div class="error-text">${error}</div>`}
        </div>`;
}

function OfflineNotice({ machineId, connection, now }) {
    if (connection.state === 'online' || !connection.error) return null;
    const retryIn = connection.retry_at_s
        ? Math.max(0, Math.ceil(connection.retry_at_s - now)) : null;
    return html`
        <div class="error-banner" title=${connection.error_details || ''}>
            ${connection.error}
            ${connection.state === 'offline' && retryIn != null && html`
                <span class="retry">retrying in ${retryIn}s</span>`}
        </div>
        ${connection.unknown_host_key && html`<${HostKeyPanel}
            machineId=${machineId} route=${connection.unknown_host_key} />`}`;
}

function MachineCard({ entry, machinesById, referenceHead, now }) {
    const { machine, connection, info, stats, workspace, vm } = entry;
    const isOnline = connection.state === 'online';
    const hostEntry = machine.host && machinesById[machine.host];
    const guestEntry = machine.vm && machine.vm.guest
        && machinesById[machine.vm.guest];
    return html`
        <article class="card ${isOnline ? '' : 'offline'}">
            <header class="card-head">
                <${StatusDot} state=${connection.state} />
                <div class="title">
                    <h3>${machine.label}
                        <span class="machine-id mono">${machine.id}</span></h3>
                    <p>${machine.role || (hostEntry
                        ? `VM on ${hostEntry.machine.label}` : '')}</p>
                </div>
                ${!isOnline && connection.state !== 'connecting' && html`
                    <button class="icon-button"
                        onClick=${() => postReconnect(machine.id)}>
                        Retry</button>`}
                ${isOnline && html`<span class="pill">${connection.state}
                    </span>`}
            </header>
            <${ConnectionRow} connection=${connection} info=${info} />
            <${OfflineNotice} machineId=${machine.id} connection=${connection}
                now=${now} />
            ${!stats && !isOnline ? null : html`<${CardBody} entry=${entry}
                guestEntry=${guestEntry} referenceHead=${referenceHead}
                now=${now} />`}
        </article>`;
}

function CardBody({ entry, guestEntry, referenceHead, now }) {
    const { machine, connection, info, stats, workspace, vm } = entry;
    const isOnline = connection.state === 'online';
    return html`
            ${stats && html`<${Meters} stats=${stats} info=${info} />`}
            ${stats && html`<${NetworkSection} stats=${stats} />`}
            ${machine.workspace && html`<${WorkspaceSection}
                path=${machine.workspace} workspace=${workspace}
                referenceHead=${referenceHead} isReference=${machine.is_local}
                now=${now} />`}
            ${machine.vm && isOnline && html`<${VmSection} vm=${vm}
                guestLabel=${guestEntry && guestEntry.machine.label} />`}`;
}


// --- Machines view -----------------------------------------------------------

// Pair each VM host with its guest so they render side by side.
function groupMachines(entries, machinesById) {
    const groups = [];
    const placed = new Set();
    for (const entry of entries) {
        const machine = entry.machine;
        if (placed.has(machine.id)) continue;
        if (machine.host && machinesById[machine.host]
                && !placed.has(machine.host)) {
            continue; // rendered with its host
        }
        const guestId = machine.vm && machine.vm.guest;
        const guest = guestId && machinesById[guestId];
        placed.add(machine.id);
        if (guest) {
            placed.add(guestId);
            groups.push([entry, guest]);
        } else {
            groups.push([entry]);
        }
    }
    // Guests whose host comes later in the list were skipped above.
    for (const entry of entries) {
        if (!placed.has(entry.machine.id)) groups.push([entry]);
    }
    // Reachable machines first; the rest keep the inventory order.
    const isOnline = (group) => group.some((entry) =>
        entry.connection.state === 'online');
    return [...groups.filter(isOnline),
        ...groups.filter((group) => !isOnline(group))];
}

function Summary({ machines }) {
    const numOnline = machines.filter((entry) =>
        entry.connection.state === 'online').length;
    const numVmsRunning = machines.filter((entry) =>
        entry.vm && entry.vm.is_running).length;
    return html`
        <div class="summary">
            <div class="stat"><b>${numOnline} / ${machines.length}</b>
                <span>machines online</span></div>
            <div class="stat"><b>${numVmsRunning}</b>
                <span>VMs running</span></div>
        </div>`;
}

export function MachinesView({ state, now }) {
    const machinesById = Object.fromEntries(
        state.machines.map((entry) => [entry.machine.id, entry]));
    const local = machinesById.local;
    const referenceHead = local && local.workspace && local.workspace.head;
    const groups = groupMachines(state.machines, machinesById);
    const card = (entry) => html`<${MachineCard} key=${entry.machine.id}
        entry=${entry} machinesById=${machinesById}
        referenceHead=${referenceHead} now=${now} />`;
    return html`
        <div class="page-head">
            <div>
                <h1>Machines</h1>
                <p>Every machine in the inventory, live over SSH.</p>
            </div>
            <${Summary} machines=${state.machines} />
        </div>
        <div class="machine-grid">
            ${groups.map((group) => group.length === 1 ? card(group[0])
                : html`<div class="group" key=${group[0].machine.id}>
                    <div class="group-label">${
                        `${group[0].machine.label} + ${group[1].machine.label}`
                    }</div>
                    ${group.map(card)}
                </div>`)}
        </div>
        <footer>
            ${state.hub.inventory_paths.map((entry) => html`
                <span>inventory <span class="mono">${entry.path}</span>
                    ${entry.exists ? '' : html`<span class="faint">${
                        ' (not found)'}</span>`}</span>`)}
            <span>agent <span class="mono">${state.hub.agent_hash}</span></span>
        </footer>`;
}

