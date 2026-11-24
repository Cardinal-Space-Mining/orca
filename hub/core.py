'''
The hub: owns one MachineLink per machine, the runs it has started, and fans
every update out to the browser windows that are listening.
'''

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from typing import Optional

from .inventory import Route
from .lance_config import ConfigError, LanceConfig
from .link import MachineLink
from .runs import PlanError, Planner, Run, newRunId
from .scenarios import loadScenarios


HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_SOURCE_PATH = os.path.join(HUB_ROOT, 'agent', 'csm_agent.py')
SUBSCRIBER_QUEUE_SIZE = 5000
MAX_FINISHED_RUNS = 20


class Hub:

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
        self.lance_config = None
        self.lance_config_error = None

        # --- Agent copy that gets installed on every machine ---
        self.agent_path = AGENT_SOURCE_PATH
        with open(AGENT_SOURCE_PATH, 'rb') as file:
            self.agent_bytes = file.read()
        # Same hash the agent computes for itself.
        self.agent_hash = hashlib.sha256(self.agent_bytes).hexdigest()[:12]

    async def run(self):
        await asyncio.gather(*(link.run() for link in self.links))

    # --- Lookups ---

    def jumpRouteFor(self, machine_id: str) -> Route:
        return self.links_by_id[machine_id].jumpRoute()

    def referenceHead(self) -> Optional[str]:
        '''Commit of the hub's own workspace (what previews are based on).'''
        local = self.links_by_id.get('local')
        workspace = local.state['workspace'] if local else None
        return workspace.get('head') if workspace else None

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
            'hub': {
                'hostname': socket.gethostname(),
                'workspace': self.workspace_root,
                'inventory_paths': [
                    {'path': path, 'exists': os.path.isfile(path)}
                    for path in self.inventory_paths],
                'agent_hash': self.agent_hash,
            },
            'machines': [link.snapshot() for link in self.links],
            'runs': [run.snapshot() for run in self.runs.values()],
        }

    # --- Machines ---

    def link(self, machine_id: str) -> MachineLink:
        link = self.links_by_id.get(machine_id)
        if link is None:
            raise PlanError('no machine ' + machine_id)
        return link

    def reconnect(self, machine_id: str):
        self.link(machine_id).requestReconnect()

    # --- Runs ---

    def plan(self, request: dict) -> dict:
        return self.planner.plan(self.scenario(request['scenario_id']),
                                 request)

    def launch(self, request: dict) -> dict:
        plan = self.plan(request)
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
        '''Route an agent's process update to its run, recreating runs the
        hub doesn't know (it restarted while they were running).'''
        meta = record.get('meta') or {}
        run_id = meta.get('run_id')
        if not run_id:
            return
        run = self.runs.get(run_id)
        if run is None:
            if record['state'] == 'exited':
                return  # an old run; nothing to recover
            run = Run(self, run_id, meta.get('run_label') or run_id,
                      meta.get('scenario_id'))
            self.runs[run_id] = run
        run.onProcessState(machine_id, record)

    async def stopProcess(self, machine_id: str, process_id: str):
        await self.link(machine_id).call('process.stop',
                                         {'process_id': process_id})

    def processOutput(self, machine_id: str, process_id: str,
                      from_line: int) -> dict:
        mirror = self.link(machine_id).processes.get(process_id)
        if mirror is None:
            raise PlanError('no process ' + process_id)
        return mirror.output(from_line)
