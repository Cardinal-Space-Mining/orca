'''The agent end to end: a real daemon in a temporary directory, reached
through `attach` exactly as the orchestrator does over SSH.'''

import asyncio
import json
import os
import sys
import tempfile
import unittest

from backend.core import AGENT_SOURCE_PATH


class AgentClient:

    def __init__(self, process):
        self.process = process
        self.next_id = 1
        self.events = []

    async def receive(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 10)
        if not line:
            raise EOFError('agent closed the connection')
        return json.loads(line)

    async def call(self, method, **params):
        request_id = self.next_id
        self.next_id += 1
        self.process.stdin.write((json.dumps(
            {'id': request_id, 'method': method, 'params': params})
            + '\n').encode())
        await self.process.stdin.drain()
        while True:
            message = await self.receive()
            if message.get('id') == request_id:
                if 'error' in message:
                    raise RuntimeError(message['error']['message'])
                return message['result']
            self.events.append(message)

    async def waitForEvent(self, predicate):
        for message in self.events:
            if predicate(message):
                return message
        while True:
            message = await self.receive()
            self.events.append(message)
            if predicate(message):
                return message


class AgentTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='orch-test-')
        environment = dict(os.environ,
                           ORCHESTRATOR_AGENT_DIR=self.directory.name)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, '-u', AGENT_SOURCE_PATH, 'attach',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            env=environment)
        self.client = AgentClient(self.process)

    async def asyncTearDown(self):
        try:
            await self.client.call('agent.shutdown', force=True)
        finally:
            self.process.stdin.close()
            await asyncio.wait_for(self.process.wait(), 10)
            # Give the daemon a moment to remove its socket and lock.
            await asyncio.sleep(0.5)
            self.directory.cleanup()

    async def test_hello_and_ping(self):
        hello = await self.client.waitForEvent(
            lambda message: message.get('event') == 'hello')
        self.assertIn('agent_hash', hello['data'])
        self.assertIsNotNone(await self.client.call('ping'))

    async def test_process_output_and_exit(self):
        await self.client.call('process.watch', enabled=True)
        record = await self.client.call(
            'process.start', process_id='test-echo', label='echo',
            argv=['/bin/sh', '-c', 'echo first; echo second; exit 3'],
            cwd=self.directory.name, env={}, meta={'run': 'test'})
        self.assertEqual(record['process_id'], 'test-echo')
        final = await self.client.waitForEvent(
            lambda message: message.get('event') == 'process.state'
            and message['data']['state'] == 'exited')
        self.assertEqual(final['data']['exit_code'], 3)
        self.assertEqual(final['data']['meta'], {'run': 'test'})
        output = await self.client.call('process.output',
                                        process_id='test-echo')
        self.assertEqual(output['lines'][:2], ['first', 'second'])

    async def test_unknown_method(self):
        with self.assertRaises(RuntimeError):
            await self.client.call('no.such.method')


if __name__ == '__main__':
    unittest.main()
