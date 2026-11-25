// The launch page: pick a scenario, fill its roles with machines, choose
// options, check the plan (exact commands, peer addresses, enabled presets),
// then launch.

import { useEffect, useState } from 'preact/hooks';
import { html, getJson, postJson, StatusDot, CommitInput, Icon }
    from './common.js';


// --- Scenario list -----------------------------------------------------------

function ScenarioList({ scenarios, selectedId, onSelect }) {
    return html`
        <nav class="scenario-list">
            ${scenarios.map((scenario) => html`
                <button class="scenario-item ${scenario.id === selectedId
                    ? 'active' : ''}" onClick=${() => onSelect(scenario.id)}>
                    <b>${scenario.label || scenario.id}</b>
                    <span>${scenario.description}</span>
                    <span class="roles">${Object.values(scenario.roles)
                        .map((role) => role.label).join(' · ')}</span>
                </button>`)}
        </nav>`;
}


// --- Choices -----------------------------------------------------------------

function RolePicker({ role, definition, value, machines, onChange }) {
    return html`
        <label class="field">
            <span>${definition.label || role}</span>
            <select class="input" value=${value}
                onChange=${(event) => onChange(event.target.value)}>
                ${machines.map((entry) => html`
                    <option value=${entry.machine.id}>
                        ${entry.machine.label}${entry.connection.state === 'online'
                            ? '' : ` (${entry.connection.state})`}
                    </option>`)}
            </select>
        </label>`;
}

function OptionPicker({ name, definition, value, onChange }) {
    const label = definition.label || name;
    if (definition.choices) {
        return html`
            <label class="field">
                <span>${label}</span>
                <select class="input" value=${value}
                    onChange=${(event) => onChange(event.target.value)}>
                    ${Object.entries(definition.choices).map(([key, text]) =>
                        html`<option value=${key}>${text}</option>`)}
                </select>
            </label>`;
    }
    return html`
        <label class="field wide">
            <span>${label}</span>
            <input class="input mono" value=${value}
                placeholder=${definition.placeholder || ''}
                onInput=${(event) => onChange(event.target.value)} />
        </label>`;
}


// --- Plan preview ------------------------------------------------------------

function AddressPicker({ process, role, value, onChange }) {
    const choices = process.address_choices[role] || [];
    const isChoice = !value || choices.some((choice) =>
        choice.address === value);
    // "other…" stays selected while its field is empty.
    const [wantsCustom, setWantsCustom] = useState(!isChoice);
    const isCustom = wantsCustom || !isChoice;
    return html`
        <div class="address-row">
            <span class="muted">${role} address</span>
            <select class="input mono" value=${isCustom ? '__custom' : value}
                onChange=${(event) => {
                    const isOther = event.target.value === '__custom';
                    setWantsCustom(isOther);
                    if (!isOther) onChange(event.target.value);
                }}>
                ${choices.map((choice) => html`<option value=${choice.address}>
                    ${choice.address} (${choice.interface}, ${choice.reason})
                    </option>`)}
                <option value="__custom">other…</option>
            </select>
            ${isCustom && html`<${CommitInput} class="input mono"
                value=${isChoice ? '' : value} placeholder="address"
                onCommit=${onChange} />`}
        </div>`;
}

function ProcessPlan({ process, addresses, setAddress, extra, setExtra }) {
    const [showDetails, setShowDetails] = useState(false);
    const selected = process.selected || {};
    const settings = process.settings || {};
    return html`
        <div class="plan-process">
            <div class="plan-head">
                <b>${process.label}</b>
                <span class="pill">${process.machine_label}</span>
                ${process.after.length > 0 && html`<span class="faint">
                    ${`starts after ${process.after.join(', ')}`}</span>`}
            </div>
            <div class="command mono">${process.display}</div>
            ${process.address_roles.map((role) => html`<${AddressPicker}
                process=${process} role=${role}
                value=${addresses[`${process.key}.${role}`]
                    || process.addresses[role]}
                onChange=${(value) => setAddress(`${process.key}.${role}`,
                    value)} />`)}
            ${(process.constants || []).length > 0 && html`
                <div class="settings-row"><span class="muted"
                    title="Current addresses instead of the config's">
                    addresses</span>
                    ${process.constants.map((constant) => html`
                        <span class="mono chip"
                            title=${`config says ${constant.was}`}>${
                            `${constant.name} = ${constant.value}`}</span>`)}
                </div>`}
            ${Object.entries(settings).map(([action, values]) => html`
                <div class="settings-row"><span class="muted">sets</span>
                    ${Object.entries(values).map(([key, value]) => html`
                        <span class="mono chip">${`${action}.${key} = ${
                            JSON.stringify(value)}`}</span>`)}</div>`)}
            <label class="field wide">
                <span>Extra arguments</span>
                <${CommitInput} class="input mono" value=${extra}
                    placeholder="e.g. foxglove_bridge:=null (Enter to apply)"
                    onCommit=${setExtra} />
            </label>
            <button class="link-button" onClick=${() =>
                setShowDetails(!showDetails)}>
                ${showDetails ? 'Hide' : 'Show'} enabled actions${
                    process.overlay ? ' and overlay' : ''}</button>
            ${showDetails && html`
                <div class="preset-table">
                    ${Object.entries(selected).map(([action, preset]) => html`
                        <span class="mono">${action}</span>
                        <span class="mono muted">${preset}</span>`)}
                </div>
                ${process.overlay && html`<pre class="code">${
                    JSON.stringify(process.overlay, null, 4)}</pre>`}`}
        </div>`;
}


// --- Scenario form -----------------------------------------------------------

