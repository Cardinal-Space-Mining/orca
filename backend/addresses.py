'''
Where machines are right now.

Every network here hands out addresses with DHCP, so the inventory can't
know them for sure. Instead each machine's agent reports its own interfaces
(address, kind, MAC) while it's connected; this module remembers them in
~/.config/orca/addresses.json, on this laptop only, and turns them
into extra routes to try when the inventory's routes don't answer:

    neighbour   the machine's MAC is in this laptop's neighbour (ARP) table
                at this address: it's there right now
    learned     the machine had this address the last time it was seen

Found routes are always checked against the machine's known host key (see
Machine.identity), so an address that now belongs to something else can't
be mistaken for the machine.
'''

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
from typing import Optional

from .inventory import Route


ADDRESSES_PATH = os.path.expanduser('~/.config/orca/addresses.json')
LEARNED_KINDS = ('ethernet', 'wifi')
FORGET_AFTER_S = 30 * 24 * 3600
MAX_FOUND_ROUTES = 4
SAVE_DELAY_S = 5.0


def isUsable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and not (ip.is_link_local or ip.is_loopback)


class AddressBook:

    def __init__(self, path: str = ADDRESSES_PATH):
        self.path = path
        # machine id -> {address -> {interface, kind, mac, prefix, seen_s,
        #                            ok_s}}
        self.machines = {}
        self.current = {}  # machine id -> addresses in its latest report
        self.save_handle = None
        self.load()

    # --- Persistence ---

    def load(self):
        try:
            with open(self.path) as file:
                data = json.load(file)
        except (OSError, ValueError):
            return
        cutoff_s = time.time() - FORGET_AFTER_S
        for machine_id, entry in (data.get('machines') or {}).items():
            addresses = {address: record for address, record
                         in (entry.get('addresses') or {}).items()
                         if record.get('seen_s', 0) >= cutoff_s}
            if addresses:
                self.machines[machine_id] = addresses

    def save(self):
        self.save_handle = None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary_path = self.path + '.tmp'
        with open(temporary_path, 'w') as file:
            json.dump({'machines': {
                machine_id: {'addresses': addresses}
                for machine_id, addresses in self.machines.items()}},
                file, indent=2, sort_keys=True)
            file.write('\n')
        os.replace(temporary_path, self.path)

    def saveSoon(self, loop=None):
        '''Coalesce writes: stats arrive every few seconds.'''
        if loop is None:
            self.save()
        elif self.save_handle is None:
            self.save_handle = loop.call_later(SAVE_DELAY_S, self.save)

    # --- Learning ---

    def learnInterfaces(self, machine_id: str, interfaces: list,
                        now_s: Optional[float] = None) -> bool:
        '''Record the addresses a machine reports for itself. Returns True
        if anything new was learned (worth saving).'''
        now_s = now_s or time.time()
        addresses = self.machines.setdefault(machine_id, {})
        current = set()
        is_new = False
        for interface in interfaces:
            if interface.get('kind') not in LEARNED_KINDS:
                continue
            for cidr in interface.get('ipv4') or []:
                address, _, prefix = cidr.partition('/')
                if not isUsable(address):
                    continue
                current.add(address)
                record = addresses.get(address)
                details = {'interface': interface['name'],
                           'kind': interface['kind'],
                           'mac': interface.get('mac'),
                           'prefix': int(prefix or 32)}
                if record is None or any(record.get(key) != value
                                         for key, value in details.items()):
                    is_new = True
                    record = dict(record or {}, **details)
                    addresses[address] = record
                record['seen_s'] = now_s
        self.current[machine_id] = current
        return is_new

    def learnConnected(self, machine_id: str, address: str,
                       now_s: Optional[float] = None) -> bool:
        '''Note that a connection to this address worked. Returns True the
        first time (worth saving).'''
        record = self.machines.get(machine_id, {}).get(address)
        if record is None:
            return False
        is_first = not record.get('ok_s')
        record['ok_s'] = now_s or time.time()
        return is_first

    def forget(self, machine_id: str):
        self.machines.pop(machine_id, None)
        self.current.pop(machine_id, None)

    # --- Using what was learned ---

    def macs(self, machine_id: str) -> set:
        '''MACs that identify this machine: ones no other machine reported
        too. (VMs started from one csm-vm setup share their MACs.)'''
        def macsOf(other_id):
            return {record['mac'] for record
                    in self.machines.get(other_id, {}).values()
                    if record.get('mac')}
        shared = set()
        for other_id in self.machines:
            if other_id != machine_id:
                shared |= macsOf(other_id)
        return macsOf(machine_id) - shared

    def candidates(self, machine_id: str, local_networks: list,
                   neighbours: dict, exclude=()) -> list:
        '''[(address, source)] to try for a machine, best first: its MAC
        seen right now, then addresses it had on a network this laptop is
        on (Ethernet before Wi-Fi, most recently working first), then
        addresses that worked before from elsewhere.'''
        known = self.machines.get(machine_id, {})
        macs = self.macs(machine_id)

        def isOnLink(address):
            ip = ipaddress.ip_address(address)
            return any(ip in network for network in local_networks)

        ranked = []
        for address, mac in neighbours.items():
            if mac in macs and isUsable(address):
                ranked.append(((0 if isOnLink(address) else 2, 0, 0),
                               address, 'neighbour'))
        for address, record in known.items():
            on_link = isOnLink(address)
            if not on_link and not record.get('ok_s'):
                continue
            ranked.append(((1 if on_link else 3,
                            0 if record['kind'] == 'ethernet' else 1,
                            -(record.get('ok_s') or record['seen_s'])),
                           address, 'learned'))
        ranked.sort(key=lambda item: item[0])
        found = []
        seen = set(exclude)
        for _, address, source in ranked:
            if address not in seen:
                seen.add(address)
                found.append((address, source))
        return found[:MAX_FOUND_ROUTES]

    def snapshot(self, machine_id: str) -> list:
        '''The machine's known addresses, for display.'''
        current = self.current.get(machine_id, set())
        entries = [dict(record, address=address,
                        is_current=address in current)
                   for address, record
                   in self.machines.get(machine_id, {}).items()]
        entries.sort(key=lambda entry: (not entry['is_current'],
                                        entry['kind'] != 'ethernet',
                                        -entry['seen_s']))
        return entries



