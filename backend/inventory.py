'''
Machine inventory: which machines Orca manages and how to find
them.

Two files are merged, the second overriding entries of the first by id:
    <workspace>/src/project/orca/machines.json
            the team's machines (versioned with the code they run)
    ~/.config/orca/machines.json
            this user's own machines, e.g. a personal test rig
Orca's own machine is always present as "local".

Addresses change (DHCP), so the inventory says how to find a machine rather
than where it is: names and addresses to try first, and the SSH host key that
proves it's the right machine. Addresses the machine reports for itself are
remembered separately (addresses.py) and tried as well.

File format:
    {
        "machines": [
            {
                "id": "robot-vm",
                "label": "Robot VM",
                "role": "Runs the robot code",
                "host": "robot-mac",           // machine this VM runs on
                "workspace": "~/lance-ws",     // colcon workspace (optional)
                "user": "lance",               // default for every route
                // The name its host key is checked under, for every address
                // (default: the first route's host).
                "host_key_alias": "robot-vm.orca",
                "routes": [                    // tried first, in order
                    {"host": "lance-vm.local"},
                    {"host": "192.168.18.2", "via": "robot-mac"}
                ],
                // lance config constants that hold this machine's address:
                // launches get its current address of that interface kind.
                "config_addresses": {
                    "ip.net1.mac_vm_eth": "ethernet",
                    "ip.net1.mac_vm_wifi": "wifi"
                },
                // Extra setup checklist items: "can" (robot computer),
                // "sharing" (shares its internet, e.g. a driver station).
                "setup": ["can"]
            },
            {
                "id": "robot-mac",
                "label": "Robot mac host",
                "routes": [{"host": "10.11.11.60", "user": "lance"}],
                "vm": {"csm_vm_path": "~/code/csm-vm", "guest": "robot-vm"}
            }
        ]
    }
'''

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from typing import Optional


PROJECT_INVENTORY_PATH = os.path.join('src', 'project', 'orca',
                                      'machines.json')
USER_INVENTORY_PATH = os.path.expanduser(
    '~/.config/orca/machines.json')
LOCAL_MACHINE_ID = 'local'


class InventoryError(Exception):
    pass



# --- Model --------------------------------------------------------------------



@dataclass
class Route:
    '''One way to reach a machine over SSH.'''

    host: str
    user: Optional[str] = None
    port: Optional[int] = None
    via: Optional[str] = None  # id of a machine to jump through
    # Check the host key under this name; lets a second address of the same
    # machine reuse its known_hosts entry.
    host_key_alias: Optional[str] = None
    # Where the route came from: "inventory", or found at run time
    # ("learned": an address the machine reported; "neighbour": its MAC
    # seen at this address).
    source: str = 'inventory'

    def target(self) -> str:
        return self.user + '@' + self.host if self.user else self.host

    def describe(self) -> str:
        if self.via:
            return 'via ' + self.via + ' · ' + self.host
        if self.source != 'inventory':
            return self.source + ' · ' + self.host
        return 'direct · ' + self.host


@dataclass
class VmConfig:
    csm_vm_path: str
    guest: Optional[str] = None  # id of the VM's own machine entry


@dataclass
class Machine:
    id: str
    label: str
    role: str = ''
    routes: list = field(default_factory=list)  # [Route]
    is_local: bool = False
    workspace: Optional[str] = None
    host: Optional[str] = None  # id of the machine this one runs on (VMs)
    vm: Optional[VmConfig] = None
    source: str = ''  # inventory file that defined it
    user: Optional[str] = None  # default user for routes found at run time
    host_key_alias: Optional[str] = None
    # lance config constant ("ip.net1.mac_vm_eth") -> interface kind
    config_addresses: dict = field(default_factory=dict)
    # What else it needs set up (setup.py): "can", "sharing"
    setup: list = field(default_factory=list)
    icon: Optional[str] = None  # map icon, when the guess is wrong

    def identity(self) -> Optional[str]:
        '''The known_hosts name that proves a connection reached this
        machine, whatever address it used.'''
        if self.host_key_alias:
            return self.host_key_alias
        for route in self.routes:
            if not route.via:
                return route.host_key_alias or route.host
        return None

    def toJson(self) -> dict:
        return {
            'id': self.id,
            'label': self.label,
            'role': self.role,
            'is_local': self.is_local,
            'workspace': self.workspace,
            'host': self.host,
            'vm': {'csm_vm_path': self.vm.csm_vm_path,
                   'guest': self.vm.guest} if self.vm else None,
            'routes': [route.describe() for route in self.routes],
            'source': self.source,
            'config_addresses': self.config_addresses,
            'setup': self.setup,
            'icon': self.icon,
        }



# --- Loading ------------------------------------------------------------------



CONFIG_ADDRESS_KINDS = ('ethernet', 'wifi')
SETUP_TAGS = ('can', 'sharing')


def parseRoute(entry: dict, machine_id: str, defaults: dict) -> Route:
    if 'host' not in entry:
        raise InventoryError(
            "machine '%s': every route needs a 'host'" % machine_id)
    return Route(host=entry['host'],
                 user=entry.get('user') or defaults.get('user'),
                 port=entry.get('port'), via=entry.get('via'),
                 host_key_alias=entry.get('host_key_alias')
                 or defaults.get('host_key_alias'))


