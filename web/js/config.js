// The config browser: every action and preset in lance/config, what a
// preset inherits from, and what a set of `key:=value` arguments resolves to
// (the same preprocessing run.sh does).

import { useEffect, useMemo, useState } from 'preact/hooks';
import { html, getJson, postJson } from './common.js';


function parseArguments(text) {
    const arguments_ = {};
    for (const token of text.split(/\s+/).filter(Boolean)) {
        const index = token.indexOf(':=');
        if (index < 0) throw new Error(`"${token}" isn't key:=value`);
        arguments_[token.slice(0, index)] = token.slice(index + 2);
    }
    return arguments_;
}

function derivationChain(action, presetName) {
    const chain = [];
    let name = presetName;
    const byName = Object.fromEntries(
        action.presets.map((preset) => [preset.name, preset]));
    while (name && byName[name] && !chain.includes(name)) {
        chain.push(name);
        name = byName[name].derived;
    }
    return chain;
}


// --- Action list -------------------------------------------------------------

function ActionList({ actions, selection, onSelect, query }) {
    const needle = query.toLowerCase();
    const groups = [
        ['Setups', actions.filter((action) => action.is_setup)],
        ['Components', actions.filter((action) => !action.is_setup)],
    ];
    return html`
        <nav class="config-list">
            ${groups.map(([title, entries]) => html`
                <div class="config-group">${title}</div>
                ${entries.map((action) => {
                    const presets = action.presets.filter((preset) =>
                        !needle || `${action.name}:=${preset.name}`
                            .toLowerCase().includes(needle));
                    if (presets.length === 0) return null;
                    return html`
                        <div class="config-action">
                            <div class="config-action-name mono">${action.name}
                                <span class="faint">${presets.length}</span>
                            </div>
                            ${presets.map((preset) => html`
                                <button class="config-preset mono ${selection
                                    && selection.action === action.name
                                    && selection.preset === preset.name
                                    ? 'active' : ''}"
                                    onClick=${() => onSelect({
                                        action: action.name,
                                        preset: preset.name })}>
                                    ${preset.name}${action.default === preset.name
                                        ? html`<span class="tag">default</span>`
                                        : ''}
                                </button>`)}
                        </div>`;
                })}`)}
        </nav>`;
}


// --- Resolution --------------------------------------------------------------

function Resolution({ resolution, onSelect }) {
    const [openAction, setOpenAction] = useState(null);
    const selected = Object.entries(resolution.selected);
    return html`
        <div class="resolution">
            <div class="section-head"><span>${selected.length} actions enabled
                </span></div>
            ${selected.map(([action, preset]) => html`
                <div class="resolution-row">
                    <button class="link-button mono" onClick=${() =>
                        setOpenAction(openAction === action ? null : action)}>
                        ${openAction === action ? '▾' : '▸'} ${action}</button>
                    <button class="link-button mono muted"
                        onClick=${() => onSelect({ action, preset })}>
                        ${preset}</button>
                </div>
                ${openAction === action && html`<pre class="code">${
                    JSON.stringify(resolution.config[action], null, 4)}</pre>`}
            `)}
        </div>`;
}

function PresetDetail({ actions, selection, onSelect }) {
    const [resolved, setResolved] = useState(null);
    const [error, setError] = useState(null);
    const action = actions.find((entry) => entry.name === selection.action);
    const preset = action && action.presets.find((entry) =>
        entry.name === selection.preset);

    useEffect(() => {
        setResolved(null);
        setError(null);
        const request = action && action.is_setup
            ? postJson('/api/config/resolve',
                { arguments: { [selection.action]: selection.preset } })
            : getJson(`/api/config/preset?action=${encodeURIComponent(
                selection.action)}&preset=${encodeURIComponent(
                selection.preset)}`);
        request.then(setResolved).catch((failure) => setError(failure.message));
    }, [selection.action, selection.preset]);

    if (!preset) return null;
    const chain = derivationChain(action, preset.name);
    return html`
        <section class="panel">
            <h2 class="mono">${`${action.name}:=${preset.name}`}</h2>
            ${preset.description && html`<p>${preset.description}</p>`}
            ${chain.length > 1 && html`<p class="muted">Inherits from
                ${chain.slice(1).map((name) => html` <button
                    class="link-button mono" onClick=${() => onSelect({
                        action: action.name, preset: name })}>${name}</button>`)}
                </p>`}
            <div class="section-head"><span>As written</span></div>
            <pre class="code">${JSON.stringify(preset.raw, null, 4)}</pre>
            <div class="section-head"><span>${action.is_setup
                ? 'Selecting it enables' : 'Resolved (inheritance and constants)'}
                </span></div>
            ${error && html`<div class="error-banner">${error}</div>`}
            ${resolved && (action.is_setup
                ? html`<${Resolution} resolution=${resolved}
                    onSelect=${onSelect} />`
                : html`<pre class="code">${JSON.stringify(resolved, null, 4)}
                    </pre>`)}
        </section>`;
}

function ArgumentsTester({ onSelect }) {
    const [text, setText] = useState('');
    const [resolution, setResolution] = useState(null);
    const [error, setError] = useState(null);

    async function resolve(event) {
        event.preventDefault();
        try {
            setResolution(await postJson('/api/config/resolve',
                { arguments: parseArguments(text) }));
            setError(null);
        } catch (failure) {
            setError(failure.message);
            setResolution(null);
        }
    }

    return html`
        <section class="panel">
            <h2>Try arguments</h2>
            <p class="muted">The same <span class="mono">key:=value</span>${
                ' arguments as '}<span class="mono">./src/run.sh</span>; nothing
                starts.</p>
            <form class="button-row" onSubmit=${resolve}>
                <input class="input mono grow" value=${text}
                    placeholder="gz_full:=lance2_ksc foxglove_bridge:=null"
                    onInput=${(event) => setText(event.target.value)} />
                <button class="button primary">Resolve</button>
            </form>
            ${error && html`<div class="error-banner">${error}</div>`}
            ${resolution && html`<${Resolution} resolution=${resolution}
                onSelect=${onSelect} />`}
        </section>`;
}


// --- Page --------------------------------------------------------------------

export function ConfigView() {
    const [catalog, setCatalog] = useState(null);
    const [error, setError] = useState(null);
    const [selection, setSelection] = useState(null);
    const [query, setQuery] = useState('');

    useEffect(() => {
        getJson('/api/config').then(setCatalog)
            .catch((failure) => setError(failure.message));
    }, []);
    const numPresets = useMemo(() => catalog ? catalog.actions.reduce(
        (total, action) => total + action.presets.length, 0) : 0, [catalog]);

    if (error) return html`<div class="error-banner">${error}</div>`;
    if (!catalog) return html`<div class="placeholder">Loading config…</div>`;
    return html`
        <div class="page-head">
            <div>
                <h1>Config</h1>
                <p>${`${catalog.actions.length} actions, ${numPresets} presets `}
                    from <span class="mono">${catalog.path}</span></p>
            </div>
            <input class="input" placeholder="Search presets" value=${query}
                onInput=${(event) => setQuery(event.target.value)} />
        </div>
        <div class="split">
            <${ActionList} actions=${catalog.actions} selection=${selection}
                onSelect=${setSelection} query=${query} />
            <div class="stack">
                <${ArgumentsTester} onSelect=${setSelection} />
                ${selection && html`<${PresetDetail} actions=${catalog.actions}
                    selection=${selection} onSelect=${setSelection} />`}
            </div>
        </div>`;
}
