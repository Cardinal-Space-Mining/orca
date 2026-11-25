// Adding a machine: find it (scan a network, a device on a shared port, or
// an address), check its host key, install this laptop's SSH key if needed,
// connect once to see what it is, then review the inventory entry and save.

import { useEffect, useState } from 'preact/hooks';
import { html, getJson, postJson, StatusDot } from './common.js';
import { SETUP_TAGS } from './inventory.js';
import { openTerminal } from './terminals.js';


const USER_KEY = 'orchestrator.add-machine-user';
const STEPS = [['find', 'Find it'], ['connect', 'Connect'],
    ['review', 'Review'], ['done', 'Done']];

// A machine to add, chosen elsewhere (a shared port, a scan on the network
// page): the wizard starts at "Connect" with it.
let pendingTarget = null;

export function startAddMachine(target) {
    pendingTarget = target;
    window.location.hash = '#/machines/add';
}

function suggestId(hostname) {
    return (hostname || '').split('.')[0].toLowerCase()
        .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
}

function machineLabel(state, machineId) {
    const entry = state.machines.find((other) =>
        other.machine.id === machineId);
    return entry ? entry.machine.label : machineId;
}


// --- Step 1: find it ---------------------------------------------------------

// Likely candidates first: unknown devices with SSH open.
function sortHosts(hosts) {
    const rank = (host) => (host.machine_id ? 2 : 0)
        + (host.ports['22'] === 'open' ? 0 : 1);
    return [...hosts].sort((a, b) => rank(a) - rank(b));
}

function ScanSource({ state, onChoose }) {
    const online = state.machines.filter((entry) =>
        entry.connection.state === 'online');
    const [machineId, setMachineId] = useState('local');
    const [networks, setNetworks] = useState([]);
    const [cidr, setCidr] = useState('');
    const [result, setResult] = useState(null);
    const [error, setError] = useState(null);
    const [isScanning, setIsScanning] = useState(false);

    useEffect(() => {
        getJson(`/api/discovery/networks?machine_id=${
            encodeURIComponent(machineId)}`).then((answer) => {
            setNetworks(answer.networks);
            setCidr(answer.networks.length ? answer.networks[0].cidr : '');
        }).catch(() => setNetworks([]));
    }, [machineId]);

    async function scan() {
        setIsScanning(true);
        setError(null);
        try {
            setResult(await postJson('/api/discovery/scan',
                { machine_id: machineId, cidr }));
        } catch (failure) {
            setError(failure.message);
        }
        setIsScanning(false);
    }

    return html`
        <div class="fields">
            <label class="field">
                <span>Scan from</span>
                <select class="input" value=${machineId}
                    onChange=${(event) => setMachineId(event.target.value)}>
                    ${online.map((entry) => html`<option
                        value=${entry.machine.id}>${entry.machine.label}
                        </option>`)}
                </select>
            </label>
            <label class="field">
                <span>Network</span>
                <input class="input mono" list="scan-networks" value=${cidr}
                    onInput=${(event) => setCidr(event.target.value)} />
                <datalist id="scan-networks">${networks.map((network) =>
                    html`<option value=${network.cidr}>${network.interface}
                        </option>`)}</datalist>
            </label>
            <div class="field"><span>${' '}</span>
                <button class="button primary" disabled=${!cidr || isScanning}
                    onClick=${scan}>${isScanning ? 'Scanning…' : 'Scan'}
                    </button></div>
        </div>
        ${error && html`<div class="error-banner">${error}</div>`}
        ${result && html`
            <p class="faint small-note">${`${result.hosts.length} devices in ${
                result.network} (${result.duration_s.toFixed(1)} s). SSH
                "closed" means the device is there but doesn't run an SSH
                server; "no reply" means it only answered ARP.`}</p>
            <table class="table">
                <thead><tr><th>Address</th><th>Name</th><th>MAC</th>
                    <th>SSH</th><th></th></tr></thead>
                <tbody>${sortHosts(result.hosts).map((host) => {
                    const ssh = host.ports['22'];
                    return html`<tr>
                        <td class="mono">${host.address}</td>
                        <td>${host.name
                            || html`<span class="faint">–</span>`}</td>
                        <td class="mono faint">${host.mac || '–'}</td>
                        <td>${ssh === 'open'
                            ? html`<span class="pill good">open</span>`
                            : html`<span class="faint">${host.arp_only
                                ? 'no reply' : ssh || '–'}</span>`}</td>
                        <td>${host.machine_id
                            ? html`<span class="faint">${machineLabel(state,
                                host.machine_id)}</span>`
                            : html`<button class="button small"
                                onClick=${() => onChoose({
                                    address: host.address,
                                    hostname: host.name, mac: host.mac,
                                    via: host.via })}>Use</button>`}</td>
                    </tr>`;
                })}</tbody>
            </table>`}`;
}

