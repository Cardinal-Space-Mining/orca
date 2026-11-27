'''
The network map: every IP network the machines are on, each machine's ports
on them, and the other devices seen there (routers, the lidar, things plugged
into a share), built from what the agents report.

A line on the map means "on the same IP network", not "cabled together":
switches and access points are invisible. Devices come from the agents'
neighbour (ARP) tables, DHCP leases of internet sharing and network scans;
the lance config's address constants name the ones it knows (10.11.10.3 →
"lidar").
'''

from __future__ import annotations

import ipaddress
import time
from typing import Optional


DEVICE_TTL_S = 15 * 60
PORT_KINDS = ('ethernet', 'wifi', 'bridge', 'can')
IGNORED_PREFIXES = ('docker', 'br-', 'veth', 'virbr', 'lo', 'vmenet', 'utun',
                    'awdl', 'llw', 'anpi', 'ap1', 'gif', 'stf')
MACHINE_KINDS = ('mac', 'vm', 'robot', 'laptop', 'desktop', 'board',
                 'computer')



# --- Devices that aren't inventory machines ----------------------------------



class DeviceBook:
    '''Everything seen on the networks recently, by MAC.'''

    def __init__(self):
        self.devices = {}  # mac -> record

    def see(self, mac: Optional[str], address: str, source: str,
            name: Optional[str] = None, seen_by: Optional[str] = None,
            now_s: Optional[float] = None):
        if not mac or not address:
            return
        now_s = time.time() if now_s is None else now_s
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return
        if ip.version != 4 or ip.is_link_local or ip.is_loopback \
                or ip.is_multicast:
            return
        record = self.devices.setdefault(mac, {
            'mac': mac, 'address': address, 'names': [], 'sources': [],
            'seen_by': [], 'seen_s': now_s})
        record['address'] = address
        record['seen_s'] = now_s
        if name and name not in record['names']:
            record['names'].append(name)
        if source not in record['sources']:
            record['sources'].append(source)
        if seen_by and seen_by not in record['seen_by']:
            record['seen_by'].append(seen_by)

    def snapshot(self, now_s: Optional[float] = None) -> list:
        now_s = time.time() if now_s is None else now_s
        for mac in [mac for mac, record in self.devices.items()
                    if now_s - record['seen_s'] > DEVICE_TTL_S]:
            del self.devices[mac]
        return [dict(record) for record in self.devices.values()]



# --- Machines and ports ------------------------------------------------------



def machineKind(machine: dict, info: Optional[dict]) -> str:
    '''The icon for a machine: its inventory "icon", else a guess.'''
    if machine.get('icon') in MACHINE_KINDS:
        return machine['icon']
    if machine.get('host'):
        return 'vm'
    if (info or {}).get('platform') == 'macos' or machine.get('vm'):
        return 'mac'
    if 'can' in (machine.get('setup') or []):
        return 'robot'
    text = ' '.join([machine.get('id', ''), machine.get('label', ''),
                     machine.get('role', ''),
                     (info or {}).get('hostname', '')]).lower()
    if 'panda' in text or 'pi' in text.split():
        return 'board'
    if 'laptop' in text or 'precision' in text or 'thinkpad' in text:
        return 'laptop'
    if machine.get('is_local'):
        return 'desktop'
    return 'computer'


def portLabel(interface: dict) -> str:
    name = interface['name']
    label = (interface.get('label') or '').lower()
    if interface['kind'] == 'wifi':
        return 'Wi-Fi'
    if interface['kind'] == 'can':
        return 'CAN'
    if interface['kind'] == 'bridge':
        return 'Bridge'
    if name.startswith('enx') or 'usb' in label:
        return 'USB Ethernet'
    return 'Ethernet'


def isPort(interface: dict) -> bool:
    if interface.get('kind') not in PORT_KINDS:
        return False
    if interface['name'].startswith(IGNORED_PREFIXES):
        return False
    # Bridges only matter when they carry an address (a VM's host-only link).
    return interface['kind'] != 'bridge' or bool(interface.get('ipv4'))


def usableNetworks(interface: dict) -> list:
    networks = []
    for cidr in interface.get('ipv4') or []:
        try:
            address = ipaddress.ip_interface(cidr)
        except ValueError:
            continue
        if not (address.ip.is_loopback or address.ip.is_link_local):
            networks.append(address)
    return networks



# --- The model ---------------------------------------------------------------



class NetworkIndex:
    '''Networks by the address range they cover. Overlapping ranges (one
    machine says /16, another /24) are one network: the widest.'''

    def __init__(self):
        self.networks = []  # [ipaddress network, ...]

    def add(self, network) -> str:
        for known in self.networks:
            if known.overlaps(network):
                if network.prefixlen < known.prefixlen:
                    self.networks[self.networks.index(known)] = network
                    return str(network)
                return str(known)
        self.networks.append(network)
        return str(network)

    def find(self, address) -> Optional[str]:
        ip = ipaddress.ip_address(address)
        for network in self.networks:
            if ip in network:
                return str(network)
        return None


