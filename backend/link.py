'''
Links from the orchestrator to each machine's agent.

A MachineLink keeps one agent connection alive: it tries the machine's routes
in order, installs or updates the agent when needed, polls status while
connected, and reconnects with back-off when the connection drops.
'''

from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
import time
from typing import Optional

from .budget import MONITOR_PROCESS_ID
from .inventory import Machine, Route
from .ssh import (ATTACH_COMMAND, DEPLOY_COMMAND, keyscanCommand,
                  knownHostsName, sshCommand)


FIRST_EVENT_TIMEOUT_S = 20.0
CALL_TIMEOUT_S = 15.0
DEPLOY_TIMEOUT_S = 30.0
RECONNECT_DELAYS_S = [2, 5, 10, 20]
# Polling periods: (normal, competition mode). Competition mode keeps the
# orchestrator's own traffic on the robot's link small.
STATS_INTERVAL_S = (2.0, 10.0)
PING_PERIOD_S = (5.0, 15.0)
VM_PERIOD_S = (5.0, 15.0)
SHARING_PERIOD_S = (5.0, 15.0)
WORKSPACE_PERIOD_S = (30.0, 120.0)
STREAM_LIMIT_BYTES = 16 * 1024 * 1024
ROUTE_STAGGER_S = 0.4
ROUTE_UPGRADE_PERIOD_S = 60.0
MIRROR_LINES = 5000


class AgentError(Exception):
    '''The agent answered a request with an error.'''


# SSH's error messages, shortened for the machine cards (most specific first).
SSH_REASONS = [
    ('Host key verification failed', 'unknown host key'),
    ('REMOTE HOST IDENTIFICATION HAS CHANGED', 'host key changed'),
    ('Permission denied', 'key not accepted'),
    ('Network is unreachable', 'network unreachable'),
    ('No route to host', 'no route'),
    ('Connection timed out', 'timed out'),
    ('Connection refused', 'SSH refused'),
    ('Could not resolve hostname', 'name not found'),
    ('Connection closed', 'connection closed'),
]


def shortReason(error) -> str:
    for pattern, reason in SSH_REASONS:
        if pattern in (error or ''):
            return reason
    return error or 'no response'



# --- Agent connection ---------------------------------------------------------



class AgentConnection:
    '''One running `agent attach` process (over SSH, or local) and the
    JSON-lines requests and events that pass through it.'''

    def __init__(self, process, on_event):
        self.process = process
        self.on_event = on_event
        self.pending = {}  # request id -> future
        self.next_id = 1
        self.first_event = asyncio.get_running_loop().create_future()
        self.closed = asyncio.Event()
        self.stderr_lines = collections.deque(maxlen=20)
        # Payload bytes through this connection, for the orchestrator's own
        # traffic.
        self.bytes_received = 0
        self.bytes_sent = 0
        self.reader_task = asyncio.ensure_future(self.readStdout())
        self.stderr_task = asyncio.ensure_future(self.readStderr())

    @classmethod
    async def start(cls, command: list, on_event) -> 'AgentConnection':
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT_BYTES)
        return cls(process, on_event)

    async def readStdout(self):
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            self.bytes_received += len(line)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            self.dispatch(message)
        self.failPending('connection closed')
        if not self.first_event.done():
            self.first_event.set_result(None)
        self.closed.set()

    async def readStderr(self):
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            text = line.decode(errors='replace').strip()
            if text:
                self.stderr_lines.append(text)

    def dispatch(self, message: dict):
        if 'event' in message:
            if not self.first_event.done():
                self.first_event.set_result(message)
            else:
                self.on_event(message['event'], message.get('data'))
            return
        future = self.pending.pop(message.get('id'), None)
        if future is None or future.done():
            return
        if 'error' in message:
            future.set_exception(AgentError(message['error'].get('message')))
        else:
            future.set_result(message.get('result'))

    def failPending(self, reason: str):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))
        self.pending.clear()

    async def waitFirstEvent(self, timeout_s: float) -> Optional[dict]:
        try:
            return await asyncio.wait_for(
                asyncio.shield(self.first_event), timeout_s)
        except asyncio.TimeoutError:
            return None

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout_s: float = CALL_TIMEOUT_S):
        if self.closed.is_set():
            raise ConnectionError('connection closed')
        request_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        data = (json.dumps({'id': request_id, 'method': method,
                            'params': params or {}}) + '\n').encode()
        self.bytes_sent += len(data)
        try:
            self.process.stdin.write(data)
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout_s)
        finally:
            self.pending.pop(request_id, None)

    def lastError(self) -> Optional[str]:
        return self.stderr_lines[-1] if self.stderr_lines else None

    async def close(self):
        if self.process.returncode is None:
            try:
                self.process.stdin.close()
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5.0)
            except asyncio.TimeoutError:
                self.process.kill()
        self.reader_task.cancel()
        self.stderr_task.cancel()



