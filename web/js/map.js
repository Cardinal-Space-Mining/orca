// The network map: each IP network is a line, machines hang from the lines
// by their ports, and other devices (the router, the lidar, things plugged
// into a share) sit on the lines. Click anything to see it in the side panel.
//
// A line means "on the same IP network", not "cabled together": switches
// and access points are invisible (backend/network_map.py).

import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import {
    html, Icon, MACHINE_ICONS, PORT_ICONS, StatusDot, formatAgo, postJson,
} from './common.js';
import { startAddMachine } from './addmachine.js';
import { MachineCard, machinesWithRuns } from './machines.js';
import { BudgetPanel, Sparkline, ToolsPanel, formatKbps } from './network.js';
import { HelperNotice, isReady } from './sharing.js';
import { openTerminal } from './terminals.js';
import { trafficStore } from './traffic.js';


// Columns widen to fill the map, then the whole map scales (CSS zoom) to the
// width it has, so it neither leaves space nor needs a scrollbar.
const MIN_COLUMN = 210;
const MAX_COLUMN = 280;
const MIN_SCALE = 0.65;
const MAX_SCALE = 1.25;
const MIN_MAP_WIDTH = 760;   // room for the busiest network's label row
const RIGHT = 30;
const COLUMN_GAP = 26;
const GUEST_GAP = 16;        // between a host and its VM in their frame
const LEFT = 28;
const TOP = 44;              // room for the first network's label
const LINE_GAP = 54;
const LOOSE_PORT_WIDTH = 30; // a port on no network: just its icon
const PORT_HEIGHT = 24;
const BOX_GAP = 40;          // between the last line and the port chips
const BOX_HEIGHT = 152;
const MAX_PORTS = 4;
const BUSY_DEVICES = 3;      // more on a network than this: show a count
const NETWORK_ICONS = {
    lan: 'network', share: 'share-2', 'host-only': 'arrow-left-right',
    local: 'network',
};
const DEVICE_ICONS = { router: 'router', lidar: 'radar', device: 'circle-dot' };


// --- Layout ------------------------------------------------------------------

function visiblePorts(machine) {
    const shown = machine.ports.filter((port) => port.addresses.length
        || port.is_up || port.sharing || port.label === 'USB Ethernet');
    return { shown: shown.slice(0, MAX_PORTS),
        hidden: machine.ports.length - Math.min(shown.length, MAX_PORTS) };
}

// Columns: the inventory's order, each VM right after its host.
function machineGroups(machines) {
    const byId = Object.fromEntries(machines.map((machine) =>
        [machine.id, machine]));
    const placed = new Set();
    const groups = [];
    for (const machine of machines) {
        if (placed.has(machine.id)) continue;
        if (machine.host && byId[machine.host] && !placed.has(machine.host)) {
            continue; // placed with its host
        }
        const group = [machine];
        placed.add(machine.id);
        const guest = machine.guest && byId[machine.guest];
        if (guest && !placed.has(guest.id)) {
            group.push(guest);
            placed.add(guest.id);
        }
        groups.push(group);
    }
    for (const machine of machines) {
        if (!placed.has(machine.id)) groups.push([machine]);
    }
    return groups;
}

// Machines on the map: everything online, and an offline VM or host whose
// partner is online (so the pair stays together). The rest are listed in a
// strip below the map.
export function splitMachines(machines) {
    const byId = Object.fromEntries(machines.map((machine) =>
        [machine.id, machine]));
    const isOnline = (machine) => machine && machine.state === 'online';
    const onMap = (machine) => isOnline(machine)
        || isOnline(byId[machine.host]) || isOnline(byId[machine.guest]);
    return { mapped: machines.filter(onMap),
        offline: machines.filter((machine) => !onMap(machine)) };
}

function devicesOf(network, devicesByMac) {
    const devices = network.devices.map((mac) => devicesByMac[mac])
        .filter(Boolean).sort((a, b) => (a.kind === 'device')
            - (b.kind === 'device'));
    const isBusy = network.kind !== 'share' && devices.length > BUSY_DEVICES;
    const shown = isBusy ? devices.filter((device) =>
        device.kind !== 'device').slice(0, 2) : devices;
    return { shown, more: devices.length - shown.length };
}

function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
}

