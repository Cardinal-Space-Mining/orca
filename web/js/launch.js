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


// --- Extra arguments ---------------------------------------------------------

// The lance config's actions and their presets, for the pickers. Fetched
// once per visit to the launch page (LaunchView forgets it, so edits on the
// Config page show up).
let actionsPromise = null;

function useConfigActions() {
    const [actions, setActions] = useState(null);
    useEffect(() => {
        actionsPromise = actionsPromise || getJson('/api/config')
            .then((config) => config.actions.map((action) => ({
                name: action.name, isSetup: action.is_setup,
                presets: action.presets.map((preset) => preset.name),
            })))
            .catch(() => []);
        actionsPromise.then(setActions);
    }, []);
    return actions;
}

// "a:=b name:='x y'" -> ["a:=b", "name:=x y"], the way the planner splits
// it (shell quoting); null while a quote is open.
export function splitArguments(text) {
    const tokens = [];
    let current = null;
    let quote = null;
    for (const character of text || '') {
        if (quote) {
            if (character === quote) quote = null;
            else current += character;
        } else if (character === '"' || character === "'") {
            quote = character;
            current = current ?? '';
        } else if (/\s/.test(character)) {
            if (current !== null) tokens.push(current);
            current = null;
        } else {
            current = (current ?? '') + character;
        }
    }
    if (quote) return null;
    if (current !== null) tokens.push(current);
    return tokens;
}

function quoteArgument(text) {
    return /^[\w.,:=\/@+%-]+$/.test(text) ? text
        : `'${text.replace(/'/g, `'"'"'`)}'`;
}

// The arguments with key:=value set (replacing an earlier value of key).
export function setArgument(text, key, value) {
    const tokens = (splitArguments(text) || []).filter((token) =>
        token.split(':=')[0] !== key);
    return [...tokens, `${key}:=${value}`].map((token) => {
        const at = token.indexOf(':=');
        return at < 0 ? quoteArgument(token)
            : `${token.slice(0, at)}:=${quoteArgument(token.slice(at + 2))}`;
    }).join(' ');
}

