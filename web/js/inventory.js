// Editing the machine inventory: the team's file (in the workspace, shared
// through git) and this user's own. Saving checks the result, writes the
// file and applies it right away: new machines connect, removed ones
// disconnect.

import { useEffect, useState } from 'preact/hooks';
import { html, getJson, postJson } from './common.js';


const SCOPES = [
    { id: 'project', label: 'Team machines',
      help: 'src/project/orchestrator/machines.json: shared with the team '
          + 'through git (commit it after editing).' },
    { id: 'user', label: 'My machines',
      help: '~/.config/orchestrator/machines.json: only on this laptop. '
          + 'Entries here replace team entries with the same id.' },
];

const EMPTY_ROUTE = { host: '', user: '' };

function cleanEntry(entry) {
    // Drop empty optional fields so the file stays tidy.
    const cleaned = {};
    for (const [key, value] of Object.entries(entry)) {
        if (value === '' || value === null || value === undefined) continue;
        cleaned[key] = value;
    }
    cleaned.routes = (entry.routes || []).map((route) => {
        const tidy = {};
        for (const [key, value] of Object.entries(route)) {
            if (value === '' || value === null || value === undefined) continue;
            tidy[key] = key === 'port' ? Number(value) : value;
        }
        return tidy;
    });
    if (cleaned.config_addresses) {
        const mapping = Object.fromEntries(Object.entries(
            cleaned.config_addresses).filter(([name]) => name.trim()));
        if (Object.keys(mapping).length) cleaned.config_addresses = mapping;
        else delete cleaned.config_addresses;
    }
    if (cleaned.vm) {
        cleaned.vm = { ...cleaned.vm };
        if (!cleaned.vm.guest) delete cleaned.vm.guest;
    }
    return cleaned;
}


// --- One machine -------------------------------------------------------------

// Address constants in the lance config ("ip.net1.mac_vm_eth" -> value),
// fetched once for suggestions.
let constantsPromise = null;

function useAddressConstants() {
    const [constants, setConstants] = useState({});
    useEffect(() => {
        constantsPromise = constantsPromise || getJson('/api/config')
            .then((config) => config.constants || {})
            .catch(() => ({}));
        constantsPromise.then(setConstants);
    }, []);
    return constants;
}

function ConfigAddresses({ value, onChange }) {
    const constants = useAddressConstants();
    const entries = Object.entries(value || {});
    const update = (next) => onChange(
        next.length ? Object.fromEntries(next) : null);
    const replace = (index, entry) => update(entries.map(
        (other, position) => position === index ? entry : other));
    return html`
        <div class="section-head"><span>Config addresses</span></div>
        <p class="faint small-note">lance config constants that hold this
            machine's address. Launches from the orchestrator fill them with
            its current address, so the config files can be out of date.</p>
        ${entries.map(([name, kind], index) => html`
            <div class="config-address-row" key=${index}>
                <input class="input mono" list="address-constants"
                    value=${name} placeholder="ip.net1.mac_vm_eth"
                    onInput=${(event) => replace(index,
                        [event.target.value, kind])} />
                <select class="input" value=${kind}
                    onChange=${(event) => replace(index,
                        [name, event.target.value])}>
                    <option value="ethernet">its Ethernet address</option>
                    <option value="wifi">its Wi-Fi address</option>
                </select>
                <span class="faint mono">${name && (name in constants
                    ? `config: ${constants[name]}` : 'not in the config')}
                    </span>
                <button class="link-button" title="Remove"
                    onClick=${() => update(entries.filter(
                        (_, position) => position !== index))}>✕</button>
            </div>`)}
        <datalist id="address-constants">${Object.keys(constants).map(
            (name) => html`<option value=${name} />`)}</datalist>
        <button class="button small" onClick=${() =>
            update([...entries, ['', 'ethernet']])}>
            Add config address</button>`;
}