export function layoutMap(model, available = 1200) {
    const { mapped, offline } = splitMachines(model.machines);
    const groups = machineGroups(mapped);
    const numGuests = groups.reduce((total, group) =>
        total + group.length - 1, 0);
    const fixed = LEFT + RIGHT + Math.max(groups.length - 1, 0) * COLUMN_GAP
        + numGuests * GUEST_GAP;
    const column = mapped.length ? clamp((available - fixed)
        / mapped.length, MIN_COLUMN, MAX_COLUMN) : MIN_COLUMN;
    const lineY = {};
    model.networks.forEach((network, index) => {
        lineY[network.id] = TOP + index * LINE_GAP;
    });
    const lastLine = TOP + Math.max(model.networks.length - 1, 0) * LINE_GAP;
    const boxY = lastLine + BOX_GAP + PORT_HEIGHT;
    const boxes = [];
    const frames = [];
    const ports = [];
    let x = LEFT;
    for (const group of groups) {
        const start = x;
        group.forEach((machine, index) => {
            if (index) x += GUEST_GAP;
            const { shown, hidden } = visiblePorts(machine);
            boxes.push({ machine, x, y: boxY, hidden, width: column });
            // Ports on a network share the width; loose ones are icons.
            const numLoose = shown.filter((port) => !port.network).length;
            const numLinked = shown.length - numLoose;
            const portSpace = column - 12 - numLoose * (LOOSE_PORT_WIDTH + 4);
            const linkedWidth = numLinked ? portSpace / numLinked : 0;
            let cursor = x + 6;
            for (const port of shown) {
                const slot = port.network ? linkedWidth : LOOSE_PORT_WIDTH + 4;
                ports.push({
                    machine, port, y: boxY - PORT_HEIGHT,
                    x: cursor + slot / 2,
                    width: port.network ? Math.min(slot - 6, 140)
                        : LOOSE_PORT_WIDTH,
                });
                cursor += slot;
            }
            x += column;
        });
        if (group.length > 1) {
            frames.push({ x: start - 8, width: x - start + 16,
                label: group.map((machine) => machine.label).join(' + ') });
        }
        x += COLUMN_GAP;
    }

    const width = Math.max(x - COLUMN_GAP + RIGHT, MIN_MAP_WIDTH);
    const devicesByMac = Object.fromEntries(model.devices.map((device) =>
        [device.mac, device]));
    const lines = model.networks.map((network) => ({
        network, y: lineY[network.id], ...devicesOf(network, devicesByMac),
        x1: LEFT - 10, x2: width - 16,
    }));
    return { boxes, frames, ports, lines, lineY, offline, width,
        height: boxY + BOX_HEIGHT + 44, boxY,
        scale: clamp(available / width, MIN_SCALE, MAX_SCALE) };
}

function trafficWidth(port) {
    const kbps = ((port.rx_bps || 0) + (port.tx_bps || 0)) / 1000;
    return 2 + Math.min(2, Math.log10(1 + kbps) / 2);
}

// Traffic on a port: dashes moving down the line (mostly incoming) or up it
// (mostly outgoing), faster the busier it is; a glow from 500 kbps. Speeds
// come in steps so a changing rate doesn't make the animation jump.
const FLOW_STEPS = [[5000, 0.35], [1000, 0.55], [200, 0.8], [40, 1.2],
    [8, 1.7], [1, 2.4]];

function flowOf(port) {
    const down = (port.rx_bps || 0) / 1000;
    const up = (port.tx_bps || 0) / 1000;
    const step = FLOW_STEPS.find(([kbps]) => down + up >= kbps);
    return step ? { seconds: step[1], isUp: up > down,
        isBusy: down + up >= 500 } : null;
}

function portTitle(port) {
    const traffic = port.rx_bps != null ? ` · down ${formatKbps(
        port.rx_bps / 1000)}, up ${formatKbps(port.tx_bps / 1000)}` : '';
    return `${port.label} ${port.name}${port.addresses.length
        ? ` · ${port.addresses.join(', ')}` : ' · no address'}${traffic}`;
}


// --- Drawing -----------------------------------------------------------------

