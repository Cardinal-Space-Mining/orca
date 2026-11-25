'''
Orca: owns one MachineLink per machine, the runs it has started,
and fans every update out to the browser windows that are listening.
'''

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import shlex
import socket
from typing import Optional

from .addresses import Finder
from .inventory import (Route, inventoryScopes, loadInventory,
                        readInventoryRaw, writeInventory)
from .config_edit import DESCRIPTIONS_RELATIVE_PATH
from .lance_config import (CONFIG_RELATIVE_PATH, INSTALLED_CONFIG_DIRECTORY,
                           ConfigError, LanceConfig)
from .budget import MONITOR_PROCESS_ID, BudgetTracker
from .deploy import Deployer
from .discovery import Discovery
from .link import MachineLink
from .network_map import DeviceBook, buildNetworkModel
from .runs import PlanError, Planner, Run, expandHome, newRunId
from .settings import (SETTINGS_PATH, loadSettings, mergeDefaults,
                       saveSettings, validateChanges)
from .scenarios import (SCENARIO_DIRECTORY, loadScenarios,
                        parseScenarioText)
from .setup import Setup
from .sharing import Sharing
from .vm import VmController


ORCA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_SOURCE_PATH = os.path.join(ORCA_ROOT, 'agent',
                                 'orca_agent.py')
MONITOR_SOURCE_PATH = os.path.join(ORCA_ROOT, 'backend',
                                   'ros_monitor.py')
SUBSCRIBER_QUEUE_SIZE = 5000
NETWORK_PUBLISH_PERIOD_S = 2.0
MAX_FINISHED_RUNS = 20