function TextField({ label, value, placeholder, onChange, mono = false }) {
    return html`
        <label class="field">
            <span>${label}</span>
            <input class="input ${mono ? 'mono' : ''}" value=${value || ''}
                placeholder=${placeholder || ''}
                onInput=${(event) => onChange(event.target.value)} />
        </label>`;
}

function MachineSelect({ label, value, ids, onChange, emptyLabel }) {
    return html`
        <label class="field">
            <span>${label}</span>
            <select class="input" value=${value || ''}
                onChange=${(event) => onChange(event.target.value)}>
                <option value="">${emptyLabel}</option>
                ${ids.map((id) => html`<option value=${id}>${id}</option>`)}
            </select>
        </label>`;
}

function RouteRow({ route, index, count, ids, defaults, onChange, onMove,
    onRemove }) {
    const set = (key) => (value) => onChange({ ...route, [key]: value });
    return html`
        <div class="route-row">
            <span class="route-number">${index + 1}</span>
            <input class="input mono" placeholder="address" value=${route.host}
                onInput=${(event) => set('host')(event.target.value)} />
            <input class="input mono" placeholder=${defaults.user || 'user'}
                value=${route.user || ''}
                onInput=${(event) => set('user')(event.target.value)} />
            <input class="input mono short" placeholder="22"
                value=${route.port || ''}
                onInput=${(event) => set('port')(event.target.value)} />
            <select class="input" value=${route.via || ''}
                onChange=${(event) => set('via')(event.target.value)}>
                <option value="">direct</option>
                ${ids.map((id) => html`<option value=${id}>via ${id}</option>`)}
            </select>
            <input class="input mono"
                placeholder=${defaults.host_key_alias || 'host key name'}
                value=${route.host_key_alias || ''}
                onInput=${(event) => set('host_key_alias')(
                    event.target.value)} />
            <span class="route-buttons">
                <button class="link-button" disabled=${index === 0}
                    title="Try earlier" onClick=${() => onMove(-1)}>↑</button>
                <button class="link-button" disabled=${index === count - 1}
                    title="Try later" onClick=${() => onMove(1)}>↓</button>
                <button class="link-button" title="Remove"
                    onClick=${onRemove}>✕</button>
            </span>
        </div>`;
}

