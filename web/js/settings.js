// The Settings page: Orca's own settings (competition mode, the
// bandwidth budget, how it starts), this browser's theme, what it has
// remembered, and where its files are.

import { useState } from 'preact/hooks';
import { html, postJson, CommitInput, Icon } from './common.js';
import { BudgetSources, CompetitionPanel } from './network.js';
import { setThemeChoice, useTheme } from './theme.js';


function save(changes, setError) {
    return postJson('/api/settings', changes)
        .then(() => setError(null))
        .catch((failure) => setError(failure.message));
}

function NumberSetting({ label, value, onCommit, unit }) {
    return html`
        <label class="field">
            <span>${label}</span>
            <div class="input-with-unit">
                <${CommitInput} class="input mono" type="number"
                    value=${String(value)}
                    onCommit=${(text) => onCommit(Number(text))} />
                ${unit && html`<span class="faint">${unit}</span>`}
            </div>
        </label>`;
}

function BudgetSettings({ state }) {
    const [error, setError] = useState(null);
    const budget = state.settings.budget;
    const setBudget = (changes) => save({ budget: changes }, setError);
    return html`
        <section class="panel">
            <h2>Bandwidth budget</h2>
            <p class="muted">The rules' numbers (Lunabotics guidebook 14.1.18
                and 14.1.19) and what measures the robot's link. The meter is
                on the Network page.</p>
            <div class="fields">
                <${NumberSetting} label="Average limit" unit="kbps"
                    value=${budget.limit_kbps}
                    onCommit=${(value) => setBudget({ limit_kbps: value })} />
                <${NumberSetting} label="Each NASA camera counts" unit="kbps"
                    value=${budget.camera_kbps}
                    onCommit=${(value) => setBudget({ camera_kbps: value })} />
            </div>
            ${error && html`<div class="error-banner">${error}</div>`}
            <div class="settings-sources"><${BudgetSources} state=${state} />
                </div>
        </section>`;
}

function StartupSettings({ state }) {
    const [error, setError] = useState(null);
    const startup = state.settings.startup;
    const runningPort = Number(window.location.port) || null;
    return html`
        <section class="panel">
            <h2>Starting Orca</h2>
            <p class="muted">Used the next time it starts, unless the command
                line says otherwise (<span class="mono">--port</span>,${' '}
                <span class="mono">--no-browser</span>).</p>
            <div class="fields">
                <${NumberSetting} label="Web page port" value=${startup.port}
                    onCommit=${(value) => save({ startup: { port: value } },
                        setError)} />
                <label class="check settings-check">
                    <input type="checkbox" checked=${startup.open_browser}
                        onChange=${(event) => save({ startup: {
                            open_browser: event.target.checked } }, setError)} />
                    Open a browser window when it starts</label>
            </div>
            ${runningPort && runningPort !== startup.port && html`
                <p class="small-note warn-text">Running on ${runningPort} now;
                    ${' '}${startup.port} from the next start.</p>`}
            ${error && html`<div class="error-banner">${error}</div>`}
        </section>`;
}

const THEME_OPTIONS = [
    { choice: 'system', label: 'Match the system', icon: 'sun-moon' },
    { choice: 'light', label: 'Light', icon: 'sun' },
    { choice: 'dark', label: 'Dark', icon: 'moon' },
];

function Appearance() {
    const { choice } = useTheme();
    return html`
        <section class="panel">
            <h2>Appearance</h2>
            <p class="muted">Light is easier to read outside on a bright day.
                Kept by this browser; the sun / moon button in the top bar
                switches it too.</p>
            <div class="segmented theme-choice">
                ${THEME_OPTIONS.map((option) => html`
                    <button class=${choice === option.choice ? 'active' : ''}
                        onClick=${() => setThemeChoice(option.choice)}>
                        <${Icon} name=${option.icon} size=${15} />
                        ${option.label}</button>`)}
            </div>
        </section>`;
}

function Remembered({ state }) {
    const [error, setError] = useState(null);
    const labels = Object.fromEntries(state.machines.map((entry) =>
        [entry.machine.id, entry.machine.label]));
    const forget = (what, machineId) => postJson('/api/settings/forget',
        { what, machine_id: machineId }).then(() => setError(null))
        .catch((failure) => setError(failure.message));
    const sharing = Object.entries(state.settings.sharing || {});
    const withAddresses = state.machines.filter((entry) =>
        (entry.addresses || []).length > 0);
    return html`
        <section class="panel">
            <h2>Remembered</h2>
            <p class="muted">What Orca learned along the way.
                Forgetting is safe: it's learned again.</p>
            <div class="section-head"><span>Internet sharing choices</span>
                </div>
            ${sharing.length === 0 ? html`<p class="faint">None yet.</p>`
                : html`<table class="table">
                    <tbody>${sharing.map(([machineId, choice]) => html`
                        <tr>
                            <td>${labels[machineId] || machineId}</td>
                            <td class="mono muted">${[choice.downstream,
                                choice.subnet].filter(Boolean).join(' · ')}</td>
                            <td class="actions"><button class="link-button"
                                onClick=${() => forget('sharing', machineId)}>
                                Forget</button></td>
                        </tr>`)}</tbody>
                </table>`}
            <div class="section-head"><span>Learned addresses</span></div>
            ${withAddresses.length === 0 ? html`<p class="faint">None yet.</p>`
                : html`<table class="table">
                    <tbody>${withAddresses.map((entry) => html`
                        <tr>
                            <td>${entry.machine.label}</td>
                            <td class="mono muted">${entry.addresses.slice(0, 4)
                                .map((record) => record.address).join(', ')}${
                                entry.addresses.length > 4 ? ' …' : ''}</td>
                            <td class="actions"><button class="link-button"
                                onClick=${() => forget('addresses',
                                    entry.machine.id)}>Forget</button></td>
                        </tr>`)}</tbody>
                </table>`}
            ${error && html`<div class="error-banner">${error}</div>`}
        </section>`;
}

function Files({ state }) {
    const info = state.orca;
    return html`
        <section class="panel">
            <h2>Files</h2>
            <p class="muted">Everything Orca keeps is plain JSON.
                The workspace files are shared through git; the rest are this
                laptop's own.</p>
            <table class="table">
                <tbody>${info.files.map((file) => html`
                    <tr>
                        <td>${file.label}</td>
                        <td class="mono muted">${file.path}</td>
                        <td>${file.exists ? '' : html`<span class="faint">
                            not created yet</span>`}</td>
                    </tr>`)}
                    <tr><td>Workspace</td><td class="mono muted">${
                        info.workspace || 'none'}</td><td></td></tr>
                    <tr><td>Agent version</td><td class="mono muted">${
                        info.agent_hash}</td><td></td></tr>
                </tbody>
            </table>
        </section>`;
}

export function SettingsView({ state }) {
    return html`
        <div class="page-head">
            <div>
                <h1>Settings</h1>
                <p>Orca's own settings. Machines are on the${' '}
                    <a class="link-button" href="#/machines/edit">Network page
                    </a>, launches under${' '}<a class="link-button"
                    href="#/launch">Launch</a>, the robot's config under${' '}
                    <a class="link-button" href="#/config">Config</a>.</p>
            </div>
        </div>
        <div class="settings-grid">
            <${CompetitionPanel} state=${state} />
            <${Appearance} />
            <${StartupSettings} state=${state} />
            <${BudgetSettings} state=${state} />
            <${Remembered} state=${state} />
            <${Files} state=${state} />
        </div>`;
}