function Lines({ layout, machinesById }) {
    return html`
        <svg class="map-lines" width=${layout.width} height=${layout.height}>
            ${layout.frames.map((frame) => html`<rect class="map-frame"
                x=${frame.x} y=${layout.boxY - PORT_HEIGHT - 10}
                width=${frame.width} height=${BOX_HEIGHT + PORT_HEIGHT + 20}
                rx="12" />`)}
            ${layout.lines.map((line) => html`<line
                class="map-bus ${line.network.kind}" x1=${line.x1} y1=${line.y}
                x2=${line.x2} y2=${line.y} />`)}
            ${layout.ports.filter((entry) => entry.port.network).map(
                (entry) => {
                const y = layout.lineY[entry.port.network];
                const entryState = machinesById[entry.machine.id];
                const isOnline = entryState
                    && entryState.connection.state === 'online';
                const kind = entry.port.is_robot_link ? 'robot-link'
                    : entry.port.sharing ? 'sharing' : '';
                const flow = isOnline && flowOf(entry.port);
                return html`
                    <line class="map-drop ${kind} ${isOnline ? '' : 'offline'}"
                        x1=${entry.x} y1=${y} x2=${entry.x} y2=${entry.y}
                        stroke-width=${trafficWidth(entry.port)} />
                    ${flow && html`<line class="map-flow ${kind} ${flow.isUp
                        ? 'up' : ''} ${flow.isBusy ? 'busy' : ''}"
                        x1=${entry.x} y1=${y + 5} x2=${entry.x} y2=${entry.y}
                        style=${`animation-duration: ${flow.seconds}s`} />`}
                    <circle class="map-joint ${kind}" cx=${entry.x} cy=${y}
                        r="4" />`;
            })}
        </svg>`;
}

function MachineBox({ box, entry, reference, isSelected, onSelect }) {
    const machine = box.machine;
    const info = entry && entry.info;
    const stats = entry && entry.stats;
    const connection = entry ? entry.connection : { state: machine.state };
    const isOnline = connection.state === 'online';
    const addressed = machine.ports.filter((port) => port.addresses.length);
    const todo = entry && entry.setup ? entry.setup.checks.filter((check) =>
        check.state === 'todo').length : 0;
    const workspace = entry && entry.workspace;
    const vm = entry && entry.vm;
    return html`
        <div class="map-box ${isOnline ? '' : 'offline'} ${isSelected
            ? 'selected' : ''}" style=${`left: ${box.x}px; top: ${box.y}px; `
            + `width: ${box.width}px; height: ${BOX_HEIGHT}px`}
            onClick=${() => onSelect(`m:${machine.id}`)}>
            <div class="map-box-head">
                <${Icon} name=${MACHINE_ICONS[machine.kind]} size=${18} />
                <b>${machine.label}</b>
                <span class="spacer"></span>
                <${StatusDot} state=${connection.state} />
            </div>
            <div class="faint map-box-os">${info ? info.os
                : connection.state}</div>
            <div class="map-addresses">${addressed.slice(0, 3).map((port) =>
                html`<span><${Icon} name=${PORT_ICONS[port.label]}
                    size=${12} /><span class="mono">${
                    port.addresses[0].split('/')[0]}</span></span>`)}
                ${!isOnline && !addressed.length && html`<span class="faint">
                    ${connection.error ? 'unreachable' : 'not connected'}
                    </span>`}
                ${box.hidden > 0 && html`<span class="faint">${`+${box.hidden}
                    inactive port${box.hidden > 1 ? 's' : ''}`}</span>`}</div>
            <div class="map-box-foot">
                ${stats && html`<span class="faint" title=${stats
                    .memory_total_bytes ? `memory ${Math.round(100
                    * stats.memory_used_bytes / stats.memory_total_bytes)}%`
                    : ''}>${`CPU ${Math.round(stats.cpu_percent || 0)}%`}
                    </span>`}
                <span class="spacer"></span>
                ${vm && vm.is_running && html`<span class="pill good"
                    title="Its VM is running">VM</span>`}
                ${todo > 0 && html`<span class="pill warn"
                    title="Setup items to do">${`setup ${todo}`}</span>`}
                ${workspace && workspace.fingerprint && reference
                    && !machine.is_local && (workspace.fingerprint === reference
                    ? html`<span class="pill good" title="Same code as yours">
                        code ✓</span>`
                    : html`<span class="pill warn" title="Different code">
                        code ≠</span>`)}
                ${isOnline && html`<button class="icon-button small"
                    title="Open a terminal"
                    onClick=${(event) => {
                        event.stopPropagation();
                        openTerminal(machine.id);
                    }}><${Icon} name="terminal" size=${14} /></button>`}
            </div>
        </div>`;
}

