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

// Restart a machine (restart.py): a VM host shuts its VM down first and
// starts it again after. If sudo wants a password, a terminal runs it.
async function restartMachine(entry, machinesById) {
    const { machine } = entry;
    const guest = machine.vm && machine.vm.guest
        && machinesById[machine.vm.guest];
    const hasVm = !!(entry.vm && entry.vm.is_running);
    const lines = [`Restart ${machine.label}?`];
    if (hasVm) {
        lines.push(`Its VM${guest ? ` (${guest.machine.label})` : ''} is shut `
            + 'down first (its overlay is committed) and started again once '
            + `${machine.label} is back.`);
    }
    if (machine.host) {
        lines.push('This restarts its OS only: the VM keeps its USB devices. '
            + 'To reset those, restart the host.');
    }
    lines.push('Anything running on it stops, including what Orca launched.');
    if (!window.confirm(lines.join('\n\n'))) return;
    try {
        const result = await postJson(`/api/machines/${encodeURIComponent(
            machine.id)}/restart`);
        if (result.needs_password && window.confirm(`sudo asks for a password `
            + `to restart ${machine.label}. Restart it from a terminal, where `
            + 'you can type it? (The "Restart without a password" setup item '
            + 'makes this one click.)')) {
            openTerminal(machine.id, 'restart');
        }
    } catch (failure) {
        window.alert(failure.message);
    }
}

// A restart in progress (or how the last one ended), for the card and map.
export function RestartStatus({ restart, compact = false }) {
    if (!restart) return null;
    const level = restart.error ? 'bad' : restart.done ? 'good' : 'warn';
    if (compact) {
        return html`<span class="pill ${level}" title=${restart.error
            || restart.phase}>${restart.error ? 'restart failed'
            : restart.done ? 'restarted' : 'restarting…'}</span>`;
    }
    return html`<div class="notice-line ${restart.error ? 'bad'
        : restart.done ? '' : 'warn'}">
        <span class="dot ${level} ${restart.done || restart.error ? ''
            : 'pulse'}"></span>
        <span><b>${restart.phase}</b>${restart.error
            ? `: ${restart.error}` : restart.done ? '' : '…'}</span>
    </div>`;
}

