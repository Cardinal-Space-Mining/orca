'''
The hub: owns one MachineLink per machine and fans their updates out to the
browser windows that are listening.
'''

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from typing import Optional

from .inventory import Machine
from .link import MachineLink


HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_SOURCE_PATH = os.path.join(HUB_ROOT, 'agent', 'csm_agent.py')
SUBSCRIBER_QUEUE_SIZE = 1000


class Hub:

    def __init__(self, machines: list, workspace_root: Optional[str],
                 inventory_paths: list):
        self.workspace_root = workspace_root
        self.inventory_paths = inventory_paths
        self.machines_by_id = {machine.id: machine for machine in machines}
        self.links = [MachineLink(self, machine) for machine in machines]
        self.links_by_id = {link.machine.id: link for link in self.links}
        self.subscribers = set()  # asyncio.Queue per browser event stream

        # --- Agent copy that gets installed on every machine ---
        self.agent_path = AGENT_SOURCE_PATH
        with open(AGENT_SOURCE_PATH, 'rb') as file:
            self.agent_bytes = file.read()
        # Same hash the agent computes for itself.
        self.agent_hash = hashlib.sha256(self.agent_bytes).hexdigest()[:12]

    async def run(self):
        await asyncio.gather(*(link.run() for link in self.links))

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
        }

    # --- Commands ---

    def reconnect(self, machine_id: str) -> bool:
        link = self.links_by_id.get(machine_id)
        if link is None:
            return False
        link.requestReconnect()
        return True
