// The machines page: one card per machine with its connection, load,
// network, workspace and (for a mac host) its VM.

import { useState } from 'preact/hooks';
import {
    html, formatBytes, formatDuration, formatAgo, formatMilliseconds,
    formatOffset, shortCommit, levelFor, getJson, postJson, StatusDot,
} from './common.js';
import { openTerminal } from './terminals.js';


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

// `reference` is the code fingerprint of the orchestrator's own workspace.
function workspaceBadges(workspace, reference, isReference) {
    const badges = [];
    if (isReference) {
        badges.push(html`<span class="pill accent">
            orchestrator's copy</span>`);
    } else if (reference && workspace.fingerprint === reference) {
        badges.push(html`<span class="pill good">
            same code as orchestrator</span>`);
    } else if (reference) {
        badges.push(html`<span class="pill warn" title="Deploy from the
            Deploy page to make it match">different code</span>`);
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

function WorkspaceSection({ path, workspace, reference, isReference, now }) {
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
                    workspace, reference, isReference)}</div></dd>
            </dl>`;
    }
    return html`
        <div class="section">
            <div class="section-head"><span>Workspace</span></div>
            ${body}
        </div>`;
}

// --- VM controls -------------------------------------------------------------

function showRun(runId) {
    window.location.hash = `#/runs/${runId}`;
}

// Start or restart: what to do with an overlay a previous session left
// behind (it holds changes made in the VM since that session started).
function StartForm({ vm, action, onSubmit, onCancel }) {
    const hasOverlay = vm.disks.overlay_bytes != null && action === 'start';
    const [overlay, setOverlay] = useState('reuse');
    const [wifi, setWifi] = useState(false);
    const choices = [
        ['reuse', 'Reuse it', 'continue with the changes made in the VM'],
        ['commit', 'Commit it', 'save the changes into the base disk first'],
        ['discard', 'Discard it', 'start from the base disk; changes are lost'],
    ];
    return html`
        <div class="vm-form">
            ${hasOverlay && html`
                <div class="muted">${`An overlay from a previous session is
                    there (${formatBytes(vm.disks.overlay_bytes)}):`}</div>
                ${choices.map(([value, label, detail]) => html`
                    <label class="check"><input type="radio" name="overlay"
                        checked=${overlay === value}
                        onChange=${() => setOverlay(value)} />
                        <b>${label}</b><span class="faint">${detail}</span>
                    </label>`)}`}
            <label class="check"><input type="checkbox" checked=${wifi}
                onChange=${(event) => setWifi(event.target.checked)} />
                Share the mac's Wi-Fi with the VM
                <span class="faint">(--wifi)</span></label>
            <div class="button-row">
                <button class="button primary"
                    onClick=${() => onSubmit({ overlay, wifi })}>
                    ${action === 'start' ? 'Start' : 'Restart'}</button>
                <button class="button" onClick=${onCancel}>Cancel</button>
            </div>
        </div>`;
}

const CONFIRMATIONS = {
    kill: 'Force-stop the VM? Like pulling its power cord: unsaved work in '
        + 'the VM is lost, and the overlay is not committed.',
    restore: 'Restore the base disk from the backup? Everything since the '
        + 'backup is lost, and the current overlay is deleted.',
    discard: 'Discard the overlay? Changes made in the VM since the last '
        + 'commit are lost.',
};