function postReconnect(machineId) {
    postJson(`/api/machines/${encodeURIComponent(machineId)}/reconnect`)
        .catch(() => {});  // the card shows the link's state anyway
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

// `reference` is the code fingerprint of Orca's own workspace.
function workspaceBadges(workspace, reference, isReference) {
    const badges = [];
    if (isReference) {
        badges.push(html`<span class="pill accent">
            Orca's copy</span>`);
    } else if (reference && workspace.fingerprint === reference) {
        badges.push(html`<span class="pill good">
            same code as Orca</span>`);
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

// Edits in a host's csm-vm checkout that exist only on that machine: easy to
// lose track of. Empty when it's in sync (or not a git checkout).
export function csmVmProblems(repo) {
    if (!repo) return [];
    const problems = [];
    if (repo.num_changes > 0) {
        problems.push(`${repo.num_changes} uncommitted`);
    }
    const unpushed = Math.max(repo.num_ahead || 0, repo.num_unpushed || 0);
    if (unpushed > 0) problems.push(`${unpushed} unpushed`);
    return problems;
}

function RepoStatus({ repo }) {
    if (!repo) return html`<span class="faint">not a git checkout</span>`;
    const problems = csmVmProblems(repo);
    const offBranch = (repo.num_unpushed || 0) > (repo.num_ahead || 0);
    const title = [
        repo.changed_files.length ? `changed: ${repo.changed_files.join(', ')}`
            + (repo.num_changes > repo.changed_files.length ? ', …' : '')
            : '',
        repo.upstream ? `tracking ${repo.upstream}`
            : 'this branch has no upstream (never pushed)',
        offBranch ? 'other local branches have commits no remote has' : '',
        repo.num_behind ? `${repo.num_behind} behind (as of the last fetch)`
            : '',
    ].filter(Boolean).join('\n');
    return html`<div class="badges" title=${title}>
        <span class="mono">${repo.branch}</span>
        ${problems.length ? problems.map((problem) => html`
            <span class="pill warn">${problem}</span>`)
            : html`<span class="pill good">in sync</span>`}
        ${repo.num_behind > 0 && html`<span class="pill">${
            `${repo.num_behind} behind`}</span>`}
    </div>`;
}

// What to know about a leftover overlay before reusing or committing it.
function overlayWarnings(vm) {
    const overlay = vm.overlay;
    if (!overlay) return [];
    const warnings = [];
    if (overlay.is_recovered) {
        warnings.push({ level: 'warn', text: 'is a recovered session',
            detail: 'Brought back from the saved sessions. Stopping the VM '
                + "won't commit it: commit it only if this session should "
                + 'become the base disk, or discard it.' });
    } else if (overlay.is_unclean) {
        warnings.push({ level: 'warn', text: "didn't shut down", detail:
            'A power cut, e-stop or force stop: the guest went down without '
            + 'shutting down, like a computer losing power. Reusing it boots '
            + 'that session again (its files and logs are there).' });
    }
    if (!overlay.is_writable) {
        warnings.push({ level: 'warn', text: `owned by ${overlay.owner}`,
            detail: 'Made by a start that ran as root: committing it '
                + 'needs sudo (run-vm.sh does that itself).' });
    }
    return warnings;
}

function showRun(runId) {
    window.location.hash = `#/runs/${runId}`;
}

// Start or restart: what to do with an overlay a previous session left
// behind (it holds changes made in the VM since that session started).
function StartForm({ vm, action, onSubmit, onCancel }) {
    const hasOverlay = vm.disks.overlay_bytes != null && action === 'start';
    const isUnclean = !!(vm.overlay && vm.overlay.is_unclean);
    const isRecovered = !!(vm.overlay && vm.overlay.is_recovered);
    // Reuse keeps everything (a power cut's logs too): never preselect less.
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
                    there (${formatBytes(vm.disks.overlay_bytes)}${vm.overlay
                    ? `, last written ${formatAgo(vm.overlay.modified_s,
                        Date.now() / 1000)}` : ''}):`}</div>
                ${isRecovered ? html`<div class="muted">It's a recovered
                    session: reusing it boots that session, and stopping the
                    VM won't commit it.</div>`
                    : isUnclean && html`<div class="muted">Its VM didn't shut
                    down (power cut, e-stop or force stop). Reusing it boots
                    that session again, like a computer after a power cut:
                    its files and logs are there.</div>`}
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
    commitUnclean: "This overlay's VM didn't shut down (power cut, e-stop or "
        + 'force stop), so files written just before may be incomplete, and '
        + 'committing writes it into the base disk for good. Commit it anyway?',
};

// The action a VM run did ("stop", "restart", ...): its plan's only step.
function runAction(run) {
    return run && run.plan && run.plan.processes
        && run.plan.processes[0] && run.plan.processes[0].key;
}

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
        if ((action === 'commit' || options.overlay === 'commit')
                && vm.overlay && vm.overlay.is_unclean
                && !window.confirm(CONFIRMATIONS.commitUnclean)) {
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
    // A shutdown that gave up (run-vm.sh's timeout) leaves the VM running.
    const stopFailed = vm.is_running && vmRun && vmRun.state === 'failed'
        && ['stop', 'restart'].includes(runAction(vmRun));
    return html`
        ${stopFailed && html`<div class="notice-line warn">
            <span>The VM didn't power off: the guest may be hung or ignoring
                its power button.</span>
            <button class="link-button" onClick=${() => showRun(
                vmRun.run_id)}>See why</button>
            <button class="button small danger" disabled=${isBusy}
                onClick=${() => act('kill')}>Force stop</button>
        </div>`}
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

function formatWhen(seconds) {
    return new Date(seconds * 1000).toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// Sessions that didn't shut down (power cuts), kept by run-vm.sh: recover
// one to boot it again (for its logs, say), or delete it.
function SavedSessions({ machineId, vm, vmRun }) {
    const [error, setError] = useState(null);
    const saved = vm.saved_overlays || [];
    if (saved.length === 0) return null;
    const isBusy = vmRun && ['starting', 'running'].includes(vmRun.state);
    async function act(action, entry) {
        const when = `${formatWhen(entry.modified_s)}, ${formatBytes(
            entry.bytes)}`;
        const question = action === 'recover'
            ? `Make the session saved ${when} the VM's overlay? `
                + (vm.disks.overlay_bytes != null ? 'The current overlay is '
                    + 'saved first. ' : '')
                + 'Then Start (Reuse) boots that session, with its files and '
                + "logs. Stopping it won't commit it."
            : `Delete the session saved ${when}? Its files and logs are lost.`;
        if (!window.confirm(question)) return;
        setError(null);
        try {
            const run = await postJson(`/api/machines/${encodeURIComponent(
                machineId)}/vm/${action}`, { name: entry.name });
            showRun(run.run_id);
        } catch (failure) {
            setError(failure.message);
        }
    }
    return html`
        <dt>Saved sessions</dt>
        <dd>
            <div class="saved-sessions">${saved.map((entry) => html`
                <div class="saved-session" title=${entry.name}>
                    <span>${formatWhen(entry.modified_s)}</span>
                    <span class="faint">${formatBytes(entry.bytes)}</span>
                    <span class="spacer"></span>
                    <button class="link-button" disabled=${vm.is_running
                        || isBusy} title=${vm.is_running
                        ? 'Shut the VM down first' : 'Boot this session again'}
                        onClick=${() => act('recover', entry)}>Recover</button>
                    <button class="link-button danger" disabled=${vm.is_running
                        || isBusy} onClick=${() => act('forget', entry)}>
                        Delete</button>
                </div>`)}</div>
            ${error && html`<div class="error-text">${error}</div>`}
        </dd>`;
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
                ].join(' · ')}
                    ${overlayWarnings(vm).length > 0 && html`<div
                        class="badges">${overlayWarnings(vm).map((warning) =>
                        html`<span class="pill ${warning.level}"
                            title=${warning.detail}>${`overlay ${
                            warning.text}`}</span>`)}</div>`}</dd>
                <${SavedSessions} machineId=${machineId} vm=${vm}
                    vmRun=${vmRun} />
                ${'repo' in vm && html`<dt>csm-vm</dt>
                    <dd><${RepoStatus} repo=${vm.repo} /></dd>`}
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
                ${isOnline && !machine.is_local && html`<button
                    class="icon-button" title="Restart this machine"
                    disabled=${!!(entry.restart && !entry.restart.done
                        && !entry.restart.error)}
                    onClick=${() => restartMachine(entry, machinesById)}>
                    Restart</button>`}
            </header>
            <${RestartStatus} restart=${entry.restart} />
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
    // An offline host's VM status is from when it was last reachable.
    const numVmsRunning = machines.filter((entry) =>
        entry.connection.state === 'online' && entry.vm
        && entry.vm.is_running).length;
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
            ${state.orca.inventory_paths.map((entry) => html`
                <span>inventory <span class="mono">${entry.path}</span>
                    ${entry.exists ? '' : html`<span class="faint">${
                        ' (not found)'}</span>`}</span>`)}
            <span>agent <span class="mono">${
                state.orca.agent_hash}</span></span>
        </footer>`;
}