function SharedSource({ state, onChoose }) {
    const sharing = state.machines.filter((entry) => entry.sharing
        && entry.sharing.sessions && entry.sharing.sessions.length);
    const devices = sharing.flatMap((entry) => entry.sharing.sessions
        .flatMap((session) => (session.devices || []).map((device) => ({
            ...device, sharer: entry.machine,
            port: session.downstream }))));
    if (sharing.length === 0) {
        return html`<${QuickShare} state=${state} />`;
    }
    return html`
        ${devices.length === 0 && html`<div class="console-note">Sharing is
            on, but nothing has shown up on the port yet. Plug the machine in
            and give it a few seconds.</div>`}
        ${devices.length > 0 && html`<table class="table">
            <thead><tr><th>Device</th><th>Address</th><th>MAC</th>
                <th>On</th><th></th></tr></thead>
            <tbody>${devices.map((device) => html`<tr>
                <td>${device.hostname
                    || html`<span class="faint">unnamed</span>`}</td>
                <td class="mono">${device.address}</td>
                <td class="mono faint">${device.mac}</td>
                <td class="faint">${`${device.sharer.label} · ${
                    device.port}`}</td>
                <td>${device.machine_id
                    ? html`<span class="faint">${machineLabel(state,
                        device.machine_id)}</span>`
                    : html`<button class="button small" onClick=${() =>
                        onChoose({
                            address: device.address,
                            hostname: device.hostname, mac: device.mac,
                            via: device.sharer.is_local ? null
                                : device.sharer.id })}>Use</button>`}</td>
            </tr>`)}</tbody>
        </table>`}`;
}

// No machine is sharing: offer to share this laptop's internet right here,
// for a machine plugged straight into it.
function QuickShare({ state }) {
    const local = state.machines.find((entry) => entry.machine.is_local);
    const status = local && local.sharing;
    const ports = status && status.supported ? status.ports.filter((port) =>
        port.kind === 'ethernet' && !port.is_default_route) : [];
    const [port, setPort] = useState(() => (ports.find((candidate) =>
        candidate.carrier) || ports[0] || {}).name || '');
    const [error, setError] = useState(null);
    const [isStarting, setIsStarting] = useState(false);
    const isReady = status && status.helper_installed && status.helper_current
        && status.sudo_ok && status.dnsmasq_installed;
    async function start() {
        setIsStarting(true);
        setError(null);
        try {
            await postJson('/api/machines/local/sharing/up',
                { downstream: port });
        } catch (failure) {
            setError(failure.message);
        }
        setIsStarting(false);
    }
    return html`
        <p class="muted">Plugged straight into this laptop with a cable?
            Share this laptop's internet on that port: the machine gets an
            address and shows up here.</p>
        ${!isReady && html`<div class="console-note">Sharing isn't set up on
            this laptop yet: <a class="link-button" href="#/network">Network
            → Internet sharing</a>.</div>`}
        ${isReady && ports.length === 0 && html`<div class="console-note">
            No free Ethernet port on this laptop (a USB adapter works).</div>`}
        ${isReady && ports.length > 0 && html`<div class="fields">
            <label class="field">
                <span>Port</span>
                <select class="input" value=${port}
                    onChange=${(event) => setPort(event.target.value)}>
                    ${ports.map((candidate) => html`<option
                        value=${candidate.name}>${candidate.name} (${
                        candidate.carrier ? 'cable in' : 'no cable'})
                        </option>`)}
                </select>
            </label>
            <div class="field"><span>${' '}</span>
                <button class="button primary" disabled=${!port || isStarting}
                    onClick=${start}>${isStarting ? 'Starting…'
                        : 'Share internet on this port'}</button></div>
        </div>`}
        ${error && html`<div class="error-banner">${error}</div>`}`;
}