# --- Finding machines at run time -------------------------------------------



NEIGHBOUR_CACHE_S = 10.0
KNOWN_HOST_CACHE_S = 60.0
DISCOVERY_PERIOD_S = 120.0
MAX_SCAN_NETWORKS = 4
SCAN_PREFIX = 24


def networksToScan(local_interfaces: list, learned: list) -> list:
    '''The networks a discovery scan covers: each network this laptop is on,
    or for big ones (a /16 test bench) just the /24 around this laptop and
    the /24s where missing machines were last seen.'''
    networks = []

    def add(network):
        if network not in networks and len(networks) < MAX_SCAN_NETWORKS:
            networks.append(network)

    for interface in local_interfaces:
        if interface.network.prefixlen >= SCAN_PREFIX:
            add(interface.network)
            continue
        add(ipaddress.ip_interface('%s/%d' % (interface.ip, SCAN_PREFIX))
            .network)
        for address in learned:
            ip = ipaddress.ip_address(address)
            if ip in interface.network:
                add(ipaddress.ip_interface('%s/%d' % (ip, SCAN_PREFIX))
                    .network)
    return networks


class Finder:
    '''Routes found at run time for machines whose inventory routes don't
    answer, using the address book and the local agent's view of the
    network.'''

    def __init__(self, orca, book: Optional[AddressBook] = None):
        self.orca = orca
        self.book = book or AddressBook()
        self.neighbours = {}
        self.neighbours_s = 0.0
        self.known_hosts = {}  # name -> (checked at, is known)
        self.discovery_task = None
        self.last_discovery_s = 0.0

    def localLink(self):
        return self.orca.links_by_id.get('local')

    def localInterfaces(self) -> list:
        link = self.localLink()
        stats = (link.state['stats'] if link else None) or {}
        found = []
        for interface in stats.get('interfaces', []):
            if interface.get('kind') not in LEARNED_KINDS:
                continue
            for cidr in interface.get('ipv4') or []:
                try:
                    found.append(ipaddress.ip_interface(cidr))
                except ValueError:
                    continue
        return found

    # --- Learning (from machine links) ---

    def onStats(self, machine_id: str, stats: dict, route):
        '''A machine's status report, with the route it's connected on.'''
        is_new = self.book.learnInterfaces(machine_id,
                                           stats.get('interfaces', []))
        if route is not None and not route.via:
            is_new = self.book.learnConnected(machine_id, route.host) \
                or is_new
        if is_new:
            self.book.saveSoon(asyncio.get_running_loop())

    # --- Routes ---

    async def foundRoutes(self, machine) -> list:
        identity = machine.identity()
        if not identity or not self.book.machines.get(machine.id):
            return []
        template = next((route for route in machine.routes
                         if not route.via), None)
        port = template.port if template else None
        # Only addresses that will prove they're this machine: its host key
        # must already be trusted under its identity name.
        if not await self.isKnownHost(identity, port):
            return []
        await self.refreshNeighbours()
        user = machine.user or (template.user if template else None)
        exclude = {route.host for route in machine.routes if not route.via}
        candidates = self.book.candidates(
            machine.id, [interface.network
                         for interface in self.localInterfaces()],
            self.neighbours, exclude)
        return [Route(host=address, user=user, port=port,
                      host_key_alias=identity, source=source)
                for address, source in candidates]

    async def refreshNeighbours(self, force: bool = False):
        link = self.localLink()
        if link is None or link.connection is None:
            return
        if not force and time.time() - self.neighbours_s < NEIGHBOUR_CACHE_S:
            return
        try:
            neighbours = await link.call('net.neighbours', timeout_s=5)
        except Exception:  # an old agent, or the local link restarting
            return
        # Devices that got an address from this machine's internet sharing.
        neighbours.update(self.orca.sharing.leaseNeighbours())
        self.neighbours = neighbours
        self.neighbours_s = time.time()

    async def isKnownHost(self, name: str, port: Optional[int]) -> bool:
        if port and port != 22:
            name = '[%s]:%d' % (name, port)
        checked = self.known_hosts.get(name)
        if checked and time.time() - checked[0] < KNOWN_HOST_CACHE_S:
            return checked[1]
        process = await asyncio.create_subprocess_exec(
            'ssh-keygen', '-F', name, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        is_known = await process.wait() == 0
        self.known_hosts[name] = (time.time(), is_known)
        return is_known

    def forgetKnownHosts(self):
        '''After a host key is trusted.'''
        self.known_hosts.clear()

    # --- Discovery ---

    def requestDiscovery(self):
        '''Scan nearby networks for machines that can't be found, at most
        every DISCOVERY_PERIOD_S. Scanning fills the neighbour table, so
        their MACs show up at their new addresses.'''
        if self.discovery_task is not None and not self.discovery_task.done():
            return
        if time.time() - self.last_discovery_s < DISCOVERY_PERIOD_S:
            return
        self.last_discovery_s = time.time()
        self.discovery_task = asyncio.ensure_future(self.discover())

    def missingLinks(self) -> list:
        return [link for link in self.orca.links
                if not link.machine.is_local and link.connection is None
                and self.book.macs(link.machine.id)]

    async def discover(self):
        link = self.localLink()
        missing = self.missingLinks()
        if link is None or link.connection is None or not missing:
            return
        learned = [address for missing_link in missing
                   for address in self.book.machines.get(
                       missing_link.machine.id, {})]
        for network in networksToScan(self.localInterfaces(), learned):
            try:
                await link.call('net.scan', {'cidr': str(network),
                                             'ports': [22],
                                             'timeout_s': 0.6},
                                timeout_s=60)
            except Exception:
                continue
        await self.refreshNeighbours(force=True)
        for missing_link in self.missingLinks():
            macs = self.book.macs(missing_link.machine.id)
            if any(mac in macs for mac in self.neighbours.values()):
                missing_link.requestReconnect()
