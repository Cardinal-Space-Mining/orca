# Preparing a machine for the hub

What a robot computer, VM host or driver station needs before the hub can
manage it. Do it once per machine (and the SSH key part once per operator
laptop). Machines are listed in `src/project/hub/machines.json`; see the
[README](../README.md#machines).

## Every machine

1. **SSH key access from each operator laptop.** The hub never types
   passwords:
   ```bash
   ssh-keygen -t ed25519            # on the laptop, if it has no key yet
   ssh-copy-id lance@10.11.11.15    # once per machine, any of its addresses
   ```
2. **Python 3.9 or newer** as `python3`. Ubuntu has it. On macOS,
   `/usr/bin/python3` comes with the Command Line Tools (installed with
   Homebrew, or `xcode-select --install`).
3. **First connection: trust the host key.** The first time the hub reaches a
   machine, its card says *Unknown host key*. Click **Show fingerprint**,
   compare it with the machine's own key, then **Trust this key**:
   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub   # on the machine itself
   ```
   Team machines use one key name for all their addresses (e.g.
   `robot-vm.csm-hub`), so this happens once per machine per laptop, not once
   per address.

Nothing else is installed by hand: the hub copies its agent to
`~/.csm-hub/agent.py` and keeps it up to date.

## Ubuntu machines (robot VM, LattePandas, driver stations)

- **Lingering**, so hub-started processes and the agent aren't tied to an SSH
  login session:
  ```bash
  sudo loginctl enable-linger lance
  ```
- **The workspace** at the path in `machines.json` (normally `~/lance-ws`),
  built with `./src/build.sh`. The Deploy page can create and build it: it
  needs `rsync` on the machine (`sudo apt install rsync`) and ROS under
  `/opt/ros`.

### Robot computers: CAN without a password prompt

`run.sh --canbus` brings CAN up with `sudo slcand` and `sudo ip link`. Started
from the hub there's no terminal to type a password into, so sudo fails at
once with *"a terminal is required to authenticate"* (visible in the run's
output). Allow just those commands without a password:

```bash
sudo visudo -f /etc/sudoers.d/csm-can
```
```
# CAN bringup/shutdown for run.sh --canbus (phoenix-driver/scripts)
lance ALL=(root) NOPASSWD: /usr/bin/slcand, /usr/bin/ip link *, /usr/sbin/ip link *, /usr/bin/pkill slcand, /usr/bin/pkill -f slcand*
```

Check with `sudo -n ip link show` (no password prompt) on the robot computer.

## macOS VM hosts (robot mac)

- csm-vm at the path in `machines.json` (`vm.csm_vm_path`, normally
  `~/code/csm-vm`). The hub reads the VM's state from it; VM control comes in
  a later phase.
- Prefer the mac's IP addresses over `sams-mac-mini.local`-style names in the
  inventory: with Wi-Fi and Ethernet on the same network, the `.local` name
  can resolve to the slower Wi-Fi address.
