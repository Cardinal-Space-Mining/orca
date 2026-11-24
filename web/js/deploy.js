// The deploy page: copy this workspace's src/ to other machines (rsync over
// the orchestrator's SSH routes) and build there.

import { useState } from 'preact/hooks';
import { html, formatAgo, postJson, shortCommit, StatusDot }
    from './common.js';


// --- Code status -------------------------------------------------------------

function codeBadge(workspace, reference) {
    if (!workspace || !workspace.exists) {
        return html`<span class="pill">not deployed yet</span>`;
    }
    if (!workspace.fingerprint || !reference) {
        return html`<span class="pill">unknown</span>`;
    }
    return workspace.fingerprint === reference
        ? html`<span class="pill good">same code</span>`
        : html`<span class="pill warn">different code</span>`;
}

function SourcePanel({ local }) {
    const workspace = local && local.workspace;
    if (!workspace) return null;
    return html`
        <section class="panel source-panel">
            <div>
                <div class="section-head"><span>From this machine</span></div>
                <div class="mono">${workspace.path}</div>
            </div>
            <div>
                <div class="section-head"><span>Code</span></div>
                <span class="mono">${workspace.branch} @ ${shortCommit(
                    workspace.head)}</span>
                ${workspace.num_changes > 0 && html`<span class="pill warn">
                    ${workspace.num_changes} uncommitted (deployed too)</span>`}
            </div>
            <div>
                <div class="section-head"><span>Fingerprint</span></div>
                <span class="mono muted">${workspace.fingerprint}</span>
            </div>
        </section>`;
}


// --- Comparison --------------------------------------------------------------

const CHANGE_LABELS = { new: 'new', update: 'changed', delete: 'deleted' };

function Comparison({ result }) {
    const [showGit, setShowGit] = useState(false);
    if (result.error) return html`<div class="error-banner">${result.error}
        </div>`;
    if (result.is_up_to_date) {
        return html`<div class="compare-box"><span class="pill good">
            up to date</span> nothing to copy</div>`;
    }
    const lost = result.remote_only_refs.filter((entry) => !entry.kept);
    const kept = result.remote_only_refs.filter((entry) => entry.kept);
    const changes = result.changes.filter((change) =>
        showGit || !change.is_git);
    return html`
        <div class="compare-box">
            <div class="compare-summary">
                <b>${result.num_code_updated}</b> changed ·
                <b>${' ' + result.num_code_new}</b> new ·
                <b>${' ' + result.num_code_deleted}</b> deleted
                <span class="faint">${` (+ ${result.num_git} git files)`}</span>
            </div>
            ${result.remote_changes.length > 0 && html`
                <div class="warn-banner">Uncommitted changes there will be
                    overwritten:
                    <div class="mono small-list">${result.remote_changes.map(
                        (line) => html`<div>${line}</div>`)}</div></div>`}
            ${lost.length > 0 && html`
                <div class="warn-banner">Commits that exist only there and
                    would become unreachable (refs in packed-refs):
                    ${lost.map((entry) => html` <span class="mono">${
                        `${entry.ref} ${entry.sha}`}</span>`)}</div>`}
            ${kept.length > 0 && html`
                <div class="muted small">Kept there (only that machine has
                    them; the orchestrator never deletes inside .git):
                    ${kept.map((entry) => html` <span class="mono">${
                        entry.ref}</span>`)}</div>`}
            <div class="change-list mono">
                ${changes.map((change) => html`
                    <div class="change ${change.change}">
                        <span>${CHANGE_LABELS[change.change]}</span>
                        ${change.path}</div>`)}
                ${result.num_unlisted > 0 && html`<div class="faint">
                    ${`… and ${result.num_unlisted} more`}</div>`}
            </div>
            <button class="link-button" onClick=${() => setShowGit(!showGit)}>
                ${showGit ? 'Hide git files' : 'Show git files too'}</button>
        </div>`;
}


// --- Targets -----------------------------------------------------------------

