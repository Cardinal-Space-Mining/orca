'''
Planning and running scenarios.

A plan turns a scenario plus choices (which machine fills each role, option
values, address overrides, extra arguments) into exact commands per machine.
The launch page shows it before anything starts, and a Run executes it:
processes start in dependency order, and if one fails to start, the ones
already started are stopped again so nothing is left half up.

Processes keep their run information in the agent (the "meta" of each
process), so a restarted hub finds its runs again.
'''

from __future__ import annotations

import asyncio
import ipaddress
import os
import posixpath
import random
import shlex
import time
from typing import Optional

from .lance_config import (INSTALLED_CONFIG_DIRECTORY, ConfigError,
                           LanceConfig, overlayJson)
from .link import AgentError
from .scenarios import ScenarioError, fillTemplate, placeholdersIn


DEFAULT_START_DELAY_S = 1.0
DEPENDENCY_TIMEOUT_S = 60.0
STOP_SPACING_S = 0.3
# Interfaces that never carry robot traffic.
IGNORED_INTERFACE_PREFIXES = ('docker', 'br-', 'veth', 'virbr', 'lo')
KIND_RANK = {'ethernet': 0, 'wifi': 1, 'bridge': 2, 'can': 9, 'virtual': 9}


class PlanError(Exception):
    pass



# --- Addresses ----------------------------------------------------------------



def interfaceAddresses(stats: Optional[dict]) -> list:
    '''[(ipv4 interface, interface entry)] usable for robot traffic.'''
    found = []
    for entry in (stats or {}).get('interfaces', []):
        if entry['name'].startswith(IGNORED_INTERFACE_PREFIXES):
            continue
        for cidr in entry.get('ipv4', []):
            try:
                found.append((ipaddress.ip_interface(cidr), entry))
            except ValueError:
                continue
    return found


def addressCandidates(target_link, from_link) -> list:
    '''Addresses of target_link's machine that from_link's machine can reach
    directly (same subnet), best first: the default-route interface, then
    Ethernet before Wi-Fi.'''
    if target_link is from_link:
        return [{'address': '127.0.0.1', 'interface': 'lo', 'kind': 'loopback',
                 'reason': 'same machine'}]
    target_stats = target_link.state['stats']
    from_networks = [interface.network for interface, _
                     in interfaceAddresses(from_link.state['stats'])]
    default_route = (target_stats or {}).get('default_route') or {}
    candidates = []
    for interface, entry in interfaceAddresses(target_stats):
        if not any(interface.ip in network for network in from_networks):
            continue
        is_default = entry['name'] == default_route.get('interface')
        candidates.append({
            'address': str(interface.ip),
            'interface': entry['name'],
            'kind': entry['kind'],
            'reason': 'default route' if is_default else entry['kind'],
            'rank': (0 if is_default else 1, KIND_RANK.get(entry['kind'], 5)),
        })
    candidates.sort(key=lambda candidate: candidate.pop('rank'))
    return candidates



# --- Planning -----------------------------------------------------------------



def expandHome(path: str, info: Optional[dict]) -> str:
    if path.startswith('~') and info and info.get('home'):
        return info['home'] + path[1:]
    return path


def dependencyOrder(processes: list) -> list:
    ordered = []
    placed = set()
    remaining = list(processes)
    while remaining:
        ready = [process for process in remaining
                 if set(process.get('after') or []) <= placed]
        if not ready:
            raise PlanError('processes wait for each other in a cycle')
        for process in ready:
            ordered.append(process)
            placed.add(process['key'])
            remaining.remove(process)
    return ordered


def parseExtraArguments(text: str) -> dict:
    '''"foxglove_bridge:=null redux:=client_mac_eth" -> dict.'''
    arguments = {}
    for token in shlex.split(text or ''):
        if ':=' not in token:
            raise PlanError("extra argument '%s' isn't key:=value" % token)
        key, value = token.split(':=', 1)
        arguments[key] = value
    return arguments


