# Development test rig setup

How the M2 mac mini and its Ubuntu VM were prepared so the orchestrator (and
Claude Code) can drive them from the development machine. Repeat these steps
when setting up a new dev machine or a new test rig. **None of this is for the
robot**: the robot gets narrower permissions from the orchestrator's installer.

| Machine | Address | Notes |
|---|---|---|
| Dev machine (Linux) | 10.180.0.40 | Runs the orchestrator. |
| M2 mac mini | `sams-mac-mini.local` (10.180.0.39) | csm-vm at `~/code/csm-vm`. Host-only link to the VM: 192.168.18.1 |
| VM (Ubuntu 26.04) | 10.180.0.82 (bridged NIC); 192.168.18.2 (host-only link) | lance-ws at `~/lance-ws` |

## 1. SSH key on the dev machine

The orchestrator only uses key authentication (it never types passwords).

```bash
ssh-keygen -t ed25519            # no passphrase, or keep it loaded in ssh-agent
ssh-copy-id samri@sams-mac-mini.local
ssh-copy-id samri@10.180.0.82
```

## 2. SSH aliases on the dev machine

Append to `~/.ssh/config`. The orchestrator doesn't need these (it builds its
own SSH options from the machine inventory); they're for manual use and for
Claude Code's permission rules, which match on the start of a command.

```
Host m2-mac
  # Ethernet address: with the mac's Wi-Fi on, sams-mac-mini.local resolves
  # to the (slower) Wi-Fi address instead. The alias keeps the known host key.
  HostName 10.180.0.39
  HostKeyAlias sams-mac-mini.local
  User samri
  BatchMode yes
  ConnectTimeout 5
  ServerAliveInterval 5
  ServerAliveCountMax 3

Host m2-vm
  HostName 10.180.0.82
  User samri
  BatchMode yes
  ConnectTimeout 5
  ServerAliveInterval 5
  ServerAliveCountMax 3

Host m2-vm-jump
  HostName 192.168.18.2
  User samri
  ProxyJump m2-mac
  HostKeyAlias 10.180.0.82
  BatchMode yes
  ConnectTimeout 5
```

`m2-vm-jump` reaches the VM through the mac's host-only network, which still
works when the VM's own network is down. `HostKeyAlias` makes SSH check the VM's
key under the address it was first saved with, instead of asking to trust a
"new" host.

## 3. Passwordless sudo on the test machines

Broad on purpose, for fast iteration on test hardware. Each command checks the
rule with `visudo` before installing it, so a typo can't break sudo.

```bash
# mac
ssh -t sams-mac-mini.local 'echo "samri ALL=(ALL) NOPASSWD: ALL" > /tmp/orchestrator-dev && sudo visudo -cf /tmp/orchestrator-dev && sudo install -m 440 -o root -g wheel /tmp/orchestrator-dev /etc/sudoers.d/90-orchestrator-dev && rm /tmp/orchestrator-dev'

# VM
ssh -t samri@10.180.0.82 'echo "samri ALL=(ALL) NOPASSWD: ALL" > /tmp/orchestrator-dev && sudo visudo -cf /tmp/orchestrator-dev && sudo install -m 440 -o root -g root /tmp/orchestrator-dev /etc/sudoers.d/90-orchestrator-dev && rm /tmp/orchestrator-dev'
```

Check with `ssh m2-mac sudo -n true && ssh m2-vm sudo -n true`. Undo with
`sudo rm /etc/sudoers.d/90-orchestrator-dev` on each machine.

## 4. User lingering on the VM

Lets the agent's systemd user service keep running after the last SSH session
closes, and start at boot.

```bash
ssh m2-vm sudo loginctl enable-linger samri
```

## 5. Claude Code permission rules (optional)

In `~/.claude/settings.json`, merged into any existing `permissions.allow`:

```json
{
  "permissions": {
    "allow": [
      "Bash(ssh m2-mac:*)",
      "Bash(ssh m2-vm:*)",
      "Bash(ssh m2-vm-jump:*)",
      "Bash(scp:*)",
      "Bash(rsync:*)"
    ]
  }
}
```

## 6. Before risky VM testing

Back up the VM's base disk (the VM must be stopped):

```bash
ssh m2-mac 'cd ~/code/csm-vm && PATH=/opt/homebrew/bin:$PATH ./run-vm.sh --stopd && PATH=/opt/homebrew/bin:$PATH ./run-vm.sh --backup'
```

The VM disk is 64 GB and large disks are slow to back up, so keep test data
(bags, build trees) on the VM small.
