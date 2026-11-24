// Live hub state over one Server-Sent Events stream: a "snapshot" when the
// stream opens, then "machine", "run", "run.removed" and "output" events.
//
// Process output is kept outside Preact state, in `outputStore`, so a busy
// process doesn't re-render the whole page: only consoles showing that
// process subscribe to it.

import { useEffect, useState } from 'preact/hooks';
import { getJson } from './common.js';


// --- Process output store ----------------------------------------------------

const MAX_LINES = 5000;

class OutputStore {
    constructor() {
        this.buffers = new Map();   // "machine/process" -> {first, lines}
        this.listeners = new Map(); // same key -> Set of callbacks
    }

    key(machineId, processId) {
        return `${machineId}/${processId}`;
    }

    buffer(machineId, processId) {
        return this.buffers.get(this.key(machineId, processId))
            || { first: 0, lines: [] };
    }

    // Merge a numbered block of lines (live events and backlog fetches can
    // overlap or arrive out of order).
    merge(machineId, processId, first, lines) {
        const key = this.key(machineId, processId);
        const current = this.buffers.get(key);
        let merged;
        if (!current || current.lines.length === 0
                || first > current.first + current.lines.length) {
            merged = { first, lines: [...lines] };
        } else {
            const start = Math.min(first, current.first);
            const end = Math.max(first + lines.length,
                current.first + current.lines.length);
            const combined = new Array(end - start);
            current.lines.forEach((line, index) => {
                combined[current.first - start + index] = line;
            });
            lines.forEach((line, index) => {
                combined[first - start + index] = line;
            });
            merged = { first: start, lines: combined };
        }
        if (merged.lines.length > MAX_LINES) {
            const overflow = merged.lines.length - MAX_LINES;
            merged = { first: merged.first + overflow,
                lines: merged.lines.slice(overflow) };
        }
        this.buffers.set(key, merged);
        for (const listener of this.listeners.get(key) || []) listener();
    }

    async fetchBacklog(machineId, processId) {
        const path = `/api/machines/${encodeURIComponent(machineId)}`
            + `/processes/${encodeURIComponent(processId)}/output?from=0`;
        const backlog = await getJson(path);
        this.merge(machineId, processId, backlog.first_line_number,
            backlog.lines);
    }

    subscribe(machineId, processId, listener) {
        const key = this.key(machineId, processId);
        if (!this.listeners.has(key)) this.listeners.set(key, new Set());
        this.listeners.get(key).add(listener);
        return () => this.listeners.get(key).delete(listener);
    }
}

export const outputStore = new OutputStore();

// Re-render when a process's output changes (at most once per frame).
export function useProcessOutput(machineId, processId) {
    const [, setVersion] = useState(0);
    useEffect(() => {
        let frame = null;
        const unsubscribe = outputStore.subscribe(machineId, processId, () => {
            if (frame === null) {
                frame = requestAnimationFrame(() => {
                    frame = null;
                    setVersion((version) => version + 1);
                });
            }
        });
        outputStore.fetchBacklog(machineId, processId).catch(() => {});
        return () => {
            unsubscribe();
            if (frame !== null) cancelAnimationFrame(frame);
        };
    }, [machineId, processId]);
    return outputStore.buffer(machineId, processId);
}


// --- Hub state ---------------------------------------------------------------

function replaceMachine(previous, machine) {
    return previous && {
        ...previous,
        machines: previous.machines.map((entry) =>
            entry.machine.id === machine.machine.id ? machine : entry),
    };
}

function replaceRun(previous, run) {
    if (!previous) return previous;
    const runs = previous.runs.filter((entry) => entry.run_id !== run.run_id);
    return { ...previous, runs: [...runs, run] };
}

export function useHubState() {
    const [state, setState] = useState(null);
    const [isLive, setIsLive] = useState(false);

    useEffect(() => {
        const events = new EventSource('/api/events');
        const on = (name, handler) => events.addEventListener(name,
            (message) => handler(JSON.parse(message.data)));
        events.addEventListener('open', () => setIsLive(true));
        events.addEventListener('error', () => setIsLive(false));
        on('snapshot', (snapshot) => {
            setState(snapshot);
            setIsLive(true);
        });
        on('machine', (machine) =>
            setState((previous) => replaceMachine(previous, machine)));
        on('run', (run) => setState((previous) => replaceRun(previous, run)));
        on('run.removed', ({ run_id }) => setState((previous) => previous && {
            ...previous,
            runs: previous.runs.filter((run) => run.run_id !== run_id),
        }));
        on('output', (output) => outputStore.merge(output.machine_id,
            output.process_id, output.first_line_number, output.lines));
        return () => events.close();
    }, []);

    return { state, isLive };
}

// Current time in seconds, ticking once per second (for "ago" labels).
export function useNow() {
    const [now, setNow] = useState(Date.now() / 1000);
    useEffect(() => {
        const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
        return () => clearInterval(timer);
    }, []);
    return now;
}