function AddressSource({ state, onChoose }) {
    const online = state.machines.filter((entry) =>
        entry.connection.state === 'online' && !entry.machine.is_local);
    const [address, setAddress] = useState('');
    const [port, setPort] = useState('');
    const [via, setVia] = useState('');
    return html`
        <div class="fields">
            <label class="field">
                <span>Address or name</span>
                <input class="input mono" value=${address}
                    placeholder="10.11.11.48 or lance-vm.local"
                    onInput=${(event) => setAddress(event.target.value.trim())}
                    />
            </label>
            <label class="field short-field">
                <span>SSH port</span>
                <input class="input mono" value=${port} placeholder="22"
                    onInput=${(event) => setPort(event.target.value.trim())} />
            </label>
            <label class="field">
                <span>Reached through</span>
                <select class="input" value=${via}
                    onChange=${(event) => setVia(event.target.value)}>
                    <option value="">directly from this laptop</option>
                    ${online.map((entry) => html`<option
                        value=${entry.machine.id}>${entry.machine.label}
                        (e.g. a VM on it)</option>`)}
                </select>
            </label>
            <div class="field"><span>${' '}</span>
                <button class="button primary" disabled=${!address}
                    onClick=${() => onChoose({ address, port: port || null,
                        via: via || null })}>Next</button></div>
        </div>`;
}

function FindStep({ state, onChoose }) {
    const isSharing = state.machines.some((entry) => entry.sharing
        && entry.sharing.sessions && entry.sharing.sessions.length);
    const [source, setSource] = useState(isSharing ? 'shared' : 'scan');
    const sources = [['scan', 'Scan a network'],
        ['shared', 'Plugged into a shared port'],
        ['address', 'Enter an address']];
    return html`
        <div class="segmented">${sources.map(([id, label]) => html`
            <button class=${source === id ? 'active' : ''}
                onClick=${() => setSource(id)}>${label}</button>`)}</div>
        ${source === 'scan' && html`<${ScanSource} state=${state}
            onChoose=${onChoose} />`}
        ${source === 'shared' && html`<${SharedSource} state=${state}
            onChoose=${onChoose} />`}
        ${source === 'address' && html`<${AddressSource} state=${state}
            onChoose=${onChoose} />`}`;
}


// --- Step 2: connect ---------------------------------------------------------

function HostKeyCheck({ check, state }) {
    if (!check) return null;
    return html`
        <div class="compare-box key-check">
            ${check.status === 'trusted' && html`<p><span class="pill good">
                trusted</span> Its host key is already trusted as
                <span class="mono">${` ${check.name}`}</span>.</p>`}
            ${check.status === 'unknown' && html`<p>Compare these with the
                machine's own (<span class="mono">ssh-keygen -lf
                /etc/ssh/ssh_host_ed25519_key.pub</span> on it) before
                trusting them. They'll be saved as
                <span class="mono">${` ${check.name}`}</span>.</p>`}
            ${check.status === 'different' && html`<div class="error-banner">
                A different key is already trusted as ${check.name}. If the
                machine was reinstalled, remove the old one with
                <span class="mono">${` ssh-keygen -R ${check.name}`}</span>,
                or pick another id.</div>`}
            ${check.same_key_as.length > 0 && html`<div class="warn-banner">
                ${machineLabel(state, check.same_key_as[0])} has the same key.
                Either this is that machine at another address, or it was
                cloned from it (cloned VMs keep their original's keys:
                regenerate them on the clone with${' '}<span class="mono">sudo
                rm /etc/ssh/ssh_host_* && sudo dpkg-reconfigure
                openssh-server</span>).</div>`}
            <pre class="mono fingerprints">${check.fingerprints.join('\n')}
                </pre>
        </div>`;
}

