# CSM Hub

One place to run the robot: which machines are up, what code they have, the
VM on the robot's mac, and (coming) synchronized launches, deployment and
logs. A web app served by a small Python program on your laptop; it talks to
each machine through SSH, so nothing on the robot listens on the network.

```bash
./src/csm-hub/csm-hub          # from the workspace root; opens a browser tab
```

Options: `--port 8740`, `--no-browser`, `--bind 127.0.0.1`,
`--workspace <path>` (default: the workspace containing csm-hub).

**Requirements:** Python 3.9+ (no packages to install) and SSH key access to
each machine (`ssh-copy-id user@host`). Each managed machine needs `python3`
3.9+; macOS's built-in one is fine.

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

- `routes` are tried in order. `via` jumps through another machine (here, the
  mac's host-only link to its VM, which still works when the VM's own network
  is down). `host_key_alias` checks the host key under another name, so a
  second address of a machine doesn't trigger "unknown host".
- **Prefer IP addresses over `.local` names** for machines with both Wi-Fi and
  Ethernet: mDNS may resolve to the Wi-Fi address (see FINDINGS.md, M6).
- `workspace` is a colcon workspace whose repo is `<workspace>/src`; the hub
  shows its commit and whether it matches yours.
- `vm` marks a mac that runs a csm-vm VM.

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
- **Web app** (`web/`): Preact + htm loaded as plain ES modules from
  `web/vendor/` (no build step, no internet needed). Edit `web/app.js` and
  reload.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Agent, inventory, machines view (status, network, workspace, VM) | done |
| 1 | Scenarios and synchronized launch with output streaming; config browser; IP overlay | next |
| 2 | Deploy (push, remote build, version checks), bags, logs | |
| 3 | VM control and serial console | |
| 4 | Network tools, bandwidth budget, competition mode | |
| 5 | Native macOS processes | |

Bugs and inconsistencies found along the way, in this and other repos, are
logged in [FINDINGS.md](FINDINGS.md). Test rig setup: [docs/dev-rig-setup.md](
docs/dev-rig-setup.md).
