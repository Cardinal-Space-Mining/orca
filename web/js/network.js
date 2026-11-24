// The network page: competition mode, the comm-link bandwidth budget, live
// traffic per interface, Wi-Fi channels, and ping / port / scan tools.
//
// Chart colours follow the data-viz palette's dark steps (validated on this
// surface): series 1 blue, series 2 orange; status colours for the meter.

import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import { html, formatDuration, postJson, StatusDot } from './common.js';
import { trafficStore } from './traffic.js';


const SOURCE_STYLE = {
    interface: { label: 'Radio link', color: '#3987e5',
        help: 'byte counters of the robot\'s link interface: everything on '
            + 'the link, overhead and the hub included' },
    net_adapter: { label: 'net_adapter', color: '#d95926',
        help: 'payload net_adapter reports, per topic, without protocol '
            + 'overhead' },
};
const SOURCE_ORDER = ['interface', 'net_adapter'];
const PORT_PRESETS = [
    [22, 'SSH'], [7447, 'zenoh (net_adapter)'], [2111, 'lidar SOPAS'],
    [8765, 'Foxglove'],
];


// --- Formatting --------------------------------------------------------------

function formatKbps(kbps) {
    if (kbps == null) return '–';
    if (kbps >= 10000) return `${(kbps / 1000).toFixed(1)} Mbps`;
    if (kbps > 0 && kbps < 10) return `${kbps.toFixed(1)} kbps`;
    return `${Math.round(kbps).toLocaleString()} kbps`;
}

// Axis ticks: in full under 10,000, compact above (10K, 20K).
function tickLabel(kbps) {
    return kbps >= 10000 ? `${Math.round(kbps / 1000)}K`
        : Math.round(kbps).toLocaleString();
}

// A round step (1, 2, 2.5 or 5 x 10^n) giving about `count` ticks.
function niceStep(maximum, count) {
    const rough = maximum / count;
    const magnitude = 10 ** Math.floor(Math.log10(Math.max(rough, 1e-9)));
    for (const step of [1, 2, 2.5, 5, 10]) {
        if (step * magnitude >= rough) return step * magnitude;
    }
    return 10 * magnitude;
}

function budgetLevel(ratio) {
    if (ratio == null) return { level: '', label: 'no data' };
    if (ratio >= 1) return { level: 'bad', label: 'over the limit' };
    if (ratio >= 0.75) return { level: 'warn', label: 'close to the limit' };
    return { level: 'good', label: 'within the limit' };
}

function useWidth(reference) {
    const [width, setWidth] = useState(600);
    useLayoutEffect(() => {
        const observer = new ResizeObserver(([entry]) =>
            setWidth(Math.max(200, entry.contentRect.width)));
        observer.observe(reference.current);
        return () => observer.disconnect();
    }, []);
    return width;
}


// --- Competition mode --------------------------------------------------------

function CompetitionPanel({ state }) {
    const isOn = state.settings.competition_mode;
    const hubKbps = state.machines.reduce((total, entry) =>
        total + (entry.machine.is_local ? 0
            : entry.connection.hub_kbps || 0), 0);
    return html`
        <section class="panel competition ${isOn ? 'on' : ''}">
            <div class="run-head">
                <div>
                    <h2>Competition mode</h2>
                    <p class="muted">Keeps the hub's own traffic on the robot's
                        link small: status every 10 s instead of 2 s, and
                        output from other machines' processes only on request
                        (errors still show when you open a run).</p>
                </div>
                <label class="switch">
                    <input type="checkbox" checked=${isOn}
                        onChange=${(event) => postJson('/api/settings',
                            { competition_mode: event.target.checked })} />
                    <span></span>${isOn ? 'On' : 'Off'}
                </label>
            </div>
            <div class="muted">Hub traffic to other machines right now:
                <b>${' ' + formatKbps(hubKbps)}</b> (payload; SSH adds a few
                percent)</div>
        </section>`;
}


// --- Budget: meter and tiles -------------------------------------------------