class Planner:

    def __init__(self, hub):
        self.hub = hub

    def plan(self, scenario: dict, request: dict) -> dict:
        '''request: {"assignment": {role: machine id},
                     "options": {name: value},
                     "addresses": {"<process key>.<role>": address},
                     "extra_arguments": {process key: "k:=v ..."}}'''
        assignment = self.assignRoles(scenario, request.get('assignment') or {})
        options = self.chooseOptions(scenario, request.get('options') or {})
        plan = {'scenario_id': scenario['id'], 'label': scenario.get('label'),
                'assignment': assignment, 'options': options,
                'processes': [], 'errors': [], 'warnings': []}
        for role, machine_id in assignment.items():
            self.checkMachine(scenario, role, machine_id, plan)
        if plan['errors']:
            return plan
        self.checkSameCode(scenario, assignment, plan)
        try:
            for process in dependencyOrder(scenario.get('processes') or []):
                plan['processes'].append(
                    self.planProcess(process, assignment, options, request))
        except (PlanError, ScenarioError, ConfigError) as error:
            plan['errors'].append(str(error))
        return plan

    # --- Inputs ---

    def assignRoles(self, scenario, requested) -> dict:
        assignment = {}
        for role, definition in scenario['roles'].items():
            assignment[role] = requested.get(role) \
                or definition.get('default') or 'local'
        return assignment

    def chooseOptions(self, scenario, requested) -> dict:
        options = {}
        for name, definition in scenario['options'].items():
            value = requested.get(name, definition.get('default', ''))
            choices = definition.get('choices')
            if choices and value not in choices:
                raise PlanError('option %s: %s is not one of %s'
                                % (name, value, ', '.join(choices)))
            options[name] = value
        return options

    def checkMachine(self, scenario, role, machine_id, plan):
        link = self.hub.links_by_id.get(machine_id)
        label = scenario['roles'][role].get('label', role)
        if link is None:
            plan['errors'].append('%s: no machine %s' % (label, machine_id))
            return
        if link.state['connection']['state'] != 'online':
            plan['errors'].append('%s: %s is %s' % (
                label, link.machine.label, link.state['connection']['state']))
            return
        uses_lance = any(process.get('role') == role and 'lance' in process
                         for process in scenario.get('processes') or [])
        if not uses_lance:
            return
        workspace = link.state['workspace']
        if not link.machine.workspace:
            plan['errors'].append('%s: %s has no workspace in the inventory'
                                  % (label, link.machine.label))
        elif workspace and not workspace.get('has_install'):
            plan['errors'].append('%s: %s has no build (install/ missing)'
                                  % (label, link.machine.label))
        reference = self.hub.referenceFingerprint()
        if workspace and reference and not link.machine.is_local \
                and workspace.get('fingerprint') != reference:
            plan['warnings'].append(
                '%s has different code than this machine (%s), which the '
                'preview is based on: presets may differ'
                % (link.machine.label, (workspace.get('head') or '?')[:7]))

    def checkSameCode(self, scenario, assignment, plan):
        '''Robot and client must run identical code: net_adapter's MS136
        codec rejects the other side if its tables differ.'''
        fingerprints = {}
        for role, machine_id in assignment.items():
            if not any(process.get('role') == role and 'lance' in process
                       for process in scenario.get('processes') or []):
                continue
            workspace = self.hub.links_by_id[machine_id].state['workspace']
            if workspace and workspace.get('fingerprint'):
                fingerprints.setdefault(workspace['fingerprint'], []).append(
                    scenario['roles'][role].get('label', role))
        if len(fingerprints) > 1:
            groups = ' vs '.join(' + '.join(labels)
                                 for labels in fingerprints.values())
            plan['warnings'].append(
                'Different code on %s: deploy first so both sides match '
                '(net_adapter needs identical codec tables)' % groups)

    # --- One process ---

    def addressesFor(self, process, assignment, request) -> tuple:
        '''Fill {ip:role} for one process. Returns (addresses, choices).'''
        link = self.hub.links_by_id[assignment[process['role']]]
        overrides = request.get('addresses') or {}
        addresses = {}
        choices = {}
        for role, machine_id in assignment.items():
            candidates = addressCandidates(
                self.hub.links_by_id[machine_id], link)
            choices[role] = candidates
            chosen = overrides.get(process['key'] + '.' + role)
            if chosen:
                addresses[role] = chosen
            elif candidates:
                addresses[role] = candidates[0]['address']
        return addresses, choices

    def planProcess(self, process, assignment, options, request) -> dict:
        role = process['role']
        link = self.hub.links_by_id[assignment[role]]
        addresses, choices = self.addressesFor(process, assignment, request)
        used_roles = sorted({argument for name, argument
                             in placeholdersIn(process) if name == 'ip'})
        for used_role in used_roles:
            if used_role not in addresses:
                raise PlanError(
                    '%s: no network shared by %s and %s; set the address by '
                    'hand' % (process.get('label', process['key']),
                              link.machine.label,
                              self.hub.links_by_id[
                                  assignment[used_role]].machine.label))
        entry = {
            'key': process['key'],
            'label': process.get('label') or process['key'],
            'role': role,
            'machine_id': link.machine.id,
            'machine_label': link.machine.label,
            'after': process.get('after') or [],
            'start_delay_s': process.get('start_delay_s',
                                         DEFAULT_START_DELAY_S),
            'address_roles': used_roles,
            'addresses': {used_role: addresses[used_role]
                          for used_role in used_roles},
            'address_choices': {used_role: choices[used_role]
                                for used_role in used_roles},
            'overlay': None,
            'selected': None,
        }
        extra = (request.get('extra_arguments') or {}).get(process['key'], '')
        if 'lance' in process:
            self.planLance(process['lance'], entry, link, options, addresses,
                           parseExtraArguments(extra))
        else:
            command = fillTemplate(process['command'], options, addresses)
            if extra:
                command += ' ' + extra
            entry['argv'] = ['/bin/sh', '-c', command]
            entry['display'] = command
            entry['cwd'] = link.machine.workspace or '~'
        return entry

    def planLance(self, lance, entry, link, options, addresses, extra):
        config = self.hub.lanceConfig()
        if config is None:
            raise PlanError("the hub has no workspace, so it can't plan "
                            "lance launches")
        arguments = {key: str(value) for key, value in fillTemplate(
            lance.get('args') or {}, options, addresses).items()}
        arguments.update(extra)
        settings = fillTemplate(lance.get('set') or {}, options, addresses)
        tag = entry['key']
        if settings:
            overlay, extra_arguments, resolution = config.overlayFor(
                arguments, settings, tag)
            arguments.update(extra_arguments)
            entry['overlay'] = overlay
        else:
            resolution = config.resolve(arguments)
        entry['selected'] = resolution['selected']
        entry['settings'] = settings

        workspace = expandHome(link.machine.workspace, link.state['info'])
        entry['cwd'] = workspace
        entry['flags'] = lance.get('flags') or []
        entry['arguments'] = arguments
        entry['run_script'] = posixpath.join(workspace, 'src', 'run.sh')
        entry['overlay_directory'] = posixpath.join(
            workspace, INSTALLED_CONFIG_DIRECTORY.replace(os.sep, '/'))
        entry['display'] = ' '.join(
            ['./src/run.sh'] + entry['flags']
            + ['%s:=%s' % item for item in arguments.items()]
            + (['json_path:=<overlay>'] if entry['overlay'] else []))




