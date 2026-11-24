# Findings log

Bugs and inconsistencies found while building csm-hub, in the other repos and on
the test machines. Only issues that block hub development get fixed here; the
rest are recorded for a later look. Newest entries at the bottom of each section.

Status: **open** (not touched), **fixed** (commit noted), **worked around** (the
hub avoids it; the underlying issue is still there).

## lance-ws

| # | Where | Finding | Status |
|---|---|---|---|
| L1 | `launch-utils/src/launch_utils/common.py` `try_load_json_from_args` | The `json_data:=` launch argument can't work: it arrives as a string, and `resolve_json_imports` calls `.pop()` on it. | open |
| L2 | `launch-utils/src/launch_utils/common.py` `resolve_json_imports` | Nested `pragma:import` paths resolve relative to the *root* file, not the file that imports them. It works today only because every import is listed in `lance/config/launch.json`. | open |
| L3 | `launch-utils/src/launch_utils/actions.py` `NodeAction` | Only `pragma:node_options` is stripped from a resolved preset; any other `pragma:*` key (e.g. a description) would be passed to the node as a ROS parameter. | open |
| L4 | `launch-utils/src/launch_utils/common.py` | Imports `ament_index_python` and `netifaces` at module level, so the config loader can't be used without ROS sourced. | open |
| L5 | `lance/config/actions/redux.json` | Robot side's `client_hostname` is pinned to `ip.net1.dev_1`; pairing the robot with any other client needs a new preset. | open (hub addresses this) |
| L6 | `run.sh` | No `trap`: if the script itself is interrupted or killed, `can_shutdown.sh` doesn't run. | open |
| L7 | `sync.sh` | Sources its cache from `/tmp/last_lance_sync.conf`, a world-writable location (another local user could plant it). Low risk. | open |
| L8 | `lance/config/presets/dev_presets.json` `lidar_dev_net1` | `pragma:action_overrides` sets `"foxglove_bride"` (typo for `foxglove_bridge`); the override is silently ignored, so `dev:=lidar_dev_net1`/`net2` start no Foxglove bridge. | open |
| L9 | `lance/config/presets/gz_sim_presets.json` `gz_client` | `lance1/2_ucf_left` and `lance1/2_ucf_right` override `gz_sim`, but their parent `lance*_ksc` doesn't: choosing a UCF arena for the driver-station side also starts a simulator there (the config browser shows `gz_sim` enabled for `gz_client:=lance2_ucf_right`). Probably meant for `gz_robot`/`gz_full` only. | open |
| L10 | `launch-utils/src/launch_utils/common.py` `resolve_json_imports` | Imports are queued in a `set` and popped in arbitrary order, while the README says earlier files take precedence on conflicts. No current files conflict, so nothing breaks today. | open |
| L11 | `phoenix-driver/scripts/can_bringup.sh`, `can_shutdown.sh` | Call `sudo` internally. Started without a terminal (the hub, or any autostart), sudo fails at once ("a terminal is required"). The hub's machine setup doc gives a narrow NOPASSWD rule; a udev/systemd-networkd CAN setup would avoid sudo entirely. | open (documented in docs/machine-setup.md) |
| L12 | `sync.sh` `--ws` | Pushes `src/.git` with `--delete`, so a push removes commits, branches and stashes that only exist on the target. The M2 VM had 5 such stashes (February WIP); the hub's push protects `.git` from deletion and reports target-only refs before pushing. | worked around (hub) |
| L13 | `sync.sh` | Pushes `__pycache__`/`.pyc` files along with the code. | worked around (hub) |
| L14 | `build.sh` | Runs colcon twice (everything but lance, then lance), so `log/latest_build` only holds lance's logs; the other packages' logs are in the previous `build_*` directory. Not a bug, but easy to trip over when looking for a build error. | info (hub lists each package's newest log) |

## csm-vm

| # | Where | Finding | Status |
|---|---|---|---|
| V1 | `run-vm.sh` `create_or_reuse_overlay interactive` | `--startd`, `--restartd` and `--term` prompt with `read` when an overlay exists. Without a terminal, `read` hits EOF and `set -e` aborts the script. | open |
| V2 | `run-vm.sh` `run_daemon` | Calls `sudo -v`, which asks for a password even when the user has passwordless sudo for specific commands (sudoers `verifypw` defaults to `all`). | open |
| V3 | `run-vm.sh` | Doesn't set `PATH`. Over non-interactive SSH on macOS, `/opt/homebrew/bin` isn't on `PATH` (zsh reads `.zprofile` only for login shells), so `qemu-img`, `qemu-system-aarch64`, `socat` and `pv` aren't found. | open |
| V4 | `docs/vm-management.md` | Lists a `MAC` variable; the script uses `MAC_BRIDGE`, `MAC_HOST` and `MAC_WIFI`. | open |

## Test machines (M2 mac mini + VM)

| # | Where | Finding | Status |
|---|---|---|---|
| M1 | `/Library/LaunchDaemons/com.samri.ubuntu-vm.plist` | A hand-made autostart daemon that runs `run-vm.sh --startd` (not `--launchd`), with no `PATH`. It would hit V1 and V3 at boot. Its label differs from the one `autostart.sh` installs (`com.samri.run-vm`). Possibly also on the robot. | open |
| M2 | `~/code/csm-vm` | Leftover `ubuntu-24.04.3-live-server-arm64.iso` (the script now uses 26.04). | open |
| M3 | Mac | `/usr/bin/rsync` is openrsync (protocol 29); `sync.sh`'s `***` filter rules are untested against it. | open |
| M4 | VM | `~/lance-ws/src` is at `a9498e4`, behind the dev machine (`eb3ab4e`). | info |
| M5 | VM | User lingering was off (systemd user services stop at logout): enabled 2026-11-24 for the hub. avahi isn't installed (no mDNS discovery of the VM). | lingering fixed; avahi open |
| M6 | Mac (any with Wi-Fi + Ethernet on one subnet) | With Wi-Fi on, `sams-mac-mini.local` resolves only to the Wi-Fi address (10.180.0.41): SSH latency went from ~2 ms to ~100 ms with jitter (Wi-Fi power saving). Use the Ethernet IP for routes (the hub inventory does now), and expect the same on the robot mac. The `m2-mac` SSH alias in docs/dev-rig-setup.md was updated to match. | worked around |
| M7 | Mac | macOS redacts the Wi-Fi network name (SSID/BSSID) from `system_profiler`, `ipconfig` and even `sudo wdutil info` unless the process has Location Services access, which the bringup docs turn off. Channel, band, signal and security are still readable. `system_profiler SPAirPortDataType` also takes ~5 s with Wi-Fi on. | worked around (hub shows channel/signal, reads in the background) |
| M8 | Mac, VM | Clocks differ from the dev machine by about −22 ms (mac) and +11 ms (VM), measured over 2–4 ms round trips. Fine for humans; worth knowing when lining up bags or logs from several machines. | info |
| M9 | VM | `~/lance-ws` had 5 git stashes from February 2026 that exist only there (`git stash list`). The hub's first deploy (2026-11-24) kept them. | info |
| M10 | VM | `~/lance-ws` was deployed from the dev machine on 2026-11-24 (branch `support-multiscan-refactor`, built for LANCE-2 only); it was on `main` @ a9498e4. | info |