function VmActions({ machineId, vm, vmRun }) {
    const [form, setForm] = useState(null);  // 'start' | 'restart' | null
    const [error, setError] = useState(null);
    const isBusy = vmRun && ['starting', 'running'].includes(vmRun.state);

    async function act(action, options = {}) {
        if (CONFIRMATIONS[action] && !window.confirm(CONFIRMATIONS[action])) {
            return;
        }
        if (options.overlay === 'discard'
                && !window.confirm(CONFIRMATIONS.discard)) {
            return;
        }
        setForm(null);
        setError(null);
        try {
            const run = await postJson(
                `/api/machines/${encodeURIComponent(machineId)}/vm/${action}`,
                options);
            showRun(run.run_id);
        } catch (failure) {
            setError(failure.message);
        }
    }

    const button = (label, onClick, extra = '') => html`<button
        class="button small ${extra}" disabled=${isBusy}
        onClick=${onClick}>${label}</button>`;
    const hasSerial = vm.is_running && vm.serial_socket;
    return html`
        <div class="vm-actions">
            ${vm.is_running ? html`
                <button class="button small primary" disabled=${!hasSerial}
                    title=${hasSerial ? 'Log in to the VM without SSH'
                        : 'Started without a serial console: restart it from '
                        + 'here to get one'}
                    onClick=${() => openTerminal(machineId, 'serial')}>
                    Serial console</button>
                ${button('Restart…', () => setForm('restart'))}
                ${button('Shut down', () => act('stop'))}
                ${button('Force stop', () => act('kill'), 'danger')}`
            : html`
                ${button('Start…', () => setForm('start'), 'primary')}
                ${vm.disks.overlay_bytes != null
                    && button('Commit overlay', () => act('commit'))}
                ${button('Back up disk', () => act('backup'))}
                ${vm.disks.backup_bytes != null
                    && button('Restore backup', () => act('restore'),
                        'danger')}`}
            ${vmRun && html`<button class="link-button"
                onClick=${() => showRun(vmRun.run_id)}>${isBusy
                    ? `${vmRun.label}…` : `last: ${vmRun.label} (${
                        vmRun.state})`}</button>`}
        </div>
        ${form && html`<${StartForm} vm=${vm} action=${form}
            onSubmit=${(options) => act(form, options)}
            onCancel=${() => setForm(null)} />`}
        ${error && html`<div class="error-text">${error}</div>`}`;
}

