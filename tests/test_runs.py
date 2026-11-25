'''Starting and stopping a run against fake agents.'''

import asyncio
import time
import unittest

from backend.runs import Run


class FakeLink:
    '''Answers process.start / process.stop like an agent, at once.'''

    def __init__(self, run_holder):
        self.connection = object()
        self.run_holder = run_holder
        self.started = []
        self.stopped = []

    async def call(self, method, params, timeout_s=None):
        await asyncio.sleep(0.01)
        if method == 'process.start':
            self.started.append(params['process_id'])
            return {'process_id': params['process_id'], 'state': 'running',
                    'started_s': time.time(), 'exit_code': None,
                    'meta': params['meta'], 'label': params['label']}
        if method == 'process.stop':
            self.stopped.append(params['process_id'])
            run = self.run_holder[0]
            for entry in run.processes.values():
                if entry['process_id'] == params['process_id']:
                    entry['record'] = dict(entry['record'], state='exited',
                                           exit_code=0)
            return {}
        raise AssertionError(method)


class FakeOrca:

    def __init__(self):
        self.run_holder = []
        self.link = FakeLink(self.run_holder)
        self.links_by_id = {'robot': self.link}

    def publish(self, event, data):
        pass


def entry(key, after=(), delay=0.0):
    return {'key': key, 'label': key, 'machine_id': 'robot',
            'after': list(after), 'start_delay_s': delay,
            'argv': ['true'], 'cwd': '~'}


class RunTest(unittest.TestCase):

    def runPlan(self, processes, stop_after_s=None):
        async def main():
            orca = FakeOrca()
            run = Run(orca, 'r1', 'test', 'scenario',
                      {'processes': processes})
            orca.run_holder.append(run)
            run.start()
            if stop_after_s is not None:
                await asyncio.sleep(stop_after_s)
                await run.stop()
            await run.task
            return orca.link, run.snapshot()
        return asyncio.run(main())

    def test_starts_in_dependency_order(self):
        link, snapshot = self.runPlan([entry('robot'),
                                       entry('client', ['robot'])])
        self.assertEqual(link.started, ['r1.robot', 'r1.client'])
        self.assertEqual(snapshot['state'], 'running')

    def test_stop_while_starting_starts_nothing_more(self):
        link, snapshot = self.runPlan(
            [entry('robot'), entry('client', ['robot'], delay=0.5)],
            stop_after_s=0.1)
        self.assertEqual(link.started, ['r1.robot'])
        self.assertEqual(link.stopped, ['r1.robot'])
        self.assertIsNone(snapshot['error'])
        self.assertEqual(snapshot['state'], 'finished')


if __name__ == '__main__':
    unittest.main()
