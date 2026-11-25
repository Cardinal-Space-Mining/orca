'''
Adding a machine to the inventory from the app: find it (a network scan,
the devices on a shared port, or an address), check its host key, install
this laptop's SSH key on it if needed, connect once to learn what it is, and
propose an inventory entry to review.
'''

from __future__ import annotations

import ipaddress
import re
import shlex
from typing import Optional

from .inventory import (Machine, Route, inventoryScopes, readInventoryRaw)
from .link import MachineLink
from .runs import IGNORED_INTERFACE_PREFIXES, PlanError
from .ssh import (keyData, knownKeys, scanHostKeys, sshBaseCommand,
                  trustHostKeys)


ADDRESS_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.:-]*$')
USER_PATTERN = re.compile(r'^[a-z_][a-z0-9_.-]*$')
ID_INVALID = re.compile(r'[^a-z0-9-]+')
SCAN_KINDS = ('ethernet', 'wifi', 'bridge')
ROUTE_KINDS = ('ethernet', 'wifi')
MAX_SCAN_PREFIX = 22  # scans cover at most a /22


class ProbeLink(MachineLink):
    '''A one-off connection to a machine that isn't in the inventory yet:
    the same connecting and agent installing as a real link, but nothing is
    published.'''

    def publish(self):
        pass


def suggestId(hostname: Optional[str], taken: set) -> str:
    base = ID_INVALID.sub('-', (hostname or 'machine').split('.')[0]
                          .lower()).strip('-') or 'machine'
    candidate = base
    number = 2
    while candidate in taken:
        candidate = '%s-%d' % (base, number)
        number += 1
    return candidate


def scanNetworks(interfaces: list) -> list:
    '''Networks worth scanning from a machine: each one it's on (the /24
    around it for bigger ones).'''
    networks = []
    ordered = sorted((interface for interface in interfaces
                      if interface.get('kind') in SCAN_KINDS
                      and not interface['name'].startswith(
                          IGNORED_INTERFACE_PREFIXES)),
                     key=lambda interface: SCAN_KINDS.index(interface['kind']))
    for interface in ordered:
        for cidr in interface.get('ipv4') or []:
            try:
                address = ipaddress.ip_interface(cidr)
            except ValueError:
                continue
            if address.ip.is_loopback or address.ip.is_link_local:
                continue
            prefix = max(address.network.prefixlen, 24) \
                if address.network.prefixlen < MAX_SCAN_PREFIX \
                else address.network.prefixlen
            network = ipaddress.ip_interface(
                '%s/%d' % (address.ip, prefix)).network
            if all(entry['cidr'] != str(network) for entry in networks):
                networks.append({'cidr': str(network),
                                 'interface': interface['name'],
                                 'kind': interface['kind']})
    return networks


def routeAddresses(interfaces: list, used: str, local_networks: list) -> list:
    '''A new machine's addresses to list as routes: the one that worked
    first, then its other Ethernet and Wi-Fi addresses on networks this
    machine is on too (Ethernet first). Addresses elsewhere, e.g. a VM's
    host-only link, can't be reached directly; the address book learns
    the rest at run time.'''
    found = []
    for interface in interfaces:
        if interface.get('kind') not in ROUTE_KINDS:
            continue
        for cidr in interface.get('ipv4') or []:
            address = cidr.split('/')[0]
            ip = ipaddress.ip_address(address)
            if address != used and not ip.is_link_local \
                    and any(ip in network for network in local_networks):
                found.append((interface['kind'] != 'ethernet', address))
    return [used] + [address for _, address in sorted(found)]


def configAddresses(interfaces: list, constants: dict, taken: set) -> dict:
    '''lance config constants whose value is one of this machine's
    addresses right now: {constant: kind}.'''
    kinds = {}
    for interface in interfaces:
        if interface.get('kind') in ROUTE_KINDS:
            for cidr in interface.get('ipv4') or []:
                kinds[cidr.split('/')[0]] = interface['kind']
    return {name: kinds[value] for name, value in sorted(constants.items())
            if value in kinds and name not in taken}


