// The orchestrator's web interface. Preact + htm, loaded as plain ES modules
// (no build step): edit a file under js/ and reload the page.

import { render } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { html, Icon, postJson, shortCommit } from './js/common.js';
import { useOrchestratorState, useNow } from './js/state.js';
import { LaunchView } from './js/launch.js';
import { ScenarioEditor } from './js/scenarioedit.js';
import { RunsView, isRunActive } from './js/runs.js';
import { ConfigView } from './js/config.js';
import { DeployView } from './js/deploy.js';
import { BagsView } from './js/bags.js';
import { LogsView } from './js/logs.js';
import { InventoryView } from './js/inventory.js';
import { AddMachineView } from './js/addmachine.js';
import { TerminalDock } from './js/terminals.js';
import { NetworkPage, preferredView } from './js/networkpage.js';


const TABS = [
    { id: 'machines', label: 'Network', icon: 'network' },
    { id: 'launch', label: 'Launch', icon: 'rocket' },
    { id: 'runs', label: 'Runs', icon: 'activity' },
    { id: 'deploy', label: 'Deploy', icon: 'upload' },
    { id: 'bags', label: 'Bags', icon: 'archive' },
    { id: 'logs', label: 'Logs', icon: 'scroll-text' },
    { id: 'config', label: 'Config', icon: 'sliders-horizontal' },
];

// The open tab lives in the URL (#/runs/<run id>, #/machines/map/<what is
// selected>) so a reload keeps it. #/network is the old network page.
function readLocation() {
    const [rawTab, detail, ...rest] = window.location.hash
        .replace(/^#\/?/, '').split('/');
    const tab = rawTab === 'network' ? 'machines' : rawTab;
    const known = TABS.some((entry) => entry.id === tab && !entry.soon);
    return {
        tab: known ? tab : 'machines',
        detail: rawTab === 'network' ? 'list' : detail || null,
        rest: rest.length ? decodeURIComponent(rest.join('/')) : null,
    };
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


// Competition mode: less orchestrator traffic on the robot's link (slower
// status, no live output from other machines).
function CompetitionSwitch({ state }) {
    const isOn = state.settings.competition_mode;
    const average = state.budget.sources.interface.average_kbps;
    return html`
        <button class="competition-switch ${isOn ? 'on' : ''}"
            title=${isOn ? 'Competition mode is on: status every 10 s, output '
                + 'from other machines only on request. Click to turn off.'
                : "Competition mode: keep the orchestrator's own traffic on "
                + "the robot's link small. Click to turn on."}
            onClick=${() => postJson('/api/settings',
                { competition_mode: !isOn })}>
            <${Icon} name="trophy" size=${15} />
            <span>Competition${isOn && average != null
                ? ` · ${Math.round(average)} kbps avg` : ''}</span>
            <span class="switch-knob"></span>
        </button>`;
}

function TopBar({ state, isLive, tab, navigate }) {
    const local = state && state.machines.find((entry) => entry.machine.is_local);
    const workspace = local && local.workspace;
    const numActiveRuns = state ? state.runs.filter(isRunActive).length : 0;
    return html`
        <header class="topbar">
            <div class="brand"><img src="/static/csm-logo-dark.svg" alt="" />
                Orchestrator</div>
            <nav class="tabs">
                ${TABS.map((entry) => html`
                    <button class="tab ${tab === entry.id ? 'active' : ''}"
                        disabled=${entry.soon}
                        onClick=${() => navigate(entry.id)}><${Icon}
                        name=${entry.icon} size=${15} />${entry.label}
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
            ${state && html`<${CompetitionSwitch} state=${state} />`}
            <div class="live">
                <span class="dot ${isLive ? 'good' : 'bad pulse'}"></span>
                ${isLive ? 'Live' : 'Orchestrator disconnected'}
            </div>
        </header>`;
}

function Page({ state, location, navigate, now }) {
    switch (location.tab) {
        case 'launch':
            if (location.detail === 'edit') {
                return html`<${ScenarioEditor} key=${location.rest || 'new'}
                    state=${state} scenarioId=${location.rest} />`;
            }
            return html`<${LaunchView} key=${location.detail || ''}
                state=${state} initialId=${location.detail
                    && decodeURIComponent(location.detail)}
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
        case 'config':
            return html`<${ConfigView} />`;
        default:
            if (location.detail === 'edit') return html`<${InventoryView} />`;
            if (location.detail === 'add') {
                return html`<${AddMachineView} state=${state} />`;
            }
            return html`<${NetworkPage} state=${state} now=${now}
                view=${['map', 'list'].includes(location.detail)
                    ? location.detail : preferredView()}
                selection=${location.detail === 'map' ? location.rest : null}
                navigate=${navigate} />`;
    }
}

function App() {
    const { state, isLive } = useOrchestratorState();
    const [location, navigate] = useLocation();
    const now = useNow();
    return html`
        <${TopBar} state=${state} isLive=${isLive} tab=${location.tab}
            navigate=${navigate} />
        <main>
            ${state ? html`<${Page} state=${state} location=${location}
                navigate=${navigate} now=${now} />`
                : html`<div class="placeholder">
                    Connecting to the orchestrator…</div>`}
        </main>
        <${TerminalDock} />`;
}

render(html`<${App} />`, document.getElementById('app'));