class Orca:

    def __init__(self, machines: list, workspace_root: Optional[str],
                 inventory_paths: list):
        self.workspace_root = workspace_root
        self.inventory_paths = inventory_paths
        self.machines_by_id = {machine.id: machine for machine in machines}
        self.links = [MachineLink(self, machine) for machine in machines]
        self.links_by_id = {link.machine.id: link for link in self.links}
        self.subscribers = set()  # asyncio.Queue per browser event stream
        self.runs = {}  # run id -> Run
        self.planner = Planner(self)
        self.deployer = Deployer(self)
        self.vm = VmController(self)
        self.stop_event = None
        self.settings = loadSettings()
        self.budget = BudgetTracker(self)
        self.finder = Finder(self)
        self.sharing = Sharing(self)
        self.discovery = Discovery(self)
        self.setup = Setup(self)
        self.devices = DeviceBook()
        self.network_json = None
        self.lance_config = None
        self.lance_config_error = None

        # --- Agent copy that gets installed on every machine ---
        self.agent_path = AGENT_SOURCE_PATH
        with open(AGENT_SOURCE_PATH, 'rb') as file:
            self.agent_bytes = file.read()
        # Same hash the agent computes for itself.
        self.agent_hash = hashlib.sha256(self.agent_bytes).hexdigest()[:12]

    async def run(self):
        self.stop_event = asyncio.Event()
        for link in self.links:
            link.start()
        publisher = asyncio.ensure_future(self.publishNetwork())
        try:
            await self.stop_event.wait()
        finally:
            publisher.cancel()

    # --- The network map ---

    def noteScan(self, machine_id: str, result: dict):
        '''Devices a scan found go on the map.'''
        for host in result.get('hosts', []):
            self.devices.see(host.get('mac'), host['address'], 'scan',
                             host.get('name'), machine_id)

    def networkModel(self) -> dict:
        try:
            config = self.lanceConfig()
            constants = config.addressConstants() if config else {}
        except ConfigError:
            constants = {}
        return buildNetworkModel(
            [link.snapshot() for link in self.links],
            self.devices.snapshot(), constants,
            self.settings['budget'].get('interface'))

    async def publishNetwork(self):
        '''Send the map when it changes (traffic rates change it every few
        seconds while anything is moving).'''
        while True:
            await asyncio.sleep(NETWORK_PUBLISH_PERIOD_S)
            try:
                model = self.networkModel()
            except Exception as error:  # keep the loop alive; show why
                print('[ORCA]: network map failed: %r' % error,
                      flush=True)
                continue
            encoded = json.dumps(model, sort_keys=True)
            if encoded != self.network_json:
                self.network_json = encoded
                self.publish('network', model)

    # --- Lookups ---

    def jumpRouteFor(self, machine_id: str) -> Optional[Route]:
        return self.links_by_id[machine_id].jumpRoute()

    def referenceHead(self) -> Optional[str]:
        '''Commit of Orca's own workspace (what previews are based
        on).'''
        local = self.links_by_id.get('local')
        workspace = local.state['workspace'] if local else None
        return workspace.get('head') if workspace else None

    def referenceFingerprint(self) -> Optional[str]:
        '''Code fingerprint of Orca's own workspace.'''
        local = self.links_by_id.get('local')
        workspace = local.state['workspace'] if local else None
        return workspace.get('fingerprint') if workspace else None

    def lanceConfig(self) -> Optional[LanceConfig]:
        if self.lance_config is None and self.workspace_root:
            try:
                self.lance_config = LanceConfig(self.workspace_root)
            except ConfigError as error:
                self.lance_config_error = str(error)
        return self.lance_config

    def scenarios(self) -> tuple:
        return loadScenarios(self.workspace_root)

    def scenario(self, scenario_id: str) -> dict:
        scenarios, _ = self.scenarios()
        for scenario in scenarios:
            if scenario['id'] == scenario_id:
                return scenario
        raise PlanError('no scenario ' + scenario_id)

    # --- Events ---

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self.subscribers.discard(queue)

    def publish(self, event: str, data):
        for queue in list(self.subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                # A stalled browser tab; it reloads the snapshot on reconnect.
                self.subscribers.discard(queue)

    def snapshot(self) -> dict:
        return {
            'orca': {
                'hostname': socket.gethostname(),
                'workspace': self.workspace_root,
                'inventory_paths': [
                    {'path': path, 'exists': os.path.isfile(path)}
                    for path in self.inventory_paths],
                'agent_hash': self.agent_hash,
                'files': self.files(),
            },
            'machines': [link.snapshot() for link in self.links],
            'runs': [run.snapshot() for run in self.runs.values()],
            'settings': self.settings,
            'budget': self.budget.snapshot(with_history=True),
            'network': self.networkModel(),
        }

    # --- Machines ---

    def link(self, machine_id: str) -> MachineLink:
        link = self.links_by_id.get(machine_id)
        if link is None:
            raise PlanError('no machine ' + machine_id)
        return link

    def reconnect(self, machine_id: str):
        self.link(machine_id).requestReconnect()

    # --- Inventory ---

    def inventory(self) -> dict:
        return {scope: readInventoryRaw(path) for scope, path
                in inventoryScopes(self.workspace_root).items()}

    async def saveInventory(self, scope: str, entries: list) -> dict:
        path = writeInventory(self.workspace_root, scope, entries)
        await self.reloadInventory()
        return {'path': path}

    async def reloadInventory(self):
        '''Apply the inventory files without restarting: new machines get a
        link, removed ones are disconnected, changed ones reconnect.'''
        machines = loadInventory(self.workspace_root)
        new_ids = [machine.id for machine in machines]
        for link in list(self.links):
            if link.machine.id not in new_ids:
                await link.stop()
                self.finder.book.forget(link.machine.id)
                self.finder.book.saveSoon(asyncio.get_running_loop())
                self.publish('machine.removed', {'id': link.machine.id})
        links = []
        for machine in machines:
            link = self.links_by_id.get(machine.id)
            if link is None:
                link = MachineLink(self, machine)
                link.start()
            elif link.machine != machine:
                link.machine = machine
                link.active_route = None
                link.requestReconnect()
            links.append(link)
        self.links = links
        self.links_by_id = {link.machine.id: link for link in links}
        self.machines_by_id = {machine.id: machine for machine in machines}
        for link in links:
            link.publish()
        self.publish('machines.order', {'ids': new_ids})

    # --- Settings ---

    async def updateSettings(self, changes: dict) -> dict:
        validateChanges(changes)
        was_competition = self.settings['competition_mode']
        self.settings = mergeDefaults(changes, self.settings)
        saveSettings(self.settings)
        self.publish('settings', self.settings)
        if self.settings['competition_mode'] != was_competition:
            await asyncio.gather(*(link.applySettings()
                                   for link in self.links))
        self.budget.publish()
        return self.settings

    def forget(self, what: str, machine_id: str) -> dict:
        """Forget a machine's remembered sharing choices or learned
        addresses (the Settings page)."""
        if what == 'sharing':
            self.settings['sharing'].pop(machine_id, None)
            saveSettings(self.settings)
            self.publish('settings', self.settings)
        elif what == 'addresses':
            self.finder.book.forget(machine_id)
            self.finder.book.saveSoon(asyncio.get_running_loop())
            link = self.links_by_id.get(machine_id)
            if link:
                link.publish()
        else:
            raise PlanError('nothing called "%s" to forget' % what)
        return {'ok': True}

    def files(self) -> list:
        """The files Orca reads and writes, for the Settings
        page."""
        workspace = self.workspace_root
        entries = [
            ('Settings (this page)', SETTINGS_PATH),
            ('Learned machine addresses', self.finder.book.path),
        ] + [('Machine inventory (%s)' % scope, path) for scope, path
             in sorted(inventoryScopes(workspace).items())]
        if workspace:
            entries += [
                ('Scenarios', os.path.join(workspace, SCENARIO_DIRECTORY)),
                ('Preset descriptions', os.path.join(
                    workspace, DESCRIPTIONS_RELATIVE_PATH)),
                ('Launch config', os.path.join(workspace,
                                               CONFIG_RELATIVE_PATH)),
            ]
        return [{'label': label, 'path': path,
                 'exists': os.path.exists(path)} for label, path in entries]

    # --- net_adapter monitor (for the bandwidth budget) ---

    async def startMonitor(self, machine_id: str) -> dict:
        '''Run ros_monitor.py on a machine that runs a net_adapter endpoint,
        with ROS and its workspace sourced and the DDS settings of
        `run.sh --local`.'''
        link = self.link(machine_id)
        workspace = link.state['workspace'] or {}
        if not link.machine.workspace or not workspace.get('has_install'):
            raise PlanError('%s has no built workspace' % link.machine.label)
        await self.stopMonitor()
        info = link.state['info'] or {}
        root = expandHome(link.machine.workspace, info)
        distro = workspace.get('ros_distro') or (
            workspace.get('ros_distros') or [None])[-1]
        if not distro:
            raise PlanError('no ROS found on %s' % link.machine.label)
        with open(MONITOR_SOURCE_PATH) as file:
            source = file.read()
        script = await link.call('file.write', {
            'path': '~/.orca/ros_monitor.py', 'content': source})
        command = 'source %s && source %s && ' % (
            shlex.quote('/opt/ros/%s/setup.bash' % distro),
            shlex.quote(root + '/install/setup.bash'))
        if self.settings['budget']['monitor_local_dds']:
            command += ('export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp '
                        'CYCLONEDDS_URI=%s && ' % shlex.quote(
                            'file://%s/src/project/cyclonedds.xml' % root))
        command += 'exec python3 -u ' + shlex.quote(script['path'])
        await link.call('process.start', {
            'process_id': MONITOR_PROCESS_ID, 'label': 'Bandwidth monitor',
            'argv': ['bash', '-c', command], 'cwd': root,
            'meta': {'kind': 'monitor'}})
        self.budget.setMonitorState(machine_id, 'starting')
        await self.updateSettings({'budget': {'monitor_machine_id':
                                              machine_id}})
        return self.budget.monitor

    async def stopMonitor(self) -> dict:
        '''Stop the monitor wherever it runs, and wait until it has exited
        (a new one can't start while it's still shutting down).'''
        for link in self.links:
            mirror = link.processes.get(MONITOR_PROCESS_ID)
            if link.connection is None or mirror is None \
                    or mirror.record['state'] == 'exited':
                continue
            await link.call('process.stop', {
                'process_id': MONITOR_PROCESS_ID, 'grace_s': 3})
            for _ in range(100):
                if mirror.record['state'] == 'exited':
                    break
                await asyncio.sleep(0.1)
        self.budget.setMonitorState(None, 'stopped')
        return self.budget.monitor

    def onMonitorState(self, machine_id: str, record: dict):
        if record['state'] != 'exited':
            if self.budget.monitor['state'] == 'stopped':
                self.budget.setMonitorState(machine_id, 'starting')
            return
        if self.budget.monitor.get('machine_id') != machine_id:
            return
        mirror = self.links_by_id[machine_id].processes.get(
            MONITOR_PROCESS_ID)
        last_lines = [line for line in (mirror.lines if mirror else [])
                      if line.strip()][-3:]
        self.budget.setMonitorState(
            machine_id, 'stopped', None if record['exit_code'] in (0, None)
            and not record['exit_signal'] else ' / '.join(last_lines)
            or 'the monitor exited')

    # --- Terminals ---

    async def openTerminal(self, machine_id: str, kind: str, columns: int,
                           rows: int, options: Optional[dict] = None) -> dict:
        link = self.link(machine_id)
        params = {'cols': columns, 'rows': rows}
        if kind == 'serial':
            params['argv'] = self.vm.serialConsoleArgv(machine_id)
            guest = self.links_by_id.get(link.machine.vm.guest or '')
            title = 'Serial console · %s' % (
                guest.machine.label if guest else link.machine.label)
        elif kind == 'share-install':
            params['argv'] = await self.sharing.installCommand(machine_id)
            title = 'Install sharing helper · %s' % link.machine.label
        elif kind == 'setup':
            check_id = str((options or {}).get('check'))
            params['argv'] = await self.setup.fixCommand(link, check_id)
            title = 'Set up %s · %s' % (check_id, link.machine.label)
        elif kind == 'install-key':
            target = (options or {}).get('target') or {}
            params['argv'] = self.discovery.keyCommand(target)
            title = 'Install SSH key · %s' % target.get('address')
        else:
            params['cwd'] = link.machine.workspace or '~'
            title = 'Shell · %s' % link.machine.label
        opened = await link.call('terminal.open', params)
        link.terminals.add(opened['terminal_id'])
        if kind in ('setup', 'share-install'):
            link.setup_terminals.add(opened['terminal_id'])
        return {'machine_id': machine_id, 'terminal_id':
                opened['terminal_id'], 'title': title, 'kind': kind}

    async def terminalCall(self, machine_id: str, method: str,
                           params: dict):
        return await self.link(machine_id).call(method, params)

    # --- Runs ---

    def plan(self, request: dict) -> dict:
        '''request["source"] plans an unsaved draft from the scenario
        editor instead of the saved file.'''
        if request.get('source') is not None:
            scenario = parseScenarioText(request['scenario_id'],
                                         request['source'])
        else:
            scenario = self.scenario(request['scenario_id'])
        return self.planner.plan(scenario, request)

    def launch(self, request: dict) -> dict:
        plan = self.plan(dict(request, source=None))
        if plan['errors']:
            raise PlanError('; '.join(plan['errors']))
        run = Run(self, newRunId(), plan['label'], plan['scenario_id'], plan)
        self.runs[run.run_id] = run
        self.forgetOldRuns()
        run.start()
        return run.snapshot()

    async def stopRun(self, run_id: str):
        run = self.runs.get(run_id)
        if run is None:
            raise PlanError('no run ' + run_id)
        await run.stop()

    def dismissRun(self, run_id: str):
        run = self.runs.get(run_id)
        if run is not None and run.snapshot()['state'] in ('finished',
                                                           'failed'):
            del self.runs[run_id]
            self.publish('run.removed', {'run_id': run_id})

    def forgetOldRuns(self):
        finished = [run_id for run_id, run in self.runs.items()
                    if run.snapshot()['state'] in ('finished', 'failed')]
        for run_id in finished[:-MAX_FINISHED_RUNS]:
            del self.runs[run_id]

    def onProcessState(self, machine_id: str, record: dict):
        '''Route an agent's process update to its run, recreating runs
        Orca doesn't know (it restarted while they were running).'''
        if record['process_id'] == MONITOR_PROCESS_ID:
            self.onMonitorState(machine_id, record)
            return
        meta = record.get('meta') or {}
        run_id = meta.get('run_id')
        if not run_id:
            return
        run = self.runs.get(run_id)
        if run is None:
            if record['state'] == 'exited':
                return  # an old run; nothing to recover
            run = Run(self, run_id, meta.get('run_label') or run_id,
                      meta.get('scenario_id'), kind=meta.get('kind', 'launch'))
            self.runs[run_id] = run
        run.onProcessState(machine_id, record)

    # --- Logs ---

    async def listLogs(self, machine_id: str) -> list:
        link = self.link(machine_id)
        return await link.call('logs.list',
                               {'workspace': link.machine.workspace})

    async def readLog(self, machine_id: str, path: str,
                      max_bytes: int) -> dict:
        link = self.link(machine_id)
        return await link.call('logs.read', {
            'path': path, 'max_bytes': max_bytes,
            'workspace': link.machine.workspace}, timeout_s=60.0)

    async def deleteLog(self, machine_id: str, path: str) -> dict:
        link = self.link(machine_id)
        return await link.call('logs.delete', {
            'path': path, 'workspace': link.machine.workspace})

    # --- Cleanup ---

    def cleanupParams(self, link) -> dict:
        workspace = link.machine.workspace
        return {'workspace': workspace,
                'overlay_directory': posixpath.join(
                    workspace, INSTALLED_CONFIG_DIRECTORY.replace(os.sep, '/'))
                if workspace else None}

    async def cleanupScan(self, machine_id: str) -> dict:
        '''What could be deleted on a machine (Logs page), by category.'''
        link = self.link(machine_id)
        return await link.call('cleanup.scan', self.cleanupParams(link),
                               timeout_s=60.0)

    async def cleanup(self, machine_id: str, categories: list) -> dict:
        link = self.link(machine_id)
        return await link.call('cleanup.run', dict(
            self.cleanupParams(link), categories=list(categories)),
            timeout_s=120.0)

    async def stopProcess(self, machine_id: str, process_id: str):
        await self.link(machine_id).call('process.stop',
                                         {'process_id': process_id})

    async def processOutput(self, machine_id: str, process_id: str,
                            from_line: int, refresh: bool = False) -> dict:
        '''Buffered output. With refresh, first fetch what the agent has that
        Orca didn't stream (competition mode pauses streaming).'''
        link = self.link(machine_id)
        mirror = link.processes.get(process_id)
        if mirror is None:
            raise PlanError('no process ' + process_id)
        if refresh and link.connection is not None:
            backlog = await link.call('process.output', {
                'process_id': process_id,
                'from_line': mirror.nextLineNumber()})
            link.onProcessOutput({
                'process_id': process_id,
                'first_line_number': backlog['first_line_number'],
                'lines': backlog['lines']})
        return mirror.output(from_line)