def buildNetworkModel(links: list, devices: list, constants: dict,
                      robot_link: Optional[dict] = None) -> dict:
    '''links: machine snapshots (MachineLink.snapshot()); devices:
    DeviceBook.snapshot(); constants: lance config address constants
    ({"ip.net2.lidar": "10.11.10.3"}); robot_link: the budget's
    {"machine_id", "name"}.'''
    robot_link = robot_link or {}
    index = NetworkIndex()
    # An offline machine keeps its last stats (its card says where it was
    # last seen), but those describe networks it may have left long ago:
    # only machines online now make networks and say what's on them. An
    # offline one still shows its ports on networks that exist.
    online = [link for link in links if link['stats']
              and link['connection']['state'] == 'online']

    # Every network first, widest ranges winning.
    for link in sorted(online, key=lambda link: link['machine']['id']):
        for interface in link['stats'].get('interfaces', []):
            if isPort(interface):
                for address in sorted(usableNetworks(interface),
                                      key=lambda a: a.network.prefixlen):
                    index.add(address.network)

    machines = []
    members = {}       # network -> [member]
    gateways = {}      # network -> gateway address
    ssids = {}         # network -> {ssid}
    shares = {}        # network -> share
    own_macs = set()
    own_addresses = set()
    for link in links:
        machine = link['machine']
        stats = link['stats'] or {}
        is_online = link['connection']['state'] == 'online'
        route = stats.get('default_route') or {}
        sessions = {session['downstream']: session for session in
                    ((link.get('sharing') or {}).get('sessions') or [])} \
            if is_online else {}
        ports = []
        for interface in stats.get('interfaces', []):
            if interface.get('mac'):
                own_macs.add(interface['mac'])
            if not isPort(interface):
                continue
            addresses = usableNetworks(interface)
            if is_online:
                for address in addresses:
                    own_addresses.add(str(address.ip))
            network = index.find(addresses[0].ip) if addresses else None
            wifi = interface.get('wifi') or {}
            port = {
                'name': interface['name'],
                'kind': interface['kind'],
                'label': portLabel(interface),
                'mac': interface.get('mac'),
                'addresses': [str(address) for address in addresses],
                'network': network,
                'is_up': interface.get('is_up'),
                'rx_bps': interface.get('rx_bps'),
                'tx_bps': interface.get('tx_bps'),
                'is_default_route': interface['name'] == route.get(
                    'interface'),
                'is_robot_link': robot_link.get('machine_id') == machine['id']
                and robot_link.get('name') == interface['name'],
                'sharing': interface['name'] in sessions,
                'wifi': wifi or None,
            }
            ports.append(port)
            if network:
                members.setdefault(network, []).append({
                    'machine_id': machine['id'], 'port': interface['name'],
                    'address': str(addresses[0].ip)})
                if not is_online:
                    continue  # its gateway and Wi-Fi may be long gone
                if port['is_default_route'] and route.get('gateway'):
                    gateways[network] = route['gateway']
                if wifi.get('ssid'):
                    ssids.setdefault(network, set()).add(wifi['ssid'])
                session = sessions.get(interface['name'])
                if session:
                    shares[network] = {'machine_id': machine['id'],
                                       'port': interface['name'],
                                       'name': session['name']}
        machines.append({
            'id': machine['id'],
            'label': machine['label'],
            'kind': machineKind(machine, link.get('info')),
            'state': link['connection']['state'],
            'host': machine.get('host'),
            'guest': (machine.get('vm') or {}).get('guest'),
            'is_local': machine.get('is_local'),
            'ports': ports,
        })

    names_by_address = {}
    for name, value in (constants or {}).items():
        names_by_address.setdefault(value, name.split('.')[-1])
    network_devices = {}
    device_list = []
    for device in devices:
        if device['mac'] in own_macs or device['address'] in own_addresses:
            continue
        network = index.find(device['address'])
        if network is None:
            continue
        label = names_by_address.get(device['address'])
        is_gateway = gateways.get(network) == device['address']
        kind = 'router' if is_gateway else 'lidar' if label and \
            'lidar' in label else 'device'
        entry = dict(device, network=network, kind=kind,
                     label='gateway' if is_gateway and not label else
                     label or (device['names'][0] if device['names']
                               else None))
        device_list.append(entry)
        network_devices.setdefault(network, []).append(device['mac'])

    networks = []
    machine_hosts = {machine['id']: machine.get('host') for machine in
                     machines}
    for network in index.networks:
        cidr = str(network)
        member_ids = {member['machine_id'] for member in members.get(cidr, [])}
        if cidr in shares:
            kind = 'share'
        elif cidr in gateways:
            kind = 'lan'
        elif len(member_ids) == 2 and any(
                machine_hosts.get(member) in member_ids
                for member in member_ids):
            kind = 'host-only'
        else:
            kind = 'local'
        networks.append({
            'id': cidr, 'cidr': cidr, 'kind': kind,
            'gateway': gateways.get(cidr),
            'internet': cidr in gateways,
            'ssids': sorted(ssids.get(cidr, [])),
            'share': shares.get(cidr),
            'members': members.get(cidr, []),
            'devices': network_devices.get(cidr, []),
        })
    order = {'lan': 0, 'share': 1, 'local': 2, 'host-only': 3}
    networks.sort(key=lambda entry: (order[entry['kind']],
                                     -len(entry['members']), entry['cidr']))
    return {'networks': networks, 'machines': machines,
            'devices': device_list}