function ConnectStep({ state, target, onReady, onBack }) {
    const [id, setId] = useState(() => suggestId(target.hostname));
    const [label, setLabel] = useState(
        () => (target.hostname || '').split('.')[0]);
    const [user, setUser] = useState(
        () => window.localStorage.getItem(USER_KEY) || 'lance');
    const [check, setCheck] = useState(null);
    const [probe, setProbe] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(null);
    const taken = new Set(state.machines.map((entry) => entry.machine.id));
    const idProblem = !id ? 'needs an id'
        : !/^[a-z0-9][a-z0-9-]*$/.test(id) ? 'lowercase letters, digits, -'
            : taken.has(id) ? 'already in the inventory' : null;
    const request = {
        address: target.address, port: target.port, via: target.via,
        user, id, label, host_key_alias: `${id}.orchestrator`,
    };

    async function step(name, action) {
        setBusy(name);
        setError(null);
        try {
            await action();
        } catch (failure) {
            setError(failure.message);
        }
        setBusy(null);
    }
    const checkKey = () => step('key', async () => {
        window.localStorage.setItem(USER_KEY, user);
        setProbe(null);
        setCheck(await postJson('/api/discovery/host-key', request));
    });
    const connect = () => step('connect', async () => {
        if (check.status === 'unknown') {
            await postJson('/api/discovery/trust',
                { ...request, fingerprints: check.fingerprints });
        }
        const result = await postJson('/api/discovery/probe', request);
        setProbe(result);
        if (result.state === 'ready') onReady(request, result);
    });

    return html`
        <p class="muted">${`Adding ${target.address}`}${target.via
            ? ` (through ${machineLabel(state, target.via)})` : ''}${
            target.mac ? `, MAC ${target.mac}` : ''}.</p>
        <div class="fields">
            <label class="field">
                <span>Id ${idProblem && html`<em class="field-error">
                    ${idProblem}</em>`}</span>
                <input class="input mono" value=${id} placeholder="robot-vm"
                    onInput=${(event) => { setId(event.target.value); setCheck(
                        null); }} />
            </label>
            <label class="field">
                <span>Name</span>
                <input class="input" value=${label} placeholder="Robot VM"
                    onInput=${(event) => setLabel(event.target.value)} />
            </label>
            <label class="field">
                <span>User on it</span>
                <input class="input mono" value=${user}
                    onInput=${(event) => { setUser(event.target.value.trim());
                        setCheck(null); }} />
            </label>
            <div class="field"><span>${' '}</span>
                <button class="button" disabled=${!!idProblem || !user
                    || !!busy} onClick=${checkKey}>${busy === 'key'
                        ? 'Checking…' : 'Check host key'}</button></div>
        </div>
        <${HostKeyCheck} check=${check} state=${state} />
        ${check && check.status !== 'different' && html`
            <div class="button-row">
                <button class="button primary" disabled=${!!busy}
                    onClick=${connect}>${busy === 'connect' ? 'Connecting…'
                        : check.status === 'unknown' ? 'Trust and connect'
                            : 'Connect'}</button>
            </div>`}
        ${probe && probe.state === 'needs_key' && html`
            <div class="notice-row">
                <span>Its SSH server doesn't accept this laptop's key yet.
                    Install it once: a terminal opens and asks for
                    ${` ${user}`}'s password on the machine (creating a key
                    first if this laptop has none).</span>
                <span class="button-row">
                    <button class="button primary small" onClick=${() =>
                        openTerminal('local', 'install-key',
                            { target: request })}>Install my SSH key</button>
                    <button class="button small" disabled=${!!busy}
                        onClick=${connect}>Try again</button>
                </span>
            </div>`}
        ${probe && probe.state !== 'needs_key' && probe.state !== 'ready'
            && html`<div class="error-banner">${probe.error
                || 'Could not connect'}</div>`}
        ${error && html`<div class="error-banner">${error}</div>`}
        <div class="button-row">
            <button class="link-button" onClick=${onBack}>← Pick another
                machine</button>
        </div>`;
}


