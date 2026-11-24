'''
Machine inventory: which machines the hub manages and how to reach them.

Two files are merged, the second overriding entries of the first by id:
    <workspace>/src/project/hub/machines.json   the team's machines (versioned
                                                with the code they run)
    ~/.config/csm-hub/machines.json             this user's own machines,
                                                e.g. a personal test rig
The hub's own machine is always present as "local".

File format:
    {
        "machines": [
            {
                "id": "robot-vm",
                "label": "Robot VM",
                "role": "Runs the robot code",
                "host": "robot-mac",           // machine this VM runs on
                "workspace": "~/lance-ws",     // colcon workspace (optional)
                "routes": [                    // tried in order
                    {"host": "10.11.11.15", "user": "csm"},
                    {"host": "192.168.18.2", "user": "csm",
                     "via": "robot-mac", "host_key_alias": "10.11.11.15"}
                ]
            },
            {
                "id": "robot-mac",
                "label": "Robot mac host",
                "routes": [{"host": "10.11.11.60", "user": "csm"}],
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


PROJECT_INVENTORY_PATH = os.path.join('src', 'project', 'hub', 'machines.json')
USER_INVENTORY_PATH = os.path.expanduser('~/.config/csm-hub/machines.json')
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

    def target(self) -> str:
        return self.user + '@' + self.host if self.user else self.host

    def describe(self) -> str:
        if self.via:
            return 'via ' + self.via + ' · ' + self.host
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
        }



# --- Loading ------------------------------------------------------------------



def parseRoute(entry: dict, machine_id: str) -> Route:
    if 'host' not in entry:
        raise InventoryError(
            "machine '%s': every route needs a 'host'" % machine_id)
    return Route(host=entry['host'], user=entry.get('user'),
                 port=entry.get('port'), via=entry.get('via'),
                 host_key_alias=entry.get('host_key_alias'))


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
        routes=[parseRoute(route, machine_id)
                for route in entry.get('routes', [])],
        workspace=entry.get('workspace'),
        host=entry.get('host'),
        vm=VmConfig(csm_vm_path=vm['csm_vm_path'], guest=vm.get('guest'))
        if vm else None,
        source=source,
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
                   role='This machine (runs the hub)', is_local=True,
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
