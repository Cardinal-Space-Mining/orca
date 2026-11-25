# Preparing a machine for the orchestrator

What a robot computer, VM host or driver station needs before the orchestrator
can manage it. Do it once per machine (and the SSH key part once per operator
laptop). Machines are listed in `src/project/orca/machines.json`; see
the [README](../README.md#machines).

## Every machine

After that, the machine card's **Setup** list checks everything below and
has a **Fix** button for most of it (a script run in a terminal on the
machine, where you type the sudo password). This page is the manual version.

**Machines → Add a machine** does steps 1 and 3 for a new machine: it finds
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

## macOS VM hosts (robot mac)

- csm-vm at the path in `machines.json` (`vm.csm_vm_path`, normally
  `~/code/csm-vm`), **with the run-vm.sh that has `--overlay` and the serial
  console** (`./run-vm.sh --help` lists `--overlay`). Older copies stop at the
  "uncommitted overlay" question when started without a terminal.
- **Passwordless sudo for QEMU and socat**, which run-vm.sh runs as root
  (vmnet networking and USB passthrough need it, and the monitor and serial
  sockets are root's):
  ```bash
  sudo visudo -f /etc/sudoers.d/csm-vm
  ```
  ```
  # csm-vm: start/stop the VM and reach its sockets without a terminal
  lance ALL=(root) NOPASSWD: /opt/homebrew/bin/qemu-system-aarch64, /opt/homebrew/bin/socat
  ```
  Passwordless QEMU as root is close to passwordless root (QEMU can open any
  file as a disk), which is acceptable on a dedicated robot computer but worth
  knowing.
- Then restart the VM once from the orchestrator (Machines → the mac's VM →
  Restart): VMs started by an older run-vm.sh have no serial console.
- Prefer the mac's IP addresses over `sams-mac-mini.local`-style names in the
  inventory: with Wi-Fi and Ethernet on the same network, the `.local` name
  can resolve to the slower Wi-Fi address.
