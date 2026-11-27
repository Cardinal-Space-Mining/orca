# Orca

Orca is the robot orchestrator for [Cardinal Space Mining](
https://github.com/Cardinal-Space-Mining)'s Lunabotics robots (LANCE-1 and
LANCE-2): one place to run the robot from a laptop. It shows which machines are
up and what code they have, launches across several machines at once with live
output, deploys and builds code, pulls bags, reads logs, and browses and edits
the launch config.

It's a web app served by a small Python program on your laptop. It talks to
each machine over SSH, so nothing on the robot listens on the network.

## Where it fits

Orca is a submodule of the robot workspace,
[lance-2026](https://github.com/Cardinal-Space-Mining/lance-2026), checked out
at `<ws>/src/orca` (where `<ws>` is the colcon workspace, normally
`~/lance-ws`, and `<ws>/src` is the lance-2026 repo). It has a `COLCON_IGNORE`,
so colcon doesn't build it. Orca's code is generic; what it knows about this
robot comes from the workspace around it:

| From the workspace | What Orca does with it |
|---|---|
| `src/project/orca/` | The team's machine inventory (`machines.json`), launch scenarios (`scenarios/`) and preset descriptions (`presets.json`). Versioned with the robot code. |
| `src/run.sh`, `src/build.sh` | Launches run `./src/run.sh <flags> <key:=value...>` on a machine; Deploy runs `./src/build.sh` there. |
| `src/lance/config/` and [launch-utils](https://github.com/Cardinal-Space-Mining/launch-utils) | The launch config: JSON *actions* (one component each) and *presets* (named settings of an action; *setups* like `robot:=lance2_ksc` select presets of other actions), resolved by launch-utils. Orca loads launch-utils from the workspace to preview and edit it. |
| `src/sync.sh` | The team's older rsync deploy script; Orca's Deploy page follows its rules. |
| `net_adapter` (in lance-2026) | The robot ↔ driver-station link over zenoh. Orca measures its bandwidth against the competition's budget. |
| [csm-vm](https://github.com/Cardinal-Space-Mining/csm-vm) | Runs the robot's Ubuntu VM on its mac mini; Orca starts, stops and backs up the VM through its `run-vm.sh`. |

Without a workspace (`--workspace` pointing nowhere, or Orca checked out on
its own), the machine pages, terminals and network tools work; launching, the
Config page and the team inventory need one.

## Getting started

**On the laptop that runs Orca:** Python 3.10+ (no packages to install;
launch-utils needs 3.10) and the robot workspace:

```bash
mkdir lance-ws && cd lance-ws
git clone --recurse-submodules https://github.com/Cardinal-Space-Mining/lance-2026 src
./src/orca/orca      # from the workspace root; opens a browser tab
```

Already have the workspace? `git -C src submodule update --init orca`. The
workspace doesn't need to be built for Orca itself, only for what you launch
on this laptop. Options: `--port 8740`, `--no-browser`,
`--bind 127.0.0.1`, `--workspace <path>` (default: the workspace Orca is
in). The port and whether a browser opens can also be set on the Settings
page.

**On each machine it manages:** an SSH server, `python3` 3.9+ (macOS's
built-in one is fine) and your laptop's SSH key. Orca installs its own agent
over SSH; nothing else is installed by hand. Robot computers and VM hosts
need some one-time setup (CAN and QEMU permissions, lingering):
[docs/machine-setup.md](docs/machine-setup.md).

**First run:** the Network page lists the team's machines from
`src/project/orca/machines.json`. For each one you'll reach, trust its host
key once (the card shows the fingerprint to compare) and make sure your SSH
key is installed (`ssh-copy-id user@host`, or **Add a machine**, which does
both). Machines of your own go in `~/.config/orca/machines.json` (see
[Machines](#machines)). Then pick a scenario on the Launch page.

## Pages

| Page | What it does |
|---|---|
| Network | Every machine and the networks between them, as a **map** (networks as lines, machines hanging from them by their ports, other devices on the lines; click anything for details) or a **list** of machine cards. A card: connection, load, network (incl. Wi-Fi channel), where an offline machine was last seen, whether it runs the same code as yours, its setup checklist, a terminal, **Restart** (a mac host shuts its VM down first and starts it again after: the remote way to recover a USB adapter macOS lost), and on a mac host its VM (start, shut down, restart, force stop, commit the overlay, back up, restore, serial console). Also the comm-link bandwidth budget, internet sharing over Ethernet, live traffic, Wi-Fi channels, and ping / port check / network scan from any machine. **Add a machine** finds a new machine (scan, shared port or address), sets up SSH access and writes its entry; **Edit machines** changes the inventory files in the app. |
| Launch | Pick a scenario, assign machines to its roles, add `action:=preset` arguments (typed or picked from the config's actions and presets), check the exact commands, launch. **Save as scenario…** turns the choices into a new scenario to name and save. |
| Runs | Everything launched, deployed or pulled, with live output per process (filter by text, level or node), stop all or one. **Run again…** opens Launch with a run's machines, options and arguments filled in. |
| Deploy | Push your laptop's workspace (`<ws>/src`) to other machines and build there; compare first to see exactly what changes. |
| Bags | Recordings on each machine with length, size and topics; pull them to your laptop, delete them on the machine once pulled. |
| Logs | Orchestrator run logs, each package's latest build log, ROS logs, the agent's and the VM autostart log, per machine; delete logs, and clean up what piles up on each machine's disk. |
| Config | Browse and edit every action and preset of `lance/config` (with descriptions), see what a preset inherits and enables, and try `key:=value` arguments without launching. |
| Settings (gear) | Competition mode, the bandwidth budget, startup options, what the orchestrator remembers, and where its files are. |

## Machines

**Add a machine** (on the Network page) finds a new machine and writes its
entry for you:

1. **Find it**: scan a network from any connected machine (devices are listed
   with name, MAC and whether SSH is open; known machines are marked), pick a
   device plugged into a shared port (see "Sharing internet over Ethernet";
   it can start sharing right there), or type an address. A VM behind its mac
   (host-only network) is reached through the mac.
2. **Connect**: choose an id and the user on it, compare the host key's
   fingerprints with the machine's own and trust them (it warns when another
   machine has the same key, as cloned VMs do), and install your SSH key if
   the machine doesn't accept it yet (a terminal asks for the password once).
3. **Review**: it connects once and fills in the entry from what it finds: its
   addresses (the reachable ones), the workspace, csm-vm on a mac, and the
   lance config constants that hold its address. Save it to the team file or
   your own; it connects right away. A mac that hosts a VM offers to add the
   VM next.

The orchestrator reads two inventory files and merges them (the second overrides
entries of the first by `id`):

| File | For |
|---|---|
| `<workspace>/src/project/orca/machines.json` | The team's machines: robot, driver stations. Versioned with the code. |
| `~/.config/orca/machines.json` | Your own machines, e.g. a bench setup at home. Only on your laptop. |

Your own machine is always included as `local`. Example entry pair for a mac
host and its VM:

```json
{
    "machines": [
        {
            "id": "robot-mac",
            "label": "Robot mac host",
            "user": "lance",
            "host_key_alias": "robot-mac.orca",
            "routes": [{"host": "10.11.11.34"}],
            "vm": {"csm_vm_path": "~/code/csm-vm", "guest": "robot-vm"}
        },
        {
            "id": "robot-vm",
            "label": "Robot VM",
            "host": "robot-mac",
            "workspace": "~/lance-ws",
            "user": "lance",
            "host_key_alias": "robot-vm.orca",
            "routes": [
                {"host": "10.11.11.48"},
                {"host": "192.168.18.2", "via": "robot-mac"}
            ],
            "config_addresses": {
                "ip.net1.mac_vm_eth": "ethernet",
                "ip.net1.mac_vm_wifi": "wifi"
            }
        }
    ]
}
```

- `routes` are tried together, each a moment after the previous one, and the
  first that answers wins (so earlier routes are preferred, and a machine on
  two networks connects through whichever works). `via` jumps through another
  machine (here, the mac's host-only link to its VM, which still works when
  the VM's own network is down). `user` and `host_key_alias` on the machine
  apply to every route that doesn't set its own.
- `workspace` is a colcon workspace whose repo is `<workspace>/src`; the
  orchestrator shows its commit and whether it matches yours.
- `vm` marks a mac that runs a csm-vm VM.

### Setup checklist

Each machine card has a **Setup** list of the one-time setup that machine
needs, checked without sudo every few minutes: lingering, rsync, the mDNS name
(avahi), Energy Efficient Ethernet on Realtek USB adapters, ROS and the
workspace; on a mac VM host, csm-vm's `run-vm.sh` version, QEMU and socat, and
passwordless sudo for them. Two roles in the inventory (`"setup": [...]`, also
checkboxes in the editor and the add-machine wizard) add items: `"can"` for a
robot computer (passwordless sudo for CAN bring-up) and `"sharing"` for a
machine that shares its internet (the sharing helper).

**Fix** writes a short script to the machine and runs it in a terminal there,
showing each command as it runs; sudo asks for the password in that terminal.
The list re-checks when the terminal closes. The sudoers rules it installs are
the narrow ones from [docs/machine-setup.md](docs/machine-setup.md), checked
with `visudo -c` before they're installed.

### Addresses change; machines are found anyway

Every network the robot meets (the team's, the competition's) hands out
addresses with DHCP, so the addresses in the
inventory are only a first guess. What identifies a machine is its **SSH host
key**, checked under `host_key_alias` on every connection whatever address it
used, so an address that now belongs to something else is never mistaken for
the machine.

- **Remembered addresses.** While a machine is connected, its agent reports
  its Ethernet and Wi-Fi addresses and MACs. They're kept on your laptop in
  `~/.config/orca/addresses.json` (not in the team file, so it doesn't
  change every time DHCP does) and tried after the inventory's routes: first
  wherever the machine's MAC shows up in your laptop's neighbour (ARP) table
  right now (*neighbour* routes), then addresses it had on a network you're on
  (*learned* routes). The machine card shows which one it's connected on, and
  where an offline machine was last seen.
- **Discovery.** When a machine with known MACs can't be reached, the
  orchestrator scans the networks your laptop is on (at most every 2 minutes;
  the /24 around you and around its last address on bigger networks) so its
  MAC shows up at its new address.
- Found routes are only tried once the machine's host key is trusted, so the
  first connection always goes through a route from the inventory.
- **The robot code's addresses.** `lance/config` names machines by address
  through constants in `lance/config/actions/shared.json`
  (`${ ip.net1.mac_vm_eth }`). `config_addresses` maps those constants to a
  machine's interfaces, and every launch from the orchestrator replaces them
  with the machine's current address, as seen from the machine the process runs
  on. The launch page lists each replacement. The values in `shared.json` only
  matter for launches started by hand.
- `.local` names work as routes, but on a machine with Wi-Fi and Ethernet on
  one network, mDNS may answer with the Wi-Fi address only
  ([docs/machine-setup.md](docs/machine-setup.md#macos-vm-hosts-robot-mac)).
  The remembered Ethernet address is tried as well.

## Scenarios

A scenario is a named setup that starts processes on one or more machines: one
JSON file per scenario in `<workspace>/src/project/orca/scenarios/`. The
format is documented at the top of [backend/scenarios.py](backend/scenarios.py);
the competition one, shortened:

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
  machine's workspace (lance-2026's launch script; `--canbus` brings CAN up,
  `--local` keeps ROS traffic on the machine, and `robot:=lance2_ksc` picks
  the LANCE-2 robot preset for the KSC arena); `command` processes run a
  shell command. In the example, `redux` is the action that configures the
  net_adapter endpoints, i.e. which address each side connects to.
- `{ip:client}` is the client machine's address as seen from the machine the
  process runs on: the orchestrator picks one on a network both share
  (default-route interface first, Ethernet before Wi-Fi) and the launch page
  lets you pick another.
- **`set` changes single values** of whatever presets the arguments select,
  which the command line can't do. The orchestrator writes a small overlay next
  to `launch.json` in the machine's `install/` tree (never `src/`), deriving a
  `orca_<key>` preset from the one that would have been chosen, and adds
  `json_path:=<overlay> redux:=orca_<key>` to the command. The launch
  page shows the overlay and the resulting presets before anything starts. No
  presets need to be added for a new pairing of machines.
- Processes start in `after` order; if one fails to start, the ones already
  started are stopped again. Stopping sends SIGINT to the whole process group
  (like Ctrl-C), then SIGTERM after 20 s and SIGKILL after 5 more, robot side
  last.
- Runs survive the orchestrator: processes belong to the machine's agent, and a
  restarted orchestrator finds them again with their output. Each process's
  output is also saved on its machine under `~/.orca/runs/<run>/` (20 MB
  cap per process, removed after 14 days).


**Editing in the app:** Launch → *New scenario*, or *Edit* / *Duplicate* on
a scenario. The editor checks the JSON as you type (with the line of a JSON
mistake) and previews the launch it makes, with the exact commands, beside
it. *Save* writes `src/project/orca/scenarios/<id>.json`; commit it to
share it. *Delete* removes the file.

## The launch config

The Config page browses `lance/config`: every action and preset, what a
preset inherits and enables, and what a set of `key:=value` arguments
resolves to (the same preprocessing `run.sh` does). It also edits it:

- **Edit**, **Derive** (a new preset that inherits from the selected one) and
  **New preset**: the preset's JSON, checked against the rest of the config
  as you type (launch-utils must resolve it; selecting an action that doesn't
  exist is flagged, since launch-utils silently skips it). *Save* writes it
  into the file that defines it, changing only that preset's text, so the
  diff shows just your change. New presets go in the file with the action's
  `pragma:default`.
- **Delete** refuses while something still names the preset: a preset
  derived from it, a setup that selects it, the action's default, or a
  scenario.
- **Descriptions** of actions and presets (the pencil next to them) are kept
  in `src/project/orca/presets.json`, not in the presets: launch-utils
  passes every key of a preset to its node as a ROS parameter (FINDINGS.md
  L3). They show in the preset list's tooltips and are searched.

Edits change this laptop's workspace: Deploy sends them to the other
machines, and committing `src/lance` shares them.

## Settings

The gear at the end of the tabs: competition mode, the bandwidth budget's
numbers and sources, the port and whether a browser opens at start (used when
the command line doesn't say), what the orchestrator has remembered (sharing
choices and learned addresses, each can be forgotten) and where all its files
are. Settings are in `~/.config/orca/settings.json`.

**Light and dark.** The sun / moon button in the top bar switches between a
dark theme and a high-contrast light one for reading outside on a bright day;
Settings → Appearance can also make it follow the system. The choice is kept
by the browser (each laptop picks its own), not in the settings file.

## Cleaning up

Robot disks are small (the VM's is 64 GB), so the Logs page has a **Clean up**
panel per machine: how much each kind of leftover takes, and a button to
delete it.

| Kind | What | Kept |
|---|---|---|
| Launch overlays | `orca-*.json` in `install/lance/share/lance/config`, written when a launch changes config | Those of running launches. Each is deleted when its launch ends, so these are from crashes or older versions. |
| Orca run logs | `~/.orca/runs/<run>/` | Runs still going. Deleted after 14 days anyway. |
| ROS logs | `~/.ros/log` | Files written in the last 10 minutes. |
| Old build logs | colcon's `log/build_*` | The newest 4 builds (the Logs page reads them). |
| Old agent folder | `~/.orchestrator`, from before the rename | All of it if an old agent is still running. |

Selecting a log shows it with a **Delete** button (run, build and ROS logs;
not the agent's own log or the VM's, which are open). The agent checks again
before deleting, so nothing a running process uses goes.

## Deploying

The Deploy page replaces lance-2026's `./src/sync.sh push --ws` (and the
build you'd run after it over SSH):

- **Same code, checked by content.** Each machine reports a *fingerprint* of
  its workspace: the commit, uncommitted changes and the contents of
  untracked files, in the repo and every submodule. Machines with the same
  fingerprint run the same code whatever their git history. The Network page
  shows *same code as orchestrator* / *different code*, and the launch page
  warns when the robot and the driver station differ (net_adapter's codec needs
  both sides deployed together).
- **Compare** shows what a push would change (an rsync dry run): files
  changed, new and deleted, and any work on the target a push would
  overwrite (uncommitted changes there).
- **Push** copies `src/` with sync.sh's rules (all of `src/` including `.git`,
  never csm-sim; files deleted on your laptop are deleted there) and two differences:
  nothing inside `.git` is ever deleted on the target, so commits, branches
  and stashes that only exist there survive (sync.sh deletes them), and
  Python caches aren't copied.
- **Build** runs `./src/build.sh` on the target after its push succeeds, with
  the ROS distro the workspace was built against, for both robots or one
  (half the time). Pushes to several machines run in parallel.

Bags are pulled into your workspace's `bag_recordings/` (like
`sync.sh pull --bags`), resumably. Deleting a bag on the robot is only
offered once an identical copy is on your laptop.

## Terminals and the VM

- **Terminal** on any machine card opens a shell there, in a dock at the
  bottom of every page (several at once, in tabs). It replaces `ssh` for
  anything the pages don't cover. It starts in the machine's workspace, or
  on a VM host in its csm-vm checkout.
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

- **Bandwidth budget.** The Lunabotics guidebook allows an average of
  4,000 kbps on the comm link over a run, minus 500 kbps per NASA camera
  switched on (rules 14.1.18-19).
  The Network page averages two measurements from when you press *Start a new
  average* (do it as a run starts):
  - **Radio link**: the byte counters of the robot's link interface (pick the
    robot computer's Wi-Fi adapter). Everything on the link: net_adapter,
    TCP/IP and zenoh overhead, the orchestrator, anything else.
  - **net_adapter**: what the endpoints report on `/lance/net_bandwidth/*`,
    with a per-topic breakdown and the link latency. Needs the *monitor*: a
    small rclpy script the orchestrator runs on a machine with a net_adapter
    endpoint (normally the driver station), with the same DDS settings as
    `run.sh --local`.
- **Competition mode** keeps the orchestrator's own traffic small: status every
  10 s, no live output from other machines (each console has *Fetch output
  now*), slower background checks. Measured with a robot VM on its mac:
  about 0.5 kbps, against 5.5 kbps normally. The top bar shows it, with the
  running average.
- **Tools** run from any machine (what it can reach is what the robot code on
  it can reach): ping, TCP port check (SSH, zenoh 7447, lidar 2111,
  Foxglove 8765) and a scan of a network (up to a /22) that lists who answers,
  with MAC addresses and which inventory machine each address belongs to.

## The network map

**Network → Map** draws every IP network the machines are on as a line, and
each machine as a box hanging from the lines by its ports:

- **Lines** are the local network (with its gateway, Wi-Fi name and the
  internet), a share, a mac's host-only link to its VM, or any other network.
  A line means "on the same IP network", not "cabled together": switches and
  access points don't show up.
- **Ports** sit on top of each box: Ethernet, USB Ethernet, Wi-Fi, bridges.
  Traffic on a port shows as dashes moving along its line: faster the busier
  it is, down for mostly incoming and up for mostly outgoing, glowing from
  500 kbps. Red is the port measured as the robot's link, green a port
  sharing internet, dashed an offline machine. Ports on no network are just
  an icon; inactive ones are counted in the box.
- **Other devices** come from the machines' neighbour tables (every 30 s),
  DHCP leases of a share and scans; the lance config's address constants name
  the ones it knows (10.11.10.3 shows up as "lidar"). Busy networks show the
  router and named devices, and a count for the rest.
- The map fits the window: columns widen, then the whole map scales to the
  width it has.
- **Click anything** for its panel (click it again to close it): a machine's full card (plus ping, port
  check and scan from it), a network's machines and devices (scan it from any
  machine on it; *Stop sharing*), a port's addresses, traffic and Wi-Fi channel
  (*Measure as the robot's link*, *Share internet here*), a device (*Add as a
  machine*). What's selected is in the URL, so a reload keeps it. The panel
  opens below the map, or beside it on very wide screens; the button in its
  header switches, and the choice is remembered.
- Offline machines are listed below the map (a VM or host whose partner is
  online stays on it), and the bandwidth budget sits above it while it's being
  measured. **List** shows the machine cards and the full budget, sharing,
  traffic, Wi-Fi and tools panels instead.

The map's icon for a machine is a guess (mac, VM, robot computer, laptop,
desktop, single-board computer); an inventory entry can set `"icon"` to fix
it.

## Sharing internet over Ethernet

In the pits the robot is plugged straight into the driver station, and the
test bench often has no router either. Click an Ethernet port on the Network
map → **Share internet here** (or **Internet sharing** in the List view) to
share a Linux machine's internet with whatever is plugged into that port (a USB adapter is fine): devices get addresses by DHCP (default subnet
10.11.11.0/24, or the next free 10.11.x.0/24 if that one is in use) and the
machine routes their traffic out through whatever connection it has.

- **Plug in, then find.** Devices that show up on the port are listed with
  their address, MAC and hostname. Known machines are matched by MAC and
  connect on their own within a few seconds (their inventory addresses don't
  need to match); anything else has **Add machine**.
- **Setup, once per machine:** *Install helper* opens a terminal on that
  machine that asks for your sudo password once. It installs a small
  root-owned program, `/usr/local/sbin/orca-share` (from
  [agent/share_helper.py](agent/share_helper.py)), plus a sudoers rule that
  lets you run just that program without a password, and `dnsmasq-base` if
  it's missing. The program only accepts checked arguments and reads no file a
  user can write.
- **Nothing permanent:** the port's address, NetworkManager leaving the port
  alone, IP forwarding, tagged firewall rules and a DHCP-only dnsmasq all go
  away on *Stop sharing* or a reboot. Leases are kept
  (`/var/lib/orca-share/`), so a device usually gets the same address
  back.
- It refuses to share over Wi-Fi (devices can't join a Wi-Fi adapter that
  isn't an access point) or over the port that is the machine's own internet.
  Energy Efficient Ethernet is switched off on the shared port while sharing:
  RTL8153 USB adapters drop packets with it on.
- Replaces [netshare](https://github.com/S1ink/netshare); see FINDINGS.md
  (N1-N4) for why it isn't used directly.

## How it works

```
 your laptop                           robot mac mini
 ┌──────────────────────┐   ssh   ┌──────────────────────────────┐
 │ orchestrator ── web  │────────▶│ agent (macOS): VM, stats     │
 │ agent (local)        │   ssh   │  ┌────────────────────────┐  │
 └──────────────────────┘─────────┼─▶│ agent (VM): workspace  │  │
                                  │  └────────────────────────┘  │
                                  └──────────────────────────────┘
```

- **Agent** (`agent/orca_agent.py`): one standard-library file. The
  orchestrator copies it to `~/.orca/agent.py` on each machine (and
  replaces it when it changes), then runs `agent.py attach` over SSH. `attach`
  connects to a long-running daemon on a unix socket, starting it if needed, so
  processes the orchestrator starts can outlive the orchestrator's connection.
  Logs: `~/.orca/agent.log`.
- **The orchestrator** (`backend/`): one link per machine (tries routes, installs or
  updates the agent, polls status, reconnects), plus a small HTTP server that
  streams state to the browser with Server-Sent Events. It has no login, so it
  only answers its own page: requests from other web pages open in the same
  browser (another origin, a non-JSON POST, or, when bound to localhost, a
  Host that isn't a loopback name) are refused.
- **Config** (`backend/lance_config.py`): reads `lance/config` with
  launch-utils' own preprocessing code from your workspace, so previews match
  what `run.sh` does. `backend/config_edit.py` edits presets in place.
- **Web app** (`web/`): Preact + htm loaded as plain ES modules from
  `web/vendor/` (no build step, no internet needed). One module per page in
  `web/js/`; edit and reload.

## Developing Orca

- `orca` is the launcher (it runs `backend/__main__.py`), `backend/` the
  server (a Python package),
  `agent/` the agent copied to each machine plus the sharing helper, `web/`
  the page (edit and reload; no build step), `tests/` the tests, `docs/` the
  machine setup guide.
- Standard library only, on both sides. The agent must keep running on
  Python 3.9 (macOS's built-in one); the laptop side needs 3.10.
- To try changes on real machines without the robot, put a bench setup (a
  spare laptop, a mac running a csm-vm VM) in `~/.config/orca/machines.json`
  and prepare it as in [docs/machine-setup.md](docs/machine-setup.md). Keep
  test data small on VMs: their disks are 64 GB and backed up whole.

### Tests

```bash
cd <ws>/src/orca && python3 -m unittest discover -s tests -t .
```

They cover scenario templates, the inventory, launch config overlays and
preset editing (against the real config of the workspace around Orca; those
tests are skipped without one), rsync summaries, the bandwidth budget, the
address book, run start/stop order, the web server's request checks, and a
real agent daemon started in a temporary directory (with a temporary home for
the cleanup tests, so they never touch your real logs).

GitHub Actions (`.github/workflows/tests.yaml`) runs them on every push and
pull request to `main`: everything with the system `python3` of Ubuntu 22.04,
24.04 and 26.04, inside a checkout of lance-2026's `main` so the config
tests run too; the agent's tests on Python 3.9; and a syntax check of every
web module.

### History

| Phase | What | State |
|---|---|---|
| 0 | Agent, inventory, machines view (status, network, workspace, VM) | done |
| 1 | Scenarios and synchronized launch with output streaming; config browser; value overrides (IP pairing); host key trust | done |
| 2 | Deploy (compare, push, remote build, code fingerprints), bags, logs | done |
| 3 | VM control, terminals and serial console; editing the inventory in the app | done |
| 4 | Network tools, bandwidth budget, competition mode; resizable, dockable terminals | done |
| 5 | Addressing: machines found by host key, remembered addresses, neighbour matching and discovery; live addresses in launches; a test suite | done |
| 6 | Internet sharing over Ethernet (replaces [netshare](https://github.com/S1ink/netshare)), with devices on the port matched to machines | done |
| 7 | Add-machine wizard: discovery, host key and SSH key setup, entry from what the machine reports | done |
| 8 | Setup checklist per machine: checked without sudo, fixed by scripts run in a terminal there | done |
| 8b | Network map: the Machines and Network pages merged; networks, ports with live traffic, devices and discovery on one diagram; icons throughout | done |
| 9 | Configurability: scenario editor (new, duplicate, edit, delete; checked as you type, launch previewed); launch-config preset editing with descriptions; Settings page | done |
| 10 | Native macOS processes | |

Bugs and inconsistencies found along the way in the repos Orca works with
(lance-2026, launch-utils, csm-vm) are logged in [FINDINGS.md](FINDINGS.md)
for their owners.

## Coming from the "orchestrator" names

Before it was called Orca, its files had "orchestrator" in their names. On
first start Orca moves `~/.config/orchestrator` to `~/.config/orca`. On each
machine the agent now lives in `~/.orca` (installed by itself on connect);
the Logs page's Clean up panel deletes the old `~/.orchestrator` once nothing
launched before the rename is still running there. Internet sharing needs its
helper installed again (the machine's setup checklist offers it; the
installer removes the old `orchestrator-share`). The team files moved from
`src/project/orchestrator` to `src/project/orca`.

The repository is now `Cardinal-Space-Mining/orca` (GitHub redirects the old
URL), checked out at `src/orca` instead of `src/orchestrator`. In an existing
workspace, after pulling the commit that renamed it:

```bash
cd src
git submodule sync orca && git submodule update --init orca
rm -rf orchestrator   # the old checkout git leaves behind (check it has no
                      # uncommitted work first)
```