// --- Step 3: review ----------------------------------------------------------

function ReviewStep({ probe, onSaved }) {
    const [entry, setEntry] = useState(probe.suggestion);
    const [scope, setScope] = useState('user');
    const [error, setError] = useState(null);
    const [isSaving, setIsSaving] = useState(false);
    const info = probe.info;
    const survey = probe.survey || {};
    const workspaces = survey.workspaces || [];
    const set = (key, value) => {
        const next = { ...entry };
        if (value === '' || value === null || value === undefined) {
            delete next[key];
        } else {
            next[key] = value;
        }
        setEntry(next);
    };

    async function save() {
        setIsSaving(true);
        setError(null);
        try {
            await postJson('/api/discovery/add', { scope, entry });
            onSaved(entry);
        } catch (failure) {
            setError(failure.message);
            setIsSaving(false);
        }
    }

    return html`
        <div class="review-grid">
            <div>
                <h3>${entry.label || entry.id}${' '}<span
                    class="machine-id mono">${entry.id}</span></h3>
                <p class="faint">${`Host name ${info.hostname}`}</p>
                <p class="faint">${`${info.os} · ${info.kernel}`}${
                    (survey.ros_distros || []).length
                        ? ` · ROS ${survey.ros_distros.join(', ')}` : ''}</p>
                <table class="table compact">
                    <tbody>${probe.interfaces.filter((port) =>
                        (port.ipv4 || []).length).map((port) => html`<tr>
                            <td class="mono">${port.name}</td>
                            <td class="faint">${port.kind}</td>
                            <td class="mono">${port.ipv4.join(', ')}</td>
                        </tr>`)}</tbody>
                </table>
            </div>
            <div>
                <div class="fields">
                    <label class="field wide">
                        <span>Role</span>
                        <input class="input" value=${entry.role || ''}
                            placeholder="What it's for"
                            onInput=${(event) => set('role',
                                event.target.value)} />
                    </label>
                </div>
                <div class="fields">
                    <label class="field">
                        <span>Workspace</span>
                        <input class="input mono" list="found-workspaces"
                            value=${entry.workspace || ''}
                            placeholder="none"
                            onInput=${(event) => set('workspace',
                                event.target.value.trim())} />
                        <datalist id="found-workspaces">${workspaces.map(
                            (path) => html`<option value=${path} />`)}
                        </datalist>
                    </label>
                    ${entry.vm && html`<label class="field">
                        <span>csm-vm folder</span>
                        <input class="input mono" value=${entry.vm.csm_vm_path}
                            onInput=${(event) => set('vm', { ...entry.vm,
                                csm_vm_path: event.target.value.trim() })} />
                    </label>`}
                </div>
                <div class="section-head"><span>Routes, tried in order</span>
                    </div>
                <ul class="plain-list mono">${entry.routes.map((route) =>
                    html`<li>${route.host}${route.via
                        ? html`<span class="faint">${
                            ` via ${route.via}`}</span>`
                        : ''}</li>`)}</ul>
                ${entry.config_addresses && html`
                    <div class="section-head"><span>Config addresses</span>
                        </div>
                    <p class="faint small-note">These lance config constants
                        hold its address right now, so launches will keep them
                        up to date:</p>
                    <ul class="plain-list mono">${Object.entries(
                        entry.config_addresses).map(([name, kind]) => html`
                        <li>${name} <span class="faint">${`← its ${kind}`}
                            </span></li>`)}</ul>`}
                <div class="check-row">${SETUP_TAGS.map(([tag, label]) =>
                    html`<label class="check"><input type="checkbox"
                        checked=${(entry.setup || []).includes(tag)}
                        onChange=${(event) => {
                            const tags = (entry.setup || []).filter(
                                (other) => other !== tag);
                            if (event.target.checked) tags.push(tag);
                            set('setup', tags.length ? tags : null);
                        }} />${label}</label>`)}</div>
                <div class="fields">
                    <label class="field">
                        <span>Save to</span>
                        <select class="input" value=${scope}
                            onChange=${(event) => setScope(event.target.value)}>
                            <option value="user">My machines (this laptop
                                only)</option>
                            <option value="project">Team machines (in the
                                workspace, shared through git)</option>
                        </select>
                    </label>
                </div>
                <p class="faint small-note">Everything else can be changed
                    later in <b>Edit machines</b>.</p>
                ${error && html`<div class="error-banner">${error}</div>`}
                <div class="button-row">
                    <button class="button primary" disabled=${isSaving}
                        onClick=${save}>${isSaving ? 'Saving…'
                            : 'Add machine'}</button>
                </div>
            </div>
        </div>`;
}


