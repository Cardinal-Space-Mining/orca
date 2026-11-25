'''The agent end to end: a real daemon in a temporary directory, reached
through `attach` exactly as Orca does over SSH.'''

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


class AgentHarness(unittest.IsolatedAsyncioTestCase):
    '''Starts a daemon in a temporary directory for each test.'''

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='orch-test-')
        environment = dict(os.environ,
                           ORCA_AGENT_DIR=self.directory.name)
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


class AgentTest(AgentHarness):

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

    async def test_exit_reported_after_last_output(self):
        await self.client.call(
            'process.start', process_id='test-burst', label='burst',
            argv=['/bin/sh', '-c', 'seq 1 200000'],
            cwd=self.directory.name, env={}, meta={})
        final = await self.client.waitForEvent(
            lambda message: message.get('event') == 'process.state'
            and message['data']['process_id'] == 'test-burst'
            and message['data']['state'] == 'exited')
        self.assertEqual(final['data']['num_lines'], 200000)

    async def test_unknown_method(self):
        with self.assertRaises(RuntimeError):
            await self.client.call('no.such.method')



class CleanupTest(AgentHarness):
    '''Cleanup with the daemon's home moved into a temporary folder too, so
    ~/.ros/log and ~/.orchestrator are the test's, never the real ones.'''

    async def asyncSetUp(self):
        self.home = tempfile.TemporaryDirectory(prefix='orca-home-')
        home = self.home.name
        self.workspace = os.path.join(home, 'ws')
        self.overlays = os.path.join(self.workspace, 'install', 'lance',
                                     'share', 'lance', 'config')
        os.makedirs(self.overlays)
        self.write(os.path.join(self.overlays, 'launch.json'))
        self.write(os.path.join(self.overlays, 'orca-old-run.robot.json'))
        self.in_use = os.path.join(self.overlays, 'orca-new-run.robot.json')
        self.write(self.in_use)
        ros_logs = os.path.join(home, '.ros', 'log')
        self.write(os.path.join(ros_logs, 'old_node.log'), age_s=86400)
        self.write(os.path.join(ros_logs, 'new_node.log'))
        for number in range(6):
            self.write(os.path.join(self.workspace, 'log',
                                    'build_2026-01-0%d' % (number + 1),
                                    'lance', 'stdout_stderr.log'))
        self.write(os.path.join(home, '.orchestrator', 'runs', 'x.log'))
        self.original_home = os.environ['HOME']
        os.environ['HOME'] = home  # inherited by the daemon
        try:
            await super().asyncSetUp()
        finally:
            os.environ['HOME'] = self.original_home

    async def asyncTearDown(self):
        try:
            await super().asyncTearDown()
        finally:
            self.home.cleanup()

    @staticmethod
    def write(path, age_s=0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as file:
            file.write('x' * 100)
        if age_s:
            stamp = os.path.getmtime(path) - age_s
            os.utime(path, (stamp, stamp))

    def params(self, **extra):
        return dict(workspace=self.workspace,
                    overlay_directory=self.overlays, **extra)

    async def test_cleanup_leaves_what_is_in_use(self):
        await self.client.call(
            'process.start', process_id='r1.robot', label='robot',
            argv=['/bin/sh', '-c', 'sleep 30', 'json_path:=' + self.in_use],
            cwd=self.home.name, env={}, meta={'run_id': 'r1'},
            remove_on_exit=[self.in_use])
        scan = await self.client.call('cleanup.scan', **self.params())
        counts = {category['id']: category['count']
                  for category in scan['categories']}
        self.assertEqual(counts, {'overlays': 1, 'run_logs': 0,
                                  'ros_logs': 1, 'build_logs': 2,
                                  'old_agent': 1})

        result = await self.client.call('cleanup.run', **self.params(
            categories=list(counts)))
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['removed']['build_logs']['count'], 2)
        home = self.home.name
        self.assertTrue(os.path.exists(self.in_use))
        self.assertTrue(os.path.exists(os.path.join(self.overlays,
                                                    'launch.json')))
        self.assertFalse(os.path.exists(os.path.join(
            self.overlays, 'orca-old-run.robot.json')))
        self.assertEqual(os.listdir(os.path.join(home, '.ros', 'log')),
                         ['new_node.log'])
        self.assertEqual(len(os.listdir(os.path.join(self.workspace, 'log'))),
                         4)
        self.assertFalse(os.path.exists(os.path.join(home, '.orchestrator')))

        # The running process's log can't go; its overlay goes when it ends.
        record = (await self.client.call('process.list'))[0]
        with self.assertRaises(RuntimeError):
            await self.client.call('logs.delete', path=record['log_path'])
        await self.client.call('process.stop', process_id='r1.robot',
                               grace_s=1)
        await self.client.waitForEvent(
            lambda message: message.get('event') == 'process.state'
            and message['data']['state'] == 'exited')
        self.assertFalse(os.path.exists(self.in_use))
        deleted = await self.client.call('logs.delete',
                                         path=record['log_path'])
        self.assertGreater(deleted['bytes'], 0)
        self.assertFalse(os.path.exists(os.path.dirname(record['log_path'])))

    async def test_only_logs_can_be_deleted(self):
        for path in [os.path.join(self.directory.name, 'agent.log'),
                     os.path.join(self.directory.name, 'agent.py'),
                     self.in_use,
                     os.path.join(self.directory.name, 'agent.sock'),
                     os.path.join(self.workspace, 'log', '..', 'install')]:
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError):
                    await self.client.call('logs.delete', path=path,
                                           workspace=self.workspace)
        old_build = os.path.join(self.workspace, 'log', 'build_2026-01-01',
                                 'lance', 'stdout_stderr.log')
        await self.client.call('logs.delete', path=old_build,
                               workspace=self.workspace)
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(
            old_build))))


if __name__ == '__main__':
    unittest.main()