# --- Running ------------------------------------------------------------------



def newRunId() -> str:
    return time.strftime('%Y%m%d-%H%M%S') + '-%04x' % random.randrange(65536)


class Run:
    """One launch: a scenario, a deploy or a bag pull. Process states come
    from the agents; the run orders starting and stopping.

    Plan entries are either services (stay running; dependents start once
    they've run for start_delay_s) or tasks ("task": true; dependents start
    once they've exited successfully, e.g. a push before its build)."""

    def __init__(self, hub, run_id: str, label: str, scenario_id: str,
                 plan: Optional[dict] = None, kind: str = 'launch'):
        self.hub = hub
        self.run_id = run_id
        self.label = label
        self.scenario_id = scenario_id
        self.plan = plan
        self.kind = kind
        # A failed launch takes down what it started; a failed deploy only
        # skips the steps that needed the failed one.
        self.stop_on_failure = kind == 'launch'
        self.started_s = time.time()
        self.error = None
        self.is_starting = False
        self.is_stopping = False
        self.has_failed = False
        # key -> {"machine_id", "process_id", "label", "task", "record"}
        self.processes = {}
        self.task = None

    def snapshot(self) -> dict:
        processes = list(self.processes.values())
        states = [entry['record']['state'] if entry['record'] else 'pending'
                  for entry in processes]
        failed_tasks = [entry for entry in processes if entry.get('task')
                        and entry['record'] and entry['record']['state']
                        == 'exited' and entry['record']['exit_code'] != 0]
        if self.is_starting:
            state = 'starting'
        elif any(state in ('running', 'starting', 'stopping')
                 for state in states):
            state = 'stopping' if self.is_stopping else 'running'
        else:
            state = 'failed' if self.error or failed_tasks else 'finished'
        return {
            'run_id': self.run_id,
            'label': self.label,
            'kind': self.kind,
            'scenario_id': self.scenario_id,
            'started_s': self.started_s,
            'state': state,
            'error': self.error,
            'processes': [dict(entry, key=key)
                          for key, entry in self.processes.items()],
            'plan': self.plan,
        }

    def publish(self):
        self.hub.publish('run', self.snapshot())

    def processId(self, key: str) -> str:
        return self.run_id + '.' + key

    # --- Starting ---

    def start(self):
        self.is_starting = True
        for entry in self.plan['processes']:
            self.processes[entry['key']] = {
                'machine_id': entry['machine_id'],
                'process_id': self.processId(entry['key']),
                'label': entry['label'],
                'task': bool(entry.get('task')),
                'record': None,
            }
        self.publish()
        self.task = asyncio.ensure_future(self.startAll())

    async def startAll(self):
        """Start every entry as soon as its own dependencies allow, so
        independent chains (pushes to two machines) run side by side."""
        results = await asyncio.gather(
            *(self.startWhenReady(entry) for entry in self.plan['processes']),
            return_exceptions=True)
        errors = [result for result in results
                  if isinstance(result, Exception)]
        self.is_starting = False
        if errors:
            self.error = '; '.join(sorted({
                str(error) or type(error).__name__ for error in errors}))
            if self.stop_on_failure:
                await self.stop()
        self.publish()

    async def startWhenReady(self, entry):
        try:
            await self.waitForDependencies(entry)
            await self.startProcess(entry)
            self.publish()
        except (AgentError, ConnectionError, asyncio.TimeoutError,
                PlanError) as error:
            self.has_failed = True
            raise PlanError('%s: %s' % (entry['label'],
                                        str(error) or type(error).__name__))

    async def waitForDependencies(self, entry):
        deadline = time.time() + DEPENDENCY_TIMEOUT_S
        for dependency in entry['after']:
            dependency_entry = self.processes[dependency]
            while not self.isDependencyMet(dependency_entry, entry):
                if self.has_failed and self.stop_on_failure:
                    raise PlanError('not started: another process failed')
                if not dependency_entry['task'] and time.time() > deadline:
                    raise PlanError('timed out waiting for '
                                    + dependency_entry['label'])
                await asyncio.sleep(0.2)

    @staticmethod
    def isDependencyMet(dependency, entry) -> bool:
        record = dependency['record']
        if record is None:
            return False
        if dependency['task']:
            if record['state'] != 'exited':
                return False
            if record['exit_code'] != 0:
                raise PlanError('skipped: %s failed' % dependency['label'])
            return True
        if record['state'] == 'exited':
            raise PlanError('%s exited first' % dependency['label'])
        return record['state'] == 'running' and \
            time.time() - record['started_s'] >= entry['start_delay_s']

    async def startProcess(self, entry):
        link = self.hub.links_by_id[entry['machine_id']]
        if 'run_script' in entry:
            argv = [entry['run_script']] + entry['flags'] + [
                '%s:=%s' % item for item in entry['arguments'].items()]
            if entry['overlay']:
                path = posixpath.join(
                    entry['overlay_directory'],
                    'csm-hub-%s.json' % self.processId(entry['key']))
                written = await link.call('file.write', {
                    'path': path, 'content': overlayJson(entry['overlay'])})
                argv.append('json_path:=' + written['path'])
        else:
            argv = entry['argv']
        record = await link.call('process.start', {
            'process_id': self.processId(entry['key']),
            'label': entry['label'],
            'argv': argv,
            'cwd': entry['cwd'],
            'env': entry.get('env') or {},
            'meta': {'run_id': self.run_id, 'run_label': self.label,
                     'scenario_id': self.scenario_id, 'key': entry['key'],
                     'kind': self.kind, 'task': bool(entry.get('task'))},
        })
        self.processes[entry['key']]['record'] = record

    # --- Stopping ---

    async def stop(self):
        '''Stop in reverse start order, so the robot goes down after the
        driver station that commands it.'''
        self.is_stopping = True
        self.publish()
        # By actual start time: a run recovered after a hub restart lists its
        # processes in the order the machines reconnected.
        newest_first = sorted(
            self.processes, reverse=True,
            key=lambda key: (self.processes[key]['record'] or {}).get(
                'started_s') or 0)
        for key in newest_first:
            entry = self.processes[key]
            record = entry['record']
            if not record or record['state'] == 'exited':
                continue
            link = self.hub.links_by_id.get(entry['machine_id'])
            if link is None or link.connection is None:
                continue
            try:
                await link.call('process.stop',
                                {'process_id': entry['process_id']})
            except (AgentError, ConnectionError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(STOP_SPACING_S)

    # --- Agent updates ---

    def onProcessState(self, machine_id: str, record: dict):
        key = record['meta'].get('key', record['process_id'])
        entry = self.processes.setdefault(key, {
            'machine_id': machine_id, 'process_id': record['process_id'],
            'label': record['label'],
            'task': bool(record['meta'].get('task')), 'record': None})
        entry['record'] = record
        self.publish()