// --- Page --------------------------------------------------------------------

export function AddMachineView({ state }) {
    const [initial] = useState(() => {
        const taken = pendingTarget;
        pendingTarget = null;
        return taken;
    });
    const [step, setStep] = useState(initial ? 'connect' : 'find');
    const [target, setTarget] = useState(initial);
    const [probe, setProbe] = useState(null);
    const [saved, setSaved] = useState(null);

    function choose(chosen) {
        setTarget(chosen);
        setStep('connect');
    }
    const guests = saved && probe && (probe.survey.vm_guests || []);
    const savedEntry = saved && state.machines.find((entry) =>
        entry.machine.id === saved.id);

    return html`
        <div class="page-head"><div>
            <h1>Add a machine</h1>
            <p><a class="link-button" href="#/machines">← Machines</a></p>
        </div></div>
        <ol class="wizard-steps">${STEPS.map(([id, label], index) => html`
            <li class=${id === step ? 'active' : STEPS.findIndex(
                ([other]) => other === step) > index ? 'done' : ''}>
                <span>${index + 1}</span>${label}</li>`)}</ol>
        <section class="panel">
            ${step === 'find' && html`<${FindStep} state=${state}
                onChoose=${choose} />`}
            ${step === 'connect' && html`<${ConnectStep} key=${
                target.address} state=${state} target=${target}
                onBack=${() => setStep('find')}
                onReady=${(request, result) => {
                    setProbe(result);
                    setStep('review');
                }} />`}
            ${step === 'review' && html`<${ReviewStep} probe=${probe}
                onSaved=${(entry) => {
                    setSaved(entry);
                    setStep('done');
                }} />`}
            ${step === 'done' && html`
                <p>${saved.label} is in the inventory${savedEntry
                    ? html`${' and '}<${StatusDot}
                        state=${savedEntry.connection.state} />${
                        ` ${savedEntry.connection.state}`}` : ''}.</p>
                ${guests && guests.length > 0 && saved.vm && html`
                    <div class="notice-row">
                        <span>${`It hosts a VM at ${guests[0].address}. Add
                            it too: it's reached through ${saved.label}.`}
                            </span>
                        <button class="button primary small" onClick=${() => {
                            setProbe(null);
                            setSaved(null);
                            choose({ address: guests[0].address,
                                mac: guests[0].mac, via: saved.id });
                        }}>Add the VM</button>
                    </div>`}
                <div class="button-row">
                    <a class="button" href="#/machines">Back to machines</a>
                    <button class="button" onClick=${() => {
                        setTarget(null);
                        setProbe(null);
                        setSaved(null);
                        setStep('find');
                    }}>Add another</button>
                </div>`}
        </section>`;
}