function TargetRow({ entry, reference, isSelected, onToggle, now }) {
    const [comparison, setComparison] = useState(null);
    const [isComparing, setIsComparing] = useState(false);
    const { machine, connection, workspace } = entry;
    const isOnline = connection.state === 'online';

    async function compare() {
        setIsComparing(true);
        try {
            setComparison(await postJson('/api/deploy/compare',
                { machine_id: machine.id }));
        } catch (failure) {
            setComparison({ error: failure.message });
        }
        setIsComparing(false);
    }

    return html`
        <div class="target ${isOnline ? '' : 'offline'}">
            <label class="target-row">
                <input type="checkbox" checked=${isSelected}
                    disabled=${!isOnline} onChange=${onToggle} />
                <${StatusDot} state=${connection.state} />
                <span class="target-name"><b>${machine.label}</b>
                    <span class="faint mono">${machine.id}</span></span>
                <span class="mono muted">${workspace && workspace.head
                    ? `${workspace.branch} @ ${shortCommit(workspace.head)}`
                    : (isOnline ? '' : connection.state)}</span>
                <span>${isOnline && codeBadge(workspace, reference)}</span>
                <span class="faint">${workspace && workspace.has_install
                    ? `built ${formatAgo(workspace.last_build_s, now)}` : ''}
                    </span>
                <span class="faint mono">${workspace
                    && (workspace.ros_distro || '')}</span>
                <button class="button small" disabled=${!isOnline
                    || isComparing} onClick=${(event) => {
                        event.preventDefault();
                        compare();
                    }}>${isComparing ? 'Comparing…' : 'Compare'}</button>
            </label>
            ${comparison && html`<${Comparison} result=${comparison} />`}
        </div>`;
}


// --- Page --------------------------------------------------------------------

export function DeployView({ state, onLaunched, now }) {
    const [selected, setSelected] = useState({});
    const [options, setOptions] = useState({
        push: true, delete: true, build: true, robot_target: 'all',
        no_sim: true, native_arch: false });
    const [error, setError] = useState(null);
    const [isStarting, setIsStarting] = useState(false);

    const local = state.machines.find((entry) => entry.machine.is_local);
    const reference = local && local.workspace
        && local.workspace.fingerprint;
    const targets = state.machines.filter((entry) =>
        entry.machine.workspace && !entry.machine.is_local);
    const chosen = targets.filter((entry) => selected[entry.machine.id]
        && entry.connection.state === 'online');
    const setOption = (name, value) => setOptions((previous) => ({
        ...previous, [name]: value }));

    async function deploy() {
        setIsStarting(true);
        setError(null);
        try {
            const run = await postJson('/api/deploy', {
                machine_ids: chosen.map((entry) => entry.machine.id),
                push: options.push, build: options.build,
                delete: options.delete,
                build_options: { robot_target: options.robot_target,
                    no_sim: options.no_sim,
                    native_arch: options.native_arch },
            });
            onLaunched(run.run_id);
        } catch (failure) {
            setError(failure.message);
        }
        setIsStarting(false);
    }

    const check = (name, label, disabled = false) => html`
        <label class="check"><input type="checkbox" checked=${options[name]}
            disabled=${disabled}
            onChange=${(event) => setOption(name, event.target.checked)} />
            ${label}</label>`;

    return html`
        <div class="page-head"><div>
            <h1>Deploy</h1>
            <p>Copy this workspace's <span class="mono">src/</span> to other
                machines and build there. Robot and driver station must run the
                same code.</p>
        </div></div>
        <${SourcePanel} local=${local} />
        <section class="panel">
            <h2>Machines</h2>
            <div class="targets">
                ${targets.map((entry) => html`<${TargetRow}
                    key=${entry.machine.id} entry=${entry}
                    reference=${reference} now=${now}
                    isSelected=${!!selected[entry.machine.id]}
                    onToggle=${() => setSelected((previous) => ({
                        ...previous,
                        [entry.machine.id]: !previous[entry.machine.id] }))}
                    />`)}
            </div>
        </section>
        <section class="panel">
            <h2>Steps</h2>
            <div class="options">
                <div>
                    ${check('push', 'Push code')}
                    ${check('delete', 'Delete files there that were deleted '
                        + 'here', !options.push)}
                </div>
                <div>
                    ${check('build', 'Build after pushing')}
                    <label class="field">
                        <span>Robot target</span>
                        <select class="input" value=${options.robot_target}
                            disabled=${!options.build}
                            onChange=${(event) => setOption('robot_target',
                                event.target.value)}>
                            <option value="all">LANCE-1 and LANCE-2</option>
                            <option value="lance2">LANCE-2 only (faster)</option>
                            <option value="lance1">LANCE-1 only</option>
                        </select>
                    </label>
                    ${check('no_sim', 'Skip csm-sim', !options.build)}
                    ${check('native_arch', 'Optimize for that CPU '
                        + '(-march=native)', !options.build)}
                </div>
            </div>
            ${error && html`<div class="error-banner">${error}</div>`}
            <div class="button-row">
                <button class="button primary large" onClick=${deploy}
                    disabled=${chosen.length === 0 || isStarting
                        || (!options.push && !options.build)}>
                    ${isStarting ? 'Starting…' : chosen.length
                        ? `Deploy to ${chosen.length} machine${
                            chosen.length > 1 ? 's' : ''}`
                        : 'Choose machines above'}</button>
                <span class="faint">Pushes run from this machine; each build
                    starts once its push succeeds.</span>
            </div>
        </section>`;
}
