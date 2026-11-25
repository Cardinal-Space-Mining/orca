'''
The setup checklist on each machine card: one-time setup the machine needs
(lingering, sudoers rules for CAN and the VM, the sharing helper, USB
Ethernet power saving...). The agent checks each item without sudo; a fix is
a short script, shown as it runs in a terminal on that machine, where sudo can
ask for the password. Nothing here types or stores passwords.
'''

from __future__ import annotations

import glob
import os
import re
import time

from .runs import PlanError


SCRIPT_REMOTE_PATH = '~/.orchestrator/setup-%s.sh'
SAFE_PATH = re.compile(r'^~?[A-Za-z0-9_./-]*$')
CSM_VM_NEW_NAME = 'run-vm.sh.orchestrator-new'

SCRIPT_START = '''#!/bin/bash
# Written by the orchestrator: %s
set -e
echo "%s"
echo
PS4='$ '
set -x
'''
SCRIPT_END = '''
{ set +x; } 2>/dev/null
echo
echo "Done. Close this terminal; the checklist updates by itself."
'''

FIXES = {
    'linger': (
        'Keep processes running after SSH sessions end',
        'sudo loginctl enable-linger "$(id -un)"\n'),
    'rsync': (
        'Install rsync (for deploying code)',
        'sudo apt-get install -y rsync\n'),
    'avahi': (
        'Answer to <hostname>.local (mDNS)',
        'sudo apt-get install -y avahi-daemon\n'
        'sudo systemctl enable --now avahi-daemon\n'),
    'eee': (
        'Keep Energy Efficient Ethernet off on Realtek USB adapters',
        "sudo tee /etc/udev/rules.d/70-orchestrator-eee.rules >/dev/null "
        "<<'RULE'\n"
        '# Written by the orchestrator: RTL8153/8156 USB Ethernet adapters '
        'drop packets with Energy Efficient Ethernet on.\n'
        'ACTION=="add", SUBSYSTEM=="net", DRIVERS=="r8152", '
        'RUN+="/usr/sbin/ethtool --set-eee $name eee off"\n'
        'RULE\n'
        'sudo udevadm control --reload\n'
        'for driver in /sys/class/net/*/device/driver; do\n'
        '  name=$(echo "$driver" | cut -d/ -f5)\n'
        '  if [ "$(basename "$(readlink -f "$driver")")" = r8152 ]; then\n'
        '    sudo ethtool --set-eee "$name" eee off || true\n'
        '  fi\n'
        'done\n'),
    'can-sudo': (
        'Let run.sh --canbus bring up CAN without a password',
        'RULE="$(mktemp)"\n'
        'echo "$(id -un) ALL=(root) NOPASSWD: /usr/bin/slcand, '
        '/usr/bin/ip link *, /usr/sbin/ip link *, /usr/bin/pkill slcand, '
        '/usr/bin/pkill -f slcand*" > "$RULE"\n'
        'sudo visudo -cf "$RULE"\n'
        'sudo install -o root -g root -m 0440 "$RULE" '
        '/etc/sudoers.d/csm-can\n'
        'rm -f "$RULE"\n'),
    'vm-sudo': (
        'Let run-vm.sh start the VM without a password',
        'RULE="$(mktemp)"\n'
        'echo "$(id -un) ALL=(root) NOPASSWD: '
        '/opt/homebrew/bin/qemu-system-aarch64, /opt/homebrew/bin/socat" '
        '> "$RULE"\n'
        'sudo visudo -cf "$RULE"\n'
        'sudo install -o root -g wheel -m 0440 "$RULE" '
        '/etc/sudoers.d/csm-vm\n'
        'rm -f "$RULE"\n'),
    'vm-tools': (
        'Install QEMU and socat with Homebrew',
        '/opt/homebrew/bin/brew install qemu socat\n'),
}
# Fixed elsewhere in the app.
LINK_FIXES = {'workspace': ('Deploy', '#/deploy')}


def scriptFor(check_id: str, title: str, body: str) -> str:
    return SCRIPT_START % (check_id, title) + body + SCRIPT_END


def localRunVm() -> str:
    '''This laptop's up-to-date run-vm.sh (from its csm-vm checkout), or
    None.'''
    home = os.path.expanduser('~')
    patterns = ('code/csm-vm/run-vm.sh', '*/run-vm.sh', '*/*/run-vm.sh')
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(home, pattern))):
            with open(path) as file:
                text = file.read()
            if '--overlay' in text and 'SERIAL_SOCKET' in text:
                return path
    return None


class Setup:

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def refresh(self, link):
        machine = link.machine
        result = await link.call('setup.check', {
            'workspace': machine.workspace,
            'csm_vm_path': machine.vm.csm_vm_path if machine.vm else None,
            'tags': machine.setup,
            'is_local': machine.is_local,
            'share_helper_hash': self.orchestrator.sharing.helper_hash,
        }, timeout_s=60)
        for check in result['checks']:
            check['fix'] = self.fixFor(check['id'])
        link.state['setup'] = {'checks': result['checks'],
                               'checked_s': time.time()}
        link.publish()

    def fixFor(self, check_id: str):
        if check_id in FIXES or check_id == 'sharing':
            return {'kind': 'terminal'}
        if check_id == 'csm-vm':
            return {'kind': 'terminal'} if localRunVm() else None
        if check_id in LINK_FIXES:
            label, href = LINK_FIXES[check_id]
            return {'kind': 'link', 'label': label, 'href': href}
        return None

    async def fixCommand(self, link, check_id: str) -> list:
        '''Write the fix script to the machine; returns the command to run
        it in a terminal there.'''
        if check_id == 'sharing':
            return await self.orchestrator.sharing.installCommand(
                link.machine.id)
        if check_id == 'csm-vm':
            title, body = await self.pushRunVm(link)
        elif check_id in FIXES:
            title, body = FIXES[check_id]
        else:
            raise PlanError('nothing to run for "%s"' % check_id)
        written = await link.call('file.write', {
            'path': SCRIPT_REMOTE_PATH % check_id,
            'content': scriptFor(check_id, title, body)})
        return ['/bin/bash', written['path']]

    async def pushRunVm(self, link) -> tuple:
        '''Copy this laptop's run-vm.sh next to the machine's and swap it in
        (the old one is kept as run-vm.sh.bak).'''
        source = localRunVm()
        if source is None or not link.machine.vm:
            raise PlanError('no up-to-date csm-vm checkout on this laptop')
        folder = link.machine.vm.csm_vm_path
        if not SAFE_PATH.match(folder):
            raise PlanError('unusual csm-vm path: ' + folder)
        with open(source) as file:
            await link.call('file.write', {
                'path': folder.rstrip('/') + '/' + CSM_VM_NEW_NAME,
                'content': file.read()})
        shell_folder = '"$HOME"/' + folder[2:] if folder.startswith('~/') \
            else '"%s"' % folder
        body = ('cd %s\n'
                'cp -p run-vm.sh run-vm.sh.bak\n'
                'mv %s run-vm.sh\n'
                'chmod +x run-vm.sh\n'
                './run-vm.sh --help | grep -e --overlay\n'
                % (shell_folder, CSM_VM_NEW_NAME))
        return ('Update run-vm.sh from %s' % source.replace(
            os.path.expanduser('~'), '~'), body)