function networkTitle(network, machinesById) {
    if (network.kind === 'share') {
        const sharer = machinesById[network.share.machine_id];
        return `Shared by ${sharer ? sharer.machine.label
            : network.share.machine_id}`;
    }
    if (network.kind === 'host-only') return 'Host-only link';
    if (network.kind === 'lan') return 'Local network';
    return 'Network';
}

// The width an element has, kept up to date.
function useElementWidth() {
    const reference = useRef(null);
    const [width, setWidth] = useState(0);
    useLayoutEffect(() => {
        const observer = new ResizeObserver(([entry]) =>
            setWidth(entry.contentRect.width));
        observer.observe(reference.current);
        return () => observer.disconnect();
    }, []);
    return [reference, width];
}

function MapView({ state, now, model, selection, onSelect }) {
    const [container, available] = useElementWidth();
    const layout = layoutMap(model, available || 1200);
    const machinesById = machinesWithRuns(state);
    const local = machinesById.local;
    const reference = local && local.workspace && local.workspace.fingerprint;
    return html`
        <div class="map-scroll" ref=${container}>
            <div class="map-canvas" style=${`width: ${layout.width}px; `
                + `height: ${layout.height}px; zoom: ${layout.scale}`}>
                <${Lines} layout=${layout} machinesById=${machinesById} />
                ${layout.lines.map((line) => html`
                    <div class="map-line-label" style=${`left: ${line.x1
                        + 6}px; top: ${line.y - 28}px`}>
                        <button class="map-network ${selection
                            === `n:${line.network.id}` ? 'selected' : ''}"
                            onClick=${() => onSelect(`n:${line.network.id}`)}>
                            <${Icon} name=${NETWORK_ICONS[line.network.kind]}
                                size=${14} />
                            <b>${networkTitle(line.network, machinesById)}</b>
                            <span class="mono faint">${line.network.cidr}</span>
                            ${line.network.ssids.map((ssid) =>
                                html`<span class="faint">${
                                    `· Wi-Fi “${ssid}”`}</span>`)}
                        </button>
                        ${line.network.internet && html`<span
                            class="map-device internet" title=${
                            `Internet through ${line.network.gateway}`}>
                            <${Icon} name="cloud" size=${13} />internet</span>`}
                        ${line.shown.map((device) => html`<button
                            class="map-device ${device.kind} ${selection
                                === `d:${device.mac}` ? 'selected' : ''}"
                            title=${`${device.address} · ${device.mac}`}
                            onClick=${() => onSelect(`d:${device.mac}`)}>
                            <${Icon} name=${DEVICE_ICONS[device.kind]}
                                size=${13} />
                            <span>${device.label || device.address}</span>
                            ${device.label && html`<span class="mono faint">${
                                device.address}</span>`}
                        </button>`)}
                        ${line.more > 0 && html`<button class="map-device more"
                            onClick=${() => onSelect(`n:${line.network.id}`)}>
                            ${`+${line.more} device${line.more > 1 ? 's' : ''}`}
                        </button>`}
                    </div>`)}
                ${layout.frames.map((frame) => html`<div class="map-frame-label"
                    style=${`left: ${frame.x + 12}px; top: ${layout.boxY
                        + BOX_HEIGHT + 14}px`}>${frame.label}</div>`)}
                ${layout.ports.map((entry) => {
                    const id = `p:${entry.machine.id}/${entry.port.name}`;
                    return html`<button class="map-port ${entry.port.network
                        ? '' : 'loose'} ${entry.port.is_robot_link
                        ? 'robot-link' : ''} ${entry.port.sharing
                        ? 'sharing' : ''} ${selection === id ? 'selected' : ''}"
                        style=${`left: ${entry.x - entry.width / 2}px; `
                            + `top: ${entry.y}px; width: ${entry.width}px`}
                        title=${portTitle(entry.port)}
                        onClick=${() => onSelect(id)}>
                        <${Icon} name=${PORT_ICONS[entry.port.label]}
                            size=${12} />${entry.port.network
                            ? html`<span>${entry.port.name}</span>` : ''}
                    </button>`;
                })}
                ${layout.boxes.map((box) => html`<${MachineBox}
                    key=${box.machine.id} box=${box}
                    entry=${machinesById[box.machine.id]} reference=${reference}
                    isSelected=${selection === `m:${box.machine.id}`}
                    onSelect=${onSelect} />`)}
            </div>
        </div>
        ${layout.offline.length > 0 && html`<div class="map-offline">
            <span class="faint">Offline</span>
            ${layout.offline.map((machine) => html`<button
                class="map-offline-machine ${selection === `m:${machine.id}`
                    ? 'selected' : ''}" onClick=${() => onSelect(
                    `m:${machine.id}`)}>
                <${Icon} name=${MACHINE_ICONS[machine.kind]} size=${14} />
                ${machine.label}<${StatusDot} state=${machine.state} />
            </button>`)}
        </div>`}
        <div class="map-legend faint">
            <span><i class="legend-line"></i>same IP network</span>
            <span><i class="legend-line flow"></i>traffic (faster: busier;
                moving down: mostly incoming)</span>
            <span><i class="legend-line robot-link"></i>robot's link
                (measured)</span>
            <span><i class="legend-line sharing"></i>sharing internet</span>
            <span><i class="legend-line offline"></i>offline</span>
        </div>`;
}


