# Findings log

Bugs and inconsistencies Orca's development turned up in the repos it works
with. They belong to those repos; Orca only fixes them itself when they block
its own work, so most are recorded here for their owners. Bugs in Orca
itself are fixed in Orca, not listed here. Newest entries at the bottom of
each section.

Status: **open** (not touched), **fixed** (where noted), **worked around**
(Orca avoids it; the underlying issue is still there), **info** (not a bug,
but worth knowing).

## lance-2026

The robot workspace ([Cardinal-Space-Mining/lance-2026](
https://github.com/Cardinal-Space-Mining/lance-2026), cloned as `<ws>/src`)
and its submodules. Paths are relative to `src/`.

| # | Where | Finding | Status |
|---|---|---|---|
| L1 | `launch-utils/src/launch_utils/common.py` `try_load_json_from_args` | The `json_data:=` launch argument can't work: it arrives as a string, and `resolve_json_imports` calls `.pop()` on it. | open |
| L2 | `launch-utils/src/launch_utils/common.py` `resolve_json_imports` | Nested `pragma:import` paths resolve relative to the *root* file, not the file that imports them. It works today only because every import is listed in `lance/config/launch.json`. | open |
| L3 | `launch-utils/src/launch_utils/actions.py` `NodeAction` | Only `pragma:node_options` is stripped from a resolved preset; any other `pragma:*` key (e.g. a description) would be passed to the node as a ROS parameter. | open |
| L4 | `launch-utils/src/launch_utils/common.py` | Imports `ament_index_python` and `netifaces` at module level, so the config loader can't be used without ROS sourced. | open |
| L5 | `lance/config/actions/redux.json` | Robot side's `client_hostname` is pinned to `ip.net1.dev_1`; pairing the robot with any other client needs a new preset. | open (orchestrator addresses this) |
| L6 | `run.sh` | No `trap`: if the script itself is interrupted or killed, `can_shutdown.sh` doesn't run. | open |
| L7 | `sync.sh` | Sources its cache from `/tmp/last_lance_sync.conf`, a world-writable location (another local user could plant it). Low risk. | open |
| L8 | `lance/config/presets/dev_presets.json` `lidar_dev_net1` | `pragma:action_overrides` sets `"foxglove_bride"` (typo for `foxglove_bridge`); the override is silently ignored, so `dev:=lidar_dev_net1`/`net2` start no Foxglove bridge. | open |
| L9 | `lance/config/presets/gz_sim_presets.json` `gz_client` | `lance1/2_ucf_left` and `lance1/2_ucf_right` override `gz_sim`, but their parent `lance*_ksc` doesn't: choosing a UCF arena for the driver-station side also starts a simulator there (the config browser shows `gz_sim` enabled for `gz_client:=lance2_ucf_right`). Probably meant for `gz_robot`/`gz_full` only. | open |
| L10 | `launch-utils/src/launch_utils/common.py` `resolve_json_imports` | Imports are queued in a `set` and popped in arbitrary order, while the README says earlier files take precedence on conflicts. No current files conflict, so nothing breaks today. | open |
| L11 | `phoenix-driver/scripts/can_bringup.sh`, `can_shutdown.sh` | Call `sudo` internally. Started without a terminal (the orchestrator, or any autostart), sudo fails at once ("a terminal is required"). The orchestrator's machine setup doc gives a narrow NOPASSWD rule; a udev/systemd-networkd CAN setup would avoid sudo entirely. | open (documented in docs/machine-setup.md) |
| L12 | `sync.sh` `--ws` | Pushes `src/.git` with `--delete`, so a push removes commits, branches and stashes that only exist on the target. Orca's push protects `.git` from deletion. | worked around (orchestrator) |
| L13 | `sync.sh` | Pushes `__pycache__`/`.pyc` files along with the code. | worked around (orchestrator) |
| L14 | `build.sh` | Runs colcon twice (everything but lance, then lance), so `log/latest_build` only holds lance's logs; the other packages' logs are in the previous `build_*` directory. Not a bug, but easy to trip over when looking for a build error. | info (orchestrator lists each package's newest log) |
| L15 | `net_adapter` motor `*/info` channels | In the two-machine motor sim, the five Talon `*/info` topics sent to the client were 120 of net_adapter's 129 kbps (24 kbps each, at the motor-sim rate). Not a bug; the obvious place to look if the real robot comes close to the 4,000 kbps budget. | info |
| L16 | `lance/config/actions/shared.json` `ip.*` | The addresses are the last ones DHCP happened to give each machine (the LattePandas set theirs statically); they go stale when leases change, and `redux` presets and the lidar driver read them. The orchestrator now replaces the mapped ones with live addresses at launch (`config_addresses` in the inventory); launches by hand still use the file. | worked around (orchestrator) |
| L17 | `net-adapter` (zenoh transport, `util/zenoh_utils.hpp`) and `/clock` in sim | Every message goes out in its own TCP segment: `transport/link/tx/batch_size` (1400, `net_batch_size`) is only an upper bound, and zenoh fills a batch only when messages queue up, which they don't on a fast link. In the split Gazebo sim (2026-11-24, driver station on Wi-Fi) the link carried 1,951 kbps for 778 kbps of net_adapter payload: 1,376 segments/s of ~82 bytes from the robot plus ~440 pure ACKs/s back, each with ~66 bytes of Ethernet/IP/TCP headers (~960 kbps). About 980 of those segments/s are `/clock` (94 kbps of data). Without `/clock` (real robot) the same arithmetic still gives roughly 300 kbps of headers for the ~370 msg/s of lidar, IMU and motor `*/info`. The budget counts what's on the air, so headers count. Options: forward `/clock` at a lower rate (or not at all; the client can follow the robot's stamps), lower the 50 Hz `*/info` rates (L15), or make zenoh coalesce (a batching time limit in newer zenoh, or a send queue that flushes every few ms). | open |
| L18 | `lance/config/presets/dev_presets.json` `dev:=lidar_dev_net1` | Its `pragma:action_overrides` select `foxglove_bride` (typo for `foxglove_bridge`); launch-utils ignores overrides of actions that don't exist, so this preset starts without the Foxglove bridge. The Config page's preset editor flags it when the preset is edited. | open |
| L19 | `launch-utils/src/launch_utils/preprocess.py` | Uses `dict \| None` type hints, which Python 3.9 can't evaluate (`TypeError` on import), so launch-utils needs 3.10+. Orca's own code runs on 3.9, but its Config, Launch and preset pages use launch-utils on the laptop; the README promised 3.9 for the laptop. A `from __future__ import annotations` at the top of the file would make it 3.9-compatible. | worked around (README now says 3.10+ on the laptop; CI tests the agent on 3.9 and everything on 3.10, 3.12, 3.14) |

## csm-vm

[Cardinal-Space-Mining/csm-vm](https://github.com/Cardinal-Space-Mining/csm-vm):
`run-vm.sh` runs the robot's Ubuntu VM in QEMU on its mac mini. "Fixed (not
committed yet)" means the change exists but hasn't reached csm-vm's main yet;
Orca's setup checklist offers to update a mac's run-vm.sh from a newer
csm-vm checkout on the laptop.

| # | Where | Finding | Status |
|---|---|---|---|
| V1 | `run-vm.sh` `create_or_reuse_overlay interactive` | `--startd`, `--restartd` and `--term` prompt with `read` when an overlay exists. Without a terminal, `read` hits EOF and `set -e` aborts the script. | fixed (not committed yet): `--overlay` (commit, reuse or discard), and a clear error instead of a silent abort without a terminal |
| V2 | `run-vm.sh` `run_daemon` | Calls `sudo -v`, which asks for a password even when the user has passwordless sudo for specific commands (sudoers `verifypw` defaults to `all`). | fixed (not committed yet): `sudo -v` only with a terminal; otherwise it checks passwordless sudo for QEMU up front |
| V3 | `run-vm.sh` | Doesn't set `PATH`. Over non-interactive SSH on macOS, `/opt/homebrew/bin` isn't on `PATH` (zsh reads `.zprofile` only for login shells), so `qemu-img`, `qemu-system-aarch64`, `socat` and `pv` aren't found. | fixed (not committed yet): run-vm.sh adds Homebrew to PATH |
| V4 | `docs/vm-management.md` | Lists a `MAC` variable; the script uses `MAC_BRIDGE`, `MAC_HOST` and `MAC_WIFI`. | open |
| V5 | `run-vm.sh` `run_daemon` | Started QEMU in the background and returned success without checking it was running, so a failed start looked like success. | fixed (not committed yet): waits 3 s and reports |
| V6 | `run-vm.sh` daemon/launchd modes | No serial console: the only way into a VM with broken networking was stopping it and restarting with `--term`. | fixed (not committed yet): serial port on `/tmp/qemu-serial.sock` |
| V7 | `run-vm.sh` `MAC_BRIDGE`, `MAC_HOST`, `MAC_WIFI` | Hard-coded (52:54:00:12:34:56/57/58), so every VM started from csm-vm without its own config has the same MACs, e.g. the robot VM and a bench VM made the same way. Two of them on one network would fight over the address, and MACs can't tell them apart (the orchestrator ignores MACs that more than one machine reported). Give each VM its own MACs in its csm-vm config. A VM cloned from another's disk also shares its SSH host keys (and `/etc/machine-id`): the add-machine wizard warns when a new machine's key is already trusted for another, and the fix on the clone is `sudo rm /etc/ssh/ssh_host_* && sudo dpkg-reconfigure openssh-server` plus a new machine-id. | open |

## netshare

[netshare](https://github.com/S1ink/netshare), a script that shares a Linux
machine's internet over Ethernet. Orca's internet sharing replaces it (README,
"Sharing internet over Ethernet"); recorded for anyone still using the script.

| # | Where | Finding | Status |
|---|---|---|---|
| N1 | `netshare.sh` `cmd_up` (dnsmasq start) | Without a dnsmasq systemd unit it runs `pkill -x dnsmasq`, which kills every dnsmasq on the machine (NetworkManager's shared connections, libvirt, LXD). With the unit it restarts and `systemctl enable`s the system-wide dnsmasq service, which stays enabled after teardown and serves DNS on port 53 next to systemd-resolved. | open (orchestrator runs its own DHCP-only instance) |
| N2 | `netshare.sh` `load_profile`, `load_state` | Profiles in `~/.config/netshare/profiles/` are `source`d by bash running as root, so anything running as the user can put commands in a profile and have them run as root the next time netshare runs; a passwordless sudo rule for netshare would be passwordless root. | open (orchestrator's helper takes checked arguments only) |
| N3 | `netshare.sh` `configure_ufw`, `configure_firewalld` | `configure_ufw` sets `DEFAULT_FORWARD_POLICY="ACCEPT"` in `/etc/default/ufw` and never puts it back, so the firewall forwards everything after sharing stops. firewalld zones are changed with `--permanent`, and the dnsmasq and NetworkManager files go to `/etc`, so a crash or reboot mid-session leaves them until the next netshare run. | open (orchestrator changes runtime state only) |
| N4 | `netshare.sh` | Asks questions (`ask_yn`) on conflicts, a missing upstream route or a Wi-Fi downstream, and has no machine-readable status, so it can't be driven by another program. | open |