function BudgetMeter({ budget }) {
    const primary = budget.sources.interface.average_kbps != null
        ? 'interface' : 'net_adapter';
    const average = budget.sources[primary].average_kbps;
    const ratio = average == null ? null : average / budget.limit_kbps;
    const { level, label } = budgetLevel(ratio);
    const icon = { good: '✓', warn: '!', bad: '✕' }[level] || '·';
    return html`
        <div class="budget-meter">
            <div class="budget-figure">
                <span class="hero">${average == null ? '–'
                    : Math.round(average).toLocaleString()}</span>
                <span class="muted">${`kbps average of ${
                    budget.limit_kbps.toLocaleString()} allowed`}</span>
            </div>
            <div class="meter-track" role="meter" aria-valuemin="0"
                aria-valuemax=${budget.limit_kbps} aria-valuenow=${average || 0}
                aria-label="Average bandwidth against the limit">
                <div class="meter-fill ${level}" style=${`width: ${Math.min(
                    100, 100 * (ratio || 0))}%`}></div>
            </div>
            <div class="budget-status ${level}"><span class="status-icon">
                ${icon}</span>${label}${ratio != null
                    ? ` · ${Math.round(ratio * 100)}% · ${
                        SOURCE_STYLE[primary].label}` : ''}</div>
        </div>`;
}

function SourceTiles({ budget }) {
    return html`
        <div class="source-tiles">
            ${SOURCE_ORDER.map((source) => {
                const summary = budget.sources[source];
                return html`
                    <div class="source-tile">
                        <div class="source-name"><span class="line-key"
                            style=${`background: ${SOURCE_STYLE[source].color}`}
                            ></span>${SOURCE_STYLE[source].label}</div>
                        <div class="tile-row">
                            <div><span>Now</span><b>${formatKbps(
                                summary.current_kbps)}</b></div>
                            <div><span>Average</span><b>${formatKbps(
                                summary.average_kbps)}</b></div>
                            <div><span>Peak</span><b>${formatKbps(
                                summary.seconds ? summary.peak_kbps : null)}
                                </b></div>
                        </div>
                        <div class="faint small-text">${SOURCE_STYLE[source].help}
                        </div>
                    </div>`;
            })}
        </div>`;
}


// --- Budget: chart -----------------------------------------------------------

function nearestPoint(points, time) {
    let best = null;
    for (const point of points) {
        if (!best || Math.abs(point[0] - time) < Math.abs(best[0] - time)) {
            best = point;
        }
    }
    return best;
}