// Pick an action and one of its presets instead of typing action:=preset.
function PresetPicker({ selected, extra, setExtra }) {
    const actions = useConfigActions();
    const [action, setAction] = useState('');
    const [preset, setPreset] = useState('');
    if (!actions || actions.length === 0) return null;
    const entry = actions.find((candidate) => candidate.name === action);
    const current = selected && selected[action];
    const isReadable = splitArguments(extra) !== null;
    const group = (label, members) => members.length > 0 && html`
        <optgroup label=${label}>${members.map((member) => html`
            <option value=${member.name}>${member.name}</option>`)}
        </optgroup>`;
    return html`
        <div class="preset-picker">
            <select class="input mono" value=${action} aria-label="Action"
                onChange=${(event) => {
                    setAction(event.target.value);
                    setPreset('');
                }}>
                <option value="">action…</option>
                ${group('Setups', actions.filter((item) => item.isSetup))}
                ${group('Components', actions.filter((item) =>
                    !item.isSetup))}
            </select>
            <span class="faint">:=</span>
            <select class="input mono" value=${preset} disabled=${!entry}
                aria-label="Preset"
                onChange=${(event) => setPreset(event.target.value)}>
                <option value="">${entry ? 'preset…' : '—'}</option>
                ${entry && entry.presets.map((name) => html`
                    <option value=${name}>${name}${name === current
                        ? '  (selected now)' : ''}</option>`)}
                ${entry && html`<option value="null">null (turn it off)
                    </option>`}
            </select>
            <button class="button small" disabled=${!entry || !preset
                || !isReadable}
                title=${isReadable ? 'Add it to the extra arguments'
                    : 'Close the quote in the extra arguments first'}
                onClick=${() => {
                    setExtra(setArgument(extra, action, preset));
                    setPreset('');
                }}>Set</button>
            ${current && html`<span class="faint small-text">${
                `${action} is ${current} now`}</span>`}
        </div>`;
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
            ${process.error && html`<div class="error-banner">
                ${process.error}</div>`}
            ${process.display && html`<div class="command mono">${
                process.display}</div>`}
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
                    placeholder=${process.kind === 'command'
                        ? 'added to the command (Enter to apply)'
                        : 'e.g. foxglove_bridge:=null (Enter to apply)'}
                    onCommit=${setExtra} />
            </label>
            ${process.kind !== 'command' && html`<${PresetPicker}
                selected=${process.selected} extra=${extra}
                setExtra=${setExtra} />`}
            ${!process.error && html`<button class="link-button"
                onClick=${() => setShowDetails(!showDetails)}>
                ${showDetails ? 'Hide' : 'Show'} enabled actions${
                    process.overlay ? ' and overlay' : ''}</button>`}
            ${showDetails && !process.error && html`
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

// The scenario's defaults, with earlier choices (a run's) over them where
// the scenario still has those roles and options.
function initialChoices(scenario, earlier = null) {
    const assignment = {};
    for (const [role, definition] of Object.entries(scenario.roles)) {
        assignment[role] = (earlier && earlier.assignment
            && earlier.assignment[role]) || definition.default || 'local';
    }
    const options = {};
    for (const [name, definition] of Object.entries(scenario.options)) {
        const value = earlier && earlier.options && earlier.options[name];
        options[name] = value ?? definition.default ?? '';
    }
    return { assignment, options,
        addresses: { ...((earlier && earlier.addresses) || {}) },
        extra_arguments: { ...((earlier && earlier.extra_arguments) || {}) } };
}

// "Run again": the launch page opens with a run's choices filled in.
const PENDING_KEY = 'orca.launch-choices';

export function launchAgain(scenarioId, choices) {
    window.sessionStorage.setItem(PENDING_KEY,
        JSON.stringify({ scenario_id: scenarioId, choices }));
    window.location.hash = `#/launch/${encodeURIComponent(scenarioId)}`;
}

function takePendingChoices() {
    const text = window.sessionStorage.getItem(PENDING_KEY);
    window.sessionStorage.removeItem(PENDING_KEY);
    try {
        return text ? JSON.parse(text) : null;
    } catch {
        return null;
    }
}

const SCENARIO_DRAFT_KEY = 'orca.scenario-draft';

// The editor's starting text for "<id>~custom" (see saveAsScenario).
export function takeScenarioDraft(scenarioId) {
    const text = window.sessionStorage.getItem(SCENARIO_DRAFT_KEY);
    try {
        const draft = text && JSON.parse(text);
        return draft && draft.scenario_id === scenarioId ? draft.text : null;
    } catch {
        return null;
    }
}

// `source`: the editor's unsaved text, planned instead of the saved file
// (preview only: no launch button).
// `earlier`: choices to start from (a run's), instead of the defaults.
// The form is keyed by scenario, so a new scenario starts it afresh.
export function ScenarioForm({ scenario, machines, onLaunched, source = null,
    actions = null, earlier = null }) {
    const [choices, setChoices] = useState(() =>
        initialChoices(scenario, earlier));
    const [isFromEarlier, setIsFromEarlier] = useState(!!earlier);
    const [plan, setPlan] = useState(null);
    const [error, setError] = useState(null);
    const [isLaunching, setIsLaunching] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const onlineKey = machines.map((entry) =>
        `${entry.machine.id}:${entry.connection.state}`).join(',');

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

    // A new scenario with these choices as its defaults, opened in the
    // editor to name and save.
    async function saveAsScenario() {
        setIsSaving(true);
        try {
            const result = await postJson(`/api/scenarios/${
                encodeURIComponent(scenario.id)}/customize`, { choices });
            window.sessionStorage.setItem(SCENARIO_DRAFT_KEY, JSON.stringify(
                { scenario_id: scenario.id, text: result.text }));
            window.location.hash = `#/launch/edit/${encodeURIComponent(
                scenario.id)}~custom`;
        } catch (failure) {
            setError(failure.message);
            setIsSaving(false);
        }
    }

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
    // A process's own error shows in its block, beside the fields that fix
    // it; the banners above take the rest.
    const processErrors = new Set((plan ? plan.processes : [])
        .filter((process) => process.error)
        .map((process) => `${process.label}: ${process.error}`));
    const bannerErrors = errors.filter((message) =>
        !processErrors.has(message));
    return html`
        <section class="panel">
            <div class="run-head">
                <h2>${scenario.label}</h2>
                ${actions}
            </div>
            <p class="muted">${scenario.description}</p>
            ${isFromEarlier && html`<div class="notice-line">
                <span>Filled in from an earlier run.</span>
                <button class="link-button" onClick=${() => {
                    setChoices(initialChoices(scenario));
                    setIsFromEarlier(false);
                }}>Use the scenario's defaults</button></div>`}
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
            ${bannerErrors.map((message) => html`<div class="error-banner">
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
                <button class="button" disabled=${isSaving}
                    title="A new scenario with these machines, options and
                        arguments as its defaults"
                    onClick=${saveAsScenario}>${isSaving ? 'Opening…'
                        : 'Save as scenario…'}</button>
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
    const [pending] = useState(takePendingChoices);

    useEffect(() => {
        actionsPromise = null;  // the config may have changed since
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
                earlier=${pending && pending.scenario_id === scenario.id
                    ? pending.choices : null}
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
