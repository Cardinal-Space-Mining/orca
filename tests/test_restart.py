'''Restarting a machine from its card, against fake links: a VM host shuts
its VM down first and starts it again once it's back.'''

import asyncio
import types
import unittest
from unittest import mock

from backend import restart
from backend.restart import Restarter


class FakeRun:

    def __init__(self, outcome):
        self.outcome = outcome
        self.polls = 0

    def snapshot(self):
        self.polls += 1  # done on the second look
        return {'state': self.outcome if self.polls > 1 else 'running'}


class FakeLink:

    def __init__(self, machine_id='mac', vm_running=True, sudo_ok=True):
        self.machine = types.SimpleNamespace(
            id=machine_id, label=machine_id, is_local=False,
            vm=types.SimpleNamespace(guest='vm') if vm_running is not None
            else None)
        self.connection = object()
        self.state = {'connection': {'state': 'online'}, 'restart': None,
                      'vm': {'is_running': bool(vm_running)}}
        self.sudo_ok = sudo_ok
        self.calls = []
        self.phases = []
        self.vm_after_boot = False

    async def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if not self.sudo_ok:
            return {'ok': False, 'needs_password': True,
                    'command': ['sudo', '/sbin/shutdown', '-r', 'now']}
        if not (params or {}).get('check_only'):
            asyncio.ensure_future(self.reboot())
        return {'ok': True}

    async def reboot(self):
        await asyncio.sleep(0.05)
        self.state['connection']['state'] = 'offline'
        await asyncio.sleep(0.05)
        self.state['connection']['state'] = 'online'

    async def refreshVm(self):
        self.state['vm'] = {'is_running': self.vm_after_boot}

    def publish(self):
        restart_state = self.state['restart']
        if restart_state and (not self.phases
                              or self.phases[-1] != restart_state['phase']):
            self.phases.append(restart_state['phase'])

    def requestReconnect(self):
        pass


class FakeVm:

    def __init__(self, stop_outcome='finished'):
        self.actions = []
        self.stop_outcome = stop_outcome

    def run(self, machine_id, action, options):
        self.actions.append((action, options))
        return FakeRun(self.stop_outcome if action == 'stop' else 'finished')

    def scriptPath(self, link):
        return '/Users/me/code/csm-vm/run-vm.sh'


class FakeOrca:

    def __init__(self, link, vm=None):
        self.links = {link.machine.id: link}
        self.vm = vm or FakeVm()

    def link(self, machine_id):
        return self.links[machine_id]


FAST = dict(POLL_S=0.01, WAKE_PERIOD_S=0.01, DONE_SHOWN_S=0)


class RestartTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        patcher = mock.patch.multiple(restart, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_vm_host_stops_restarts_and_starts_its_vm(self):
        link = FakeLink()
        orca = FakeOrca(link)
        restarter = Restarter(orca)
        self.assertEqual(await restarter.restart('mac'), {'ok': True})
        await restarter.tasks['mac']
        self.assertEqual([action for action, _ in orca.vm.actions],
                         ['stop', 'start'])
        self.assertEqual(orca.vm.actions[1][1], {'overlay': 'reuse'})
        self.assertEqual(link.calls, [
            ('machine.reboot', {'check_only': True}),
            ('machine.reboot', {})])
        self.assertEqual(link.phases, [
            'Shutting down the VM', 'Restarting',
            'Waiting for it to come back', 'Starting the VM', 'Restarted'])
        self.assertIsNone(link.state['restart'])  # cleared after a while

    async def test_plain_machine_just_restarts(self):
        link = FakeLink(vm_running=None)
        orca = FakeOrca(link)
        restarter = Restarter(orca)
        await restarter.restart('mac')
        await restarter.tasks['mac']
        self.assertEqual(orca.vm.actions, [])
        self.assertEqual(link.phases[-1], 'Restarted')

    async def test_needs_a_password(self):
        link = FakeLink(sudo_ok=False)
        restarter = Restarter(FakeOrca(link))
        self.assertEqual(await restarter.restart('mac'),
                         {'needs_password': True})
        self.assertNotIn('mac', restarter.tasks)

    async def test_vm_that_wont_stop_stops_the_restart(self):
        link = FakeLink()
        orca = FakeOrca(link, FakeVm(stop_outcome='failed'))
        restarter = Restarter(orca)
        await restarter.restart('mac')
        await restarter.tasks['mac']
        self.assertEqual(link.calls, [
            ('machine.reboot', {'check_only': True})])  # never restarted
        self.assertEqual(link.state['restart']['phase'], 'Restart failed')
        self.assertIn("didn't shut down", link.state['restart']['error'])

    async def test_terminal_restart_stops_the_vm_in_the_terminal(self):
        link = FakeLink(sudo_ok=False)
        orca = FakeOrca(link)
        restarter = Restarter(orca)
        argv = await restarter.terminalArgv('mac')
        self.assertEqual(argv[:2], ['/bin/sh', '-c'])
        self.assertIn('/Users/me/code/csm-vm/run-vm.sh --stopd && sudo '
                      '/sbin/shutdown -r now', argv[2])
        await link.reboot()  # the user typed the password
        await restarter.tasks['mac']
        # The terminal shut the VM down; Orca starts it again.
        self.assertEqual([action for action, _ in orca.vm.actions],
                         ['start'])
        self.assertEqual(link.phases[-1], 'Restarted')

    async def test_local_machine_is_refused(self):
        link = FakeLink()
        link.machine.is_local = True
        with self.assertRaises(restart.PlanError):
            await Restarter(FakeOrca(link)).restart('mac')


if __name__ == '__main__':
    unittest.main()