class Discovery:

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    # --- Finding ---

    def knownMachines(self) -> tuple:
        '''({mac: machine id}, {address: machine id}) for everything the
        orchestrator already knows.'''
        book = self.orchestrator.finder.book
        by_mac = {mac: machine_id for machine_id in book.machines
                  for mac in book.macs(machine_id)}
        by_address = {address: machine_id for machine_id, addresses
                      in book.machines.items() for address in addresses}
        for link in self.orchestrator.links:
            for interface in (link.state['stats'] or {}).get('interfaces',
                                                             []):
                for cidr in interface.get('ipv4') or []:
                    by_address[cidr.split('/')[0]] = link.machine.id
                if interface.get('mac') and link.machine.is_local:
                    by_mac.setdefault(interface['mac'], link.machine.id)
        return by_mac, by_address

    def networks(self, machine_id: str) -> list:
        link = self.orchestrator.link(machine_id)
        return scanNetworks((link.state['stats'] or {}).get('interfaces', []))

    async def scan(self, machine_id: str, cidr: str) -> dict:
        link = self.orchestrator.link(machine_id)
        result = await link.call('net.scan', {
            'cidr': cidr, 'ports': [22], 'timeout_s': 0.8}, timeout_s=90)
        self.orchestrator.noteScan(machine_id, result)
        by_mac, by_address = self.knownMachines()
        local_networks = [interface.network for interface
                          in self.orchestrator.finder.localInterfaces()]
        for host in result['hosts']:
            host['machine_id'] = by_mac.get(host.get('mac')) \
                or by_address.get(host['address'])
            # Reached through the scanning machine unless this one is on the
            # same network (e.g. a VM on its mac's host-only link).
            ip = ipaddress.ip_address(host['address'])
            host['via'] = None if link.machine.is_local or any(
                ip in network for network in local_networks) else machine_id
        return result

    # --- Reaching it ---

    def route(self, request: dict) -> Route:
        address = (request.get('address') or '').strip()
        if not ADDRESS_PATTERN.match(address):
            raise PlanError('enter an address or host name')
        user = (request.get('user') or '').strip() or None
        if user and not USER_PATTERN.match(user):
            raise PlanError('"%s" is not a user name' % user)
        via = request.get('via') or None
        if via:
            jump = self.orchestrator.link(via)
            if jump.connection is None:
                raise PlanError('%s must be online to reach machines '
                                'behind it' % jump.machine.label)
        port = int(request['port']) if request.get('port') else None
        alias = (request.get('host_key_alias') or '').strip() or None
        return Route(host=address, user=user, port=port, via=via,
                     host_key_alias=alias)

    async def hostKey(self, request: dict) -> dict:
        '''The machine's host keys, whether they're already trusted under its
        key name, and whether another machine already uses the same key
        (cloned VMs keep their original's keys).'''
        route = self.route(request)
        scanned = await scanHostKeys(route, self.orchestrator.jumpRouteFor)
        offered = {keyData(key) for key in scanned['keys']}
        trusted = await knownKeys(scanned['name'])
        if offered & trusted:
            status = 'trusted'
        elif trusted:
            status = 'different'
        else:
            status = 'unknown'
        same_key = []
        for link in self.orchestrator.links:
            identity = link.machine.identity()
            if identity and identity != scanned['name'] \
                    and offered & await knownKeys(identity):
                same_key.append(link.machine.id)
        return dict(scanned, status=status, same_key_as=same_key)

    async def trustKey(self, request: dict) -> dict:
        scanned = await self.hostKey(request)
        if sorted(scanned['fingerprints']) != sorted(
                request.get('fingerprints') or []):
            raise PlanError('the host keys changed since they were shown')
        if scanned['status'] == 'different':
            raise PlanError(
                'another key is already trusted under %s: remove it from '
                '~/.ssh/known_hosts first if the machine was reinstalled'
                % scanned['name'])
        if scanned['status'] == 'unknown':
            trustHostKeys(scanned['name'], scanned['keys'])
            self.orchestrator.finder.forgetKnownHosts()
        return {'ok': True}

    def keyCommand(self, request: dict) -> list:
        '''Install this laptop's SSH key on the machine (ssh-copy-id asks for
        the account's password in a terminal), creating a key first if there
        is none.'''
        route = self.route(request)
        # ssh's options without "ssh -T", as option/value pairs, minus
        # BatchMode: ssh-copy-id has to ask for the password.
        arguments = sshBaseCommand(route, self.orchestrator.jumpRouteFor)[2:]
        options = []
        for option, value in zip(arguments[::2], arguments[1::2]):
            if value != 'BatchMode=yes':
                options += [option, value]
        copy = ['ssh-copy-id'] + options + [route.target()]
        script = (
            'set -e\n'
            'if ! ls ~/.ssh/id_*.pub >/dev/null 2>&1; then\n'
            '  echo "This laptop has no SSH key yet: creating one '
            '(Enter accepts the defaults)."\n'
            '  ssh-keygen -t ed25519\n'
            'fi\n'
            'echo "Copying your SSH key to %s. Type its password when '
            'asked."\n'
            'exec %s\n') % (route.target(), shlex.join(copy))
        return ['/bin/bash', '-c', script]

    # --- Connecting once ---

    async def probe(self, request: dict) -> dict:
        '''Connect with the agent and learn what the machine is. Returns
        {"state": "ready", ...} or {"state": "needs_key" | "host_key" |
        "unreachable", "error"}.'''
        route = self.route(request)
        machine = Machine(id='new:' + route.host, label=route.host,
                          routes=[route])
        link = ProbeLink(self.orchestrator, machine)
        winner = await link.firstReachable([route])
        if winner is None:
            details = link.state['connection'].get('error_details') or ''
            if 'Permission denied' in details:
                state = 'needs_key'
            elif 'Host key verification failed' in details \
                    or 'REMOTE HOST IDENTIFICATION' in details:
                state = 'host_key'
            else:
                state = 'unreachable'
            return {'state': state,
                    'error': link.state['connection'].get('error')
                    or details}
        connection = await link.open(*winner)
        if connection is None:
            return {'state': 'unreachable',
                    'error': link.state['connection'].get('error')}
        link.connection = connection
        try:
            info = await link.call('system.info')
            stats = await link.call('system.stats')
            try:
                survey = await link.call('machine.survey', timeout_s=30)
            except Exception:
                survey = {}
        finally:
            await connection.close()
        interfaces = [{key: interface.get(key) for key in
                       ('name', 'kind', 'mac', 'ipv4', 'is_up')}
                      for interface in stats.get('interfaces', [])]
        return {'state': 'ready', 'info': info, 'interfaces': interfaces,
                'survey': survey,
                'suggestion': self.suggestEntry(request, route, info,
                                                interfaces, survey)}

    def suggestEntry(self, request, route, info, interfaces, survey) -> dict:
        taken = set(self.orchestrator.links_by_id)
        local_networks = [interface.network for interface
                          in self.orchestrator.finder.localInterfaces()]
        machine_id = request.get('id') or suggestId(info.get('hostname'),
                                                    taken)
        entry = {
            'id': machine_id,
            'label': request.get('label') or (info.get('hostname') or
                                              machine_id).split('.')[0],
            'role': '',
            'user': route.user,
            'host_key_alias': route.host_key_alias
            or machine_id + '.orchestrator',
        }
        if route.via:
            # A VM behind its host: its own network first, the jump last.
            entry['host'] = route.via
            entry['routes'] = [{'host': address} for address in
                               routeAddresses(interfaces, route.host,
                                              local_networks)[1:]]
            entry['routes'].append({'host': route.host, 'via': route.via})
        else:
            entry['routes'] = [{'host': address} for address in
                               routeAddresses(interfaces, route.host,
                                              local_networks)]
        if route.port:
            for entry_route in entry['routes']:
                entry_route['port'] = route.port
        workspaces = survey.get('workspaces') or []
        if workspaces:
            entry['workspace'] = next((path for path in workspaces
                                       if 'lance' in path), workspaces[0])
        csm_vm = survey.get('csm_vm') or []
        if csm_vm and info.get('platform') == 'macos':
            entry['vm'] = {'csm_vm_path': csm_vm[0]['path']}
        config = self.orchestrator.lanceConfig()
        if config is not None:
            mapped = {name for link in self.orchestrator.links
                      for name in link.machine.config_addresses}
            mapping = configAddresses(interfaces, config.addressConstants(),
                                      mapped)
            if mapping:
                entry['config_addresses'] = mapping
        return entry

    # --- Saving ---

    async def add(self, scope: str, entry: dict) -> dict:
        if not entry.get('id'):
            raise PlanError('the machine needs an id')
        if entry['id'] in self.orchestrator.links_by_id:
            raise PlanError('there is already a machine "%s"' % entry['id'])
        scopes = inventoryScopes(self.orchestrator.workspace_root)
        if scope not in scopes:
            raise PlanError('no inventory "%s"' % scope)
        entries = readInventoryRaw(scopes[scope])['machines'] + [entry]
        if entry.get('host'):
            # A VM: tell its host about it.
            for host_entry in entries:
                if host_entry.get('id') == entry['host'] \
                        and host_entry.get('vm') \
                        and not host_entry['vm'].get('guest'):
                    host_entry['vm']['guest'] = entry['id']
        return await self.orchestrator.saveInventory(scope, entries)

