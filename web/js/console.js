// Line viewer with filters (used for live process output and for log files).

import { useLayoutEffect, useMemo, useRef, useState } from 'preact/hooks';
import { html, stripAnsi, AnsiText } from './common.js';
import { useProcessOutput } from './state.js';


const MAX_RENDERED_LINES = 2000;

// `ros2 launch` prefixes node output with "[name-N]" and writes its own
// messages as "[INFO] [name-N]: ...".
const NODE_PREFIX = /^\[([A-Za-z_][\w./]*)-\d+\]/;
const LAUNCH_MESSAGE = /^\[(?:INFO|WARNING|ERROR)\] \[([A-Za-z_][\w./]*)-\d+\]/;

function describeLine(line) {
    const plain = stripAnsi(line);
    const nodeMatch = NODE_PREFIX.exec(plain) || LAUNCH_MESSAGE.exec(plain);
    let level = 'info';
    // ROS log levels, plus compiler and Python messages.
    if (/\[(ERROR|FATAL)\]|\berror:|Traceback/.test(plain)) level = 'error';
    else if (/\[WARN(ING)?\]|\bwarning:/.test(plain)) level = 'warn';
    return { plain, node: nodeMatch ? nodeMatch[1] : null, level };
}


// Live output of one supervised process.
export function Console({ machineId, processId, height }) {
    const buffer = useProcessOutput(machineId, processId);
    return html`<${LineView} lines=${buffer.lines} first=${buffer.first}
        height=${height} emptyText="No output yet." />`;
}

// Any list of lines (numbered from `first`), with text, level and node
// filters and follow-the-end scrolling.
export function LineView({ lines, first = 0, height, emptyText = 'Empty.',
    tools = null }) {
    const [filter, setFilter] = useState('');
    const [level, setLevel] = useState('all');
    const [node, setNode] = useState('');
    const [isFollowing, setIsFollowing] = useState(true);
    const scroller = useRef(null);

    const described = useMemo(() => lines.map(
        (line, index) => ({ line, number: first + index,
            ...describeLine(line || '') })), [lines, first]);
    const nodes = useMemo(() => [...new Set(described
        .map((entry) => entry.node).filter(Boolean))].sort(), [described]);

    const needle = filter.toLowerCase();
    const shown = described.filter((entry) =>
        (level === 'all' || (level === 'problems' && entry.level !== 'info'))
        && (!node || entry.node === node)
        && (!needle || entry.plain.toLowerCase().includes(needle)));
    const hidden = Math.max(0, shown.length - MAX_RENDERED_LINES);
    const visible = shown.slice(hidden);

    useLayoutEffect(() => {
        if (isFollowing && scroller.current) {
            scroller.current.scrollTop = scroller.current.scrollHeight;
        }
    });

    function onScroll() {
        const element = scroller.current;
        const atBottom = element.scrollHeight - element.scrollTop
            - element.clientHeight < 30;
        if (atBottom !== isFollowing) setIsFollowing(atBottom);
    }

    return html`
        <div class="console">
            <div class="console-tools">
                <input class="input" placeholder="Filter output"
                    value=${filter}
                    onInput=${(event) => setFilter(event.target.value)} />
                <select class="input" value=${level}
                    onChange=${(event) => setLevel(event.target.value)}>
                    <option value="all">All levels</option>
                    <option value="problems">Warnings and errors</option>
                </select>
                <select class="input" value=${node}
                    onChange=${(event) => setNode(event.target.value)}>
                    <option value="">All nodes</option>
                    ${nodes.map((name) => html`<option value=${name}>${name}
                        </option>`)}
                </select>
                <span class="spacer"></span>
                ${tools}
                <span class="faint">${shown.length} lines</span>
                ${!isFollowing && html`<button class="button small"
                    onClick=${() => setIsFollowing(true)}>
                    Jump to end</button>`}
            </div>
            <div class="console-output" ref=${scroller} onScroll=${onScroll}
                style=${height ? `height: ${height}` : ''}>
                ${hidden > 0 && html`<div class="console-note">
                    ${hidden} earlier lines not shown; filter to narrow
                    down</div>`}
                ${visible.length === 0 && html`<div class="console-note">
                    ${lines.length ? 'No lines match.' : emptyText}</div>`}
                ${visible.map((entry) => html`
                    <div class="console-line level-${entry.level}"
                        key=${entry.number}>
                        <${AnsiText} text=${entry.line || ''} /></div>`)}
            </div>
        </div>`;
}
