# Preparing a machine for Orca

What a robot computer, VM host or driver station needs before Orca (the
orchestrator) can manage it. Do it once per machine, and the SSH key part once
per operator laptop. The team's machines are listed in the robot workspace,
`<ws>/src/project/orca/machines.json` (lance-2026); see the
[README](../README.md#machines).

"Operator laptop" is the computer that runs Orca; every other machine only
needs SSH and Python, as below.

## Every machine

After that, the machine card's **Setup** list checks everything below and
has a **Fix** button for most of it (a script run in a terminal on the
machine, where you type the sudo password). This page is the manual version.

**Network → Add a machine** does steps 1 and 3 for a new machine: it finds
the machine (network scan, a device plugged into a shared port, or an
address), shows its host key to compare and trusts it, installs your SSH key
(a terminal asks for the account's password once), connects, and proposes
the inventory entry. The steps below are what it does, for doing them by hand
or on another operator laptop.

The machine needs an SSH server: on Ubuntu `sudo apt install openssh-server`,
on macOS System Settings → General → Sharing → Remote Login.

1. **SSH key access from each operator laptop.** The orchestrator never types
   passwords:
   ```bash
   ssh-keygen -t ed25519            # on the laptop, if it has no key yet
   ssh-copy-id lance@10.11.11.15    # once per machine, any of its addresses
   ```
2. **Python 3.9 or newer** as `python3`. Ubuntu has it. On macOS,
   `/usr/bin/python3` comes with the Command Line Tools (installed with
   Homebrew, or `xcode-select --install`).
3. **First connection: trust the host key.** The first time the orchestrator
   reaches a machine, its card says *Unknown host key*. Click **Show
   fingerprint**, compare it with the machine's own key, then **Trust this
   key**:
   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub   # on the machine itself
   ```
   Team machines use one key name for all their addresses (e.g.
   `robot-vm.orca`), so this happens once per machine per laptop, not
   once per address.

Nothing else is installed by hand: the orchestrator copies its agent to
`~/.orca/agent.py` and keeps it up to date.

## Ubuntu machines (robot VM, LattePandas, driver stations)

- **Lingering**, so orchestrator-started processes and the agent aren't tied to
  an SSH login session:
  ```bash
  sudo loginctl enable-linger lance
  ```
- **The workspace** at the path in `machines.json` (normally `~/lance-ws`),
  built with `./src/build.sh`. The Deploy page can create and build it: it
  needs `rsync` on the machine (`sudo apt install rsync`) and ROS under
  `/opt/ros`.
- **Internet sharing** (driver stations, or any machine that shares its
  internet over Ethernet): click one of its Ethernet ports on the Network map
  (or Network → List → Internet sharing) → *Install helper* on that machine, and type your sudo password in the terminal that opens. It
  installs `/usr/local/sbin/orca-share`, the sudoers rule
  `/etc/sudoers.d/orca-share` and `dnsmasq-base`. To remove it:
  `sudo rm /usr/local/sbin/orca-share /etc/sudoers.d/orca-share`.

### Robot computers: CAN without a password prompt

`run.sh --canbus` brings CAN up with `sudo slcand` and `sudo ip link`. Started
from the orchestrator there's no terminal to type a password into, so sudo fails
at once with *"a terminal is required to authenticate"* (visible in the run's
output). Allow just those commands without a password:

```bash
sudo visudo -f /etc/sudoers.d/csm-can
```
```
# CAN bringup/shutdown for run.sh --canbus (phoenix-driver/scripts)
lance ALL=(root) NOPASSWD: /usr/bin/slcand, /usr/bin/ip link *, /usr/sbin/ip link *, /usr/bin/pkill slcand, /usr/bin/pkill -f slcand*
```

Check with `sudo -n ip link show` (no password prompt) on the robot computer.

## Restarting from Orca (optional)

A machine's card has **Restart**. Without a password for the restart
command it opens a terminal to type the sudo password in; with this rule
it's one click (the setup checklist's *Restart without a password* → Fix
installs it):
```
# Linux (/etc/sudoers.d/orca-reboot)
lance ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
# macOS (/etc/sudoers.d/orca-reboot)
lance ALL=(root) NOPASSWD: /sbin/shutdown -r now
```

## macOS VM hosts (robot mac)

- csm-vm at the path in `machines.json` (`vm.csm_vm_path`, normally
  `~/code/csm-vm`), **with the run-vm.sh that has `--overlay` and the serial
  console** (`./run-vm.sh --help` lists `--overlay`). Older copies stop at the
  "uncommitted overlay" question when started without a terminal. A newer
  run-vm.sh needs its `lib/` folder next to it; the checklist's "Update
  run-vm.sh" fix copies both from this laptop's checkout.
- **Passwordless sudo for QEMU, socat, kill and qemu-img**, which run-vm.sh
  runs as root (vmnet networking and USB passthrough need it, the monitor and
  serial sockets are root's, Force stop kills root's QEMU, and an overlay a
  root-run start made needs root to commit):
  ```bash
  sudo visudo -f /etc/sudoers.d/csm-vm
  ```
  ```
  # csm-vm: start/stop the VM and reach its sockets without a terminal
  lance ALL=(root) NOPASSWD: /opt/homebrew/bin/qemu-system-aarch64, /opt/homebrew/bin/socat, /bin/kill, /opt/homebrew/bin/qemu-img
  ```
  Passwordless QEMU as root is close to passwordless root (QEMU can open any
  file as a disk), which is acceptable on a dedicated robot computer but worth
  knowing.
- Then restart the VM once from Orca (the mac's card on the Network page →
  VM → Restart): VMs started by an older run-vm.sh have no serial console.
- No other job should start the VM: an old LaunchDaemon that runs
  `run-vm.sh --startd` at boot (as root) leaves a root-owned overlay behind
  each time it fails, which Orca then shows as an unexpected overlay. Check
  with `grep -l run-vm /Library/LaunchDaemons/*.plist`; csm-vm's
  `autostart.sh --install` is the supported way to start it at boot.
- Prefer the mac's IP addresses over its `<name>.local` name in the
  inventory: with Wi-Fi and Ethernet on the same network, the `.local` name
  can resolve to the Wi-Fi address only, and SSH over a mac's Wi-Fi has
  ~100 ms of jittery latency (power saving) against ~2 ms over Ethernet.
  Orca also tries the remembered Ethernet address.

## Good to know

- **macOS hides the Wi-Fi network name** (SSID) from programs without
  Location Services access, which robot macs usually have turned off. Orca
  still shows a mac's Wi-Fi channel, band and signal, just not the name.
- **Clocks.** Machine cards show each machine's clock offset from the laptop.
  A few tens of milliseconds is normal; right after a VM (re)starts it can be
  about half a second behind until NTP syncs. Worth knowing when lining up
  bags or logs from several machines.
- **macOS's built-in rsync** is openrsync (protocol 29). Deploy targets are
  the Ubuntu machines that run the robot code, so this only matters if you
  push to a mac.
