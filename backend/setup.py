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


SCRIPT_REMOTE_PATH = '~/.orca/setup-%s.sh'
SAFE_PATH = re.compile(r'^~?[A-Za-z0-9_./-]*$')
CSM_VM_NEW_NAME = 'run-vm.sh.orca-new'
CSM_VM_NEW_LIBRARY = 'run-vm.lib-%s.orca-new'  # lib/<name>, uploaded
# The files run-vm.sh sources: source "$SCRIPTPATH/lib/common.sh"
RUN_VM_LIBRARY = re.compile(r'^\s*source "\$SCRIPTPATH/(lib/[\w.-]+\.sh)"',
                            re.M)

SCRIPT_START = '''#!/bin/bash
# Written by Orca: %s
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
        "sudo tee /etc/udev/rules.d/70-orca-eee.rules >/dev/null "
        "<<'RULE'\n"
        '# Written by Orca: RTL8153/8156 USB Ethernet adapters '
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
        '/opt/homebrew/bin/qemu-system-aarch64, /opt/homebrew/bin/socat, '
        '/bin/kill, /opt/homebrew/bin/qemu-img" > "$RULE"\n'
        'sudo visudo -cf "$RULE"\n'
        'sudo install -o root -g wheel -m 0440 "$RULE" '
        '/etc/sudoers.d/csm-vm\n'
        'rm -f "$RULE"\n'),
    'reboot-sudo': (
        'Let Orca restart this machine without a password',
        'if [ "$(uname)" = Darwin ]; then\n'
        '  COMMAND="/sbin/shutdown -r now"; GROUP=wheel\n'
        'else\n'
        '  COMMAND="$(command -v systemctl) reboot"; GROUP=root\n'
        'fi\n'
        'RULE="$(mktemp)"\n'
        'echo "$(id -un) ALL=(root) NOPASSWD: $COMMAND" > "$RULE"\n'
        'sudo visudo -cf "$RULE"\n'
        'sudo install -o root -g "$GROUP" -m 0440 "$RULE" '
        '/etc/sudoers.d/orca-reboot\n'
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

    def __init__(self, orca):
        self.orca = orca

    async def refresh(self, link):
        machine = link.machine
        result = await link.call('setup.check', {
            'workspace': machine.workspace,
            'csm_vm_path': machine.vm.csm_vm_path if machine.vm else None,
            'tags': machine.setup,
            'is_local': machine.is_local,
            'share_helper_hash': self.orca.sharing.helper_hash,
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
            return await self.orca.sharing.installCommand(
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
        '''Copy this laptop's run-vm.sh, and the lib/ files it sources, next
        to the machine's and swap them in (the old ones are kept as
        run-vm.sh.bak and lib.bak).'''
        source = localRunVm()
        if source is None or not link.machine.vm:
            raise PlanError('no up-to-date csm-vm checkout on this laptop')
        folder = link.machine.vm.csm_vm_path
        if not SAFE_PATH.match(folder):
            raise PlanError('unusual csm-vm path: ' + folder)
        libraries = sorted(glob.glob(os.path.join(
            os.path.dirname(source), 'lib', '*.sh')))
        with open(source) as file:
            needed = set(RUN_VM_LIBRARY.findall(file.read()))
        missing = needed - {'lib/' + os.path.basename(path)
                            for path in libraries}
        if missing:
            raise PlanError('the csm-vm checkout on this laptop is missing '
                            + ', '.join(sorted(missing)))
        uploads = [(source, CSM_VM_NEW_NAME)] + [
            (path, CSM_VM_NEW_LIBRARY % os.path.basename(path))
            for path in libraries]
        for path, name in uploads:
            with open(path) as file:
                await link.call('file.write', {
                    'path': folder.rstrip('/') + '/' + name,
                    'content': file.read()})
        shell_folder = '"$HOME"/' + folder[2:] if folder.startswith('~/') \
            else '"%s"' % folder
        body = 'cd %s\n' % shell_folder
        if libraries:
            body += ('rm -rf lib.bak\n'
                     '[ -d lib ] && cp -Rp lib lib.bak\n'
                     'mkdir -p lib\n')
            for path in libraries:
                name = os.path.basename(path)
                body += 'mv %s lib/%s\n' % (CSM_VM_NEW_LIBRARY % name, name)
        body += ('cp -p run-vm.sh run-vm.sh.bak\n'
                 'mv %s run-vm.sh\n'
                 'chmod +x run-vm.sh\n'
                 './run-vm.sh --help | grep -e --overlay\n'
                 % CSM_VM_NEW_NAME)
        return ('Update run-vm.sh from %s' % source.replace(
            os.path.expanduser('~'), '~'), body)