function initialChoices(scenario) {
    const assignment = {};
    for (const [role, definition] of Object.entries(scenario.roles)) {
        assignment[role] = definition.default || 'local';
    }
    const options = {};
    for (const [name, definition] of Object.entries(scenario.options)) {
        options[name] = definition.default ?? '';
    }
    return { assignment, options, addresses: {}, extra_arguments: {} };
}

// `source`: the editor's unsaved text, planned instead of the saved file
// (preview only: no launch button).
export function ScenarioForm({ scenario, machines, onLaunched, source = null,
    actions = null }) {
    const [choices, setChoices] = useState(() => initialChoices(scenario));
    const [plan, setPlan] = useState(null);
    const [error, setError] = useState(null);
    const [isLaunching, setIsLaunching] = useState(false);
    const onlineKey = machines.map((entry) =>
        `${entry.machine.id}:${entry.connection.state}`).join(',');

    useEffect(() => setChoices(initialChoices(scenario)), [scenario.id]);

    // Re-plan when choices change or machines come and go.
    useEffect(() => {
        let isCurrent = true;
        const timer = setTimeout(async () => {
            try {
                const result = await postJson('/api/plan',
                    { scenario_id: scenario.id, ...choices, source });
                if (isCurrent) {
                    setPlan(result);
                    setError(null);
                }
            } catch (failure) {
                if (isCurrent) setError(failure.message);
            }
        }, 250);
        return () => {
            isCurrent = false;
            clearTimeout(timer);
        };
    }, [scenario.id, JSON.stringify(choices), onlineKey, source]);

    const update = (part, key, value) => setChoices((previous) => ({
        ...previous, [part]: { ...previous[part], [key]: value } }));

    async function launch() {
        setIsLaunching(true);
        try {
            const run = await postJson('/api/runs',
                { scenario_id: scenario.id, ...choices });
            onLaunched(run.run_id);
        } catch (failure) {
            setError(failure.message);
        }
        setIsLaunching(false);
    }

    const errors = [...(plan ? plan.errors : []), ...(error ? [error] : [])];
    return html`
        <section class="panel">
            <div class="run-head">
                <h2>${scenario.label}</h2>
                ${actions}
            </div>
            <p class="muted">${scenario.description}</p>
            <div class="fields">
                ${Object.entries(scenario.roles).map(([role, definition]) =>
                    html`<${RolePicker} role=${role} definition=${definition}
                        value=${choices.assignment[role]} machines=${machines}
                        onChange=${(value) => update('assignment', role,
                            value)} />`)}
                ${Object.entries(scenario.options).map(([name, definition]) =>
                    html`<${OptionPicker} name=${name} definition=${definition}
                        value=${choices.options[name]}
                        onChange=${(value) => update('options', name,
                            value)} />`)}
            </div>
            ${errors.map((message) => html`<div class="error-banner">
                ${message}</div>`)}
            ${plan && plan.warnings.map((message) => html`
                <div class="warn-banner">${message}</div>`)}
            ${plan && plan.processes.map((process) => html`<${ProcessPlan}
                key=${process.key} process=${process}
                addresses=${choices.addresses}
                setAddress=${(key, value) => update('addresses', key, value)}
                extra=${choices.extra_arguments[process.key] || ''}
                setExtra=${(value) => update('extra_arguments', process.key,
                    value)} />`)}
            ${source === null && html`<div class="button-row">
                <button class="button primary large"
                    disabled=${!plan || errors.length > 0 || isLaunching}
                    onClick=${launch}>
                    ${isLaunching ? 'Launching…' : 'Launch'}</button>
                <span class="faint">${plan && plan.processes.length > 1
                    ? 'Processes start in order; if one fails, the others are '
                        + 'stopped again.' : ''}</span>
            </div>`}
        </section>`;
}


// --- Page --------------------------------------------------------------------

export function LaunchView({ state, onLaunched, initialId = null }) {
    const [catalog, setCatalog] = useState(null);
    const [selectedId, setSelectedId] = useState(initialId);
    const [error, setError] = useState(null);

    useEffect(() => {
        getJson('/api/scenarios').then((result) => {
            setCatalog(result);
            if (result.scenarios.length && !result.scenarios.some((entry) =>
                entry.id === initialId)) {
                setSelectedId(result.scenarios[0].id);
            }
        }).catch((failure) => setError(failure.message));
    }, []);

    if (error) return html`<div class="error-banner">${error}</div>`;
    if (!catalog) return html`<div class="placeholder">Loading scenarios…
        </div>`;
    const scenario = catalog.scenarios.find((entry) =>
        entry.id === selectedId);
    return html`
        <div class="page-head">
            <div>
                <h1>Launch</h1>
                <p>Scenarios from <span class="mono">
                    src/project/orca/scenarios</span>: one launch can
                    start processes on several machines.</p>
            </div>
            <a class="button" href="#/launch/edit"><${Icon} name="plus"
                size=${15} />${' New scenario'}</a>
        </div>
        ${catalog.errors.map((entry) => html`<div class="error-banner">
            <span class="mono">${entry.path}</span>: ${entry.error}</div>`)}
        <div class="split">
            <${ScenarioList} scenarios=${catalog.scenarios}
                selectedId=${selectedId} onSelect=${setSelectedId} />
            ${scenario ? html`<${ScenarioForm} key=${scenario.id}
                scenario=${scenario} machines=${state.machines}
                onLaunched=${onLaunched}
                actions=${html`<div class="button-row">
                    <a class="button small" href=${`#/launch/edit/${
                        scenario.id}`}><${Icon} name="pencil" size=${13} />
                        ${' Edit'}</a>
                    <a class="button small" href=${`#/launch/edit/${
                        scenario.id}~copy`}><${Icon} name="layers"
                        size=${13} />${' Duplicate'}</a>
                </div>`} />`
                : html`<div class="placeholder">No scenarios found.</div>`}
        </div>`;
}
