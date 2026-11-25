// Editing a scenario file (src/project/orchestrator/scenarios/<id>.json):
// its JSON, checked as you type, with the launch it would make previewed
// beside it. Saving writes the file in the workspace; commit it to share it.

import { useEffect, useState } from 'preact/hooks';
import { html, getJson, postJson, Icon } from './common.js';
import { ScenarioForm } from './launch.js';


const TEMPLATE = {
    label: 'New scenario',
    description: 'What it runs and when to use it.',
    order: 100,
    roles: {
        machine: { label: 'Machine', default: 'local' },
    },
    options: {},
    processes: [{
        key: 'main',
        label: 'Robot code',
        role: 'machine',
        lance: { flags: ['--local'], args: { dev: 'robot_motor_sim' } },
    }],
};

const ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,40}$/;

function FormatHelp() {
    return html`
        <details class="format-help">
            <summary>Scenario format</summary>
            <dl>
                <dt>label, description, order</dt>
                <dd>How it's listed on the launch page (lower order first).</dd>
                <dt>roles</dt>
                <dd>${'{"robot": {"label": "Robot computer", "default": '
                    + '"robot-vm"}}'}: the machines it needs; picked at launch.
                    </dd>
                <dt>options</dt>
                <dd>${'{"arena": {"label": "Arena", "default": "ksc", '
                    + '"choices": {"ksc": "KSC"}}}'}, or a free text option
                    with "placeholder" instead of "choices".</dd>
                <dt>processes</dt>
                <dd>Each has a unique "key", a "role", an optional "label",
                    and either "lance" (<span class="mono">run.sh</span>
                    "flags", "args" as <span class="mono">key:=value</span>,
                    and "set" for config values through an overlay) or
                    "command" (a shell command, run in the workspace).</dd>
                <dt>after, start_delay_s</dt>
                <dd>Start after other processes (by key) are running, plus a
                    delay. "task": true for something that's done when it
                    exits.</dd>
                <dt>Placeholders</dt>
                <dd><span class="mono">{option}</span> is an option's value;
                    <span class="mono">{ip:role}</span> the address of that
                    role's machine as reached from the process's machine.</dd>
            </dl>
        </details>`;
}