# --- Process output mirror ----------------------------------------------------



class ProcessMirror:
    """The orchestrator's copy of one agent process: its latest record and
    recent output lines, numbered as the agent numbers them."""

    def __init__(self, record: dict):
        self.record = record
        self.lines = collections.deque(maxlen=MIRROR_LINES)
        self.first_line_number = 0

    def nextLineNumber(self) -> int:
        return self.first_line_number + len(self.lines)

    def merge(self, first: int, lines: list):
        """Add a block of lines, which may overlap what's already here (live
        events and a backlog fetch can cross)."""
        end = first + len(lines)
        mine_first = self.first_line_number
        mine_end = self.nextLineNumber()
        if not self.lines or first > mine_end:
            combined_first, combined = first, list(lines)
        elif end < mine_first:
            return  # older than anything kept
        elif first == mine_end:
            combined_first, combined = mine_first, None  # plain append
        else:
            combined_first = min(first, mine_first)
            combined = list(lines[:max(0, mine_first - first)])
            combined.extend(self.lines)
            if end > mine_end:
                combined.extend(lines[mine_end - first:])
        if combined is None:
            overflow = max(0, len(self.lines) + len(lines) - MIRROR_LINES)
            self.lines.extend(lines)
            self.first_line_number += overflow
            return
        overflow = max(0, len(combined) - MIRROR_LINES)
        self.first_line_number = combined_first + overflow
        self.lines = collections.deque(combined[overflow:],
                                       maxlen=MIRROR_LINES)

    def output(self, from_line: int) -> dict:
        start = max(from_line, self.first_line_number)
        lines = list(self.lines)[start - self.first_line_number:]
        return {'first_line_number': start, 'lines': lines,
                'next_line_number': self.nextLineNumber()}



# --- Machine link -------------------------------------------------------------