function BudgetChart({ budget }) {
    const container = useRef(null);
    const width = useWidth(container);
    const [hoverTime, setHoverTime] = useState(null);
    const [showTable, setShowTable] = useState(false);
    const height = 220;
    const margin = { top: 14, right: 96, bottom: 24, left: 52 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;

    const now = Date.now() / 1000;
    const history = budget.history || {};
    const series = SOURCE_ORDER.map((source) => ({
        source, points: history[source] || [] }))
        .filter((entry) => entry.points.length > 0);
    const start = Math.max(budget.window_start_s, now - 15 * 60,
        Math.min(...series.map((entry) => entry.points[0][0]), now - 60));
    const largest = Math.max(budget.limit_kbps * 1.1,
        ...series.flatMap((entry) => entry.points.map((point) => point[1])));
    const step = niceStep(largest, 4);
    const maximum = Math.ceil(largest / step) * step;
    const x = (time) => margin.left + (time - start) / (now - start)
        * plotWidth;
    const y = (kbps) => margin.top + plotHeight - kbps / maximum * plotHeight;
    const ticks = Array.from({ length: Math.round(maximum / step) + 1 },
        (_, index) => index * step);

    const path = (points) => points.filter((point) => point[0] >= start)
        .map((point, index) => `${index ? 'L' : 'M'}${x(point[0]).toFixed(1)},${
            y(point[1]).toFixed(1)}`).join('');
    const ends = series.map((entry) => ({ ...entry,
        last: entry.points[entry.points.length - 1] }));
    // Direct end labels only when they don't collide; the legend always
    // names the series.
    const labelsFit = ends.length < 2 || Math.abs(
        y(ends[0].last[1]) - y(ends[1].last[1])) > 16;

    function onPointerMove(event) {
        const box = event.currentTarget.getBoundingClientRect();
        const time = start + (event.clientX - box.left - margin.left)
            / plotWidth * (now - start);
        setHoverTime(time >= start && time <= now ? time : null);
    }

    const hover = hoverTime == null ? null : series.map((entry) => ({
        ...entry, point: nearestPoint(entry.points, hoverTime) }))
        .filter((entry) => entry.point);

    return html`
        <div class="chart-card" ref=${container}>
            <div class="chart-legend">
                ${series.map((entry) => html`<span><span class="line-key"
                    style=${`background: ${SOURCE_STYLE[entry.source].color}`}
                    ></span>${SOURCE_STYLE[entry.source].label}</span>`)}
                <span class="faint">${`kbps, since ${new Date(start * 1000)
                    .toLocaleTimeString()}`}</span>
                <span class="spacer"></span>
                <button class="link-button" onClick=${() =>
                    setShowTable(!showTable)}>${showTable ? 'Chart' : 'Table'}
                    </button>
            </div>
            ${series.length === 0 ? html`<div class="chart-empty">No samples
                yet: choose a link interface or start the net_adapter monitor
                below.</div>`
            : showTable ? html`<${BudgetTable} series=${series} />`
            : html`
                <svg width=${width} height=${height} class="chart"
                    onPointerMove=${onPointerMove}
                    onPointerLeave=${() => setHoverTime(null)}>
                    ${ticks.map((tick) => html`
                        <line class="grid" x1=${margin.left}
                            x2=${margin.left + plotWidth} y1=${y(tick)}
                            y2=${y(tick)} />
                        <text class="axis-label" x=${margin.left - 8}
                            y=${y(tick) + 4} text-anchor="end">
                            ${tickLabel(tick)}</text>`)}
                    <line class="limit" x1=${margin.left}
                        x2=${margin.left + plotWidth} y1=${y(budget.limit_kbps)}
                        y2=${y(budget.limit_kbps)} />
                    <text class="limit-label" x=${margin.left + 6}
                        y=${y(budget.limit_kbps) - 6}>${`limit ${
                        budget.limit_kbps.toLocaleString()}`}</text>
                    ${series.map((entry) => html`<path d=${path(entry.points)}
                        class="series" stroke=${SOURCE_STYLE[entry.source].color}
                        />`)}
                    ${ends.map((entry) => html`
                        <circle cx=${x(entry.last[0])} cy=${y(entry.last[1])}
                            r="4" fill=${SOURCE_STYLE[entry.source].color}
                            class="end-dot" />
                        ${labelsFit && html`<text class="end-label"
                            x=${x(entry.last[0]) + 9} y=${y(entry.last[1]) + 4}>
                            ${formatKbps(entry.last[1])}</text>`}`)}
                    ${hover && hover.length > 0 && html`
                        <line class="crosshair" x1=${x(hover[0].point[0])}
                            x2=${x(hover[0].point[0])} y1=${margin.top}
                            y2=${margin.top + plotHeight} />
                        ${hover.map((entry) => html`<circle
                            cx=${x(entry.point[0])} cy=${y(entry.point[1])}
                            r="4" fill=${SOURCE_STYLE[entry.source].color}
                            class="end-dot" />`)}`}
                </svg>
                ${hover && hover.length > 0 && html`
                    <div class="chart-tooltip" style=${`left: ${Math.min(
                        x(hover[0].point[0]) + 12, width - 190)}px`}>
                        <div class="faint">${new Date(hover[0].point[0] * 1000)
                            .toLocaleTimeString()}</div>
                        ${hover.map((entry) => html`<div class="tooltip-row">
                            <span class="line-key" style=${`background: ${
                                SOURCE_STYLE[entry.source].color}`}></span>
                            <b>${formatKbps(entry.point[1])}</b>
                            <span class="muted">${SOURCE_STYLE[entry.source]
                                .label}</span></div>`)}
                    </div>`}`}
        </div>`;
}

function BudgetTable({ series }) {
    const times = [...new Set(series.flatMap((entry) =>
        entry.points.map((point) => Math.round(point[0]))))]
        .sort((a, b) => b - a).slice(0, 40);
    const valueAt = (entry, time) => {
        const point = nearestPoint(entry.points, time);
        return point && Math.abs(point[0] - time) < 1.5 ? point[1] : null;
    };
    return html`
        <div class="table-scroll"><table class="table">
            <thead><tr><th>Time</th>${series.map((entry) => html`<th>${
                SOURCE_STYLE[entry.source].label}</th>`)}</tr></thead>
            <tbody>${times.map((time) => html`<tr>
                <td>${new Date(time * 1000).toLocaleTimeString()}</td>
                ${series.map((entry) => html`<td>${formatKbps(
                    valueAt(entry, time))}</td>`)}</tr>`)}</tbody>
        </table></div>`;
}


// --- Budget: net_adapter breakdown -------------------------------------------

function ChannelBars({ budget }) {
    const channels = budget.channels.filter((channel) => channel.kbps > 0.05);
    if (channels.length === 0) return null;
    const maximum = Math.max(...channels.map((channel) => channel.kbps));
    return html`
        <div class="section-head"><span>net_adapter by topic</span>
            ${budget.latency_ms != null && html`<span>${`latency ${
                budget.latency_ms} ms`}</span>`}</div>
        <div class="channel-bars">
            ${channels.slice(0, 10).map((channel) => html`
                <div class="channel-row" title=${`${channel.msgs_per_s
                    .toFixed(1)} messages/s`}>
                    <span class="mono channel-name">${channel.topic}
                        <span class="faint">${' ' + channel.direction}</span>
                    </span>
                    <span class="channel-track"><span class="channel-bar"
                        style=${`width: ${100 * channel.kbps / maximum}%`}>
                        </span></span>
                    <span class="channel-value">${formatKbps(channel.kbps)}
                        </span>
                </div>`)}
        </div>`;
}


// --- Budget: sources ---------------------------------------------------------

function linkInterfaceOptions(machines) {
    const options = [];
    for (const entry of machines) {
        if (entry.connection.state !== 'online' || !entry.stats) continue;
        for (const iface of entry.stats.interfaces || []) {
            if (iface.rx_bytes == null || (!iface.is_up
                    && iface.ipv4.length === 0)) {
                continue;
            }
            options.push({ value: `${entry.machine.id}/${iface.name}`,
                label: `${entry.machine.label} · ${iface.name}${
                    iface.kind === 'wifi' ? ' (Wi-Fi)' : ''}` });
        }
    }
    return options;
}

function BudgetSources({ state }) {
    const budget = state.budget;
    const settings = budget.settings;
    const [monitorMachine, setMonitorMachine] = useState(
        settings.monitor_machine_id || 'local');
    const [error, setError] = useState(null);
    const chosen = settings.interface
        ? `${settings.interface.machine_id}/${settings.interface.name}` : '';
    const monitor = budget.monitor;
    const withWorkspace = state.machines.filter((entry) =>
        entry.machine.workspace && entry.connection.state === 'online');

    const save = (changes) => postJson('/api/settings', { budget: changes })
        .catch((failure) => setError(failure.message));
    async function toggleMonitor() {
        setError(null);
        try {
            await postJson('/api/budget/monitor', monitor.state === 'stopped'
                ? { machine_id: monitorMachine } : {});
        } catch (failure) {
            setError(failure.message);
        }
    }

    return html`
        <div class="budget-sources">
            <label class="field">
                <span>Robot's link interface</span>
                <select class="input" value=${chosen}
                    onChange=${(event) => {
                        const [machineId, ...name] = event.target.value
                            .split('/');
                        save({ interface: event.target.value ? {
                            machine_id: machineId, name: name.join('/') }
                            : null });
                    }}>
                    <option value="">not measured</option>
                    ${linkInterfaceOptions(state.machines).map((option) =>
                        html`<option value=${option.value}>${option.label}
                            </option>`)}
                </select>
            </label>
            <label class="field">
                <span>NASA cameras on (500 kbps each)</span>
                <select class="input" value=${settings.num_cameras}
                    onChange=${(event) => save(
                        { num_cameras: Number(event.target.value) })}>
                    ${[0, 1, 2, 3].map((count) => html`<option
                        value=${count}>${count}</option>`)}
                </select>
            </label>
            <label class="field">
                <span>net_adapter monitor on</span>
                <div class="button-row tight">
                    <select class="input" value=${monitorMachine}
                        disabled=${monitor.state !== 'stopped'}
                        onChange=${(event) => setMonitorMachine(
                            event.target.value)}>
                        ${withWorkspace.map((entry) => html`<option
                            value=${entry.machine.id}>${entry.machine.label}
                            </option>`)}
                    </select>
                    <button class="button" onClick=${toggleMonitor}>
                        ${monitor.state === 'stopped' ? 'Start' : 'Stop'}
                        </button>
                    <span class="pill ${monitor.state === 'receiving' ? 'good'
                        : monitor.state === 'stopped' ? '' : 'warn'}">
                        ${monitor.state}</span>
                </div>
            </label>
            <label class="check">
                <input type="checkbox" checked=${settings.monitor_local_dds}
                    onChange=${(event) => save(
                        { monitor_local_dds: event.target.checked })} />
                The launch used <span class="mono">--local</span> DDS</label>
            <button class="button" onClick=${() =>
                postJson('/api/budget/reset')}>Start a new average</button>
            ${(error || monitor.error) && html`<div class="error-text">
                ${error || monitor.error}</div>`}
        </div>`;
}

function BudgetPanel({ state, now }) {
    const budget = state.budget;
    return html`
        <section class="panel">
            <div class="run-head">
                <div>
                    <h2>Bandwidth budget</h2>
                    <p class="muted">The comm link must average at most
                        ${' ' + budget.limit_kbps.toLocaleString()} kbps over a
                        run (guidebook 14.1.19; NASA cameras count 500 kbps
                        each). Averaging since ${formatDuration(now
                            - budget.window_start_s)} ago.</p>
                </div>
            </div>
            <div class="budget-top">
                <${BudgetMeter} budget=${budget} />
                <${SourceTiles} budget=${budget} />
            </div>
            <${BudgetChart} budget=${budget} />
            <${ChannelBars} budget=${budget} />
            <${BudgetSources} state=${state} />
        </section>`;
}


// --- Traffic and Wi-Fi -------------------------------------------------------

function Sparkline({ points }) {
    if (points.length < 2) return html`<span class="faint">–</span>`;
    const width = 120;
    const height = 26;
    const maximum = Math.max(...points.map((point) => point[1]), 1);
    const first = points[0][0];
    const span = points[points.length - 1][0] - first || 1;
    const d = points.map((point, index) => `${index ? 'L' : 'M'}${(
        (point[0] - first) / span * (width - 4) + 2).toFixed(1)},${(
        height - 3 - point[1] / maximum * (height - 6)).toFixed(1)}`).join('');
    return html`<svg width=${width} height=${height} class="sparkline">
        <path d=${d} /></svg>`;
}

function TrafficPanel({ state }) {
    const rows = [];
    for (const entry of state.machines) {
        if (entry.connection.state !== 'online' || !entry.stats) continue;
        for (const iface of entry.stats.interfaces || []) {
            if (iface.rx_bps == null || (!iface.is_up && !iface.ipv4.length)) {
                continue;
            }
            rows.push({ entry, iface });
        }
    }
    return html`
        <section class="panel">
            <h2>Traffic</h2>
            <p class="muted">Every interface on every machine, from its byte
                counters (everything, not just robot code). Hub column: the
                hub's own traffic with that machine.</p>
            <table class="table">
                <thead><tr><th>Machine</th><th>Interface</th><th>Address</th>
                    <th>Down</th><th>Up</th><th>Last 3 min</th><th>Hub</th>
                </tr></thead>
                <tbody>${rows.map(({ entry, iface }, index) => html`
                    <tr key=${`${entry.machine.id}/${iface.name}`}>
                        <td>${index === 0 || rows[index - 1].entry !== entry
                            ? entry.machine.label : ''}</td>
                        <td class="mono">${iface.name}${iface.kind === 'wifi'
                            ? html`<span class="tag">wifi</span>` : ''}</td>
                        <td class="mono muted">${iface.ipv4.join(', ')}</td>
                        <td>${formatKbps(iface.rx_bps / 1000)}</td>
                        <td>${formatKbps(iface.tx_bps / 1000)}</td>
                        <td><${Sparkline} points=${trafficStore.points(
                            entry.machine.id, iface.name)} /></td>
                        <td class="muted">${index === 0
                            || rows[index - 1].entry !== entry
                            ? (entry.machine.is_local ? '–'
                                : formatKbps(entry.connection.hub_kbps)) : ''}
                            </td>
                    </tr>`)}</tbody>
            </table>
        </section>`;
}

function WifiPanel({ state }) {
    const rows = [];
    for (const entry of state.machines) {
        for (const iface of (entry.stats || {}).interfaces || []) {
            if (iface.kind === 'wifi') rows.push({ entry, iface });
        }
    }
    return html`
        <section class="panel">
            <h2>Wi-Fi</h2>
            <p class="muted">Arena runs are on channel 1 (2.4 GHz); the pits
                use 5 GHz or Ethernet (guidebook 14.2 and 14.4). macOS hides
                network names.</p>
            <table class="table">
                <thead><tr><th>Machine</th><th>Interface</th><th>Network</th>
                    <th>Channel</th><th>Band</th><th>Signal</th></tr></thead>
                <tbody>${rows.map(({ entry, iface }) => {
                    const wifi = iface.wifi || {};
                    const signal = wifi.signal_dbm != null
                        ? `${wifi.signal_dbm} dBm`
                        : wifi.signal_percent != null
                            ? `${wifi.signal_percent}%` : '–';
                    return html`<tr>
                        <td>${entry.machine.label}</td>
                        <td class="mono">${iface.name}</td>
                        <td>${!wifi.connected ? html`<span class="faint">
                            not connected</span>` : wifi.ssid
                            || html`<span class="faint">hidden</span>`}</td>
                        <td>${wifi.channel ?? '–'}</td>
                        <td>${wifi.band ? wifi.band.replace(/^\d+\s*\(?|\)$/g,
                            '') : wifi.frequency_mhz
                            ? `${wifi.frequency_mhz} MHz` : '–'}</td>
                        <td>${signal}</td>
                    </tr>`;
                })}</tbody>
            </table>
        </section>`;
}


// --- Tools -------------------------------------------------------------------

function defaultNetwork(entry) {
    const stats = entry && entry.stats;
    if (!stats) return '';
    const route = stats.default_route || {};
    const iface = (stats.interfaces || []).find((candidate) =>
        candidate.name === route.interface) || (stats.interfaces || []).find(
        (candidate) => candidate.ipv4.length);
    if (!iface || !iface.ipv4.length) return '';
    const [address] = iface.ipv4[0].split('/');
    return `${address.split('.').slice(0, 3).join('.')}.0/24`;
}

function ToolsPanel({ state }) {
    const online = state.machines.filter((entry) =>
        entry.connection.state === 'online');
    const [machineId, setMachineId] = useState('local');
    const source = online.find((entry) => entry.machine.id === machineId)
        || online[0];
    const [host, setHost] = useState('');
    const [port, setPort] = useState('7447');
    const [cidr, setCidr] = useState('');
    const [ports, setPorts] = useState({ 22: true });
    const [result, setResult] = useState(null);
    const [busy, setBusy] = useState(null);

    useEffect(() => setCidr(defaultNetwork(source)), [source && source
        .machine.id]);

    async function run(tool, body) {
        setBusy(tool);
        setResult(null);
        try {
            const answer = await postJson(`/api/machines/${encodeURIComponent(
                source.machine.id)}/net/${tool}`, body);
            setResult({ tool, answer });
        } catch (failure) {
            setResult({ tool, error: failure.message });
        }
        setBusy(null);
    }

    // Addresses the inventory knows: its routes, and every address the
    // connected machines report (Wi-Fi, host-only, this machine...).
    const knownAddresses = {};
    for (const entry of state.machines) {
        for (const route of entry.machine.routes || []) {
            knownAddresses[route.split(' · ').pop()] = entry.machine.label;
        }
        for (const iface of (entry.stats || {}).interfaces || []) {
            for (const cidr of iface.ipv4) {
                knownAddresses[cidr.split('/')[0]] =
                    `${entry.machine.label} (${iface.name})`;
            }
        }
    }

    return html`
        <section class="panel">
            <h2>Tools</h2>
            <p class="muted">Run from any machine: what it can reach is what the
                robot code on it can reach.</p>
            <div class="fields">
                <label class="field">
                    <span>From</span>
                    <select class="input" value=${source && source.machine.id}
                        onChange=${(event) => setMachineId(event.target.value)}>
                        ${online.map((entry) => html`<option
                            value=${entry.machine.id}>${entry.machine.label}
                            </option>`)}
                    </select>
                </label>
                <label class="field">
                    <span>Host</span>
                    <input class="input mono" value=${host}
                        placeholder="10.11.11.15"
                        onInput=${(event) => setHost(event.target.value)} />
                </label>
                <div class="field"><span>${'\u00a0'}</span>
                    <button class="button" disabled=${!host || busy}
                        onClick=${() => run('ping', { host, count: 4 })}>
                        ${busy === 'ping' ? 'Pinging…' : 'Ping'}</button></div>
                <label class="field short-field">
                    <span>Port</span>
                    <input class="input mono" value=${port}
                        onInput=${(event) => setPort(event.target.value)} />
                </label>
                <div class="field"><span>${'\u00a0'}</span>
                    <button class="button" disabled=${!host || !port || busy}
                        onClick=${() => run('tcp', { host, port })}>
                        ${busy === 'tcp' ? 'Checking…' : 'Check port'}</button>
                </div>
            </div>
            <div class="fields">
                <label class="field">
                    <span>Scan network</span>
                    <input class="input mono" value=${cidr}
                        onInput=${(event) => setCidr(event.target.value)} />
                </label>
                <div class="field"><span>For</span>
                    <div class="port-checks">${PORT_PRESETS.map(
                        ([value, label]) => html`<label class="check"><input
                            type="checkbox"
                            checked=${!!ports[value]} onChange=${() =>
                                setPorts({ ...ports, [value]: !ports[value] })}
                            />${`${value} ${label}`}</label>`)}</div>
                </div>
                <div class="field"><span>${'\u00a0'}</span>
                    <button class="button" disabled=${!cidr || busy}
                        onClick=${() => run('scan', { cidr, ports: Object
                            .keys(ports).filter((key) => ports[key])
                            .map(Number) })}>
                        ${busy === 'scan' ? 'Scanning…' : 'Scan'}</button></div>
            </div>
            ${result && html`<${ToolResult} result=${result}
                knownAddresses=${knownAddresses} />`}
        </section>`;
}

function ToolResult({ result, knownAddresses }) {
    if (result.error) return html`<div class="error-banner">${result.error}
        </div>`;
    const answer = result.answer;
    if (result.tool === 'ping') {
        return html`<div class="compare-box">${answer.average_ms != null
            ? html`<b>${`${answer.average_ms.toFixed(1)} ms`}</b> average
                (${`${answer.min_ms.toFixed(1)}–${answer.max_ms.toFixed(1)} ms`}),
                ${` ${answer.loss_percent}% lost of ${answer.sent}`}`
            : html`<span class="pill bad">no reply</span> ${answer.host}`}
            </div>`;
    }
    if (result.tool === 'tcp') {
        const level = { open: 'good', closed: 'warn' }[answer.state] || 'bad';
        return html`<div class="compare-box"><span class="pill ${level}">
            ${answer.state}</span>${` ${answer.host}:${answer.port}`}${
            answer.time_ms != null ? ` in ${answer.time_ms.toFixed(1)} ms` : ''}
            ${answer.state === 'closed' ? html`<span class="muted">${
                ' (host is up, nothing listening)'}</span>` : ''}</div>`;
    }
    return html`
        <div class="compare-box">
            <div class="muted">${`${answer.hosts.length} hosts answered in ${
                answer.network} (${answer.duration_s.toFixed(1)} s). "closed"
                means the host is up but nothing listens on that port.`}</div>
            <table class="table">
                <thead><tr><th>Address</th><th>Name</th><th>MAC</th>
                    ${answer.ports.map((port) => html`<th>${port}</th>`)}
                    <th>In inventory</th></tr></thead>
                <tbody>${answer.hosts.map((host) => html`<tr>
                    <td class="mono">${host.address}</td>
                    <td>${host.name || html`<span class="faint">–</span>`}</td>
                    <td class="mono muted">${host.mac || '–'}</td>
                    ${answer.ports.map((port) => {
                        const state = host.ports[String(port)];
                        return html`<td>${state === 'open'
                            ? html`<span class="pill good">open</span>`
                            : html`<span class="faint">${state || '–'}</span>`}
                            </td>`;
                    })}
                    <td>${knownAddresses[host.address]
                        || html`<span class="faint">–</span>`}</td>
                </tr>`)}</tbody>
            </table>
        </div>`;
}


// --- Page --------------------------------------------------------------------

export function NetworkView({ state, now }) {
    return html`
        <div class="page-head"><div>
            <h1>Network</h1>
            <p>The robot's link: bandwidth against the competition limit, live
                traffic, Wi-Fi, and tools to find and test machines.</p>
        </div></div>
        <div class="stack">
            <${CompetitionPanel} state=${state} />
            <${BudgetPanel} state=${state} now=${now} />
            <${TrafficPanel} state=${state} />
            <${WifiPanel} state=${state} />
            <${ToolsPanel} state=${state} />
        </div>`;
}