export function ScenarioEditor({ state, scenarioId }) {
    // "<id>~copy" duplicates <id>; no id is a new scenario.
    const [sourceId, isCopy] = scenarioId
        ? [scenarioId.replace(/~copy$/, ''), scenarioId.endsWith('~copy')]
        : [null, false];
    const isNew = !sourceId || isCopy;
    const [id, setId] = useState(isNew ? '' : sourceId);
    const [text, setText] = useState(null);
    const [check, setCheck] = useState(null);
    const [error, setError] = useState(null);
    const [isSaving, setIsSaving] = useState(false);

    useEffect(() => {
        if (!sourceId) {
            setText(JSON.stringify(TEMPLATE, null, 4) + '\n');
            return;
        }
        getJson(`/api/scenarios/${encodeURIComponent(sourceId)}/source`)
            .then((source) => {
                if (!source.exists) throw new Error(`no scenario ${sourceId}`);
                if (!isCopy) {
                    setText(source.text);
                    return;
                }
                const data = JSON.parse(source.text);
                data.label = `${data.label || sourceId} (copy)`;
                setId(`${sourceId}-copy`);
                setText(JSON.stringify(data, null, 4) + '\n');
            }).catch((failure) => setError(failure.message));
    }, [scenarioId]);

    // Check as you type (a little after you stop).
    useEffect(() => {
        if (text === null) return undefined;
        let isCurrent = true;
        const timer = setTimeout(async () => {
            try {
                const result = await postJson(`/api/scenarios/${
                    encodeURIComponent(id || 'draft')}/check`, { text });
                if (isCurrent) setCheck(result);
            } catch (failure) {
                if (isCurrent) setCheck({ ok: false, error: failure.message });
            }
        }, 400);
        return () => {
            isCurrent = false;
            clearTimeout(timer);
        };
    }, [text, id]);

    const idProblem = !id ? 'needs an id'
        : !ID_PATTERN.test(id) ? 'lowercase letters, digits and -' : null;

    async function save() {
        setIsSaving(true);
        setError(null);
        try {
            if (isNew) {
                const existing = await getJson(`/api/scenarios/${
                    encodeURIComponent(id)}/source`);
                if (existing.exists) {
                    throw new Error(`there is already a scenario "${id}"`);
                }
            }
            await postJson(`/api/scenarios/${encodeURIComponent(id)}`,
                { text });
            window.location.hash = `#/launch/${encodeURIComponent(id)}`;
        } catch (failure) {
            setError(failure.message);
            setIsSaving(false);
        }
    }
    async function remove() {
        if (!window.confirm(`Delete the scenario file ${id}.json?`)) return;
        try {
            await postJson(`/api/scenarios/${encodeURIComponent(id)}/delete`);
            window.location.hash = '#/launch';
        } catch (failure) {
            setError(failure.message);
        }
    }
    // Tab indents instead of leaving the editor.
    function onKeyDown(event) {
        if (event.key !== 'Tab') return;
        event.preventDefault();
        const area = event.target;
        const start = area.selectionStart;
        const next = `${text.slice(0, start)}    ${text.slice(
            area.selectionEnd)}`;
        setText(next);
        requestAnimationFrame(() => {
            area.selectionStart = area.selectionEnd = start + 4;
        });
    }

    if (error && text === null) {
        return html`<div class="error-banner">${error}</div>`;
    }
    if (text === null) return html`<div class="placeholder">Loading…</div>`;
    const draft = check && check.ok ? check.scenario : null;
    return html`
        <div class="page-head">
            <div>
                <h1>${isNew ? 'New scenario' : `Edit ${sourceId}`}</h1>
                <p><a class="link-button" href=${sourceId && !isCopy
                    ? `#/launch/${encodeURIComponent(sourceId)}`
                    : '#/launch'}>← Launch</a>${' · '}saved to${' '}<span
                    class="mono">${`src/project/orchestrator/scenarios/${
                    id || '<id>'}.json`}</span>${' '}(commit it to share it)
                    </p>
            </div>
        </div>
        <div class="scenario-editor">
            <section class="panel editor-panel">
                <div class="fields">
                    <label class="field">
                        <span>Id ${idProblem && html`<em class="field-error">
                            ${idProblem}</em>`}</span>
                        <input class="input mono" value=${id} disabled=${!isNew}
                            placeholder="motor-sim-pair"
                            onInput=${(event) => setId(event.target.value
                                .trim())} />
                    </label>
                    <div class="field"><span>${' '}</span>
                        <div class="button-row">
                            <button class="button primary" onClick=${save}
                                disabled=${isSaving || !!idProblem || !draft}>
                                <${Icon} name="check" size=${14} />
                                ${isSaving ? ' Saving…' : ' Save'}</button>
                            ${!isNew && html`<button class="button danger"
                                onClick=${remove}><${Icon} name="trash-2"
                                size=${14} />${' Delete'}</button>`}
                        </div>
                    </div>
                </div>
                <div class="check-line ${check ? (check.ok ? 'good' : 'bad')
                    : ''}">${!check ? 'Checking…' : check.ok
                        ? html`<${Icon} name="circle-check" size=${14} />
                            ${' Valid: the preview shows the launch it makes.'}`
                        : html`<${Icon} name="triangle-alert" size=${14} />
                            ${` ${check.error}`}`}</div>
                ${error && html`<div class="error-banner">${error}</div>`}
                <textarea class="input code-editor scenario-text"
                    spellcheck="false"
                    value=${text} onKeyDown=${onKeyDown}
                    onInput=${(event) => setText(event.target.value)} />
                <${FormatHelp} />
            </section>
            <div class="editor-preview">
                ${draft ? html`<${ScenarioForm} key=${JSON.stringify(
                    [draft.roles, draft.options])} scenario=${draft}
                    machines=${state.machines} source=${text}
                    onLaunched=${() => {}} />`
                    : html`<div class="placeholder">The preview appears when
                        the scenario is valid.</div>`}
            </div>
        </div>`;
}