function MachineForm({ entry, ids, onChange, onRemove }) {
    const [isOpen, setIsOpen] = useState(!entry.id);
    const set = (key) => (value) => onChange({ ...entry, [key]: value });
    const routes = entry.routes || [];
    const setRoutes = (next) => onChange({ ...entry, routes: next });
    const otherIds = ids.filter((id) => id !== entry.id);
    return html`
        <div class="machine-form ${isOpen ? 'open' : ''}">
            <button class="machine-form-head" onClick=${() =>
                setIsOpen(!isOpen)}>
                <span>${isOpen ? '▾' : '▸'}</span>
                <b>${entry.label || entry.id || 'New machine'}</b>
                <span class="mono faint">${entry.id}</span>
                <span class="faint">${routes.map((route) => route.host)
                    .filter(Boolean).join(', ')}</span>
            </button>
            ${isOpen && html`
                <div class="machine-form-body">
                    <div class="fields">
                        <${TextField} label="Id" value=${entry.id} mono
                            placeholder="robot-vm" onChange=${set('id')} />
                        <${TextField} label="Name" value=${entry.label}
                            placeholder="Robot VM" onChange=${set('label')} />
                        <label class="field wide">
                            <span>Role</span>
                            <input class="input" value=${entry.role || ''}
                                placeholder="What it's for"
                                onInput=${(event) => set('role')(
                                    event.target.value)} />
                        </label>
                    </div>
                    <div class="fields">
                        <${TextField} label="Workspace" value=${entry.workspace}
                            mono placeholder="~/lance-ws (none if empty)"
                            onChange=${set('workspace')} />
                        <${MachineSelect} label="Runs on (VMs)"
                            value=${entry.host} ids=${otherIds}
                            emptyLabel="its own hardware"
                            onChange=${set('host')} />
                    </div>
                    <div class="fields">
                        <${TextField} label="User" value=${entry.user} mono
                            placeholder="for every route, e.g. lance"
                            onChange=${set('user')} />
                        <${TextField} label="Host key name"
                            value=${entry.host_key_alias} mono
                            placeholder=${`${entry.id || 'robot-vm'}`
                                + '.orchestrator'}
                            onChange=${set('host_key_alias')} />
                    </div>
                    <label class="check">
                        <input type="checkbox" checked=${!!entry.vm}
                            onChange=${(event) => set('vm')(event.target.checked
                                ? { csm_vm_path: '~/code/csm-vm' } : null)} />
                        Hosts a csm-vm virtual machine</label>
                    ${entry.vm && html`<div class="fields">
                        <${TextField} label="csm-vm folder" mono
                            value=${entry.vm.csm_vm_path}
                            onChange=${(value) => set('vm')({
                                ...entry.vm, csm_vm_path: value })} />
                        <${MachineSelect} label="The VM's own entry"
                            value=${entry.vm.guest} ids=${otherIds}
                            emptyLabel="none"
                            onChange=${(value) => set('vm')({
                                ...entry.vm, guest: value })} />
                    </div>`}
                    <div class="section-head"><span>Routes, tried in order
                        </span></div>
                    <p class="faint small-note">Names or addresses to try
                        first. Addresses the machine reports while connected
                        are remembered on this laptop and tried too, so these
                        can be out of date; its host key proves it's the right
                        machine.</p>
                    <div class="route-head">
                        <span></span><span>Address</span><span>User</span>
                        <span>Port</span><span>Via</span>
                        <span>Host key name</span><span></span>
                    </div>
                    ${routes.map((route, index) => html`<${RouteRow}
                        key=${index} route=${route} index=${index}
                        count=${routes.length} ids=${otherIds}
                        defaults=${entry}
                        onChange=${(next) => setRoutes(routes.map(
                            (other, position) => position === index
                                ? next : other))}
                        onMove=${(step) => {
                            const next = [...routes];
                            const [moved] = next.splice(index, 1);
                            next.splice(index + step, 0, moved);
                            setRoutes(next);
                        }}
                        onRemove=${() => setRoutes(routes.filter(
                            (_, position) => position !== index))} />`)}
                    <div class="button-row">
                        <button class="button small" onClick=${() =>
                            setRoutes([...routes, { ...EMPTY_ROUTE }])}>
                            Add route</button>
                    </div>
                    <${ConfigAddresses} value=${entry.config_addresses}
                        onChange=${set('config_addresses')} />
                    <div class="button-row">
                        <span class="spacer"></span>
                        <button class="button small danger"
                            onClick=${onRemove}>Remove machine</button>
                    </div>
                </div>`}
        </div>`;
}


// --- One file ----------------------------------------------------------------

