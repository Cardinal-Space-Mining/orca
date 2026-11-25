// The config browser: every action and preset in lance/config, what a
// preset inherits from, and what a set of `key:=value` arguments resolves to
// (the same preprocessing run.sh does).

import { useEffect, useMemo, useState } from 'preact/hooks';
import { html, getJson, postJson, Icon } from './common.js';


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
                        !needle || `${action.name}:=${preset.name} ${
                            preset.description || ''}`
                            .toLowerCase().includes(needle));
                    if (presets.length === 0 && !(needle && action.name
                        .toLowerCase().includes(needle))) return null;
                    return html`
                        <div class="config-action">
                            <button class="config-action-name mono ${selection
                                && selection.action === action.name
                                && !selection.preset ? 'active' : ''}"
                                title=${action.description || ''}
                                onClick=${() => onSelect({
                                    action: action.name, preset: null })}>
                                ${action.name}
                                <span class="faint">${presets.length}</span>
                            </button>
                            ${presets.map((preset) => html`
                                <button class="config-preset mono ${selection
                                    && selection.action === action.name
                                    && selection.preset === preset.name
                                    ? 'active' : ''}"
                                    title=${preset.description || ''}
                                    onClick=${() => onSelect({
                                        action: action.name,
                                        preset: preset.name })}>
                                    ${preset.name}${preset.problems.length > 0
                                        ? html`<span class="tag warn"
                                            title=${preset.problems.join('; ')}
                                            >check</span>` : ''}${
                                        action.default === preset.name
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

// Inline description editing ("action" or "action:=preset").
function Description({ descriptionKey, text, onSaved, placeholder }) {
    const [draft, setDraft] = useState(null);
    const [error, setError] = useState(null);
    async function save() {
        try {
            await postJson('/api/config/description',
                { key: descriptionKey, text: draft });
            setDraft(null);
            setError(null);
            onSaved();
        } catch (failure) {
            setError(failure.message);
        }
    }
    if (draft === null) {
        return html`
            <p class="description-line">
                ${text ? html`<span>${text}</span>`
                    : html`<span class="faint">${placeholder}</span>`}
                <button class="link-button" title="Edit the description"
                    onClick=${() => setDraft(text || '')}><${Icon}
                    name="pencil" size=${13} /></button>
            </p>`;
    }
    return html`
        <div class="description-edit">
            <textarea class="input" rows="2" value=${draft}
                placeholder=${placeholder}
                onInput=${(event) => setDraft(event.target.value)} />
            <div class="button-row">
                <button class="button primary" onClick=${save}>Save</button>
                <button class="button" onClick=${() => setDraft(null)}>
                    Cancel</button>
            </div>
            ${error && html`<div class="error-banner">${error}</div>`}
        </div>`;
}

// Editing (or creating) one preset: its JSON as written, checked against
// the rest of the config as you type.
function PresetEditor({ catalog, action, preset, initial, onDone, onCancel }) {
    const isNew = !preset;
    const [name, setName] = useState(isNew ? '' : preset.name);
    const [text, setText] = useState(JSON.stringify(initial, null, 4));
    const [description, setDescription] = useState(
        (preset && preset.description) || '');
    const [check, setCheck] = useState(null);
    const [error, setError] = useState(null);
    const [isSaving, setIsSaving] = useState(false);

    const exists = isNew && action.presets.some((entry) => entry.name === name);
    let value = null;
    let parseError = null;
    try {
        value = JSON.parse(text);
    } catch (failure) {
        parseError = failure.message;
    }

    useEffect(() => {
        if (parseError || !name || exists) {
            setCheck(null);
            return undefined;
        }
        let isCurrent = true;
        const timer = setTimeout(() => {
            postJson('/api/config/check', { action: action.name, preset: name,
                value })
                .then((result) => isCurrent && setCheck({ ok: true, ...result }))
                .catch((failure) => isCurrent && setCheck({ ok: false,
                    error: failure.message }));
        }, 350);
        return () => {
            isCurrent = false;
            clearTimeout(timer);
        };
    }, [text, name]);

    async function save() {
        setIsSaving(true);
        try {
            const result = await postJson('/api/config/save', {
                action: action.name, preset: name, value, description });
            onDone(name, result.path);
        } catch (failure) {
            setError(failure.message);
            setIsSaving(false);
        }
    }
    function onKeyDown(event) {
        if (event.key !== 'Tab') return;
        event.preventDefault();
        const area = event.target;
        const start = area.selectionStart;
        setText(`${text.slice(0, start)}    ${text.slice(area.selectionEnd)}`);
        requestAnimationFrame(() => {
            area.selectionStart = area.selectionEnd = start + 4;
        });
    }

    const problem = !name ? 'needs a name' : exists ? 'already exists'
        : !/^[A-Za-z0-9_][A-Za-z0-9_.-]*$/.test(name)
            ? 'letters, digits, _ . -' : null;
    const status = parseError ? { ok: false, error: parseError }
        : problem ? null : check;
    return html`
        <section class="panel">
            <h2>${isNew ? `New ${action.name} preset` : html`Edit${' '}<span
                class="mono">${`${action.name}:=${preset.name}`}</span>`}</h2>
            <p class="muted small-note">Saved into${' '}<span class="mono">${
                (preset && preset.source) || action.source}</span>${'. '}
                Deploy to send it to other machines; commit it in lance to
                share it.</p>
            <div class="fields">
                ${isNew && html`<label class="field">
                    <span>Name ${problem && html`<em class="field-error">${
                        problem}</em>`}</span>
                    <input class="input mono" value=${name}
                        placeholder="lance2_test"
                        onInput=${(event) => setName(event.target.value.trim())} />
                </label>`}
                <label class="field wide">
                    <span>Description</span>
                    <input class="input" value=${description}
                        placeholder="What it's for and when to pick it"
                        onInput=${(event) => setDescription(event.target.value)} />
                </label>
            </div>
            <div class="check-line ${status ? (status.ok ? 'good' : 'bad') : ''}">
                ${!status ? (problem ? '' : 'Checking…') : status.ok
                    ? html`<${Icon} name="circle-check" size=${14} />${
                        ' launch-utils resolves it'}`
                    : html`<${Icon} name="triangle-alert" size=${14} />${
                        ` ${status.error}`}`}</div>
            ${status && status.ok && status.problems.map((entry) => html`
                <div class="check-line warn"><${Icon} name="triangle-alert"
                    size=${14} />${` ${entry}`}</div>`)}
            <textarea class="input code-editor preset-text" spellcheck="false"
                value=${text} onKeyDown=${onKeyDown}
                onInput=${(event) => setText(event.target.value)} />
            ${error && html`<div class="error-banner">${error}</div>`}
            <div class="button-row editor-actions">
                <button class="button primary" onClick=${save}
                    disabled=${isSaving || !!problem || !status || !status.ok}>
                    <${Icon} name="check" size=${14} />${isSaving ? ' Saving…'
                        : ' Save'}</button>
                <button class="button" onClick=${onCancel}>Cancel</button>
            </div>
            ${status && status.ok && html`
                <div class="section-head"><span>${action.is_setup
                    ? 'Selecting it enables' : 'Resolved'}</span></div>
                ${action.is_setup
                    ? html`<${Resolution} resolution=${{ selected:
                        status.selected, config: {} }} onSelect=${() => {}} />`
                    : html`<pre class="code">${JSON.stringify(status.resolved,
                        null, 4)}</pre>`}`}
        </section>`;
}

function PresetDetail({ actions, selection, onSelect, onChanged, onEdit }) {
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
    }, [selection.action, selection.preset, preset && preset.raw]);

    async function remove() {
        if (!window.confirm(`Delete ${action.name}:=${preset.name} from ${
            preset.source}?`)) return;
        try {
            await postJson('/api/config/delete',
                { action: action.name, preset: preset.name });
            onChanged(null);
        } catch (failure) {
            setError(failure.message);
        }
    }

    if (!preset) return null;
    const chain = derivationChain(action, preset.name);
    return html`
        <section class="panel">
            <div class="panel-title-row">
                <h2 class="mono">${`${action.name}:=${preset.name}`}</h2>
                <div class="button-row">
                    <button class="button" onClick=${() => onEdit(
                        { mode: 'edit' })}><${Icon} name="pencil" size=${14} />
                        ${' Edit'}</button>
                    <button class="button" title="A new preset that inherits
                        from this one" onClick=${() => onEdit({ mode: 'derive' })}>
                        <${Icon} name="git-branch" size=${14} />${' Derive'}
                    </button>
                    <button class="button danger" onClick=${remove}><${Icon}
                        name="trash-2" size=${14} />${' Delete'}</button>
                </div>
            </div>
            <${Description} key=${`${action.name}:=${preset.name}`}
                descriptionKey=${`${action.name}:=${preset.name}`}
                text=${preset.description} onSaved=${() => onChanged(selection)}
                placeholder="No description yet" />
            <p class="muted small-note">In <span class="mono">${preset.source}
                </span>${chain.length > 1 && html`${' · inherits from'}
                ${chain.slice(1).map((name) => html` <button
                    class="link-button mono" onClick=${() => onSelect({
                        action: action.name, preset: name })}>${name}</button>`)}`}
                </p>
            ${preset.problems.map((entry) => html`
                <div class="check-line warn"><${Icon} name="triangle-alert"
                    size=${14} />${` ${entry}`}</div>`)}
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

function ActionHeader({ action, onChanged, onNew }) {
    return html`
        <section class="panel action-header">
            <div class="panel-title-row">
                <h2><span class="mono">${action.name}</span>${' '}<span
                    class="faint">${`${action.is_setup ? 'setup'
                    : 'component'} · ${action.presets.length} presets`}</span>
                    </h2>
                <button class="button" onClick=${onNew}><${Icon} name="plus"
                    size=${14} />${' New preset'}</button>
            </div>
            <${Description} key=${action.name} descriptionKey=${action.name}
                text=${action.description} onSaved=${onChanged}
                placeholder=${`What ${action.name} is for`} />
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
    const [editing, setEditing] = useState(null);  // {mode, action}
    const [notice, setNotice] = useState(null);
    const [query, setQuery] = useState('');

    const load = () => getJson('/api/config').then(setCatalog)
        .catch((failure) => setError(failure.message));
    useEffect(() => {
        load();
    }, []);
    const numPresets = useMemo(() => catalog ? catalog.actions.reduce(
        (total, action) => total + action.presets.length, 0) : 0, [catalog]);

    if (error) return html`<div class="error-banner">${error}</div>`;
    if (!catalog) return html`<div class="placeholder">Loading config…</div>`;
    const select = (next) => {
        setSelection(next);
        setEditing(null);
        setNotice(null);
    };
    const action = selection && catalog.actions.find((entry) =>
        entry.name === selection.action);
    const preset = action && action.presets.find((entry) =>
        entry.name === selection.preset);

    let editor = null;
    if (editing && action) {
        const initial = editing.mode === 'edit' ? preset.raw
            : editing.mode === 'derive' ? { 'pragma:derived': preset.name }
                : action.is_setup ? { 'pragma:action_overrides': {} } : {};
        editor = html`<${PresetEditor} key=${JSON.stringify(editing)}
            catalog=${catalog} action=${action}
            preset=${editing.mode === 'edit' ? preset : null}
            initial=${initial}
            onCancel=${() => setEditing(null)}
            onDone=${async (name, path) => {
                await load();
                setSelection({ action: action.name, preset: name });
                setEditing(null);
                setNotice(`Saved ${action.name}:=${name} in ${path}.`);
            }} />`;
    }
    return html`
        <div class="page-head">
            <div>
                <h1>Config</h1>
                <p>${`${catalog.actions.length} actions, ${numPresets} presets `}
                    from <span class="mono">${catalog.path}</span>; descriptions
                    in <span class="mono">${catalog.descriptions_path}</span></p>
            </div>
            <input class="input" placeholder="Search presets" value=${query}
                onInput=${(event) => setQuery(event.target.value)} />
        </div>
        <div class="split">
            <${ActionList} actions=${catalog.actions} selection=${selection}
                onSelect=${select} query=${query} />
            <div class="stack">
                ${notice && html`<div class="ok-banner">${notice}</div>`}
                ${action && html`<${ActionHeader} action=${action}
                    onChanged=${load}
                    onNew=${() => setEditing({ mode: 'new',
                        action: action.name })} />`}
                ${editor || (preset && html`<${PresetDetail}
                    actions=${catalog.actions} selection=${selection}
                    onSelect=${select}
                    onEdit=${(next) => setEditing({ ...next,
                        action: action.name, preset: preset.name })}
                    onChanged=${async (next) => {
                        await load();
                        if (next === null) {
                            setSelection({ action: selection.action,
                                preset: null });
                            setNotice(`Deleted ${selection.action}:=${
                                selection.preset}.`);
                        }
                    }} />`)}
                ${!editing && html`<${ArgumentsTester} onSelect=${select} />`}
            </div>
        </div>`;
}