function VmSection({ machineId, vm, vmRun, guestLabel }) {
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
            <${VmActions} machineId=${machineId} vm=${vm} vmRun=${vmRun} />
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

// --- Setup checklist ---------------------------------------------------------

const SETUP_MARKS = { ok: '✓', todo: '!', optional: '·' };

function SetupSection({ machineId, setup }) {
    const checks = (setup && setup.checks) || [];
    const todo = checks.filter((check) => check.state === 'todo');
    const [isOpen, setIsOpen] = useState(null);
    const [isChecking, setIsChecking] = useState(false);
    if (checks.length === 0) return null;
    const open = isOpen === null ? todo.length > 0 : isOpen;
    async function recheck() {
        setIsChecking(true);
        try {
            await postJson(`/api/machines/${encodeURIComponent(machineId)}`
                + '/setup/refresh');
        } finally {
            setIsChecking(false);
        }
    }
    return html`
        <div class="section-head">
            <span>Setup</span>
            <span class="pill ${todo.length ? 'warn' : 'good'}">${todo.length
                ? `${todo.length} to do` : 'ready'}</span>
            <span class="spacer"></span>
            <button class="link-button" onClick=${() => setIsOpen(!open)}>
                ${open ? 'Hide' : 'Show'}</button>
        </div>
        ${open && html`<ul class="setup-list">
            ${checks.map((check) => html`<li class=${check.state}>
                <span class="setup-mark">${SETUP_MARKS[check.state]}</span>
                <span class="setup-text"><b>${check.title}</b>
                    <small>${check.detail}</small></span>
                ${check.state !== 'ok' && check.fix && (
                    check.fix.kind === 'link'
                    ? html`<a class="button small" href=${check.fix.href}>
                        ${check.fix.label}</a>`
                    : html`<button class="button small" onClick=${() =>
                        openTerminal(machineId, 'setup',
                            { check: check.id })}>Fix</button>`)}
            </li>`)}
            <li class="setup-footer">
                <span class="faint">Fixes run in a terminal on the machine,
                    where sudo asks for the password.</span>
                <button class="link-button" disabled=${isChecking}
                    onClick=${recheck}>${isChecking ? 'Checking…'
                        : 'Check again'}</button>
            </li>
        </ul>`}`;
}

// Where an offline machine was last seen: these addresses are tried along
// with the inventory's routes.
function LastSeen({ addresses, now }) {
    if (!addresses || addresses.length === 0) return null;
    return html`
        <div class="last-seen">
            <span class="faint">Last seen at</span>
            ${addresses.slice(0, 4).map((address) => html`
                <span class="chip mono" title=${`${address.interface}`
                    + `${address.mac ? ` · ${address.mac}` : ''}`}>
                    ${address.address}${' '}
                    <span class="faint">${address.kind === 'wifi'
                        ? 'Wi-Fi' : 'Ethernet'}, ${formatAgo(
                        address.seen_s, now)}</span></span>`)}
        </div>`;
}

export function MachineCard({ entry, machinesById, reference, now }) {
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
                ${isOnline && html`<button class="icon-button"
                    title="Open a shell on this machine"
                    onClick=${() => openTerminal(machine.id)}>Terminal</button>`}
            </header>
            <${ConnectionRow} connection=${connection} info=${info} />
            <${OfflineNotice} machineId=${machine.id} connection=${connection}
                now=${now} />
            ${!isOnline && html`<${LastSeen} addresses=${entry.addresses}
                now=${now} />`}
            ${!stats && !isOnline ? null : html`<${CardBody} entry=${entry}
                guestEntry=${guestEntry} reference=${reference}
                now=${now} />`}
        </article>`;
}

function CardBody({ entry, guestEntry, reference, now }) {
    const { machine, connection, info, stats, workspace, vm } = entry;
    const isOnline = connection.state === 'online';
    return html`
            ${stats && html`<${Meters} stats=${stats} info=${info} />`}
            ${stats && html`<${NetworkSection} stats=${stats} />`}
            ${machine.workspace && html`<${WorkspaceSection}
                path=${machine.workspace} workspace=${workspace}
                reference=${reference} isReference=${machine.is_local}
                now=${now} />`}
            ${machine.vm && isOnline && html`<${VmSection}
                machineId=${machine.id} vm=${vm} vmRun=${entry.vmRun}
                guestLabel=${guestEntry && guestEntry.machine.label} />`}
            ${isOnline && html`<${SetupSection} machineId=${machine.id}
                setup=${entry.setup} />`}`;
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

export function Summary({ machines }) {
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

// The newest VM action run per host machine.
export function latestVmRuns(runs) {
    const latest = {};
    for (const run of runs) {
        if (run.kind !== 'vm') continue;
        const current = latest[run.scenario_id];
        if (!current || run.started_s > current.started_s) {
            latest[run.scenario_id] = run;
        }
    }
    return latest;
}

// Machines by id, with the VM run each host has going (for its card).
export function machinesWithRuns(state) {
    const vmRuns = latestVmRuns(state.runs);
    return Object.fromEntries(state.machines.map((entry) =>
        [entry.machine.id, { ...entry, vmRun: vmRuns[entry.machine.id] }]));
}

// The list view: one card per machine, a mac and its VM side by side.
export function MachineGrid({ state, now }) {
    const machinesById = machinesWithRuns(state);
    const local = machinesById.local;
    const reference = local && local.workspace && local.workspace.fingerprint;
    const groups = groupMachines(
        state.machines.map((entry) => machinesById[entry.machine.id]),
        machinesById);
    const card = (entry) => html`<${MachineCard} key=${entry.machine.id}
        entry=${entry} machinesById=${machinesById}
        reference=${reference} now=${now} />`;
    return html`
        <div class="machine-grid">
            ${groups.map((group) => group.length === 1 ? card(group[0])
                : html`<div class="group" key=${group[0].machine.id}>
                    <div class="group-label">${
                        `${group[0].machine.label} + ${group[1].machine.label}`
                    }</div>
                    ${group.map(card)}
                </div>`)}
        </div>`;
}

// Where the inventory comes from and the agent version, at the bottom of
// the page.
export function InventoryFooter({ state }) {
    return html`
        <footer>
            ${state.orchestrator.inventory_paths.map((entry) => html`
                <span>inventory <span class="mono">${entry.path}</span>
                    ${entry.exists ? '' : html`<span class="faint">${
                        ' (not found)'}</span>`}</span>`)}
            <span>agent <span class="mono">${
                state.orchestrator.agent_hash}</span></span>
        </footer>`;
}

