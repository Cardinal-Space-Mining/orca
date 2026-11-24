'''
Links from the hub to each machine's agent.

A MachineLink keeps one agent connection alive: it tries the machine's routes
in order, installs or updates the agent when needed, polls status while
connected, and reconnects with back-off when the connection drops.
'''

from __future__ import annotations

import asyncio
import collections
import json
import sys
import time
from typing import Optional

from .inventory import Machine, Route
from .ssh import ATTACH_COMMAND, DEPLOY_COMMAND, sshCommand


FIRST_EVENT_TIMEOUT_S = 20.0
CALL_TIMEOUT_S = 15.0
DEPLOY_TIMEOUT_S = 30.0
RECONNECT_DELAYS_S = [2, 5, 10, 20]
STATS_INTERVAL_S = 2.0
PING_PERIOD_S = 5.0
VM_PERIOD_S = 5.0
WORKSPACE_PERIOD_S = 30.0
STREAM_LIMIT_BYTES = 16 * 1024 * 1024


class AgentError(Exception):
    '''The agent answered a request with an error.'''



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
        line = json.dumps({'id': request_id, 'method': method,
                           'params': params or {}}) + '\n'
        try:
            self.process.stdin.write(line.encode())
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



# --- Machine link -------------------------------------------------------------



class MachineLink:
    '''Keeps the hub connected to one machine and holds its latest state.'''

    def __init__(self, hub, machine: Machine):
        self.hub = hub
        self.machine = machine
        self.connection: Optional[AgentConnection] = None
        self.wake_event = asyncio.Event()
        self.state = {
            'connection': {'state': 'idle', 'route': None, 'error': None,
                           'since_s': time.time(), 'retry_at_s': None,
                           'rtt_ms': None, 'clock_offset_ms': None,
                           'agent_hash': None, 'update_pending': False},
            'info': None,
            'stats': None,
            'workspace': None,
            'vm': None,
        }

    # --- State ---

    def snapshot(self) -> dict:
        return dict(self.state, machine=self.machine.toJson())

    def publish(self):
        self.hub.publish('machine', self.snapshot())

    def setConnection(self, **changes):
        connection = self.state['connection']
        if 'state' in changes and changes['state'] != connection['state']:
            connection['since_s'] = time.time()
        connection.update(changes)
        self.publish()

    def requestReconnect(self):
        '''Drop the current connection (if any) and retry immediately.'''
        self.wake_event.set()
        if self.connection is not None:
            asyncio.ensure_future(self.connection.close())

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

    async def connectAnyRoute(self) -> bool:
        routes = self.machine.routes or [None]  # None: the local machine
        for route in routes:
            self.setConnection(
                state='connecting', retry_at_s=None,
                route=route.describe() if route else 'local')
            connection = await self.open(route)
            if connection is not None:
                self.connection = connection
                return True
        return False

    def attachCommand(self, route: Optional[Route]) -> list:
        if route is None:
            return [sys.executable, '-u', self.hub.agent_path, 'attach']
        return sshCommand(route, self.hub.machines_by_id, ATTACH_COMMAND)

    async def open(self, route: Optional[Route]):
        '''Connect through one route, installing or restarting the agent as
        needed. Returns a ready connection, or None (error recorded).'''
        for _ in range(3):
            connection = await AgentConnection.start(
                self.attachCommand(route), self.onEvent)
            first = await connection.waitFirstEvent(FIRST_EVENT_TIMEOUT_S)
            action = await self.checkFirstEvent(first, connection, route)
            if action == 'ready':
                return connection
            await connection.close()
            if action == 'failed':
                return None
        self.setConnection(error='agent did not come up after updating')
        return None

    async def checkFirstEvent(self, first, connection, route) -> str:
        '''Decide what to do after the agent's first message:
        "ready", "retry" (agent was installed or restarted), or "failed".'''
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
        if hello['agent_hash'] == self.hub.agent_hash:
            self.setConnection(agent_hash=hello['agent_hash'],
                               update_pending=False, error=None)
            return 'ready'
        return await self.updateAgent(hello, connection, route)

    async def updateAgent(self, hello, connection, route) -> str:
        '''The agent runs different code than the hub's copy: replace the file
        if it differs, then restart the daemon unless it's busy.'''
        if route is not None and hello['file_hash'] != self.hub.agent_hash:
            if not await self.deploy(route):
                return 'failed'
        if not hello['is_idle']:
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
            *sshCommand(route, self.hub.machines_by_id, DEPLOY_COMMAND),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(self.hub.agent_bytes), DEPLOY_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            self.setConnection(error='timed out copying the agent')
            return False
        if process.returncode != 0:
            self.setConnection(error='could not copy the agent: '
                               + stderr.decode(errors='replace').strip())
            return False
        return True

    # --- While connected ---

    async def serve(self):
        connection = self.connection
        self.setConnection(state='online', error=None)
        tasks = [asyncio.ensure_future(task) for task in self.pollTasks()]
        try:
            self.state['info'] = await connection.call('system.info')
            await connection.call(
                'subscribe', {'stats_interval_s': STATS_INTERVAL_S})
            self.publish()
            await connection.closed.wait()
        except (AgentError, ConnectionError, asyncio.TimeoutError) as error:
            self.setConnection(error=str(error) or type(error).__name__)
        finally:
            for task in tasks:
                task.cancel()
            await connection.close()
            self.connection = None
            if not self.state['connection']['error']:
                self.setConnection(
                    error=connection.lastError() or 'connection lost')

    def pollTasks(self) -> list:
        tasks = [self.poll(PING_PERIOD_S, self.measureLatency)]
        if self.machine.workspace:
            tasks.append(self.poll(WORKSPACE_PERIOD_S, self.refreshWorkspace))
        if self.machine.vm:
            tasks.append(self.poll(VM_PERIOD_S, self.refreshVm))
        return tasks

    async def poll(self, period_s: float, refresh):
        while True:
            try:
                await refresh()
            except (AgentError, ConnectionError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(period_s)

    async def measureLatency(self):
        sent_s = time.time()
        reply = await self.connection.call('ping')
        received_s = time.time()
        # Assumes the request and the reply take equally long, as NTP does.
        offset_s = reply['time_s'] - (sent_s + received_s) / 2.0
        self.setConnection(rtt_ms=(received_s - sent_s) * 1000.0,
                           clock_offset_ms=offset_s * 1000.0)

    async def refreshWorkspace(self):
        self.state['workspace'] = await self.connection.call(
            'workspace.status', {'path': self.machine.workspace})
        self.publish()

    async def refreshVm(self):
        self.state['vm'] = await self.connection.call(
            'vm.status', {'csm_vm_path': self.machine.vm.csm_vm_path})
        self.publish()

    def onEvent(self, name: str, data):
        if name == 'stats':
            self.state['stats'] = data
            self.publish()