// --- Side panel --------------------------------------------------------------

function Facts({ rows }) {
    return html`<dl class="facts">${rows.filter(([, value]) =>
        value !== null && value !== undefined && value !== '').map(
        ([label, value]) => html`<dt>${label}</dt><dd>${value}</dd>`)}</dl>`;
}

function NetworkPanel({ network, model, state, onSelect }) {
    const machinesById = machinesWithRuns(state);
    const devicesByMac = Object.fromEntries(model.devices.map((device) =>
        [device.mac, device]));
    const devices = network.devices.map((mac) => devicesByMac[mac])
        .filter(Boolean);
    const scanners = network.members.filter((member) => machinesById[
        member.machine_id] && machinesById[member.machine_id].connection
        .state === 'online');
    const [scanner, setScanner] = useState(() => (scanners.find((member) =>
        member.machine_id === 'local') || scanners[0] || {}).machine_id);
    const [scan, setScan] = useState(null);
    const [error, setError] = useState(null);
    const [isBusy, setIsBusy] = useState(false);

    async function runScan() {
        const member = scanners.find((entry) => entry.machine_id === scanner);
        const prefix = Number(network.cidr.split('/')[1]);
        const cidr = prefix >= 22 ? network.cidr : `${member.address.split('.')
            .slice(0, 3).join('.')}.0/24`;
        setIsBusy(true);
        setError(null);
        try {
            setScan(await postJson(`/api/machines/${encodeURIComponent(
                scanner)}/net/scan`, { cidr, ports: [22] }));
        } catch (failure) {
            setError(failure.message);
        }
        setIsBusy(false);
    }
    async function stopSharing() {
        setIsBusy(true);
        try {
            await postJson(`/api/machines/${encodeURIComponent(
                network.share.machine_id)}/sharing/down`,
            { name: network.share.name });
        } catch (failure) {
            setError(failure.message);
        }
        setIsBusy(false);
    }

    return html`
        <h2><${Icon} name=${NETWORK_ICONS[network.kind]} size=${20} />
            ${' '}${networkTitle(network, machinesById)}</h2>
        <${Facts} rows=${[
            ['Range', html`<span class="mono">${network.cidr}</span>`],
            ['Gateway', network.gateway && html`<span class="mono">${
                network.gateway}</span>`],
            ['Internet', network.internet ? 'through the gateway' : null],
            ['Wi-Fi', network.ssids.join(', ')],
            ['Sharing', network.share && `${machinesById[network.share
                .machine_id].machine.label} · ${network.share.port}`],
        ]} />
        ${network.share && html`<div class="button-row">
            <button class="button small danger" disabled=${isBusy}
                onClick=${stopSharing}>Stop sharing</button></div>`}
        <h3>Machines</h3>
        <table class="table compact"><tbody>${network.members.map((member) =>
            html`<tr class="clickable" onClick=${() => onSelect(
                `p:${member.machine_id}/${member.port}`)}>
                <td>${(machinesById[member.machine_id] || {}).machine
                    ? machinesById[member.machine_id].machine.label
                    : member.machine_id}</td>
                <td class="mono faint">${member.port}</td>
                <td class="mono">${member.address}</td>
            </tr>`)}</tbody></table>
        <h3>${`Other devices (${devices.length})`}</h3>
        ${devices.length === 0 && html`<p class="faint small-note">None seen
            yet. A scan fills the machines' neighbour tables.</p>`}
        <table class="table compact"><tbody>${devices.map((device) => html`
            <tr class="clickable" onClick=${() => onSelect(`d:${device.mac}`)}>
                <td><${Icon} name=${DEVICE_ICONS[device.kind]} size=${13} />
                    ${' '}${device.label || html`<span class="faint">unnamed
                    </span>`}</td>
                <td class="mono">${device.address}</td>
                <td class="mono faint">${device.mac}</td>
            </tr>`)}</tbody></table>
        ${scanners.length > 0 && html`<div class="fields">
            <label class="field">
                <span>Scan from</span>
                <select class="input" value=${scanner}
                    onChange=${(event) => setScanner(event.target.value)}>
                    ${scanners.map((member) => html`<option
                        value=${member.machine_id}>${machinesById[
                            member.machine_id].machine.label}</option>`)}
                </select>
            </label>
            <div class="field"><span>${' '}</span>
                <button class="button" disabled=${isBusy} onClick=${runScan}>
                    <${Icon} name="scan-search" size=${14} />${' '}${isBusy
                        ? 'Scanning…' : 'Scan for devices'}</button></div>
        </div>`}
        ${scan && html`<p class="faint small-note">${`Found ${
            scan.hosts.length} in ${scan.network} (${scan.duration_s.toFixed(
            1)} s); they show up on the map.`}</p>`}
        ${error && html`<div class="error-banner">${error}</div>`}`;
}

function SharePort({ machineEntry, port }) {
    const status = machineEntry.sharing;
    const [subnet, setSubnet] = useState('');
    const [error, setError] = useState(null);
    const [isBusy, setIsBusy] = useState(false);
    if (!status || !status.supported || port.kind !== 'ethernet'
            || port.is_default_route) {
        return null;
    }
    const session = status.sessions.find((entry) =>
        entry.downstream === port.name);
    const path = (action) => `/api/machines/${encodeURIComponent(
        machineEntry.machine.id)}/sharing/${action}`;
    async function act(action, body) {
        setIsBusy(true);
        setError(null);
        try {
            await postJson(path(action), body);
        } catch (failure) {
            setError(failure.message);
        }
        setIsBusy(false);
    }
    return html`
        <h3><${Icon} name="share-2" size=${15} />${' '}Internet sharing</h3>
        ${session ? html`
            <p class="small-note">${`Sharing ${machineEntry.machine.label}'s
                internet here: ${session.subnet}, ${(session.devices || [])
                .length} device(s).`}</p>
            <button class="button small danger" disabled=${isBusy}
                onClick=${() => act('down', { name: session.name })}>
                Stop sharing</button>`
        : !isReady(status) ? html`<${HelperNotice}
                machineId=${machineEntry.machine.id} status=${status} />`
            : html`<p class="small-note faint">Give devices plugged into this
                port addresses and this machine's internet.</p>
            <div class="fields">
                <label class="field">
                    <span>Subnet</span>
                    <input class="input mono" value=${subnet}
                        placeholder=${status.suggested_subnet}
                        onInput=${(event) => setSubnet(event.target.value)} />
                </label>
                <div class="field"><span>${' '}</span>
                    <button class="button primary" disabled=${isBusy}
                        onClick=${() => act('up', { downstream: port.name,
                            name: status.sessions.length ? `share-${port.name}`
                                .toLowerCase().replace(/[^a-z0-9-]/g, '-')
                                .slice(0, 20) : 'share',
                            subnet: subnet || status.suggested_subnet })}>
                        Share internet here</button></div>
            </div>`}
        ${error && html`<div class="error-banner">${error}</div>`}`;
}

function PortPanel({ machine, port, state, onSelect }) {
    const entry = state.machines.find((other) =>
        other.machine.id === machine.id);
    const wifi = port.wifi || {};
    const [error, setError] = useState(null);
    const measure = (value) => postJson('/api/settings',
        { budget: { interface: value } })
        .catch((failure) => setError(failure.message));
    const points = trafficStore.points(machine.id, port.name);
    const channelNote = wifi.connected && wifi.channel != null
        ? (wifi.channel === 1 ? 'arena channel'
            : wifi.frequency_mhz && wifi.frequency_mhz > 4000
                ? '5 GHz (pits)' : 'not the arena channel (1)') : null;
    return html`
        <h2><${Icon} name=${PORT_ICONS[port.label]} size=${20} />${' '}${
            `${port.label} ${port.name}`}</h2>
        <p class="muted">${'on '}<a class="link-button" onClick=${() =>
            onSelect(`m:${machine.id}`)}>${machine.label}</a></p>
        <${Facts} rows=${[
            ['Addresses', port.addresses.length ? html`<span class="mono">${
                port.addresses.join(', ')}</span>` : 'none'],
            ['MAC', port.mac && html`<span class="mono">${port.mac}</span>`],
            ['Network', port.network && html`<a class="link-button"
                onClick=${() => onSelect(`n:${port.network}`)}>${
                port.network}</a>`],
            ['State', `${port.is_up ? 'up' : 'down'}${port.is_default_route
                ? " · this machine's internet" : ''}`],
            ['Traffic', port.rx_bps != null ? `down ${formatKbps(
                port.rx_bps / 1000)} · up ${formatKbps(port.tx_bps / 1000)}`
                : null],
            ['Wi-Fi', port.kind === 'wifi' ? (wifi.connected ? `“${wifi.ssid
                || 'hidden'}” · channel ${wifi.channel ?? '?'}${wifi.band
                ? ` · ${wifi.band}` : ''}${channelNote ? ` · ${channelNote}`
                : ''}` : 'not connected') : null],
            ['Signal', wifi.signal_dbm != null ? `${wifi.signal_dbm} dBm`
                : wifi.signal_percent != null ? `${wifi.signal_percent}%`
                    : null],
        ]} />
        ${points.length > 1 && html`<div class="port-sparkline">
            <${Sparkline} points=${points} /><span class="faint">last 3 min
            </span></div>`}
        ${['ethernet', 'wifi'].includes(port.kind) && html`
            <h3><${Icon} name="gauge" size=${15} />${' '}Robot's link</h3>
            ${port.is_robot_link
                ? html`<p class="small-note">The bandwidth budget measures this
                    port.</p><button class="button small"
                    onClick=${() => measure(null)}>Stop measuring</button>`
                : html`<p class="small-note faint">Measure the competition
                    bandwidth budget on this port: pick the robot computer's
                    link to the arena network.</p><button class="button small"
                    onClick=${() => measure({ machine_id: machine.id,
                        name: port.name })}>Measure as the robot's link
                    </button>`}`}
        ${entry && html`<${SharePort} machineEntry=${entry} port=${port} />`}
        ${error && html`<div class="error-banner">${error}</div>`}`;
}

function DevicePanel({ device, state, now, onSelect }) {
    const machinesById = machinesWithRuns(state);
    return html`
        <h2><${Icon} name=${DEVICE_ICONS[device.kind]} size=${20} />${' '}${
            device.label || device.address}</h2>
        <${Facts} rows=${[
            ['Address', html`<span class="mono">${device.address}</span>`],
            ['MAC', html`<span class="mono">${device.mac}</span>`],
            ['Names', device.names.join(', ')],
            ['Network', html`<a class="link-button" onClick=${() =>
                onSelect(`n:${device.network}`)}>${device.network}</a>`],
            ['Seen', `${formatAgo(device.seen_s, now)} · ${device.sources
                .join(', ')}`],
            ['Seen by', device.seen_by.map((id) => (machinesById[id]
                || { machine: { label: id } }).machine.label).join(', ')],
        ]} />
        ${device.kind !== 'router' && html`<div class="button-row">
            <button class="button primary" onClick=${() => startAddMachine({
                address: device.address, hostname: device.names[0],
                mac: device.mac })}><${Icon} name="plus" size=${14} />
                ${' '}Add as a machine</button>
        </div>`}`;
}

function SidePanel({ selection, state, model, now, onSelect, placement,
    onPlacement }) {
    const reference = useRef(null);
    useEffect(() => {
        if (placement === 'below' && reference.current) {
            reference.current.scrollIntoView({ behavior: 'smooth',
                block: 'nearest' });
        }
    }, [selection, placement]);
    const [kind, ...rest] = selection.split(':');
    const id = rest.join(':');
    let body = null;
    if (kind === 'm') {
        const machinesById = machinesWithRuns(state);
        const entry = machinesById[id];
        const local = machinesById.local;
        if (entry) {
            body = html`
                <${MachineCard} entry=${entry} machinesById=${machinesById}
                    reference=${local && local.workspace
                        && local.workspace.fingerprint} now=${now} />
                ${entry.connection.state === 'online' && html`<${ToolsPanel}
                    key=${id} state=${state} machineId=${id}
                    title="Ping, check a port or scan from here" />`}`;
        }
    } else if (kind === 'n') {
        const network = model.networks.find((entry) => entry.id === id);
        if (network) {
            body = html`<${NetworkPanel} key=${id} network=${network}
                model=${model} state=${state} onSelect=${onSelect} />`;
        }
    } else if (kind === 'p') {
        const [machineId, portName] = id.split('/');
        const machine = model.machines.find((entry) =>
            entry.id === machineId);
        const port = machine && machine.ports.find((entry) =>
            entry.name === portName);
        if (port) {
            body = html`<${PortPanel} key=${id} machine=${machine}
                port=${port} state=${state} onSelect=${onSelect} />`;
        }
    } else if (kind === 'd') {
        const device = model.devices.find((entry) => entry.mac === id);
        if (device) {
            body = html`<${DevicePanel} device=${device} state=${state}
                now=${now} onSelect=${onSelect} />`;
        }
    } else if (kind === 'budget') {
        body = html`<${BudgetPanel} state=${state} now=${now} />`;
    }
    const heading = { m: 'Machine', n: 'Network', p: 'Port', d: 'Device',
        budget: 'Bandwidth budget' }[kind] || '';
    return html`
        <aside class="side-panel ${placement}" ref=${reference}>
            <div class="side-head">
                <span class="faint">${heading}</span>
                <span class="spacer"></span>
                <button class="icon-button" title=${placement === 'below'
                    ? 'Show beside the map' : 'Show below the map'}
                    onClick=${() => onPlacement(placement === 'below'
                        ? 'side' : 'below')}><${Icon} name=${placement
                        === 'below' ? 'arrow-left-right' : 'layers'}
                        size=${14} /></button>
                <button class="icon-button" title="Close"
                    onClick=${() => onSelect(null)}><${Icon} name="x"
                    size=${14} /></button>
            </div>
            <div class=${kind === 'm' || kind === 'budget' ? 'side-cards'
                : 'panel side-body'}>
                ${body || html`<p class="faint">It's no longer on the map.</p>`}
            </div>
        </aside>`;
}


// --- Map with its panel ------------------------------------------------------

// Where the details panel goes: below the map on ordinary screens (the map
// is wide and short), beside it on very wide ones; the panel's header can
// switch it, and that choice is remembered.
const PLACEMENT_KEY = 'orca.map-panel';

function defaultPlacement() {
    const saved = window.localStorage.getItem(PLACEMENT_KEY);
    if (saved === 'side' || saved === 'below') return saved;
    return window.innerWidth >= 1700
        && window.innerWidth / window.innerHeight >= 2 ? 'side' : 'below';
}

export function NetworkMap({ state, now, selection, onSelect }) {
    const [placement, setPlacement] = useState(defaultPlacement);
    const model = state.network;
    if (!model) return html`<div class="placeholder">Building the map…</div>`;
    function choosePlacement(next) {
        window.localStorage.setItem(PLACEMENT_KEY, next);
        setPlacement(next);
    }
    return html`
        <div class="map-layout ${selection && placement === 'side'
            ? 'with-panel' : ''}">
            <div class="map-main">
                <${MapView} state=${state} now=${now} model=${model}
                    selection=${selection} onSelect=${onSelect} />
            </div>
            ${selection && html`<${SidePanel} selection=${selection}
                state=${state} model=${model} now=${now}
                onSelect=${onSelect} placement=${placement}
                onPlacement=${choosePlacement} />`}
        </div>`;
}
