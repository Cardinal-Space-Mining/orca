// The Network page: machines and the networks between them, as a map or as
// the list of machine cards (with the bandwidth budget, sharing, traffic,
// Wi-Fi and tools panels below).

import { html, Icon } from './common.js';
import { InventoryFooter, MachineGrid, Summary } from './machines.js';
import { NetworkMap } from './map.js';
import { NetworkPanels, formatKbps } from './network.js';


const VIEW_KEY = 'orca.network-view';

export function preferredView() {
    return window.localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'map';
}

// The robot link's average against the limit, while it's being measured.
function BudgetWidget({ budget, onOpen }) {
    const measuring = budget.settings.interface
        || budget.monitor.state !== 'stopped';
    if (!measuring) return null;
    const source = budget.sources.interface.average_kbps != null
        ? budget.sources.interface : budget.sources.net_adapter;
    const average = source.average_kbps;
    const ratio = average == null ? 0 : average / budget.limit_kbps;
    const level = ratio > 0.9 ? 'bad' : ratio > 0.7 ? 'warn' : 'good';
    return html`
        <button class="budget-widget" onClick=${onOpen}
            title="The bandwidth budget">
            <${Icon} name="gauge" size=${15} />
            <span>Robot link${' '}<b>${average == null ? '–'
                : formatKbps(average)}</b>${` avg of ${formatKbps(
                budget.limit_kbps)}`}</span>
            <span class="mini-meter"><span class="meter-fill ${level}"
                style=${`width: ${Math.min(100, 100 * ratio)}%`}></span></span>
        </button>`;
}

export function NetworkPage({ state, now, view, selection, navigate }) {
    // Clicking what's already selected closes its panel.
    const select = (next) => navigate('machines', next && next !== selection
        ? `map/${encodeURIComponent(next)}` : 'map');
    const setView = (next) => {
        window.localStorage.setItem(VIEW_KEY, next);
        navigate('machines', next);
    };
    return html`
        <div class="page-head">
            <div>
                <h1>Network</h1>
                <p>Machines and the networks between them, live over
                    SSH.${' '}<a class="link-button" href="#/machines/add">
                    Add a machine</a>${' · '}<a class="link-button"
                    href="#/machines/edit">Edit machines</a></p>
            </div>
            <${Summary} machines=${state.machines} />
        </div>
        <div class="page-toolbar">
            <div class="segmented">
                <button class=${view === 'map' ? 'active' : ''}
                    onClick=${() => setView('map')}><${Icon} name="map"
                    size=${15} />${' Map'}</button>
                <button class=${view === 'list' ? 'active' : ''}
                    onClick=${() => setView('list')}><${Icon} name="list"
                    size=${15} />${' List'}</button>
            </div>
            <span class="spacer"></span>
            <${BudgetWidget} budget=${state.budget}
                onOpen=${() => select('budget')} />
        </div>
        ${view === 'list'
            ? html`<${MachineGrid} state=${state} now=${now} />
                <${NetworkPanels} state=${state} now=${now} />`
            : html`<${NetworkMap} state=${state} now=${now}
                selection=${selection} onSelect=${select} />`}
        <${InventoryFooter} state=${state} />`;
}
