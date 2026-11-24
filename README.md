# CSM Hub

One place to run the robot: which machines are up and what code they have,
launching across several machines at once with live output, deploying and
building code, pulling bags, reading logs, and browsing the launch config. A web app served by a small Python program on your laptop; it
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
| Machines | Every machine: connection, load, network (incl. Wi-Fi channel), whether it runs the same code as yours. A terminal on any machine; the VM on a mac host: start, shut down, restart, force stop, commit the overlay, back up, restore, and its serial console. **Edit machines** changes the inventory files in the app. |
| Launch | Pick a scenario, assign machines to its roles, check the exact commands, launch. |
| Runs | Everything launched, deployed or pulled, with live output per process (filter by text, level or node), stop all or one. |
| Deploy | Push this workspace's `src/` to other machines and build there; compare first to see exactly what changes. |
| Bags | Recordings on each machine with length, size and topics; pull them here, delete them there once pulled. |
| Logs | Hub run logs, each package's latest build log, ROS logs, the agent's and the VM autostart log, per machine. |
| Network | The comm-link bandwidth budget (average against the guidebook's 4,000 kbps, per source and per topic), competition mode, live traffic on every interface, Wi-Fi channels, and ping / port check / network scan from any machine. |
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

## Deploying

The Deploy page replaces `sync.sh push --ws` (and the build you'd run after
it over SSH):

- **Same code, checked by content.** Each machine reports a *fingerprint* of
  its workspace: the commit, uncommitted changes and the contents of
  untracked files, in the repo and every submodule. Machines with the same
  fingerprint run the same code whatever their git history. The Machines page
  shows *same code as hub* / *different code*, and the launch page warns when
  the robot and the driver station differ (net_adapter's codec needs both
  sides deployed together).
- **Compare** shows what a push would change (an rsync dry run): files
  changed, new and deleted, and any work on the target a push would
  overwrite (uncommitted changes there).
- **Push** copies `src/` with sync.sh's rules (all of `src/` including `.git`,
  never csm-sim; files deleted here are deleted there) and two differences:
  nothing inside `.git` is ever deleted on the target, so commits, branches
  and stashes that only exist there survive (sync.sh deletes them), and
  Python caches aren't copied.
- **Build** runs `./src/build.sh` on the target after its push succeeds, with
  the ROS distro the workspace was built against, for both robots or one
  (half the time). Pushes to several machines run in parallel.

Bags are pulled into this workspace's `bag_recordings/` (like
`sync.sh pull --bags`), resumably. Deleting a bag on the robot is only
offered once an identical copy is here.

## Terminals and the VM

- **Terminal** on any machine card opens a shell there, in a dock at the
  bottom of every page (several at once, in tabs). It replaces `ssh` for
  anything the pages don't cover.
- **Serial console** on a running VM connects to its serial port, so you can
  log in and fix networking while the VM's own network is down (what
  `run-vm.sh --term` was for, without stopping the VM). Every key goes to the
  VM, Ctrl-C included; close the tab to leave.
- **VM actions** run csm-vm's `run-vm.sh` on the mac and show its output on
  the Runs page. Starting asks what to do with an overlay a previous session
  left (reuse, commit, discard). Needs the updated run-vm.sh and passwordless
  sudo for QEMU and socat on the mac ([docs/machine-setup.md](
  docs/machine-setup.md)).

## The robot's link

- **Bandwidth budget.** The guidebook allows an average of 4,000 kbps on the
  comm link over a run, minus 500 kbps per NASA camera switched on (14.1.18-19).
  The Network page averages two measurements from when you press *Start a new
  average* (do it as a run starts):
  - **Radio link**: the byte counters of the robot's link interface (pick the
    robot computer's Wi-Fi adapter). Everything on the link: net_adapter,
    TCP/IP and zenoh overhead, the hub, anything else.
  - **net_adapter**: what the endpoints report on `/lance/net_bandwidth/*`,
    with a per-topic breakdown and the link latency. Needs the *monitor*: a
    small rclpy script the hub runs on a machine with a net_adapter endpoint
    (normally the driver station), with the same DDS settings as
    `run.sh --local`.
- **Competition mode** keeps the hub's own traffic small: status every 10 s,
  no live output from other machines (each console has *Fetch output now*),
  slower background checks. Measured on the test rig: about 0.5 kbps to the
  robot VM, against 5.5 kbps normally. The top bar shows it, with the
  running average.
- **Tools** run from any machine (what it can reach is what the robot code on
  it can reach): ping, TCP port check (SSH, zenoh 7447, lidar 2111,
  Foxglove 8765) and a scan of a network (up to a /22) that lists who answers,
  with MAC addresses and which inventory machine each address belongs to.

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
| 2 | Deploy (compare, push, remote build, code fingerprints), bags, logs | done |
| 3 | VM control, terminals and serial console; editing the inventory in the app | done |
| 4 | Network tools, bandwidth budget, competition mode; resizable, dockable terminals | done |
| 5 | Configurability: edit scenarios, launch-config presets (with descriptions) and hub settings in the app | next |
| 6 | Native macOS processes | |

Bugs and inconsistencies found along the way, in this and other repos, are
logged in [FINDINGS.md](FINDINGS.md). Test rig setup: [docs/dev-rig-setup.md](
docs/dev-rig-setup.md).