def parseSetup(entry: dict, machine_id: str) -> list:
    tags = entry.get('setup') or []
    unknown = [tag for tag in tags if tag not in SETUP_TAGS]
    if unknown:
        raise InventoryError("machine '%s': unknown setup %s (known: %s)"
                             % (machine_id, ', '.join(unknown),
                                ', '.join(SETUP_TAGS)))
    return list(tags)


def parseConfigAddresses(entry: dict, machine_id: str) -> dict:
    mapping = entry.get('config_addresses') or {}
    for constant, kind in mapping.items():
        if kind not in CONFIG_ADDRESS_KINDS:
            raise InventoryError(
                "machine '%s': config address %s must be one of %s"
                % (machine_id, constant, ', '.join(CONFIG_ADDRESS_KINDS)))
    return dict(mapping)


def parseMachine(entry: dict, source: str) -> Machine:
    machine_id = entry.get('id')
    if not machine_id or machine_id == LOCAL_MACHINE_ID:
        raise InventoryError(
            "%s: each machine needs an 'id' (and '%s' is reserved)"
            % (source, LOCAL_MACHINE_ID))
    vm = entry.get('vm')
    return Machine(
        id=machine_id,
        label=entry.get('label', machine_id),
        role=entry.get('role', ''),
        routes=[parseRoute(route, machine_id, entry)
                for route in entry.get('routes', [])],
        workspace=entry.get('workspace'),
        host=entry.get('host'),
        vm=VmConfig(csm_vm_path=vm['csm_vm_path'], guest=vm.get('guest'))
        if vm else None,
        source=source,
        user=entry.get('user'),
        host_key_alias=entry.get('host_key_alias'),
        config_addresses=parseConfigAddresses(entry, machine_id),
        setup=parseSetup(entry, machine_id),
        icon=entry.get('icon'),
    )


def readInventoryFile(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as file:
            data = json.load(file)
    except ValueError as error:
        raise InventoryError('%s is not valid JSON: %s' % (path, error))
    return [parseMachine(entry, path) for entry in data.get('machines', [])]


def localMachine(workspace_root: Optional[str]) -> Machine:
    return Machine(id=LOCAL_MACHINE_ID,
                   label=socket.gethostname().split('.')[0],
                   role='This machine (runs Orca)', is_local=True,
                   workspace=workspace_root, source='built in')


def checkReferences(machines: dict) -> None:
    for machine in machines.values():
        for route in machine.routes:
            if route.via and not machines.get(route.via, Machine('', '')).routes:
                raise InventoryError(
                    "machine '%s': route via '%s', which isn't a machine with "
                    "routes" % (machine.id, route.via))
        if not machine.is_local and not machine.routes:
            raise InventoryError(
                "machine '%s' has no routes" % machine.id)


def inventoryPaths(workspace_root: Optional[str]) -> list:
    paths = []
    if workspace_root:
        paths.append(os.path.join(workspace_root, PROJECT_INVENTORY_PATH))
    paths.append(USER_INVENTORY_PATH)
    return paths


def loadInventory(workspace_root: Optional[str]) -> list:
    '''Return the machines in display order: this machine first, then the
    inventory files' machines in file order.'''
    machines = {LOCAL_MACHINE_ID: localMachine(workspace_root)}
    for path in inventoryPaths(workspace_root):
        for machine in readInventoryFile(path):
            machines[machine.id] = machine
    checkReferences(machines)
    return list(machines.values())



# --- Editing ------------------------------------------------------------------



def inventoryScopes(workspace_root: Optional[str]) -> dict:
    '''The editable files: "project" (the team's, in the workspace) and
    "user" (this user's own).'''
    scopes = {'user': USER_INVENTORY_PATH}
    if workspace_root:
        scopes['project'] = os.path.join(workspace_root,
                                         PROJECT_INVENTORY_PATH)
    return scopes


def readInventoryRaw(path: str) -> dict:
    '''A file's machines as written, unknown keys included, for editing.'''
    if not os.path.isfile(path):
        return {'path': path, 'exists': False, 'machines': []}
    with open(path) as file:
        data = json.load(file)
    return {'path': path, 'exists': True,
            'machines': data.get('machines', [])}


def validateEdit(workspace_root: Optional[str], scope: str,
                 entries: list) -> None:
    '''Check a new version of one file against the others, as loadInventory
    would merge them. Raises InventoryError.'''
    scopes = inventoryScopes(workspace_root)
    if scope not in scopes:
        raise InventoryError('no inventory file "%s"' % scope)
    ids = [entry.get('id') for entry in entries]
    duplicates = sorted({machine_id for machine_id in ids
                         if ids.count(machine_id) > 1})
    if duplicates:
        raise InventoryError('duplicate machine ids: ' + ', '.join(duplicates))
    machines = {LOCAL_MACHINE_ID: localMachine(workspace_root)}
    for path in inventoryPaths(workspace_root):
        if path == scopes[scope]:
            parsed = [parseMachine(entry, path) for entry in entries]
        else:
            parsed = readInventoryFile(path)
        for machine in parsed:
            machines[machine.id] = machine
    checkReferences(machines)


def writeInventory(workspace_root: Optional[str], scope: str,
                   entries: list) -> str:
    validateEdit(workspace_root, scope, entries)
    path = inventoryScopes(workspace_root)[scope]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = path + '.tmp'
    with open(temporary_path, 'w') as file:
        json.dump({'machines': entries}, file, indent=4)
        file.write('\n')
    os.replace(temporary_path, path)
    return path