function InventoryFile({ scope, file, allIds, notice, onSaved }) {
    const [entries, setEntries] = useState(file.machines);
    const [isJson, setIsJson] = useState(false);
    const [jsonText, setJsonText] = useState('');
    const [message, setMessage] = useState(notice);
    const [isSaving, setIsSaving] = useState(false);
    const isChanged = JSON.stringify(entries) !== JSON.stringify(file.machines);

    function toggleJson() {
        if (!isJson) {
            setJsonText(JSON.stringify(entries, null, 4));
        } else {
            try {
                setEntries(JSON.parse(jsonText));
            } catch (failure) {
                setMessage({ error: `Not valid JSON: ${failure.message}` });
                return;
            }
        }
        setMessage(null);
        setIsJson(!isJson);
    }

    async function save() {
        let machines = entries;
        if (isJson) {
            try {
                machines = JSON.parse(jsonText);
            } catch (failure) {
                setMessage({ error: `Not valid JSON: ${failure.message}` });
                return;
            }
        }
        machines = machines.map(cleanEntry);
        setIsSaving(true);
        try {
            const result = await postJson(`/api/inventory/${scope.id}`,
                { machines });
            onSaved({ ok: `Saved to ${result.path}. Machines updated.` });
        } catch (failure) {
            setMessage({ error: failure.message });
        }
        setIsSaving(false);
    }

    return html`
        <section class="panel">
            <div class="run-head">
                <div>
                    <h2>${scope.label}</h2>
                    <p class="muted">${scope.help}</p>
                </div>
                <div class="button-row">
                    <button class="button" onClick=${toggleJson}>
                        ${isJson ? 'Form' : 'JSON'}</button>
                    <button class="button" disabled=${!isChanged || isSaving}
                        onClick=${() => {
                            setEntries(file.machines);
                            setIsJson(false);
                            setMessage(null);
                        }}>Undo changes</button>
                    <button class="button primary" onClick=${save}
                        disabled=${isSaving || (!isChanged && !isJson)}>
                        ${isSaving ? 'Saving…' : 'Save'}</button>
                </div>
            </div>
            ${message && message.error && html`<div class="error-banner">
                ${message.error}</div>`}
            ${message && message.ok && html`<div class="ok-banner">
                ${message.ok}</div>`}
            ${isJson
                ? html`<textarea class="input code-editor" spellcheck="false"
                    value=${jsonText}
                    onInput=${(event) => setJsonText(event.target.value)} />`
                : html`
                    ${entries.length === 0 && html`<div class="faint">
                        No machines in this file yet.</div>`}
                    ${entries.map((entry, index) => html`<${MachineForm}
                        key=${index} entry=${entry} ids=${allIds}
                        onChange=${(next) => setEntries(entries.map(
                            (other, position) => position === index
                                ? next : other))}
                        onRemove=${() => {
                            if (window.confirm(`Remove ${entry.label
                                || entry.id || 'this machine'}?`)) {
                                setEntries(entries.filter(
                                    (_, position) => position !== index));
                            }
                        }} />`)}
                    <div class="button-row">
                        <button class="button" onClick=${() => setEntries([
                            ...entries, { id: '', label: '',
                                routes: [{ ...EMPTY_ROUTE }] }])}>
                            Add machine</button>
                    </div>`}
        </section>`;
}


// --- Page --------------------------------------------------------------------

export function InventoryView() {
    const [inventory, setInventory] = useState(null);
    const [scopeId, setScopeId] = useState('project');
    const [error, setError] = useState(null);
    const [version, setVersion] = useState(0);
    const [notice, setNotice] = useState(null);

    useEffect(() => {
        getJson('/api/inventory').then(setInventory)
            .catch((failure) => setError(failure.message));
    }, [version]);

    if (error) return html`<div class="error-banner">${error}</div>`;
    if (!inventory) return html`<div class="placeholder">Loading…</div>`;
    const scopes = SCOPES.filter((scope) => inventory[scope.id]);
    const scope = scopes.find((entry) => entry.id === scopeId) || scopes[0];
    const allIds = ['local', ...new Set(Object.values(inventory).flatMap(
        (file) => file.machines.map((entry) => entry.id).filter(Boolean)))];
    return html`
        <div class="page-head"><div>
            <h1>Edit machines</h1>
            <p><a class="link-button" href="#/machines">← Machines</a></p>
        </div></div>
        <div class="machine-tabs">
            ${scopes.map((entry) => html`
                <button class="process-tab ${entry === scope ? 'active' : ''}"
                    onClick=${() => setScopeId(entry.id)}>
                    <span><b>${entry.label}</b>
                        <small>${inventory[entry.id].machines.length}
                            ${' machines'}</small></span>
                </button>`)}
        </div>
        <${InventoryFile} key=${`${scope.id}-${version}`} scope=${scope}
            file=${inventory[scope.id]} allIds=${allIds} notice=${notice}
            onSaved=${(message) => {
                setNotice(message);
                setVersion(version + 1);
            }} />`;
}