class MachineLink:
    """Keeps the orchestrator connected to one machine and holds its latest
    state."""

    def __init__(self, orchestrator, machine: Machine):
        self.orchestrator = orchestrator
        self.machine = machine
        self.connection: Optional[AgentConnection] = None
        self.active_route: Optional[Route] = None
        self.wake_event = asyncio.Event()
        self.processes = {}  # process id -> ProcessMirror
        self.terminals = set()  # ids of terminals open on this connection
        self.task = None
        self.traffic_sample = None  # (time, bytes, connection)
        self.previous_counters = {}  # interface -> (time_s, rx, tx)
        self.state = {
            'connection': {'state': 'idle', 'route': None, 'error': None,
                           'since_s': time.time(), 'retry_at_s': None,
                           'rtt_ms': None, 'clock_offset_ms': None,
                           'agent_hash': None, 'update_pending': False,
                           'unknown_host_key': None, 'error_details': None,
                           'orchestrator_kbps': None},
            'info': None,
            'stats': None,
            'workspace': None,
            'vm': None,
            'sharing': None,
        }

    # --- State ---

    def snapshot(self) -> dict:
        return dict(self.state, machine=self.machine.toJson(),
                    addresses=self.orchestrator.finder.book.snapshot(
                        self.machine.id))

    def publish(self):
        self.orchestrator.publish('machine', self.snapshot())

    def setConnection(self, **changes):
        connection = self.state['connection']
        if 'state' in changes and changes['state'] != connection['state']:
            connection['since_s'] = time.time()
        connection.update(changes)
        self.publish()

    def start(self):
        self.task = asyncio.ensure_future(self.run())

    async def stop(self):
        '''Disconnect for good (the machine left the inventory).'''
        if self.task:
            self.task.cancel()
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def requestReconnect(self):
        """Drop the current connection (if any) and retry immediately."""
        self.wake_event.set()
        if self.connection is not None:
            asyncio.ensure_future(self.connection.close())

    def jumpRoute(self) -> Optional[Route]:
        """Route for other machines to jump through: the live one if
        connected, else the first listed."""
        if self.active_route is not None:
            return self.active_route
        return self.machine.routes[0] if self.machine.routes else None

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout_s: float = CALL_TIMEOUT_S):
        if self.connection is None:
            raise ConnectionError(self.machine.label + ' is not connected')
        return await self.connection.call(method, params, timeout_s)

    # --- Main loop ---

    async def run(self):
        num_failures = 0
        while True:
            self.wake_event.clear()
            if await self.connectAnyRoute():
                num_failures = 0
                await self.serve()
            else:
                num_failures += 1
            delay_s = RECONNECT_DELAYS_S[
                min(num_failures, len(RECONNECT_DELAYS_S) - 1)]
            self.setConnection(state='offline',
                               retry_at_s=time.time() + delay_s)
            try:
                await asyncio.wait_for(self.wake_event.wait(), delay_s)
            except asyncio.TimeoutError:
                pass

    async def candidateRoutes(self) -> list:
        '''Routes to try, best first: the inventory's direct routes (in its
        order), then addresses found at run time (addresses.py), then jumps
        through other machines (a fallback, e.g. the mac's host-only link to
        its VM).'''
        if self.machine.is_local:
            return [None]  # None: run the agent directly
        direct = [route for route in self.machine.routes if not route.via]
        jumps = [route for route in self.machine.routes if route.via]
        return (direct
                + await self.orchestrator.finder.foundRoutes(self.machine)
                + jumps)

    async def connectAnyRoute(self) -> bool:
        finder = self.orchestrator.finder
        routes = await self.candidateRoutes()
        if not routes:
            self.setConnection(
                state='connecting', retry_at_s=None, unknown_host_key=None,
                route=None, error='no address known yet: add a route, or '
                'connect it once so its addresses are remembered')
            return False
        self.setConnection(
            state='connecting', retry_at_s=None, unknown_host_key=None,
            route=self.describeRoutes(routes))
        winner = await self.firstReachable(routes)
        if winner is None:
            if finder.book.macs(self.machine.id):
                finder.requestDiscovery()
            return False
        route, connection, first = winner
        self.setConnection(route=route.describe() if route else 'local')
        connection = await self.open(route, connection, first)
        if connection is None:
            return False
        self.connection = connection
        self.active_route = route
        return True

    @staticmethod
    def describeRoutes(routes) -> str:
        if routes == [None]:
            return 'local'
        if len(routes) == 1:
            return routes[0].describe()
        return 'trying %d routes' % len(routes)

    # --- Choosing a route ---

    async def firstReachable(self, routes):
        """Try every route, each a little after the previous one so earlier
        (preferred) routes win when several work. Returns (route, connection,
        first event) for the first route whose agent answers, or None."""
        attempts = [asyncio.ensure_future(self.probe(route, index))
                    for index, route in enumerate(routes)]
        winner = None
        errors = []
        pending = set(attempts)
        while pending and winner is None:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for task in sorted(done, key=attempts.index):
                route, connection, first, error = task.result()
                if connection is not None and winner is None:
                    winner = (route, connection, first)
                elif connection is not None:
                    await connection.close()
                else:
                    errors.append((route, error))
        for task in pending:
            task.cancel()
        if winner is None:
            self.recordRouteErrors(errors)
        return winner

    async def probe(self, route: Optional[Route], index: int):
        await asyncio.sleep(index * ROUTE_STAGGER_S)
        try:
            connection = await AgentConnection.start(
                self.attachCommand(route), self.onEvent)
        except OSError as error:
            return route, None, None, 'could not start ssh: %s' % error
        try:
            first = await connection.waitFirstEvent(FIRST_EVENT_TIMEOUT_S)
        except asyncio.CancelledError:
            await connection.close()
            raise
        if first is None:
            await asyncio.sleep(0.2)  # let stderr catch up
            error = connection.lastError() or 'no response from agent'
            await connection.close()
            return route, None, None, error
        return route, connection, first, None

    def recordRouteErrors(self, errors):
        unknown_key = next((route for route, error in errors
                            if route and 'Host key verification failed'
                            in (error or '')), None)
        reasons = ['%s: %s' % (route.host if route else 'local',
                               shortReason(error))
                   for route, error in errors]
        details = '\n'.join('%s: %s' % (
            route.describe() if route else 'local', error)
            for route, error in errors)
        self.setConnection(
            error=' · '.join(reasons), error_details=details,
            unknown_host_key=unknown_key.describe() if unknown_key else None)

    def attachCommand(self, route: Optional[Route]) -> list:
        if route is None:
            return [sys.executable, '-u', self.orchestrator.agent_path,
                    'attach']
        return sshCommand(route, self.orchestrator.jumpRouteFor,
                          ATTACH_COMMAND)

    # --- Agent install and update ---

    async def open(self, route, connection, first):
        """Turn a reachable route into a ready connection, installing or
        restarting the agent as needed. Returns None on failure (error
        recorded)."""
        for _ in range(3):
            action = await self.checkFirstEvent(first, connection, route)
            if action == 'ready':
                return connection
            await connection.close()
            if action == 'failed':
                return None
            connection = await AgentConnection.start(
                self.attachCommand(route), self.onEvent)
            first = await connection.waitFirstEvent(FIRST_EVENT_TIMEOUT_S)
        await connection.close()
        self.setConnection(error='agent did not come up after updating')
        return None

    async def checkFirstEvent(self, first, connection, route) -> str:
        """Decide what to do after the agent's first message:
        "ready", "retry" (agent was installed or restarted), or "failed"."""
        if first is None:
            await asyncio.sleep(0.2)  # let stderr catch up
            self.setConnection(
                error=connection.lastError() or 'no response from agent')
            return 'failed'
        if first['event'] == 'agent-missing':
            return 'retry' if await self.deploy(route) else 'failed'
        if first['event'] != 'hello':
            self.setConnection(error=(first.get('data') or {}).get(
                'message', 'unexpected reply: ' + first['event']))
            return 'failed'

        hello = first['data']
        if hello['agent_hash'] == self.orchestrator.agent_hash:
            self.setConnection(agent_hash=hello['agent_hash'],
                               update_pending=False, error=None)
            return 'ready'
        return await self.updateAgent(hello, connection, route)

    async def updateAgent(self, hello, connection, route) -> str:
        """The agent runs different code than the orchestrator's copy: replace
        the file if it differs, then restart the daemon unless it's busy."""
        if route is not None \
                and hello['file_hash'] != self.orchestrator.agent_hash:
            if not await self.deploy(route):
                return 'failed'
        if not hello['is_idle']:
            # Running processes belong to this daemon; update it later.
            self.setConnection(agent_hash=hello['agent_hash'],
                               update_pending=True, error=None)
            return 'ready'
        self.setConnection(state='updating agent')
        try:
            await connection.call('agent.shutdown')
        except (AgentError, ConnectionError, asyncio.TimeoutError):
            pass
        try:
            await asyncio.wait_for(connection.closed.wait(), 10.0)
        except asyncio.TimeoutError:
            pass  # open() closes the connection either way
        return 'retry'

    async def deploy(self, route: Optional[Route]) -> bool:
        if route is None:
            return True
        self.setConnection(state='installing agent')
        process = await asyncio.create_subprocess_exec(
            *sshCommand(route, self.orchestrator.jumpRouteFor, DEPLOY_COMMAND),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(self.orchestrator.agent_bytes),
                DEPLOY_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            self.setConnection(error='timed out copying the agent')
            return False
        if process.returncode != 0:
            self.setConnection(error='could not copy the agent: '
                               + stderr.decode(errors='replace').strip())
            return False
        return True

    # --- Host keys ---

    def unknownKeyRoute(self) -> Optional[Route]:
        described = self.state['connection']['unknown_host_key']
        return next((route for route in self.machine.routes
                     if route.describe() == described), None)

    async def scanHostKey(self) -> dict:
        """Fetch the host keys of the route that failed verification, for the
        user to compare before trusting them."""
        route = self.unknownKeyRoute()
        if route is None:
            raise ValueError('no route is waiting for a host key')
        process = await asyncio.create_subprocess_exec(
            *keyscanCommand(route, self.orchestrator.jumpRouteFor),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(process.communicate(), 20.0)
        keys = [line for line in output.decode().splitlines()
                if line and not line.startswith('#')]
        if not keys:
            raise ValueError('could not read host keys from ' + route.host)
        fingerprint = await asyncio.create_subprocess_exec(
            'ssh-keygen', '-l', '-f', '-', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE)
        fingerprints, _ = await fingerprint.communicate(
            '\n'.join(keys).encode() + b'\n')
        return {'route': route.describe(), 'name': knownHostsName(route),
                'keys': keys,
                'fingerprints': fingerprints.decode().splitlines()}

    async def trustHostKey(self, expected_fingerprints: list) -> None:
        """Add the scanned keys to ~/.ssh/known_hosts under the route's name,
        but only if they're still the ones the user saw."""
        scanned = await self.scanHostKey()
        if sorted(scanned['fingerprints']) != sorted(expected_fingerprints):
            raise ValueError('the host keys changed since they were shown')
        name = scanned['name']
        lines = []
        for key in scanned['keys']:
            _, key_type, key_data = key.split(None, 2)
            lines.append('%s %s %s\n' % (name, key_type, key_data))
        known_hosts = os.path.expanduser('~/.ssh/known_hosts')
        os.makedirs(os.path.dirname(known_hosts), mode=0o700, exist_ok=True)
        with open(known_hosts, 'a') as file:
            file.writelines(lines)
        self.orchestrator.finder.forgetKnownHosts()
        self.requestReconnect()

    # --- While connected ---

    async def serve(self):
        connection = self.connection
        self.setConnection(state='online', error=None, error_details=None,
                           unknown_host_key=None)
        tasks = [asyncio.ensure_future(task) for task in self.pollTasks()]
        try:
            self.state['info'] = await connection.call('system.info')
            await connection.call(
                'subscribe',
                {'stats_interval_s': self.period(STATS_INTERVAL_S)})
            self.publish()
            await self.syncProcesses(connection)
            await connection.closed.wait()
        except (AgentError, ConnectionError, asyncio.TimeoutError) as error:
            self.setConnection(error=str(error) or type(error).__name__)
        finally:
            for task in tasks:
                task.cancel()
            await connection.close()
            self.connection = None
            self.endTerminals()
            if not self.state['connection']['error']:
                self.setConnection(
                    error=connection.lastError() or 'connection lost')

    async def syncProcesses(self, connection):
        """Catch up after (re)connecting: watch live output first, then fetch
        whatever was printed while the orchestrator was away."""
        await connection.call('process.watch', self.watchParams())
        for record in await connection.call('process.list'):
            self.onProcessState(record)
            mirror = self.processes[record['process_id']]
            backlog = await connection.call('process.output', {
                'process_id': record['process_id'],
                'from_line': mirror.nextLineNumber()})
            self.onProcessOutput({
                'process_id': record['process_id'],
                'first_line_number': backlog['first_line_number'],
                'lines': backlog['lines']})

    def period(self, periods: tuple) -> float:
        competition = self.orchestrator.settings['competition_mode']
        return periods[1 if competition else 0]

    def pollTasks(self) -> list:
        tasks = [self.poll(PING_PERIOD_S, self.measureLatency),
                 self.poll(SHARING_PERIOD_S, self.refreshSharing)]
        if not self.machine.is_local:
            tasks.append(self.preferEarlierRoutes())
        if self.machine.workspace:
            tasks.append(self.poll(WORKSPACE_PERIOD_S, self.refreshWorkspace))
        if self.machine.vm:
            tasks.append(self.poll(VM_PERIOD_S, self.refreshVm))
        return tasks

    async def poll(self, periods: tuple, refresh):
        while True:
            try:
                await refresh()
            except (AgentError, ConnectionError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(self.period(periods))

    async def applySettings(self):
        '''Competition mode changed: re-subscribe at the new rate and watch
        output accordingly.'''
        if self.connection is None:
            return
        try:
            await self.call('subscribe', {
                'stats_interval_s': self.period(STATS_INTERVAL_S)})
            await self.call('process.watch', self.watchParams())
        except (AgentError, ConnectionError, asyncio.TimeoutError):
            pass

    def watchParams(self) -> dict:
        '''Live output from every process, except in competition mode, where
        other machines only stream the orchestrator's monitor (output can still
        be fetched on demand).'''
        if self.machine.is_local \
                or not self.orchestrator.settings['competition_mode']:
            return {'enabled': True}
        return {'enabled': False, 'process_ids': [MONITOR_PROCESS_ID]}

    async def preferEarlierRoutes(self):
        '''Connected through a fallback (e.g. the jump through the mac
        because the VM's own network wasn't up yet at boot)? Move back to a
        preferred route once it answers. Not while terminals are open: they
        live on this connection. Supervised processes are unaffected.'''
        while True:
            await asyncio.sleep(ROUTE_UPGRADE_PERIOD_S)
            if self.terminals or self.active_route is None:
                continue
            routes = await self.candidateRoutes()
            # Same address and jump: the same route, even if it was found a
            # different way this time (neighbour vs learned).
            position = next((index for index, route in enumerate(routes)
                             if route.host == self.active_route.host
                             and route.via == self.active_route.via),
                            len(routes))
            for route in routes[:position]:
                _, connection, _, _ = await self.probe(route, 0)
                if connection is not None:
                    await connection.close()
                    self.requestReconnect()
                    return

    async def measureLatency(self):
        sent_s = time.time()
        reply = await self.call('ping')
        received_s = time.time()
        # Assumes the request and the reply take equally long, as NTP does.
        offset_s = reply['time_s'] - (sent_s + received_s) / 2.0
        self.setConnection(rtt_ms=(received_s - sent_s) * 1000.0,
                           clock_offset_ms=offset_s * 1000.0,
                           orchestrator_kbps=self.orchestratorTrafficKbps(
                               received_s))

    def orchestratorTrafficKbps(self, now_s: float):
        '''The orchestrator's own payload traffic with this machine since the
        last call (SSH adds a little on top).'''
        connection = self.connection
        total = connection.bytes_received + connection.bytes_sent
        previous = self.traffic_sample
        self.traffic_sample = (now_s, total, connection)
        if previous is None or previous[2] is not connection \
                or now_s <= previous[0]:
            return None
        return (total - previous[1]) * 8.0 / (now_s - previous[0]) / 1000.0

    async def refreshWorkspace(self):
        self.state['workspace'] = await self.call(
            'workspace.status', {'path': self.machine.workspace})
        self.publish()

    async def refreshSharing(self):
        await self.orchestrator.sharing.refresh(self)

    async def refreshVm(self):
        self.state['vm'] = await self.call(
            'vm.status', {'csm_vm_path': self.machine.vm.csm_vm_path})
        self.publish()

    # --- Agent events ---

    def onEvent(self, name: str, data):
        if name == 'stats':
            self.addInterfaceRates(data)
            self.state['stats'] = data
            self.orchestrator.budget.onStats(self.machine.id, data)
            if not self.machine.is_local:
                self.orchestrator.finder.onStats(self.machine.id, data,
                                                 self.active_route)
            self.publish()
        elif name == 'process.state':
            self.onProcessState(data)
        elif name == 'process.output':
            self.onProcessOutput(data)
        elif name == 'terminal.output':
            self.orchestrator.publish('terminal.output',
                                      dict(data, machine_id=self.machine.id))
        elif name == 'terminal.exit':
            self.terminals.discard(data['terminal_id'])
            self.orchestrator.publish('terminal.exit',
                                      dict(data, machine_id=self.machine.id))

    def addInterfaceRates(self, stats: dict):
        '''Bits per second per interface, from the byte counters of this and
        the previous sample.'''
        counters = {}
        for entry in stats.get('interfaces', []):
            if entry.get('rx_bytes') is None:
                continue
            counters[entry['name']] = (stats['time_s'], entry['rx_bytes'],
                                       entry['tx_bytes'])
            previous = self.previous_counters.get(entry['name'])
            if previous is None or stats['time_s'] <= previous[0]:
                continue
            period_s = stats['time_s'] - previous[0]
            received = entry['rx_bytes'] - previous[1]
            sent = entry['tx_bytes'] - previous[2]
            if received < 0 or sent < 0:
                continue  # counters reset (interface re-plugged)
            entry['rx_bps'] = received * 8.0 / period_s
            entry['tx_bps'] = sent * 8.0 / period_s
            entry['period_s'] = period_s
        self.previous_counters = counters

    def endTerminals(self):
        '''Terminals live on the connection; tell the browser they ended.'''
        for terminal_id in list(self.terminals):
            self.orchestrator.publish('terminal.exit', {
                'machine_id': self.machine.id, 'terminal_id': terminal_id,
                'exit_code': None, 'reason': 'connection lost'})
        self.terminals.clear()

    def onProcessState(self, record: dict):
        mirror = self.processes.get(record['process_id'])
        if mirror is None:
            mirror = self.processes[record['process_id']] = \
                ProcessMirror(record)
        mirror.record = record
        self.orchestrator.onProcessState(self.machine.id, record)

    def onProcessOutput(self, data: dict):
        mirror = self.processes.get(data['process_id'])
        if mirror is None or not data['lines']:
            return
        mirror.merge(data['first_line_number'], data['lines'])
        if data['process_id'] == MONITOR_PROCESS_ID:
            self.orchestrator.budget.onMonitorLines(data['lines'])
            return
        self.orchestrator.publish('output',
                                  dict(data, machine_id=self.machine.id))
