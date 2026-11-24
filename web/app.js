// csm-hub web interface. Preact + htm, loaded as plain ES modules (no build
// step): edit a file under js/ and reload the page.

import { render } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { html, shortCommit } from './js/common.js';
import { useHubState, useNow } from './js/state.js';
import { MachinesView } from './js/machines.js';
import { LaunchView } from './js/launch.js';
import { RunsView, isRunActive } from './js/runs.js';
import { ConfigView } from './js/config.js';
import { DeployView } from './js/deploy.js';
import { BagsView } from './js/bags.js';
import { LogsView } from './js/logs.js';
import { InventoryView } from './js/inventory.js';
import { TerminalDock } from './js/terminals.js';
import { NetworkView } from './js/network.js';


const TABS = [
    { id: 'machines', label: 'Machines' },
    { id: 'launch', label: 'Launch' },
    { id: 'runs', label: 'Runs' },
    { id: 'deploy', label: 'Deploy' },
    { id: 'bags', label: 'Bags' },
    { id: 'logs', label: 'Logs' },
    { id: 'config', label: 'Config' },
    { id: 'network', label: 'Network' },
];

// The open tab lives in the URL (#/runs/<run id>) so a reload keeps it.
function readLocation() {
    const [tab, detail] = window.location.hash.replace(/^#\/?/, '').split('/');
    const known = TABS.some((entry) => entry.id === tab && !entry.soon);
    return { tab: known ? tab : 'machines', detail: detail || null };
}

function useLocation() {
    const [location, setLocation] = useState(readLocation);
    useEffect(() => {
        const onChange = () => setLocation(readLocation());
        window.addEventListener('hashchange', onChange);
        return () => window.removeEventListener('hashchange', onChange);
    }, []);
    const navigate = (tab, detail = null) => {
        window.location.hash = `#/${tab}${detail ? `/${detail}` : ''}`;
    };
    return [location, navigate];
}


function TopBar({ state, isLive, tab, navigate }) {
    const local = state && state.machines.find((entry) => entry.machine.is_local);
    const workspace = local && local.workspace;
    const numActiveRuns = state ? state.runs.filter(isRunActive).length : 0;
    return html`
        <header class="topbar">
            <div class="brand"><img src="/static/csm-logo-dark.svg" alt="" />
                CSM Hub</div>
            <nav class="tabs">
                ${TABS.map((entry) => html`
                    <button class="tab ${tab === entry.id ? 'active' : ''}"
                        disabled=${entry.soon}
                        onClick=${() => navigate(entry.id)}>${entry.label}
                        ${entry.id === 'runs' && numActiveRuns > 0 && html`
                            <span class="badge">${numActiveRuns}</span>`}
                        ${entry.soon && html`<span class="soon">soon</span>`}
                    </button>`)}
            </nav>
            <div class="spacer"></div>
            ${workspace && workspace.is_git && html`
                <div class="workspace-chip">
                    <span>lance-ws</span>
                    <span class="mono">${workspace.branch} @ ${shortCommit(
                        workspace.head)}</span>
                </div>`}
            ${state && state.settings.competition_mode && html`
                <a class="competition-pill" href="#/network"
                    title="Competition mode: less hub traffic on the robot's link">
                    Competition${state.budget.sources.interface.average_kbps
                        != null ? ` · ${Math.round(state.budget.sources
                            .interface.average_kbps)} kbps avg` : ''}</a>`}
            <div class="live">
                <span class="dot ${isLive ? 'good' : 'bad pulse'}"></span>
                ${isLive ? 'Live' : 'Hub disconnected'}
            </div>
        </header>`;
}

function Page({ state, location, navigate, now }) {
    switch (location.tab) {
        case 'launch':
            return html`<${LaunchView} state=${state}
                onLaunched=${(runId) => navigate('runs', runId)} />`;
        case 'runs':
            return html`<${RunsView} state=${state}
                selectedId=${location.detail}
                onSelect=${(runId) => navigate('runs', runId)} now=${now} />`;
        case 'deploy':
            return html`<${DeployView} state=${state} now=${now}
                onLaunched=${(runId) => navigate('runs', runId)} />`;
        case 'bags':
            return html`<${BagsView} state=${state}
                selectedId=${location.detail}
                onSelect=${(machineId) => navigate('bags', machineId)}
                onLaunched=${(runId) => navigate('runs', runId)} />`;
        case 'logs':
            return html`<${LogsView} state=${state}
                selectedId=${location.detail} now=${now}
                onSelect=${(machineId) => navigate('logs', machineId)} />`;
        case 'network':
            return html`<${NetworkView} state=${state} now=${now} />`;
        case 'config':
            return html`<${ConfigView} />`;
        default:
            if (location.detail === 'edit') return html`<${InventoryView} />`;
            return html`<${MachinesView} state=${state} now=${now} />`;
    }
}

function App() {
    const { state, isLive } = useHubState();
    const [location, navigate] = useLocation();
    const now = useNow();
    return html`
        <${TopBar} state=${state} isLive=${isLive} tab=${location.tab}
            navigate=${navigate} />
        <main>
            ${state ? html`<${Page} state=${state} location=${location}
                navigate=${navigate} now=${now} />`
                : html`<div class="placeholder">Connecting to the hub…</div>`}
        </main>
        <${TerminalDock} />`;
}

render(html`<${App} />`, document.getElementById('app'));
