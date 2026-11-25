'''
Sharing a machine's internet over one of its Ethernet ports, e.g. the
driver station's internet with the robot plugged straight into it in the
pits: NAT and DHCP through the root-owned helper (agent/share_helper.py)
that the agent runs with sudo.

The devices on a shared port (DHCP leases plus anything else seen there) are
matched to inventory machines by MAC, and machines that were missing are
retried as soon as they show up.
'''

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os

from .runs import PlanError
from .settings import saveSettings


HELPER_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'agent', 'share_helper.py')
HELPER_REMOTE_PATH = '~/.orchestrator/share_helper.py'
INSTALL_SCRIPT_REMOTE_PATH = '~/.orchestrator/install-share-helper.sh'
DEFAULT_NAME = 'share'
# Subnets offered by default, first one not already in use on the machine.
DEFAULT_SUBNETS = ['10.11.11.0/24'] + ['10.11.%d.0/24' % third
                                      for third in range(12, 40)]
RECONNECT_DELAY_S = 3.0

INSTALL_SCRIPT = '''#!/bin/bash
# Installs the orchestrator's internet-sharing helper. Asks for your sudo
# password once; after that the orchestrator can start and stop sharing
# without one.
set -e
HELPER="$HOME/.orchestrator/share_helper.py"
USER_NAME="$(id -un)"
echo "This installs /usr/local/sbin/orchestrator-share (owned by root) and a"
echo "sudoers rule that lets $USER_NAME run just that program without a"
echo "password. It only accepts checked arguments (see the file's header)."
echo
if ! command -v dnsmasq >/dev/null && [ ! -x /usr/sbin/dnsmasq ]; then
    echo "Installing dnsmasq-base (the DHCP server)..."
    sudo apt-get install -y dnsmasq-base
fi
sudo install -o root -g root -m 0755 "$HELPER" /usr/local/sbin/orchestrator-share
RULE="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/orchestrator-share" > "$RULE"
sudo visudo -cf "$RULE" >/dev/null
sudo install -o root -g root -m 0440 "$RULE" /etc/sudoers.d/orchestrator-share
rm -f "$RULE"
echo
echo "Done: internet sharing is ready. You can close this terminal."
'''


def suggestSubnet(ports: list, downstream: str = None) -> str:
    '''The first default subnet that doesn't overlap a network the machine
    is already on (other than the port being shared).'''
    used = []
    for port in ports:
        if port['name'] == downstream:
            continue
        for cidr in port.get('ipv4') or []:
            try:
                used.append(ipaddress.ip_interface(cidr).network)
            except ValueError:
                continue
    for candidate in DEFAULT_SUBNETS:
        network = ipaddress.ip_network(candidate)
        if not any(network.overlaps(other) for other in used):
            return candidate
    return DEFAULT_SUBNETS[-1]


def devicesOn(session: dict) -> list:
    '''Leases and neighbours on a shared port, one entry per MAC.'''
    devices = {}
    for lease in session.get('leases', []):
        devices[lease['mac']] = dict(lease, source='lease')
    for neighbour in session.get('neighbours', []):
        device = devices.get(neighbour['mac'])
        if device is None:
            devices[neighbour['mac']] = {
                'mac': neighbour['mac'], 'address': neighbour['address'],
                'hostname': None, 'expires_s': None, 'source': 'neighbour',
                'state': neighbour['state']}
        else:
            device['state'] = neighbour['state']
            device['address'] = neighbour['address']
    return sorted(devices.values(),
                  key=lambda device: ipaddress.ip_address(device['address']))


class Sharing:

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        with open(HELPER_SOURCE_PATH, 'rb') as file:
            self.helper_bytes = file.read()
        self.helper_hash = hashlib.sha256(self.helper_bytes).hexdigest()[:12]
        self.seen_macs = set()  # MACs already used to wake missing machines

    # --- Status ---

    async def refresh(self, link):
        info = link.state['info'] or {}
        if info.get('platform') != 'linux':
            return
        status = await link.call('share.status',
                                 {'expected_hash': self.helper_hash})
        status['suggested_subnet'] = suggestSubnet(status.get('ports', []))
        status['options'] = self.savedOptions(link.machine.id)
        book = self.orchestrator.finder.book
        owners = {mac: machine_id for machine_id in book.machines
                  for mac in book.macs(machine_id)}
        new_macs = []
        for session in status.get('sessions', []):
            session['devices'] = devicesOn(session)
            for device in session['devices']:
                device['machine_id'] = owners.get(device['mac'])
                if device['mac'] not in self.seen_macs:
                    self.seen_macs.add(device['mac'])
                    new_macs.append(device['mac'])
        link.state['sharing'] = status
        link.publish()
        if any(mac in owners for mac in new_macs):
            asyncio.get_running_loop().call_later(
                RECONNECT_DELAY_S, self.wakeMissing)

    def wakeMissing(self):
        '''A known machine just appeared on a shared port: retry it now
        instead of waiting out its back-off.'''
        for link in self.orchestrator.finder.missingLinks():
            link.requestReconnect()

    def leaseNeighbours(self) -> dict:
        '''{address: mac} from the local machine's shares, for the finder.'''
        link = self.orchestrator.links_by_id.get('local')
        status = (link.state.get('sharing') if link else None) or {}
        return {device['address']: device['mac']
                for session in status.get('sessions', [])
                for device in session.get('devices', [])}

    # --- Starting and stopping ---

    def savedOptions(self, machine_id: str) -> dict:
        return (self.orchestrator.settings.get('sharing') or {}).get(
            machine_id) or {}

    async def up(self, machine_id: str, options: dict) -> dict:
        link = self.orchestrator.link(machine_id)
        if not options.get('downstream'):
            raise PlanError('choose the port to share on')
        params = {
            'name': options.get('name') or DEFAULT_NAME,
            'downstream': options['downstream'],
            'upstream': options.get('upstream') or None,
            'subnet': options.get('subnet') or None,
            'dns': options.get('dns') or None,
            'lease': options.get('lease') or None,
            'keep_eee': bool(options.get('keep_eee')),
        }
        result = await link.call('share.up', params, timeout_s=60)
        # Remember the choices for next time (replaced, not merged, so a
        # cleared option stays cleared).
        settings = self.orchestrator.settings
        settings['sharing'] = dict(settings.get('sharing') or {})
        settings['sharing'][machine_id] = {
            key: value for key, value in params.items()
            if value not in (None, False)}
        saveSettings(settings)
        self.orchestrator.publish('settings', settings)
        await self.refresh(link)
        return result

    async def down(self, machine_id: str, name: str) -> dict:
        link = self.orchestrator.link(machine_id)
        result = await link.call('share.down', {'name': name or DEFAULT_NAME},
                                 timeout_s=60)
        await self.refresh(link)
        return result

    async def installCommand(self, machine_id: str) -> list:
        '''Copy the helper and its install script to the machine; returns
        the command to run in a terminal there (it asks for sudo).'''
        link = self.orchestrator.link(machine_id)
        await link.call('file.write', {
            'path': HELPER_REMOTE_PATH,
            'content': self.helper_bytes.decode()})
        written = await link.call('file.write', {
            'path': INSTALL_SCRIPT_REMOTE_PATH, 'content': INSTALL_SCRIPT})
        return ['/bin/bash', written['path']]

