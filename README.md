# CSM Hub

One place to run the robot: which machines are up and what code they have,
launching across several machines at once with live output, and browsing the
launch config. A web app served by a small Python program on your laptop; it
talks to each machine through SSH, so nothing on the robot listens on the
network.

```bash
./src/csm-hub/csm-hub          # from the workspace root; opens a browser tab
```

Options: `--port 8740`, `--no-browser`, `--bind 127.0.0.1`,
`--workspace <path>` (default: the workspace containing csm-hub).

**Requirements:** Python 3.9+ (no packages to install) and SSH key access to
each machine (`ssh-copy-id user@host`). Each managed machine needs `python3`
3.9+; macOS's built-in one is fine. One-time setup per machine (host keys,
CAN permissions): [docs/machine-setup.md](docs/machine-setup.md).

| Page | What it does |
|---|---|
| Machines | Every machine: connection, load, network (incl. Wi-Fi channel), workspace commit vs. yours, the VM on a mac host. |
| Launch | Pick a scenario, assign machines to its roles, check the exact commands, launch. |
| Runs | Everything launched, with live output per process (filter by text, level or node), stop all or one. |
| Config | Browse every action and preset of `lance/config`, what a preset inherits and enables, and try `key:=value` arguments without launching. |

## Machines

The hub reads two inventory files and merges them (the second overrides
entries of the first by `id`):

| File | For |
|---|---|
| `<workspace>/src/project/hub/machines.json` | The team's machines: robot, driver stations. Versioned with the code. |
| `~/.config/csm-hub/machines.json` | Your own machines, e.g. a test rig. |

Your own machine is always included as `local`. Example entry pair for a mac
host and its VM:

```json
{
    "machines": [
        {
            "id": "robot-mac",
            "label": "Robot mac host",
            "routes": [{"host": "10.11.11.34", "user": "csm"}],
            "vm": {"csm_vm_path": "~/code/csm-vm", "guest": "robot-vm"}
        },
        {
            "id": "robot-vm",
            "label": "Robot VM",
            "host": "robot-mac",
            "workspace": "~/lance-ws",
            "routes": [
                {"host": "10.11.11.48", "user": "csm"},
                {"host": "192.168.18.2", "user": "csm", "via": "robot-mac",
                 "host_key_alias": "10.11.11.48"}
            ]
        }
    ]
}
```

- `routes` are tried together, each a moment after the previous one, and the
  first that answers wins (so earlier routes are preferred, and a machine on
  two networks connects through whichever works). `via` jumps through another machine (here, the
  mac's host-only link to its VM, which still works when the VM's own network
  is down). `host_key_alias` checks the host key under another name, so a
  second address of a machine doesn't trigger "unknown host".
- **Prefer IP addresses over `.local` names** for machines with both Wi-Fi and
  Ethernet: mDNS may resolve to the Wi-Fi address (see FINDINGS.md, M6).
- `workspace` is a colcon workspace whose repo is `<workspace>/src`; the hub
  shows its commit and whether it matches yours.
- `vm` marks a mac that runs a csm-vm VM.

## Scenarios

A scenario is a named setup that starts processes on one or more machines: one
JSON file per scenario in `<workspace>/src/project/hub/scenarios/`. The format
is documented at the top of [hub/scenarios.py](hub/scenarios.py); the
competition one, shortened:

```json
{
    "label": "Competition run",
    "description": "Robot code on the robot computer and the driver station...",
    "roles": {
        "robot": {"label": "Robot computer", "default": "robot-vm"},
        "client": {"label": "Driver station", "default": "local"}
    },
    "options": {
        "arena": {"label": "Arena", "default": "ksc",
                  "choices": {"ksc": "KSC", "ucf_left": "UCF left"}}
    },
    "processes": [
        {"key": "robot", "role": "robot",
         "lance": {"flags": ["--canbus", "--local"],
                   "args": {"robot": "lance2_{arena}"},
                   "set": {"redux": {"client_hostname": "{ip:client}"}}}},
        {"key": "client", "role": "client", "after": ["robot"],
         "lance": {"flags": ["--local"],
                   "args": {"client": "lance2_{arena}"},
                   "set": {"redux": {"robot_hostname": "{ip:robot}"}}}}
    ]
}
```

- `lance` processes run `./src/run.sh <flags> <key:=value...>` in the
  machine's workspace; `command` processes run a shell command.
- `{ip:client}` is the client machine's address as seen from the machine the
  process runs on: the hub picks one on a network both share (default-route
  interface first, Ethernet before Wi-Fi) and the launch page lets you pick
  another.
- **`set` changes single values** of whatever presets the arguments select,
  which the command line can't do. The hub writes a small overlay next to
  `launch.json` in the machine's `install/` tree (never `src/`), deriving a
  `hub_<key>` preset from the one that would have been chosen, and adds
  `json_path:=<overlay> redux:=hub_<key>` to the command. The launch page
  shows the overlay and the resulting presets before anything starts. No
  presets need to be added for a new pairing of machines.
- Processes start in `after` order; if one fails to start, the ones already
  started are stopped again. Stopping sends SIGINT to the whole process group
  (like Ctrl-C), then SIGTERM after 20 s and SIGKILL after 5 more, robot side
  last.
- Runs survive the hub: processes belong to the machine's agent, and a
  restarted hub finds them again with their output. Each process's output is
  also saved on its machine under `~/.csm-hub/runs/<run>/` (20 MB cap per
  process, removed after 14 days).

## How it works

```
 your laptop                           robot mac mini
 ┌──────────────────────┐   ssh   ┌──────────────────────────────┐
 │ hub (python) ── web  │────────▶│ agent (macOS): VM, stats     │
 │ agent (local)        │   ssh   │  ┌────────────────────────┐  │
 └──────────────────────┘─────────┼─▶│ agent (VM): workspace  │  │
                                  │  └────────────────────────┘  │
                                  └──────────────────────────────┘
```

- **Agent** (`agent/csm_agent.py`): one standard-library file. The hub copies
  it to `~/.csm-hub/agent.py` on each machine (and replaces it when it
  changes), then runs `agent.py attach` over SSH. `attach` connects to a
  long-running daemon on a unix socket, starting it if needed, so processes
  the hub starts can outlive the hub's connection. Logs:
  `~/.csm-hub/agent.log`.
- **Hub** (`hub/`): one link per machine (tries routes, installs or updates
  the agent, polls status, reconnects), plus a small HTTP server that streams
  state to the browser with Server-Sent Events.
- **Config** (`hub/lance_config.py`): reads `lance/config` with
  launch-utils' own preprocessing code from your workspace, so previews match
  what `run.sh` does.
- **Web app** (`web/`): Preact + htm loaded as plain ES modules from
  `web/vendor/` (no build step, no internet needed). One module per page in
  `web/js/`; edit and reload.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Agent, inventory, machines view (status, network, workspace, VM) | done |
| 1 | Scenarios and synchronized launch with output streaming; config browser; value overrides (IP pairing); host key trust | done |
| 2 | Deploy (push, remote build, version checks), bags, logs | next |
| 3 | VM control and serial console | |
| 4 | Network tools, bandwidth budget, competition mode | |
| 5 | Native macOS processes | |

Bugs and inconsistencies found along the way, in this and other repos, are
logged in [FINDINGS.md](FINDINGS.md). Test rig setup: [docs/dev-rig-setup.md](
docs/dev-rig-setup.md).
